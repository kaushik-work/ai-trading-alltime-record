"""
CryptoRunner — schedules crypto strategies in parallel with NSE BotRunner.

Sibling to core/bot_runner.py. Where BotRunner ticks NSE Q5 ensemble during
market hours, CryptoRunner ticks crypto strategies 24/7.

Lifecycle:
  • 15-minute entry tick — runs both strategies.
  • 2-second position-management tick — stops, targets, trails.
  • Entry: when strategy emits a SignalDecision, place market order on Delta.
  • Exit: position manager checks every tick for stop / partial TP / trail /
          max-hold / time-stop. ALL exit logic from v5 backtest is preserved.
  • Safety: kill switch on daily loss, max position cap, max concurrent.

How to enable (api/server.py startup):

    from core.execution.crypto_runner import init_crypto_runner
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
from core.strategy_toggles import is_strategy_enabled, is_instrument_enabled
from core.risk_management import (
    TICK_INTERVAL_SECONDS, BASE_EQUITY_USD,
    CAPITAL_USE_PCT, BTC_CAPITAL_PCT, ETH_CAPITAL_PCT, capital_pct_for,
    DAILY_LOSS_KILL_PCT, MAX_LIVE_CONTRACTS, MAX_LIVE_CONTRACTS_BY_ASSET,
    MAX_ORDER_NOTIONAL_MULT,
    LEVERAGE, CONTRACT_SIZE_BY_ASSET,
    ENABLE_CRYPTO_RUNNER, EXIT_REGIME, EXCHANGE_BRACKET_ENABLED,
    FIXED_CAPITAL_MODE, FIXED_CAPITAL_INR, USD_INR_RATE,
)
from strategies.price_action_sr import (
    ETHPriceActionSRSignal, MAX_HOLD_MINUTES, ASSET_DIALS, SL_PCT, RR_RATIO,
)

from strategies.crypto_base import CryptoSignalDecision

logger = logging.getLogger(__name__)

# Backwards-compat alias — old code calls _capital_pct_for(name); risk_mgmt
# exports the public name `capital_pct_for`.
_capital_pct_for = capital_pct_for

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
# Missed signals: signals that crossed the entry gate but did NOT result in a
# live order (empty wallet, API/order failure, zero sizing, kill switch, etc.).
# These are NOT tracked for P&L — they are purely for dashboard visibility.
_MISSED_SIGNALS: list[dict] = []
_MAX_MISSED_SIGNALS = 50


# ── strategies ────────────────────────────────────────────────────────────────
def _get_strategies():
    if not _STRATEGY_INSTANCES:
        broker = get_crypto_broker()
        # Running ETH-only: vol filter is ETH-specific and BTC filter degraded
        # backtest performance.  Add BTCPriceActionSRSignal back to re-enable BTC.
        classes = (ETHPriceActionSRSignal,)
        logger.info("using ETH-only price-action S/R strategy")
        for cls in classes:
            inst = cls(broker=broker)
            _STRATEGY_INSTANCES[inst.name] = inst
    return _STRATEGY_INSTANCES


# ── sizing ────────────────────────────────────────────────────────────────────
def _contracts_for_notional(symbol: str, notional_usd: float, mark: float) -> int:
    """Convert USD notional → integer contract count using Delta's contract size.

    Two independent ceilings apply: a per-asset contract cap, and a notional
    ceiling that holds even if CONTRACT_SIZE_BY_ASSET is wrong for a symbol.
    """
    cs = CONTRACT_SIZE_BY_ASSET.get(symbol, 0.001)
    if mark <= 0 or cs <= 0: return 0
    n = int(notional_usd / (cs * mark))
    cap = MAX_LIVE_CONTRACTS_BY_ASSET.get(symbol, MAX_LIVE_CONTRACTS)
    n = max(0, min(cap, n))
    # Notional ceiling — catches a wrong contract_value that the contract cap
    # would happily pass through.
    max_notional = notional_usd * MAX_ORDER_NOTIONAL_MULT
    if n * cs * mark > max_notional:
        clamped = int(max_notional / (cs * mark))
        logger.error("%s: sizing %d contracts = $%.2f exceeds %.1fx budget "
                     "$%.2f — clamping to %d. Verify contract_value=%s "
                     "against Delta /v2/products.",
                     symbol, n, n * cs * mark, MAX_ORDER_NOTIONAL_MULT,
                     notional_usd, clamped, cs)
        n = max(0, clamped)
    return n


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
        # CLAUDE.md rule: crypto collections use crypto_ prefix.
        db["crypto_signal_log"].insert_one({
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
        logger.warning("crypto_signal_log write failed: %s", e)


def _record_missed_signal(
    decision: CryptoSignalDecision | None,
    reason: str,
    detail: str = "",
) -> None:
    """Log a signal that crossed the gate but could not become a live trade.

    Reasons: kill_switch, wallet_empty, order_failed, zero_contracts.
    These are surfaced on the dashboard so the user sees every missed entry.
    """
    global _MISSED_SIGNALS
    _MISSED_SIGNALS.append({
        "id":        f"miss-{datetime.now(timezone.utc).strftime('%H%M%S')}-{len(_MISSED_SIGNALS)}",
        "ts":        datetime.now(timezone.utc).isoformat(),
        "strategy":  decision.name if decision else "global",
        "symbol":    decision.symbol if decision else "",
        "side":      decision.side if decision else "",
        "width_pct": decision.pred_pct if decision else 0.0,
        "reason":    reason,
        "detail":    detail,
    })
    _MISSED_SIGNALS = _MISSED_SIGNALS[-_MAX_MISSED_SIGNALS:]


def _write_trade_event(event: dict) -> None:
    try:
        from core import mongo
        db = mongo.get_db()
        if db is None: return
        db["crypto_trades"].insert_one(event)
    except Exception as e:
        logger.warning("crypto_trades write failed: %s", e)


# ── position management — forks on EXIT_REGIME ────────────────────────────────
def _manage_open_position(strategy_name: str, broker, pos: dict) -> bool:
    """Returns True if position was closed and should be removed.

    Two exit regimes, set via core.risk_management.EXIT_REGIME:
      - "pure_sltp"     : full exit on stop or target. dec.partial_tp_pct
                          is reinterpreted as the target threshold.
      - "trail_partial" : v5.5 baseline (partial TP at +1%, trail arms at
                          peak ≥0.5%, exits on 0.25% giveback).
    """
    global _DAY_PNL_USD
    symbol = pos["symbol"]
    side = pos["side"]                       # "buy" or "sell"
    sign = 1 if side == "buy" else -1
    entry_px = pos["entry_price"]
    held_min = (time.time() - pos["entry_ts"]) / 60

    current_mark = broker.get_perp_mark(symbol)
    if current_mark is None: return False

    unrealized_pct = sign * (current_mark - entry_px) / entry_px
    dec = pos["decision"]

    if EXIT_REGIME == "pure_sltp":
        # ── PURE BRACKET — full exit on stop or target ──
        exit_reason = None
        if held_min >= MAX_HOLD_MINUTES:
            exit_reason = "max_hold"
        elif unrealized_pct >= dec.partial_tp_pct:   # +1% target → full exit
            exit_reason = "target"
        elif unrealized_pct <= -dec.stop_loss_pct:   # -1.5% stop → full exit
            exit_reason = "stop_loss"
        if exit_reason is None: return False
    else:
        # ── TRAIL+PARTIAL — original v5.5 ──
        pos["peak_pct"] = max(pos.get("peak_pct", 0.0), unrealized_pct)
        # Partial TP at +1% — close half once
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
        exit_reason = None
        if held_min >= MAX_HOLD_MINUTES:
            exit_reason = "max_hold"
        elif unrealized_pct <= -dec.stop_loss_pct:
            exit_reason = "stop_loss"
        elif pos["peak_pct"] >= dec.trail_peak_pct and \
             (pos["peak_pct"] - unrealized_pct) > dec.trail_giveback:
            exit_reason = "trail"
        if exit_reason is None: return False

    # ── execute the full exit (shared by both regimes) ──
    if pos["contracts"] <= 0: return True
    # Drop the exchange bracket BEFORE closing. If the close ran first, the
    # bracket would still be armed against a position that no longer exists and
    # could re-open one on the next trigger.
    if EXCHANGE_BRACKET_ENABLED and pos.get("bracket", {}).get("ok"):
        if not broker.cancel_bracket(symbol):
            logger.error("%s: could not cancel exchange bracket before exit — "
                         "check open stop orders on %s manually",
                         strategy_name, symbol)
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
        "unrealized_pct": unrealized_pct, "held_minutes": held_min,
    })
    logger.info("%s EXIT (%s) at %s, pnl=%.2f", strategy_name, exit_reason, fill, pnl)
    # Notify strategy for block-after-loss logic
    try:
        strat = _get_strategies().get(strategy_name)
        if strat and hasattr(strat, "notify_trade_closed"):
            strat.notify_trade_closed(side, unrealized_pct)
    except Exception:
        pass
    return True


# ── position reconciliation ───────────────────────────────────────────────────
# _OPEN_POSITIONS is in-memory. Three things can put it out of step with the
# exchange, and every one of them used to go unnoticed:
#   1. the exchange bracket fills   → we keep managing a position that is gone
#   2. the container restarts       → a live position nobody is managing, and
#                                     the runner will happily open another
#   3. a partial fill or partial close → wrong size on every subsequent order
# This engine is the only thing that closes that loop.

def _position_side_and_size(row: dict) -> tuple[Optional[str], int]:
    """Delta position size is signed: >0 long, <0 short, 0 flat."""
    raw = row.get("size")
    try:
        size = int(float(raw))
    except (TypeError, ValueError):
        return None, 0
    if size == 0:
        return None, 0
    return ("buy" if size > 0 else "sell"), abs(size)


def _exchange_positions(broker) -> Optional[dict[str, dict]]:
    """{symbol: {side, contracts, entry_price}} from the exchange.

    Returns None when the exchange could not be read — callers must skip
    reconciliation entirely rather than assume flat.
    """
    rows = broker.get_positions()
    if rows is None:
        return None
    out: dict[str, dict] = {}
    for row in rows:
        symbol = row.get("product_symbol") or (row.get("product") or {}).get("symbol")
        if not symbol:
            pid = row.get("product_id")
            symbol = next((s for s in CONTRACT_SIZE_BY_ASSET
                           if broker.get_product_id(s) == pid), None)
        if not symbol:
            logger.warning("reconcile: could not resolve symbol for position row %s", row)
            continue
        side, contracts = _position_side_and_size(row)
        if side is None:
            continue
        try:
            entry = float(row.get("entry_price") or 0)
        except (TypeError, ValueError):
            entry = 0.0
        out[symbol] = {"side": side, "contracts": contracts, "entry_price": entry}
    return out


def _synth_decision(symbol: str, side: str) -> CryptoSignalDecision:
    """Bracket dials for a position we adopted rather than opened.

    Uses the strategy's own per-asset SL/RR so an adopted position is managed
    on exactly the same terms as one this runner entered.
    """
    underlying = symbol.replace("USD", "")
    dials = ASSET_DIALS.get(underlying, {"sl_pct": SL_PCT, "rr_ratio": RR_RATIO})
    sl = float(dials["sl_pct"])
    return CryptoSignalDecision(
        name="reconciled", symbol=symbol, side=side, pred_pct=0.0, n_strikes=0,
        stop_loss_pct=sl, partial_tp_pct=sl * float(dials["rr_ratio"]),
        trail_peak_pct=sl, trail_giveback=sl * 0.25,
        metadata={"adopted": True},
    )


def _strategy_for_symbol(symbol: str) -> Optional[str]:
    for name, strat in _get_strategies().items():
        if getattr(strat, "symbol", None) == symbol:
            return name
    return None


def reconcile_positions() -> dict:
    """Make _OPEN_POSITIONS agree with the exchange. Safe to call repeatedly."""
    broker = get_crypto_broker()
    if broker.mode != "live":
        return {"skipped": "paper mode"}

    exch = _exchange_positions(broker)
    if exch is None:
        logger.warning("reconcile: exchange unreachable — leaving local state untouched")
        return {"skipped": "exchange unreachable"}

    report = {"closed": [], "adopted": [], "resized": []}

    # 1. Tracked locally but flat (or gone) at the exchange.
    for name, pos in list(_OPEN_POSITIONS.items()):
        symbol = pos["symbol"]
        live = exch.get(symbol)
        if live and live["side"] == pos["side"]:
            continue
        mark = broker.get_perp_mark(symbol)
        entry = float(pos["entry_price"])
        sign = 1 if pos["side"] == "buy" else -1
        unreal_pct = (sign * (mark - entry) / entry) if (mark and entry) else 0.0
        pnl = (unreal_pct * float(pos.get("notional_usd") or 0))
        global _DAY_PNL_USD
        _DAY_PNL_USD += pnl
        _write_trade_event({
            "ts": datetime.now(timezone.utc), "venue": "delta_india",
            "mode": broker.mode, "strategy": name, "symbol": symbol,
            "side": pos["side"], "event": "reconciled_close",
            "exit_price": mark, "contracts_closed": pos["contracts"],
            "pnl_usd": pnl, "unrealized_pct": unreal_pct,
            # The real fill happened at the exchange; we only see the mark now.
            "approximate_pnl": True,
        })
        logger.warning("reconcile: %s %s closed at the exchange (bracket or manual). "
                       "Booking approx pnl %.2f at mark %s", name, symbol, pnl, mark)
        broker.cancel_bracket(symbol)
        try:
            strat = _get_strategies().get(name)
            if strat and hasattr(strat, "notify_trade_closed"):
                strat.notify_trade_closed(pos["side"], unreal_pct)
        except Exception:
            pass
        del _OPEN_POSITIONS[name]
        report["closed"].append(symbol)

    # 2. Live at the exchange but untracked — adopt it, then protect it.
    for symbol, live in exch.items():
        if any(p["symbol"] == symbol for p in _OPEN_POSITIONS.values()):
            continue
        name = _strategy_for_symbol(symbol)
        if name is None:
            logger.error("reconcile: ORPHAN position %s %s x%d at the exchange with no "
                         "matching strategy — this runner will not manage it. Close it "
                         "manually or add the strategy.",
                         symbol, live["side"], live["contracts"])
            continue
        mark = broker.get_perp_mark(symbol)
        entry = live["entry_price"] or mark or 0.0
        decision = _synth_decision(symbol, live["side"])
        _OPEN_POSITIONS[name] = {
            "symbol": symbol, "side": live["side"], "entry_price": entry,
            # Unknown open time — restart the max-hold clock rather than
            # force-closing something we cannot date.
            "entry_ts": time.time(),
            "contracts": live["contracts"],
            "notional_usd": live["contracts"] * CONTRACT_SIZE_BY_ASSET.get(symbol, 0.001) * (mark or entry),
            "decision": decision, "peak_pct": 0.0,
        }
        logger.warning("reconcile: ADOPTED untracked %s %s x%d @ %s — re-arming bracket",
                       symbol, live["side"], live["contracts"], entry)
        if EXCHANGE_BRACKET_ENABLED:
            # Clear whatever stops may be there and attach exactly one correct
            # bracket; Delta permits only one per open position anyway.
            broker.cancel_bracket(symbol)
            sign = 1 if live["side"] == "buy" else -1
            br = broker.place_bracket(
                symbol, live["side"],
                stop_price=entry * (1 - sign * decision.stop_loss_pct),
                take_profit_price=entry * (1 + sign * decision.partial_tp_pct),
            )
            _OPEN_POSITIONS[name]["bracket"] = br
            if not br.get("ok"):
                logger.error("reconcile: could not bracket adopted %s: %s",
                             symbol, br.get("error"))
        report["adopted"].append(symbol)

    # 3. Same side, different size — trust the exchange.
    for name, pos in _OPEN_POSITIONS.items():
        live = exch.get(pos["symbol"])
        if live and live["side"] == pos["side"] and live["contracts"] != pos["contracts"]:
            logger.warning("reconcile: %s size drift local=%d exchange=%d — taking exchange",
                           name, pos["contracts"], live["contracts"])
            pos["contracts"] = live["contracts"]
            report["resized"].append(pos["symbol"])

    if any(report.values()):
        logger.warning("reconcile summary: %s", report)
    return report


def tick_reconcile() -> None:
    """Scheduled reconciliation. Errors here must never kill the job."""
    try:
        reconcile_positions()
    except Exception as e:
        logger.error("reconcile tick failed: %s", e, exc_info=True)


def _open_shadow_trade(name: str, strat, decision: CryptoSignalDecision,
                       mark: float) -> None:
    """Record a paper trade for a signal the exchange would not take."""
    import uuid
    shadow_id = uuid.uuid4().hex[:8]
    shadow = {
        "id":              shadow_id,
        "entry_ts":        datetime.now(timezone.utc).isoformat(),
        "strategy":        name,
        "symbol":          decision.symbol,
        "side":            decision.side,
        "entry_px":        mark,
        "width_pct":       decision.pred_pct,
        "size_mult":       decision.size_mult,
        "status":          "open",
        "peak_pct":        0.0,
        "stop_loss_pct":   decision.stop_loss_pct,
        "partial_tp_pct":  decision.partial_tp_pct,
        "trail_peak_pct":  decision.trail_peak_pct,
        "trail_giveback":  decision.trail_giveback,
    }
    _SHADOW_TRADES.append(shadow)
    _SHADOW_POSITIONS[shadow_id] = shadow
    del _SHADOW_TRADES[:-_MAX_SHADOW_TRADES]
    # Drop any open positions that fell out of the ring buffer.
    for k in list(_SHADOW_POSITIONS):
        if not any(t["id"] == k for t in _SHADOW_TRADES):
            _SHADOW_POSITIONS.pop(k, None)
    if hasattr(strat, "notify_entry_taken"):
        strat.notify_entry_taken()


def _manage_shadow_positions(broker) -> None:
    """Apply the active exit regime to each open shadow trade. Shadow exits
    are always 'full close' (single row) regardless of regime — simpler for
    the dashboard. In pure_sltp mode we ignore peak/trail dials entirely and
    fire on stop or target. In trail_partial mode the original v5.5 trail
    logic applies (with partial_tp collapsed into the final exit row)."""
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
            held_min = (_dt.now(timezone.utc) - entry_dt).total_seconds() / 60
            exit_reason = None
            # Use partial_tp_pct as the target threshold under pure_sltp regime
            # (same numeric value, different semantics — see _manage_open_position).
            target_pct = pos.get("partial_tp_pct", 0.010)
            if EXIT_REGIME == "pure_sltp":
                if held_min >= MAX_HOLD_MINUTES:        exit_reason = "max_hold"
                elif unreal_pct >= target_pct:          exit_reason = "target"
                elif unreal_pct <= -pos["stop_loss_pct"]: exit_reason = "stop_loss"
            else:
                if held_min >= MAX_HOLD_MINUTES:        exit_reason = "max_hold"
                elif unreal_pct <= -pos["stop_loss_pct"]: exit_reason = "stop_loss"
                elif pos["peak_pct"] >= pos["trail_peak_pct"] and \
                     (pos["peak_pct"] - unreal_pct) > pos["trail_giveback"]:
                    exit_reason = "trail"
            if exit_reason:
                pos["status"]      = "closed"
                pos["exit_ts"]     = _dt.now(timezone.utc).isoformat()
                pos["exit_px"]     = mark
                pos["pnl_pct"]     = float(unreal_pct * 100)
                pos["held_minutes"]  = float(held_min)
                pos["exit_reason"] = exit_reason
                _SHADOW_POSITIONS.pop(sid, None)
                logger.info("shadow %s %s closed at %s reason=%s pnl=%+0.2f%%",
                            pos["strategy"], pos["symbol"], mark, exit_reason,
                            pos["pnl_pct"])
                # Notify strategy for block-after-loss logic in paper mode
                try:
                    strat = _get_strategies().get(pos["strategy"])
                    if strat and hasattr(strat, "notify_trade_closed"):
                        strat.notify_trade_closed(pos["side"], unreal_pct)
                except Exception:
                    pass
        except Exception as e:
            logger.error("shadow manage error: %s", e)


# ── main ticks (split: 2s position-mgmt, hourly entry-decision) ───────────────
# Backtest evaluates entries once per hour at HH:00 UTC against smoothed 1h
# option marks. The previous every-2s entry tick caused live-vs-backtest
# divergence: noisy real-time WS marks jitter pred above/below the 0.6% gate
# many times per minute, while backtest sees one clean print. Splitting the
# ticks closes that gap. Position management (stops/trails) still runs every
# 2s so exits remain millisecond-fast.
def tick_position_management() -> None:
    """Runs every 2s — manages open + shadow positions + kill check + day reset.
    Cheap (just reads marks). Does NOT consider new entries."""
    strategies = _get_strategies()  # ensure instantiated for warm-up
    broker = get_crypto_broker()
    _reset_day_pnl_if_needed()
    # Price-action strategies need frequent mark updates to build 1m candles.
    for name, strat in strategies.items():
        if hasattr(strat, "update_bars"):
            try:
                mark = broker.get_perp_mark(strat.symbol)
                if mark is not None:
                    strat.update_bars(mark)
            except Exception as e:
                logger.debug("%s bar update error: %s", name, e)
    _manage_shadow_positions(broker)
    to_remove = []
    for name, pos in list(_OPEN_POSITIONS.items()):
        try:
            if _manage_open_position(name, broker, pos):
                to_remove.append(name)
        except Exception as e:
            logger.error("%s position mgmt error: %s", name, e, exc_info=True)
    for name in to_remove:
        del _OPEN_POSITIONS[name]


def tick_entry_decisions() -> None:
    """Runs at top-of-hour — matches the backtest's hourly decision grid.
    Same entry logic as before; position management runs in a separate job."""
    strategies = _get_strategies()
    broker = get_crypto_broker()
    if _check_kill_switch():
        logger.info("15-min entry tick: kill switch active — skipping entries")
        for name, strat in strategies.items():
            if name in _OPEN_POSITIONS: continue
            _record_missed_signal(None, "kill_switch",
                                  "daily loss kill switch active")
        return

    for name, strat in strategies.items():
        if name in _OPEN_POSITIONS: continue
        if not is_strategy_enabled(name):
            continue
        try:
            decision = strat.on_tick()
        except Exception as e:
            logger.error("%s tick error: %s", name, e, exc_info=True); continue
        if decision is None: continue
        if not is_instrument_enabled(name, decision.symbol):
            logger.debug("%s: instrument %s disabled; skipping entry", name, decision.symbol)
            continue

        logger.info("%s SIGNAL: %s %s pred=%+0.3f%% strikes=%d size=%.1fx",
                     name, decision.side, decision.symbol,
                     decision.pred_pct, decision.n_strikes, decision.size_mult)
        _log_signal(decision)

        # sizing
        mark = broker.get_perp_mark(decision.symbol)
        if mark is None or mark <= 0:
            _record_missed_signal(decision, "no_mark",
                                  "perp mark unavailable")
            continue

        # Sizing. NOTE: we deliberately do NOT pre-check the wallet here.
        # The exchange is the authority on whether an order can be funded, and
        # asking first only adds a failure mode: a slow or failed balance call
        # used to suppress a valid signal entirely. We size, send, and let
        # Delta accept or reject — a rejection falls through to a shadow trade
        # below, so nothing is lost either way.
        effective_equity = BASE_EQUITY_USD
        if broker.mode == "live":
            if FIXED_CAPITAL_MODE:
                effective_equity = FIXED_CAPITAL_INR / USD_INR_RATE
                logger.info("%s: fixed-capital sizing Rs %.0f / %.2f = $%.2f",
                            name, FIXED_CAPITAL_INR, USD_INR_RATE, effective_equity)
            else:
                # Only this branch genuinely needs the balance, because size
                # is a fraction of it. Fall back to base equity if unreadable.
                balance = broker.get_balance()
                if balance and balance > 0:
                    effective_equity = balance * _capital_pct_for(name)
                    logger.info("%s: sizing on wallet $%.2f × %.0f%% = $%.2f",
                                name, balance, _capital_pct_for(name) * 100,
                                effective_equity)
                else:
                    logger.warning("%s: wallet unreadable — sizing on base equity $%.2f",
                                   name, effective_equity)

        notional = effective_equity * decision.size_mult
        contracts = _contracts_for_notional(decision.symbol, notional, mark)
        if contracts <= 0:
            logger.warning("%s: sizing produced 0 contracts (notional %.0f, mark %s)",
                            name, notional, mark)
            _record_missed_signal(decision, "zero_contracts",
                                  f"notional ${notional:.2f}, mark {mark}")
            continue

        order = broker.place_order(
            symbol=decision.symbol, side=decision.side, size=contracts,
            order_type="market_order", tag=f"{name}_entry",
            leverage=LEVERAGE,
        )
        if not order.get("ok"):
            logger.error("%s entry rejected by exchange: %s", name, order)
            _record_missed_signal(decision, "order_failed",
                                  str(order.get("error") or order))
            # Track it as a paper trade so an unfunded or rejected signal still
            # shows what it would have done. This used to happen via a wallet
            # pre-check; the exchange's own rejection is the better trigger.
            _open_shadow_trade(name, strat, decision, mark)
            continue

        _OPEN_POSITIONS[name] = {
            "symbol": decision.symbol, "side": decision.side,
            "entry_price": order.get("fill_price", mark),
            "entry_ts": time.time(),
            "contracts": contracts,
            "notional_usd": notional,
            "decision": decision,
            "peak_pct": 0.0,
        }
        if hasattr(strat, "notify_entry_taken"):
            strat.notify_entry_taken()

        # Park the stop and target at the exchange. The 2s tick still manages
        # this position (and owns max-hold, which a bracket cannot express),
        # but the position is no longer defenceless if this process dies.
        if EXCHANGE_BRACKET_ENABLED:
            entry_px = order.get("fill_price", mark)
            sign = 1 if decision.side == "buy" else -1
            stop_px = entry_px * (1 - sign * decision.stop_loss_pct)
            target_px = entry_px * (1 + sign * decision.partial_tp_pct)
            br = broker.place_bracket(decision.symbol, decision.side,
                                      stop_price=stop_px, take_profit_price=target_px)
            _OPEN_POSITIONS[name]["bracket"] = br
            if not br.get("ok"):
                logger.error("%s: EXCHANGE BRACKET FAILED (%s) — position is only "
                             "protected while this runner is alive",
                             name, br.get("error"))
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
    logger.info("crypto runner enabled — mode=%s regime=%s mgmt_tick=%ds "
                "entry=1min@*:05 UTC equity=$%.0f kill=-%.1f%% "
                "max_contracts=%d",
                mode, EXIT_REGIME, TICK_INTERVAL_SECONDS, BASE_EQUITY_USD,
                DAILY_LOSS_KILL_PCT * 100, MAX_LIVE_CONTRACTS)

    # Seed strategy candle buffers from Delta history so the bot is ready
    # immediately after deploy instead of waiting 24h for warmup.
    try:
        strategies = _get_strategies()
        for name, strat in strategies.items():
            if hasattr(strat, "backfill_history"):
                n = strat.backfill_history(lookback_hours=24)
                logger.info("%s: seeded %d historical candles", name, n)
                # Run one signal evaluation to populate _last_state (trend,
                # range, vol) so the dashboard is useful immediately.
                if hasattr(strat, "signal_now"):
                    strat.signal_now()
    except Exception as e:
        logger.warning("crypto runner history backfill failed: %s", e)

    # Adopt or clear anything already at the exchange BEFORE the entry job can
    # run. Without this a restart with a live position would leave it
    # unmanaged and immediately open a second one on the next signal.
    try:
        logger.info("crypto runner: startup reconciliation — %s", reconcile_positions())
    except Exception as e:
        logger.error("crypto runner: startup reconciliation failed: %s", e, exc_info=True)

    try:
        # 1) Position-management tick — every 2s. Cheap mark reads + stop/trail.
        scheduler.add_job(
            tick_position_management, "interval",
            seconds=TICK_INTERVAL_SECONDS,
            id="crypto_position_management_tick", replace_existing=True,
            next_run_time=datetime.now(timezone.utc),
            max_instances=1, coalesce=True,
        )
        # 2) 1-minute entry decision — the price-action S/R strategy is
        #    intrinsically 1m-candle based. A 15-minute grid was inherited from
        #    the old options strategy and was shown to miss ~88% of valid setups
        #    in the corrected backtest. Evaluate every minute at :05s so the
        #    just-completed 1m candle is fully formed and WS marks have settled.
        scheduler.add_job(
            tick_entry_decisions, "cron",
            minute="*", second=5, timezone="UTC",
            id="crypto_1min_entry_tick", replace_existing=True,
            max_instances=1, coalesce=True,
        )
        # 3) Position reconciliation — every 30s. The exchange bracket can
        #    close a position without telling us, and a restart can leave one
        #    untracked; this is the only job that resolves either.
        scheduler.add_job(
            tick_reconcile, "interval",
            seconds=30,
            id="crypto_reconcile_tick", replace_existing=True,
            max_instances=1, coalesce=True,
        )
        # 4) Wallet heartbeat — every 5 min, log Delta wallet breakdown.
        scheduler.add_job(
            _wallet_heartbeat, "interval",
            minutes=5,
            id="crypto_wallet_heartbeat", replace_existing=True,
            next_run_time=datetime.now(timezone.utc),
            max_instances=1, coalesce=True,
        )
    except Exception as e:
        logger.error("crypto runner init failed: %s", e)


def _is_enabled() -> bool:
    return ENABLE_CRYPTO_RUNNER


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
                # Bracket levels so the dashboard can show distance-to-stop
                # and distance-to-target rather than just a position count.
                "stop_loss_pct": pos["decision"].stop_loss_pct,
                "target_pct": pos["decision"].partial_tp_pct,
                "max_hold_minutes": MAX_HOLD_MINUTES,
            } for name, pos in _OPEN_POSITIONS.items()
        },
        "shadow_trades":   list(_SHADOW_TRADES[-_MAX_SHADOW_TRADES:]),
        "shadow_summary":  _shadow_summary(),
        "missed_signals":  list(_MISSED_SIGNALS[-_MAX_MISSED_SIGNALS:]),
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


def _wallet_heartbeat() -> None:
    """Log the live Delta wallet breakdown every 5 minutes (scheduled job).
    Shows the user exactly what assets are held and what's tradeable, even
    when the dashboard would otherwise display a dash."""
    broker = get_crypto_broker()
    if broker.mode != "live":
        return
    try:
        # Bust the 15s cache so the heartbeat fetches a truly current view.
        broker._bal_cache = {"value": -1.0, "ts": 0.0}
        breakdown = broker.get_wallet_breakdown()
    except Exception as e:
        logger.error("wallet heartbeat error: %s", e)
        return
    if not breakdown:
        logger.warning("wallet heartbeat: empty breakdown — auth or API issue?")
        return
    usd = float(breakdown.get("usd_total", 0))
    inr = float(breakdown.get("inr_balance", 0))
    by_asset = breakdown.get("by_asset", {})
    rate = float(os.environ.get("USD_INR_RATE", "86"))
    total_usd = usd + (inr / rate if rate > 0 else 0)
    if FIXED_CAPITAL_MODE:
        deploy = FIXED_CAPITAL_INR / USD_INR_RATE
        logger.info(
            "wallet heartbeat: pool=$%.2f (USD=$%.2f + ₹%.0f @ %s) "
            "→ FIXED-CAPITAL deploy Rs %.0f = $%.2f per cycle  by_asset=%s",
            total_usd, usd, inr, rate, FIXED_CAPITAL_INR, deploy, by_asset,
        )
    else:
        deploy = total_usd * CAPITAL_USE_PCT
        logger.info(
            "wallet heartbeat: pool=$%.2f (USD=$%.2f + ₹%.0f @ %s) "
            "→ deploy %.0f%% = $%.2f per cycle  by_asset=%s",
            total_usd, usd, inr, rate, CAPITAL_USE_PCT * 100, deploy, by_asset,
        )


def manual_kill():
    """Emergency stop — closes all positions and halts new entries."""
    global _KILLED
    _KILLED = True
    broker = get_crypto_broker()
    for name, pos in list(_OPEN_POSITIONS.items()):
        try:
            if EXCHANGE_BRACKET_ENABLED and pos.get("bracket", {}).get("ok"):
                broker.cancel_bracket(pos["symbol"])
            order = broker.place_order(pos["symbol"],
                                       "sell" if pos["side"] == "buy" else "buy",
                                       size=pos["contracts"], order_type="market_order",
                                       reduce_only=True, tag=f"{name}_manual_kill")
            if order.get("ok"):
                del _OPEN_POSITIONS[name]
            else:
                logger.error("manual_kill %s order failed: %s", name, order)
        except Exception as e:
            logger.error("manual_kill %s failed: %s", name, e)
    logger.warning("MANUAL KILL — all positions closed, new entries halted")
