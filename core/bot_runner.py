"""
BotRunner — runs ATR Intraday strategy inside the FastAPI process.

Uses APScheduler (AsyncIOScheduler) so it shares FastAPI's event loop.
The WebSocket loop in server.py already broadcasts _build_snapshot() every 5 s,
so bot_runner only needs to write trades to TradeMemory — no separate broadcast needed.

Strategy:
  ATR Intraday — AishDoc multi-signal, Claude AI scoring, -10 to +10 (via watchlist)
"""

import asyncio
import logging
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional

import numpy as np

IST = ZoneInfo("Asia/Kolkata")

import config
from core.memory import TradeMemory
from core import ipc
from core.trade_analyst import generate_entry_remark, generate_exit_remark

logger = logging.getLogger(__name__)

# ── market data helpers ───────────────────────────────────────────────────────

def _fetch_intraday(symbol: str, interval: str):
    """
    Fetch today's intraday OHLCV + 60-day daily closes.

    Priority:
      1. Zerodha via jugaad-trader — real NSE data, correct volume
      2. NSE India public API    — official source, no login needed

    Returns (opens, highs, lows, closes, volumes, all_closes, last_bar_time)
    or None if both sources fail.
    """
    # ── 1. Zerodha (real data, real volume) ────────────────────────────────
    try:
        from data.zerodha_fetcher import ZerodhaFetcher
        result = ZerodhaFetcher.get().fetch_intraday(symbol, interval)
        if result is not None:
            return result
        logger.warning("_fetch_intraday: Zerodha returned None for %s %s — trying NSE fallback", symbol, interval)
    except Exception as e:
        logger.warning("_fetch_intraday: Zerodha error (%s) — trying NSE fallback", e)

    # ── 2. NSE India public API (official source, no volume for index) ─────
    try:
        from data.nse_fetcher import NseFetcher
        result = NseFetcher.get().fetch_intraday(symbol, interval)
        if result is not None:
            logger.info("_fetch_intraday: using NSE India fallback for %s %s", symbol, interval)
            return result
        logger.error("_fetch_intraday: NSE fallback also returned None for %s %s — skipping cycle", symbol, interval)
    except Exception as e:
        logger.error("_fetch_intraday: NSE fallback error for %s %s: %s — skipping cycle", symbol, interval, e)

    return None


def _is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 15) <= t <= dtime(15, 30)


def _is_event_blocked() -> bool:
    """Return True if today is in EVENT_BLOCK_DATES (Budget/RBI MPC etc.)."""
    today_str = date.today().isoformat()
    blocked = config.EVENT_BLOCK_DATES.get(today_str)
    if blocked:
        logger.warning("Trading BLOCKED today — %s (%s). Skipping all cycles.", today_str, blocked)
    return bool(blocked)


# ── per-day state (reset at midnight) ─────────────────────────────────────────

class _DailyState:
    def __init__(self):
        self._date: Optional[date] = None
        self._trades: dict = {}        # strategy -> count
        self._positions: dict = {}     # strategy -> position dict or None

    def _maybe_reset(self):
        today = date.today()
        if self._date != today:
            self._date = today
            self._trades.clear()
            self._positions.clear()
            logger.info("Daily state reset for %s", today)

    def can_trade(self, strategy: str, max_trades: int) -> bool:
        self._maybe_reset()
        return self._trades.get(strategy, 0) < max_trades

    def record_trade(self, strategy: str):
        self._maybe_reset()
        self._trades[strategy] = self._trades.get(strategy, 0) + 1

    def get_position(self, strategy: str) -> Optional[dict]:
        self._maybe_reset()
        return self._positions.get(strategy)

    def set_position(self, strategy: str, pos: Optional[dict]):
        self._maybe_reset()
        if pos is None:
            self._positions.pop(strategy, None)
        else:
            self._positions[strategy] = pos

    def all_open_positions(self) -> list:
        self._maybe_reset()
        return [p for p in self._positions.values() if p]


# ── BotRunner ─────────────────────────────────────────────────────────────────

class BotRunner:
    """
    Manages all 3 strategy loops as async scheduled jobs.
    Start it once from FastAPI lifespan; it runs until the process exits.
    """

    def __init__(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        self.scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
        self.memory = TradeMemory()
        self.state = _DailyState()
        self._atr_strategy = None   # lazy-init TrendStrategy
        self.last_heartbeat: Optional[str] = None   # ISO string, IST
        self.last_scores: dict = {}                 # strategy → last signal scores
        self.last_vix: Optional[float] = None       # last fetched India VIX

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        ipc.clear_all_flags()

        # Warm up Zerodha login so first cycle doesn't pay the auth cost
        try:
            from data.zerodha_fetcher import ZerodhaFetcher
            ZerodhaFetcher.get()._ensure_logged_in()
        except Exception as e:
            logger.warning("Zerodha warm-up failed (will retry per cycle): %s", e)

        now_ist = datetime.now(IST)

        # VIX fetch at 9:20 — before trading starts, once per day
        self.scheduler.add_job(self._fetch_vix, "cron", hour=9, minute=20, id="vix_fetch")

        # ATR Intraday — every 5 min
        self.scheduler.add_job(
            self._atr_cycle, "interval", minutes=5,
            id="atr_intraday", next_run_time=now_ist,
        )
        # EOD square-off at 15:15, journal save at 15:20
        self.scheduler.add_job(self._eod_squareoff, "cron", hour=15, minute=15, id="eod")
        self.scheduler.add_job(self._save_journal,  "cron", hour=15, minute=20, id="journal")

        self.scheduler.start()
        logger.info("BotRunner started — ATR Intraday(5m)")

    def stop(self):
        self.scheduler.shutdown(wait=False)

    @property
    def paused(self) -> bool:
        return ipc.flag_exists(ipc.FLAG_PAUSE)

    # ── VIX fetch (9:20 daily) ────────────────────────────────────────────────

    async def _fetch_vix(self):
        try:
            from data.zerodha_fetcher import ZerodhaFetcher
            loop = asyncio.get_event_loop()
            vix = await loop.run_in_executor(None, ZerodhaFetcher.get().fetch_vix)
            self.last_vix = vix
            if vix is not None:
                threshold = config.VIX_THRESHOLD
                if vix > threshold:
                    logger.warning("India VIX %.2f > threshold %.1f — all new entries BLOCKED today.", vix, threshold)
                else:
                    logger.info("India VIX %.2f — below threshold %.1f, trading allowed.", vix, threshold)
        except Exception as e:
            logger.warning("VIX fetch job failed: %s", e)

    def _is_vix_blocked(self) -> bool:
        """Return True if VIX was fetched and exceeds the configured threshold."""
        if self.last_vix is None:
            return False  # no data = don't block (fail open)
        return self.last_vix > config.VIX_THRESHOLD

    # ── ATR Intraday (TrendStrategy / Claude) ─────────────────────────────────

    async def _atr_cycle(self):
        self.last_heartbeat = datetime.now(IST).isoformat()
        if self.paused or not _is_market_hours() or _is_event_blocked():
            return
        if self._is_vix_blocked():
            logger.warning("ATR cycle skipped — India VIX %.2f > threshold %.1f", self.last_vix, config.VIX_THRESHOLD)
            return
        try:
            if self._atr_strategy is None:
                from strategies.trend_strategy import TrendStrategy
                self._atr_strategy = TrendStrategy()

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._atr_strategy.run_watchlist)
        except Exception as e:
            logger.error("ATR Intraday cycle: %s", e, exc_info=True)

    # ── EOD square-off ────────────────────────────────────────────────────────

    async def _eod_squareoff(self):
        logger.info("EOD square-off triggered")
        if self._atr_strategy:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._atr_strategy.square_off_all)

    async def _save_journal(self):
        """Save daily journal JSON at 15:20 (after EOD square-off)."""
        try:
            from core.journal import save_daily_journal
            loop = asyncio.get_event_loop()
            path = await loop.run_in_executor(None, save_daily_journal)
            logger.info("Journal saved: %s", path)
        except Exception as e:
            logger.error("Journal save failed: %s", e, exc_info=True)

    # ── trade helpers ─────────────────────────────────────────────────────────

    async def _generate_entry_remark(self, pos: dict, sig: dict):
        """Run Claude entry remark in background thread — non-blocking."""
        try:
            loop = asyncio.get_event_loop()
            remark = await loop.run_in_executor(
                None, lambda: generate_entry_remark(pos, sig)
            )
            if remark:
                self.memory.update_remarks(pos["order_id"], entry_remark=remark)
                logger.info("[%s] Entry remark saved.", pos["strategy"])
        except Exception as e:
            logger.debug("Entry remark failed: %s", e)

    async def _generate_exit_remark(self, pos: dict, close_price: float, pnl: float, reason: str):
        """Run Claude exit remark in background thread — non-blocking."""
        try:
            loop = asyncio.get_event_loop()
            remark = await loop.run_in_executor(
                None, lambda: generate_exit_remark(pos, close_price, pnl, reason)
            )
            if remark:
                self.memory.update_remarks(pos["order_id"], exit_remark=remark)
                logger.info("[%s] Exit remark saved.", pos["strategy"])
        except Exception as e:
            logger.debug("Exit remark failed: %s", e)

    def _open_trade(self, strategy: str, side: str, entry_spot: float,
                    sl: float, tp: float, score: float,
                    option_type: str = "CE", strike: int = 0,
                    expiry=None, opt_sym: str = "", entry_prem: float = 0.0) -> dict:
        ts       = datetime.now().isoformat()
        order_id = f"{strategy.upper()}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        lot_qty  = config.LOT_SIZES.get("NIFTY", 25)
        expiry_str = expiry.isoformat() if expiry and hasattr(expiry, "isoformat") else str(expiry or "")
        order = {
            "order_id":    order_id,
            "symbol":      opt_sym or f"NIFTY{strike}{option_type}",
            "side":        side,
            "quantity":    lot_qty,
            "price":       round(entry_prem, 2),     # option premium paid
            "pnl":         0,
            "status":      "OPEN",
            "timestamp":   ts,
            "strategy":    strategy,
            "option_type": option_type,
            "strike":      strike,
            "lot_size":    lot_qty,
            "expiry":      expiry_str,
            "sl_price":    round(sl, 2),
            "tp_price":    round(tp, 2),
            "score":       score,
            "nifty_entry": round(entry_spot, 2),     # spot at entry (for fallback delta calc)
        }
        decision = {
            "reasoning": (f"{strategy} score={score:.1f} | {option_type} strike={strike} "
                          f"prem=₹{entry_prem:.2f} spot={entry_spot:.0f}"),
            "confidence": min(score / 10.0, 1.0),
            "risk_level": "MEDIUM",
        }
        self.memory.log_trade(order, decision)
        logger.info(
            "[%s] ENTRY %s %s @ ₹%.2f (NIFTY spot=%.0f) | SL=%.2f TP=%.2f score=%.1f lot=%d",
            strategy, side, opt_sym or f"{option_type}{strike}",
            entry_prem, entry_spot, sl, tp, score, lot_qty,
        )
        return {
            "strategy":    strategy,
            "order_id":    order_id,
            "symbol":      opt_sym or f"NIFTY{strike}{option_type}",
            "side":        side,
            "entry":       round(entry_prem, 2),   # entry = option premium
            "nifty_entry": round(entry_spot, 2),
            "sl":          round(sl, 2),
            "tp":          round(tp, 2),
            "qty":         lot_qty,
            "score":       score,
            "timestamp":   ts,
            "option_type": option_type,
            "strike":      strike,
            "expiry":      expiry_str,
        }

    def _close_trade(self, pos: dict, close_price: float, pnl: float, reason: str):
        ts = datetime.now().isoformat()
        order_id = f"{pos['strategy'].upper()}-CLOSE-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        order = {
            "order_id":    order_id,
            "symbol":      pos["symbol"],
            "side":        "SELL" if pos["side"] == "BUY" else "BUY",
            "quantity":    pos["qty"],
            "price":       close_price,
            "pnl":         pnl,
            "status":      "COMPLETE",
            "timestamp":   ts,
            "strategy":    pos["strategy"],
            "option_type": pos.get("option_type"),
            "strike":      pos.get("strike"),
            "lot_size":    pos["qty"],
            "close_reason":reason,
            "score":       pos.get("score"),
        }
        decision = {
            "reasoning": f"{pos['strategy']} closed: {reason} | PnL=₹{pnl:.2f}",
            "confidence": 1.0,
            "risk_level": "LOW",
        }
        self.memory.log_trade(order, decision)
        self.memory.close_trade(pos["order_id"], pnl)
        logger.info("[%s] CLOSE %s @ ₹%.2f | PnL=₹%.2f | %s",
                    pos["strategy"], pos["symbol"], close_price, pnl, reason)


# ── singleton ─────────────────────────────────────────────────────────────────

_runner: Optional[BotRunner] = None


def get_runner() -> BotRunner:
    global _runner
    if _runner is None:
        _runner = BotRunner()
    return _runner
