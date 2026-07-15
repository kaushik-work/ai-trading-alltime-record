"""Naked long options strategy based on the synthetic-forward signal.

When the synthetic forward is cheap vs spot (F < spot), the implied forward is
below the cash spot. That usually means calls are cheap / puts are rich, so we
buy an ATM call.  When F > spot we buy an ATM put.

This is a pure long-option strategy: limited risk to premium paid, no short
legs, no margin beyond the premium.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from nse.config import ENTRY_PCT, MONEYNESS, PERSIST_HOURS
from nse.strategies.synthetic_forward import SyntheticForwardStrategy


class NakedOptionsStrategy:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self._sf = SyntheticForwardStrategy(symbol)

    def compute(self, snapshot: pd.DataFrame, t: datetime) -> Optional[dict]:
        """Return trade idea or None.

        Returns dict with keys: side ('call'/'put'), signal_side ('long'/'short'),
        pred, spot, expiry, timestamp.
        """
        sigs = self._sf.compute(snapshot, t)
        if not sigs:
            return None
        chosen = max(sigs, key=lambda s: abs(s.pred))
        # Reuse the gating logic: persistence, min strikes, etc.
        # For a single run we keep it simple and gate by magnitude.
        if abs(chosen.pred) < ENTRY_PCT:
            return None
        side = "call" if chosen.side == "long" else "put"
        return {
            "symbol": chosen.symbol,
            "expiry": chosen.expiry,
            "pred": chosen.pred,
            "spot": chosen.spot,
            "side": side,
            "signal_side": chosen.side,
            "timestamp": t,
        }
