"""
BotRunner — runs the Q5 multi-strategy SHADOW executor inside FastAPI.

Uses APScheduler (AsyncIOScheduler) so it shares FastAPI's event loop. The
WebSocket loop in server.py broadcasts state every 5s — bot_runner just
drives the schedulers.

What this bot does:
  • Polls 3 independent shadow signals every 30 s during market hours:
      q5_straddle_level  q5_straddle_mom3  q5_pcr_mom3
  • Opens / closes SIMULATED trades only (Mongo collection shadow_trades).
    NO real Angel One orders are placed by this process.
  • Refreshes the Angel One JWT three times a day (08:30 / 12:00 / 14:00 IST).
  • Refreshes the option-chain panel for the dashboard every 15 min.
  • Writes a daily journal at 15:20 IST summarising shadow performance.

What this bot does NOT do:
  • Trade ATR Intraday or any other real-money strategy (removed).
  • Hold any live positions in the broker account.
  • Manage SL/TP for live trades (no live trades exist).

Hard rules:
  • Linux-only (`_is_cloud_host`) — refuses to schedule on macOS/Windows.
  • Market-hours-only — every job no-ops outside 09:15–15:30 IST.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

import config
from core import ipc
from core.memory import TradeMemory
from core.utils import now_ist

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_cloud_host() -> bool:
    """True only on Linux (the droplet). Prevents laptop double-runs.
    Override via ENABLE_LOCAL_SCHEDULERS=1 — use with extreme care."""
    if os.environ.get("ENABLE_LOCAL_SCHEDULERS") == "1":
        return True
    return platform.system() == "Linux"


def _is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 15) <= t <= dtime(15, 30)


def _is_event_blocked() -> bool:
    """True if today is on the event-block list (config or runtime) AND not
    explicitly unblocked via the dashboard."""
    today_str = now_ist().date().isoformat()
    if today_str in ipc.read_event_unblocks():
        logger.info("Trading UNBLOCKED today — %s (manual override).", today_str)
        return False
    blocked = (config.EVENT_BLOCK_DATES.get(today_str)
               or ipc.read_event_blocks().get(today_str))
    if blocked:
        logger.warning("Trading BLOCKED today — %s (%s). Skipping cycles.",
                       today_str, blocked)
    return bool(blocked)


# ── BotRunner ────────────────────────────────────────────────────────────────

class BotRunner:
    """Drives the shadow executor scheduler. Stop/start via FastAPI lifespan."""

    def __init__(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self.scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
        self.memory = TradeMemory()
        self.last_heartbeat: Optional[str] = None   # ISO IST
        self.last_option_chain: dict = {}
        # Multi-strategy shadow signals (one (signal, book) per element)
        self._shadow_signals: list = []
        self._shadow_books: dict = {}
        self._paused: bool = False
        # Live tick infrastructure (WebSocket + market state + sub manager)
        self._ws_client = None
        self._market_state = None
        self._sub_manager = None

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True
        logger.info("BotRunner: paused")

    def resume(self) -> None:
        self._paused = False
        logger.info("BotRunner: resumed")

    def start(self) -> None:
        if not _is_cloud_host():
            logger.warning("BotRunner.start: not a cloud host (Linux) — schedulers OFF. "
                           "Set ENABLE_LOCAL_SCHEDULERS=1 to override.")
            return

        # ── Live tick infrastructure (sub-second lag instead of up-to-5min) ──
        # Start the Angel One WebSocket, prime market state with backfill,
        # wire the subscription manager. All three are best-effort — if
        # the WebSocket can't connect, the signals fall back to Mongo reads.
        try:
            from data.angel_websocket import get_client as _get_ws
            from core.market_state import get_state as _get_market_state
            from core.subscription_manager import get_manager as _get_sub_mgr

            self._ws_client    = _get_ws()
            self._market_state = _get_market_state()
            self._sub_manager  = _get_sub_mgr(self._ws_client, self._market_state)

            # Wire WebSocket ticks → market_state
            self._ws_client.on_tick(self._market_state.on_tick)

            # Cold-start backfill BEFORE starting WS (so signals see history)
            self._market_state.cold_start_from_mongo(today_only=True)

            self._ws_client.start()
            self._sub_manager.start()

            # First subscription kick — once spot arrives, refresh registers strikes
            self.scheduler.add_job(
                self._refresh_subscriptions, "interval", seconds=60,
                id="subscription_refresh",
            )
            logger.info("BotRunner: live tick stack started (WS + MarketState + SubManager)")
        except Exception as e:
            logger.error("BotRunner: failed to start live tick stack — "
                          "signals will fall back to Mongo reads: %s", e, exc_info=True)

        # Option chain panel refresh — every 15 min (dashboard widget)
        self.scheduler.add_job(
            self._option_chain_refresh, "cron", minute="*/15", second=20,
            id="option_chain_refresh",
        )
        # Daily Angel One JWT refresh — 08:30 / 12:00 / 14:00 IST
        # KEPT: required for the option_snapshots collector to authenticate.
        for h, m in [(8, 30), (12, 0), (14, 0)]:
            self.scheduler.add_job(
                self._daily_token_refresh, "cron", hour=h, minute=m,
                id=f"token_refresh_{h:02d}{m:02d}",
            )

        # NSE shadow strategies (Q5 ensemble) — REMOVED per user direction.
        # Data collection (scripts/collect_option_snapshots.py) + Angel One
        # auth remain. Crypto strategies run via core/crypto_runner.py.

        self.scheduler.start()
        logger.info("BotRunner started — OptionChain(15m) TokenRefresh(x3) "
                    "(NSE strategies removed; data collection only)")

    def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
        # Tear down live tick infra
        try:
            if self._sub_manager is not None:
                self._sub_manager.stop()
        except Exception:
            pass
        try:
            if self._ws_client is not None:
                self._ws_client.stop()
        except Exception:
            pass

    async def _refresh_subscriptions(self):
        """Periodic call to SubscriptionManager.refresh() — rotates strikes
        when spot drifts more than the configured threshold."""
        if self._paused or not _is_market_hours():
            return
        try:
            if self._sub_manager is not None:
                # SubscriptionManager.refresh is synchronous; run in executor
                # to keep the event loop free.
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._sub_manager.refresh)
        except Exception as e:
            logger.debug("_refresh_subscriptions failed (non-fatal): %s", e)

    # ── Q5 multi-strategy SHADOW signal tick ────────────────────────────────

    async def _shadow_signal_tick(self):
        """Drives all 3 shadow signals: open/close simulated trades against
        live LTP. No real orders are ever placed.

        Flow:
          1. Read latest option_snapshot bar from Mongo
          2. For each of 3 signals:
               a. If position is open: fetch live LTP, tick book for SL/TP/EOD
               b. Else: compute signal; if it fires, fetch live ITM-CE LTP and open
        """
        # NSE shadow strategies removed — this method is kept as a no-op stub
        # so any leftover scheduler references don't crash. Crypto strategies
        # live in core/crypto_runner.py.
        return
        # legacy code below intentionally unreachable
        if self._paused or not _is_market_hours():
            return
        self.last_heartbeat = datetime.now(IST).isoformat()
        try:
            from core.shadow_book import ShadowBook
            from core import mongo as _mongo
            from data.angel_fetcher import AngelFetcher

            # First call: instantiate all signals + their books
            if not self._shadow_signals:
                self._shadow_signals = [cls() for cls in ALL_SIGNALS]
                self._shadow_books = {s.name: ShadowBook(s.name)
                                       for s in self._shadow_signals}
                logger.info("shadow signals initialised: %s",
                            ", ".join(s.name for s in self._shadow_signals))

            now_dt = datetime.now(IST).replace(tzinfo=None)
            db = _mongo.get_db()
            if db is None:
                logger.debug("shadow_signal: Mongo unreachable — skipping tick")
                return

            today_bars = _today_bars(db, now_dt.date())
            if not today_bars:
                logger.debug("shadow_signal: no today bars yet — skipping")
                return
            latest_ts = max(today_bars.keys())
            current_rows = today_bars[latest_ts]
            spot = current_rows[0].get("spot")
            if not spot:
                return
            atm           = _atm_strike_for(float(spot))
            chosen_strike = _chosen_strike_for(float(spot))   # ITM-50 for CE

            af = AngelFetcher.get()
            expiry = af.nearest_weekly_expiry()

            for sig in self._shadow_signals:
                book = self._shadow_books[sig.name]

                # Open position? Tick against live LTP for SL/TP/EOD.
                if book.has_open():
                    pos = book.open_position()
                    _, ltp = af.get_option_ltp("NIFTY", int(pos["strike"]),
                                                pos["side"], expiry)
                    if ltp is None:
                        continue
                    closed = book.tick(now_dt, float(ltp))
                    if closed:
                        logger.info("shadow[%s] closed %s pnl=Rs %+.2f reason=%s",
                                    sig.name, closed["signal_id"],
                                    closed.get("pnl", 0), closed.get("reason", ""))
                    continue

                # No open position — compute signal value
                decision = sig.compute(now_dt, float(spot), current_rows)
                logger.info("shadow[%s]: val=%.4f thr=%s fire=%s",
                            sig.name, decision.current_value,
                            f"{decision.threshold:.4f}" if decision.threshold else "N/A",
                            decision.fire)
                if not decision.fire:
                    continue

                # Fire — fetch live LTP for the ITM strike
                _, ce_ltp = af.get_option_ltp("NIFTY", chosen_strike,
                                                decision.side, expiry)
                if not ce_ltp:
                    logger.warning("shadow[%s] fired but %d CE LTP fetch failed",
                                   sig.name, chosen_strike)
                    continue
                book.open(now_dt, chosen_strike, decision.side, float(ce_ltp),
                          decision.threshold, float(spot))
        except Exception as e:
            logger.warning("shadow_signal_tick failed (non-fatal): %s", e)

    # ── Option chain panel refresh (every 15 min) ───────────────────────────

    async def _option_chain_refresh(self):
        """Pull a fresh ATM-±N option chain for the dashboard panel.
        Not used by signal logic — that path reads from option_snapshots."""
        if not _is_market_hours():
            return
        try:
            from data.option_chain import OptionChainFetcher
            loop = asyncio.get_event_loop()
            self.last_option_chain = await loop.run_in_executor(
                None, OptionChainFetcher().fetch_full
            ) or {}
        except Exception as e:
            logger.debug("option_chain_refresh failed (non-fatal): %s", e)

    # ── Daily Angel One JWT refresh ─────────────────────────────────────────

    async def _daily_token_refresh(self):
        """Re-login to Angel One — tokens live ~24h from issue.

        Three refreshes per day (08:30 / 12:00 / 14:00) ensure no single
        long session expires mid-day."""
        try:
            from data.angel_fetcher import AngelFetcher
            af = AngelFetcher.get()
            af._api = None
            af._login_date = None
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, af._ensure_logged_in)
            ok = bool(af._api)
            logger.info("Daily token refresh: %s", "OK" if ok else "FAILED")
        except Exception as e:
            logger.error("Daily token refresh failed: %s", e, exc_info=True)

    # ── Daily shadow journal (15:25 IST) ────────────────────────────────────

    async def _save_journal(self):
        """No-op stub — NSE shadow journal removed."""
        return


# ── Singleton accessor ──────────────────────────────────────────────────────

_runner: Optional[BotRunner] = None


def get_runner() -> BotRunner:
    global _runner
    if _runner is None:
        _runner = BotRunner()
    return _runner
