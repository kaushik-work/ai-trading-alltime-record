"""
Synthetic-Forward strategy (v5) — packaged as a CryptoStrategy
================================================================
Same logic as delta_exchange/backtest_synth_forward_v5.py, factored to use
the live DeltaCryptoBroker. Hooks into the bot_runner scheduler.

Per-asset instances are created by the scheduler with their own dials.
Production-ready defaults below match v5 tight-gate (validated +462% BTC OOS,
+1038% ETH OOS).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import numpy as np

from strategies.crypto_base import CryptoStrategy, CryptoSignalDecision

logger = logging.getLogger(__name__)


# v5 production dials — validated by backtests + OOS
ENTRY_PCT     = 0.006     # 0.6% gate
PERSIST_HOURS = 2
MIN_STRIKES   = 3
TT_MIN_HOURS  = 6
TT_MAX_HOURS  = 72
MONEYNESS     = 0.05
SIZE_BASE_PCT = 0.005     # 0.5% per unit
SIZE_MIN_MULT = 0.5
SIZE_MAX_MULT = 3.0


def _parse_option_symbol(sym: str):
    """C-BTC-71400-310525 → ('C', 'BTC', 71400, datetime(2025, 5, 31))"""
    m = re.match(r"^([CP])-([A-Z]+)-(\d+)-(\d{6})$", sym)
    if not m: return None
    side, asset, strike, ddmmyy = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    try:
        dd, mm, yy = int(ddmmyy[:2]), int(ddmmyy[2:4]), int(ddmmyy[4:6])
        expiry = datetime(2000 + yy, mm, dd, 12, 0, tzinfo=timezone.utc)
    except Exception:
        return None
    return side, asset, strike, expiry


class SynthForwardSignal(CryptoStrategy):
    """v5 strategy. Subclass per underlying (BTC, ETH).

    Subclasses just set `name` and `underlying`.
    """

    underlying: str = ""

    @property
    def symbol(self) -> str:
        return f"{self.underlying}USD"

    def _compute_signal(self) -> Optional[CryptoSignalDecision]:
        spot = self.broker.get_perp_mark(self.symbol)
        if spot is None or spot <= 0: return None
        chain = self.broker.get_option_chain(self.underlying)
        if not chain: return None

        now = datetime.now(timezone.utc)
        # group by expiry, build (side, strike, mark) per expiry
        per_expiry: dict[datetime, dict[str, dict]] = {}
        for c in chain:
            parsed = _parse_option_symbol(c["symbol"])
            if not parsed: continue
            side, asset, strike, expiry = parsed
            if asset != self.underlying: continue
            tte_h = (expiry - now).total_seconds() / 3600
            if not (TT_MIN_HOURS <= tte_h <= TT_MAX_HOURS): continue
            per_expiry.setdefault(expiry, {"C": {}, "P": {}})[side][strike] = c["mark"]

        # for each expiry compute median dislocation across near-money strikes
        candidates: list[tuple[float, datetime, int]] = []  # (pred, expiry, n_strikes)
        for expiry, sides in per_expiry.items():
            common = sorted(set(sides["C"]) & set(sides["P"]))
            near = [K for K in common if abs(K - spot) / spot <= MONEYNESS]
            if len(near) < MIN_STRIKES: continue
            devs = []
            for K in near:
                cp, pp = sides["C"][K], sides["P"][K]
                if cp <= 0 or pp <= 0: continue
                devs.append(((cp - pp + K) - spot) / spot)
            if len(devs) < MIN_STRIKES: continue
            pos = sum(1 for d in devs if d > 0)
            neg = sum(1 for d in devs if d < 0)
            if pos < MIN_STRIKES and neg < MIN_STRIKES: continue
            candidates.append((float(np.median(devs)), expiry, len(devs)))

        if not candidates: return None

        # pick strongest
        candidates.sort(key=lambda c: -abs(c[0]))
        pred, expiry, n_strikes = candidates[0]

        # Record raw pred for charting BEFORE gating — so the dashboard chart
        # has a continuous line even when signals are well below the gate.
        self._record_pred_trace(pred * 100)

        # gate
        if abs(pred) < ENTRY_PCT: return None

        # persistence (caller must have been on_tick()-ing)
        recent_same_sign = self.signal_persistence_hours(lookback_hours=PERSIST_HOURS)
        if recent_same_sign < PERSIST_HOURS: return None

        size_mult = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(pred) / SIZE_BASE_PCT))
        return CryptoSignalDecision(
            name=self.name,
            symbol=self.symbol,
            side="buy" if pred > 0 else "sell",
            pred_pct=pred * 100,
            n_strikes=n_strikes,
            expiry=expiry.isoformat(),
            size_mult=size_mult,
            metadata={"spot": spot, "tte_hours":
                      (expiry - now).total_seconds() / 3600},
        )


# Concrete per-asset strategies — scheduler instantiates these by name
class BTCSynthForwardSignal(SynthForwardSignal):
    name = "btc_synth_forward"
    underlying = "BTC"


class ETHSynthForwardSignal(SynthForwardSignal):
    name = "eth_synth_forward"
    underlying = "ETH"
