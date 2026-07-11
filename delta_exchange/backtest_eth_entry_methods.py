"""
Compare ETH entry methods:
  1. Baseline: enter at next 1m candle open on signal.
  2. Live 15m grid: enter at next :00/:15/:30/:45:30 UTC.
  3. 5m grid + 2min persistence: wait for next 5m grid, require signal still valid 2min later.
  4. Limit order at S/R: place limit at signal candle low/high, fill if retested.

Usage:
    .venv/Scripts/python backtest_eth_entry_methods.py
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd

from backtest_price_action_sweep import (
    load_perp, prepare, START_USD, LEVERAGE, CAPITAL_USE_PCT,
    LOOKBACK, TREND_LOOKBACK, MAX_HOLD_CANDLES, COOLDOWN_CANDLES,
)
from backtest_options_spread_overlay import SLIPPAGE_BPS


def next_grid_time(t: pd.Timestamp, minutes: int) -> pd.Timestamp:
    """Next grid time: boundary of `minutes` + 30s grace."""
    boundary = (t.minute // minutes + 1) * minutes
    if boundary >= 60:
        return (t + pd.Timedelta(hours=1)).replace(minute=0, second=30, microsecond=0)
    return t.replace(minute=boundary % 60, second=30, microsecond=0)


def run_mode(df: pd.DataFrame, mode: str, sl: float, rr: float):
    s = prepare(df, use_trend=True, retest_mode="wick_touch",
                body_pos_threshold=0.70, wick_touch_tol=0.0007)
    o, h, l, c = s["o"], s["h"], s["l"], s["c"]
    r_high, r_low = s["r_high"], s["r_low"]
    ts = df.index
    n = len(df)
    long_sig, short_sig = s["retest_long"], s["retest_short"]

    rets = []
    trades = []
    pos = None
    cooldown = -1
    block_long = -1
    block_short = -1
    pending_limit = None  # for limit-sr mode

    start_i = max(LOOKBACK, TREND_LOOKBACK) + 10
    for i in range(start_i, n - 1):
        t = ts[i]
        ci = c[i]

        # Check pending limit order fill
        if pending_limit is not None:
            limit_price = pending_limit["limit_price"]
            side = pending_limit["side"]
            filled = False
            fill_px = None
            if side == "long" and l[i] <= limit_price:
                fill_px = min(limit_price, o[i])  # best of limit or open
                filled = True
            elif side == "short" and h[i] >= limit_price:
                fill_px = max(limit_price, o[i])
                filled = True

            if filled:
                sl_dist = pending_limit["sl_dist"]
                entry = fill_px
                if side == "long":
                    sl_p = entry * (1 - sl_dist)
                    tp_p = entry * (1 + sl_dist * rr)
                else:
                    sl_p = entry * (1 + sl_dist)
                    tp_p = entry * (1 - sl_dist * rr)
                pos = {
                    "side": side, "entry": entry, "sl": sl_p, "tp": tp_p,
                    "entry_idx": i, "entry_time": t,
                    "signal_time": pending_limit["signal_time"],
                }
                pending_limit = None
            else:
                pending_limit["candles_left"] -= 1
                if pending_limit["candles_left"] <= 0:
                    trades.append({
                        "signal_time": pending_limit["signal_time"],
                        "mode": mode, "status": "limit_expired",
                        "limit_price": pending_limit["limit_price"],
                    })
                    pending_limit = None

        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            reason = None
            exit_px = None
            if (sign > 0 and h[i] >= pos["tp"]) or (sign < 0 and l[i] <= pos["tp"]):
                reason = "tp"; exit_px = pos["tp"]
            elif (sign > 0 and l[i] <= pos["sl"]) or (sign < 0 and h[i] >= pos["sl"]):
                reason = "sl"; exit_px = pos["sl"]
            elif i - pos["entry_idx"] >= MAX_HOLD_CANDLES:
                reason = "hold"; exit_px = ci

            if reason:
                pnl = sign * (exit_px - pos["entry"]) / pos["entry"]
                rets.append(pnl * LEVERAGE * CAPITAL_USE_PCT)
                trades.append({
                    "signal_time": pos["signal_time"],
                    "entry_time": pos["entry_time"], "exit_time": t,
                    "side": pos["side"], "entry": pos["entry"], "exit": exit_px,
                    "reason": reason, "pnl_pct": pnl * 100, "mode": mode,
                    "status": "closed",
                })
                pos = None
                cooldown = i + COOLDOWN_CANDLES
                if pnl <= 0:
                    if sign > 0: block_long = i + 180
                    else: block_short = i + 180
            continue

        if i < cooldown or pending_limit is not None:
            continue

        # Determine entry logic based on mode
        is_long = long_sig[i] and i >= block_long
        is_short = short_sig[i] and i >= block_short
        if not is_long and not is_short:
            continue

        side = "long" if is_long else "short"

        if mode == "baseline":
            entry = o[i + 1] * (1 + SLIPPAGE_BPS / 10_000) if side == "long" else o[i + 1] * (1 - SLIPPAGE_BPS / 10_000)
            t_entry = ts[i + 1]
            stop_level = l[i] * (1 - SLIPPAGE_BPS / 10_000) if side == "long" else h[i] * (1 + SLIPPAGE_BPS / 10_000)
            sl_dist = max(sl, (entry - stop_level) / entry) if side == "long" else max(sl, (stop_level - entry) / entry)

        elif mode == "grid15":
            t_entry = next_grid_time(ts[i + 1], 15)
            idx = df.index.get_indexer([t_entry], method="nearest")[0]
            if idx < 0 or idx >= n:
                continue
            entry = df["open"].iloc[idx] * (1 + SLIPPAGE_BPS / 10_000) if side == "long" else df["open"].iloc[idx] * (1 - SLIPPAGE_BPS / 10_000)
            stop_level = l[i] * (1 - SLIPPAGE_BPS / 10_000) if side == "long" else h[i] * (1 + SLIPPAGE_BPS / 10_000)
            sl_dist = max(sl, (entry - stop_level) / entry) if side == "long" else max(sl, (stop_level - entry) / entry)

        elif mode == "grid5":
            t_entry = next_grid_time(ts[i + 1], 5)
            idx = df.index.get_indexer([t_entry], method="nearest")[0]
            if idx < 0 or idx >= n:
                continue
            entry = df["open"].iloc[idx] * (1 + SLIPPAGE_BPS / 10_000) if side == "long" else df["open"].iloc[idx] * (1 - SLIPPAGE_BPS / 10_000)
            stop_level = l[i] * (1 - SLIPPAGE_BPS / 10_000) if side == "long" else h[i] * (1 + SLIPPAGE_BPS / 10_000)
            sl_dist = max(sl, (entry - stop_level) / entry) if side == "long" else max(sl, (stop_level - entry) / entry)

        elif mode == "grid5_persist":
            # 2-minute persistence: price must still be in retest zone after 2 candles
            if i + 2 >= n:
                continue
            zone_pct = 0.004
            if side == "long":
                level = r_low[i]  # support level at signal time
                price = c[i + 2]
                if (price - level) / price > zone_pct:
                    continue  # price moved too far above support
            else:
                level = r_high[i]
                price = c[i + 2]
                if (level - price) / price > zone_pct:
                    continue  # price moved too far below resistance
            t_entry = next_grid_time(ts[i + 2], 5)
            idx = df.index.get_indexer([t_entry], method="nearest")[0]
            if idx < 0 or idx >= n:
                continue
            entry = df["open"].iloc[idx] * (1 + SLIPPAGE_BPS / 10_000) if side == "long" else df["open"].iloc[idx] * (1 - SLIPPAGE_BPS / 10_000)
            stop_level = l[i + 2] * (1 - SLIPPAGE_BPS / 10_000) if side == "long" else h[i + 2] * (1 + SLIPPAGE_BPS / 10_000)
            sl_dist = max(sl, (entry - stop_level) / entry) if side == "long" else max(sl, (stop_level - entry) / entry)

        elif mode == "limit_sr":
            # Place limit at signal candle low/high with small buffer
            if side == "long":
                limit_price = l[i] * 1.0002
            else:
                limit_price = h[i] * 0.9998
            pending_limit = {
                "side": side,
                "limit_price": limit_price,
                "sl_dist": sl,
                "signal_time": t,
                "candles_left": 10,  # expire after 10 minutes if not filled
            }
            continue

        else:
            raise ValueError(f"Unknown mode: {mode}")

        if side == "long":
            sl_p = entry * (1 - sl_dist)
            tp_p = entry * (1 + sl_dist * rr)
        else:
            sl_p = entry * (1 + sl_dist)
            tp_p = entry * (1 - sl_dist * rr)

        pos = {
            "side": side, "entry": entry, "sl": sl_p, "tp": tp_p,
            "entry_idx": idx if mode in ("grid15", "grid5_persist") else i + 1,
            "entry_time": t_entry,
            "signal_time": t,
        }

    if pos:
        sign = 1 if pos["side"] == "long" else -1
        pnl = sign * (c[-1] - pos["entry"]) / pos["entry"]
        rets.append(pnl * LEVERAGE * CAPITAL_USE_PCT)
        trades.append({
            "signal_time": pos["signal_time"],
            "entry_time": pos["entry_time"], "exit_time": ts[-1],
            "side": pos["side"], "entry": pos["entry"], "exit": c[-1],
            "reason": "eof", "pnl_pct": pnl * 100, "mode": mode,
            "status": "closed",
        })

    return rets, trades


def summarize(rets):
    if not rets:
        return {"trades": 0}
    eq = [START_USD]
    for r in rets:
        eq.append(eq[-1] * (1 + r))
    eq = np.array(eq)
    total = (eq[-1] - START_USD) / START_USD * 100
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    wr = len(wins) / len(rets) * 100
    pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / peak
    return {"trades": len(rets), "wr": wr, "pf": pf, "ret": total, "max_dd": dd.max() * 100}


if __name__ == "__main__":
    subdir = "fresh_june_eth"
    sym = "ETHUSD"
    sl, rr = 0.007, 7

    df = load_perp(subdir, sym)
    df = df[(df.index >= "2026-06-01") & (df.index < "2026-07-01")]

    modes = ["baseline", "grid15", "grid5", "grid5_persist", "limit_sr"]
    print(f"\n=== ETH entry-method comparison (June 2026) ===")
    print(f"SL={sl*100:.2f}% | R:R 1:{rr}\n")
    print(f"{'Mode':20} {'Trades':>7} {'WR':>7} {'PF':>6} {'Ret':>8} {'MaxDD':>8}")
    for mode in modes:
        rets, trades = run_mode(df, mode, sl, rr)
        m = summarize(rets)
        limit_expired = len([t for t in trades if t.get("status") == "limit_expired"])
        suffix = f" ({limit_expired} limit expired)" if limit_expired else ""
        if m['trades'] == 0:
            print(f"{mode:20}       0     —      —       —        —{suffix}")
        else:
            print(f"{mode:20} {m['trades']:>7} {m['wr']:>6.1f}% {m['pf']:>6.2f} {m['ret']:>+7.2f}% {m['max_dd']:>7.2f}%{suffix}")

    # Detailed trade log for grid5_persist and limit_sr
    for mode in ["grid5_persist", "limit_sr"]:
        _, trades = run_mode(df, mode, sl, rr)
        closed = [t for t in trades if t.get("status") == "closed"]
        print(f"\n=== {mode} trade log ===")
        print(f"{'Signal':19} {'Entry':19} {'Exit':19} {'Side':>5} {'EntryPx':>10} {'ExitPx':>10} {'Reason':>6} {'PnL%':>8}")
        for t in closed:
            print(f"{str(t['signal_time'])[:19]} {str(t['entry_time'])[:19]} {str(t['exit_time'])[:19]} "
                  f"{t['side']:>5} {t['entry']:>10.2f} {t['exit']:>10.2f} {t['reason']:>6} {t['pnl_pct']:>+7.2f}%")
