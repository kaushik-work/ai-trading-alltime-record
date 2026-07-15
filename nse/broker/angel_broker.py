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

    def _build_order_payload(self, leg: ComboLeg, variety: str = "NORMAL") -> dict:
        """Build Angel One placeOrder payload for an option leg."""
        return {
            "variety": variety,
            "tradingsymbol": leg.tradingsymbol,
            "symboltoken": leg.token,
            "transactiontype": leg.side,
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
                           option_type: str, side: str, lots: int) -> dict:
        """Place a single option leg order. Returns structured result."""
        if not self._ensure_logged_in():
            return {"status": False, "message": "not logged in"}
        payload = {
            "variety": "NORMAL",
            "tradingsymbol": tradingsymbol,
            "symboltoken": token,
            "transactiontype": side,
            "exchange": EXCHANGE.get(symbol, "NFO"),
            "ordertype": "MARKET",
            "producttype": PRODUCT_TYPE,
            "duration": "DAY",
            "quantity": str(lots * LOT_SIZES.get(symbol, 1)),
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "triggerprice": "0",
        }
        logger.info("TEST order | %s %s %s %s lots=%d", side, symbol, option_type, tradingsymbol, lots)
        raw = self.fetcher._api.placeOrder(payload)
        order_id = self._extract_order_id(raw)
        if not order_id:
            logger.error("place_single_order: no orderid in response: %r", raw)
            return {"status": False, "message": "no orderid in response", "raw": raw}
        return {"status": True, "order_id": order_id, "raw": raw}

    def place_combo(self, signal: SyntheticForwardSignal, legs: list[ComboLeg]) -> Optional[Position]:
        """Place a synthetic-forward combo. Returns Position or None on failure."""
        if not legs:
            logger.warning("place_combo: no legs provided")
            return None

        position_id = f"nse_{signal.symbol}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        entry_time = datetime.now(timezone.utc)

        if not self._ensure_logged_in():
            logger.error("place_combo: not logged in")
            return None

        filled_legs = []
        try:
            for leg in legs:
                payload = self._build_order_payload(leg)
                logger.info("LIVE order %s | %s %s %s @ %s",
                            position_id, leg.side, leg.option_type, leg.strike, leg.tradingsymbol)
                resp = self.fetcher._api.placeOrder(payload)
                order_id = self._extract_order_id(resp)
                if not order_id:
                    err = (resp or {}).get("message", "unknown")
                    logger.error("place_combo: order failed for %s: %s", leg.tradingsymbol, err)
                    self._revert_partial_combo(filled_legs)
                    return None
                leg.order_id = order_id
                filled_legs.append(leg)

            self._attach_fill_prices(filled_legs)
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
