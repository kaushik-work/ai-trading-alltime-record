"""
Detailed ETH June backtest with full trade log + live execution lag analysis.

Perp data is freshly fetched into data/fresh_june_eth/perp.
Options data is copied from data/june_eth/options (historical expired-contract marks).

Usage:
    .venv/Scripts/python backtest_eth_june_detailed.py
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path
import numpy as np
import pandas as pd

from backtest_price_action_sweep import (
    load_perp, prepare, START_USD, LEVERAGE, CAPITAL_USE_PCT,
    LOOKBACK, TREND_LOOKBACK, MAX_HOLD_CANDLES, COOLDOWN_CANDLES,
)
from backtest_options_spread_overlay import (
    _load_option_catalog, _find_spread_legs, _spread_pnl_at_entry_exit,
    SLIPPAGE_BPS, OPTION_FEE_BPS,
)


def next_live_entry_time(t: pd.Timestamp) -> pd.Timestamp:
    """Live bot evaluates at :00/:15/:30/:45:30 UTC.
    Return the first live evaluation after signal candle close t."""
    minute = t.minute
    second = t.second
    # next 15-minute boundary
    next_boundary_min = ((minute // 15) + 1) * 15
    if next_boundary_min >= 60:
        # roll to next hour
        return (t + pd.Timedelta(hours=1)).replace(minute=0, second=30, microsecond=0)
    return t.replace(minute=next_boundary_min % 60, second=30, microsecond=0)


def run_eth_detailed(subdir: str, sym: str, sl_pct: float, rr: float,
                     otm_pct: float,
                     date_start: pd.Timestamp | None = None,
                     date_end: pd.Timestamp | None = None):
    underlying = sym.replace("USD", "")
    df = load_perp(subdir, sym)
    if date_start:
        df = df[df.index >= date_start]
    if date_end:
        df = df[df.index < date_end]

    catalog = _load_option_catalog(subdir)
    s = prepare(df, use_trend=True, retest_mode="wick_touch",
                body_pos_threshold=0.70, wick_touch_tol=0.0007)
    o, h, l, c = s["o"], s["h"], s["l"], s["c"]
    ts = df.index
    n = len(df)
    long_sig, short_sig = s["retest_long"], s["retest_short"]

    records = []
    pos = None
    cooldown = -1
    block_long_until = -1
    block_short_until = -1

    start_i = max(LOOKBACK, TREND_LOOKBACK) + 10
    for i in range(start_i, n - 1):
        t = ts[i]
        ci = c[i]

        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            hi, lo = h[i], l[i]
            reason = None
            exit_px = None

            if (sign > 0 and hi >= pos["tp"]) or (sign < 0 and lo <= pos["tp"]):
                reason = "tp"; exit_px = pos["tp"]
            else:
                stop = pos["sl"]
                if (sign > 0 and lo <= stop) or (sign < 0 and hi >= stop):
                    reason = "sl"; exit_px = stop
                elif i - pos["entry_idx"] >= MAX_HOLD_CANDLES:
                    reason = "hold"; exit_px = ci

            if reason:
                pnl = sign * (exit_px - pos["entry"]) / pos["entry"]
                perp_ret = pnl * LEVERAGE * CAPITAL_USE_PCT
                spread_ret, spread_details = 0.0, None
                if pos.get("legs"):
                    spread_pnl = _close_spread(pos, t, catalog, subdir, underlying)
                    if spread_pnl is not None:
                        spread_ret = spread_pnl * CAPITAL_USE_PCT
                        spread_details = pos.get("spread_details")
                records.append({
                    "signal_time": pos["signal_time"],
                    "bt_entry_time": pos["entry_time"],
                    "live_entry_time": pos["live_entry_time"],
                    "lag_seconds": (pos["live_entry_time"] - pos["entry_time"]).total_seconds(),
                    "side": pos["side"],
                    "entry_spot": pos["entry"],
                    "exit_spot": exit_px,
                    "exit_time": t,
                    "exit_reason": reason,
                    "sl_pct": pos["sl_pct"],
                    "tp_pct": pos["tp_pct"],
                    "perp_ret_pct": perp_ret * 100,
                    "spread_ret_pct": spread_ret * 100,
                    "legs": pos.get("legs"),
                    "spread_details": spread_details,
                    "status": "taken",
                })
                pos = None
                cooldown = i + COOLDOWN_CANDLES
                if pnl <= 0:
                    block_long_until = i + 180 if sign > 0 else block_long_until
                    block_short_until = i + 180 if sign < 0 else block_short_until
            continue

        if i < cooldown:
            continue

        tp_pct = sl_pct * rr
        next_open = o[i + 1]
        t_entry = ts[i + 1]
        live_entry = next_live_entry_time(t_entry)

        if long_sig[i] and i >= block_long_until:
            entry = next_open * (1 + SLIPPAGE_BPS / 10_000)
            stop_level = l[i] * (1 - SLIPPAGE_BPS / 10_000)
            sl_dist = max(sl_pct, (entry - stop_level) / entry)
            sl = entry * (1 - sl_dist)
            tp = entry * (1 + tp_pct)
            legs = _find_spread_legs(catalog, "C", underlying, t_entry, entry, otm_pct)
            pos = {
                "side": "long", "entry": entry, "sl": sl, "tp": tp,
                "entry_idx": i + 1, "entry_time": t_entry,
                "live_entry_time": live_entry,
                "signal_time": t, "sl_pct": sl_dist, "tp_pct": tp_pct,
                "legs": legs, "spread_side": "C",
            }
            if legs:
                pos["spread_details"] = _spread_details(subdir, legs, t_entry)
            continue

        if short_sig[i] and i >= block_short_until:
            entry = next_open * (1 - SLIPPAGE_BPS / 10_000)
            stop_level = h[i] * (1 + SLIPPAGE_BPS / 10_000)
            sl_dist = max(sl_pct, (stop_level - entry) / entry)
            sl = entry * (1 + sl_dist)
            tp = entry * (1 - tp_pct)
            legs = _find_spread_legs(catalog, "P", underlying, t_entry, entry, otm_pct)
            pos = {
                "side": "short", "entry": entry, "sl": sl, "tp": tp,
                "entry_idx": i + 1, "entry_time": t_entry,
                "live_entry_time": live_entry,
                "signal_time": t, "sl_pct": sl_dist, "tp_pct": tp_pct,
                "legs": legs, "spread_side": "P",
            }
            if legs:
                pos["spread_details"] = _spread_details(subdir, legs, t_entry)
            continue

    if pos:
        sign = 1 if pos["side"] == "long" else -1
        pnl = sign * (c[-1] - pos["entry"]) / pos["entry"]
        perp_ret = pnl * LEVERAGE * CAPITAL_USE_PCT
        spread_ret = 0.0
        if pos.get("legs"):
            spread_pnl = _close_spread(pos, ts[-1], catalog, subdir, underlying)
            if spread_pnl is not None:
                spread_ret = spread_pnl * CAPITAL_USE_PCT
        records.append({
            "signal_time": pos["signal_time"],
            "bt_entry_time": pos["entry_time"],
            "live_entry_time": pos["live_entry_time"],
            "lag_seconds": (pos["live_entry_time"] - pos["entry_time"]).total_seconds(),
            "side": pos["side"],
            "entry_spot": pos["entry"],
            "exit_spot": c[-1],
            "exit_time": ts[-1],
            "exit_reason": "eof",
            "sl_pct": pos["sl_pct"],
            "tp_pct": pos["tp_pct"],
            "perp_ret_pct": perp_ret * 100,
            "spread_ret_pct": spread_ret * 100,
            "legs": pos.get("legs"),
            "spread_details": pos.get("spread_details"),
            "status": "taken",
        })

    return records


def _spread_details(subdir, legs, t_entry):
    long_sym, short_sym = legs
    res = _spread_pnl_at_entry_exit(subdir, long_sym, short_sym, t_entry, t_entry)
    if res is None:
        return None
    entry_cost, _, _ = res
    return {"long": long_sym, "short": short_sym, "entry_cost": entry_cost}


def _close_spread(pos, t_exit, catalog, subdir, underlying):
    legs = pos.get("legs")
    if not legs:
        return None
    long_sym, short_sym = legs
    res = _spread_pnl_at_entry_exit(subdir, long_sym, short_sym,
                                    pos["entry_time"], t_exit)
    if res is None:
        return None
    entry_cost, exit_value, gross_pnl_ratio = res
    return gross_pnl_ratio - 4 * OPTION_FEE_BPS / 1e4


if __name__ == "__main__":
    subdir, sym, sl, rr = "fresh_june_eth", "ETHUSD", 0.007, 7
    otm_pct = sl * rr
    date_start = pd.Timestamp("2026-06-01", tz="UTC")
    date_end = pd.Timestamp("2026-07-01", tz="UTC")

    records = run_eth_detailed(subdir, sym, sl, rr, otm_pct, date_start, date_end)

    print(f"\n=== ETHUSD June detailed backtest ===")
    print(f"SL={sl*100:.2f}% | R:R 1:{rr} | spread wing {otm_pct*100:.2f}%\n")

    perp_rets = [r["perp_ret_pct"] / 100 for r in records]
    spread_rets = [r["spread_ret_pct"] / 100 for r in records if r["legs"]]
    taken = [r for r in records if r["status"] == "taken"]
    skipped = [r for r in records if r["status"] != "taken"]

    print(f"Total signals: {len(records)}")
    print(f"Spread trades: {len(spread_rets)} ({len(records) - len(spread_rets)} skipped/missed options)\n")

    # Summary
    def summarize(rets, label):
        if not rets:
            return
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
        print(f"{label}: trades={len(rets)} WR={wr:.1f}% PF={pf:.2f} ret={total:+.2f}% MaxDD={dd.max()*100:.2f}%")

    summarize(perp_rets, "PERP  ")
    summarize(spread_rets, "SPREAD")

    # Lag stats
    lags = [r["lag_seconds"] for r in taken]
    print(f"\nLive execution lag (backtest entry -> next 15m grid + 30s):")
    print(f"  min={min(lags):.0f}s | max={max(lags):.0f}s | mean={np.mean(lags):.0f}s | median={np.median(lags):.0f}s")

    # Trade log
    print("\n=== Trade log ===")
    print(f"{'Signal':19} {'BT entry':19} {'Live entry':19} {'Lag':>6} {'Side':>5} "
          f"{'Entry':>10} {'Exit':>10} {'Reason':>6} {'Perp%':>8} {'Sprd%':>8} {'Legs':>30}")
    for r in taken:
        legs_str = " / ".join(r["legs"]) if r["legs"] else "no options"
        print(f"{str(r['signal_time'])[:19]} {str(r['bt_entry_time'])[:19]} {str(r['live_entry_time'])[:19]} "
              f"{r['lag_seconds']:>6.0f} {r['side']:>5} {r['entry_spot']:>10.2f} {r['exit_spot']:>10.2f} "
              f"{r['exit_reason']:>6} {r['perp_ret_pct']:>+7.2f}% {r['spread_ret_pct']:>+7.2f}% {legs_str[:30]:>30}")

    if skipped:
        print("\n=== Skipped signals ===")
        for r in skipped:
            print(f"  {r['signal_time']} {r['side']} spot={r['entry_spot']:.2f}")
