"""
In-memory market state — the live truth source for signal computation.

Replaces the "read latest snapshot from Mongo" path that imposed up-to-5min
lag on signal features. Now:

  Angel WebSocket tick → MarketState.on_tick(...) → in-memory dict
                                                        ↑
  Signal.compute()  ───reads from────────────────────────┘

The Mongo `option_snapshots` collection is still written (by the separate
collector container) for backtest continuity, but signal computation never
reads from Mongo during normal operation.

Three layers maintained per (exchange, token):
  1. latest          — last known LTP + OI + ts (one dict)
  2. tick buffer     — last ~300 raw ticks (for intra-bar analytics later)
  3. 5-min bar       — current in-progress bar + last 12 closed bars (deque)

Each strike has its own state. Spot (NSE index) has its own dedicated bar.

Thread-safety:
  Ticks arrive on the WebSocket thread, signals read on the scheduler thread.
  All mutations guarded by per-key locks; reads return shallow copies.

Cold-start backfill (Phase 4) populates the closed-bar deques from Mongo
before signals are allowed to fire.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

BAR_MINUTES        = 5     # match the historical snapshot cadence
ROLLING_BARS_KEPT  = 12    # 60 min — enough for RV calc + 3-bar momentum


def _bar_start(now_dt: datetime) -> datetime:
    """Round a datetime DOWN to the start of its 5-min bar."""
    floored_min = (now_dt.minute // BAR_MINUTES) * BAR_MINUTES
    return now_dt.replace(minute=floored_min, second=0, microsecond=0)


class _TokenState:
    """Per-(exchange, token) live state. Internal — managed by MarketState."""

    def __init__(self, exchange: str, token: str,
                  strike: Optional[int] = None,
                  option_type: Optional[str] = None):
        self.exchange    = exchange
        self.token       = token
        self.strike      = strike
        self.option_type = option_type
        self.lock        = threading.Lock()

        # Latest single tick
        self.latest_ltp: Optional[float] = None
        self.latest_oi:  Optional[int]   = None
        self.latest_ts:  Optional[datetime] = None

        # Closed 5-min bars (oldest → newest), tuples of:
        #   (bar_start_dt, open, high, low, close, final_oi)
        self.bars: deque = deque(maxlen=ROLLING_BARS_KEPT)

        # In-progress bar accumulator
        self._cur_bar_start: Optional[datetime] = None
        self._cur_open      = None
        self._cur_high      = None
        self._cur_low       = None
        self._cur_close     = None
        self._cur_oi        = None

    def _roll_if_needed(self, tick_dt: datetime) -> None:
        """If tick crossed into a new 5-min bar, close prior bar to deque."""
        new_bar_start = _bar_start(tick_dt)
        if self._cur_bar_start is None:
            self._cur_bar_start = new_bar_start
            return
        if new_bar_start != self._cur_bar_start:
            # Close prior bar
            if self._cur_open is not None:
                self.bars.append((
                    self._cur_bar_start, self._cur_open, self._cur_high,
                    self._cur_low, self._cur_close, self._cur_oi,
                ))
            # Start fresh bar
            self._cur_bar_start = new_bar_start
            self._cur_open  = None
            self._cur_high  = None
            self._cur_low   = None
            self._cur_close = None
            self._cur_oi    = None

    def on_tick(self, ltp: Optional[float], oi: Optional[int],
                tick_dt: datetime) -> None:
        with self.lock:
            self._roll_if_needed(tick_dt)

            if ltp is not None and ltp > 0:
                if self._cur_open is None:
                    self._cur_open = ltp
                    self._cur_high = ltp
                    self._cur_low  = ltp
                else:
                    if ltp > self._cur_high: self._cur_high = ltp
                    if ltp < self._cur_low:  self._cur_low  = ltp
                self._cur_close = ltp
                self.latest_ltp = ltp

            if oi is not None:
                self._cur_oi   = oi
                self.latest_oi = oi

            self.latest_ts = tick_dt

    def seed_bar(self, bar_start: datetime, o: float, h: float, l: float,
                 c: float, oi: int) -> None:
        """Backfill a closed bar from Mongo (oldest-first calls)."""
        with self.lock:
            self.bars.append((bar_start, o, h, l, c, oi))
            self.latest_ltp = c
            self.latest_oi  = oi
            self.latest_ts  = bar_start + timedelta(minutes=BAR_MINUTES)

    def snapshot(self) -> dict:
        """Read-side: returns a shallow snapshot of state (safe to use without lock)."""
        with self.lock:
            return {
                "exchange":    self.exchange,
                "token":       self.token,
                "strike":      self.strike,
                "option_type": self.option_type,
                "latest_ltp":  self.latest_ltp,
                "latest_oi":   self.latest_oi,
                "latest_ts":   self.latest_ts,
                "bars":        list(self.bars),  # copy
                "cur_close":   self._cur_close,
            }


class MarketState:
    """Singleton live market state. Read from signals, written from WS."""

    def __init__(self):
        self._tokens: dict = {}    # (exchange, token) -> _TokenState
        self._strike_idx: dict = {}  # (strike, side) -> (exchange, token)
        self._index_lock = threading.Lock()
        self._is_ready = False     # set True after cold-start backfill
        self._spot_token: Optional[str] = None
        self._spot_exchange: str = "NSE"
        self._registry_lock = threading.Lock()

    # ── Registration ────────────────────────────────────────────────────────

    def register_option(self, exchange: str, token: str, strike: int,
                         option_type: str) -> None:
        """Register an option-strike token so signals can look it up by
        (strike, option_type)."""
        key = (exchange, str(token))
        with self._registry_lock:
            if key not in self._tokens:
                self._tokens[key] = _TokenState(exchange, str(token),
                                                  strike, option_type)
            self._strike_idx[(int(strike), option_type)] = key

    def register_spot(self, exchange: str, token: str) -> None:
        """Tell market state which token is NIFTY spot."""
        self._spot_exchange = exchange
        self._spot_token    = str(token)
        key = (exchange, str(token))
        with self._registry_lock:
            if key not in self._tokens:
                self._tokens[key] = _TokenState(exchange, str(token))

    def unregister_option(self, exchange: str, token: str) -> None:
        """Drop an option token (called when ATM shifts and we stop watching)."""
        key = (exchange, str(token))
        with self._registry_lock:
            state = self._tokens.pop(key, None)
            if state and state.strike is not None and state.option_type is not None:
                self._strike_idx.pop((state.strike, state.option_type), None)

    # ── Tick ingest (called from WebSocket thread) ──────────────────────────

    def on_tick(self, tick: dict) -> None:
        key = (tick.get("exchange"), str(tick.get("token", "")))
        state = self._tokens.get(key)
        if state is None:
            return   # not subscribed / not registered
        ltp = tick.get("ltp")
        oi  = tick.get("oi")
        ts  = tick.get("received_at") or datetime.now(IST)
        # Normalise tz-naive for internal comparisons
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        ts = ts.astimezone(IST).replace(tzinfo=None)
        state.on_tick(ltp, oi, ts)

    # ── Read API (called from signal thread) ────────────────────────────────

    def is_ready(self) -> bool:
        return self._is_ready

    def mark_ready(self) -> None:
        self._is_ready = True
        logger.info("MarketState: marked ready (cold-start backfill complete)")

    def get_spot(self) -> Optional[float]:
        if self._spot_token is None:
            return None
        state = self._tokens.get((self._spot_exchange, self._spot_token))
        if state is None:
            return None
        return state.latest_ltp

    def get_option(self, strike: int, option_type: str) -> Optional[dict]:
        key = self._strike_idx.get((int(strike), option_type))
        if key is None:
            return None
        state = self._tokens.get(key)
        if state is None:
            return None
        return state.snapshot()

    def all_option_snapshots(self) -> list:
        """All currently-registered option tokens as snapshot dicts."""
        with self._registry_lock:
            return [s.snapshot() for k, s in self._tokens.items()
                    if s.strike is not None]

    def registered_strikes(self) -> list:
        """List of (strike, option_type) tuples currently in the index."""
        with self._registry_lock:
            return list(self._strike_idx.keys())

    def diagnostics(self) -> dict:
        with self._registry_lock:
            return {
                "is_ready":            self._is_ready,
                "registered_tokens":   len(self._tokens),
                "registered_strikes":  len(self._strike_idx),
                "spot_token":          self._spot_token,
                "spot_value":          self.get_spot(),
                "latest_ts": (max((s.latest_ts for s in self._tokens.values()
                                    if s.latest_ts), default=None) or "—"),
            }


    # ── Cold-start backfill from Mongo ──────────────────────────────────────

    def cold_start_from_mongo(self, today_only: bool = True) -> dict:
        """Pre-fill the rolling-bar deques from option_snapshots.

        After bot restart the in-memory state is empty — momentum signals
        need 3 prior bars and the IV-cheap signal needs 12. This reads the
        last ROLLING_BARS_KEPT bars from Mongo and seeds each registered
        strike's bar deque, then marks the state ready.

        Returns a summary dict: {tokens_seeded, bars_seeded, missing_strikes}.
        Safe to call multiple times — bars are appended, but the deque is
        bounded so old ones drop off.
        """
        summary = {"tokens_seeded": 0, "bars_seeded": 0,
                    "missing_strikes": [], "skipped_no_state": 0}
        try:
            from core import mongo
            db = mongo.get_db()
            if db is None:
                logger.warning("MarketState.cold_start: Mongo unreachable")
                self._is_ready = True   # still allow live ticks to drive state
                return summary

            today = datetime.now(IST).date().isoformat()
            query: dict = {"symbol": "NIFTY"}
            if today_only:
                query["date"] = today

            # Get last ROLLING_BARS_KEPT distinct timestamps
            distinct_ts = sorted(db.option_snapshots.distinct("timestamp", query))
            usable_ts = distinct_ts[-ROLLING_BARS_KEPT:]
            if not usable_ts:
                logger.info("MarketState.cold_start: no historical snapshots "
                            "for backfill (today_only=%s)", today_only)
                self._is_ready = True
                return summary

            for ts in usable_ts:
                rows = list(db.option_snapshots.find(
                    {**query, "timestamp": ts},
                    projection={"_id": 0, "strike": 1, "option_type": 1,
                                "ltp": 1, "oi": 1, "spot": 1},
                ))
                # Bar timestamp normalisation
                try:
                    bar_dt = datetime.fromisoformat(ts.replace(" ", "T"))
                except Exception:
                    continue
                if bar_dt.tzinfo is not None:
                    bar_dt = bar_dt.astimezone(IST).replace(tzinfo=None)
                bar_start = _bar_start(bar_dt)

                for r in rows:
                    strike = int(r.get("strike", 0))
                    side   = r.get("option_type", "")
                    key = self._strike_idx.get((strike, side))
                    if key is None:
                        # Not yet registered — happens for strikes outside the
                        # current ATM±N window. Track once for diagnostics.
                        if (strike, side) not in summary["missing_strikes"]:
                            summary["missing_strikes"].append((strike, side))
                        summary["skipped_no_state"] += 1
                        continue
                    state = self._tokens.get(key)
                    if state is None:
                        continue
                    ltp = float(r.get("ltp", 0) or 0)
                    oi  = int(r.get("oi", 0) or 0)
                    if ltp > 0:
                        # Treat snapshot LTP as O=H=L=C for the seeded bar
                        state.seed_bar(bar_start, ltp, ltp, ltp, ltp, oi)
                        summary["bars_seeded"] += 1

                # Also seed spot from this bar
                if rows and rows[0].get("spot"):
                    spot_key = (self._spot_exchange, self._spot_token or "")
                    spot_state = self._tokens.get(spot_key) if self._spot_token else None
                    if spot_state is not None:
                        s = float(rows[0]["spot"])
                        spot_state.seed_bar(bar_start, s, s, s, s, 0)

            summary["tokens_seeded"] = sum(
                1 for s in self._tokens.values() if len(s.bars) > 0
            )
            self._is_ready = True
            logger.info("MarketState.cold_start: seeded %d bars across %d tokens "
                        "(missing %d strike-keys)",
                        summary["bars_seeded"], summary["tokens_seeded"],
                        summary["skipped_no_state"])
        except Exception as e:
            logger.warning("MarketState.cold_start failed: %s", e)
            self._is_ready = True   # don't block forever on backfill error
        return summary


# ── Singleton ─────────────────────────────────────────────────────────────

_instance: Optional[MarketState] = None
_singleton_lock = threading.Lock()


def get_state() -> MarketState:
    global _instance
    with _singleton_lock:
        if _instance is None:
            _instance = MarketState()
    return _instance
