"""
Multi-strategy shadow signals derived from the alpha-mining pass (2026-05-22).

Three independent signals, each with its own threshold and shadow ledger:

    q5_straddle_level   atm_straddle > trailing-5d P70                  (existing)
    q5_straddle_mom3    (atm_straddle - atm_straddle_3bars_ago) > P70
    q5_pcr_mom3         (pcr_oi - pcr_oi_3bars_ago) > P70

Each signal lives in its own subclass with a unique `name`. The scheduler
instantiates all three on bot startup and ticks each independently.

Same exit logic for all three (SL=10, RR=3.0, side=CE) — only the entry
trigger differs.

NOTE: this module replaces nothing in `strategies/straddle_signal.py` —
that file remains the canonical reference implementation for the level
signal. This module factors out the shared logic so we can add new signals
without duplicating the threshold cache.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Locked params — same for all three signals
N_DAYS      = 5
PERCENTILE  = 0.70
SIDE        = "CE"
SL_DIST     = 10.0
RR          = 2.25   # WR-maximising — replay shows 38.2% WR at 2.25 vs 36.8% at 2.5.
STRIKE_OFFSET_STEPS = -1   # ITM-50 (one step in-the-money for CE) — replay shows
                           # WR 40.3% / PF 1.52 vs ATM's WR 38.2% / PF 1.39.
                           # Higher delta + lower IV exposure for the same SL/RR.
STRIKE_STEP = 50  # NIFTY


@dataclass
class SignalDecision:
    fire:             bool
    atm_strike:       int
    current_value:    float        # the feature value at this bar
    threshold:        Optional[float]
    reason:           str
    side:             str = SIDE
    sl_dist:          float = SL_DIST
    rr:               float = RR


# ── Helpers ──────────────────────────────────────────────────────────────────

def _atm_strike_for(spot: float) -> int:
    return int(round(spot / STRIKE_STEP)) * STRIKE_STEP


def _chosen_strike_for(spot: float) -> int:
    """Strike actually traded — ATM shifted by STRIKE_OFFSET_STEPS.
    Negative offset = ITM for CE (strike below spot)."""
    return _atm_strike_for(spot) + STRIKE_OFFSET_STEPS * STRIKE_STEP


def _atm_straddle_for_bar(rows: list, atm: int) -> Optional[float]:
    ce = next((r["ltp"] for r in rows
               if r.get("strike") == atm and r.get("option_type") == "CE"), None)
    pe = next((r["ltp"] for r in rows
               if r.get("strike") == atm and r.get("option_type") == "PE"), None)
    if ce is None or pe is None:
        return None
    return float(ce) + float(pe)


def _pcr_oi_for_bar(rows: list) -> Optional[float]:
    ce_oi = sum(int(r.get("oi", 0) or 0) for r in rows
                if r.get("option_type") == "CE")
    pe_oi = sum(int(r.get("oi", 0) or 0) for r in rows
                if r.get("option_type") == "PE")
    if ce_oi == 0:
        return None
    return pe_oi / ce_oi


def _load_prior_bars(db, today: date, n_days: int = N_DAYS) -> dict:
    """{(date, ts): [snapshot_row, ...]} for the last `n_days` trading days
    strictly BEFORE `today`. None if Mongo unreachable or not enough history."""
    today_str = today.isoformat()
    prior_dates = sorted(
        db.option_snapshots.distinct("date", {"date": {"$lt": today_str},
                                              "symbol": "NIFTY"}),
        reverse=True,
    )[:n_days]
    if len(prior_dates) < n_days:
        return {}
    cur = db.option_snapshots.find(
        {"date": {"$in": prior_dates}, "symbol": "NIFTY"},
        projection={"_id": 0, "date": 1, "timestamp": 1, "strike": 1,
                    "option_type": 1, "ltp": 1, "oi": 1, "spot": 1},
    )
    bars: dict = {}
    for d in cur:
        bars.setdefault((d["date"], d["timestamp"]), []).append(d)
    return bars


def _today_bars(db, today: date) -> dict:
    """{ts: [snapshot_row, ...]} for today's snapshots so far, sorted by ts."""
    cur = db.option_snapshots.find(
        {"date": today.isoformat(), "symbol": "NIFTY"},
        projection={"_id": 0, "timestamp": 1, "strike": 1,
                    "option_type": 1, "ltp": 1, "oi": 1, "spot": 1},
    )
    bars: dict = {}
    for d in cur:
        bars.setdefault(d["timestamp"], []).append(d)
    return bars


# ── Base class ───────────────────────────────────────────────────────────────

class FeatureSignal:
    """Abstract: subclasses define `name` and `_compute_feature(rows)`."""

    name: str = "abstract"

    def __init__(self):
        self._cached_threshold: Optional[float] = None
        self._cached_for_date: Optional[str] = None

    def _compute_feature(self, current_rows: list,
                          history_rows: Optional[list] = None) -> Optional[float]:
        """Override: compute the feature value for a single bar.

        `current_rows`  — list of snapshot dicts for the current bar
        `history_rows`  — optional list-of-list (the 3 prior bars on same day)
                          for momentum signals. None for level signals.
        """
        raise NotImplementedError

    def _refresh_threshold(self, today: date) -> Optional[float]:
        today_str = today.isoformat()
        if self._cached_for_date == today_str and self._cached_threshold is not None:
            return self._cached_threshold

        try:
            from core import mongo
            db = mongo.get_db()
            if db is None:
                return None
            prior_bars = _load_prior_bars(db, today, N_DAYS)
            if not prior_bars:
                logger.info("%s: insufficient prior days — warmup", self.name)
                return None

            # Order per-day bars so momentum signals have history available
            by_day: dict = {}
            for (d, ts), rows in prior_bars.items():
                by_day.setdefault(d, []).append((ts, rows))
            for d in by_day:
                by_day[d].sort(key=lambda x: x[0])

            values = []
            for d, sorted_bars in by_day.items():
                for i, (ts, rows) in enumerate(sorted_bars):
                    history = [sorted_bars[i - k][1] for k in (1, 2, 3)] \
                              if i >= 3 else None
                    v = self._compute_feature(rows, history)
                    if v is not None:
                        values.append(v)

            if len(values) < 30:
                logger.warning("%s: only %d historical values — refusing",
                               self.name, len(values))
                return None

            values.sort()
            idx = int(PERCENTILE * (len(values) - 1))
            threshold = values[idx]
            self._cached_threshold = threshold
            self._cached_for_date = today_str
            logger.info("%s: trailing-%dd P%d threshold = %.4f "
                        "(from %d values)",
                        self.name, N_DAYS, int(PERCENTILE * 100),
                        threshold, len(values))
            return threshold
        except Exception as e:
            logger.warning("%s threshold refresh failed: %s", self.name, e)
            return None

    def _today_history(self, now_dt: datetime) -> Optional[list]:
        """For momentum signals: return the 3 prior bars on today (each a list
        of snapshot rows), or None if not enough today-bars yet."""
        try:
            from core import mongo
            db = mongo.get_db()
            if db is None:
                return None
            today_bars = _today_bars(db, now_dt.date())
            sorted_ts = sorted(today_bars.keys())
            # Take the 3 bars immediately PRIOR to now_dt
            prior_ts = [ts for ts in sorted_ts
                        if datetime.fromisoformat(ts.replace(" ", "T")) < now_dt]
            if len(prior_ts) < 3:
                return None
            return [today_bars[t] for t in prior_ts[-3:]]
        except Exception as e:
            logger.debug("%s today_history failed: %s", self.name, e)
            return None

    def compute(self, now_dt: datetime, spot: float,
                current_rows: list) -> SignalDecision:
        """Caller passes `current_rows` = list of snapshot dicts for the
        current bar (ATM CE/PE at minimum, full chain for PCR signals).
        For momentum signals, this method internally fetches today_history
        from Mongo."""
        atm = _atm_strike_for(spot)

        # For momentum signals we also need 3 prior bars on TODAY
        history = self._today_history(now_dt) if "mom" in self.name else None
        current_val = self._compute_feature(current_rows, history)

        if current_val is None:
            return SignalDecision(False, atm, 0.0, None,
                                  "could not compute feature")

        threshold = self._refresh_threshold(now_dt.date())
        if threshold is None:
            return SignalDecision(False, atm, current_val, None,
                                  "warmup: threshold unavailable")

        if current_val <= threshold:
            return SignalDecision(False, atm, current_val, threshold,
                                  f"{self.name}={current_val:.4f} <= "
                                  f"threshold={threshold:.4f}")

        return SignalDecision(True, atm, current_val, threshold,
                              f"{self.name}={current_val:.4f} > "
                              f"threshold={threshold:.4f}")


# ── Concrete signals ─────────────────────────────────────────────────────────

class StraddleLevelSignal(FeatureSignal):
    name = "q5_straddle_level"

    def _compute_feature(self, current_rows, history_rows=None):
        if not current_rows:
            return None
        spot = current_rows[0].get("spot")
        if not spot:
            return None
        atm = _atm_strike_for(float(spot))
        return _atm_straddle_for_bar(current_rows, atm)


class StraddleMom3Signal(FeatureSignal):
    """3-bar (15-min) change in ATM straddle. IC +0.120 in mining."""
    name = "q5_straddle_mom3"

    def _compute_feature(self, current_rows, history_rows=None):
        if not current_rows or history_rows is None or len(history_rows) < 3:
            return None
        spot = current_rows[0].get("spot")
        if not spot:
            return None
        atm = _atm_strike_for(float(spot))
        cur_straddle = _atm_straddle_for_bar(current_rows, atm)
        # ATM strike may have shifted across 3 bars — use each bar's own ATM
        past_straddles = []
        for h in history_rows:
            if not h:
                return None
            h_spot = h[0].get("spot")
            if not h_spot:
                return None
            h_atm = _atm_strike_for(float(h_spot))
            v = _atm_straddle_for_bar(h, h_atm)
            if v is None:
                return None
            past_straddles.append(v)
        if cur_straddle is None:
            return None
        return cur_straddle - past_straddles[0]   # current - 3-bars-ago


class PcrMom3Signal(FeatureSignal):
    """3-bar (15-min) change in PCR OI. IC +0.115 in mining."""
    name = "q5_pcr_mom3"

    def _compute_feature(self, current_rows, history_rows=None):
        if not current_rows or history_rows is None or len(history_rows) < 3:
            return None
        cur_pcr = _pcr_oi_for_bar(current_rows)
        if cur_pcr is None:
            return None
        past = _pcr_oi_for_bar(history_rows[0])
        if past is None:
            return None
        return cur_pcr - past


# ── Registry ─────────────────────────────────────────────────────────────────

ALL_SIGNALS = [StraddleLevelSignal, StraddleMom3Signal, PcrMom3Signal]
