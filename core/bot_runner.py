"""
BotRunner — runs all 3 strategies inside the FastAPI process.

Uses APScheduler (AsyncIOScheduler) so it shares FastAPI's event loop.
The WebSocket loop in server.py already broadcasts _build_snapshot() every 5 s,
so bot_runner only needs to write trades to TradeMemory — no separate broadcast needed.

Strategies:
  Musashi     — NIFTY 5-min EMA/VWAP/HA trend                (max 2 trades/day)
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
        self.last_pcr: Optional[float] = None       # last fetched NIFTY PCR

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

        # Musashi — every 15 min
        self.scheduler.add_job(
            self._musashi_cycle, "interval", minutes=15,
            id="musashi", next_run_time=now_ist,
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
        logger.info("BotRunner started — Musashi(15m) + ATR(5m)")

    def stop(self):
        self.scheduler.shutdown(wait=False)

    @property
    def paused(self) -> bool:
        return ipc.flag_exists(ipc.FLAG_PAUSE)

    # ── Musashi ───────────────────────────────────────────────────────────────

    async def _musashi_cycle(self):
        self.last_heartbeat = datetime.now(IST).isoformat()
        if self.paused or not _is_market_hours() or _is_event_blocked():
            return
        try:
            from strategies.nifty_intraday import (
                score_signal, in_entry_window,
                MAX_TRADES_DAY, SCORE_THRESHOLD, EOD_EXIT,
            )
            now_t = datetime.now(IST).time()

            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _fetch_intraday("NIFTY", "5m")
            )
            if result is None:
                logger.warning("[Musashi] _fetch_intraday returned None — yfinance may have failed")
                return
            opens, highs, lows, closes, volumes, all_closes, bar_time = result

            # ── fetch India VIX (non-blocking, best-effort) ───────────────────
            try:
                from data.zerodha_fetcher import ZerodhaFetcher as _ZF_VIX
                vix_val = await asyncio.get_event_loop().run_in_executor(
                    None, _ZF_VIX.get().fetch_vix
                )
                if vix_val is not None:
                    self.last_vix = vix_val
            except Exception:
                vix_val = self.last_vix  # reuse cached if fetch fails

            # ── fetch NIFTY PCR (non-blocking, best-effort, 5-min cache in oi_data) ──
            try:
                from data.oi_data import get_pcr as _get_pcr
                pcr_data = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _get_pcr("NIFTY")
                )
                pcr_val = pcr_data.get("pcr")
                if pcr_val is not None:
                    self.last_pcr = pcr_val
            except Exception:
                pcr_val = self.last_pcr  # reuse cached if fetch fails

            # ── manage open position ──────────────────────────────────────────
            pos = self.state.get_position("musashi")
            if pos:
                from data.zerodha_fetcher import ZerodhaFetcher as _ZF
                expiry = date.fromisoformat(pos["expiry"])
                _, opt_price = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _ZF.get().get_option_ltp("NIFTY", pos["strike"], pos["option_type"], expiry)
                )
                if opt_price is None or opt_price <= 0:
                    # Paper fallback: delta-approximate from NIFTY spot
                    nifty_move = float(closes[-1]) - pos["nifty_entry"]
                    direction  = 1 if pos["option_type"] == "CE" else -1
                    opt_price  = max(pos["entry"] + nifty_move * 0.5 * direction, 0.05)
                # SL/TP are stored in option premium terms
                hit_sl = opt_price <= pos["sl"]
                hit_tp = opt_price >= pos["tp"]
                eod    = now_t >= EOD_EXIT
                if hit_sl or hit_tp or eod:
                    reason = "SL" if hit_sl else ("TP" if hit_tp else "EOD")
                    pnl = round((opt_price - pos["entry"]) * pos["qty"], 2)
                    self._close_trade(pos, opt_price, pnl, reason)
                    self.state.set_position("musashi", None)
                    asyncio.ensure_future(self._generate_exit_remark(pos, opt_price, pnl, reason))
                return

            # ── look for entry ────────────────────────────────────────────────
            if not in_entry_window(now_t):
                logger.info("[Musashi] Outside entry window (now_t=%s, bar_time=%s)", now_t, bar_time)
                return
            if not self.state.can_trade("musashi", MAX_TRADES_DAY):
                return

            sig = score_signal(opens, highs, lows, closes, volumes, all_closes, pcr=pcr_val, vix=vix_val)
            self.last_scores["Musashi"] = {
                "buy": sig.get("buy_score", 0), "sell": sig.get("sell_score", 0),
                "action": sig.get("action"), "threshold": SCORE_THRESHOLD,
                "in_window": True, "bar_time": str(bar_time), "now_t": str(now_t),
                "vix": vix_val, "pcr": pcr_val,
            }
            logger.info("[Musashi] score buy=%.1f sell=%.1f action=%s threshold=%.1f bar_time=%s now_t=%s vix=%s pcr=%s",
                        sig.get("buy_score", 0), sig.get("sell_score", 0),
                        sig.get("action"), SCORE_THRESHOLD, bar_time, now_t,
                        f"{vix_val:.1f}" if vix_val else "n/a",
                        f"{pcr_val:.2f}" if pcr_val else "n/a")
            if sig["action"] == "HOLD":
                return

            entry_spot = float(closes[-1])
            atr_v      = sig["atr"] or 1.0
            option_type = "CE" if sig["action"] == "BUY" else "PE"
            strike      = round(entry_spot / 50) * 50  # nearest ATM 50-pt strike

            from data.zerodha_fetcher import ZerodhaFetcher as _ZF
            expiry   = _ZF.nearest_weekly_expiry()
            opt_sym, entry_prem = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _ZF.get().get_option_ltp("NIFTY", strike, option_type, expiry)
            )
            if entry_prem is None or entry_prem < 1:
                # Paper fallback: rough synthetic premium (ATR × 1.5)
                opt_sym    = f"NIFTY_SYN_{expiry}_{strike}{option_type}"
                entry_prem = max(atr_v * 1.5, 50.0)
                logger.warning("[Musashi] Option LTP unavailable — synthetic premium ₹%.2f", entry_prem)

            # SL/TP in option premium terms using delta = 0.5 (ATM)
            delta = 0.5
            sl = max(entry_prem - 1.25 * atr_v * delta, entry_prem * 0.10)
            tp = entry_prem + 3.125 * atr_v * delta

            pos = self._open_trade("Musashi", sig["action"], entry_spot, sl, tp, sig["score"],
                                   option_type=option_type, strike=strike, expiry=expiry,
                                   opt_sym=opt_sym, entry_prem=entry_prem)
            self.state.set_position("musashi", pos)
            self.state.record_trade("musashi")
            asyncio.ensure_future(self._generate_entry_remark(pos, sig))

        except Exception as e:
            logger.error("Musashi cycle: %s", e, exc_info=True)

    # ── ATR Intraday (TrendStrategy / Claude) ─────────────────────────────────

    async def _atr_cycle(self):
        self.last_heartbeat = datetime.now(IST).isoformat()
        if self.paused or not _is_market_hours() or _is_event_blocked():
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
        pos = self.state.get_position("musashi")
        if pos:
            self._close_trade(pos, pos["entry"], 0.0, "EOD")
            self.state.set_position("musashi", None)
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
