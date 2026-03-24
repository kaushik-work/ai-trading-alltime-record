import logging
from datetime import datetime
from typing import Optional
import config
from core.broker import get_broker
from core.brain import TradingBrain
from core.memory import TradeMemory
from core.records import RecordTracker
from data.market import get_market_data

logger = logging.getLogger(__name__)


class BaseStrategy:
    """
    Base trading strategy.
    Flow per symbol:
      1. Check open position → apply stop-loss / take-profit
      2. If no position → ask Claude whether to enter
    """

    def __init__(self):
        self.broker = get_broker()
        self.brain = TradingBrain()
        self.memory = TradeMemory()
        self.records = RecordTracker()
        self.market = get_market_data(self.broker if not config.IS_PAPER else None)
        self.paused = False
        logger.info("Strategy initialized | Mode: %s", "PAPER" if config.IS_PAPER else "LIVE")

    # ── Manual intervention ────────────────────────────────────────────────────

    def pause(self):
        self.paused = True
        logger.warning("Trading PAUSED by manual intervention.")

    def resume(self):
        self.paused = False
        logger.info("Trading RESUMED.")

    # ── Core cycle ─────────────────────────────────────────────────────────────

    def run_once(self, symbol: str) -> Optional[dict]:
        """Run one full decision cycle for a symbol."""
        if self.paused:
            logger.info("Trading is paused. Skipping %s.", symbol)
            return None

        portfolio = self.broker.get_portfolio_summary()

        # Hard stop — daily loss limit breached
        if portfolio.get("pnl", 0) <= -config.MAX_DAILY_LOSS:
            logger.warning("Daily loss limit hit (₹%.2f). Pausing trading.", config.MAX_DAILY_LOSS)
            self.pause()
            return None

        # Fetch real market data + indicators
        market_data = self.market.get_indicators(symbol)
        current_price = market_data.get("price", 0)

        if current_price == 0:
            logger.warning("Could not get price for %s. Skipping.", symbol)
            return None

        # ── Step 1: manage existing position if any ────────────────────────
        positions = self.broker.get_positions()
        if symbol in positions:
            return self._manage_open_position(symbol, positions[symbol], current_price, market_data, portfolio)

        # ── Step 2: look for a new entry ───────────────────────────────────
        if portfolio.get("open_positions", 0) >= config.MAX_OPEN_POSITIONS:
            logger.info("Max open positions (%d) reached. No new entries.", config.MAX_OPEN_POSITIONS)
            return None

        return self._consider_entry(symbol, current_price, market_data, portfolio)

    # ── Position management ────────────────────────────────────────────────────

    def _manage_open_position(self, symbol: str, position: dict, current_price: float,
                               market_data: dict, portfolio: dict) -> Optional[dict]:
        """Decide what to do with an existing open position."""
        avg_price = position.get("avg_price", current_price)
        quantity = position.get("quantity", 0)
        pnl_pct = ((current_price - avg_price) / avg_price) * 100 if avg_price else 0

        logger.info(
            "Open position: %s | qty=%d | avg=₹%.2f | now=₹%.2f | P&L=%.2f%%",
            symbol, quantity, avg_price, current_price, pnl_pct
        )

        # ── Hard stop-loss ─────────────────────────────────────────────────
        if pnl_pct <= -config.STOP_LOSS_PCT:
            logger.warning("STOP-LOSS triggered for %s (%.2f%%). Force selling.", symbol, pnl_pct)
            reason = f"Stop-loss triggered at {pnl_pct:.2f}% loss (threshold: -{config.STOP_LOSS_PCT}%)"
            decision = {
                "action": "SELL", "symbol": symbol, "quantity": quantity,
                "confidence": 1.0, "reasoning": reason, "risk_level": "HIGH",
            }
            return self._execute(decision, market_data)

        # ── Take-profit threshold reached — ask Claude ─────────────────────
        if pnl_pct >= config.TAKE_PROFIT_PCT:
            logger.info("Take-profit threshold reached for %s (%.2f%%). Asking Claude.", symbol, pnl_pct)

        # Ask Claude with full context including current unrealized P&L
        trade_history = self.memory.get_trades_for_symbol(symbol)
        enriched_market = {
            **market_data,
            "open_position": {
                "quantity": quantity,
                "avg_buy_price": avg_price,
                "current_price": current_price,
                "unrealized_pnl_pct": round(pnl_pct, 2),
                "unrealized_pnl_inr": round((current_price - avg_price) * quantity, 2),
            }
        }
        decision = self.brain.analyze(symbol, enriched_market, trade_history, portfolio)

        if decision["action"] == "SELL" and decision.get("confidence", 0) >= 0.6:
            decision["quantity"] = quantity  # always close full position
            return self._execute(decision, market_data)

        logger.info("Holding %s — Claude says %s (confidence %.0f%%)",
                    symbol, decision["action"], decision.get("confidence", 0) * 100)
        return None

    # ── Entry logic ────────────────────────────────────────────────────────────

    def _consider_entry(self, symbol: str, current_price: float,
                         market_data: dict, portfolio: dict) -> Optional[dict]:
        """Ask Claude whether to open a new position."""
        trade_history = self.memory.get_trades_for_symbol(symbol)
        decision = self.brain.analyze(symbol, market_data, trade_history, portfolio)

        if decision["action"] == "BUY" and decision.get("confidence", 0) >= 0.6:
            return self._execute(decision, market_data)

        logger.info("No entry for %s — Claude says %s (confidence %.0f%%)",
                    symbol, decision["action"], decision.get("confidence", 0) * 100)
        return None

    # ── Order execution ────────────────────────────────────────────────────────

    def _execute(self, decision: dict, market_data: dict) -> dict:
        symbol = decision["symbol"]
        side = decision["action"]
        quantity = max(1, decision.get("quantity", 1))

        # Cap quantity by max trade amount
        price = market_data.get("price", 1)
        if side == "BUY" and price > 0:
            max_qty = int(config.MAX_TRADE_AMOUNT / price)
            quantity = min(quantity, max(1, max_qty))

        order = self.broker.place_order(symbol, side, quantity)

        if order.get("status") in ("COMPLETE", "PLACED"):
            self.memory.log_trade(order, decision)
            broken = self.records.check_trade(order)
            if broken:
                logger.info("ALL-TIME RECORDS BROKEN: %s", broken)

        return order

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

        logger.info("EOD Review complete. Records broken today: %s", broken_records)
        logger.info(review)
        return {"date": today, "trades": len(trades), "broken_records": broken_records, "review": review}
