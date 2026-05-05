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
from core.utils import now_ist
from zoneinfo import ZoneInfo
from typing import Optional


IST = ZoneInfo("Asia/Kolkata")

import config
from core.memory import TradeMemory
from core import ipc

logger = logging.getLogger(__name__)

def _is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 15) <= t <= dtime(15, 30)


def _calc_vix_lots(vix: float) -> int:
    """Return recommended lots based on India VIX at market open.

    Per Mu Hat research: VIX spikes (>30) are historically the best entry
    windows — returns +32.5% avg over next 3M. So we don't block, we scale down.
    """
    if vix is None or vix < 20:
        return 3   # calm / normal vol — full size
    elif vix < 25:
        return 2   # elevated
    elif vix < 40:
        return 1   # high / spike — trade light, stay in
    else:
        return 1   # extreme panic — minimum, don't block


def _is_event_blocked() -> bool:
    """Return True if today is blocked (config or runtime) and not explicitly unblocked."""
    today_str = now_ist().date().isoformat()
    if today_str in ipc.read_event_unblocks():
        logger.info("Trading UNBLOCKED today — %s (manual override). Proceeding.", today_str)
        return False
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
        self.last_heartbeat: Optional[str] = None   # ISO string, IST
        self.last_scores: dict = {}                 # strategy → last signal scores
        self.last_vix: Optional[float] = None       # India VIX, updated each cycle
        self.last_vix_regime: str = "UNKNOWN"       # VIX regime string
        self.last_day_bias: dict = ipc.read_day_bias()  # cached; updated by set_bias API
        self.last_option_chain: dict = {}
        self.last_angel_trades: list = []               # Angel One tradeBook, synced every 5m
        self.last_zones: list = []                      # today's watch zones (pre-market briefing)
        from core.paper_seller import get_paper_seller
        self._paper_seller = get_paper_seller()
        self._zone_entry_fired_today: bool = False      # zone reversal: one entry per day max

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        ipc.clear_all_flags()

        # Warm up Angel One login so first cycle doesn't pay the auth cost
        try:
            from data.angel_fetcher import AngelFetcher
            AngelFetcher.get()._ensure_logged_in()
        except Exception as e:
            logger.warning("Angel One warm-up failed (will retry per cycle): %s", e)

        # ── Candle-aligned cron schedules ─────────────────────────────────────
        # NSE 5m candles close at 9:20, 9:25, 9:30 ... (every :00/:05/:10/:15/:20/:25/:30/:35/:40/:45/:50/:55)
        # NSE 15m candles close at 9:30, 9:45, 10:00 ... (every :00/:15/:30/:45)
        # Each strategy fires a few seconds after its candle closes so it reads the
        # freshest closed bar. Staggered seconds prevent simultaneous Angel One API calls.

        # ATR Intraday — 5s after every 5m candle close (9:20:05, 9:25:05, ...)
        self.scheduler.add_job(
            self._atr_cycle, "cron", minute="*/5", second=5,
            id="atr_intraday",
        )
        # Fast entry check — 2 min into each 5m candle (9:22:30, 9:27:30 ...)
        # Only fires if the PREVIOUS bar's score was near-threshold (6-7).
        # This catches moves that scored 6 last bar → score 8+ now → enter 3 min early.
        # Avoids re-entry loops: after SL the score drops back to HOLD/negative
        # so last_score is no longer near threshold → fast check stays quiet.
        self.scheduler.add_job(
            self._atr_fast_check, "cron", minute="2-59/5", second=30,
            id="atr_fast_check",
        )
        # Paper monitor — 25s after 5m close (after strategies have placed orders)
        self.scheduler.add_job(
            self._paper_monitor, "cron", minute="*/5", second=25,
            id="paper_monitor",
        )
        # VIX + option chain refresh — every 15m
        self.scheduler.add_job(
            self._vix_refresh, "cron", minute="*/15", second=20,
            id="vix_refresh",
        )
        # Force trade fast-poll — every 30s, no-op unless flag file exists
        self.scheduler.add_job(
            self._force_trade_poll, "interval", seconds=30,
            id="force_trade_poll",
        )
        # EOD square-off at configured intraday cutoff, journal save after exits settle.
        exit_hour, exit_minute = map(int, config.INTRADAY_EXIT_BY.split(":"))
        self.scheduler.add_job(self._eod_squareoff, "cron", hour=exit_hour, minute=exit_minute, id="eod")
        self.scheduler.add_job(self._save_journal,  "cron", hour=15, minute=20, id="journal")
        self.scheduler.add_job(self._weekly_review, "cron", day_of_week="sat", hour=8, minute=0, id="weekly_review")
        # Reset day bias to NEUTRAL at 20:00 IST each evening
        self.scheduler.add_job(self._reset_day_bias, "cron", hour=20, minute=0, id="bias_reset")
        # Angel One trade book sync — every 5 minutes during market hours
        self.scheduler.add_job(self._sync_angel_trades, "cron", minute="*/5", second=45, id="angel_sync")
        # VIX auto-lots — fetch VIX at 9:30 IST and set min_lots for the day
        self.scheduler.add_job(self._vix_auto_lots_set, "cron", hour=9, minute=30, id="vix_auto_lots")
        # Pre-market zone briefing — 9:00 AM, before session starts
        self.scheduler.add_job(self._zone_briefing, "cron", hour=9, minute=0, id="zone_briefing")
        # Position guardian — every 60s during market hours: checks SL/TP on open positions
        # independent of strategy cycle. Catches fast moves between 5m candle ticks.
        self.scheduler.add_job(self._position_guardian, "interval", seconds=60, id="position_guardian")

        self.scheduler.start()
        logger.info(
            "BotRunner started — ATR(5m+5s) "
            "PaperMon(5m+25s) VIX(15m+20s) ForcePoll(30s) PosGuard(60s)"
        )

    def stop(self):
        self.scheduler.shutdown(wait=False)

    @property
    def paused(self) -> bool:
        return ipc.flag_exists(ipc.FLAG_PAUSE)

    # ── ATR Intraday (TrendStrategy / Claude) ─────────────────────────────────

    async def _atr_cycle(self):
        self.last_heartbeat = datetime.now(IST).isoformat()
        if self.paused or not _is_market_hours() or _is_event_blocked():
            return
        try:
            if self._atr_strategy is None:
                from strategies.trend_strategy import TrendStrategy
                self._atr_strategy = TrendStrategy(strategy_name="ATR Intraday", score_mode="atr_only")

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._atr_strategy.run_watchlist)

            # Update last_scores for the debug/signal-radar endpoint
            sc = self._atr_strategy.last_score
            if sc:
                score      = sc.get("score", 0)
                threshold  = sc.get("threshold", 6)
                direction  = sc.get("action", "HOLD")
                will_trade = abs(score) >= threshold
                entry = {
                    "score": score, "direction": direction, "action": direction,
                    "threshold": threshold, "will_trade": will_trade,
                    "note": "ATR technical analysis only (sections 1–11)",
                }
                self.last_scores["ATR Intraday"] = entry
                self._paper_seller.on_signal("ATR Intraday", entry)

                # ── Persist every evaluation to signal_log ────────────────────
                try:
                    from core.memory import log_signal
                    from data.angel_fetcher import AngelFetcher
                    af         = AngelFetcher.get()
                    nifty_spot = af.get_index_ltp("NIFTY") or 0
                    opt_type   = "CE" if direction == "BUY" else ("PE" if direction == "SELL" else "")
                    strike     = int(round(nifty_spot / 50) * 50) if nifty_spot else 0
                    opt_prem   = 0.0
                    if opt_type and strike and nifty_spot:
                        from data.angel_fetcher import AngelFetcher as _AF
                        expiry = af.nearest_weekly_expiry()
                        _, opt_prem = af.get_option_ltp("NIFTY", strike, opt_type, expiry)
                        opt_prem = opt_prem or 0.0
                    did_trade  = bool(self._atr_strategy.last_score.get("did_trade"))
                    reason     = sc.get("skip_reason", "")
                    if not will_trade and not reason:
                        reason = f"score {score:+.0f} below threshold {threshold}"
                    log_signal(
                        strategy="ATR Intraday", score=score, threshold=threshold,
                        direction=direction, will_trade=will_trade, did_trade=did_trade,
                        reason_skipped=reason, nifty_spot=nifty_spot,
                        option_type=opt_type, strike=strike, option_premium=float(opt_prem),
                        signals_fired="|".join(sc.get("signals", [])[:5]),
                    )
                except Exception as _log_err:
                    logger.debug("signal_log write failed: %s", _log_err)

                # ── Zone reversal early entry ─────────────────────────────────
                try:
                    self._check_zone_reversal_entry()
                except Exception as _ze_err:
                    logger.debug("zone_reversal_entry check failed: %s", _ze_err)

        except Exception as e:
            logger.error("ATR Intraday cycle: %s", e, exc_info=True)

    def _check_zone_reversal_entry(self):
        """Enter early when price taps a supply/demand zone and the last 5m candle
        confirms rejection — independent of the ATR score build-up."""
        if self._zone_entry_fired_today:
            return
        if self.memory.get_open_trade_for_symbol("NIFTY"):
            return
        if self._atr_strategy is None:
            return

        try:
            from core.ipc import read_watch_zones
            from core.zone_briefing import get_active_zone
            from data.angel_fetcher import AngelFetcher

            zones = read_watch_zones()
            if not zones:
                return

            af    = AngelFetcher.get()
            spot  = af.get_index_ltp("NIFTY")
            if not spot:
                return

            zone = get_active_zone(spot, zones, proximity_pts=40)
            if not zone:
                return

            # Fetch last two 5m bars to confirm rejection candle
            from data.market import _get_intraday_df
            df = _get_intraday_df("NIFTY", "5m")
            if df is None or len(df) < 2:
                return

            last      = df.iloc[-1]
            prev      = df.iloc[-2]
            last_open  = float(last["Open"])
            last_close = float(last["Close"])
            prev_close = float(prev["Close"])

            zone_dir = zone["direction"]  # "CE" = support (buy CE), "PE" = resistance (buy PE)

            if zone_dir == "PE":
                # Resistance zone — look for bearish rejection:
                # last candle closed below open AND below previous close
                bearish = last_close < last_open and last_close < prev_close
                if not bearish:
                    return
                direction = "SELL"
                logger.info(
                    "[ZONE REVERSAL] Resistance zone at ₹%.0f | NIFTY=%.0f | bearish rejection → PE entry",
                    zone["mid"], spot,
                )
            elif zone_dir == "CE":
                # Support zone — look for bullish rejection (hammer / green candle)
                bullish = last_close > last_open and last_close > prev_close
                if not bullish:
                    return
                direction = "BUY"
                logger.info(
                    "[ZONE REVERSAL] Support zone at ₹%.0f | NIFTY=%.0f | bullish bounce → CE entry",
                    zone["mid"], spot,
                )
            else:
                return

            self._zone_entry_fired_today = True
            self._atr_strategy._force_early_entry(direction)

        except AttributeError:
            pass  # _get_intraday_df_raw not available — skip silently
        except Exception as e:
            logger.debug("Zone reversal entry: %s", e)

    async def _atr_fast_check(self):
        """Runs 2 min into each 5m candle to catch moves 3 min earlier than bar close.

        SAFE re-entry guard: only fires when the PREVIOUS bar's score was 6-7
        (near threshold but below). After SL the signal reverses → last_score drops
        to HOLD/negative → this check stays quiet → no re-entry loop.

        Logic:
          1. Last bar score must be 6 or 7 in the signal direction (near miss)
          2. No open position
          3. Not within 15 min of last trade entry (cooldown)
        """
        if self.paused or not _is_market_hours():
            return
        if self._atr_strategy is None:
            return
        try:
            strat = self._atr_strategy

            # Guard 1: previous bar score must be near-threshold (6 or 7)
            # If it was 0 or negative (HOLD / reversed), the move is over — skip.
            last = strat.last_score or {}
            last_score_val = abs(last.get("score", 0))
            last_threshold = last.get("threshold", 8)
            if not (last_threshold - 2 <= last_score_val < last_threshold):
                return   # not near threshold → nothing building → skip

            # Guard 2: no open position
            positions = strat.broker.get_positions()
            if any(v.get("quantity", 0) > 0 for v in positions.values()):
                return

            # Guard 3: cooldown — no trade in last 15 minutes
            today_trades = strat.memory.get_today_trades()
            if today_trades:
                from core.utils import now_ist as _now_ist
                from datetime import timedelta
                last_ts_str = max(
                    (t.get("timestamp", "") for t in today_trades if t.get("side") == "BUY"),
                    default=""
                )
                if last_ts_str:
                    try:
                        from datetime import datetime as _dt
                        last_ts = _dt.fromisoformat(last_ts_str)
                        if last_ts.tzinfo is None:
                            last_ts = last_ts.replace(tzinfo=_now_ist().tzinfo)
                        if (_now_ist() - last_ts).total_seconds() < 900:  # 15 min
                            return
                    except Exception:
                        pass

            logger.info(
                "FastCheck: last score %+d near threshold %d — running early entry check",
                last.get("score", 0), last_threshold
            )
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, strat.run_watchlist)
        except Exception as e:
            logger.debug("_atr_fast_check: %s", e)

    async def _position_guardian(self):
        """Runs every 60s — checks SL/TP on open positions between 5m candle ticks.
        Only activates when the exchange SL-M order was not placed (fallback in-process SL)."""
        if self.paused or not _is_market_hours():
            return
        if self._atr_strategy is None:
            return
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._atr_strategy.run_watchlist)
        except Exception as e:
            logger.debug("position_guardian: %s", e)


    # ── VIX regime refresh (every 15 min) ────────────────────────────────────

    async def _vix_refresh(self):
        if not _is_market_hours():
            return
        try:
            from strategies.vix_filter import get_live_regime
            loop = asyncio.get_event_loop()
            regime, vix = await loop.run_in_executor(None, get_live_regime)
            self.last_vix = vix
            self.last_vix_regime = regime
            self.last_scores["VIX-Regime"] = {
                "score": 0, "direction": "HOLD", "action": "HOLD",
                "threshold": 0, "will_trade": False,
                "regime": regime,
                "vix": vix,
                "note": f"India VIX={vix:.2f} → {regime}" if vix else f"VIX unavailable → {regime}",
            }
            logger.info("VIX refresh: regime=%s vix=%.2f", regime, vix or 0)
        except Exception as e:
            logger.error("VIX refresh: %s", e)
        try:
            from data.option_chain import OptionChainFetcher
            oc = OptionChainFetcher.get().fetch("NIFTY")
            if oc and not oc.get("error"):
                self.last_option_chain = oc
        except Exception as e:
            logger.warning("Option chain refresh: %s", e)

    def _has_margin(self, min_required: float = 15_000.0) -> bool:
        """Check Angel One available margin before entering a trade."""
        try:
            from data.angel_fetcher import AngelFetcher
            af = AngelFetcher.get()
            if not af._ensure_logged_in():
                return True  # fail-open
            rms = af._api.rmsLimit()
            if rms and rms.get("status") and rms.get("data"):
                d = rms["data"]
                available = float(d.get("availablecash", 0) or d.get("net", 0) or 0)
                if available < min_required:
                    logger.warning(
                        "Insufficient margin: ₹%.0f available, ₹%.0f required — skipping trade",
                        available, min_required,
                    )
                    return False
            return True
        except Exception as e:
            logger.warning("Margin check failed: %s — allowing trade", e)
            return True  # fail-open: let Angel One reject if truly insufficient


    # ── Force trade fast poll (every 30s, no-op unless flag exists) ──────────

    async def _force_trade_poll(self):
        if self.paused or not _is_market_hours() or _is_event_blocked():
            return
        if not ipc.flag_exists(ipc.FLAG_FORCE_TRADE):
            return
        logger.info("force_trade_poll: flag detected — running ATR cycle immediately")
        await self._atr_cycle()

    # ── Paper seller monitor (every 5 min) ───────────────────────────────────

    async def _paper_monitor(self):
        if not _is_market_hours():
            return
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._paper_seller.mark_to_market)
        except Exception as e:
            logger.error("Paper monitor: %s", e)

    # ── EOD square-off ────────────────────────────────────────────────────────

    async def _eod_squareoff(self):
        logger.info("EOD square-off triggered")
        loop = asyncio.get_event_loop()
        if self._atr_strategy:
            await loop.run_in_executor(None, self._atr_strategy.square_off_all)
        # Close all paper comparison positions at EOD
        await loop.run_in_executor(None, self._paper_seller.eod_close)

    async def _save_journal(self):
        """Save daily journal JSON at 15:20 (after EOD square-off)."""
        try:
            from core.journal import save_daily_journal
            loop = asyncio.get_event_loop()
            path = await loop.run_in_executor(None, save_daily_journal)
            logger.info("Journal saved: %s", path)
        except Exception as e:
            logger.error("Journal save failed: %s", e, exc_info=True)

    async def _weekly_review(self):
        """Generate Claude-powered weekly review every Saturday at 08:00 IST."""
        try:
            from core.journal import save_weekly_review
            loop = asyncio.get_event_loop()
            path = await loop.run_in_executor(None, save_weekly_review)
            if path:
                logger.info("Weekly review saved: %s", path)
        except Exception as e:
            logger.error("Weekly review failed: %s", e, exc_info=True)

    async def _sync_angel_trades(self):
        """Pull Angel One tradeBook and reconcile against SQLite open trades.

        For each BUY: if our DB has no matching order_id, import it so manually
        placed or Windows-bot trades are visible in the P&L dashboard.
        For each SELL: if our DB shows that symbol open, compute real PnL and close.
        """
        if config.IS_PAPER:
            return
        try:
            from data.angel_fetcher import AngelFetcher
            loop = asyncio.get_event_loop()
            angel_trades = await loop.run_in_executor(None, AngelFetcher.get().get_trade_book)
            self.last_angel_trades = angel_trades

            # ── Import missing BUY trades ─────────────────────────────────────
            buys = [t for t in angel_trades if t["side"] == "BUY"]
            for buy in buys:
                symbol   = buy["symbol"]
                order_id = buy["order_id"]
                existing = self.memory.get_open_trade_for_symbol(symbol)
                if existing and existing.get("order_id") == order_id:
                    continue  # already in DB
                order = {
                    "order_id":  order_id,
                    "symbol":    symbol,
                    "side":      "BUY",
                    "quantity":  buy["quantity"],
                    "price":     buy["price"],
                    "pnl":       0,
                    "status":    "OPEN",
                    "timestamp": now_ist().isoformat(),
                    "strategy":  "ANGEL_SYNC",
                }
                decision = {"reasoning": f"imported from Angel One trade book: {symbol}", "confidence": 1.0, "risk_level": "UNKNOWN"}
                self.memory.log_trade(order, decision)
                logger.info("angel_sync: imported BUY %s qty=%d @ ₹%.2f (order %s)",
                            symbol, buy["quantity"], buy["price"], order_id)

            # ── Close matched SELL trades ─────────────────────────────────────
            sells = [t for t in angel_trades if t["side"] == "SELL"]
            for sell in sells:
                symbol = sell["symbol"]
                open_trade = self.memory.get_open_trade_for_symbol(symbol)
                if not open_trade:
                    continue
                buy_price  = float(open_trade.get("price") or 0)
                sell_price = sell["price"]
                qty        = int(open_trade.get("quantity") or sell["quantity"])
                pnl        = round((sell_price - buy_price) * qty, 2)
                order_id   = open_trade.get("order_id", "")
                self.memory.close_trade(order_id, pnl)
                logger.info(
                    "angel_sync: closed %s (order %s) pnl=%.2f buy=%.2f sell=%.2f",
                    symbol, order_id, pnl, buy_price, sell_price,
                )
        except Exception as e:
            logger.error("_sync_angel_trades: %s", e, exc_info=True)

    async def _reset_day_bias(self):
        """Reset day bias and early-entry state at 20:00 IST each evening."""
        try:
            ipc.write_day_bias("NEUTRAL", "")
            self.last_day_bias = ipc.read_day_bias()
            self._zone_entry_fired_today = False
            logger.info("Day bias reset to NEUTRAL for tomorrow.")
        except Exception as e:
            logger.error("Bias reset failed: %s", e)

    async def _vix_auto_lots_set(self):
        """Fetch India VIX at 9:30 IST and auto-set min_lots for the day.

        Uses _calc_vix_lots() to map VIX level to a recommended position size.
        Writes to settings.json so the header dropdown reflects it immediately.
        User can override the dropdown at any time — this just sets the default.
        """
        try:
            from data.angel_fetcher import AngelFetcher
            loop = asyncio.get_event_loop()
            vix = await loop.run_in_executor(None, AngelFetcher.get().fetch_vix)
            if vix is None:
                logger.warning("VIX auto-lots: could not fetch India VIX at open — skipping")
                return
            recommended = _calc_vix_lots(vix)
            ipc.write_settings({"min_lots": recommended, "vix_at_open": round(vix, 2), "vix_auto_lots": recommended})
            logger.info(
                "VIX auto-lots: India VIX=%.2f → %d lot%s set for today",
                vix, recommended, "s" if recommended != 1 else "",
            )
        except Exception as e:
            logger.error("VIX auto-lots failed: %s", e)

    async def _zone_briefing(self):
        """Pre-market zone briefing at 9:00 AM IST.
        Computes today's watch zones from weekly + daily NIFTY bars.
        Zones are used by the strategy to enter at key price levels
        instead of chasing indicator signals mid-move.
        """
        try:
            from data.angel_fetcher import AngelFetcher
            from core.zone_briefing import compute_daily_zones, today_zones_summary
            loop = asyncio.get_event_loop()
            df = await loop.run_in_executor(
                None,
                lambda: AngelFetcher.get().fetch_historical_df("NIFTY", "5m", days=5),
            )
            if df is None or len(df) < 20:
                logger.warning("Zone briefing: insufficient data — zones not computed")
                return
            zones = compute_daily_zones(df)
            self.last_zones = zones
            ipc.write_watch_zones(zones)   # persist so strategy can read without runner ref
            logger.info("Zone briefing complete:\n%s", today_zones_summary(zones))
        except Exception as e:
            logger.error("Zone briefing failed: %s", e)

    # ── trade helpers ─────────────────────────────────────────────────────────

    def _open_trade(self, strategy: str, side: str, entry_spot: float,
                    sl: float, tp: float, score: float,
                    option_type: str = "CE", strike: int = 0,
                    expiry=None, opt_sym: str = "", entry_prem: float = 0.0) -> dict:
        ts       = now_ist().isoformat()
        order_id = f"{strategy.upper()}-{now_ist().strftime('%Y%m%d%H%M%S')}"
        min_lots = ipc.read_settings().get("min_lots", config.MIN_LOTS)
        lot_qty  = config.LOT_SIZES.get("NIFTY", 65) * min_lots
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
