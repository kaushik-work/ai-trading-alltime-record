"""Granular SL / R:R sweep for price-action S/R strategy."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import argparse
from pathlib import Path
from backtest_price_action_sweep import run_asset, metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--btc-subdir", default=".")
    parser.add_argument("--eth-subdir", default="eth")
    parser.add_argument("--date-start", default=None)
    parser.add_argument("--date-end", default=None)
    args = parser.parse_args()

    import pandas as pd
    date_start = pd.Timestamp(args.date_start, tz="UTC") if args.date_start else None
    date_end = pd.Timestamp(args.date_end, tz="UTC") if args.date_end else None

    run_kw = dict(
        use_trend=True, trail_be=True,
        date_start=date_start, date_end=date_end,
        retest_mode="wick_touch",
        body_pos_threshold=0.70,
        wick_touch_tol=0.0007,
        min_volume_mult=1.0,
        rsi_period=14, rsi_long_max=100, rsi_short_min=0,
        trend_slope_candles=0, trend_slope_min_pct=0.0,
        range_pct_min=0.0,
        trading_hours="all",
        htf_align=False,
        require_engulfing=False,
        pin_bar_wick_ratio=0.0,
        cooldown_candles=60,
        block_after_loss_candles=180,
    )

    sls = [0.003, 0.004, 0.005, 0.006, 0.007]
    rrs = [3, 4, 5, 6, 7, 8, 10]

    print("=" * 130)
    print(f"Granular SL/R:R sweep  ({args.btc_subdir} / {args.eth_subdir})")
    if date_start or date_end:
        print(f"date range: {date_start} to {date_end}")
    print("=" * 130)

    for subdir, sym in [(args.btc_subdir, "BTCUSD"), (args.eth_subdir, "ETHUSD")]:
        print(f"\n--- {sym} ---")
        print(f"{'SL':>5} {'RR':>4} {'Trades':>7} {'WR':>7} {'PF':>7} {'P&L $':>11} {'Ret%':>8} {'MaxDD%':>8} {'MaxCL':>6}")
        best_pf = 0
        best_wr = 0
        best_ret = 0
        for sl in sls:
            for rr in rrs:
                trades, equity, curve = run_asset(subdir, sym, sl, rr, **run_kw)
                m = metrics(trades, equity, curve)
                if m["trades"] == 0:
                    continue
                print(f"{sl*100:>5.2f}% {rr:>4} {m['trades']:7d} {m['wr']:6.1f}% {m['pf']:7.2f} "
                      f"${m['pnl']:>+10.2f} {m['ret_pct']:>+7.2f}% {m['max_dd_pct']:>7.2f}% {m['max_cl']:>6d}")
                if m["pf"] > best_pf:
                    best_pf = m["pf"]
                    best_pf_cfg = (sl, rr, m)
                if m["wr"] > best_wr:
                    best_wr = m["wr"]
                    best_wr_cfg = (sl, rr, m)
                if m["ret_pct"] > best_ret:
                    best_ret = m["ret_pct"]
                    best_ret_cfg = (sl, rr, m)

        print(f"\nBest PF:  SL={best_pf_cfg[0]*100:.2f}% RR=1:{best_pf_cfg[1]}  WR={best_pf_cfg[2]['wr']:.1f}% PF={best_pf_cfg[2]['pf']:.2f} Ret={best_pf_cfg[2]['ret_pct']:.2f}%")
        print(f"Best WR:  SL={best_wr_cfg[0]*100:.2f}% RR=1:{best_wr_cfg[1]}  WR={best_wr_cfg[2]['wr']:.1f}% PF={best_wr_cfg[2]['pf']:.2f} Ret={best_wr_cfg[2]['ret_pct']:.2f}%")
        print(f"Best Ret: SL={best_ret_cfg[0]*100:.2f}% RR=1:{best_ret_cfg[1]}  WR={best_ret_cfg[2]['wr']:.1f}% PF={best_ret_cfg[2]['pf']:.2f} Ret={best_ret_cfg[2]['ret_pct']:.2f}%")


if __name__ == "__main__":
    main()
