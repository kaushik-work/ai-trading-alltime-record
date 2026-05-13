import logging
import time as _time
from typing import Optional
import config
from core.utils import now_ist

_portfolio_cache: dict = {"result": None, "ts": 0.0}
_PORTFOLIO_CACHE_TTL = 60  # seconds
_positions_cache: dict = {"result": {}, "ts": 0.0}
_POSITIONS_CACHE_TTL = 30  # seconds — avoids hitting Angel One rate limit

logger = logging.getLogger(__name__)

_BROKER_INSTANCE = None

# Angel One product type mapping
_PRODUCT_MAP = {
    "MIS":      "INTRADAY",
    "NRML":     "CARRYFORWARD",
    "CNC":      "DELIVERY",
    "INTRADAY": "INTRADAY",
    "CARRYFORWARD": "CARRYFORWARD",
    "DELIVERY": "DELIVERY",
}

# Angel One variety mapping
_VARIETY_MAP = {
    "regular":  "NORMAL",
    "amo":      "AMO",
    "stoploss": "STOPLOSS",
    "NORMAL":   "NORMAL",
    "AMO":      "AMO",
    "STOPLOSS": "STOPLOSS",
}

# Angel One order type mapping
_ORDER_TYPE_MAP = {
    "MARKET":          "MARKET",
    "LIMIT":           "LIMIT",
    "SL-M":            "STOPLOSS_MARKET",
    "SL":              "STOPLOSS_LIMIT",
    "STOPLOSS_MARKET": "STOPLOSS_MARKET",
    "STOPLOSS_LIMIT":  "STOPLOSS_LIMIT",
}


# MockBroker (paper-trading simulator) removed entirely. The bot is
# live-trading-only via AngelOneBroker. If Angel One credentials are
# missing or the session fails to establish, AngelOneBroker.__init__
# raises RuntimeError and the bot refuses to start — no silent
# fallback to simulation.


def _extract_order_id(resp) -> str | None:
    """Pull the order-id out of Angel One placeOrder's response, regardless
    of shape. smartapi-python returns different structures across versions:

        1. {"status": True, "data": {"orderid": "NXXX"}}      ← canonical
        2. {"status": True, "data": "NXXX"}                   ← string in data
        3. "NXXX"                                              ← raw string
        4. {"status": True, "orderid": "NXXX"}                ← top-level

    Returns the orderid string if any of those shapes matches, else None.
    Critical: an early version of this code only handled shape (1) and
    treated the rest as "silent rejection", which threw away real successful
    orders and prevented downstream SL/TP placement.
    """
    # Shape 3: raw string return
    if isinstance(resp, str):
        return resp.strip() or None
    if not isinstance(resp, dict):
        return None
    # Shape 4: orderid at top level
    top_oid = resp.get("orderid") or resp.get("orderId") or resp.get("uniqueorderid")
    if isinstance(top_oid, str) and top_oid.strip():
        return top_oid.strip()
    data = resp.get("data")
    # Shape 2: data is the orderid string directly
    if isinstance(data, str):
        return data.strip() or None
    # Shape 1: data is a dict with orderid inside
    if isinstance(data, dict):
        oid = data.get("orderid") or data.get("orderId") or data.get("uniqueorderid")
        if isinstance(oid, str) and oid.strip():
            return oid.strip()
    return None


class AngelOneBroker:
    """Live broker using Angel One SmartAPI (https://smartapi.angelbroking.com/)."""

    def __init__(self):
        from data.angel_fetcher import AngelFetcher
        self._fetcher = AngelFetcher.get()
        self._last_positions_ok = False  # True after a successful get_positions call
        if not self._fetcher._ensure_logged_in():
            raise RuntimeError(
                "Angel One login failed. Check ANGEL_API_KEY / ANGEL_CLIENT_ID / "
                "ANGEL_PASSWORD / ANGEL_TOTP_TOKEN in .env"
            )
        logger.info("Angel One broker connected.")

    @property
    def _api(self):
        return self._fetcher._api

    def _angel_product(self, product: str) -> str:
        return _PRODUCT_MAP.get(product.upper(), "INTRADAY")

    def _angel_variety(self, variety: str) -> str:
        return _VARIETY_MAP.get(variety, "NORMAL")

    def _angel_order_type(self, order_type: str) -> str:
        return _ORDER_TYPE_MAP.get(order_type, order_type)

    def get_quote(self, symbol: str) -> dict:
        price = self._fetcher.get_index_ltp(symbol)
        return {
            "symbol": symbol,
            "last_price": price or 0,
            "timestamp": now_ist().isoformat(),
        }

    def preflight_order(self, symbol: str, side: str, quantity: int,
                        order_type: str = "MARKET", price: float = 0,
                        exchange: str = "NFO", product: str = "MIS",
                        variety: str = "regular", validity: str = "DAY",
                        trigger_price: float | None = None, tag: str | None = None,
                        tradingsymbol: str | None = None, option_type: str | None = None,
                        strike: int | None = None, expiry=None, log_failures: bool = False) -> dict:
        from core.angel_error_log import log_error as _log_err
        report = {
            "ok": False,
            "mode": "live",
            "timestamp": now_ist().isoformat(),
            "checks": [],
            "resolved": {
                "underlying": symbol,
                "exchange": exchange,
                "product": self._angel_product(product),
                "order_type": order_type,
                "transaction_type": side,
                "quantity": quantity,
            },
        }

        def add_check(name: str, ok: bool, detail: str, critical: bool = True):
            report["checks"].append({"name": name, "ok": ok, "detail": detail, "critical": critical})

        try:
            from data.angel_fetcher import AngelFetcher
            creds = AngelFetcher._read_env()
            self._fetcher._api.getProfile(creds.get("refresh_token", ""))
            add_check("session", True, f"Logged in as {config.ANGEL_CLIENT_ID or 'user'}")
        except Exception as e:
            reason = f"Angel One session invalid: {e}"
            add_check("session", False, reason)
            report["error"] = reason
            if log_failures:
                _log_err("live_order_preflight", reason, symbol=symbol, detail=exchange)
            return report

        lot = config.LOT_SIZES.get(symbol, 1) if exchange == "NFO" else 1
        lot_ok = quantity > 0 and quantity % lot == 0
        add_check("lot_size", lot_ok, f"Quantity {quantity} {'matches' if lot_ok else 'must be multiple of'} lot size {lot}")

        # Cash balance is CRITICAL: if available margin is below an estimated
        # need for this trade, Angel One will silently reject the order
        # ("order placed but no orderid returned"). Better to fail preflight
        # here than to let the bot fire-and-virtual-SL all day.
        # Estimated need = price × quantity (premium paid). Add a small buffer
        # for charges/slippage. INTRADAY (MIS) margin is roughly this much
        # for option buying — option selling needs ~10x more, but we only
        # buy options today.
        balance = self.get_portfolio_summary().get("balance", 0)
        est_need = float(price or 0) * int(quantity or 0)
        if est_need <= 0:
            # No price → can't estimate need; fall back to a non-zero check
            add_check("cash_balance", balance > 0,
                      f"Available cash ₹{balance:,.2f}", critical=True)
        else:
            add_check(
                "cash_balance",
                balance >= est_need,
                f"Available cash ₹{balance:,.2f} vs required ~₹{est_need:,.0f}",
                critical=True,
            )

        resolved_symbol = tradingsymbol
        resolved_price = float(price or 0)
        resolved_expiry = expiry
        resolved_option_type = option_type
        resolved_strike = strike
        resolved_token = None

        try:
            if exchange == "NFO":
                from data.angel_fetcher import AngelFetcher
                spot = self.get_quote(symbol).get("last_price", 0)
                add_check("spot_quote", spot > 0, f"NIFTY spot ₹{spot:,.2f}" if spot else "Spot quote unavailable")

                resolved_option_type = option_type or ("PE" if side == "SELL" else "CE")
                resolved_strike = int(strike or round(spot / 50) * 50) if spot else int(strike or 0)
                resolved_expiry = expiry or AngelFetcher.nearest_weekly_expiry()

                if not resolved_symbol and resolved_strike and resolved_option_type:
                    ts, ltp = AngelFetcher.get().get_option_ltp(symbol, resolved_strike, resolved_option_type, resolved_expiry)
                    resolved_symbol = ts
                    resolved_price = resolved_price or float(ltp or 0)

                if resolved_symbol:
                    resolved_token = AngelFetcher.get().get_option_token(resolved_symbol)

                add_check("contract", bool(resolved_symbol),
                          f"Resolved {resolved_symbol or 'no tradingsymbol'} for {symbol} {resolved_strike}{resolved_option_type or ''} exp {resolved_expiry}")
                add_check("option_ltp", resolved_price > 0,
                          f"Option LTP ₹{resolved_price:,.2f}" if resolved_price > 0 else "Option LTP unavailable")
            else:
                if not resolved_symbol:
                    resolved_symbol = symbol
                if resolved_price <= 0:
                    resolved_price = self.get_quote(symbol).get("last_price", 0)
                add_check("quote", resolved_price > 0, f"Price ₹{resolved_price:,.2f}" if resolved_price > 0 else "Quote unavailable")
        except Exception as e:
            add_check("contract_resolution", False, f"Failed to resolve tradingsymbol/LTP: {e}")

        report["resolved"].update({
            "tradingsymbol": resolved_symbol,
            "symboltoken": resolved_token,
            "option_type": resolved_option_type,
            "strike": resolved_strike,
            "expiry": resolved_expiry.isoformat() if hasattr(resolved_expiry, "isoformat") else resolved_expiry,
            "price": resolved_price,
        })

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
        from core.angel_error_log import log_error as _log_err
        try:
            # Look up Angel One symboltoken (required for all orders)
            from data.angel_fetcher import AngelFetcher
            token = AngelFetcher.get().get_option_token(symbol)
            if not token and exchange == "NFO":
                raise RuntimeError(f"Angel One symboltoken not found for {symbol} — is it in the instrument master?")

            angel_order_type = self._angel_order_type(order_type)
            is_limit_type = angel_order_type in ("LIMIT", "STOPLOSS_LIMIT")
            params = {
                "variety":         self._angel_variety(variety),
                "tradingsymbol":   symbol,
                "symboltoken":     token or "",
                "transactiontype": side,
                "exchange":        exchange,
                "ordertype":       angel_order_type,
                "producttype":     self._angel_product(product),
                "duration":        validity,
                "price":           str(price) if is_limit_type else "0",
                "quantity":        str(quantity),
            }
            if trigger_price:
                params["triggerprice"] = str(trigger_price)

            resp = self._api.placeOrder(params)

            # ── Robust orderid extraction ─────────────────────────────────────
            # smartapi-python's placeOrder returns the orderid in different
            # shapes across versions / endpoints — we've observed:
            #   1. {"status": True, "data": {"orderid": "NXXX"}}        ← dict
            #   2. {"status": True, "data": "NXXX"}                     ← string in data
            #   3. "NXXX"                                                ← raw string return
            #   4. {"status": True, "orderid": "NXXX"}                  ← top-level
            # Previous code only handled (1) and treated (2)–(4) as "silent
            # rejection", causing real successful orders to be marked REJECTED
            # in our DB and preventing the SL/TP from being placed downstream.
            order_id = _extract_order_id(resp)

            if not order_id:
                # No orderid in ANY known shape — log the raw response so we
                # can spot new shapes Angel One introduces in future SDK versions.
                msg = ""
                if isinstance(resp, dict):
                    msg = resp.get("message") or resp.get("errorcode") or ""
                logger.error("[LIVE/ANGEL] could not parse orderid from response: %r", resp)
                raise RuntimeError(msg or f"order placed but no orderid returned (raw: {resp!r})")
            logger.info("[LIVE/ANGEL] %s %d %s@%s | Order ID: %s", side, quantity, symbol, exchange, order_id)
            return {
                "order_id": order_id, "symbol": symbol, "side": side, "quantity": quantity,
                "status": "PLACED", "exchange": exchange, "product": product,
                "order_type": order_type, "variety": variety, "validity": validity,
                "trigger_price": trigger_price, "tag": tag,
            }
        except Exception as e:
            reason = str(e)
            logger.error("[LIVE/ANGEL] order rejected for %s: %s", symbol, reason)
            _log_err("live_order_rejected", reason, symbol=symbol, detail=f"{exchange}:{product}")
            return {
                "order_id": None, "symbol": symbol, "side": side, "quantity": quantity,
                "status": "REJECTED", "exchange": exchange, "product": product,
                "order_type": order_type, "variety": variety, "validity": validity,
                "trigger_price": trigger_price, "tag": tag, "reason": reason,
            }

    def invalidate_positions_cache(self):
        """Force next get_positions() to hit Angel One API — call after any exit
        so the position guardian doesn't see a stale position and double-sell."""
        _positions_cache["ts"] = 0.0

    def get_fill_price(self, order_id: str, retries: int = 3, delay: float = 1.5) -> float | None:
        """Fetch actual average fill price from Angel One order book after a MARKET fill."""
        import time as _t
        for attempt in range(retries):
            try:
                resp = self._api.orderBook()
                if resp and resp.get("data"):
                    for o in resp["data"]:
                        if str(o.get("orderid")) == str(order_id):
                            status = (o.get("status") or "").upper()
                            avg = float(o.get("averageprice") or 0)
                            if status == "COMPLETE" and avg > 0:
                                logger.info("[LIVE/ANGEL] Fill price order %s: ₹%.2f", order_id, avg)
                                return avg
            except Exception as e:
                logger.debug("get_fill_price attempt %d: %s", attempt + 1, e)
            if attempt < retries - 1:
                _t.sleep(delay)
        return None

    def cancel_order(self, order_id: str, variety: str = "regular") -> bool:
        from core.angel_error_log import log_error as _log_err
        try:
            self._api.cancelOrder(order_id=order_id, variety=self._angel_variety(variety))
            logger.info("[LIVE/ANGEL] Cancelled order %s", order_id)
            return True
        except Exception as e:
            logger.error("[LIVE/ANGEL] Cancel order %s failed: %s", order_id, e)
            _log_err("cancel_order", str(e), detail=order_id)
            return False

    def get_positions(self) -> dict:
        if _time.time() - _positions_cache["ts"] < _POSITIONS_CACHE_TTL:
            return _positions_cache["result"]
        try:
            if self._api is None:
                self._last_positions_ok = False
                return {}
            resp = self._api.position()
            if resp and resp.get("status") and resp.get("data"):
                result = {}
                for p in resp["data"]:
                    qty = int(p.get("netqty", 0))
                    if qty == 0:
                        continue
                    result[p["tradingsymbol"]] = {
                        **p,
                        "quantity": qty,
                        "avg_price": float(p.get("averageprice", 0) or 0),
                    }
                _positions_cache["result"] = result
                _positions_cache["ts"] = _time.time()
                self._last_positions_ok = True
                return result
            # Empty/null data but valid response → no open positions
            if resp and resp.get("status"):
                _positions_cache["result"] = {}
                _positions_cache["ts"] = _time.time()
                self._last_positions_ok = True
                return {}
        except Exception as e:
            msg = str(e)
            logger.error("AngelOneBroker.get_positions: %s", msg)
            from core.angel_error_log import log_error as _log_err
            _log_err("get_positions", msg)
            # Angel One returns HTML when session expires — smartapi raises JSON parse error
            if "parse" in msg.lower() or "json" in msg.lower() or "Unauthorized" in msg or "AG8001" in msg:
                self._fetcher._invalidate_token()
        self._last_positions_ok = False
        return {}

    def get_portfolio_summary(self) -> dict:
        if _time.time() - _portfolio_cache["ts"] < _PORTFOLIO_CACHE_TTL and _portfolio_cache["result"]:
            return _portfolio_cache["result"]
        try:
            if self._api is None:
                return {"balance": 0, "pnl": 0, "open_positions": 0}
            rms = self._api.rmsLimit()
            if rms and rms.get("status") and rms.get("data"):
                d = rms["data"]
                net            = float(d.get("net", 0) or 0)
                available_cash = float(d.get("availablecash", 0) or 0)
                # CRITICAL: balance used for trade decisions (preflight cash check,
                # _calc_quantity position sizing) MUST be availablecash, not net.
                # net includes pledged collateral, MTM PnL of holdings, unsettled
                # funds and other locked amounts that Angel One won't let us spend
                # on a fresh MIS option BUY. Fall back to net only if Angel One
                # didn't return availablecash at all (very rare; usually 0 is the
                # accurate answer in that case anyway).
                balance = available_cash if available_cash > 0 else net
                positions = self.get_positions()
                pnl = sum(float(p.get("unrealised", 0) or 0) for p in positions.values())
                result = {
                    "balance":        round(balance, 2),
                    "available_cash": round(available_cash, 2),
                    "net":            round(net, 2),
                    "pnl":            round(pnl, 2),
                    "open_positions": len(positions),
                }
                _portfolio_cache["result"] = result
                _portfolio_cache["ts"] = _time.time()
                return result
        except Exception as e:
            logger.error("AngelOneBroker.get_portfolio_summary: %s", e)
            from core.angel_error_log import log_error as _log_err
            _log_err("get_portfolio_summary", str(e))
        return {"balance": 0, "pnl": 0, "open_positions": 0}

    def get_unrealized_pnl_pct(self, symbol: str, current_price: float) -> float:
        positions = self.get_positions()
        pos = positions.get(symbol)
        if not pos:
            return 0.0
        avg = float(pos.get("averageprice", 0) or 0)
        if avg == 0:
            return 0.0
        return ((current_price - avg) / avg) * 100


def get_broker():
    """Return the live Angel One broker singleton. There is no paper variant."""
    global _BROKER_INSTANCE
    if _BROKER_INSTANCE is None:
        _BROKER_INSTANCE = AngelOneBroker()
    return _BROKER_INSTANCE
