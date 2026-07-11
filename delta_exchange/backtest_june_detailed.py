"""
Detailed June backtest for BTC/ETH with live bid/ask filter.

Usage:
    .venv/Scripts/python backtest_june_detailed.py BTC
    .venv/Scripts/python backtest_june_detailed.py ETH

Fresh perp data is expected in data/fresh_june_<asset>/perp.
Options data is reused from data/june_<asset>/options.
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
    _load_option_catalog, _find_spread_legs,
    SLIPPAGE_BPS, OPTION_FEE_BPS,
)

# Bid/ask model: spread as fraction of option mark price.
# Example: 0.02 means the option trades at mark ±1%.
DEFAULT_BA_SPREAD = 0.02
# Filter: skip spread if total bid/ask cost > X% of the debit paid.
DEFAULT_BA_FILTER = 0.10


def next_live_entry_time(t: pd.Timestamp) -> pd.Timestamp:
    """Live bot evaluates at :00/:15/:30/:45:30 UTC."""
    minute = t.minute
    next_boundary_min = ((minute // 15) + 1) * 15
    if next_boundary_min >= 60:
        return (t + pd.Timedelta(hours=1)).replace(minute=0, second=30, microsecond=0)
    return t.replace(minute=next_boundary_min % 60, second=30, microsecond=0)


def _spread_pnl_with_ba(subdir: str, long_sym: str, short_sym: str,
                        t_entry: pd.Timestamp, t_exit: pd.Timestamp,
                        ba_spread: float):
    """Return (entry_cost_after_ba, exit_value_after_ba, net_pnl_ratio, ba_cost_pct).
    ba_spread is fraction of option mark (e.g. 0.02 for 2%).
    Buy long at ask = mark*(1+ba/2), sell short at bid = mark*(1-ba/2).
    Close: sell long at bid, buy short at ask.
    """
    long_s = _load_option_series(subdir, long_sym)
    short_s = _load_option_series(subdir, short_sym)

    long_mark_entry = _mark_at(long_s, t_entry)
    short_mark_entry = _mark_at(short_s, t_entry)
    long_mark_exit = _mark_at(long_s, t_exit)
    short_mark_exit = _mark_at(short_s, t_exit)

    if any(v is None or v <= 0 for v in [long_mark_entry, short_mark_entry, long_mark_exit, short_mark_exit]):
        return None

    # entry: pay ask on long, receive bid on short
    long_entry = long_mark_entry * (1 + ba_spread / 2)
    short_entry = short_mark_entry * (1 - ba_spread / 2)
    entry_cost = long_entry - short_entry

    # exit: receive bid on long, pay ask on short
    long_exit = long_mark_exit * (1 - ba_spread / 2)
    short_exit = short_mark_exit * (1 + ba_spread / 2)
    exit_value = long_exit - short_exit

    if entry_cost <= 0:
        return None

    ba_cost = (long_mark_entry * ba_spread) + (short_mark_entry * ba_spread)
    ba_cost_pct = ba_cost / entry_cost

    gross_pnl = exit_value - entry_cost
    gross_pnl_ratio = gross_pnl / entry_cost
    net_pnl_ratio = gross_pnl_ratio - 4 * OPTION_FEE_BPS / 1e4
    return entry_cost, exit_value, net_pnl_ratio, ba_cost_pct


def _load_option_series(subdir: str, sym: str) -> pd.Series:
    p = Path(__file__).parent / "data" / subdir / "options" / f"{sym}_mark_1m.csv"
    df = pd.read_csv(p)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


def _mark_at(series: pd.Series, t: pd.Timestamp) -> float | None:
    if series.empty:
        return None
    try:
        if t in series.index:
            return float(series.loc[t])
        idx = series.index.get_indexer([t], method="nearest")[0]
        if idx < 0:
            return None
        nearest_t = series.index[idx]
        if abs((nearest_t - t).total_seconds()) <= 120:
            return float(series.iloc[idx])
    except Exception:
        pass
    return None


def run_detailed(asset: str, sl_pct: float, rr: float,
                 ba_spread: float, ba_filter: float,
                 date_start: pd.Timestamp | None = None,
                 date_end: pd.Timestamp | None = None):
    sym = f"{asset}USD"
    subdir = f"fresh_june_{asset.lower()}"
    underlying = asset

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

    otm_pct = sl_pct * rr
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
                spread_ret = 0.0
                spread_skip_reason = None
                if pos.get("legs"):
                    res = _spread_pnl_with_ba(subdir, pos["legs"][0], pos["legs"][1],
                                              pos["entry_time"], t, ba_spread)
                    if res is None:
                        spread_skip_reason = "price_missing_exit"
                    else:
                        entry_cost, exit_value, net_pnl_ratio, ba_cost_pct = res
                        if ba_cost_pct > ba_filter:
                            spread_skip_reason = f"ba_filter_{ba_cost_pct:.1%}"
                            spread_ret = 0.0
                        else:
                            spread_ret = net_pnl_ratio * CAPITAL_USE_PCT
                            pos["spread_entry_cost"] = entry_cost
                            pos["spread_exit_value"] = exit_value
                            pos["ba_cost_pct"] = ba_cost_pct
                else:
                    spread_skip_reason = "no_options"

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
                    "spread_entry_cost": pos.get("spread_entry_cost"),
                    "spread_exit_value": pos.get("spread_exit_value"),
                    "ba_cost_pct": pos.get("ba_cost_pct"),
                    "spread_skip_reason": spread_skip_reason,
                    "status": "taken" if spread_skip_reason is None or spread_skip_reason == "price_missing_exit" else "spread_filtered",
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
                "legs": legs,
            }
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
                "legs": legs,
            }
            continue

    if pos:
        sign = 1 if pos["side"] == "long" else -1
        pnl = sign * (c[-1] - pos["entry"]) / pos["entry"]
        perp_ret = pnl * LEVERAGE * CAPITAL_USE_PCT
        spread_ret = 0.0
        spread_skip_reason = None
        if pos.get("legs"):
            res = _spread_pnl_with_ba(subdir, pos["legs"][0], pos["legs"][1],
                                      pos["entry_time"], ts[-1], ba_spread)
            if res is None:
                spread_skip_reason = "price_missing_exit"
            else:
                entry_cost, exit_value, net_pnl_ratio, ba_cost_pct = res
                if ba_cost_pct > ba_filter:
                    spread_skip_reason = f"ba_filter_{ba_cost_pct:.1%}"
                else:
                    spread_ret = net_pnl_ratio * CAPITAL_USE_PCT
        else:
            spread_skip_reason = "no_options"
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
            "spread_entry_cost": pos.get("spread_entry_cost"),
            "spread_exit_value": pos.get("spread_exit_value"),
            "ba_cost_pct": pos.get("ba_cost_pct"),
            "spread_skip_reason": spread_skip_reason,
            "status": "taken" if spread_skip_reason is None or spread_skip_reason == "price_missing_exit" else "spread_filtered",
        })

    return records


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


def main():
    asset = sys.argv[1].upper() if len(sys.argv) > 1 else "BTC"
    if asset not in ("BTC", "ETH"):
        print("Usage: backtest_june_detailed.py [BTC|ETH]")
        return

    sl = 0.006 if asset == "BTC" else 0.007
    rr = 7
    ba_spread = DEFAULT_BA_SPREAD
    ba_filter = DEFAULT_BA_FILTER

    date_start = pd.Timestamp("2026-06-01", tz="UTC")
    date_end = pd.Timestamp("2026-07-01", tz="UTC")

    records = run_detailed(asset, sl, rr, ba_spread, ba_filter, date_start, date_end)

    print(f"\n=== {asset}USD June detailed backtest ===")
    print(f"SL={sl*100:.2f}% | R:R 1:{rr} | spread wing {sl*rr*100:.2f}%")
    print(f"Bid/ask model: {ba_spread*100:.1f}% of option mark | filter max {ba_filter*100:.0f}% of debit\n")

    perp_rets = [r["perp_ret_pct"] / 100 for r in records]
    spread_rets = [r["spread_ret_pct"] / 100 for r in records]
    taken = [r for r in records if r["status"] == "taken"]
    filtered = [r for r in records if r["status"] == "spread_filtered"]
    no_options = [r for r in records if r.get("spread_skip_reason") == "no_options"]

    print(f"Total signals: {len(records)}")
    print(f"Spread trades passed filter: {len(taken)}")
    print(f"Filtered by bid/ask: {len([r for r in filtered if r.get('spread_skip_reason', '').startswith('ba_filter')])}")
    print(f"No options available: {len(no_options)}\n")

    mp = summarize(perp_rets)
    ms = summarize(spread_rets)
    print(f"PERP  : trades={mp['trades']} WR={mp['wr']:.1f}% PF={mp['pf']:.2f} ret={mp['ret']:+.2f}% MaxDD={mp['max_dd']:.2f}%")
    print(f"SPREAD: trades={ms['trades']} WR={ms['wr']:.1f}% PF={ms['pf']:.2f} ret={ms['ret']:+.2f}% MaxDD={ms['max_dd']:.2f}%")

    lags = [r["lag_seconds"] for r in records]
    if lags:
        print(f"\nLive lag (signal -> 15m grid entry): min={min(lags):.0f}s max={max(lags):.0f}s mean={np.mean(lags):.0f}s median={np.median(lags):.0f}s")

    print("\n=== Trade log ===")
    print(f"{'Signal':19} {'BT entry':19} {'Live entry':19} {'Lag':>6} {'Side':>5} "
          f"{'Entry':>10} {'Exit':>10} {'Reason':>6} {'Perp%':>8} {'Sprd%':>8} {'BA%':>7} {'Status':>16}")
    for r in records:
        ba_str = f"{r.get('ba_cost_pct', 0)*100:.1f}%" if r.get('ba_cost_pct') else "-"
        status = r.get("spread_skip_reason") or "taken"
        print(f"{str(r['signal_time'])[:19]} {str(r['bt_entry_time'])[:19]} {str(r['live_entry_time'])[:19]} "
              f"{r['lag_seconds']:>6.0f} {r['side']:>5} {r['entry_spot']:>10.2f} {r['exit_spot']:>10.2f} "
              f"{r['exit_reason']:>6} {r['perp_ret_pct']:>+7.2f}% {r['spread_ret_pct']:>+7.2f}% {ba_str:>7} {status[:16]:>16}")


if __name__ == "__main__":
    main()
