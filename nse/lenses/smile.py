"""Smile lens — the CURVATURE of the volatility surface, not its tilt.

`greeks.py` reads the 25-delta risk reversal: IV(call) - IV(put), the surface's
TILT. It measured no edge (RESEARCH_LEARNINGS section 3.5). This lens reads the
orthogonal moment — the BUTTERFLY:

    butterfly = (IV_25d_call + IV_25d_put) / 2  -  IV_atm

Tilt says which tail the market is paying up for. Curvature says how much it is
paying for BOTH tails relative to the middle — how convex the surface is. A
market can be flat-tilted and violently convex, or heavily skewed and flat. They
are different numbers and one failing says nothing about the other.

That distinction is why this lens exists despite sharing the greeks lens's raw
input, and it is also the honest caveat: SHARED INPUT MEANS POSSIBLE CORRELATION.
Both read the same IV column. The pairwise correlation must be measured before
both are given weight, exactly as it was for vwap and volume_oi, where a -0.769
correlation revealed one opinion wearing two hats (section 3.12).

CONVENTION, DECLARED BEFORE MEASUREMENT

High curvature = the market is paying for tails = crowded protection on both
sides = the index tends to stay pinned between them. So:

    high butterfly (rich wings)  -> NEUTRAL/fade, low directional conviction
    low  butterfly (cheap wings) -> the market is not paying for a move, and
                                    moves are underpriced -> follow the tilt

Direction therefore comes from the TILT, and this lens's job is to say how much
to trust it. If that reads as backwards after measurement, the honest conclusion
is "the other convention was right on this data" — ONE bit — not a licence to
flip and re-run. Same rule that governed vwap.

Greeks are NOT trusted under 2 DTE (OPTIONS_GREEKS_LEARNINGS section 3): stored
IV is up to 100% wrong there. This lens abstains rather than reading garbage.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from nse.lenses.base import BaseLens, Direction, LensVerdict, abstain
from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

#: Minimum quoted contracts per side before the surface means anything.
MIN_PER_SIDE = 4

#: Butterfly normalised by ATM IV — a pure ratio. MEASURED on TRAIN: the median
#: of 1,892 observations.
#:
#: The first version of this lens hardcoded 0.0, on the assumption that "cheap
#: wings" means wings below ATM. It does not: a normal equity smile has wings
#: ABOVE ATM, and butterfly_norm was positive in 97.7% of TRAIN observations.
#: The lens therefore called the wings rich almost always and returned NEUTRAL,
#: producing n=34 directional verdicts across three years — noise, not a
#: measurement.
#:
#: This is the SAME bug `greeks.py` already hit and fixed with
#: SKEW_NEUTRAL = -0.2098 (rr_norm negative in 98.4% of observations). Neutral
#: is wherever the market actually sits, never zero, and that has now cost two
#: lenses. If you add a third lens keyed on a normalised surface quantity,
#: measure its median BEFORE choosing the pivot.
BUTTERFLY_NEUTRAL: float = 0.0210

#: Ratio at which conviction saturates. MEASURED on TRAIN as the p90-p50 spread
#: (0.0686 - 0.0210), so full conviction means "as far from normal as the
#: richest decile", not a guessed constant.
BUTTERFLY_FULL_CONVICTION: float = 0.0476

#: Wings this far from ATM define the "25 delta" proxy. The archive has no
#: delta column, so moneyness stands in for it — stated because a proxy that
#: silently drifts from what it proxies is how a lens ends up measuring
#: something other than its own name.
WING_STRIKES = 4


class SmileLens(BaseLens):
    name = "smile"
    backtestable = True

    def _evaluate(self, snap: MarketSnapshot) -> LensVerdict:
        if not snap.greeks_trustworthy:
            return abstain(self.name,
                           f"{snap.dte:.2f} DTE — stored IV is unreliable this "
                           f"close to expiry")

        ce, pe = snap.ce(), snap.pe()
        if len(ce) < MIN_PER_SIDE or len(pe) < MIN_PER_SIDE:
            return abstain(self.name,
                           f"chain too thin ({len(ce)} CE / {len(pe)} PE)")

        step = snap.step
        atm_iv = _iv_at(snap, snap.atm)
        if atm_iv is None or atm_iv <= 0:
            return abstain(self.name, "no usable ATM implied vol")

        call_wing = _iv_at(snap, snap.atm + WING_STRIKES * step, "CE")
        put_wing = _iv_at(snap, snap.atm - WING_STRIKES * step, "PE")
        if call_wing is None or put_wing is None:
            return abstain(self.name, "wings not quoted — cannot read curvature")

        butterfly = (call_wing + put_wing) / 2.0 - atm_iv
        bf_norm = butterfly / atm_iv
        tilt = (call_wing - put_wing) / atm_iv

        # Curvature sets CONVICTION; tilt sets DIRECTION. See the docstring.
        cheap = (BUTTERFLY_NEUTRAL - bf_norm) / BUTTERFLY_FULL_CONVICTION
        conviction = float(np.clip(cheap, -1.0, 1.0))

        if abs(tilt) < 1e-9 or conviction <= 0:
            direction, confidence = Direction.NEUTRAL, 0.0
            why = ("wings rich — market is paying for both tails, expect pinning"
                   if conviction <= 0 else "no tilt to follow")
        else:
            direction = Direction.LONG if tilt > 0 else Direction.SHORT
            confidence = min(1.0, conviction * min(1.0, abs(tilt) / 0.05))
            why = (f"wings cheap (butterfly {bf_norm:+.4f} of ATM), "
                   f"tilt {tilt:+.4f} unopposed")

        return LensVerdict(
            lens=self.name,
            direction=direction,
            confidence=confidence,
            rationale=(f"ATM IV {atm_iv:.2%}, wings {call_wing:.2%}/{put_wing:.2%} "
                       f"— {why}"),
            features={
                "atm_iv": round(float(atm_iv), 6),
                "call_wing_iv": round(float(call_wing), 6),
                "put_wing_iv": round(float(put_wing), 6),
                "butterfly": round(float(butterfly), 6),
                "butterfly_norm": round(float(bf_norm), 6),
                "tilt": round(float(tilt), 6),
                "conviction_raw": round(float(conviction), 6),
                "wing_strikes": WING_STRIKES,
                "dte": round(snap.dte, 3),
                "spot": round(snap.spot, 2),
            },
        )


def _iv_at(snap: MarketSnapshot, strike: int,
           option_type: Optional[str] = None) -> Optional[float]:
    """Implied vol at a strike, as a decimal. None when not quoted.

    When `option_type` is None the two sides are averaged — at the money they
    should agree by put-call parity, and averaging is more robust than picking
    a side arbitrarily.
    """
    sides = (option_type,) if option_type else ("CE", "PE")
    vals = []
    for s in sides:
        row = snap.at(strike, s)
        if row is None:
            continue
        iv = row.get("iv")
        if iv is None or pd.isna(iv):
            continue
        iv = float(iv)
        # The archive stores IV in percent; the rest of the codebase works in
        # decimals. Converting at this boundary is the same fix applied to the
        # VIX/solver mismatch — do it once, here, not at every use site.
        if iv > 3.0:
            iv /= 100.0
        if 0.0 < iv < 5.0:
            vals.append(iv)
    return float(np.mean(vals)) if vals else None
