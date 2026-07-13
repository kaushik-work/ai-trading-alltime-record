"""
High-return compounding sweep for ETH price-action strategy.
Assumes bracket orders for exits → low exit slippage.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from backtest_price_action_sweep import load_perp, prepare

SYMBOL = "ETHUSD"
BUDGET_INR = 50_000.0

# Realistic costs with bracket orders:
# - entry market order: 2 bps slippage
# - exit stop/target via bracket: 0.5 bps slippage (tighter)
# - Delta taker fee: 5 bps/side (10 bps round trip)
PERP_FEE_BPS = 5.0
ENTRY_SLIP_BPS = 2.0
EXIT_SLIP_BPS = 0.5


def load_eth_df():
    dfs = []
    for subdir in ["eth", "july_eth"]:
        try:
            dfs.append(load_perp(subdir, SYMBOL))
        except Exception:
            pass
    df = pd.concat(dfs).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[df.index >= pd.Timestamp("2026-04-01", tz="UTC")]
    return df


def run(df, leverage, sl_pct, rr, max_hold_candles, vol_filter_max,
        block_after_loss_candles, cooldown_candles):
    s = prepare(df, use_trend=True, retest_mode="wick_touch",
                body_pos_threshold=0.70, wick_touch_tol=0.0007,
                vol_filter_max=vol_filter_max)
    o, h, l, c = s["o"], s["h"], s["l"], s["c"]
    ts = df.index
    n = len(df)
    long_sig, short_sig = s["retest_long"], s["retest_short"]

    tp_pct = sl_pct * rr
    equity = BUDGET_INR
    peak = equity
    trades = 0
    wins = 0
    max_dd = 0.0
    pos = None
    cooldown = -1
    block_long_until = -1
    block_short_until = -1

    start_i = max(240, 1440) + 10
    for i in range(start_i, n - 1):
        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            hi, lo = h[i], l[i]
            reason = None
            exit_px = None

            # Target — bracket order, tight slippage
            if (sign > 0 and hi >= pos["tp"]) or (sign < 0 and lo <= pos["tp"]):
                reason = "tp"
                exit_px = pos["tp"] * (1 - sign * EXIT_SLIP_BPS / 1e4)
            else:
                # Stop — bracket order, but stop-market has some slip
                if (sign > 0 and lo <= pos["sl"]) or (sign < 0 and hi >= pos["sl"]):
                    reason = "sl"
                    exit_px = pos["sl"] * (1 - sign * EXIT_SLIP_BPS / 1e4)
                elif i - pos["entry_idx"] >= max_hold_candles:
                    reason = "hold"
                    exit_px = c[i] * (1 - sign * EXIT_SLIP_BPS / 1e4)

            if reason is not None:
                gross = sign * (exit_px - pos["entry"]) / pos["entry"]
                net = gross - 2 * PERP_FEE_BPS / 1e4
                # Compounding: deploy current equity at leverage
                pnl = equity * leverage * net
                equity += pnl
                peak = max(peak, equity)
                max_dd = max(max_dd, peak - equity)
                trades += 1
                if pnl > 0:
                    wins += 1
                pos = None
                cooldown = i + cooldown_candles
                if net <= 0:
                    if sign > 0:
                        block_long_until = i + block_after_loss_candles
                    else:
                        block_short_until = i + block_after_loss_candles
            continue

        if i < cooldown:
            continue

        next_open = o[i + 1]
        if long_sig[i] and i >= block_long_until:
            entry = next_open * (1 + ENTRY_SLIP_BPS / 1e4)
            stop_level = l[i] * (1 - ENTRY_SLIP_BPS / 1e4)
            sl_dist = max(sl_pct, (entry - stop_level) / entry)
            pos = {
                "side": "long", "entry": entry,
                "sl": entry * (1 - sl_dist),
                "tp": entry * (1 + tp_pct),
                "entry_idx": i + 1,
            }
            continue
        if short_sig[i] and i >= block_short_until:
            entry = next_open * (1 - ENTRY_SLIP_BPS / 1e4)
            stop_level = h[i] * (1 + ENTRY_SLIP_BPS / 1e4)
            sl_dist = max(sl_pct, (stop_level - entry) / entry)
            pos = {
                "side": "short", "entry": entry,
                "sl": entry * (1 + sl_dist),
                "tp": entry * (1 - tp_pct),
                "entry_idx": i + 1,
            }
            continue

    return {
        "trades": trades,
        "wins": wins,
        "equity": equity,
        "return_pct": 100 * (equity - BUDGET_INR) / BUDGET_INR,
        "max_dd": max_dd,
        "dd_pct": 100 * max_dd / BUDGET_INR,
    }


def main():
    df = load_eth_df()
    print(f"ETH compounding sweep: {df.index[0].date()} to {df.index[-1].date()}")
    print(f"Budget Rs {BUDGET_INR:,.0f}, bracket-order exits (entry slip {ENTRY_SLIP_BPS}bps, exit slip {EXIT_SLIP_BPS}bps), fee {PERP_FEE_BPS}bps/side\n")
    print(f"{'Lev':>4} {'SL%':>5} {'R:R':>4} {'hold':>5} {'vol':>4} {'bal':>4} "
          f"{'Trades':>7} {'Wins':>5} {'WR%':>6} {'Final Rs':>12} {'Return%':>9} {'MaxDD Rs':>12} {'DD%':>6}")
    print("-" * 105)

    for lev in [15, 20, 25, 30, 40, 50]:
        for sl in [0.005, 0.007]:
            for rr in [5.0, 7.0, 10.0]:
                for hold in [240, 480, 720]:
                    for vol in [0.0, 0.34, 0.38, 0.50]:
                        for bal in ["no", "yes"]:
                            vol_val = vol if bal == "yes" else 0.0
                            if vol_val == 0.0 and vol != 0.0:
                                continue
                            block = 180 if bal == "yes" else 0
                            cool = 60 if bal == "yes" else 0
                            res = run(df, lev, sl, rr, hold, vol_val, block, cool)
                            wr = 100 * res["wins"] / res["trades"] if res["trades"] else 0
                            print(f"{lev:>4} {sl*100:>5.2f} {rr:>4.1f} {hold:>5} {vol_val*100:>4.0f} {bal:>4} "
                                  f"{res['trades']:>7} {res['wins']:>5} {wr:>6.1f} "
                                  f"Rs{res['equity']:>11,.0f} {res['return_pct']:>8.1f}% "
                                  f"Rs{res['max_dd']:>11,.0f} {res['dd_pct']:>6.1f}%")


if __name__ == "__main__":
    main()
