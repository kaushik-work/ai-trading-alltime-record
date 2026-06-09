"""
Crypto strategy base — mirrors the FeatureSignal pattern in feature_signals.py
=============================================================================
NSE strategies (Q5 ensemble) operate on 5-min OI snapshot bars from Mongo.
Crypto strategies operate on live Delta API + accumulated signal history.

So we use a separate base class. Same lifecycle (init, on_tick, decide), but
the data source is the broker, not Mongo.

Public interface:
  strategy.name             → unique identifier
  strategy.on_tick()        → call hourly; updates internal state
  strategy.signal_now()     → returns SignalDecision or None
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config

logger = logging.getLogger(__name__)


@dataclass
class CryptoSignalDecision:
    """What a crypto strategy emits when it wants to trade."""
    name: str                       # strategy id
    symbol: str                     # perp symbol (e.g. BTCUSD)
    side: str                       # "buy" | "sell"
    pred_pct: float                 # signal strength in percent
    n_strikes: int                  # corroborating strikes
    expiry: Optional[str] = None    # associated option expiry (for context)
    size_mult: float = 1.0          # 0.5x–3.0x equity multiplier
    stop_loss_pct: float = 0.015
    partial_tp_pct: float = 0.010
    trail_peak_pct: float = 0.005
    trail_giveback: float = 0.0025
    metadata: dict = field(default_factory=dict)


class CryptoStrategy:
    """Abstract base. Subclasses must define `name`, `symbol`, `_compute_signal`."""

    name: str = "abstract"
    symbol: str = ""

    def __init__(self, broker=None):
        from core.brokers.delta_crypto import get_broker
        self.broker = broker or get_broker()
        self._sig_history: list[tuple[float, float]] = []   # (ts, pred_pct)
        self._last_tick: float = 0.0

    def _compute_signal(self) -> Optional[CryptoSignalDecision]:
        """Override: return SignalDecision or None."""
        raise NotImplementedError

    def on_tick(self) -> Optional[CryptoSignalDecision]:
        """Called by scheduler. Caches signal history."""
        self._last_tick = time.time()
        try:
            sig = self._compute_signal()
        except Exception as e:
            logger.error("%s on_tick error: %s", self.name, e)
            return None
        if sig is not None:
            self._sig_history.append((time.time(), sig.pred_pct))
            # trim to last 24h
            cutoff = time.time() - 24 * 3600
            self._sig_history = [(t, p) for t, p in self._sig_history if t >= cutoff]
        return sig

    def signal_now(self) -> Optional[CryptoSignalDecision]:
        """Convenience: same as on_tick but doesn't update history."""
        try:
            return self._compute_signal()
        except Exception as e:
            logger.error("%s signal_now error: %s", self.name, e)
            return None

    def signal_persistence_hours(self, lookback_hours: float = 2.0) -> int:
        """Approximate number of hourly observations with same sign as latest.

        Tick-rate independent: span_hours+1 yields the same answer whether
        the strategy is ticking at 60min or 5s. Backtest semantic (60min bars,
        N consecutive same-sign bars = 2) maps to (span 1h => returns 2),
        so the existing PERSIST_HOURS=2 gate continues to require ~1h of
        real-time signal persistence regardless of tick rate.
        """
        if not self._sig_history: return 0
        latest_t, latest_pred = self._sig_history[-1]
        latest_sign = 1 if latest_pred > 0 else -1
        cutoff = latest_t - lookback_hours * 3600
        same_ts = [t for t, p in self._sig_history
                   if t >= cutoff and (1 if p > 0 else -1) == latest_sign]
        if not same_ts: return 0
        span_h = (max(same_ts) - min(same_ts)) / 3600
        return int(span_h) + 1
