"""
BotRunner — runs all 3 strategies inside the FastAPI process.

Uses APScheduler (AsyncIOScheduler) so it shares FastAPI's event loop.
The WebSocket loop in server.py already broadcasts _build_snapshot() every 5 s,
so bot_runner only needs to write trades to TradeMemory — no separate broadcast needed.

Strategies:
  Musashi     — NIFTY 15-min EMA/VWAP/HA trend               (max 2 trades/day)
  Raijin      — NIFTY 5-min VWAP-band mean-reversion scalp   (max 3 trades/day)
  ATR Intraday — legacy TrendStrategy (Claude-based)          (via watchlist)
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

# ── yfinance helpers ──────────────────────────────────────────────────────────

_YF_MAP = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}


def _fetch_intraday(symbol: str, interval: str):
    """
    Fetch today's intraday OHLCV + 60-day daily closes (for EMA/RSI stability).
    Returns (opens, highs, lows, closes, volumes, all_closes, last_bar_time)
    or None on failure.
    """
    try:
        import yfinance as yf
        yf_sym = _YF_MAP.get(symbol, f"{symbol}.NS")
        ticker = yf.Ticker(yf_sym)

        df = ticker.history(period="1d", interval=interval, auto_adjust=True)
        if df is None or df.empty or len(df) < 3:
            return None

        df_daily = ticker.history(period="60d", interval="1d", auto_adjust=True)
        if df_daily is not None and not df_daily.empty:
            all_closes = df_daily["Close"].values.astype(float)
        else:
            all_closes = df["Close"].values.astype(float)

        last_idx = df.index[-1]
        if hasattr(last_idx, "tzinfo") and last_idx.tzinfo is not None:
            last_idx = last_idx.astimezone(IST)
        bar_time = last_idx.time()
        return (
            df["Open"].values.astype(float),
            df["High"].values.astype(float),
            df["Low"].values.astype(float),
            df["Close"].values.astype(float),
            df["Volume"].values.astype(float),
            all_closes,
            bar_time,
        )
    except Exception as e:
        logger.warning("_fetch_intraday %s %s failed: %s", symbol, interval, e)
        return None


def _is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 15) <= t <= dtime(15, 30)


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

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        ipc.clear_all_flags()
        now_ist = datetime.now(IST)

        # Musashi — every 15 min
        self.scheduler.add_job(
            self._musashi_cycle, "interval", minutes=15,
            id="musashi", next_run_time=now_ist,
        )
        # Raijin — every 5 min
        self.scheduler.add_job(
            self._raijin_cycle, "interval", minutes=5,
            id="raijin", next_run_time=now_ist,
        )
        # ATR Intraday — every 5 min
        self.scheduler.add_job(
            self._atr_cycle, "interval", minutes=5,
            id="atr_intraday", next_run_time=now_ist,
        )
        # EOD square-off at 15:15, journal save at 15:20
        self.scheduler.add_job(self._eod_squareoff, "cron", hour=15, minute=15, id="eod")
        self.scheduler.add_job(self._save_journal,  "cron", hour=15, minute=20, id="journal")

        self.scheduler.start()
        logger.info("BotRunner started — Musashi(15m) + Raijin(5m) + ATR(5m)")

    def stop(self):
        self.scheduler.shutdown(wait=False)

    @property
    def paused(self) -> bool:
        return ipc.flag_exists(ipc.FLAG_PAUSE)

    # ── Musashi ───────────────────────────────────────────────────────────────

    async def _musashi_cycle(self):
        self.last_heartbeat = datetime.now(IST).isoformat()
        if self.paused or not _is_market_hours():
            return
        try:
            from strategies.nifty_intraday import (
                score_signal, in_entry_window,
                MAX_TRADES_DAY, SCORE_THRESHOLD, EOD_EXIT,
            )
            now_t = datetime.now(IST).time()

            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _fetch_intraday("NIFTY", "15m")
            )
            if result is None:
                logger.warning("[Musashi] _fetch_intraday returned None — yfinance may have failed")
                return
            opens, highs, lows, closes, volumes, all_closes, bar_time = result

            # ── manage open position ──────────────────────────────────────────
            pos = self.state.get_position("musashi")
            if pos:
                price = float(closes[-1])
                hit_sl = (price <= pos["sl"]) if pos["side"] == "BUY" else (price >= pos["sl"])
                hit_tp = (price >= pos["tp"]) if pos["side"] == "BUY" else (price <= pos["tp"])
                eod    = now_t >= EOD_EXIT
                if hit_sl or hit_tp or eod:
                    reason = "SL" if hit_sl else ("TP" if hit_tp else "EOD")
                    pnl = (price - pos["entry"]) * pos["qty"]
                    if pos["side"] == "SELL":
                        pnl = -pnl
                    pnl = round(pnl, 2)
                    self._close_trade(pos, price, pnl, reason)
                    self.state.set_position("musashi", None)
                    asyncio.ensure_future(self._generate_exit_remark(pos, price, pnl, reason))
                return

            # ── look for entry ────────────────────────────────────────────────
            if not in_entry_window(now_t):
                logger.info("[Musashi] Outside entry window (now_t=%s, bar_time=%s)", now_t, bar_time)
                return
            if not self.state.can_trade("musashi", MAX_TRADES_DAY):
                return

            sig = score_signal(opens, highs, lows, closes, volumes, all_closes)
            self.last_scores["Musashi"] = {
                "buy": sig.get("buy_score", 0), "sell": sig.get("sell_score", 0),
                "action": sig.get("action"), "threshold": SCORE_THRESHOLD,
                "in_window": True, "bar_time": str(bar_time), "now_t": str(now_t),
            }
            logger.info("[Musashi] score buy=%.1f sell=%.1f action=%s threshold=%.1f bar_time=%s now_t=%s",
                        sig.get("buy_score", 0), sig.get("sell_score", 0),
                        sig.get("action"), SCORE_THRESHOLD, bar_time, now_t)
            if sig["action"] == "HOLD":
                return

            entry = float(closes[-1])
            atr_v = sig["atr"] or 1.0
            if sig["action"] == "BUY":
                sl, tp = entry - 1.25 * atr_v, entry + 3.125 * atr_v
            else:
                sl, tp = entry + 1.25 * atr_v, entry - 3.125 * atr_v

            pos = self._open_trade("Musashi", sig["action"], entry, sl, tp, sig["score"])
            self.state.set_position("musashi", pos)
            self.state.record_trade("musashi")
            asyncio.ensure_future(self._generate_entry_remark(pos, sig))

        except Exception as e:
            logger.error("Musashi cycle: %s", e, exc_info=True)

    # ── Raijin ────────────────────────────────────────────────────────────────

    async def _raijin_cycle(self):
        self.last_heartbeat = datetime.now(IST).isoformat()
        if self.paused or not _is_market_hours():
            return
        try:
            from strategies.nifty_scalp import (
                score_signal, in_entry_window,
                MAX_TRADES_DAY, SCORE_THRESHOLD, EOD_EXIT,
            )
            now_t = datetime.now(IST).time()

            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _fetch_intraday("NIFTY", "5m")
            )
            if result is None:
                logger.warning("[Raijin] _fetch_intraday returned None — yfinance may have failed")
                return
            opens, highs, lows, closes, volumes, all_closes, bar_time = result

            # ── manage open position ──────────────────────────────────────────
            pos = self.state.get_position("raijin")
            if pos:
                price = float(closes[-1])
                hit_sl = (price <= pos["sl"]) if pos["side"] == "BUY" else (price >= pos["sl"])
                hit_tp = (price >= pos["tp"]) if pos["side"] == "BUY" else (price <= pos["tp"])
                eod    = now_t >= EOD_EXIT
                if hit_sl or hit_tp or eod:
                    reason = "SL" if hit_sl else ("TP" if hit_tp else "EOD")
                    pnl = (price - pos["entry"]) * pos["qty"]
                    if pos["side"] == "SELL":
                        pnl = -pnl
                    pnl = round(pnl, 2)
                    self._close_trade(pos, price, pnl, reason)
                    self.state.set_position("raijin", None)
                    asyncio.ensure_future(self._generate_exit_remark(pos, price, pnl, reason))
                return

            # ── look for entry ────────────────────────────────────────────────
            if not in_entry_window(now_t):
                logger.info("[Raijin] Outside entry window (now_t=%s, bar_time=%s)", now_t, bar_time)
                return
            if not self.state.can_trade("raijin", MAX_TRADES_DAY):
                return

            sig = score_signal(opens, highs, lows, closes, volumes, all_closes)
            self.last_scores["Raijin"] = {
                "buy": sig.get("buy_score", 0), "sell": sig.get("sell_score", 0),
                "action": sig.get("action"), "threshold": SCORE_THRESHOLD,
                "in_window": True, "bar_time": str(bar_time), "now_t": str(now_t),
            }
            logger.info("[Raijin] score buy=%.1f sell=%.1f action=%s threshold=%.1f bar_time=%s now_t=%s",
                        sig.get("buy_score", 0), sig.get("sell_score", 0),
                        sig.get("action"), SCORE_THRESHOLD, bar_time, now_t)
            if sig["action"] == "HOLD":
                return

            entry = float(closes[-1])
            atr_v = sig["atr"] or 1.0
            if sig["action"] == "BUY":
                sl, tp = entry - 0.6 * atr_v, entry + 1.2 * atr_v
            else:
                sl, tp = entry + 0.6 * atr_v, entry - 1.2 * atr_v

            pos = self._open_trade("Raijin", sig["action"], entry, sl, tp, sig["score"])
            self.state.set_position("raijin", pos)
            self.state.record_trade("raijin")
            asyncio.ensure_future(self._generate_entry_remark(pos, sig))

        except Exception as e:
            logger.error("Raijin cycle: %s", e, exc_info=True)

    # ── ATR Intraday (TrendStrategy / Claude) ─────────────────────────────────

    async def _atr_cycle(self):
        self.last_heartbeat = datetime.now(IST).isoformat()
        if self.paused or not _is_market_hours():
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
        for strat in ("musashi", "raijin"):
            pos = self.state.get_position(strat)
            if pos:
                self._close_trade(pos, pos["entry"], 0.0, "EOD")
                self.state.set_position(strat, None)
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

    def _open_trade(self, strategy: str, side: str, entry: float,
                    sl: float, tp: float, score: float) -> dict:
        ts = datetime.now().isoformat()
        order_id = f"{strategy.upper()}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        option_type = "CE" if side == "BUY" else "PE"
        strike = round(entry / 50) * 50   # nearest ATM 50-point strike
        order = {
            "order_id":    order_id,
            "symbol":      "NIFTY",
            "side":        side,
            "quantity":    75,
            "price":       entry,
            "pnl":         0,
            "status":      "OPEN",
            "timestamp":   ts,
            "strategy":    strategy,
            "option_type": option_type,
            "strike":      strike,
            "lot_size":    75,
            "sl_price":    round(sl, 2),
            "tp_price":    round(tp, 2),
            "score":       score,
        }
        decision = {
            "reasoning": f"{strategy} score={score:.1f} | {option_type} strike={strike}",
            "confidence": min(score / 10.0, 1.0),
            "risk_level": "MEDIUM",
        }
        self.memory.log_trade(order, decision)
        logger.info("[%s] ENTRY %s NIFTY %s%d @ ₹%.2f | SL=%.2f TP=%.2f score=%.1f",
                    strategy, side, option_type, strike, entry, sl, tp, score)
        return {
            "strategy": strategy, "order_id": order_id, "symbol": "NIFTY",
            "side": side, "entry": entry, "sl": round(sl, 2), "tp": round(tp, 2),
            "qty": 75, "score": score, "timestamp": ts,
            "option_type": option_type, "strike": strike,
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
