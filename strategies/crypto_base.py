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
        # _sig_history holds the EVERY-TICK pred trace used by the persistence
        # gate. Backtest matches: appends every raw pred regardless of gate.
        self._sig_history: list[tuple[float, float]] = []
        self._pred_trace: list[tuple[float, float]] = []    # mirror for chart
        self._last_tick: float = 0.0
        self._load_persisted_history()

    def _load_persisted_history(self) -> None:
        """Restore _sig_history from Mongo on startup so the persistence gate
        works immediately after a container restart instead of needing a full
        warm-up window. Reads the last 24h from `crypto_signal_history`."""
        try:
            from core import mongo
            db = mongo.get_db()
            if db is None: return
            cutoff = time.time() - 24 * 3600
            rows = list(db["crypto_signal_history"].find(
                {"strategy": self.name, "ts": {"$gte": cutoff}},
                projection={"_id": 0, "ts": 1, "pred_pct": 1},
            ).sort("ts", 1))
            for row in rows:
                self._sig_history.append((float(row["ts"]), float(row["pred_pct"])))
            if rows:
                logger.info("%s: restored %d signal-history points from mongo",
                            self.name, len(rows))
        except Exception as e:
            logger.warning("%s: signal history restore failed: %s", self.name, e)

    def _persist_signal_point(self, ts: float, pred_pct: float) -> None:
        """Append one pred sample to Mongo. Fire-and-forget — write failures
        don't break the trade loop. TTL on insert ts keeps the collection
        bounded (24h hot window is what the gate cares about)."""
        try:
            from core import mongo
            db = mongo.get_db()
            if db is None: return
            db["crypto_signal_history"].insert_one({
                "strategy": self.name,
                "ts": ts,
                "pred_pct": pred_pct,
            })
        except Exception:
            pass  # silent — never block the trade path on logging

    def _record_pred_trace(self, pred_pct: float) -> None:
        """Record the raw (un-gated) pred for charting. Trimmed to 24h."""
        self._pred_trace.append((time.time(), pred_pct))
        cutoff = time.time() - 24 * 3600
        self._pred_trace = [(t, p) for t, p in self._pred_trace if t >= cutoff]

    def _record_sig_history(self, pred_pct: float) -> None:
        """Append a raw pred to the persistence-gate history AND mirror to
        Mongo so it survives restarts. Trimmed to 24h."""
        now = time.time()
        self._sig_history.append((now, pred_pct))
        cutoff = now - 24 * 3600
        self._sig_history = [(t, p) for t, p in self._sig_history if t >= cutoff]
        self._persist_signal_point(now, pred_pct)

    def _compute_signal(self) -> Optional[CryptoSignalDecision]:
        """Override: return SignalDecision or None."""
        raise NotImplementedError

    def on_tick(self) -> Optional[CryptoSignalDecision]:
        """Called by scheduler. _sig_history is now seeded inside
        _compute_signal via _record_sig_history (raw pred every call, not
        gate-crossings only) — see backtest parity fix."""
        self._last_tick = time.time()
        try:
            return self._compute_signal()
        except Exception as e:
            logger.error("%s on_tick error: %s", self.name, e)
            return None

    def signal_now(self) -> Optional[CryptoSignalDecision]:
        """Run _compute_signal once. The history side-effects (pred trace +
        persistence history) happen inside _compute_signal itself, so this is
        functionally the same as on_tick — both are kept for clarity at the
        scheduler layer (on_tick = top-of-hour, signal_now = 5-min sample)."""
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
