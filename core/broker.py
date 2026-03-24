import logging
from datetime import datetime
from typing import Optional
import config

logger = logging.getLogger(__name__)


class MockBroker:
    """Paper trading broker — simulates order execution without real money."""

    def __init__(self):
        self.positions = {}
        self.orders = []
        self.balance = 100000.0  # virtual ₹1,00,000
        self.pnl = 0.0
        logger.info("Paper trading mode active. Virtual balance: ₹%.2f", self.balance)

    def get_quote(self, symbol: str) -> dict:
        """Returns a mock quote — replace with real data feed in live mode."""
        import random
        base_prices = {
            "RELIANCE": 2500, "TCS": 3800, "INFY": 1700,
            "HDFCBANK": 1600, "ICICIBANK": 1100,
            "NIFTY": 22000, "BANKNIFTY": 48000,
        }
        price = base_prices.get(symbol, 1000)
        price += random.uniform(-price * 0.01, price * 0.01)
        return {"symbol": symbol, "last_price": round(price, 2), "timestamp": datetime.now().isoformat()}

    def place_order(self, symbol: str, side: str, quantity: int, order_type: str = "MARKET", price: float = 0) -> dict:
        order = {
            "order_id": f"PAPER-{len(self.orders)+1:04d}",
            "symbol": symbol,
            "side": side,  # BUY or SELL
            "quantity": quantity,
            "order_type": order_type,
            "price": price or self.get_quote(symbol)["last_price"],
            "status": "COMPLETE",
            "timestamp": datetime.now().isoformat(),
        }
        self.orders.append(order)

        filled_price = order["price"]
        cost = filled_price * quantity

        if side == "BUY":
            if cost > self.balance:
                order["status"] = "REJECTED"
                logger.warning("Order rejected: insufficient balance for %s", symbol)
                return order
            self.balance -= cost
            self.positions[symbol] = self.positions.get(symbol, {"quantity": 0, "avg_price": 0})
            pos = self.positions[symbol]
            total_qty = pos["quantity"] + quantity
            pos["avg_price"] = ((pos["avg_price"] * pos["quantity"]) + cost) / total_qty
            pos["quantity"] = total_qty

        elif side == "SELL":
            pos = self.positions.get(symbol)
            if not pos or pos["quantity"] < quantity:
                order["status"] = "REJECTED"
                logger.warning("Order rejected: insufficient position for %s", symbol)
                return order
            self.balance += cost
            pnl = (filled_price - pos["avg_price"]) * quantity
            self.pnl += pnl
            order["pnl"] = round(pnl, 2)
            order["avg_buy_price"] = pos["avg_price"]
            pos["quantity"] -= quantity
            if pos["quantity"] == 0:
                del self.positions[symbol]

        logger.info("[PAPER] %s %d %s @ ₹%.2f | Balance: ₹%.2f", side, quantity, symbol, filled_price, self.balance)
        return order

    def get_positions(self) -> dict:
        return self.positions

    def get_unrealized_pnl_pct(self, symbol: str, current_price: float) -> float:
        """Returns unrealized P&L % for an open position. 0 if no position."""
        pos = self.positions.get(symbol)
        if not pos or pos["avg_price"] == 0:
            return 0.0
        return ((current_price - pos["avg_price"]) / pos["avg_price"]) * 100

    def get_portfolio_summary(self) -> dict:
        return {
            "balance": round(self.balance, 2),
            "pnl": round(self.pnl, 2),
            "open_positions": len(self.positions),
            "total_orders": len(self.orders),
        }


class JugaadBroker:
    """Live broker using jugaad-trader (free, no API key needed for Zerodha)."""

    def __init__(self):
        try:
            from jugaad_trader import Zerodha
            self.broker = Zerodha(
                user_id=config.ZERODHA_USER_ID,
                password=config.ZERODHA_PASSWORD,
                twofa=config.ZERODHA_TOTP_SECRET,
            )
            self.broker.login()
            logger.info("Jugaad-trader (Zerodha) connected successfully.")
        except ImportError:
            raise RuntimeError("jugaad-trader not installed. Run: pip install jugaad-trader")
        except Exception as e:
            raise RuntimeError(f"Jugaad-trader connection failed: {e}")

    def get_quote(self, symbol: str) -> dict:
        data = self.broker.quote(f"NSE:{symbol}")
        instrument = data.get(f"NSE:{symbol}", {})
        return {
            "symbol": symbol,
            "last_price": instrument.get("last_price", 0),
            "timestamp": datetime.now().isoformat(),
        }

    def place_order(self, symbol: str, side: str, quantity: int, order_type: str = "MARKET", price: float = 0) -> dict:
        from jugaad_trader import Zerodha
        transaction = "BUY" if side == "BUY" else "SELL"
        order_id = self.broker.place_order(
            variety="regular",
            exchange="NSE",
            tradingsymbol=symbol,
            transaction_type=transaction,
            quantity=quantity,
            product="CNC",
            order_type=order_type,
            price=price if order_type == "LIMIT" else None,
        )
        logger.info("[LIVE/JUGAAD] %s %d %s | Order ID: %s", side, quantity, symbol, order_id)
        return {"order_id": order_id, "symbol": symbol, "side": side, "quantity": quantity, "status": "PLACED"}

    def get_positions(self) -> dict:
        positions = self.broker.positions()
        return {p["tradingsymbol"]: p for p in positions.get("net", [])}

    def get_portfolio_summary(self) -> dict:
        margins = self.broker.margins()
        equity = margins.get("equity", {})
        return {
            "balance": equity.get("available", {}).get("cash", 0),
            "pnl": sum(p.get("pnl", 0) for p in self.broker.positions().get("net", [])),
            "open_positions": len(self.get_positions()),
        }


def get_broker():
    """Factory — returns paper or live broker based on config."""
    if config.IS_PAPER:
        return MockBroker()
    else:
        return JugaadBroker()
