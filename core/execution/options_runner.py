"""
OptionsRunner — live manager for short-option strategies.

Runs alongside CryptoRunner but is completely separate: it manages two-leg
option positions (call + put) with their own entry cadence, margin checks, and
exit rules. Defaults to paper mode even when enabled; live mode must be set
explicitly via OPTIONS_TRADING_MODE=live.

Positions are evaluated on the same 2-second tick as the perp runner so exits
are caught promptly, but entries are intentionally slow (once per day) to match
the backtest design.
"""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Optional

from core.brokers.delta_crypto import DeltaCryptoBroker
from core.risk_management import (
    USD_INR_RATE,
    ENABLE_OPTIONS_RUNNER,
    OPTIONS_TRADING_MODE,
    OPTIONS_ENTRY_HOUR_UTC,
    OPTIONS_MAX_POSITIONS,
    OPTIONS_FEE_BPS,
    OPTIONS_SLIPPAGE_BPS,
    OPTIONS_PROFIT_PCT,
    OPTIONS_STOP_MULT,
)
from strategies.eth_short_straddle import ETHShortStraddleSignal
from strategies.crypto_base import OptionsSignalDecision

logger = logging.getLogger(__name__)

# Runtime state
_OPTIONS_BROKER: Optional[DeltaCryptoBroker] = None
_STRATEGY_INSTANCES: dict[str, object] = {}
_OPEN_POSITIONS: list[dict] = []
_CLOSED_TRADES: list[dict] = []
_MAX_CLOSED_TRADES = 200
_KILLED: bool = False


def _get_broker() -> DeltaCryptoBroker:
    """Dedicated broker instance for the options runner.

    Uses OPTIONS_TRADING_MODE so the options book can be paper even when the
    perp runner is live. The shared Delta WebSocket stream still feeds both.
    """
    global _OPTIONS_BROKER
    if _OPTIONS_BROKER is None:
        _OPTIONS_BROKER = DeltaCryptoBroker(mode=OPTIONS_TRADING_MODE)
    return _OPTIONS_BROKER


def _get_strategies():
    if not _STRATEGY_INSTANCES:
        broker = _get_broker()
        inst = ETHShortStraddleSignal(broker=broker)
        _STRATEGY_INSTANCES[inst.name] = inst
        logger.info("options runner initialized with %s", inst.name)
    return _STRATEGY_INSTANCES


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_entry_time() -> bool:
    """Allow one entry attempt per day at the configured UTC hour."""
    now = _now()
    return now.hour == OPTIONS_ENTRY_HOUR_UTC and now.minute < 5


def _entry_already_today() -> bool:
    """True if an entry was already attempted in the current UTC hour today."""
    today = _now().date().isoformat()
    for pos in _OPEN_POSITIONS:
        if pos.get("entry_date") == today:
            return True
    for t in _CLOSED_TRADES:
        if t.get("entry_date") == today:
            return True
    return False


def _place_leg(broker, symbol: str, side: str, size: int, tag: str) -> dict:
    """Place one option leg. Returns order dict or failed dict."""
    if size <= 0:
        return {"ok": False, "error": "zero size"}
    # Short options do not use the perp leverage dial; margin is exchange-set.
    return broker.place_order(
        symbol=symbol,
        side=side,
        size=size,
        order_type="market_order",
        tag=tag,
    )


def _slipped_price(side: str, mark: float, slippage_bps: float) -> float:
    """Apply slippage against the option mark for paper fills.

    Selling → fill worse (lower) by slippage; buying → fill worse (higher).
    """
    mult = 1 - slippage_bps / 1e4 if side == "sell" else 1 + slippage_bps / 1e4
    return mark * mult


def _open_position(dec: OptionsSignalDecision) -> Optional[dict]:
    """Execute entry orders for both legs. Returns position dict or None."""
    broker = _get_broker()
    tag_base = f"{dec.name}_entry"
    qty = dec.qty

    call_fill = _place_leg(broker, dec.call_symbol, "sell", qty, f"{tag_base}_call")
    put_fill = _place_leg(broker, dec.put_symbol, "sell", qty, f"{tag_base}_put")

    if not call_fill.get("ok") or not put_fill.get("ok"):
        logger.error("%s entry failed: call=%s put=%s", dec.name, call_fill, put_fill)
        # Best-effort unwind any filled leg
        if call_fill.get("ok"):
            broker.place_order(dec.call_symbol, "buy", qty,
                               order_type="market_order", tag=f"{tag_base}_unwind_call")
        if put_fill.get("ok"):
            broker.place_order(dec.put_symbol, "buy", qty,
                               order_type="market_order", tag=f"{tag_base}_unwind_put")
        return None

    call_px = call_fill.get("fill_price") or dec.call_mark
    put_px = put_fill.get("fill_price") or dec.put_mark
    credit = (call_px + put_px) * dec.contract_size

    now = _now()
    pos = {
        "id": f"{dec.name}-{now.strftime('%Y%m%d%H%M%S')}",
        "strategy": dec.name,
        "entry_ts": now.timestamp(),
        "entry_date": now.date().isoformat(),
        "expiry": dec.expiry,
        "underlying": dec.underlying,
        "call_symbol": dec.call_symbol,
        "put_symbol": dec.put_symbol,
        "call_strike": dec.call_strike,
        "put_strike": dec.put_strike,
        "qty": qty,
        "entry_call_px": call_px,
        "entry_put_px": put_px,
        "entry_credit": credit,
        "entry_spot": dec.spot_mark,
        "contract_size": dec.contract_size,
        "margin_per_straddle": dec.margin_per_straddle,
        "total_margin": dec.total_margin,
        "profit_pct": dec.profit_pct,
        "stop_mult": dec.stop_mult,
        "fee_bps": dec.fee_bps,
        "slippage_bps": dec.slippage_bps,
        "status": "open",
        "mode": OPTIONS_TRADING_MODE,
    }
    _log_trade_event({
        "ts": now, "event": "entry", "venue": "delta_india",
        "mode": OPTIONS_TRADING_MODE, "strategy": dec.name,
        "position_id": pos["id"], "underlying": dec.underlying,
        "expiry": dec.expiry, "qty": qty,
        "call_symbol": dec.call_symbol, "put_symbol": dec.put_symbol,
        "entry_credit": credit, "entry_spot": dec.spot_mark,
        "total_margin": dec.total_margin,
    })
    logger.info(
        "%s ENTRY qty=%d credit=%.4f margin=%.2f call=%s@%.4f put=%s@%.4f",
        dec.name, qty, credit, dec.total_margin,
        dec.call_symbol, call_px, dec.put_symbol, put_px,
    )
    return pos


def _exit_position(pos: dict, reason: str,
                   call_mark: float, put_mark: float) -> None:
    """Close both legs and record the trade."""
    broker = _get_broker()
    qty = pos["qty"]
    tag = f"{pos['strategy']}_{reason}"

    # In paper mode, apply slippage to the mark. In live mode we rely on the
    # actual fill price returned by the broker.
    if broker.mode == "paper":
        call_fill_px = _slipped_price("buy", call_mark, pos["slippage_bps"])
        put_fill_px = _slipped_price("buy", put_mark, pos["slippage_bps"])
        call_fill = {"ok": True, "fill_price": call_fill_px}
        put_fill = {"ok": True, "fill_price": put_fill_px}
    else:
        call_fill = _place_leg(broker, pos["call_symbol"], "buy", qty,
                               f"{tag}_call")
        put_fill = _place_leg(broker, pos["put_symbol"], "buy", qty,
                              f"{tag}_put")
        if not call_fill.get("ok") or not put_fill.get("ok"):
            logger.error("%s exit failed: call=%s put=%s", pos["id"], call_fill, put_fill)
            return

    call_px = call_fill.get("fill_price") or call_mark
    put_px = put_fill.get("fill_price") or put_mark
    buyback = (call_px + put_px) * pos["contract_size"]
    credit = pos["entry_credit"]
    gross = qty * (credit - buyback)

    # Fees: 2 legs in, 2 legs out
    fee = qty * (credit + buyback) * 4 * pos["fee_bps"] / 1e4
    net = gross - fee

    pos["status"] = "closed"
    pos["exit_ts"] = _now().timestamp()
    pos["exit_reason"] = reason
    pos["exit_call_px"] = call_px
    pos["exit_put_px"] = put_px
    pos["exit_buyback"] = buyback
    pos["gross_pnl"] = gross
    pos["fee"] = fee
    pos["net_pnl"] = net

    _CLOSED_TRADES.append(pos)
    if len(_CLOSED_TRADES) > _MAX_CLOSED_TRADES:
        _CLOSED_TRADES.pop(0)
    try:
        idx = _OPEN_POSITIONS.index(pos)
        _OPEN_POSITIONS.pop(idx)
    except ValueError:
        pass

    _log_trade_event({
        "ts": _now(), "event": "exit", "venue": "delta_india",
        "mode": OPTIONS_TRADING_MODE, "strategy": pos["strategy"],
        "position_id": pos["id"], "underlying": pos["underlying"],
        "expiry": pos["expiry"], "qty": qty, "reason": reason,
        "exit_buyback": buyback, "gross_pnl": gross, "fee": fee, "net_pnl": net,
    })
    logger.info(
        "%s EXIT %s qty=%d buyback=%.4f gross=%.4f fee=%.4f net=%.4f",
        pos["id"], reason, qty, buyback, gross, fee, net,
    )


def _log_trade_event(event: dict) -> None:
    """Persist options trade events to Mongo."""
    try:
        from core import mongo
        db = mongo.get_db()
        if db is None:
            return
        db["crypto_options_trades"].insert_one(event)
    except Exception as e:
        logger.warning("crypto_options_trades write failed: %s", e)


def _manage_positions() -> None:
    """Evaluate every open straddle for profit target, stop, or expiry."""
    broker = _get_broker()
    now = _now()
    for pos in list(_OPEN_POSITIONS):
        if pos.get("status") != "open":
            continue
        call_mark = broker.get_option_mark(pos["call_symbol"])
        put_mark = broker.get_option_mark(pos["put_symbol"])
        if call_mark is None or put_mark is None:
            logger.debug("%s: missing mark for exit check; skipping tick", pos["id"])
            continue

        buyback = (call_mark + put_mark) * pos["contract_size"]
        credit = pos["entry_credit"]

        try:
            expiry_dt = datetime.fromisoformat(pos["expiry"].replace("Z", "+00:00"))
        except Exception:
            expiry_dt = None

        reason = None
        if expiry_dt is not None and now >= expiry_dt:
            reason = "expiry"
        elif buyback <= credit * (1 - pos["profit_pct"]):
            reason = "profit_target"
        elif buyback >= credit * pos["stop_mult"]:
            reason = "stop_loss"

        if reason:
            _exit_position(pos, reason, call_mark, put_mark)


def _try_entry() -> None:
    """Run the strategy and open a position if it emits a decision."""
    if _KILLED:
        return
    if len(_OPEN_POSITIONS) >= OPTIONS_MAX_POSITIONS:
        logger.debug("max open option positions reached (%d)", OPTIONS_MAX_POSITIONS)
        return
    if not _is_entry_time():
        return
    if _entry_already_today():
        return

    strategies = _get_strategies()
    strat = strategies.get("eth_short_straddle")
    if strat is None:
        return

    dec = strat.on_tick()
    if dec is None:
        return
    if not isinstance(dec, OptionsSignalDecision):
        logger.warning("options runner received non-options decision: %s", dec)
        return

    pos = _open_position(dec)
    if pos is not None:
        _OPEN_POSITIONS.append(pos)


def tick_position_management() -> None:
    """Scheduler hook: run every 2s from the crypto bot."""
    if not ENABLE_OPTIONS_RUNNER:
        return
    try:
        _manage_positions()
    except Exception as e:
        logger.error("options position management error: %s", e)


def tick_entry() -> None:
    """Scheduler hook: run once per hour; entry is gated internally by hour."""
    if not ENABLE_OPTIONS_RUNNER:
        return
    try:
        _try_entry()
    except Exception as e:
        logger.error("options entry error: %s", e)


def init_options_runner(scheduler) -> None:
    """Register options jobs with the APScheduler instance."""
    if not ENABLE_OPTIONS_RUNNER:
        logger.info("options runner is disabled (ENABLE_OPTIONS_RUNNER=false)")
        return
    logger.warning(
        "OPTIONS RUNNER ENABLED — mode=%s. Short options carry tail risk; "
        "verify margin rules on Delta before switching to live.",
        OPTIONS_TRADING_MODE,
    )
    # Eagerly instantiate strategy so symbol discovery has a head start.
    _get_strategies()

    scheduler.add_job(
        tick_entry,
        "cron", hour="*", minute="0",
        id="options_entry_hourly", replace_existing=True,
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        tick_position_management,
        "interval", seconds=2,
        id="options_position_mgmt", replace_existing=True,
        max_instances=1, coalesce=True,
    )
    logger.info("options runner jobs registered")


def get_options_state() -> dict:
    """Snapshot for dashboard / diagnostics."""
    return {
        "enabled": ENABLE_OPTIONS_RUNNER,
        "mode": OPTIONS_TRADING_MODE,
        "killed": _KILLED,
        "open_positions": _OPEN_POSITIONS,
        "closed_trades": _CLOSED_TRADES[-20:],
    }


def kill_options_runner() -> None:
    """Manual kill switch: halt new entries. Existing positions remain managed."""
    global _KILLED
    _KILLED = True
    logger.warning("options runner killed — no new entries")
