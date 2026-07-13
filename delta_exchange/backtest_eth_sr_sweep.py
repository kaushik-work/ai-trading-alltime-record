"""
ETH S/R retest parameter sweep using the live signal engine.

Tests combinations of SL, R:R, vol filter, retest mode, and body-position
threshold with realistic fixed-capital costs. Outputs a ranked table.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from itertools import product
from pathlib import Path
import numpy as np
import pandas as pd
from backtest_price_action_sweep import load_perp, prepare

BUDGET_INR = 50_000.0
LEVERAGE = 15.0
PERP_FEE_BPS = 5.0
SLIPPAGE_BPS = 2.0
SYMBOL = "ETHUSD"

COOLDOWN_CANDLES = 60
BLOCK_AFTER_LOSS_CANDLES = 180
MAX_HOLD_CANDLES = 240


def load_eth_df():
    dfs = []
    for subdir in ["eth", "july_eth"]:
        try:
            dfs.append(load_perp(subdir, SYMBOL))
        except Exception as e:
            print(f"Warning: could not load {subdir}: {e}")
    df = pd.concat(dfs).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[df.index >= pd.Timestamp("2026-04-01", tz="UTC")]
    return df


def run_signals(df, sl_pct, rr, vol_filter_max, retest_mode, body_pos_threshold):
    tp_pct = sl_pct * rr
    s = prepare(df, use_trend=True, retest_mode=retest_mode,
                body_pos_threshold=body_pos_threshold, wick_touch_tol=0.0007,
                vol_filter_max=vol_filter_max)
    o, h, l, c = s["o"], s["h"], s["l"], s["c"]
    ts = df.index
    n = len(df)
    long_sig, short_sig = s["retest_long"], s["retest_short"]

    trades = []
    pos = None
    cooldown = -1
    block_long_until = -1
    block_short_until = -1
    start_i = max(240, 1440) + 10

    for i in range(start_i, n - 1):
        t = ts[i]

        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            reason = None
            exit_px = None
            if (sign > 0 and h[i] >= pos["tp"]) or (sign < 0 and l[i] <= pos["tp"]):
                reason = "tp"
                exit_px = pos["tp"] * (1 - sign * SLIPPAGE_BPS / 1e4)
            elif (sign > 0 and l[i] <= pos["sl"]) or (sign < 0 and h[i] >= pos["sl"]):
                reason = "sl"
                exit_px = pos["sl"] * (1 - sign * SLIPPAGE_BPS / 1e4)
            elif i - pos["entry_idx"] >= MAX_HOLD_CANDLES:
                reason = "hold"
                exit_px = c[i] * (1 - sign * SLIPPAGE_BPS / 1e4)

            if reason is not None:
                gross = sign * (exit_px - pos["entry"]) / pos["entry"]
                net = gross - 2 * PERP_FEE_BPS / 1e4
                trades.append({**pos, "exit_px": exit_px, "exit_reason": reason, "net_pnl_pct": net})
                pos = None
                cooldown = i + COOLDOWN_CANDLES
                if net <= 0:
                    if sign > 0:
                        block_long_until = i + BLOCK_AFTER_LOSS_CANDLES
                    else:
                        block_short_until = i + BLOCK_AFTER_LOSS_CANDLES
            continue

        if i < cooldown:
            continue

        next_open = o[i + 1]
        t_entry = ts[i + 1]

        if long_sig[i] and i >= block_long_until:
            entry = next_open * (1 + SLIPPAGE_BPS / 1e4)
            stop_level = l[i] * (1 - SLIPPAGE_BPS / 1e4)
            sl_dist = max(sl_pct, (entry - stop_level) / entry)
            pos = {
                "side": "long", "entry": entry,
                "sl": entry * (1 - sl_dist),
                "tp": entry * (1 + tp_pct),
                "entry_idx": i + 1, "entry_time": t_entry,
                "decision_time": t, "sl_dist": sl_dist,
            }
            continue

        if short_sig[i] and i >= block_short_until:
            entry = next_open * (1 - SLIPPAGE_BPS / 1e4)
            stop_level = h[i] * (1 + SLIPPAGE_BPS / 1e4)
            sl_dist = max(sl_pct, (stop_level - entry) / entry)
            pos = {
                "side": "short", "entry": entry,
                "sl": entry * (1 + sl_dist),
                "tp": entry * (1 - tp_pct),
                "entry_idx": i + 1, "entry_time": t_entry,
                "decision_time": t, "sl_dist": sl_dist,
            }

    return trades


def evaluate(trades):
    equity = BUDGET_INR
    peak = equity
    gross = 0.0
    wins = 0
    max_dd = 0.0
    for t in trades:
        pnl = BUDGET_INR * LEVERAGE * t["net_pnl_pct"]
        gross += pnl
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if pnl > 0:
            wins += 1
    n = len(trades)
    return {
        "trades": n, "wins": wins,
        "win_pct": 100 * wins / n if n else 0,
        "gross": gross, "max_dd": max_dd,
        "ret_pct": 100 * gross / BUDGET_INR,
        "pr": gross / max(max_dd, 1),
    }


def main():
    df = load_eth_df()
    print(f"ETH S/R sweep: {df.index[0].date()} to {df.index[-1].date()}, {len(df)} 1m bars")

    param_grid = {
        "sl_pct": [0.005, 0.007, 0.010],
        "rr": [7.0, 9.0],
        "vol_filter_max": [0.34, 999.0],
        "retest_mode": ["wick_touch"],
        "body_pos_threshold": [0.70],
    }

    results = []
    keys = list(param_grid.keys())
    combos = list(product(*param_grid.values()))
    print(f"Running {len(combos)} combinations...\n")

    for idx, vals in enumerate(combos, 1):
        params = dict(zip(keys, vals))
        trades = run_signals(df, **params)
        res = evaluate(trades)
        results.append({**params, **res})
        if idx % 10 == 0 or idx == len(combos):
            print(f"  {idx}/{len(combos)} done  (last gross={res['gross']:+.0f}  "
                  f"MaxDD={res['max_dd']:+.0f}  P/R={res['pr']:.2f})")

    results_df = pd.DataFrame(results)
    results_df = results_df.sort_values("pr", ascending=False)

    print("\n" + "=" * 100)
    print("Top 20 by Profit / MaxDD")
    print("=" * 100)
    print(results_df.head(20).to_string(index=False))

    print("\n" + "=" * 100)
    print("Top 10 by absolute gross P&L")
    print("=" * 100)
    print(results_df.sort_values("gross", ascending=False).head(10).to_string(index=False))

    out = Path(__file__).parent / "sr_sweep_results.csv"
    results_df.to_csv(out, index=False)
    print(f"\nFull results saved to {out}")


if __name__ == "__main__":
    main()
