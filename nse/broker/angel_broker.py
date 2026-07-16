"""Angel One SmartAPI broker for NSE synthetic-forward combo orders.

Executes a synthetic forward as two separate option legs:
  long  synthetic → BUY CE + SELL PE
  short synthetic → SELL CE + BUY PE

Only live trading — no paper mode.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from data.angel_fetcher import AngelFetcher
from nse.config import EXCHANGE, LOT_SIZES, PRODUCT_TYPE
from nse.models import ComboLeg, Position, SyntheticForwardSignal

logger = logging.getLogger(__name__)


class AngelBroker:
    """Thin wrapper around AngelFetcher for live order placement."""

    def __init__(self, fetcher: Optional[AngelFetcher] = None):
        self.fetcher = fetcher or AngelFetcher.get()

    def _ensure_logged_in(self) -> bool:
        return self.fetcher._ensure_logged_in()

    @staticmethod
    def _extract_order_id(resp) -> Optional[str]:
        """Angel One placeOrder returns the orderid in many shapes."""
        if isinstance(resp, str):
            return resp.strip() or None
        if not isinstance(resp, dict):
            return None
        top = resp.get("orderid") or resp.get("orderId") or resp.get("uniqueorderid")
        if isinstance(top, str) and top.strip():
            return top.strip()
        data = resp.get("data")
        if isinstance(data, str):
            return data.strip() or None
        if isinstance(data, dict):
            oid = data.get("orderid") or data.get("orderId") or data.get("uniqueorderid")
            if isinstance(oid, str) and oid.strip():
                return oid.strip()
        return None

    def _build_order_payload(self, leg: ComboLeg, variety: str = "NORMAL",
                             limit_price: Optional[float] = None) -> dict:
        """Build Angel One placeOrder payload for an option leg.

        If limit_price is supplied the leg is placed as a LIMIT order to avoid
        entry slippage; otherwise it is a MARKET order.
        """
        if limit_price is not None and limit_price > 0:
            ordertype = "LIMIT"
            price = str(round(limit_price, 2))
        else:
            ordertype = "MARKET"
            price = "0"
        return {
            "variety": variety,
            "tradingsymbol": leg.tradingsymbol,
            "symboltoken": leg.token,
            "transactiontype": leg.side,
            "exchange": EXCHANGE.get(self._symbol_from_ts(leg.tradingsymbol), "NFO"),
            "ordertype": ordertype,
            "producttype": PRODUCT_TYPE,
            "duration": "DAY",
            "quantity": str(leg.lots * LOT_SIZES.get(self._symbol_from_ts(leg.tradingsymbol), 1)),
            "price": price,
            "squareoff": "0",
            "stoploss": "0",
            "triggerprice": "0",
        }

    @staticmethod
    def _symbol_from_ts(tradingsymbol: str) -> str:
        for sym in ("BANKNIFTY", "FINNIFTY", "NIFTY", "SENSEX"):
            if tradingsymbol.startswith(sym):
                return sym
        return "NIFTY"

    def get_combo_margin_required(self, legs: list[ComboLeg]) -> Optional[float]:
        """Query Angel One live margin API for the combo. Returns INR or None."""
        if not legs:
            return 0.0
        positions = []
        for leg in legs:
            sym = self._symbol_from_ts(leg.tradingsymbol)
            positions.append({
                "exchange": EXCHANGE.get(sym, "NFO"),
                "qty": leg.lots * LOT_SIZES.get(sym, 1),
                "price": 0,
                "productType": PRODUCT_TYPE,
                "orderType": "MARKET",
                "token": leg.token,
                "tradeType": leg.side,
            })
        return self.fetcher.get_margin_required(positions)

    def place_single_order(self, symbol: str, tradingsymbol: str, token: str,
                           option_type: str, side: str, lots: int,
                           limit_price: Optional[float] = None,
                           sl_points: Optional[float] = None,
                           target_points: Optional[float] = None) -> dict:
        """Place a single option leg order.

        If limit_price is provided the entry is placed as a LIMIT order (no
        entry slippage).  If sl_points and/or target_points are provided, GTT
        rules are created after the entry order is acknowledged so the
        stop-loss and target sit at the exchange until triggered.

        Returns structured result with order_id, gtt_rules, etc.
        """
        if not self._ensure_logged_in():
            return {"status": False, "message": "not logged in"}

        qty = lots * LOT_SIZES.get(symbol, 1)
        exchange = EXCHANGE.get(symbol, "NFO")

        # Entry order.  Use LIMIT when a price is supplied to avoid slippage.
        if limit_price is not None and limit_price > 0:
            ordertype = "LIMIT"
            price = str(round(limit_price, 2))
            triggerprice = "0"
        else:
            ordertype = "MARKET"
            price = "0"
            triggerprice = "0"

        payload = {
            "variety": "NORMAL",
            "tradingsymbol": tradingsymbol,
            "symboltoken": token,
            "transactiontype": side,
            "exchange": exchange,
            "ordertype": ordertype,
            "producttype": PRODUCT_TYPE,
            "duration": "DAY",
            "quantity": str(qty),
            "price": price,
            "squareoff": "0",
            "stoploss": "0",
            "triggerprice": triggerprice,
        }
        logger.info("TEST order | %s %s %s %s lots=%d ordertype=%s price=%s",
                    side, symbol, option_type, tradingsymbol, lots, ordertype, price)
        raw = self.fetcher._api.placeOrder(payload)
        order_id = self._extract_order_id(raw)
        if not order_id:
            logger.error("place_single_order: no orderid in response: %r", raw)
            return {"status": False, "message": "no orderid in response", "raw": raw}

        result = {"status": True, "order_id": order_id, "ordertype": ordertype,
                  "entry_price": float(price) if ordertype == "LIMIT" else None, "raw": raw}

        # GTT SL + target rules.  Wait briefly for the entry fill so the GTT
        # reference price is the actual fill, and we don't create sell GTTs
        # before we own the position.
        if (sl_points is not None or target_points is not None) and result["entry_price"]:
            fill_px = self._wait_for_fill(order_id, max_wait_sec=5, poll_sec=0.5)
            entry = fill_px if fill_px else result["entry_price"]
            result["fill_price"] = entry
            gtt_rules = []
            if sl_points is not None:
                sl_trigger = round(entry - sl_points, 2)
                sl_price = round(entry - sl_points, 2)
                resp = self.fetcher.gtt_create_rule(
                    tradingsymbol=tradingsymbol,
                    token=token,
                    exchange=exchange,
                    transactiontype="SELL" if side == "BUY" else "BUY",
                    producttype=PRODUCT_TYPE,
                    qty=qty,
                    triggerprice=sl_trigger,
                    price=sl_price,
                )
                gtt_rules.append({"type": "SL", "trigger": sl_trigger, "price": sl_price, "raw": resp})
            if target_points is not None:
                tgt_trigger = round(entry + target_points, 2)
                tgt_price = round(entry + target_points, 2)
                resp = self.fetcher.gtt_create_rule(
                    tradingsymbol=tradingsymbol,
                    token=token,
                    exchange=exchange,
                    transactiontype="SELL" if side == "BUY" else "BUY",
                    producttype=PRODUCT_TYPE,
                    qty=qty,
                    triggerprice=tgt_trigger,
                    price=tgt_price,
                )
                gtt_rules.append({"type": "TARGET", "trigger": tgt_trigger, "price": tgt_price, "raw": resp})
            result["gtt_rules"] = gtt_rules

        return result

    def _wait_for_fill(self, order_id: str, max_wait_sec: float = 5.0, poll_sec: float = 0.5) -> Optional[float]:
        """Poll trade book for an order fill. Returns fill price or None."""
        import time
        deadline = time.time() + max_wait_sec
        while time.time() < deadline:
            try:
                tb = self.fetcher.get_trade_book()
                for t in tb:
                    if t.get("order_id") == order_id:
                        px = float(t.get("price") or 0)
                        if px > 0:
                            return px
            except Exception as e:
                logger.debug("_wait_for_fill poll error: %s", e)
            time.sleep(poll_sec)
        return None

    def place_combo(self, signal: SyntheticForwardSignal, legs: list[ComboLeg],
                    use_limit: bool = True,
                    sl_points: Optional[float] = None,
                    target_points: Optional[float] = None) -> Optional[Position]:
        """Place a synthetic-forward combo with LIMIT entry and optional GTT exits.

        When use_limit is True each leg is entered at the current ask (buy) or
        bid (sell) to avoid slippage.  After fills, GTT rules are created to
        exit the combo when its net value moves by sl_points/target_points.
        """
        if not legs:
            logger.warning("place_combo: no legs provided")
            return None

        position_id = f"nse_{signal.symbol}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        entry_time = datetime.now(timezone.utc)

        if not self._ensure_logged_in():
            logger.error("place_combo: not logged in")
            return None

        # Fetch quotes for limit prices.
        leg_quotes: dict[str, dict] = {}
        if use_limit:
            for leg in legs:
                q = self.fetcher.get_option_quote(leg.tradingsymbol, leg.token,
                                                  EXCHANGE.get(signal.symbol, "NFO"))
                if not q:
                    logger.error("place_combo: quote unavailable for %s", leg.tradingsymbol)
                    return None
                leg_quotes[leg.tradingsymbol] = q

        filled_legs = []
        try:
            for leg in legs:
                if use_limit:
                    # Buy at ask, sell at bid to avoid immediate slippage.
                    lp = leg_quotes[leg.tradingsymbol]["ask"] if leg.side == "BUY" else leg_quotes[leg.tradingsymbol]["bid"]
                else:
                    lp = None
                payload = self._build_order_payload(leg, limit_price=lp)
                logger.info("LIVE order %s | %s %s %s @ %s ordertype=%s price=%s",
                            position_id, leg.side, leg.option_type, leg.strike,
                            leg.tradingsymbol, payload["ordertype"], payload["price"])
                resp = self.fetcher._api.placeOrder(payload)
                order_id = self._extract_order_id(resp)
                if not order_id:
                    err = (resp or {}).get("message", "unknown")
                    logger.error("place_combo: order failed for %s: %s", leg.tradingsymbol, err)
                    self._revert_partial_combo(filled_legs)
                    return None
                leg.order_id = order_id
                leg.entry_px = float(payload["price"]) if payload["ordertype"] == "LIMIT" else 0.0
                filled_legs.append(leg)

            # Wait for fills before attaching prices / creating GTT exits.
            self._attach_fill_prices(filled_legs)

            # Create GTT exit rules if requested.
            if sl_points is not None or target_points is not None:
                gtt_rules = self._create_combo_gtt_rules(
                    signal.symbol, filled_legs, signal.side,
                    sl_points=sl_points, target_points=target_points,
                )
                logger.info("LIVE combo %s | GTT rules: %s", position_id, gtt_rules)

            return Position(
                position_id=position_id,
                symbol=signal.symbol,
                signal_side=signal.side,
                entry_time=entry_time,
                legs=filled_legs,
                spot_at_entry=signal.spot,
                pred_pct=signal.pred * 100,
                stop_loss_pct=0.015,
                target_pct=0.010,
                max_hold_until=signal.expiry,
            )
        except Exception as e:
            logger.exception("place_combo: exception placing combo: %s", e)
            self._revert_partial_combo(filled_legs)
            return None

    def _create_combo_gtt_rules(self, symbol: str, legs: list[ComboLeg], signal_side: str,
                                sl_points: Optional[float],
                                target_points: Optional[float]) -> list[dict]:
        """Create GTT rules to exit a synthetic-forward combo.

        The combo value is approximated as CE_price - PE_price.  A move of
        `sl_points` against the position or `target_points` for the position is
        split equally between the two legs.
        """
        ce_leg = next((l for l in legs if l.option_type == "CE"), None)
        pe_leg = next((l for l in legs if l.option_type == "PE"), None)
        if ce_leg is None or pe_leg is None:
            logger.warning("_create_combo_gtt_rules: missing CE/PE leg")
            return []

        ce_fill = ce_leg.filled_px or ce_leg.entry_px or 0
        pe_fill = pe_leg.filled_px or pe_leg.entry_px or 0
        if ce_fill <= 0 or pe_fill <= 0:
            logger.warning("_create_combo_gtt_rules: missing fill prices")
            return []

        exchange = EXCHANGE.get(symbol, "NFO")
        qty = ce_leg.lots * LOT_SIZES.get(symbol, 1)
        rules = []

        # Long synthetic: bought CE, sold PE.  Loss when combo value drops.
        # Short synthetic: sold CE, bought PE.  Loss when combo value rises.
        if signal_side == "long":
            if sl_points is not None:
                rules.append(self._gtt_leg(symbol, ce_leg, "SELL", ce_fill - sl_points / 2))
                rules.append(self._gtt_leg(symbol, pe_leg, "BUY",  pe_fill + sl_points / 2))
            if target_points is not None:
                rules.append(self._gtt_leg(symbol, ce_leg, "SELL", ce_fill + target_points / 2))
                rules.append(self._gtt_leg(symbol, pe_leg, "BUY",  pe_fill - target_points / 2))
        else:  # short
            if sl_points is not None:
                rules.append(self._gtt_leg(symbol, ce_leg, "BUY",  ce_fill + sl_points / 2))
                rules.append(self._gtt_leg(symbol, pe_leg, "SELL", pe_fill - sl_points / 2))
            if target_points is not None:
                rules.append(self._gtt_leg(symbol, ce_leg, "BUY",  ce_fill - target_points / 2))
                rules.append(self._gtt_leg(symbol, pe_leg, "SELL", pe_fill + target_points / 2))

        return rules

    def _gtt_leg(self, symbol: str, leg: ComboLeg, transactiontype: str, price: float) -> dict:
        """Place one GTT rule for a leg. Returns a dict describing the result."""
        exchange = EXCHANGE.get(symbol, "NFO")
        qty = leg.lots * LOT_SIZES.get(symbol, 1)
        trigger = round(price, 2)
        limit = round(price, 2)
        resp = self.fetcher.gtt_create_rule(
            tradingsymbol=leg.tradingsymbol,
            token=leg.token,
            exchange=exchange,
            transactiontype=transactiontype,
            producttype=PRODUCT_TYPE,
            qty=qty,
            triggerprice=trigger,
            price=limit,
        )
        return {
            "tradingsymbol": leg.tradingsymbol,
            "transactiontype": transactiontype,
            "trigger": trigger,
            "price": limit,
            "raw": resp,
        }

    def _revert_partial_combo(self, filled_legs: list[ComboLeg]):
        """Best-effort square off of legs already filled before failure."""
        if not filled_legs:
            return
        logger.warning("Reverting partial combo: %d legs", len(filled_legs))
        for leg in filled_legs:
            try:
                revert_side = "BUY" if leg.side == "SELL" else "SELL"
                payload = {
                    "variety": "NORMAL",
                    "tradingsymbol": leg.tradingsymbol,
                    "symboltoken": leg.token,
                    "transactiontype": revert_side,
                    "exchange": EXCHANGE.get(self._symbol_from_ts(leg.tradingsymbol), "NFO"),
                    "ordertype": "MARKET",
                    "producttype": PRODUCT_TYPE,
                    "duration": "DAY",
                    "quantity": str(leg.lots * LOT_SIZES.get(self._symbol_from_ts(leg.tradingsymbol), 1)),
                    "price": "0",
                    "squareoff": "0",
                    "stoploss": "0",
                    "triggerprice": "0",
                }
                self.fetcher._api.placeOrder(payload)
            except Exception as e:
                logger.error("Revert leg failed for %s: %s", leg.tradingsymbol, e)

    def _attach_fill_prices(self, legs: list[ComboLeg]):
        """Pull fill prices from Angel trade book."""
        try:
            tb = self.fetcher.get_trade_book()
            by_order = {t["order_id"]: t for t in tb}
            for leg in legs:
                t = by_order.get(leg.order_id)
                if t:
                    leg.filled_px = float(t.get("price") or 0)
                else:
                    leg.filled_px = leg.entry_px
        except Exception as e:
            logger.warning("Could not attach fill prices: %s", e)
            for leg in legs:
                leg.filled_px = leg.entry_px

    def close_combo(self, position: Position) -> bool:
        """Square off an open combo position. Returns True on full success."""
        if not self._ensure_logged_in():
            logger.error("close_combo: not logged in")
            return False

        all_ok = True
        for leg in position.legs:
            try:
                close_side = "BUY" if leg.side == "SELL" else "SELL"
                payload = {
                    "variety": "NORMAL",
                    "tradingsymbol": leg.tradingsymbol,
                    "symboltoken": leg.token,
                    "transactiontype": close_side,
                    "exchange": EXCHANGE.get(position.symbol, "NFO"),
                    "ordertype": "MARKET",
                    "producttype": PRODUCT_TYPE,
                    "duration": "DAY",
                    "quantity": str(leg.lots * LOT_SIZES.get(position.symbol, 1)),
                    "price": "0",
                    "squareoff": "0",
                    "stoploss": "0",
                    "triggerprice": "0",
                }
                resp = self.fetcher._api.placeOrder(payload)
                if not resp or not resp.get("status") or not resp.get("data"):
                    logger.error("close_combo: failed to close %s", leg.tradingsymbol)
                    all_ok = False
            except Exception as e:
                logger.exception("close_combo: exception closing %s: %s", leg.tradingsymbol, e)
                all_ok = False
        return all_ok

    def get_open_positions(self) -> list[dict]:
        """Fetch open positions from Angel One (best-effort)."""
        if not self._ensure_logged_in():
            return []
        try:
            resp = self.fetcher._api.position()
            if resp and resp.get("status") and resp.get("data"):
                return resp["data"]
        except Exception as e:
            logger.warning("get_open_positions failed: %s", e)
        return []
