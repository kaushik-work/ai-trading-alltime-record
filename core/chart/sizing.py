"""Risk-based sizing: the stop decides the size, not the leverage dial.

Fixed leverage and sweep-based stops are in direct conflict. A sweep stop sits
beyond the wick by construction, so it is WIDER than a tight technical stop —
and on a leveraged venue a wide stop at fixed leverage walks the position toward
liquidation. Measured on the current 20x setting:

    stop 0.7%  ->  6.8x room to liquidation   fine
    stop 1.5%  ->  3.2x room                  fine
    stop 2.5%  ->  1.9x room                  tight
    stop 4.5%  ->  1.1x room                  LIQUIDATED BEFORE THE STOP FIRES

That last row is the same failure already recorded for 200x in
core/risk_management.py: above roughly 75x the stop stops being reachable and
the only exit is liquidation, so the strategy's entire risk model quietly stops
applying.

The fix is to invert the dependency. Fix the RUPEES risked per trade, then
derive quantity from the stop distance. Leverage becomes an output. A tight stop
gets a big position, a wide stop gets a small one, and the loss when wrong is
the same either way — which is the only property that makes a run of losers
survivable.

    qty = risk_rupees / stop_distance_per_unit

The leverage cap remains as a second guard, because an extremely tight stop
would otherwise produce an enormous notional off a small risk budget.

THE INVARIANT THAT MAKES THIS WORK

Sizing this way produces a result worth stating plainly, verified numerically
across stop widths from 0.5% to 4%:

    liquidation headroom  =  capital / risk_amount

It does NOT depend on how wide the stop is. Because quantity shrinks in exact
proportion as the stop widens, the distance to liquidation measured in stops is
fixed by one number: the fraction of margin risked per trade.

    risk  5% of margin  ->  liquidation ~20 stops away
    risk 10% of margin  ->  ~10 stops
    risk 25% of margin  ->  ~4 stops

So the liquidation question, which looked like a leverage question, is really a
position-risk question. Keep per-trade risk to a few percent of margin and
liquidation stops being the binding constraint at any stop width. Which also
reframes the 20x setting: you do not choose it. It is what the tightest setups
produce, and it falls to ~1x on the widest ones, with the loss identical either
way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Maintenance margin on Delta BTC/ETH perps, verified in core/risk_management.py.
MAINTENANCE_MARGIN = 0.0025

# Never let derived leverage exceed this, whatever the stop says.
MAX_LEVERAGE = 20

# Require the liquidation price to sit at least this many multiples of the stop
# distance away. 2.0 means the market must travel twice as far past the stop
# before liquidation — headroom for a gap or a bad fill.
MIN_LIQUIDATION_HEADROOM = 2.0


@dataclass
class Position:
    qty: float                  # units of the underlying
    notional: float
    leverage: float
    margin: float
    risk_amount: float          # what is lost if the stop fills exactly
    stop_distance: float
    stop_pct: float
    liquidation_price: Optional[float]
    liquidation_headroom: Optional[float]   # in multiples of the stop distance
    capped_by: Optional[str] = None

    @property
    def safe(self) -> bool:
        return (self.qty > 0 and
                (self.liquidation_headroom is None
                 or self.liquidation_headroom >= MIN_LIQUIDATION_HEADROOM))

    def as_dict(self) -> dict:
        return {"qty": round(self.qty, 6), "notional": round(self.notional, 2),
                "leverage": round(self.leverage, 2), "margin": round(self.margin, 2),
                "risk_amount": round(self.risk_amount, 2),
                "stop_pct": round(self.stop_pct, 4),
                "liquidation_price": (None if self.liquidation_price is None
                                      else round(self.liquidation_price, 2)),
                "liquidation_headroom": (None if self.liquidation_headroom is None
                                         else round(self.liquidation_headroom, 2)),
                "capped_by": self.capped_by, "safe": self.safe}


def size_position(entry: float, stop: float, risk_amount: float, *,
                  capital: float,
                  direction: int = 1,
                  max_leverage: int = MAX_LEVERAGE,
                  contract_size: float = 1.0,
                  leveraged: bool = True) -> Optional[Position]:
    """Size from the stop. Returns None when the inputs make no sense.

    `risk_amount`   currency lost if the stop fills exactly at its price
    `capital`       MARGIN posted for this trade, e.g. the Rs 50,000 budget.
                    NOT the notional: at 20x, Rs 50,000 of margin controls
                    Rs 1,000,000 of notional. Conflating the two collapses
                    computed leverage to 1.0 and makes every position look
                    safe when it is not.
    `contract_size` underlying units per contract (Delta ETHUSD is 0.01)
    `leveraged`     False for cash instruments like bought options, where the
                    most you can lose is what you paid and there is no
                    liquidation to model

    Slippage is NOT modelled here — the realised loss on a gap through the stop
    will exceed `risk_amount`. That is precisely why liquidation headroom is
    enforced separately rather than trusting the stop to be the worst case.
    """
    if entry <= 0 or risk_amount <= 0 or capital <= 0:
        return None
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return None

    # The whole idea: quantity falls out of the stop, not out of a leverage dial.
    qty = risk_amount / stop_distance
    notional = qty * entry
    capped: Optional[str] = None

    if not leveraged:
        # Cash instrument: you cannot spend more than the budget.
        if notional > capital:
            qty = capital / entry
            notional = qty * entry
            capped = "capital"
        return Position(qty=qty, notional=notional, leverage=1.0, margin=notional,
                        risk_amount=qty * stop_distance,
                        stop_distance=stop_distance,
                        stop_pct=stop_distance / entry * 100,
                        liquidation_price=None, liquidation_headroom=None,
                        capped_by=capped)

    leverage = notional / capital
    if leverage > max_leverage:
        leverage = float(max_leverage)
        notional = capital * leverage
        qty = notional / entry
        capped = "max_leverage"

    liq_move = (1.0 / leverage) - MAINTENANCE_MARGIN if leverage > 0 else 1.0
    liq_price = (entry * (1 - liq_move) if direction > 0 else entry * (1 + liq_move))
    headroom = (abs(entry - liq_price) / stop_distance) if stop_distance > 0 else None

    pos = Position(qty=qty, notional=notional, leverage=leverage,
                   margin=notional / leverage if leverage else notional,
                   risk_amount=qty * stop_distance, stop_distance=stop_distance,
                   stop_pct=stop_distance / entry * 100,
                   liquidation_price=liq_price, liquidation_headroom=headroom,
                   capped_by=capped)

    if not pos.safe:
        logger.warning(
            "sizing: stop %.2f%% at %.1fx leaves only %.1fx headroom to "
            "liquidation (need %.1fx) — refuse or reduce leverage",
            pos.stop_pct, leverage, headroom or 0, MIN_LIQUIDATION_HEADROOM)
    return pos


def max_safe_leverage(stop_pct: float,
                      headroom: float = MIN_LIQUIDATION_HEADROOM) -> float:
    """Highest leverage at which liquidation stays `headroom` stops away.

    Invert the liquidation relation:
        liq_move = 1/L - m   and we require   liq_move >= headroom * stop
        =>  L <= 1 / (headroom * stop + m)
    """
    s = stop_pct / 100.0
    denom = headroom * s + MAINTENANCE_MARGIN
    return (1.0 / denom) if denom > 0 else float("inf")
