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
from nse.config import (
    EXCHANGE,
    GTT_ENABLED,
    GTT_MAX_TIMEPERIOD_DAYS,
    GTT_MIN_PREMIUM,
    LOT_SIZES,
    PRODUCT_TYPE,
    gtt_levels_for_leg,
    gtt_limit_through,
)
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
        """Place a single option leg order with a protective OCO GTT bracket.

        If limit_price is provided the entry is a LIMIT order (no entry
        slippage). sl_points / target_points are distances in OPTION PREMIUM
        from the fill, and are attached as ONE OCO GTT rule so the exchange
        cancels the target when the stop fires and vice versa.

        Returns a structured result with order_id, fill_price and gtt.
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

        # Protective bracket. Wait briefly for the entry fill so the levels
        # reference the real fill, and so we never arm an exit rule before we
        # actually own the position.
        if (sl_points is not None or target_points is not None) and result["entry_price"]:
            fill_px = self._wait_for_fill(order_id, max_wait_sec=5, poll_sec=0.5)
            entry = fill_px if fill_px else result["entry_price"]
            result["fill_price"] = entry

            long_leg = side == "BUY"
            exit_side = "SELL" if long_leg else "BUY"
            sl = sl_points if sl_points is not None else 0.0
            tg = target_points if target_points is not None else 0.0
            # A long option loses as premium falls; a short loses as it rises.
            stop_px = round(max(GTT_MIN_PREMIUM, entry - sl if long_leg else entry + sl), 2)
            target_px = round(max(GTT_MIN_PREMIUM, entry + tg if long_leg else entry - tg), 2)

            rule_id = self.fetcher.gtt_create_rule(
                tradingsymbol=tradingsymbol,
                token=token,
                exchange=exchange,
                transactiontype=exit_side,
                producttype=PRODUCT_TYPE,
                qty=qty,
                triggerprice=target_px,
                price=gtt_limit_through(target_px, exit_side == "SELL"),
                stoploss_trigger=stop_px,
                stoploss_price=gtt_limit_through(stop_px, exit_side == "SELL"),
                timeperiod=GTT_MAX_TIMEPERIOD_DAYS,
            )
            result["gtt"] = {
                "rule_id": rule_id, "exit_side": exit_side,
                "target": target_px, "stop": stop_px, "fill": entry,
            }
            logger.info("TEST order GTT OCO | %s %s target=%.2f stop=%.2f id=%s",
                        tradingsymbol, exit_side, target_px, stop_px, rule_id)

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
                    attach_gtt: bool = GTT_ENABLED) -> Optional[Position]:
        """Place a synthetic-forward combo: LIMIT entry + protective OCO GTT.

        When use_limit is True each leg is entered at the current ask (buy) or
        bid (sell) to avoid slippage. After fills, each leg gets one OCO GTT
        rule holding its target and stop at the exchange.

        The GTT bracket is a disaster backstop sized off each leg's premium
        (see nse/config.py). The strategy's real exit is combo-level and runs
        in nse_runner._position_tick — a per-leg rule cannot express "exit when
        CE - PE moves 1.5%", and firing one leg alone would leave the other
        naked, so the bracket sits deliberately wide.
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

            # Attach the broker-side protective bracket. This is a backstop
            # that survives this process dying — the strategy's own combo-level
            # exit still runs in _position_tick and normally acts first.
            if attach_gtt:
                rules = self.attach_gtt_brackets(signal.symbol, filled_legs,
                                                 signal.expiry, signal.spot)
                logger.info("LIVE combo %s | GTT brackets: %s", position_id, rules)
                if any(r.get("rule_id") is None for r in rules) or len(rules) < len(filled_legs):
                    logger.error("LIVE combo %s | one or more legs have NO broker-side "
                                 "bracket — this position is only protected while the "
                                 "runner is alive", position_id)

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

    def _gtt_timeperiod_days(self, expiry: datetime) -> int:
        """Rule lifetime, clamped to the contract's remaining life.

        A weekly option must never carry a 365-day rule: the rule would
        outlive the instrument it references.
        """
        days = (expiry - datetime.now(timezone.utc)).days + 1
        return max(1, min(GTT_MAX_TIMEPERIOD_DAYS, days))

    def attach_gtt_brackets(self, symbol: str, legs: list[ComboLeg],
                            expiry: datetime, spot: float) -> list[dict]:
        """Attach ONE protective OCO GTT rule to each filled leg.

        Levels are derived from each leg's own fill premium — not from spot
        points. (The previous implementation passed a spot-derived distance,
        e.g. 360 NIFTY points, and subtracted it from a ~150 premium, which
        produced negative GTT prices.)

        The rule closes the leg, so its transaction type is the opposite of
        the entry side. Target and stop live in a single OCO rule so the
        exchange cancels one when the other fires.
        """
        if not GTT_ENABLED:
            return []
        exchange = EXCHANGE.get(symbol, "NFO")
        lot = LOT_SIZES.get(symbol, 1)
        timeperiod = self._gtt_timeperiod_days(expiry)
        out: list[dict] = []

        for leg in legs:
            fill = leg.filled_px or leg.entry_px or 0.0
            if fill <= 0:
                logger.warning("attach_gtt_brackets: no fill price for %s — skipping bracket",
                               leg.tradingsymbol)
                continue

            exit_side = "SELL" if leg.side == "BUY" else "BUY"
            # Shared with the backtest so the two cannot drift apart.
            stop_px, target_px = gtt_levels_for_leg(
                fill, is_long=leg.side == "BUY", spot=spot)
            if stop_px <= 0 or target_px <= 0:
                logger.error("attach_gtt_brackets: computed non-positive levels for %s "
                             "(fill=%.2f stop=%.2f target=%.2f) — skipping",
                             leg.tradingsymbol, fill, stop_px, target_px)
                continue

            rule_id = self.fetcher.gtt_create_rule(
                tradingsymbol=leg.tradingsymbol,
                token=leg.token,
                exchange=exchange,
                transactiontype=exit_side,
                producttype=PRODUCT_TYPE,
                qty=leg.lots * lot,
                triggerprice=target_px,
                price=gtt_limit_through(target_px, exit_side == "SELL"),
                stoploss_trigger=stop_px,
                stoploss_price=gtt_limit_through(stop_px, exit_side == "SELL"),
                timeperiod=timeperiod,
            )
            leg.gtt_rule_id = rule_id
            leg.gtt_stop_px = stop_px
            leg.gtt_target_px = target_px
            out.append({
                "tradingsymbol": leg.tradingsymbol,
                "rule_id": rule_id,
                "exit_side": exit_side,
                "fill": fill,
                "stop": stop_px,
                "target": target_px,
                "timeperiod_days": timeperiod,
            })
            if rule_id:
                logger.info("GTT OCO attached | %s %s qty=%d target=%.2f stop=%.2f id=%s",
                            leg.tradingsymbol, exit_side, leg.lots * lot,
                            target_px, stop_px, rule_id)
            else:
                logger.error("GTT OCO FAILED | %s — leg has no broker-side protection",
                             leg.tradingsymbol)
        return out

    def cancel_gtt_brackets(self, symbol: str, legs: list[ComboLeg]) -> bool:
        """Cancel every leg's GTT rule. Call before squaring off.

        An armed rule on a position we've already closed will trigger later and
        open a fresh unintended position, so this runs on EVERY exit path.
        """
        exchange = EXCHANGE.get(symbol, "NFO")
        all_ok = True
        for leg in legs:
            if not leg.gtt_rule_id:
                continue
            ok = self.fetcher.gtt_cancel_rule(
                rule_id=leg.gtt_rule_id,
                tradingsymbol=leg.tradingsymbol,
                token=leg.token,
                exchange=exchange,
            )
            if ok:
                logger.info("GTT cancelled | %s id=%s", leg.tradingsymbol, leg.gtt_rule_id)
                leg.gtt_rule_id = None
            else:
                all_ok = False
                logger.error("GTT cancel FAILED | %s id=%s — rule may still be armed at "
                             "the exchange; cancel it manually",
                             leg.tradingsymbol, leg.gtt_rule_id)
        return all_ok

    def _revert_partial_combo(self, filled_legs: list[ComboLeg]):
        """Best-effort square off of legs already filled before failure."""
        if not filled_legs:
            return
        logger.warning("Reverting partial combo: %d legs", len(filled_legs))
        # Drop any bracket already attached, so it can't fire on a leg we are
        # about to flatten.
        for leg in filled_legs:
            if leg.gtt_rule_id:
                self.fetcher.gtt_cancel_rule(
                    rule_id=leg.gtt_rule_id, tradingsymbol=leg.tradingsymbol,
                    token=leg.token,
                    exchange=EXCHANGE.get(self._symbol_from_ts(leg.tradingsymbol), "NFO"),
                )
                leg.gtt_rule_id = None
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
        """Square off an open combo position. Returns True on full success.

        Cancels the protective GTT rules FIRST. If the square-off ran first,
        the exit fills could trip a still-armed rule, and any rule that
        survives this position would later open a fresh unintended one.
        """
        if not self._ensure_logged_in():
            logger.error("close_combo: not logged in")
            return False

        gtt_ok = self.cancel_gtt_brackets(position.symbol, position.legs)
        if not gtt_ok:
            logger.error("close_combo: %s — some GTT rules could not be cancelled; "
                         "squaring off anyway, but check the GTT book",
                         position.position_id)

        all_ok = gtt_ok
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
