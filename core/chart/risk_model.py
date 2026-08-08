"""Risk sized from a MEASURED hit probability, not a flat fraction.

Given a probability p that a setup reaches target before stop, and a reward
ratio b = R:R, the growth-optimal fraction of capital to risk is Kelly:

    edge = p*b - (1 - p)
    f*   = edge / b  =  p - (1 - p)/b

Three things make raw Kelly dangerous here, and each has a guard.

1. IT ASSUMES p IS KNOWN. It is estimated, and Kelly is brutally asymmetric
   about estimation error — overbetting compounds losses far faster than
   underbetting costs growth. Standard defence is fractional Kelly, and the
   default here is a quarter.

2. IT WILL HAPPILY BET EVERYTHING. At p=0.6 with b=3, f* is 0.47 — risking 47%
   of capital on one trade. Which collides with the invariant from
   core/chart/sizing.py:

       liquidation headroom = capital / risk_amount = 1 / f

   f = 0.47 puts liquidation barely two stops away, so a single gap through the
   stop liquidates. MAX_RISK_FRACTION is therefore a hard cap derived from the
   headroom we insist on, not a preference.

3. IT SAYS NOTHING ABOUT COSTS. A setup can be genuinely positive-expectancy
   and still lose money after the flat brokerage and the spread, which is
   exactly what the Rs 20/order fee did at 1 lot. MIN_RISK_FRACTION exists so a
   trade is either big enough to matter or is not taken.

WHERE p COMES FROM

Not from here, and not from a constant someone found reasonable. It comes from
core/chart/outcomes.py: replay every sweep, record whether it hit target before
stop, fit on TRAIN, check on VALIDATE. Until that is done, `BaseRate` is the
only honest estimator — it returns the measured unconditional hit rate and
makes no claim to distinguish one setup from another.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

# Fraction of full Kelly. A quarter is the conventional allowance for the fact
# that p is estimated rather than known.
KELLY_FRACTION = 0.25

# Hard cap on risk per trade. Derived, not chosen: headroom = 1/f, so 0.05
# keeps liquidation at least 20 stop-distances away at any stop width.
MAX_RISK_FRACTION = 0.05

# Below this the trade is not worth its fixed costs.
MIN_RISK_FRACTION = 0.005

# Refuse to act on a probability fitted to fewer than this many outcomes.
MIN_SAMPLE_FOR_MODEL = 200


class ProbabilityModel(Protocol):
    """Anything that can estimate P(target before stop) for a setup."""

    n_fitted: int

    def probability(self, features: dict) -> Optional[float]: ...


@dataclass
class BaseRate:
    """Unconditional hit rate. The honest estimator before any model is fitted.

    Returns the same number for every setup, which is the point: it claims no
    ability to tell setups apart, so it cannot manufacture false conviction.
    Anything that beats it has to prove it does so out of sample.
    """

    rate: float
    n_fitted: int = 0

    def probability(self, features: dict) -> Optional[float]:
        if self.n_fitted < MIN_SAMPLE_FOR_MODEL:
            return None
        return self.rate


@dataclass
class RiskDecision:
    take: bool
    risk_fraction: float
    risk_amount: float
    probability: Optional[float]
    rr: float
    edge: float                  # expected R per unit risked
    kelly_full: float
    reason: str

    def as_dict(self) -> dict:
        return {"take": self.take,
                "risk_fraction": round(self.risk_fraction, 5),
                "risk_amount": round(self.risk_amount, 2),
                "probability": (None if self.probability is None
                                else round(self.probability, 4)),
                "rr": round(self.rr, 3), "edge": round(self.edge, 4),
                "kelly_full": round(self.kelly_full, 4), "reason": self.reason}


def decide_risk(capital: float, rr: float, probability: Optional[float], *,
                kelly_fraction: float = KELLY_FRACTION,
                max_fraction: float = MAX_RISK_FRACTION,
                min_fraction: float = MIN_RISK_FRACTION) -> RiskDecision:
    """How much to risk on one setup. Refuses when the edge is not there.

    `probability` of None means no calibrated estimate exists yet. That is
    treated as "do not trade", NOT as "assume the base rate" — a system that
    silently falls back to trading when its model is missing is a system that
    trades on nothing at all.
    """
    if capital <= 0 or rr <= 0:
        return RiskDecision(False, 0.0, 0.0, probability, rr, 0.0, 0.0,
                            "invalid capital or R:R")

    if probability is None:
        return RiskDecision(False, 0.0, 0.0, None, rr, 0.0, 0.0,
                            "no calibrated probability — refusing to size on a guess")

    p = max(0.0, min(1.0, probability))
    edge = p * rr - (1.0 - p)
    kelly = edge / rr

    if edge <= 0:
        return RiskDecision(False, 0.0, 0.0, p, rr, edge, kelly,
                            f"negative expectancy: p={p:.3f} at {rr:.2f}R needs "
                            f"p>{1/(1+rr):.3f}")

    f = kelly * kelly_fraction
    capped = ""
    if f > max_fraction:
        f, capped = max_fraction, f" (capped from {f:.3f} to hold liquidation " \
                                   f"{1/max_fraction:.0f} stops away)"
    if f < min_fraction:
        return RiskDecision(False, 0.0, 0.0, p, rr, edge, kelly,
                            f"edge too thin to clear fixed costs: "
                            f"f={f:.4f} < {min_fraction:.4f}")

    return RiskDecision(True, f, capital * f, p, rr, edge, kelly,
                        f"p={p:.3f} at {rr:.2f}R -> edge {edge:+.3f}R, "
                        f"quarter-Kelly {f:.4f} of capital{capped}")


def breakeven_probability(rr: float) -> float:
    """The p below which a setup at this R:R loses money. p* = 1/(1+b)."""
    return 1.0 / (1.0 + rr) if rr > 0 else 1.0
