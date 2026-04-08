import logging
from typing import Optional
import config
from core.utils import now_ist

logger = logging.getLogger(__name__)

_BROKER_INSTANCE = None
_BROKER_MODE = None


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
        paper_order_id = f"PAPER-{now_ist().strftime('%Y%m%d%H%M%S%f')}-{len(self.orders)+1:03d}"
        order = {
            "order_id": paper_order_id,
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

    def preflight_order(self, **kwargs) -> dict:
        return {
            "ok": True,
            "mode": "paper",
            "checks": [{"name": "paper_mode", "ok": True, "detail": "Paper broker accepts simulated orders"}],
            "resolved": kwargs,
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

    def preflight_order(self, symbol: str, side: str, quantity: int,
                        order_type: str = "MARKET", price: float = 0,
                        exchange: str = "NFO", product: str = "MIS",
                        variety: str = "regular", validity: str = "DAY",
                        trigger_price: float | None = None, tag: str | None = None,
                        tradingsymbol: str | None = None, option_type: str | None = None,
                        strike: int | None = None, expiry=None, log_failures: bool = False) -> dict:
        from core.zerodha_error_log import log_error as _log_err
        report = {
            "ok": False,
            "mode": "live",
            "timestamp": now_ist().isoformat(),
            "checks": [],
            "resolved": {
                "underlying": symbol,
                "exchange": exchange,
                "product": product,
                "order_type": order_type,
                "transaction_type": side,
                "quantity": quantity,
            },
        }

        def add_check(name: str, ok: bool, detail: str, critical: bool = True):
            report["checks"].append({"name": name, "ok": ok, "detail": detail, "critical": critical})

        try:
            profile = self.kite.profile()
            add_check("session", True, f"Logged in as {profile.get('user_name') or profile.get('user_id') or 'user'}")
        except Exception as e:
            reason = f"Kite session invalid: {e}"
            add_check("session", False, reason)
            report["error"] = reason
            if log_failures:
                _log_err("live_order_preflight", reason, symbol=symbol, detail=exchange)
            return report

        lot = config.LOT_SIZES.get(symbol, 1) if exchange == "NFO" else 1
        lot_ok = quantity > 0 and quantity % lot == 0
        add_check("lot_size", lot_ok, f"Quantity {quantity} {'matches' if lot_ok else 'must be a multiple of'} lot size {lot}")

        balance = self.get_portfolio_summary().get("balance", 0)
        add_check("cash_balance", balance > 0, f"Available cash ₹{balance:,.2f}")

        resolved_symbol = tradingsymbol
        resolved_price = float(price or 0)
        resolved_expiry = expiry
        resolved_option_type = option_type
        resolved_strike = strike

        try:
            if exchange == "NFO":
                from data.zerodha_fetcher import ZerodhaFetcher
                spot = self.get_quote(symbol).get("last_price", 0)
                add_check("spot_quote", spot > 0, f"NIFTY spot ₹{spot:,.2f}" if spot else "Underlying spot quote unavailable")

                resolved_option_type = option_type or ("PE" if side == "SELL" else "CE")
                resolved_strike = int(strike or round(spot / 50) * 50) if spot else int(strike or 0)
                resolved_expiry = expiry or ZerodhaFetcher.nearest_weekly_expiry()

                if not resolved_symbol and resolved_strike and resolved_option_type:
                    ts, ltp = ZerodhaFetcher.get().get_option_ltp(symbol, resolved_strike, resolved_option_type, resolved_expiry)
                    resolved_symbol = ts
                    resolved_price = resolved_price or float(ltp or 0)

                add_check(
                    "contract",
                    bool(resolved_symbol),
                    f"Resolved {resolved_symbol or 'no tradingsymbol'} for {symbol} {resolved_strike}{resolved_option_type or ''} exp {resolved_expiry}",
                )
                add_check(
                    "option_ltp",
                    resolved_price > 0,
                    f"Option LTP ₹{resolved_price:,.2f}" if resolved_price > 0 else "Option LTP unavailable",
                )
            else:
                if not resolved_symbol:
                    resolved_symbol = symbol
                if resolved_price <= 0:
                    resolved_price = self.get_quote(symbol).get("last_price", 0)
                add_check("quote", resolved_price > 0, f"Spot/equity price ₹{resolved_price:,.2f}" if resolved_price > 0 else "Quote unavailable")
        except Exception as e:
            add_check("contract_resolution", False, f"Failed to resolve tradingsymbol/LTP: {e}")

        report["resolved"].update({
            "tradingsymbol": resolved_symbol,
            "option_type": resolved_option_type,
            "strike": resolved_strike,
            "expiry": resolved_expiry.isoformat() if hasattr(resolved_expiry, "isoformat") else resolved_expiry,
            "price": resolved_price,
        })

        if resolved_symbol:
            payload = [{
                "exchange": exchange,
                "tradingsymbol": resolved_symbol,
                "transaction_type": side,
                "variety": variety,
                "product": product,
                "order_type": order_type,
                "quantity": quantity,
                "price": resolved_price if order_type == "LIMIT" else None,
                "trigger_price": trigger_price,
            }]
            payload[0] = {k: v for k, v in payload[0].items() if v is not None}
            try:
                margins = self.kite.order_margins(payload)
                margin_row = margins[0] if isinstance(margins, list) and margins else {}
                margin_required = float(
                    margin_row.get("total")
                    or margin_row.get("final", {}).get("total", 0)
                    or 0
                )
                report["resolved"]["margin_required"] = margin_required
                add_check(
                    "margins",
                    margin_required <= balance if margin_required else True,
                    f"Margin ₹{margin_required:,.2f} vs cash ₹{balance:,.2f}",
                )
            except Exception as e:
                add_check("margins", False, f"Margin check failed: {e}")

        report["ok"] = all(c["ok"] for c in report["checks"] if c.get("critical", True))
        if not report["ok"]:
            first_bad = next((c["detail"] for c in report["checks"] if c.get("critical", True) and not c["ok"]), "Live preflight failed")
            report["error"] = first_bad
            if log_failures:
                _log_err("live_order_preflight", first_bad, symbol=symbol, detail=report["resolved"].get("tradingsymbol") or "")
        return report

    def place_order(self, symbol: str, side: str, quantity: int,
                    order_type: str = "MARKET", price: float = 0,
                    exchange: str = "NFO", product: str = "MIS",
                    variety: str = "regular", validity: str = "DAY",
                    trigger_price: float | None = None, tag: str | None = None) -> dict:
        """Place an order on Kite.

        Defaults: exchange=NFO, product=MIS (intraday options/futures).
        Pass exchange="NSE", product="CNC" for equity delivery orders.
        """
        from core.zerodha_error_log import log_error as _log_err
        try:
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
        except Exception as e:
            reason = str(e)
            logger.error("[LIVE/KITE] order rejected for %s: %s", symbol, reason)
            _log_err("live_order_rejected", reason, symbol=symbol, detail=f"{exchange}:{product}")
            return {
                "order_id": None, "symbol": symbol, "side": side, "quantity": quantity, "status": "REJECTED",
                "exchange": exchange, "product": product, "order_type": order_type,
                "variety": variety, "validity": validity, "trigger_price": trigger_price, "tag": tag,
                "reason": reason,
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
    global _BROKER_INSTANCE, _BROKER_MODE
    mode = "paper" if config.IS_PAPER else "live"
    if _BROKER_INSTANCE is None or _BROKER_MODE != mode:
        _BROKER_INSTANCE = MockBroker() if config.IS_PAPER else KiteBroker()
        _BROKER_MODE = mode
    return _BROKER_INSTANCE
