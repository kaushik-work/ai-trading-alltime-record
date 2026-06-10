"""
CryptoRunner — schedules crypto strategies in parallel with NSE BotRunner.

Sibling to core/bot_runner.py. Where BotRunner ticks NSE Q5 ensemble during
market hours, CryptoRunner ticks crypto strategies 24/7.

Lifecycle:
  • Hourly tick — runs both strategies + position manager.
  • Entry: when strategy emits a SignalDecision, place market order on Delta.
  • Exit: position manager checks every tick for stop / partial TP / trail /
          max-hold / time-stop. ALL exit logic from v5 backtest is preserved.
  • Safety: kill switch on daily loss, max position cap, max concurrent.

How to enable (api/server.py startup):

    from core.crypto_runner import init_crypto_runner
    from api.routes_crypto import router as crypto_router

    init_crypto_runner(scheduler)
    app.include_router(crypto_router)

Required env:
    ENABLE_CRYPTO_RUNNER=1
    CRYPTO_TRADING_MODE=live          # or paper
    DELTA_API_KEY=...                  # for live
    DELTA_API_SECRET=...
    CRYPTO_TICK_SECONDS=5              # default 5 (was 60min — now WS-fed)
    CRYPTO_EQUITY_USD=10000            # base equity for sizing
    CRYPTO_DAILY_LOSS_KILL_PCT=0.05    # kill at -5% day P&L
    CRYPTO_MAX_LIVE_CONTRACTS=200      # absolute cap per asset
"""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Optional

from core.brokers.delta_crypto import get_broker as get_crypto_broker
from strategies.synth_forward import BTCSynthForwardSignal, ETHSynthForwardSignal
from strategies.crypto_base import CryptoSignalDecision

logger = logging.getLogger(__name__)

# Default 2s — the runner is now WS-fed (core/ws/delta_stream.py), so the
# tick is cheap and we react to real-time mark changes. The legacy
# CRYPTO_TICK_MINUTES env var is deliberately ignored: a stale .env on
# any deploy was silently downgrading the bot to 60-min polling.
TICK_INTERVAL_SECONDS = max(1, int(os.environ.get("CRYPTO_TICK_SECONDS", "2")))
BASE_EQUITY_USD       = float(os.environ.get("CRYPTO_EQUITY_USD", "10000"))
# Safety buffer applied to live wallet balance for sizing — leaves room
# for fees, slippage, and partial margin requirements.
WALLET_SAFETY_BUFFER  = float(os.environ.get("CRYPTO_WALLET_BUFFER", "0.95"))
DAILY_LOSS_KILL_PCT   = float(os.environ.get("CRYPTO_DAILY_LOSS_KILL_PCT", "0.05"))
MAX_LIVE_CONTRACTS    = int(os.environ.get("CRYPTO_MAX_LIVE_CONTRACTS", "200"))
MAX_HOLD_HOURS        = 72
# Leverage applied per order. Safe range for this strategy: 5–20×.
# At 200× liquidation is at 0.5% — less than our stop loss distance.
LEVERAGE              = int(os.environ.get("CRYPTO_LEVERAGE", "10"))

# Delta India BTCUSD/ETHUSD perp contract size = 0.001 underlying
CONTRACT_SIZE_BY_ASSET = {"BTCUSD": 0.001, "ETHUSD": 0.001, "XAUTUSD": 0.001}

# In-memory runtime state
_STRATEGY_INSTANCES: dict[str, object] = {}
_OPEN_POSITIONS: dict[str, dict] = {}
_DAY_PNL_USD: float = 0.0
_DAY_PNL_RESET_DATE: Optional[str] = None
_KILLED: bool = False
# Shadow trades: gate-crossed signals that were blocked from going live by
# an empty wallet. We track the FULL lifecycle (open → stop/TP/trail/max-hold
# → closed) so the dashboard can show what the bot WOULD have earned.
# Each entry mutates in place: peak_pct / status / exit_* fields get set as
# the position progresses. _SHADOW_POSITIONS holds same-references to the
# open ones for fast tick-time updates.
_SHADOW_TRADES: list[dict] = []
_SHADOW_POSITIONS: dict[str, dict] = {}
_MAX_SHADOW_TRADES = 50


# ── strategies ────────────────────────────────────────────────────────────────
def _get_strategies():
    if not _STRATEGY_INSTANCES:
        broker = get_crypto_broker()
        for cls in (BTCSynthForwardSignal, ETHSynthForwardSignal):
            inst = cls(broker=broker)
            _STRATEGY_INSTANCES[inst.name] = inst
    return _STRATEGY_INSTANCES


# ── sizing ────────────────────────────────────────────────────────────────────
def _contracts_for_notional(symbol: str, notional_usd: float, mark: float) -> int:
    """Convert USD notional → integer contract count using Delta's contract size."""
    cs = CONTRACT_SIZE_BY_ASSET.get(symbol, 0.001)
    if mark <= 0: return 0
    n = int(notional_usd / (cs * mark))
    return max(0, min(MAX_LIVE_CONTRACTS, n))


# ── daily P&L tracking + kill switch ──────────────────────────────────────────
def _reset_day_pnl_if_needed():
    global _DAY_PNL_USD, _DAY_PNL_RESET_DATE
    today = datetime.now(timezone.utc).date().isoformat()
    if _DAY_PNL_RESET_DATE != today:
        _DAY_PNL_USD = 0.0
        _DAY_PNL_RESET_DATE = today


def _check_kill_switch() -> bool:
    """Returns True if killed. Loss > -X% of base equity → halt new entries."""
    global _KILLED
    _reset_day_pnl_if_needed()
    if _KILLED: return True
    if _DAY_PNL_USD < -BASE_EQUITY_USD * DAILY_LOSS_KILL_PCT:
        _KILLED = True
        logger.error("KILL SWITCH: day PnL %.0f < -%.1f%% of base — halting entries",
                     _DAY_PNL_USD, DAILY_LOSS_KILL_PCT * 100)
        return True
    return False


# ── mongo logging ─────────────────────────────────────────────────────────────
def _log_signal(decision: CryptoSignalDecision) -> None:
    try:
        from core import mongo
        db = mongo.get_db()
        if db is None: return
        db["signal_log"].insert_one({
            "ts": datetime.now(timezone.utc),
            "venue": "delta_india",
            "strategy": decision.name,
            "symbol": decision.symbol,
            "side": decision.side,
            "pred_pct": decision.pred_pct,
            "n_strikes": decision.n_strikes,
            "size_mult": decision.size_mult,
            "metadata": decision.metadata,
        })
    except Exception as e:
        logger.warning("signal_log write failed: %s", e)


def _write_trade_event(event: dict) -> None:
    try:
        from core import mongo
        db = mongo.get_db()
        if db is None: return
        db["crypto_trades"].insert_one(event)
    except Exception as e:
        logger.warning("crypto_trades write failed: %s", e)


# ── position management — the missing piece from before ───────────────────────
def _manage_open_position(strategy_name: str, broker, pos: dict) -> bool:
    """Returns True if position was closed and should be removed."""
    global _DAY_PNL_USD
    symbol = pos["symbol"]
    side = pos["side"]                       # "buy" or "sell"
    sign = 1 if side == "buy" else -1
    entry_px = pos["entry_price"]
    held_h = (time.time() - pos["entry_ts"]) / 3600

    current_mark = broker.get_perp_mark(symbol)
    if current_mark is None: return False

    unrealized_pct = sign * (current_mark - entry_px) / entry_px
    pos["peak_pct"] = max(pos.get("peak_pct", 0.0), unrealized_pct)
    dec = pos["decision"]

    # ── partial TP at +1% (close half once) ──
    if (not pos.get("tp_taken")) and unrealized_pct >= dec.partial_tp_pct:
        half = max(1, pos["contracts"] // 2)
        order = broker.place_order(symbol, "sell" if side == "buy" else "buy",
                                   size=half, order_type="market_order",
                                   reduce_only=True, tag=f"{strategy_name}_partial_tp")
        if order.get("ok"):
            pnl = sign * half * CONTRACT_SIZE_BY_ASSET.get(symbol, 0.001) * \
                  (order.get("fill_price", current_mark) - entry_px)
            _DAY_PNL_USD += pnl
            pos["contracts"] -= half
            pos["tp_taken"] = True
            _write_trade_event({
                "ts": datetime.now(timezone.utc), "venue": "delta_india",
                "mode": broker.mode, "strategy": strategy_name,
                "symbol": symbol, "side": side,
                "event": "partial_tp", "exit_price": order.get("fill_price"),
                "contracts_closed": half, "pnl_usd": pnl,
                "unrealized_pct": unrealized_pct,
            })
            logger.info("%s partial_tp at %s, pnl=%.2f", strategy_name,
                         order.get("fill_price"), pnl)
        # don't return; remaining half continues

    # ── full exit conditions ──
    exit_reason = None
    if held_h >= MAX_HOLD_HOURS:
        exit_reason = "max_hold"
    elif unrealized_pct < -dec.stop_loss_pct:
        exit_reason = "stop_loss"
    elif pos["peak_pct"] >= dec.trail_peak_pct and \
         (pos["peak_pct"] - unrealized_pct) > dec.trail_giveback:
        exit_reason = "trail"
    if exit_reason is None: return False

    # close remaining
    if pos["contracts"] <= 0: return True   # nothing to close
    order = broker.place_order(symbol, "sell" if side == "buy" else "buy",
                               size=pos["contracts"], order_type="market_order",
                               reduce_only=True, tag=f"{strategy_name}_{exit_reason}")
    if not order.get("ok"):
        logger.error("%s exit order failed: %s", strategy_name, order); return False
    fill = order.get("fill_price", current_mark)
    pnl = sign * pos["contracts"] * CONTRACT_SIZE_BY_ASSET.get(symbol, 0.001) * \
          (fill - entry_px)
    _DAY_PNL_USD += pnl
    _write_trade_event({
        "ts": datetime.now(timezone.utc), "venue": "delta_india",
        "mode": broker.mode, "strategy": strategy_name,
        "symbol": symbol, "side": side,
        "event": exit_reason, "exit_price": fill,
        "contracts_closed": pos["contracts"], "pnl_usd": pnl,
        "unrealized_pct": unrealized_pct, "held_hours": held_h,
    })
    logger.info("%s EXIT (%s) at %s, pnl=%.2f", strategy_name, exit_reason, fill, pnl)
    return True


def _manage_shadow_positions(broker) -> None:
    """Apply v5 exit logic (stop/trail/max-hold) to each open shadow trade.
    Same thresholds as real positions; partial_tp is collapsed into a single
    final exit so the dashboard sees a clean 'closed at X% reason Y' row."""
    from datetime import datetime as _dt
    for sid, pos in list(_SHADOW_POSITIONS.items()):
        try:
            mark = broker.get_perp_mark(pos["symbol"])
            if mark is None or mark <= 0: continue
            sign = 1 if pos["side"] == "buy" else -1
            entry_px = float(pos["entry_px"])
            unreal_pct = sign * (mark - entry_px) / entry_px
            pos["peak_pct"] = max(pos.get("peak_pct", 0.0), unreal_pct)
            entry_dt = _dt.fromisoformat(pos["entry_ts"].replace("Z", "+00:00"))
            held_h = (_dt.now(timezone.utc) - entry_dt).total_seconds() / 3600
            exit_reason = None
            if held_h >= MAX_HOLD_HOURS:
                exit_reason = "max_hold"
            elif unreal_pct < -pos["stop_loss_pct"]:
                exit_reason = "stop_loss"
            elif pos["peak_pct"] >= pos["trail_peak_pct"] and \
                 (pos["peak_pct"] - unreal_pct) > pos["trail_giveback"]:
                exit_reason = "trail"
            if exit_reason:
                pos["status"]      = "closed"
                pos["exit_ts"]     = _dt.now(timezone.utc).isoformat()
                pos["exit_px"]     = mark
                pos["pnl_pct"]     = float(unreal_pct * 100)
                pos["held_hours"]  = float(held_h)
                pos["exit_reason"] = exit_reason
                _SHADOW_POSITIONS.pop(sid, None)
                logger.info("shadow %s %s closed at %s reason=%s pnl=%+0.2f%%",
                            pos["strategy"], pos["symbol"], mark, exit_reason,
                            pos["pnl_pct"])
        except Exception as e:
            logger.error("shadow manage error: %s", e)


# ── main tick ─────────────────────────────────────────────────────────────────
def tick_crypto_strategies() -> None:
    """Single tick — runs entry logic + position manager."""
    strategies = _get_strategies()
    broker = get_crypto_broker()
    _reset_day_pnl_if_needed()

    # Manage shadow positions every tick (cheap — just reads marks)
    _manage_shadow_positions(broker)

    # First: manage all open positions
    to_remove = []
    for name, pos in list(_OPEN_POSITIONS.items()):
        try:
            if _manage_open_position(name, broker, pos):
                to_remove.append(name)
        except Exception as e:
            logger.error("%s position mgmt error: %s", name, e, exc_info=True)
    for name in to_remove:
        del _OPEN_POSITIONS[name]

    if _check_kill_switch():
        logger.warning("kill switch active — no new entries this tick")
        return

    # Second: check for new entries
    for name, strat in strategies.items():
        if name in _OPEN_POSITIONS: continue
        try:
            decision = strat.on_tick()
        except Exception as e:
            logger.error("%s tick error: %s", name, e, exc_info=True); continue
        if decision is None: continue

        logger.info("%s SIGNAL: %s %s pred=%+0.3f%% strikes=%d size=%.1fx",
                     name, decision.side, decision.symbol,
                     decision.pred_pct, decision.n_strikes, decision.size_mult)
        _log_signal(decision)

        # sizing
        mark = broker.get_perp_mark(decision.symbol)
        if mark is None or mark <= 0: continue

        # Cap effective equity at real wallet balance (live mode only). Paper
        # mode keeps BASE_EQUITY_USD so backtest-style sizing works without a
        # funded account. With any non-zero USDT balance we attempt the order
        # at wallet-scaled size and let Delta accept/reject -- the user wants
        # visibility that the bot IS trying to trade, even on tiny accounts.
        # Only when balance is truly zero / unreadable do we shadow-log instead.
        effective_equity = BASE_EQUITY_USD
        wallet_blocked = False
        if broker.mode == "live":
            balance = broker.get_balance()
            if balance is None or balance <= 0:
                wallet_blocked = True
                shown = f"${balance:.2f}" if balance is not None else "unavailable"
                logger.warning("%s: wallet %s — recording shadow trade only "
                               "(fund Delta with USDT to enable live orders)",
                               name, shown)
            else:
                wallet_cap = balance * WALLET_SAFETY_BUFFER
                if wallet_cap < BASE_EQUITY_USD:
                    logger.info("%s: sizing capped by wallet $%.2f -> $%.0f "
                                "(env cap $%.0f)",
                                name, balance, wallet_cap, BASE_EQUITY_USD)
                    effective_equity = wallet_cap

        if wallet_blocked:
            import uuid
            shadow_id = uuid.uuid4().hex[:8]
            shadow = {
                "id":           shadow_id,
                "entry_ts":     datetime.now(timezone.utc).isoformat(),
                "strategy":     name,
                "symbol":       decision.symbol,
                "side":         decision.side,
                "entry_px":     mark,
                "pred_pct":     decision.pred_pct,
                "size_mult":    decision.size_mult,
                "status":       "open",
                "peak_pct":     0.0,
                "stop_loss_pct":   decision.stop_loss_pct,
                "trail_peak_pct":  decision.trail_peak_pct,
                "trail_giveback":  decision.trail_giveback,
            }
            _SHADOW_TRADES.append(shadow)
            _SHADOW_POSITIONS[shadow_id] = shadow
            del _SHADOW_TRADES[:-_MAX_SHADOW_TRADES]
            # Drop any open positions that fell out of the ring buffer
            _SHADOW_POSITIONS.update({
                t["id"]: t for t in _SHADOW_TRADES if t.get("status") == "open"
            })
            for k in list(_SHADOW_POSITIONS):
                if not any(t["id"] == k for t in _SHADOW_TRADES):
                    _SHADOW_POSITIONS.pop(k, None)
            continue

        notional = effective_equity * decision.size_mult
        contracts = _contracts_for_notional(decision.symbol, notional, mark)
        if contracts <= 0:
            logger.warning("%s: sizing produced 0 contracts (notional %.0f, mark %s)",
                            name, notional, mark); continue

        order = broker.place_order(
            symbol=decision.symbol, side=decision.side, size=contracts,
            order_type="market_order", tag=f"{name}_entry",
            leverage=LEVERAGE,
        )
        if not order.get("ok"):
            logger.error("%s entry failed: %s", name, order); continue

        _OPEN_POSITIONS[name] = {
            "symbol": decision.symbol, "side": decision.side,
            "entry_price": order.get("fill_price", mark),
            "entry_ts": time.time(),
            "contracts": contracts,
            "notional_usd": notional,
            "decision": decision,
            "peak_pct": 0.0,
        }
        _write_trade_event({
            "ts": datetime.now(timezone.utc), "venue": "delta_india",
            "mode": broker.mode, "strategy": name,
            "symbol": decision.symbol, "side": decision.side,
            "event": "entry", "entry_price": order.get("fill_price", mark),
            "contracts": contracts, "notional_usd": notional,
            "pred_pct": decision.pred_pct, "n_strikes": decision.n_strikes,
            "size_mult": decision.size_mult, "expiry": decision.expiry,
        })
        logger.info("%s ENTRY %d contracts at %s (notional $%.0f)",
                    name, contracts, order.get("fill_price"), notional)


# ── scheduler integration ─────────────────────────────────────────────────────
def init_crypto_runner(scheduler) -> None:
    if not _is_enabled():
        logger.info("crypto runner: DISABLED (set ENABLE_CRYPTO_RUNNER=1 to enable)")
        return
    broker = get_crypto_broker()
    mode = broker.mode
    logger.info("crypto runner enabled — mode=%s tick=%ds equity=$%.0f "
                "kill=-%.1f%% max_contracts=%d",
                mode, TICK_INTERVAL_SECONDS, BASE_EQUITY_USD,
                DAILY_LOSS_KILL_PCT * 100, MAX_LIVE_CONTRACTS)
    try:
        scheduler.add_job(
            tick_crypto_strategies, "interval",
            seconds=TICK_INTERVAL_SECONDS,
            id="crypto_synth_forward_tick", replace_existing=True,
            next_run_time=datetime.now(timezone.utc),
            max_instances=1, coalesce=True,
        )
    except Exception as e:
        logger.error("crypto runner init failed: %s", e)


def _is_enabled() -> bool:
    return os.environ.get("ENABLE_CRYPTO_RUNNER") == "1"


def get_state() -> dict:
    strategies = _get_strategies()
    return {
        "enabled": _is_enabled(),
        "mode": get_crypto_broker().mode,
        "killed": _KILLED,
        "day_pnl_usd": _DAY_PNL_USD,
        "strategies": list(strategies.keys()),
        "open_positions": {
            name: {
                "symbol": pos["symbol"], "side": pos["side"],
                "entry_price": pos["entry_price"],
                "contracts": pos["contracts"],
                "notional_usd": pos["notional_usd"],
                "held_hours": (time.time() - pos["entry_ts"]) / 3600,
                "peak_pct": pos.get("peak_pct", 0.0),
                "tp_taken": pos.get("tp_taken", False),
            } for name, pos in _OPEN_POSITIONS.items()
        },
        "shadow_trades":   list(_SHADOW_TRADES[-_MAX_SHADOW_TRADES:]),
        "shadow_summary":  _shadow_summary(),
    }


def _shadow_summary() -> dict:
    """Aggregate stats across the shadow-trade ring buffer."""
    open_n   = sum(1 for s in _SHADOW_TRADES if s.get("status") == "open")
    closed   = [s for s in _SHADOW_TRADES if s.get("status") == "closed"]
    wins     = [s for s in closed if (s.get("pnl_pct") or 0) > 0]
    losses   = [s for s in closed if (s.get("pnl_pct") or 0) <= 0]
    total    = sum((s.get("pnl_pct") or 0) for s in closed)
    avg_win  = (sum(s["pnl_pct"] for s in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(s["pnl_pct"] for s in losses) / len(losses)) if losses else 0.0
    return {
        "open":        open_n,
        "closed":      len(closed),
        "wins":        len(wins),
        "losses":      len(losses),
        "win_rate":    (len(wins) / len(closed) * 100) if closed else 0.0,
        "total_pct":   float(total),
        "avg_win_pct":  float(avg_win),
        "avg_loss_pct": float(avg_loss),
    }


def manual_kill():
    """Emergency stop — closes all positions and halts new entries."""
    global _KILLED
    _KILLED = True
    broker = get_crypto_broker()
    for name, pos in list(_OPEN_POSITIONS.items()):
        try:
            broker.place_order(pos["symbol"],
                               "sell" if pos["side"] == "buy" else "buy",
                               size=pos["contracts"], order_type="market_order",
                               reduce_only=True, tag=f"{name}_manual_kill")
            del _OPEN_POSITIONS[name]
        except Exception as e:
            logger.error("manual_kill %s failed: %s", name, e)
    logger.warning("MANUAL KILL — all positions closed, new entries halted")
