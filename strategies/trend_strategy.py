"""
Trend Strategy — AishDoc intraday approach for NIFTY.

Roadmap:
  Phase A : ₹20,000 budget | intraday only | goal ₹1.5L
  Phase B : ₹1.5L budget   | intraday + swing | goal ₹15L
  Phase C : ₹15L+ budget   | options selling (future module)

AishDoc rules enforced here:
  1. No trading first 30 min (9:15–9:44) — wait for direction
  2. 15-min trend must be confirmed before entry
  3. VWAP is the key intraday level — only buy above, only sell below
  4. ORB breakout is the primary entry trigger
  5. PDH/PDL are key S/R — respect them
  6. Mandatory square-off by 15:10 — never carry intraday to overnight
  7. Risk ≤ 5% of budget per trade (₹1,000 on ₹20k)
  8. Target 1:2 R:R minimum (SL 1.5% → TP 3%)
"""
import logging
import re
from datetime import datetime, time as dtime, date
from typing import Optional
from core.utils import now_ist as _now_ist, today_ist
import config
from core import ipc
from core.broker import get_broker
from core.memory import TradeMemory
from core.records import RecordTracker
from data.market import get_market_data, RealMarketData
from strategies.patterns import detect_patterns, get_candles_from_df
from strategies.signal_scorer import score_symbol

logger = logging.getLogger(__name__)


def _parse_time(t: str) -> dtime:
    h, m = map(int, t.split(":"))
    return dtime(h, m)


class TrendStrategy:
    """
    Production intraday strategy for NIFTY & BANKNIFTY.
    Replaces BaseStrategy as the main trading engine.
    """

    def __init__(self, strategy_name: str = "ATR Intraday", score_mode: str = "full"):
        self.strategy_name = strategy_name
        self.score_mode = score_mode      # "full" | "atr_only" | "ict_only"
        self.last_score: dict = {}        # most recent score result — read by BotRunner for debug
        self.broker = get_broker()
        self.memory = TradeMemory()
        self.records = RecordTracker()
        self.market = get_market_data(self.broker if not config.IS_PAPER else None)
        self.stop_loss_pct = config.STOP_LOSS_PCT
        _rr_map = {
            "atr_only": getattr(config, "ATR_RR_RATIO", 3.0),
        }
        self.rr_ratio = _rr_map.get(score_mode, getattr(config, "ATR_RR_RATIO", 3.0))
        self.take_profit_pct = config.STOP_LOSS_PCT * self.rr_ratio
        self.paused = False
        self._sl_orders: dict[str, str] = self._load_sl_orders()
        self._tp_orders: dict[str, str] = self._load_tp_orders()
        logger.info(
            "TrendStrategy[%s] initialized | score_mode=%s | Trading: %s | Phase: %s | Budget: ₹%s",
            strategy_name, score_mode,
            "PAPER" if config.IS_PAPER else "LIVE",
            config.TRADING_PHASE,
            f"{config.STARTING_BUDGET:,}",
        )

    # ── Manual intervention ────────────────────────────────────────────────────

    def pause(self):
        self.paused = True
        logger.warning("Trading PAUSED.")

    def resume(self):
        self.paused = False
        logger.info("Trading RESUMED.")

    # ── Intraday timing guards ─────────────────────────────────────────────────

    def _in_trading_window(self) -> bool:
        """AishDoc: only trade between INTRADAY_START and INTRADAY_EXIT_BY."""
        now = _now_ist().time().replace(second=0, microsecond=0)
        start = _parse_time(config.INTRADAY_START)
        end   = _parse_time(config.INTRADAY_EXIT_BY)
        return start <= now <= end

    def _in_lunch_skip(self) -> bool:
        """True during the NSE lunch chop window (12:30–13:30 IST). No new entries."""
        now   = _now_ist().time().replace(second=0, microsecond=0)
        start = _parse_time(config.LUNCH_SKIP_START)
        end   = _parse_time(config.LUNCH_SKIP_END)
        return start <= now <= end

    def _must_square_off(self) -> bool:
        """True if we're past the square-off deadline."""
        now = _now_ist().time().replace(second=0, microsecond=0)
        return now >= _parse_time(config.INTRADAY_EXIT_BY)

    # ── Position sizing (budget-aware) ─────────────────────────────────────────

    def _calc_quantity(self, symbol: str, price: float, atr: float = 0) -> int:
        """
        AishDoc ATR-based position sizing:
          risk_amount = budget × RISK_PER_TRADE_PCT%
          sl_distance = 1× ATR (adapts to current volatility)
          quantity    = risk_amount / sl_distance

        Falls back to fixed % SL if ATR is unavailable.
        """
        portfolio = self.broker.get_portfolio_summary()
        budget = portfolio.get("balance", config.STARTING_BUDGET)

        risk_amount = budget * (config.RISK_PER_TRADE_PCT / 100)  # e.g. ₹1,000 on ₹20k

        # Use ATR for SL distance — more accurate than fixed %
        if atr and atr > 0:
            sl_distance = atr          # 1× ATR = SL level below entry
        else:
            sl_distance = price * (config.STOP_LOSS_PCT / 100)  # fallback fixed %

        if sl_distance <= 0:
            return 1

        qty = int(risk_amount / sl_distance)

        # Cap by MAX_TRADE_AMOUNT
        max_qty_by_budget = int(config.MAX_TRADE_AMOUNT / price) if price > 0 else 1

        # Round to lot size, enforce MIN_LOTS (runtime-configurable from dashboard)
        lot      = config.LOT_SIZES.get(symbol, 1)
        min_lots = ipc.read_settings().get("min_lots", config.MIN_LOTS)
        min_qty  = lot * min_lots
        qty      = max(min_qty, (qty // lot) * lot)
        qty      = min(qty, max(min_qty, (max_qty_by_budget // lot) * lot))

        logger.info(
            "Position sizing %s: risk=₹%.0f / ATR=₹%.2f → %d units (%d lots, min_lots=%d)",
            symbol, risk_amount, sl_distance, qty, qty // lot, min_lots
        )
        return max(lot, qty)

    # ── Main cycle ─────────────────────────────────────────────────────────────

    def run_once(self, symbol: str) -> Optional[dict]:
        if self.paused:
            return None

        portfolio = self.broker.get_portfolio_summary()

        # ── Per-strategy daily loss — pauses THIS strategy only ───────────────
        # Calculate today's realised PnL for this specific strategy from memory.
        # If it exceeds PER_STRATEGY_DAILY_LOSS_PCT (3% = ₹3,750), pause only
        # this instance — the other two strategies continue unaffected.
        try:
            today_trades = self.memory.get_today_trades()
            strategy_pnl = sum(
                t.get("pnl", 0) for t in today_trades
                if t.get("strategy") == self.strategy_name and t.get("side") == "SELL"
            )
            per_strategy_loss_limit = config.STARTING_BUDGET * (config.PER_STRATEGY_DAILY_LOSS_PCT / 100)
            if strategy_pnl <= -per_strategy_loss_limit:
                logger.warning(
                    "[%s] Per-strategy loss limit hit: ₹%.0f today (limit ₹%.0f). Pausing this strategy only.",
                    self.strategy_name, abs(strategy_pnl), per_strategy_loss_limit,
                )
                self.pause()   # only this instance pauses — others keep running
                return None
        except Exception as _e:
            logger.debug("[%s] Per-strategy loss check skipped: %s", self.strategy_name, _e)

        # ── Combined global stop — ALL strategies pause ────────────────────────
        # ₹6,250 combined hard stop (5% of ₹1.25L). Hits only on catastrophic days.
        if portfolio.get("pnl", 0) <= -config.MAX_DAILY_LOSS:
            logger.warning(
                "[%s] Combined daily loss limit ₹%s hit. Pausing ALL strategies.",
                self.strategy_name, config.MAX_DAILY_LOSS,
            )
            ipc.write_flag(ipc.FLAG_PAUSE)   # stops all bots immediately next cycle
            self.pause()                      # also stop this instance right now
            return None

        # Fetch daily + intraday indicators
        indicators  = self.market.get_indicators(symbol)
        indicators["symbol"] = symbol

        forced = self._consume_force_trade(symbol, indicators)
        if forced:
            return forced

        intraday = {}
        if isinstance(self.market, RealMarketData):
            intraday = self.market.get_intraday_indicators(symbol)
            # Use intraday price if available (more current)
            if intraday.get("price"):
                current_price = intraday["price"]
                indicators["price"] = current_price
            else:
                current_price = indicators.get("price", 0)
        else:
            current_price = indicators.get("price", 0)

        if current_price == 0:
            logger.warning("[%s] No usable price for %s — skipping cycle", self.strategy_name, symbol)
            return None

        patterns = self._get_patterns(symbol)

        # Score with intraday signals + order flow included
        df_5m = self.market._get_df(symbol) if isinstance(self.market, RealMarketData) else None
        from data.option_chain import OptionChainFetcher
        try:
            oi_data = OptionChainFetcher.get().fetch(symbol)
        except Exception:
            oi_data = {}
        scored = score_symbol(indicators, oi_data, patterns, intraday, df_5m=df_5m, mode=self.score_mode)
        self.last_score = scored  # expose for debug/monitoring

        logger.info(
            "[%s][%s] Score: %+d/10 → %s | %s",
            self.strategy_name, symbol, scored["score"], scored["action"],
            " | ".join(scored["signals"][:4])
        )

        # ── Check open position first ──────────────────────────────────────────
        position = self._find_open_option_position(symbol)
        if position:
            return self._manage_position(
                symbol, position, current_price,
                indicators, intraday, scored, portfolio
            )

        # ── Mandatory square-off check (no new entries after exit time) ────────
        if self._must_square_off():
            logger.info("Past square-off time. No new entries.")
            return None

        if not self._in_trading_window():
            logger.info("[%s] Outside trading window (%s–%s). Waiting.",
                        symbol, config.INTRADAY_START, config.INTRADAY_EXIT_BY)
            return None

        if self._in_lunch_skip():
            logger.info("[%s] Lunch chop window (%s–%s). Skipping new entry.",
                        symbol, config.LUNCH_SKIP_START, config.LUNCH_SKIP_END)
            return None

        if portfolio.get("open_positions", 0) >= config.MAX_OPEN_POSITIONS:
            return None

        # Duplicate guard — DB-backed so it survives container restarts.
        # If ANY strategy has an unclosed BUY for this underlying today, skip.
        # This prevents: (a) cross-strategy double-entries and (b) re-entry
        # after a restart when broker.positions is empty but the DB is not.
        if self.memory.has_open_underlying_today(symbol):
            logger.info(
                "[%s] %s already has an unclosed BUY in DB today (another strategy or restart). Skipping.",
                self.strategy_name, symbol,
            )
            return None

        # Always send to Claude — it reads raw candles and decides autonomously
        return self._confirm_and_execute(symbol, current_price, indicators, intraday, scored, portfolio)

    # ── Position management ────────────────────────────────────────────────────

    def _manage_position(self, symbol: str, position: dict, current_price: float,
                          indicators: dict, intraday: dict, scored: dict,
                          portfolio: dict) -> Optional[dict]:
        avg_price = position.get("avg_price", position.get("average_price", current_price))
        quantity  = position.get("quantity", 0)
        option_price = current_price
        option_type = position.get("option_type", "CE")
        strike = position.get("atm_strike") or position.get("strike")
        if strike:
            _, _, live_ltp, _ = self._get_option_ltp(symbol, option_type, current_price, strike=int(strike))
            if live_ltp:
                option_price = live_ltp
        pnl_pct = ((option_price - avg_price) / avg_price) * 100 if avg_price else 0

        logger.info("Position %s | qty=%d avg=₹%.2f now=₹%.2f pnl=%.2f%%",
                    position.get("symbol", symbol), quantity, avg_price, option_price, pnl_pct)

        # ① Mandatory EOD square-off — no questions asked
        if self._must_square_off():
            logger.warning("SQUARE-OFF TIME: closing %s intraday position.", symbol)
            return self._force_exit(symbol, quantity, indicators,
                                    f"Mandatory intraday square-off at {config.INTRADAY_EXIT_BY}",
                                    position=position)

        # ② Stop-loss — absolute price from entry (ATR/2 distance stored at trade open)
        db_trade = self.memory.get_open_trade_for_symbol(position.get("symbol") or position.get("tradingsymbol") or symbol)
        sl_price = (db_trade or {}).get("sl_price") or position.get("sl_price")
        tp_price = (db_trade or {}).get("tp_price") or position.get("tp_price")

        if sl_price and option_price <= sl_price:
            logger.warning("STOP-LOSS: %s option ₹%.2f ≤ SL ₹%.2f — exiting", symbol, option_price, sl_price)
            return self._force_exit(symbol, quantity, indicators,
                                    f"SL hit: ₹{option_price:.2f} ≤ ₹{sl_price:.2f}",
                                    position=position)
        elif not sl_price and pnl_pct <= -self.stop_loss_pct:
            logger.warning("STOP-LOSS (%%): %s %.2f%% — exiting", symbol, pnl_pct)
            return self._force_exit(symbol, quantity, indicators,
                                    f"SL hit at {pnl_pct:.2f}%",
                                    position=position)

        # ③ VWAP flip — AishDoc: if price crosses below VWAP, exit long
        reverse_signal = (
            (option_type == "CE" and scored["action"] == "SELL")
            or (option_type == "PE" and scored["action"] == "BUY")
        )
        if reverse_signal:
            return self._force_exit(
                symbol,
                quantity,
                indicators,
                f"Signal reversed to {scored['action']} at score {scored['score']:+d}",
                position=position,
            )

        if tp_price and option_price >= tp_price:
            return self._force_exit(
                symbol,
                quantity,
                indicators,
                f"TP hit: ₹{option_price:.2f} ≥ ₹{tp_price:.2f} (1:{self.rr_ratio:.1f})",
                position=position,
            )
        elif not tp_price and pnl_pct >= self.take_profit_pct:
            return self._force_exit(
                symbol,
                quantity,
                indicators,
                f"TP hit at {pnl_pct:.2f}% (target {self.take_profit_pct:.2f}%)",
                position=position,
            )

        return None

    # ── Entry confirmation ─────────────────────────────────────────────────────

    def _confirm_and_execute(self, symbol: str, current_price: float,
                              indicators: dict, intraday: dict,
                              scored: dict, portfolio: dict) -> Optional[dict]:
        if scored["action"] not in ("BUY", "SELL"):
            return None

        # Live threshold = 8 minimum regardless of scorer default.
        # Backtest uses its own threshold via --threshold flag.
        # Score 6-7 in live hits too many traps / fake breakouts.
        live_threshold = max(scored["threshold"], 8)
        if abs(scored["score"]) < live_threshold:
            logger.info(
                "[%s] Score %+d below live threshold %d — skipping (would pass backtest at %d)",
                self.strategy_name, scored["score"], live_threshold, scored["threshold"]
            )
            return None

        entry_direction = scored["action"]
        atr = intraday.get("atr_5m") or indicators.get("atr_14", 0)
        decision = {
            "action": "BUY",
            "symbol": symbol,
            "quantity": self._calc_quantity(symbol, current_price, atr),
            "confidence": round(abs(scored["score"]) / 10, 2),
            "reasoning": f"Deterministic {self.score_mode} entry at score {scored['score']:+d}",
            "risk_level": "MEDIUM" if abs(scored["score"]) >= 8 else "STANDARD",
            "score": scored["score"],
            "signals": scored["signals"],
            "entry_direction": entry_direction,
            "option_type": "CE" if entry_direction == "BUY" else "PE",
            "atr": atr,
        }
        return self._execute(decision, indicators)

    # ── Force exit (SL / square-off) ──────────────────────────────────────────

    def _force_exit(self, symbol: str, quantity: int, indicators: dict,
                    reason: str, position: dict = None) -> dict:
        decision = {
            "action": "SELL", "symbol": symbol, "quantity": quantity,
            "confidence": 1.0, "reasoning": reason, "risk_level": "HIGH",
        }
        if position:
            decision["_position"] = position
        return self._execute(decision, indicators)

    # ── Order execution ────────────────────────────────────────────────────────

    def _estimate_paper_option_ltp(self, symbol: str, option_type: str,
                                   spot_price: float, strike: int) -> tuple[str, float]:
        """Paper-only premium model so simulations can proceed without live NFO quotes."""
        intrinsic = max(0.0, spot_price - strike) if option_type == "CE" else max(0.0, strike - spot_price)
        distance = abs(spot_price - strike)
        base_time_value = max(18.0, spot_price * 0.0035)
        time_value = max(8.0, base_time_value - distance * 0.45)
        premium = round(intrinsic * 0.55 + time_value, 2)
        return "", max(1.0, premium)

    def _paper_option_symbol(self, symbol: str, expiry: date, strike: int, option_type: str) -> str:
        expiry_str = expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry)
        return f"{symbol}-{expiry_str}-{strike}{option_type}"

    def _get_option_ltp(self, symbol: str, option_type: str, current_price: float,
                        strike: int = None) -> tuple[str | None, int, float | None, date | None]:
        """Find the strike whose live premium is in [MIN_OPTION_PREMIUM, MAX_OPTION_PREMIUM].

        Starts at ATM, then walks ITM (to raise premium) or OTM (to lower it) in ₹50 steps,
        up to 10 strikes either side. If no strike hits the range, returns the closest one found.
        Returns (tradingsymbol, strike, ltp, expiry) — ltp=None only on total fetch failure.
        """
        atm_strike = int(strike or round(current_price / 50) * 50)
        min_prem = getattr(config, "MIN_OPTION_PREMIUM", 150)
        max_prem = getattr(config, "MAX_OPTION_PREMIUM", 170)
        expiry = None

        try:
            from data.angel_fetcher import AngelFetcher
            expiry = AngelFetcher.nearest_weekly_expiry()

            # Walk strikes: ITM raises premium, OTM lowers it
            # CE: ITM = lower strike, OTM = higher strike
            # PE: ITM = higher strike, OTM = lower strike
            step = 50
            best_sym, best_strike, best_ltp = None, atm_strike, None

            for i in range(11):  # ATM + up to 10 strikes either direction
                for direction in ([0] if i == 0 else [-1, 1]):
                    candidate = atm_strike + direction * i * step
                    tsym, ltp = AngelFetcher.get().get_option_ltp(symbol, candidate, option_type, expiry)
                    if not ltp or ltp <= 0:
                        continue
                    logger.info("Strike search: %s %d%s @ ₹%.2f", symbol, candidate, option_type, ltp)
                    if min_prem <= ltp <= max_prem:
                        logger.info("Strike found in range: %s %d%s @ ₹%.2f", symbol, candidate, option_type, ltp)
                        return tsym, candidate, ltp, expiry
                    # track best seen so far (closest to range)
                    if best_ltp is None or abs(ltp - (min_prem + max_prem) / 2) < abs(best_ltp - (min_prem + max_prem) / 2):
                        best_sym, best_strike, best_ltp = tsym, candidate, ltp

            if best_ltp:
                logger.warning(
                    "No strike in ₹%d–₹%d range for %s %s — using closest: %d @ ₹%.2f",
                    min_prem, max_prem, symbol, option_type, best_strike, best_ltp,
                )
                return best_sym, best_strike, best_ltp, expiry

        except Exception as e:
            logger.warning("Option LTP fetch failed for %s %s: %s", symbol, option_type, e)

        if config.IS_PAPER:
            # Paper mode: find the strike whose model premium is in range
            best_strike, best_prem = atm_strike, None
            for i in range(11):
                for direction in ([0] if i == 0 else [-1, 1]):
                    candidate = atm_strike + direction * i * step
                    _, prem = self._estimate_paper_option_ltp(symbol, option_type, current_price, candidate)
                    if min_prem <= prem <= max_prem:
                        best_strike, best_prem = candidate, prem
                        break
                    if best_prem is None or abs(prem - (min_prem + max_prem) / 2) < abs(best_prem - (min_prem + max_prem) / 2):
                        best_strike, best_prem = candidate, prem
                if best_prem and min_prem <= best_prem <= max_prem:
                    break
            if expiry is None:
                try:
                    from data.angel_fetcher import AngelFetcher
                    expiry = AngelFetcher.nearest_weekly_expiry()
                except Exception:
                    expiry = today_ist()
            paper_symbol = self._paper_option_symbol(symbol, expiry, best_strike, option_type)
            logger.info("Paper strike in range: %s @ ₹%.2f", paper_symbol, best_prem)
            return paper_symbol, best_strike, best_prem, expiry

        msg = f"Option LTP unavailable for {symbol} {option_type}"
        logger.error(msg + " — skipping trade")
        from core.angel_error_log import log_error as _log_err
        _log_err("get_option_ltp", msg, symbol=symbol, detail=option_type)
        return None, atm_strike, None, expiry

    def _execute(self, decision: dict, indicators: dict) -> dict:
        symbol        = decision["symbol"]
        side          = decision["action"]
        quantity      = max(1, decision.get("quantity", 1))
        current_price = indicators.get("price", 0)
        atm_strike    = int(round(current_price / 50) * 50)

        if side == "BUY":
            option_type = decision.get("option_type") or ("CE" if decision.get("entry_direction", "BUY") == "BUY" else "PE")
            option_symbol, atm_strike, option_ltp, expiry = self._get_option_ltp(
                symbol, option_type, current_price, strike=decision.get("strike")
            )
            if option_ltp is None:
                return {"status": "SKIPPED", "reason": "option LTP unavailable"}
            if option_ltp < 5.0:
                logger.warning(
                    "[%s] %s %s premium ₹%.2f below ₹5 minimum — skipping (likely near-expiry worthless option)",
                    self.strategy_name, option_symbol, option_type, option_ltp,
                )
                return {"status": "SKIPPED", "reason": f"premium ₹{option_ltp:.2f} too low (min ₹5)"}
            if not config.IS_PAPER and hasattr(self.broker, "preflight_order"):
                preflight = self.broker.preflight_order(
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    order_type="MARKET",
                    price=option_ltp,
                    exchange="NFO",
                    product="MIS",
                    variety="regular",
                    validity="DAY",
                    tag=self.strategy_name[:20],
                    tradingsymbol=option_symbol,
                    option_type=option_type,
                    strike=atm_strike,
                    expiry=expiry,
                    log_failures=True,
                )
                if not preflight.get("ok"):
                    logger.error("[%s] Live preflight failed for %s: %s", self.strategy_name, option_symbol, preflight.get("error"))
                    return {
                        "status": "REJECTED",
                        "symbol": option_symbol,
                        "side": side,
                        "quantity": quantity,
                        "reason": preflight.get("error"),
                        "preflight": preflight,
                    }
            order = self.broker.place_order(
                option_symbol, side, quantity, price=option_ltp,
                exchange="NFO", product="MIS", order_type="MARKET",
                variety="regular", validity="DAY", tag=self.strategy_name[:20],
            )
            order["price"] = option_ltp
            order["timestamp"] = order.get("timestamp") or _now_ist().isoformat()
            order["expiry"] = expiry.isoformat() if expiry else None

            # Place exchange-level SL-M order immediately after entry (live only).
            # This protects the position even if the bot process dies.
            # SL distance = ATR/2 (absolute points); TP = entry + SL_dist × RR ratio.
            _atr = decision.get("atr") or indicators.get("atr_5m") or indicators.get("atr_14") or 0
            _sl_dist = round(_atr / 2, 1) if _atr else round(option_ltp * (self.stop_loss_pct / 100), 1)
            _sl_dist = max(_sl_dist, 1.0)  # never less than ₹1
            order["sl_price"] = round(option_ltp - _sl_dist, 1)
            order["tp_price"] = round(option_ltp + _sl_dist * self.rr_ratio, 1)
            logger.info(
                "[%s] SL/TP: entry=₹%.2f ATR=%.1f SL_dist=%.1f → SL=₹%.1f TP=₹%.1f (1:%.1f)",
                self.strategy_name, option_ltp, _atr, _sl_dist,
                order["sl_price"], order["tp_price"], self.rr_ratio,
            )
            if order.get("status") in ("COMPLETE", "PLACED") and not config.IS_PAPER:
                sl_trigger = order["sl_price"]
                try:
                    sl_ord = self.broker.place_order(
                        option_symbol, "SELL", quantity,
                        order_type="SL-M", trigger_price=sl_trigger,
                        exchange="NFO", product="MIS",
                        variety="stoploss", validity="DAY",
                        tag=f"SL-{self.strategy_name[:14]}",
                    )
                    if sl_ord.get("order_id"):
                        self._sl_orders[option_symbol] = sl_ord["order_id"]
                        ipc.write_sl_orders(self.strategy_name, self._sl_orders)
                        order["sl_order_id"] = sl_ord["order_id"]
                        order["sl_trigger"]  = sl_trigger
                        logger.info("[%s] SL-M placed: %s trigger ₹%.2f order_id=%s",
                                    self.strategy_name, option_symbol, sl_trigger, sl_ord["order_id"])
                    else:
                        _reason = sl_ord.get("reason", "unknown rejection")
                        logger.error("[%s] SL-M order rejected for %s: %s — bot managing SL in-process at ₹%.2f",
                                     self.strategy_name, option_symbol, _reason, sl_trigger)
                        from core.angel_error_log import log_error as _log_err
                        _log_err("sl_order_rejected",
                                 f"SL-M rejected: {_reason} — in-process SL active at ₹{sl_trigger:.2f}",
                                 symbol=option_symbol, detail=f"trigger=₹{sl_trigger:.2f}")
                except Exception as e:
                    logger.error("[%s] Failed to place SL-M for %s: %s — bot managing SL in-process at ₹%.2f",
                                 self.strategy_name, option_symbol, e, sl_trigger)
                    from core.angel_error_log import log_error as _log_err
                    _log_err("sl_order_failed",
                             f"SL-M exception: {e} — in-process SL active at ₹{sl_trigger:.2f}",
                             symbol=option_symbol, detail=f"trigger=₹{sl_trigger:.2f}")

            # Place exchange TP LIMIT SELL order immediately after entry.
            # If SL fires first → cancel this TP order to avoid naked short.
            # If TP fires first → cancel SL-M order.
            if order.get("status") in ("COMPLETE", "PLACED") and not config.IS_PAPER:
                tp_price_val = order.get("tp_price")
                if tp_price_val:
                    try:
                        tp_ord = self.broker.place_order(
                            option_symbol, "SELL", quantity,
                            order_type="LIMIT", price=float(tp_price_val),
                            exchange="NFO", product="MIS",
                            variety="regular", validity="DAY",
                            tag=f"TP-{self.strategy_name[:14]}",
                        )
                        if tp_ord.get("order_id"):
                            self._tp_orders[option_symbol] = tp_ord["order_id"]
                            ipc.write_tp_orders(self.strategy_name, self._tp_orders)
                            order["tp_order_id"] = tp_ord["order_id"]
                            logger.info("[%s] TP LIMIT placed: %s at ₹%.2f order_id=%s",
                                        self.strategy_name, option_symbol, tp_price_val, tp_ord["order_id"])
                        else:
                            logger.warning("[%s] TP LIMIT rejected for %s: %s — software TP active",
                                           self.strategy_name, option_symbol, tp_ord.get("reason"))
                            from core.angel_error_log import log_error as _log_err
                            _log_err("tp_order_rejected",
                                     f"TP LIMIT rejected: {tp_ord.get('reason')} — software TP at ₹{tp_price_val}",
                                     symbol=option_symbol, detail=f"tp=₹{tp_price_val}")
                    except Exception as e:
                        logger.error("[%s] Failed to place TP LIMIT for %s: %s", self.strategy_name, option_symbol, e)

            # Persist option metadata in broker position for accurate exit pricing
            if hasattr(self.broker, "positions") and option_symbol in self.broker.positions:
                self.broker.positions[option_symbol]["symbol"] = option_symbol
                self.broker.positions[option_symbol]["underlying"] = symbol
                self.broker.positions[option_symbol]["option_type"] = option_type
                self.broker.positions[option_symbol]["atm_strike"]  = atm_strike
                self.broker.positions[option_symbol]["expiry"] = order["expiry"]
        else:
            pos         = decision.get("_position") or self._find_open_option_position(symbol) or {}
            option_type = pos.get("option_type", "CE")
            atm_strike  = pos.get("atm_strike") or pos.get("strike") or atm_strike
            option_symbol = pos.get("symbol") or symbol

            # Cancel both SL-M and TP LIMIT before placing normal exit
            # to avoid double-sell / naked short on exchange.
            if not config.IS_PAPER:
                self._cancel_sl_order(option_symbol)
                self._cancel_tp_order(option_symbol)

            _, _, exit_ltp, expiry = self._get_option_ltp(
                pos.get("underlying", symbol), option_type, current_price, strike=int(atm_strike)
            )
            if exit_ltp is None:
                return {"status": "SKIPPED", "reason": "option LTP unavailable at exit"}
            if not config.IS_PAPER and hasattr(self.broker, "preflight_order"):
                preflight = self.broker.preflight_order(
                    symbol=pos.get("underlying", symbol),
                    side=side,
                    quantity=quantity,
                    order_type="MARKET",
                    price=exit_ltp,
                    exchange=pos.get("exchange", "NFO"),
                    product=pos.get("product", "MIS"),
                    variety="regular",
                    validity="DAY",
                    tag=self.strategy_name[:20],
                    tradingsymbol=option_symbol,
                    option_type=option_type,
                    strike=int(atm_strike),
                    expiry=expiry,
                    log_failures=True,
                )
                if not preflight.get("ok"):
                    logger.error("[%s] Live exit preflight failed for %s: %s", self.strategy_name, option_symbol, preflight.get("error"))
                    return {
                        "status": "REJECTED",
                        "symbol": option_symbol,
                        "side": side,
                        "quantity": quantity,
                        "reason": preflight.get("error"),
                        "preflight": preflight,
                    }
            order = self.broker.place_order(
                option_symbol, side, quantity, price=exit_ltp,
                exchange=pos.get("exchange", "NFO"), product=pos.get("product", "MIS"), order_type="MARKET",
                variety="regular", validity="DAY", tag=self.strategy_name[:20],
            )
            order["price"] = exit_ltp
            order["timestamp"] = order.get("timestamp") or _now_ist().isoformat()
            order["expiry"] = pos.get("expiry") or (expiry.isoformat() if expiry else None)

        if order.get("status") in ("COMPLETE", "PLACED"):
            order["underlying"]  = symbol if side == "BUY" else pos.get("underlying", symbol)
            order["option_type"] = option_type
            order["strike"]      = atm_strike
            order["strategy"]    = self.strategy_name
            self.memory.log_trade(order, decision)
            if side == "SELL":
                self.memory.close_latest_open_trade(
                    order.get("symbol"),
                    self.strategy_name,
                    float(order.get("pnl") or 0),
                )
            broken = self.records.check_trade(order)
            if broken:
                logger.info("ALL-TIME RECORD BROKEN: %s", broken)
        elif order.get("status") == "REJECTED":
            logger.error("[%s] Order rejected for %s: %s", self.strategy_name, order.get("symbol"), order.get("reason"))

        return order

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _parse_option_meta(self, tradingsymbol: str, underlying: str) -> dict:
        option_type = "PE" if tradingsymbol.endswith("PE") else "CE"
        strike = None
        expiry = None
        match = re.search(r"(\d{5})(CE|PE)$", tradingsymbol)
        if match:
            strike = int(match.group(1))
        exp_match = re.match(rf"{re.escape(underlying)}-(\d{{4}}-\d{{2}}-\d{{2}})-\d{{5}}(CE|PE)$", tradingsymbol)
        if exp_match:
            expiry = exp_match.group(1)
        return {"symbol": tradingsymbol, "underlying": underlying,
                "option_type": option_type, "atm_strike": strike, "expiry": expiry}

    def _find_open_option_position(self, underlying: str) -> Optional[dict]:
        positions = self.broker.get_positions()
        legacy = positions.get(underlying)
        if legacy and legacy.get("quantity", 0) > 0 and self.memory.has_open_trade(underlying, self.strategy_name):
            return {**legacy, "symbol": underlying, "underlying": underlying}

        for key, raw in positions.items():
            tradingsymbol = raw.get("tradingsymbol") or raw.get("symbol") or key
            qty = raw.get("quantity", raw.get("net_quantity", 0)) or 0
            if qty <= 0 or not tradingsymbol.startswith(underlying) or not tradingsymbol.endswith(("CE", "PE")):
                continue
            if not self.memory.has_open_trade(tradingsymbol, self.strategy_name):
                continue
            avg_price = raw.get("avg_price", raw.get("average_price", raw.get("buy_price", 0)))
            return {**raw, **self._parse_option_meta(tradingsymbol, underlying),
                    "quantity": qty, "avg_price": avg_price}

        # Reconcile: DB says open but broker has no position and fetch was successful.
        # This means the position was manually closed on Angel One — update DB so
        # the bot doesn't block new entries for the rest of the day.
        positions_ok = getattr(self.broker, "_last_positions_ok", False)
        if positions_ok and self.memory.has_open_underlying_today(underlying):
            rows = self.memory.close_open_underlying_today(underlying, close_reason="manual_close_detected")
            if rows:
                logger.warning(
                    "[%s] %s: position gone from broker but open in DB (%d row(s)) — "
                    "marked as manually closed. Bot is unblocked for new entries.",
                    self.strategy_name, underlying, rows,
                )
        return None

    def _consume_force_trade(self, symbol: str, indicators: dict) -> Optional[dict]:
        forced = ipc.read_and_clear_force_trade()
        if not forced:
            return None
        if forced.get("symbol", symbol) != symbol:
            logger.warning("[%s] Ignoring force trade for unsupported symbol: %s", self.strategy_name, forced)
            return None

        side = str(forced.get("side", "BUY")).upper()
        open_pos = self._find_open_option_position(symbol)
        if side == "SELL" and open_pos:
            return self._force_exit(
                symbol,
                int(forced.get("quantity") or open_pos.get("quantity", 1)),
                indicators,
                forced.get("reason", "Manual force exit"),
                position=open_pos,
            )

        decision = {
            "action": "BUY",
            "symbol": symbol,
            "quantity": int(forced.get("quantity", 1)),
            "confidence": 1.0,
            "reasoning": forced.get("reason", "Manual force trade"),
            "risk_level": "HIGH",
            "entry_direction": side,
            "option_type": forced.get("option_type") or ("PE" if side == "SELL" else "CE"),
        }
        if forced.get("strike"):
            decision["strike"] = int(forced["strike"])
        return self._execute(decision, indicators)

    def _get_patterns(self, symbol: str) -> dict:
        try:
            if isinstance(self.market, RealMarketData):
                df = self.market._get_df(symbol)
                if df is not None and len(df) >= 3:
                    return detect_patterns(get_candles_from_df(df))
        except Exception as e:
            logger.warning("Pattern detection failed for %s: %s", symbol, e)
        return {"patterns": [], "bias": "neutral", "strength": 0}

    def _load_sl_orders(self) -> dict:
        try:
            return ipc.read_sl_orders(self.strategy_name)
        except Exception:
            return {}

    def _load_tp_orders(self) -> dict:
        try:
            return ipc.read_tp_orders(self.strategy_name)
        except Exception:
            return {}

    def _cancel_tp_order(self, option_symbol: str):
        """Cancel the exchange TP LIMIT order for a symbol (call when SL hits or position closed)."""
        tp_order_id = self._tp_orders.pop(option_symbol, None)
        if tp_order_id:
            ipc.write_tp_orders(self.strategy_name, self._tp_orders)
            try:
                self.broker.cancel_order(tp_order_id)
                logger.info("[%s] Cancelled TP LIMIT %s for %s", self.strategy_name, tp_order_id, option_symbol)
            except Exception as e:
                logger.warning("[%s] Could not cancel TP order %s: %s", self.strategy_name, tp_order_id, e)

    def _cancel_sl_order(self, option_symbol: str):
        """Cancel the exchange SL-M order for a symbol (call when TP hits)."""
        sl_order_id = self._sl_orders.pop(option_symbol, None)
        if sl_order_id:
            ipc.write_sl_orders(self.strategy_name, self._sl_orders)
            try:
                self.broker.cancel_order(sl_order_id)
                logger.info("[%s] Cancelled SL-M %s for %s", self.strategy_name, sl_order_id, option_symbol)
            except Exception as e:
                logger.warning("[%s] Could not cancel SL order %s: %s", self.strategy_name, sl_order_id, e)

    # ── Square-off all open positions ─────────────────────────────────────────

    def square_off_all(self) -> dict:
        """Called at INTRADAY_EXIT_BY — close every open position."""
        positions = self.broker.get_positions()
        results = {}
        for symbol, pos in positions.items():
            qty = pos.get("quantity", 0)
            if qty > 0:
                logger.warning("Square-off: closing %d %s", qty, symbol)
                try:
                    underlying = pos.get("underlying") or ("NIFTY" if str(symbol).startswith("NIFTY") else symbol)
                    if not self.memory.has_open_trade(str(symbol), self.strategy_name):
                        continue
                    normalized = {**self._parse_option_meta(str(symbol), underlying), **pos}
                    indicators = self.market.get_indicators(underlying)
                    result = self._force_exit(
                        underlying, qty, indicators,
                        f"EOD intraday square-off at {config.INTRADAY_EXIT_BY}",
                        position=normalized,
                    )
                    results[symbol] = result
                except Exception as e:
                    logger.error("Square-off failed for %s: %s", symbol, e)
        ipc.clear_sl_orders(self.strategy_name)
        ipc.clear_tp_orders(self.strategy_name)
        self._sl_orders.clear()
        self._tp_orders.clear()
        return results

    # ── Watchlist loop ─────────────────────────────────────────────────────────

    def run_watchlist(self) -> dict:
        results = {}
        for category, symbols in config.WATCHLIST.items():
            for symbol in symbols:
                try:
                    result = self.run_once(symbol)
                    if result:
                        results[symbol] = result
                except Exception as e:
                    logger.error("Error processing %s: %s", symbol, e)
        return results

    # ── End of day ─────────────────────────────────────────────────────────────

    def end_of_day(self) -> dict:
        today = today_ist()
        trades = self.memory.get_today_trades()
        broken_records = self.records.check_daily(trades)
        completed = self.memory.build_round_trips(trades)
        total_pnl = round(sum(t.get("pnl", 0) for t in completed), 2)
        review = (
            f"Deterministic review for {self.strategy_name}: "
            f"{len(completed)} completed trades, total PnL ₹{total_pnl:.2f}."
        )
        self.memory.save_daily_summary(today, trades, review)
        logger.info("EOD done. Trades: %d | Records: %s", len(trades), broken_records)
        logger.info(review)
        return {"date": today, "trades": len(trades), "broken_records": broken_records, "review": review}
