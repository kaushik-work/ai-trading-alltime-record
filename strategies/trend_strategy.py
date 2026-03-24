"""
Trend Strategy — AishDoc intraday approach for NIFTY & BANKNIFTY.

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
from datetime import datetime, time as dtime
from typing import Optional
import config
from core.broker import get_broker
from core.brain import TradingBrain
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

    def __init__(self):
        self.broker = get_broker()
        self.brain = TradingBrain()
        self.memory = TradeMemory()
        self.records = RecordTracker()
        self.market = get_market_data(self.broker if not config.IS_PAPER else None)
        self.paused = False
        logger.info(
            "TrendStrategy initialized | Mode: %s | Phase: %s | Budget: ₹%s",
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
        now = datetime.now().time().replace(second=0, microsecond=0)
        start = _parse_time(config.INTRADAY_START)
        end   = _parse_time(config.INTRADAY_EXIT_BY)
        return start <= now <= end

    def _must_square_off(self) -> bool:
        """True if we're past the square-off deadline."""
        now = datetime.now().time().replace(second=0, microsecond=0)
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

        # Round to lot size
        lot = config.LOT_SIZES.get(symbol, 1)
        qty = max(lot, (qty // lot) * lot)
        qty = min(qty, max(lot, (max_qty_by_budget // lot) * lot))

        logger.info(
            "Position sizing %s: risk=₹%.0f / ATR=₹%.2f → %d units (%d lots)",
            symbol, risk_amount, sl_distance, qty, qty // lot
        )
        return max(lot, qty)

    # ── Main cycle ─────────────────────────────────────────────────────────────

    def run_once(self, symbol: str) -> Optional[dict]:
        if self.paused:
            return None

        portfolio = self.broker.get_portfolio_summary()

        # Daily loss limit
        if portfolio.get("pnl", 0) <= -config.MAX_DAILY_LOSS:
            logger.warning("Daily loss limit ₹%s hit. Pausing.", config.MAX_DAILY_LOSS)
            self.pause()
            return None

        # Fetch daily + intraday indicators
        indicators  = self.market.get_indicators(symbol)
        current_price = indicators.get("price", 0)
        if current_price == 0:
            return None

        intraday = {}
        if isinstance(self.market, RealMarketData):
            intraday = self.market.get_intraday_indicators(symbol)
            # Use intraday price if available (more current)
            if intraday.get("price"):
                current_price = intraday["price"]
                indicators["price"] = current_price

        patterns = self._get_patterns(symbol)

        # Score with intraday signals included
        scored = score_symbol(indicators, {}, patterns, intraday)

        logger.info(
            "[%s] Score: %+d/10 → %s | %s",
            symbol, scored["score"], scored["action"],
            " | ".join(scored["signals"][:4])
        )

        # ── Check open position first ──────────────────────────────────────────
        positions = self.broker.get_positions()
        if symbol in positions:
            return self._manage_position(
                symbol, positions[symbol], current_price,
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

        if portfolio.get("open_positions", 0) >= config.MAX_OPEN_POSITIONS:
            return None

        # Only send to Claude if score clears threshold
        if scored["action"] == "BUY":
            return self._confirm_and_execute(symbol, current_price, indicators, intraday, scored, portfolio)

        return None

    # ── Position management ────────────────────────────────────────────────────

    def _manage_position(self, symbol: str, position: dict, current_price: float,
                          indicators: dict, intraday: dict, scored: dict,
                          portfolio: dict) -> Optional[dict]:
        avg_price = position.get("avg_price", current_price)
        quantity  = position.get("quantity", 0)
        pnl_pct   = ((current_price - avg_price) / avg_price) * 100 if avg_price else 0

        logger.info("Position %s | qty=%d avg=₹%.2f now=₹%.2f pnl=%.2f%%",
                    symbol, quantity, avg_price, current_price, pnl_pct)

        # ① Mandatory EOD square-off — no questions asked
        if self._must_square_off():
            logger.warning("SQUARE-OFF TIME: closing %s intraday position.", symbol)
            return self._force_exit(symbol, quantity, indicators,
                                    f"Mandatory intraday square-off at {config.INTRADAY_EXIT_BY}")

        # ② ATR-based stop-loss — dynamic, adapts to volatility
        # Use intraday ATR SL level if available, else fall back to fixed %
        atr_sl_price = intraday.get("atr_sl") if intraday else None
        if atr_sl_price and current_price <= atr_sl_price:
            logger.warning("ATR STOP-LOSS: %s price ₹%.2f ≤ ATR-SL ₹%.2f — exiting",
                           symbol, current_price, atr_sl_price)
            return self._force_exit(symbol, quantity, indicators,
                                    f"ATR stop-loss hit: price ₹{current_price:.2f} ≤ ATR-SL ₹{atr_sl_price:.2f}")
        elif pnl_pct <= -config.STOP_LOSS_PCT:
            logger.warning("STOP-LOSS: %s %.2f%% — exiting", symbol, pnl_pct)
            return self._force_exit(symbol, quantity, indicators,
                                    f"Stop-loss hit at {pnl_pct:.2f}% (threshold -{config.STOP_LOSS_PCT}%)")

        # ③ VWAP flip — AishDoc: if price crosses below VWAP, exit long
        if intraday.get("above_vwap") is False and scored["action"] == "SELL":
            logger.info("VWAP flip bearish for %s — asking Claude to exit", symbol)

        # ④ Score strongly bearish OR take-profit hit → ask Claude
        should_review = scored["action"] == "SELL" or pnl_pct >= config.TAKE_PROFIT_PCT
        if should_review:
            trade_history = self.memory.get_trades_for_symbol(symbol)
            enriched = {
                **indicators,
                "open_position": {
                    "quantity": quantity,
                    "avg_buy_price": avg_price,
                    "current_price": current_price,
                    "unrealized_pnl_pct": round(pnl_pct, 2),
                    "unrealized_pnl_inr": round((current_price - avg_price) * quantity, 2),
                },
                "signal_score": scored["score"],
                "signals": scored["signals"],
                "intraday": intraday,
                "trading_type": config.TRADING_TYPE,
                "must_exit_by": config.INTRADAY_EXIT_BY,
            }
            decision = self.brain.analyze(symbol, enriched, trade_history, portfolio)
            if decision["action"] == "SELL" and decision.get("confidence", 0) >= 0.55:
                decision["quantity"] = quantity
                return self._execute(decision, indicators)

        return None

    # ── Entry confirmation ─────────────────────────────────────────────────────

    def _confirm_and_execute(self, symbol: str, current_price: float,
                              indicators: dict, intraday: dict,
                              scored: dict, portfolio: dict) -> Optional[dict]:
        trade_history = self.memory.get_trades_for_symbol(symbol)

        enriched = {
            **indicators,
            "signal_score": scored["score"],
            "signal_threshold": scored["threshold"],
            "signals": scored["signals"],
            "signal_breakdown": scored["breakdown"],
            "intraday": intraday,
            "trading_type": config.TRADING_TYPE,
            "trading_phase": config.TRADING_PHASE,
            "budget": portfolio.get("balance", config.STARTING_BUDGET),
            "risk_per_trade_pct": config.RISK_PER_TRADE_PCT,
            "stop_loss_pct": config.STOP_LOSS_PCT,
            "take_profit_pct": config.TAKE_PROFIT_PCT,
            "must_exit_by": config.INTRADAY_EXIT_BY,
            "lot_size": config.LOT_SIZES.get(symbol, 1),
        }

        decision = self.brain.analyze(symbol, enriched, trade_history, portfolio)

        if decision["action"] == "BUY" and decision.get("confidence", 0) >= 0.55:
            # ATR-based position sizing — uses intraday ATR if available, else daily ATR
            atr = intraday.get("atr_5m") or indicators.get("atr_14", 0)
            decision["quantity"] = self._calc_quantity(symbol, current_price, atr)
            decision["score"] = scored["score"]
            decision["signals"] = scored["signals"]
            return self._execute(decision, indicators)

        logger.info("Claude vetoed entry for %s (action=%s conf=%.0f%%)",
                    symbol, decision["action"], decision.get("confidence", 0) * 100)
        return None

    # ── Force exit (SL / square-off) ──────────────────────────────────────────

    def _force_exit(self, symbol: str, quantity: int, indicators: dict,
                    reason: str) -> dict:
        decision = {
            "action": "SELL", "symbol": symbol, "quantity": quantity,
            "confidence": 1.0, "reasoning": reason, "risk_level": "HIGH",
        }
        return self._execute(decision, indicators)

    # ── Order execution ────────────────────────────────────────────────────────

    def _execute(self, decision: dict, indicators: dict) -> dict:
        symbol   = decision["symbol"]
        side     = decision["action"]
        quantity = max(1, decision.get("quantity", 1))

        order = self.broker.place_order(symbol, side, quantity)

        if order.get("status") in ("COMPLETE", "PLACED"):
            self.memory.log_trade(order, decision)
            broken = self.records.check_trade(order)
            if broken:
                logger.info("ALL-TIME RECORD BROKEN: %s", broken)

        return order

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_patterns(self, symbol: str) -> dict:
        try:
            if isinstance(self.market, RealMarketData):
                df = self.market._get_df(symbol)
                if df is not None and len(df) >= 3:
                    return detect_patterns(get_candles_from_df(df))
        except Exception as e:
            logger.warning("Pattern detection failed for %s: %s", symbol, e)
        return {"patterns": [], "bias": "neutral", "strength": 0}

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
                    indicators = self.market.get_indicators(symbol)
                    result = self._force_exit(
                        symbol, qty, indicators,
                        f"EOD intraday square-off at {config.INTRADAY_EXIT_BY}"
                    )
                    results[symbol] = result
                except Exception as e:
                    logger.error("Square-off failed for %s: %s", symbol, e)
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
        today = datetime.now().strftime("%Y-%m-%d")
        trades = self.memory.get_today_trades()
        broken_records = self.records.check_daily(trades)
        review = self.brain.daily_review(trades, self.records.get_all_records())
        self.memory.save_daily_summary(today, trades, review)
        logger.info("EOD done. Trades: %d | Records: %s", len(trades), broken_records)
        logger.info(review)
        return {"date": today, "trades": len(trades), "broken_records": broken_records, "review": review}
