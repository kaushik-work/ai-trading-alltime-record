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
from core.utils import now_ist, today_ist
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
    Fetch today's intraday OHLCV + 60-day daily closes via Zerodha.
    Returns None if Zerodha fails — caller must skip the cycle.
    """
    from core.zerodha_error_log import log_error as _log_err
    try:
        from data.zerodha_fetcher import ZerodhaFetcher
        result = ZerodhaFetcher.get().fetch_intraday(symbol, interval)
        if result is not None:
            return result
        msg = "fetch_intraday returned None"
        logger.error("_fetch_intraday: Zerodha returned None for %s %s — skipping cycle", symbol, interval)
        _log_err("fetch_intraday", msg, symbol=symbol, detail=interval)
    except Exception as e:
        logger.error("_fetch_intraday: Zerodha error for %s %s: %s — skipping cycle", symbol, interval, e)
        _log_err("fetch_intraday", str(e), symbol=symbol, detail=interval)
    return None


def _is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 15) <= t <= dtime(15, 30)


def _is_event_blocked() -> bool:
    """Return True if today is in EVENT_BLOCK_DATES or runtime overrides."""
    today_str = now_ist().date().isoformat()
    blocked = config.EVENT_BLOCK_DATES.get(today_str) or ipc.read_event_blocks().get(today_str)
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
        today = now_ist().date()
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
        self._atr_strategy = None   # lazy-init TrendStrategy (ATR Intraday, atr_only mode)
        self._ict_strategy = None   # lazy-init TrendStrategy (C-ICT, ict_only mode)
        self._fib_strategy = None   # lazy-init TrendStrategy (Fib-OF, fib_of_only mode)
        self.last_heartbeat: Optional[str] = None   # ISO string, IST
        self.last_scores: dict = {}                 # strategy → last signal scores
        self.last_vix: Optional[float] = None       # last fetched India VIX
        self.last_day_bias: dict = ipc.read_day_bias()  # cached; updated by set_bias API

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

        # VIX fetch every 30 min during market hours (9:20, 9:50, ..., 15:20)
        self.scheduler.add_job(
            self._fetch_vix, "cron",
            day_of_week="mon-fri", hour="9-15", minute="20,50",
            id="vix_fetch",
        )

        # ATR Intraday — every 5 min
        self.scheduler.add_job(
            self._atr_cycle, "interval", minutes=5,
            id="atr_intraday", next_run_time=now_ist,
        )
        # C-ICT — every 5 min, offset +2m30s so it doesn't collide with ATR API calls
        from datetime import timedelta
        self.scheduler.add_job(
            self._ict_cycle, "interval", minutes=5,
            id="ict_intraday", next_run_time=now_ist + timedelta(minutes=2, seconds=30),
        )
        self.scheduler.add_job(
            self._fib_cycle, "interval", minutes=15,
            id="fib_of_intraday", next_run_time=now_ist + timedelta(minutes=1, seconds=15),
        )
        # EOD square-off at configured intraday cutoff, journal save after exits settle.
        exit_hour, exit_minute = map(int, config.INTRADAY_EXIT_BY.split(":"))
        self.scheduler.add_job(self._eod_squareoff, "cron", hour=exit_hour, minute=exit_minute, id="eod")
        self.scheduler.add_job(self._save_journal,  "cron", hour=15, minute=20, id="journal")
        # Reset day bias to NEUTRAL at 20:00 IST each evening
        self.scheduler.add_job(self._reset_day_bias, "cron", hour=20, minute=0, id="bias_reset")

        self.scheduler.start()
        logger.info("BotRunner started — ATR Intraday(5m) + C-ICT(5m, +2m30s offset) + Fib-OF(15m)")

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

    def _is_vix_blocked(self, strategy: str = "all") -> bool:
        """Return True if VIX exceeds threshold AND no override is set for this strategy.

        strategy: "ATR Intraday" | "C-ICT" | "all" (checks global override only)
        Per-strategy flags take precedence over VIX threshold.
        """
        # Check per-strategy override first
        per_flag = {
            "ATR Intraday": ipc.FLAG_VIX_OVERRIDE_ATR,
            "C-ICT":        ipc.FLAG_VIX_OVERRIDE_ICT,
            "Fib-OF":       ipc.FLAG_VIX_OVERRIDE_FIB,
        }.get(strategy)
        if per_flag and ipc.flag_exists(per_flag):
            return False  # per-strategy override active
        # Fall back to global override
        if ipc.flag_exists(ipc.FLAG_VIX_OVERRIDE):
            return False  # global override active
        if self.last_vix is None:
            return False  # no data = don't block (fail open)
        return self.last_vix > config.VIX_THRESHOLD

    # ── ATR Intraday (TrendStrategy / Claude) ─────────────────────────────────

    async def _atr_cycle(self):
        self.last_heartbeat = datetime.now(IST).isoformat()
        if self.paused or not _is_market_hours() or _is_event_blocked():
            return
        if self._is_vix_blocked("ATR Intraday"):
            # Allow cycle if trader has explicitly set a directional bias (they know the risk)
            bias = self.last_day_bias
            if bias.get("bias", "NEUTRAL") == "NEUTRAL" or not bias.get("set_at"):
                logger.warning("ATR cycle skipped — India VIX %.2f > threshold %.1f", self.last_vix, config.VIX_THRESHOLD)
                return
            logger.warning(
                "India VIX %.2f > threshold %.1f but trader bias is %s — ATR proceeding.",
                self.last_vix, config.VIX_THRESHOLD, bias["bias"],
            )
        try:
            if self._atr_strategy is None:
                from strategies.trend_strategy import TrendStrategy
                self._atr_strategy = TrendStrategy(strategy_name="ATR Intraday", score_mode="atr_only")

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._atr_strategy.run_watchlist)

            # Update last_scores for the debug/signal-radar endpoint
            sc = self._atr_strategy.last_score
            if sc:
                self.last_scores["ATR Intraday"] = {
                    "score": sc.get("score", 0),
                    "direction": sc.get("action", "HOLD"),
                    "action": sc.get("action", "HOLD"),
                    "threshold": sc.get("threshold", 6),
                    "will_trade": abs(sc.get("score", 0)) >= sc.get("threshold", 6),
                    "note": "ATR technical analysis only (sections 1–11)",
                }
        except Exception as e:
            logger.error("ATR Intraday cycle: %s", e, exc_info=True)

    # ── C-ICT (Strategy C — ICT Order Blocks + Liquidity) ────────────────────

    async def _ict_cycle(self):
        self.last_heartbeat = datetime.now(IST).isoformat()
        if self.paused or not _is_market_hours() or _is_event_blocked():
            return
        if self._is_vix_blocked("C-ICT"):
            bias = self.last_day_bias
            if bias.get("bias", "NEUTRAL") == "NEUTRAL" or not bias.get("set_at"):
                logger.warning("ICT cycle skipped — India VIX %.2f > threshold %.1f", self.last_vix, config.VIX_THRESHOLD)
                return
            logger.warning(
                "India VIX %.2f > threshold %.1f but trader bias is %s — C-ICT proceeding.",
                self.last_vix, config.VIX_THRESHOLD, bias["bias"],
            )
        try:
            if self._ict_strategy is None:
                from strategies.trend_strategy import TrendStrategy
                self._ict_strategy = TrendStrategy(strategy_name="C-ICT", score_mode="ict_only")

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._ict_strategy.run_watchlist)

            # Update last_scores for the debug/signal-radar endpoint
            sc = self._ict_strategy.last_score
            if sc:
                self.last_scores["C-ICT"] = {
                    "score": sc.get("score", 0),
                    "direction": sc.get("action", "HOLD"),
                    "action": sc.get("action", "HOLD"),
                    "threshold": sc.get("threshold", 2),
                    "will_trade": abs(sc.get("score", 0)) >= sc.get("threshold", 2),
                    "note": "ICT order blocks + liquidity sweeps only (section 12)",
                    "order_flow": sc.get("order_flow", {}),
                }
        except Exception as e:
            logger.error("C-ICT cycle: %s", e, exc_info=True)

    # ── Fib-OF (Strategy F — Fibonacci Order Flow, 15m) ──────────────────────

    async def _fib_cycle(self):
        self.last_heartbeat = datetime.now(IST).isoformat()
        if self.paused or not _is_market_hours() or _is_event_blocked():
            return
        if self._is_vix_blocked("Fib-OF"):
            bias = self.last_day_bias
            if bias.get("bias", "NEUTRAL") == "NEUTRAL" or not bias.get("set_at"):
                logger.warning("Fib-OF cycle skipped — India VIX %.2f > threshold %.1f", self.last_vix, config.VIX_THRESHOLD)
                return
            logger.warning(
                "India VIX %.2f > threshold %.1f but trader bias is %s — Fib-OF proceeding.",
                self.last_vix, config.VIX_THRESHOLD, bias["bias"],
            )
        try:
            if self._fib_strategy is None:
                from strategies.trend_strategy import TrendStrategy
                self._fib_strategy = TrendStrategy(strategy_name="Fib-OF", score_mode="fib_of_only")

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._fib_strategy.run_watchlist)

            sc = self._fib_strategy.last_score
            if sc:
                self.last_scores["Fib-OF"] = {
                    "score":      sc.get("score", 0),
                    "direction":  sc.get("action", "HOLD"),
                    "action":     sc.get("action", "HOLD"),
                    "threshold":  sc.get("threshold", config.FIB_OF_SIGNAL_SCORE),
                    "will_trade": abs(sc.get("score", 0)) >= sc.get("threshold", config.FIB_OF_SIGNAL_SCORE),
                    "note":       f"Fib-OF 15m | R:R 1:{config.FIB_OF_RR_RATIO:.0f}",
                }
        except Exception as e:
            logger.error("Fib-OF cycle: %s", e, exc_info=True)

    # ── EOD square-off ────────────────────────────────────────────────────────

    async def _eod_squareoff(self):
        logger.info("EOD square-off triggered")
        loop = asyncio.get_event_loop()
        if self._atr_strategy:
            await loop.run_in_executor(None, self._atr_strategy.square_off_all)
        if self._ict_strategy:
            await loop.run_in_executor(None, self._ict_strategy.square_off_all)
        if self._fib_strategy:
            await loop.run_in_executor(None, self._fib_strategy.square_off_all)

    async def _save_journal(self):
        """Save daily journal JSON at 15:20 (after EOD square-off)."""
        try:
            from core.journal import save_daily_journal
            loop = asyncio.get_event_loop()
            path = await loop.run_in_executor(None, save_daily_journal)
            logger.info("Journal saved: %s", path)
        except Exception as e:
            logger.error("Journal save failed: %s", e, exc_info=True)

    async def _reset_day_bias(self):
        """Reset day bias to NEUTRAL at 20:00 IST each evening."""
        try:
            ipc.write_day_bias("NEUTRAL", "")
            self.last_day_bias = ipc.read_day_bias()
            logger.info("Day bias reset to NEUTRAL for tomorrow.")
        except Exception as e:
            logger.error("Bias reset failed: %s", e)

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
        ts       = now_ist().isoformat()
        order_id = f"{strategy.upper()}-{now_ist().strftime('%Y%m%d%H%M%S')}"
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
        ts = now_ist().isoformat()
        order_id = f"{pos['strategy'].upper()}-CLOSE-{now_ist().strftime('%Y%m%d%H%M%S')}"
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
