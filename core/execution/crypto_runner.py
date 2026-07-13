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
from core.risk_management import (
    TICK_INTERVAL_SECONDS, BASE_EQUITY_USD,
    CAPITAL_USE_PCT, BTC_CAPITAL_PCT, ETH_CAPITAL_PCT, capital_pct_for,
    DAILY_LOSS_KILL_PCT, MAX_LIVE_CONTRACTS, MAX_LIVE_CONTRACTS_BY_ASSET,
    LEVERAGE, CONTRACT_SIZE_BY_ASSET,
    ENABLE_CRYPTO_RUNNER, EXIT_REGIME,
    FIXED_CAPITAL_MODE, FIXED_CAPITAL_INR, USD_INR_RATE,
)
from strategies.price_action_sr import (
    ETHPriceActionSRSignal, MAX_HOLD_MINUTES,
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
    """Convert USD notional → integer contract count using Delta's contract size."""
    cs = CONTRACT_SIZE_BY_ASSET.get(symbol, 0.001)
    if mark <= 0: return 0
    n = int(notional_usd / (cs * mark))
    cap = MAX_LIVE_CONTRACTS_BY_ASSET.get(symbol, MAX_LIVE_CONTRACTS)
    return max(0, min(cap, n))


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


def tick_signal_sample() -> None:
    """Runs every 5 minutes — samples raw pred into history WITHOUT placing
    orders. Lets the persistence gate warm up between hourly entry decisions
    so an hourly entry tick has ~12 prior samples of same-sign context in the
    1h window, matching how backtest builds sig_history continuously."""
    strategies = _get_strategies()
    for name, strat in strategies.items():
        try:
            # signal_now() runs _compute_signal once, which records pred to
            # both _pred_trace (charting) and _sig_history (persistence) via
            # the base-class helpers. Returns None at the gate — we discard it.
            strat.signal_now()
        except Exception as e:
            logger.error("%s signal sample error: %s", name, e)


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
        if mark is None or mark <= 0:
            _record_missed_signal(decision, "no_mark",
                                  "perp mark unavailable")
            continue

        # Sizing: fixed-capital mode uses a constant INR budget per trade;
        # otherwise we compound from the live wallet pool.  Delta handles the
        # INR→USD conversion at trade time.
        effective_equity = BASE_EQUITY_USD
        wallet_blocked = False
        if broker.mode == "live":
            balance = broker.get_balance()
            if balance is None or balance <= 0:
                wallet_blocked = True
                shown = f"${balance:.2f}" if balance is not None else "unavailable"
                logger.warning("%s: wallet %s — recording shadow trade only "
                               "(deposit INR/USDT to enable live orders)",
                               name, shown)
                _record_missed_signal(decision, "wallet_empty",
                                      f"wallet balance {shown}")
            elif FIXED_CAPITAL_MODE:
                effective_equity = FIXED_CAPITAL_INR / USD_INR_RATE
                logger.info("%s: fixed-capital sizing Rs %.0f / %.2f = $%.2f",
                            name, FIXED_CAPITAL_INR, USD_INR_RATE, effective_equity)
            else:
                pct = _capital_pct_for(name)
                effective_equity = balance * pct
                logger.info("%s: sizing on wallet $%.2f × %.0f%% = $%.2f",
                            name, balance, pct * 100, effective_equity)

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
                "width_pct":    decision.pred_pct,
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
            logger.error("%s entry failed: %s", name, order)
            _record_missed_signal(decision, "order_failed",
                                  str(order.get("error") or order))
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
                "sample=5m entry=1min@*:05 UTC equity=$%.0f kill=-%.1f%% "
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

    try:
        # 1) Position-management tick — every 2s. Cheap mark reads + stop/trail.
        scheduler.add_job(
            tick_position_management, "interval",
            seconds=TICK_INTERVAL_SECONDS,
            id="crypto_position_management_tick", replace_existing=True,
            next_run_time=datetime.now(timezone.utc),
            max_instances=1, coalesce=True,
        )
        # 2) Signal-history warm-up — every 5 min. Records raw pred to
        #    _sig_history so the hourly entry tick has prior samples to gate on.
        scheduler.add_job(
            tick_signal_sample, "interval",
            minutes=5,
            id="crypto_signal_sample_tick", replace_existing=True,
            next_run_time=datetime.now(timezone.utc),
            max_instances=1, coalesce=True,
        )
        # 3) 1-minute entry decision — the price-action S/R strategy is
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
            broker.place_order(pos["symbol"],
                               "sell" if pos["side"] == "buy" else "buy",
                               size=pos["contracts"], order_type="market_order",
                               reduce_only=True, tag=f"{name}_manual_kill")
            del _OPEN_POSITIONS[name]
        except Exception as e:
            logger.error("manual_kill %s failed: %s", name, e)
    logger.warning("MANUAL KILL — all positions closed, new entries halted")
