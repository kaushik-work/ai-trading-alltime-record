"""
Q5 ATM-Straddle Shadow Signal.

Pure signal-computation module. No I/O beyond reading historical
option_snapshots from Mongo for the trailing-N-day percentile threshold.

Locked parameters (from sweep_straddle.py best-robust combo on 2026-05-22):
    n_days = 5       — trailing window of prior trading days
    pct    = 0.70    — fire when today's atm_straddle exceeds prior-window 70th pct
    side   = "CE"    — directional (signed corr +0.18 vs fwd_30m spot return)
    sl     = 10.0    — SL distance in premium points
    rr     = 3.0     — R:R ratio (3:1, matches sweep)

Usage:
    sig = StraddleSignal()
    decision = sig.compute(now_dt, spot, atm_ce_ltp, atm_pe_ltp)
    if decision.fire:
        # open ATM CE shadow position at decision.atm_strike
        ...

The threshold is recomputed once per trading day (cached); intra-day calls
just compare today's straddle to that cached value.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Locked params (do not change without re-running sweep_straddle.py)
N_DAYS    = 5
PERCENTILE = 0.70
SIDE      = "CE"
SL_DIST   = 10.0
RR        = 2.25   # WR-maximising choice from RR fine-tune (38.2% WR vs 36.8% at 2.5)
STRIKE_STEP = 50  # NIFTY


@dataclass
class StraddleDecision:
    fire:             bool
    atm_strike:       int
    current_straddle: float
    threshold:        Optional[float]   # None = warmup, can't decide
    reason:           str
    side:             str = SIDE
    sl_dist:          float = SL_DIST
    rr:               float = RR


class StraddleSignal:
    def __init__(self):
        self._cached_threshold: Optional[float] = None
        self._cached_for_date:  Optional[str] = None

    def _refresh_threshold(self, today: date) -> Optional[float]:
        """Compute the trailing-N-day P70 atm_straddle from option_snapshots.

        Returns None if Mongo unreachable or insufficient prior history.
        Idempotent within a single trading day — caches the result.
        """
        today_str = today.isoformat()
        if self._cached_for_date == today_str and self._cached_threshold is not None:
            return self._cached_threshold

        try:
            from core import mongo
            db = mongo.get_db()
            if db is None:
                return None
            # Get the last N distinct dates strictly BEFORE today
            prior_dates = sorted(
                db.option_snapshots.distinct("date", {"date": {"$lt": today_str},
                                                      "symbol": "NIFTY"}),
                reverse=True,
            )[:N_DAYS]
            if len(prior_dates) < N_DAYS:
                logger.info("straddle_signal: only %d prior days available (need %d) — "
                            "warmup", len(prior_dates), N_DAYS)
                return None

            # Pull spot + ATM CE/PE LTPs for those days, compute per-bar atm_straddle
            cur = db.option_snapshots.find(
                {"date": {"$in": prior_dates}, "symbol": "NIFTY"},
                projection={"_id": 0, "date": 1, "timestamp": 1, "strike": 1,
                            "option_type": 1, "ltp": 1, "spot": 1},
            )
            # Group by (date, timestamp) and compute straddle at ATM strike
            bars: dict = {}
            for d in cur:
                key = (d["date"], d["timestamp"])
                bars.setdefault(key, []).append(d)

            straddles = []
            for _, rows in bars.items():
                spot = rows[0].get("spot")
                if not spot:
                    continue
                atm = int(round(spot / STRIKE_STEP)) * STRIKE_STEP
                ce_ltp = next((r["ltp"] for r in rows
                               if r["strike"] == atm and r["option_type"] == "CE"), None)
                pe_ltp = next((r["ltp"] for r in rows
                               if r["strike"] == atm and r["option_type"] == "PE"), None)
                if ce_ltp and pe_ltp:
                    straddles.append(float(ce_ltp) + float(pe_ltp))

            if len(straddles) < 30:
                logger.warning("straddle_signal: only %d straddle samples — refusing",
                               len(straddles))
                return None

            straddles.sort()
            idx = int(PERCENTILE * (len(straddles) - 1))
            threshold = straddles[idx]
            self._cached_threshold = threshold
            self._cached_for_date  = today_str
            logger.info("straddle_signal: trailing-%dd P%d threshold = Rs %.2f "
                        "(from %d bars over %d days)",
                        N_DAYS, int(PERCENTILE * 100), threshold,
                        len(straddles), len(prior_dates))
            return threshold
        except Exception as e:
            logger.warning("straddle_signal threshold refresh failed: %s", e)
            return None

    def compute(self, now_dt: datetime, spot: float,
                atm_ce_ltp: float, atm_pe_ltp: float) -> StraddleDecision:
        """Decide whether to fire a shadow trade on this bar.

        Caller supplies the live spot + ATM CE/PE LTPs from its own Angel One
        fetch so we don't double-call the broker.
        """
        atm_strike = int(round(spot / STRIKE_STEP)) * STRIKE_STEP
        current_straddle = float(atm_ce_ltp or 0) + float(atm_pe_ltp or 0)

        if current_straddle <= 0:
            return StraddleDecision(False, atm_strike, current_straddle, None,
                                    "missing ATM premium data")

        threshold = self._refresh_threshold(now_dt.date())
        if threshold is None:
            return StraddleDecision(False, atm_strike, current_straddle, None,
                                    "warmup: insufficient history")

        if current_straddle <= threshold:
            return StraddleDecision(False, atm_strike, current_straddle, threshold,
                                    f"straddle Rs {current_straddle:.2f} <= "
                                    f"threshold Rs {threshold:.2f}")

        return StraddleDecision(True, atm_strike, current_straddle, threshold,
                                f"straddle Rs {current_straddle:.2f} > "
                                f"threshold Rs {threshold:.2f}")
