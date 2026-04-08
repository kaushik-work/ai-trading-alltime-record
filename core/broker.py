import logging
from typing import Optional
import config
from core.utils import now_ist

logger = logging.getLogger(__name__)


class MockBroker:
    """Paper trading broker — simulates order execution without real money."""

    def __init__(self):
        self.positions = {}
        self.orders = []
        self.balance = float(config.STARTING_BUDGET)
        self.pnl = 0.0
        logger.info("Paper trading mode active. Virtual balance: ₹%.2f", self.balance)

    def get_quote(self, symbol: str) -> dict:
        """Fetch live quote from Zerodha even in paper mode — no mock prices."""
        from data.zerodha_fetcher import ZerodhaFetcher
        try:
            ltp_data = ZerodhaFetcher.get()._broker.ltp([f"NSE:{symbol}"])
            price = float(ltp_data.get(f"NSE:{symbol}", {}).get("last_price", 0))
            if price > 0:
                return {"symbol": symbol, "last_price": price, "timestamp": now_ist().isoformat()}
            from core.zerodha_error_log import log_error as _log_err
            _log_err("get_quote", "LTP returned 0", symbol=symbol)
        except Exception as e:
            logger.warning("MockBroker.get_quote: Zerodha LTP failed for %s: %s", symbol, e)
            from core.zerodha_error_log import log_error as _log_err
            _log_err("get_quote", str(e), symbol=symbol)
        return {"symbol": symbol, "last_price": 0, "timestamp": now_ist().isoformat()}

    def place_order(self, symbol: str, side: str, quantity: int,
                    order_type: str = "MARKET", price: float = 0,
                    exchange: str = "NFO", product: str = "MIS",
                    variety: str = "regular", validity: str = "DAY",
                    trigger_price: float | None = None, tag: str | None = None) -> dict:
        order = {
            "order_id": f"PAPER-{len(self.orders)+1:04d}",
            "symbol": symbol,
            "side": side,  # BUY or SELL
            "quantity": quantity,
            "order_type": order_type,
            "exchange": exchange,
            "product": product,
            "variety": variety,
            "validity": validity,
            "trigger_price": trigger_price,
            "tag": tag,
            "price": price or self.get_quote(symbol)["last_price"],
            "status": "COMPLETE",
            "timestamp": now_ist().isoformat(),
        }
        self.orders.append(order)

        filled_price = order["price"]

        if side == "BUY":
            # For options paper trading: cap cost at MAX_TRADE_AMOUNT (simulates premium outlay).
            # Using spot price × quantity (≈₹4M notional) would always reject — options cost is premium only.
            premium_cost = min(filled_price * quantity, float(config.MAX_TRADE_AMOUNT))
            if premium_cost > self.balance:
                order["status"] = "REJECTED"
                logger.warning("Order rejected: insufficient balance for %s (need ₹%.0f, have ₹%.0f)",
                               symbol, premium_cost, self.balance)
                return order
            self.balance -= premium_cost
            self.positions[symbol] = self.positions.get(symbol, {
                "quantity": 0, "avg_price": 0, "premium_cost": 0,
                "exchange": exchange, "product": product,
            })
            pos = self.positions[symbol]
            total_qty = pos["quantity"] + quantity
            pos["avg_price"] = ((pos["avg_price"] * pos["quantity"]) + filled_price * quantity) / total_qty
            pos["quantity"] = total_qty
            pos["premium_cost"] = pos.get("premium_cost", 0) + premium_cost

        elif side == "SELL":
            pos = self.positions.get(symbol)
            if not pos or pos["quantity"] < quantity:
                order["status"] = "REJECTED"
                logger.warning("Order rejected: insufficient position for %s", symbol)
                return order
            pnl = (filled_price - pos["avg_price"]) * quantity
            self.pnl += pnl
            # Restore the original premium paid, adjusted for partial closes
            close_ratio = quantity / pos["quantity"]
            returned_premium = pos.get("premium_cost", 0) * close_ratio
            self.balance += returned_premium + pnl
            order["pnl"] = round(pnl, 2)
            order["avg_buy_price"] = pos["avg_price"]
            pos["quantity"] -= quantity
            pos["premium_cost"] = pos.get("premium_cost", 0) - returned_premium
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


class KiteBroker:
    """Live broker using Zerodha Kite Connect API (https://kite.trade/docs/connect/v3/)."""

    def __init__(self):
        try:
            from kiteconnect import KiteConnect
            if not config.ZERODHA_API_KEY or not config.ZERODHA_ACCESS_TOKEN:
                raise RuntimeError(
                    "ZERODHA_API_KEY or ZERODHA_ACCESS_TOKEN not set. "
                    "Run scripts/get_token.py to generate today's token."
                )
            self.kite = KiteConnect(api_key=config.ZERODHA_API_KEY)
            self.kite.set_access_token(config.ZERODHA_ACCESS_TOKEN)
            logger.info("Kite Connect broker connected successfully.")
        except ImportError:
            raise RuntimeError("kiteconnect not installed. Run: pip install kiteconnect")
        except Exception as e:
            raise RuntimeError(f"Kite Connect connection failed: {e}")

    def get_quote(self, symbol: str) -> dict:
        data = self.kite.quote([f"NSE:{symbol}"])
        instrument = data.get(f"NSE:{symbol}", {})
        return {
            "symbol": symbol,
            "last_price": instrument.get("last_price", 0),
            "timestamp": now_ist().isoformat(),
        }

    def place_order(self, symbol: str, side: str, quantity: int,
                    order_type: str = "MARKET", price: float = 0,
                    exchange: str = "NFO", product: str = "MIS",
                    variety: str = "regular", validity: str = "DAY",
                    trigger_price: float | None = None, tag: str | None = None) -> dict:
        """Place an order on Kite.

        Defaults: exchange=NFO, product=MIS (intraday options/futures).
        Pass exchange="NSE", product="CNC" for equity delivery orders.
        """
        order_id = self.kite.place_order(
            variety=variety,
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=side,
            quantity=quantity,
            product=product,
            order_type=order_type,
            price=price if order_type == "LIMIT" else None,
            validity=validity,
            trigger_price=trigger_price,
            tag=tag,
        )
        logger.info("[LIVE/KITE] %s %d %s@%s | Order ID: %s", side, quantity, symbol, exchange, order_id)
        return {
            "order_id": order_id, "symbol": symbol, "side": side, "quantity": quantity, "status": "PLACED",
            "exchange": exchange, "product": product, "order_type": order_type,
            "variety": variety, "validity": validity, "trigger_price": trigger_price, "tag": tag,
        }

    def get_positions(self) -> dict:
        positions = self.kite.positions()
        return {p["tradingsymbol"]: p for p in positions.get("net", []) if p.get("quantity", 0) != 0}

    def get_portfolio_summary(self) -> dict:
        margins = self.kite.margins()
        equity = margins.get("equity", {})
        return {
            "balance": equity.get("available", {}).get("cash", 0),
            "pnl": sum(p.get("pnl", 0) for p in self.kite.positions().get("net", [])),
            "open_positions": len(self.get_positions()),
        }


def get_broker():
    """Factory — returns paper or live broker based on config."""
    if config.IS_PAPER:
        return MockBroker()
    else:
        return KiteBroker()
