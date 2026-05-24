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
    """{ts: [snapshot_row, ...]} for today's snapshots so far, sorted by ts.

    LEGACY path — reads from Mongo. Still used as the cold-start fallback,
    but the live signal path now prefers _today_bars_from_state() below.
    """
    cur = db.option_snapshots.find(
        {"date": today.isoformat(), "symbol": "NIFTY"},
        projection={"_id": 0, "timestamp": 1, "strike": 1,
                    "option_type": 1, "ltp": 1, "oi": 1, "spot": 1},
    )
    bars: dict = {}
    for d in cur:
        bars.setdefault(d["timestamp"], []).append(d)
    return bars


def _today_bars_from_state(now_dt: datetime) -> Optional[dict]:
    """{ts_str: [row_dict, ...]} built from in-memory MarketState.

    Returns None when MarketState isn't ready (cold-start window) — caller
    should fall back to _today_bars() reading from Mongo.

    Output shape matches the Mongo-derived path so downstream feature
    functions (_atm_straddle_for_bar, _pcr_oi_for_bar) work unchanged.
    """
    try:
        from core.market_state import get_state
        state = get_state()
        if not state.is_ready():
            return None

        spot = state.get_spot()
        if spot is None:
            return None

        # Build {bar_start_dt: {strike, side: {ltp, oi}}} by walking each
        # registered option's bar deque.
        bar_records: dict = {}   # bar_start_dt -> list[row_dict]
        for snap in state.all_option_snapshots():
            strike = snap.get("strike")
            side   = snap.get("option_type")
            if strike is None or side is None:
                continue
            for (bar_start, o, h, l, c, oi) in snap.get("bars", []):
                bar_records.setdefault(bar_start, []).append({
                    "strike":      int(strike),
                    "option_type": side,
                    "ltp":         float(c),
                    "oi":          int(oi or 0),
                    "spot":        spot,    # use latest spot for all rows
                })
            # Include in-progress bar (today's currently-live one) — the
            # in-progress close is the freshest LTP we have, which is what
            # the signal should see for the "current" bar.
            cur_close = snap.get("cur_close")
            if cur_close is not None:
                # Compute the in-progress bar's start time
                latest_ts = snap.get("latest_ts")
                if latest_ts is not None:
                    from core.market_state import _bar_start as _bs
                    in_progress_start = _bs(latest_ts)
                    bar_records.setdefault(in_progress_start, []).append({
                        "strike":      int(strike),
                        "option_type": side,
                        "ltp":         float(cur_close),
                        "oi":          int(snap.get("latest_oi") or 0),
                        "spot":        spot,
                    })

        # Convert datetime keys to string format matching Mongo timestamp
        # ("YYYY-MM-DD HH:MM:SS") so the regime/momentum helpers downstream
        # can still index by string.
        out: dict = {}
        for bar_dt, rows in bar_records.items():
            ts_str = bar_dt.strftime("%Y-%m-%d %H:%M:%S")
            out[ts_str] = rows
        return out
    except Exception as e:
        logger.debug("_today_bars_from_state failed (will fall back): %s", e)
        return None


def _get_today_bars(db, now_dt: datetime) -> dict:
    """Hybrid getter — in-memory state first, Mongo fallback."""
    from_state = _today_bars_from_state(now_dt)
    if from_state is not None and from_state:
        return from_state
    return _today_bars(db, now_dt.date())


# ── Regime filter ────────────────────────────────────────────────────────────
# Backed by scripts/regime_filter.py findings:
#   trend_up regime  → PF 1.13 (essentially noise) → REFUSE fires
#   chop regime      → PF 2.52
#   trend_down regime → PF 3.26
#
# Trend defined as: spot return over last 6 bars (30 min) exceeds threshold
# AND OLS slope of those 6 bars confirms direction.
REGIME_WINDOW_BARS    = 6        # 30 min lookback
REGIME_TREND_RETURN   = 0.0015   # 0.15% over 30 min defines a trend


def _classify_regime(today_bars: dict, now_ts: str) -> str:
    """Returns one of: 'trend_up', 'trend_down', 'chop', 'warmup'.

    `today_bars` is {ts: [row, ...]} from _today_bars().
    `now_ts` is the current bar's timestamp string (already keyed in today_bars).
    """
    sorted_ts = sorted(today_bars.keys())
    if now_ts not in sorted_ts:
        return "warmup"
    idx = sorted_ts.index(now_ts)
    if idx < REGIME_WINDOW_BARS:
        return "warmup"

    # Last REGIME_WINDOW_BARS+1 bars of spot (inclusive of current)
    window_ts = sorted_ts[idx - REGIME_WINDOW_BARS : idx + 1]
    spots = []
    for ts in window_ts:
        rows = today_bars.get(ts, [])
        if not rows or rows[0].get("spot") is None:
            return "warmup"
        spots.append(float(rows[0]["spot"]))

    cur = spots[-1]
    past = spots[0]
    if past <= 0:
        return "warmup"
    ret = cur / past - 1.0

    # OLS slope sign confirmation
    import numpy as _np
    x = _np.arange(len(spots))
    slope = float(_np.polyfit(x, spots, 1)[0])

    if ret >= REGIME_TREND_RETURN and slope > 0:
        return "trend_up"
    if ret <= -REGIME_TREND_RETURN and slope < 0:
        return "trend_down"
    return "chop"


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
        of snapshot rows), or None if not enough today-bars yet.

        Now reads from in-memory MarketState when ready (sub-second lag),
        falls back to Mongo (up-to-5min lag) during cold-start.
        """
        try:
            from core import mongo
            db = mongo.get_db()
            today_bars = _get_today_bars(db, now_dt)
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

    def _regime_now(self, now_dt: datetime) -> str:
        """Return current 30-min regime: trend_up | trend_down | chop | warmup.

        Reads from MarketState when ready, falls back to Mongo."""
        try:
            from core import mongo
            db = mongo.get_db()
            today_bars = _get_today_bars(db, now_dt)
            if not today_bars:
                return "warmup"
            sorted_ts = sorted(today_bars.keys())
            latest = max((ts for ts in sorted_ts
                          if datetime.fromisoformat(ts.replace(" ", "T")) <= now_dt),
                         default=None)
            if latest is None:
                return "warmup"
            return _classify_regime(today_bars, latest)
        except Exception as e:
            logger.debug("%s regime check failed: %s", self.name, e)
            return "warmup"

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

        # ── Regime gate: refuse fires when in trend_up (worst regime, PF 1.13)
        regime = self._regime_now(now_dt)
        if regime == "trend_up":
            return SignalDecision(False, atm, current_val, threshold,
                                  f"regime=trend_up — refused (noise regime)")

        return SignalDecision(True, atm, current_val, threshold,
                              f"{self.name}={current_val:.4f} > "
                              f"threshold={threshold:.4f} regime={regime}")


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


class IvCheapSignal(FeatureSignal):
    """Fires when Black-Scholes ATM implied vol < 0.90 × realised vol.

    Orthogonal to the three Q5 signals (Jaccard < 0.07 with all of them) —
    catches "cheap IV vs realised" bars while Q5 signals catch
    "rich/rising IV" bars. Validated by scripts/check_signal_overlap.py:
    74.6% of fires are on bars no Q5 signal catches.

    Threshold is FIXED at 0.90 (not percentile-rolling) per backtest sweep:
    PF 1.60 at 0.90, PF degrades both ways. Above ~1.5 the signal becomes
    indistinguishable from random.

    Live cost: 2 Black-Scholes inversions per tick + one Mongo query for
    recent spot bars. Negligible (~5ms per tick worst case).
    """
    name = "q5_iv_cheap_090"
    FIXED_THRESHOLD  = 0.90
    RISK_FREE_RATE   = 0.07
    CALENDAR_DAYS_YR = 365
    RV_WINDOW_BARS   = 12   # 60-min trailing RV
    BARS_PER_DAY     = 75
    TRADING_DAYS_YR  = 252

    def _bs_call(self, S, K, T, r, sigma):
        import math
        from scipy.stats import norm
        if T <= 0 or sigma <= 0:
            return max(0.0, S - K)
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)

    def _bs_put(self, S, K, T, r, sigma):
        import math
        from scipy.stats import norm
        if T <= 0 or sigma <= 0:
            return max(0.0, K - S)
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    def _implied_vol(self, market_price, S, K, T, option_type):
        from scipy.optimize import brentq
        if T <= 0 or market_price <= 0:
            return None
        intrinsic = max(0.0, S - K) if option_type == "CE" else max(0.0, K - S)
        if market_price <= intrinsic:
            return None
        f = self._bs_call if option_type == "CE" else self._bs_put

        def diff(sigma):
            return f(S, K, T, self.RISK_FREE_RATE, sigma) - market_price

        try:
            return brentq(diff, 1e-3, 5.0, maxiter=100, xtol=1e-4)
        except (ValueError, RuntimeError):
            return None

    def _realised_vol(self, now_dt: datetime) -> Optional[float]:
        """Annualised RV from last RV_WINDOW_BARS spot values today.
        Hybrid: MarketState first, Mongo fallback."""
        import math
        import numpy as _np
        try:
            from core import mongo
            db = mongo.get_db()
            today_bars = _get_today_bars(db, now_dt)
            if not today_bars:
                return None
            sorted_ts = sorted(today_bars.keys())
            # Take last N bars at or before now_dt
            usable = [ts for ts in sorted_ts
                      if datetime.fromisoformat(ts.replace(" ", "T")) <= now_dt]
            if len(usable) < 6:
                return None
            window = usable[-self.RV_WINDOW_BARS:]
            spots = []
            for ts in window:
                rows = today_bars.get(ts, [])
                if not rows or rows[0].get("spot") is None:
                    return None
                spots.append(float(rows[0]["spot"]))
            if len(spots) < 6:
                return None
            log_returns = []
            for i in range(1, len(spots)):
                if spots[i-1] <= 0:
                    continue
                log_returns.append(math.log(spots[i] / spots[i-1]))
            if len(log_returns) < 5:
                return None
            sigma = float(_np.std(log_returns, ddof=1))
            return sigma * math.sqrt(self.TRADING_DAYS_YR * self.BARS_PER_DAY)
        except Exception as e:
            logger.debug("%s realised_vol failed: %s", self.name, e)
            return None

    def _refresh_threshold(self, today: date) -> Optional[float]:
        # Threshold is fixed — no percentile recomputation needed
        return self.FIXED_THRESHOLD

    def compute(self, now_dt: datetime, spot: float,
                current_rows: list) -> SignalDecision:
        atm = _atm_strike_for(spot)
        ce_ltp = next((r["ltp"] for r in current_rows
                        if r.get("strike") == atm and r.get("option_type") == "CE"),
                       None)
        pe_ltp = next((r["ltp"] for r in current_rows
                        if r.get("strike") == atm and r.get("option_type") == "PE"),
                       None)
        if ce_ltp is None or pe_ltp is None:
            return SignalDecision(False, atm, 0.0, None,
                                  "missing ATM premiums")

        # Calendar days to nearest weekly expiry (from broker)
        try:
            from data.angel_fetcher import AngelFetcher
            af = AngelFetcher.get()
            expiry = af.nearest_weekly_expiry()
            days_to_expiry = max(1, (expiry - now_dt.date()).days)
        except Exception as e:
            logger.debug("%s expiry lookup failed: %s", self.name, e)
            return SignalDecision(False, atm, 0.0, None, "expiry lookup failed")

        T = days_to_expiry / self.CALENDAR_DAYS_YR
        iv_ce = self._implied_vol(float(ce_ltp), float(spot), int(atm), T, "CE")
        iv_pe = self._implied_vol(float(pe_ltp), float(spot), int(atm), T, "PE")
        if iv_ce is None or iv_pe is None:
            return SignalDecision(False, atm, 0.0, None, "IV inversion failed")
        iv_atm = (iv_ce + iv_pe) / 2.0

        rv = self._realised_vol(now_dt)
        if rv is None or rv <= 0:
            return SignalDecision(False, atm, iv_atm, None,
                                  "warmup: insufficient bars for RV")

        ratio = iv_atm / rv

        if ratio >= self.FIXED_THRESHOLD:
            return SignalDecision(False, atm, ratio, self.FIXED_THRESHOLD,
                                  f"iv_rv={ratio:.3f} >= {self.FIXED_THRESHOLD} "
                                  f"(IV not cheap enough)")

        # Regime gate (same as other signals)
        regime = self._regime_now(now_dt)
        if regime == "trend_up":
            return SignalDecision(False, atm, ratio, self.FIXED_THRESHOLD,
                                  f"regime=trend_up — refused (noise regime)")

        return SignalDecision(True, atm, ratio, self.FIXED_THRESHOLD,
                              f"iv_rv={ratio:.3f} < {self.FIXED_THRESHOLD} "
                              f"regime={regime}")


# ── Registry ─────────────────────────────────────────────────────────────────

ALL_SIGNALS = [StraddleLevelSignal, StraddleMom3Signal, PcrMom3Signal, IvCheapSignal]
