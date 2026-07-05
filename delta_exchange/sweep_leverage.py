"""Leverage sweep for price-action S/R strategy.

Shows return / MaxDD / ruin trade-offs at different effective exposures.
Effective exposure = LEVERAGE * CAPITAL_USE_PCT.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import argparse
import numpy as np
import backtest_price_action_sweep as bt


def run_with_exposure(subdir, sym, sl, rr, exposure, **kw):
    """Set LEVERAGE * CAPITAL_USE_PCT = exposure and run backtest."""
    old_lev, old_cap = bt.LEVERAGE, bt.CAPITAL_USE_PCT
    bt.CAPITAL_USE_PCT = 0.50
    bt.LEVERAGE = exposure / bt.CAPITAL_USE_PCT
    try:
        return bt.run_asset(subdir, sym, sl, rr, **kw)
    finally:
        bt.LEVERAGE, bt.CAPITAL_USE_PCT = old_lev, old_cap


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--btc-subdir", default=".")
    parser.add_argument("--eth-subdir", default="eth")
    args = parser.parse_args()

    import pandas as pd
    kw = dict(
        use_trend=True, trail_be=True,
        date_start=pd.Timestamp("2026-04-01", tz="UTC"),
        date_end=pd.Timestamp("2026-06-21", tz="UTC"),
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

    configs = [
        (args.btc_subdir, "BTCUSD", 0.006, 7),
        (args.eth_subdir, "ETHUSD", 0.007, 7),
    ]

    exposures = [1.0, 1.5, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0, 50.0]
    months = 2.67  # Apr 1 - Jun 20

    print("=" * 140)
    print("Leverage sweep — effective exposure = LEVERAGE × CAPITAL_USE_PCT")
    print("Capital_use_pct fixed at 0.50 (live-like); leverage shown is what Delta must provide.")
    print("Max single-trade loss is historical worst trade × exposure.  If it exceeds 100%,")
    print("one bad trade liquidates the account.")
    print("=" * 140)

    for subdir, sym, sl, rr in configs:
        print(f"\n--- {sym} SL={sl*100:.2f}% RR=1:{rr} ---")
        print(f"{'Exp':>6} {'Lev':>6} {'Ret%':>9} {'Ret/mo':>9} {'MaxDD%':>9} {'WorstTrade%':>13} {'Liquidated?':>12}")
        for exp in exposures:
            trades, equity, curve = run_with_exposure(subdir, sym, sl, rr, exp, **kw)
            m = bt.metrics(trades, equity, curve)
            if m["trades"] == 0:
                continue
            ret_mo = m["ret_pct"] / months
            worst_trade_pct = max((abs(t["pnl"]) for t in trades), default=0) * 100
            worst_equity_hit = worst_trade_pct * exp
            liquidated = "YES" if worst_equity_hit >= 100 else "no"
            print(f"{exp:>6.2f}x {exp/0.5:>6.1f}x {m['ret_pct']:>+8.2f}% {ret_mo:>+8.2f}% "
                  f"{m['max_dd_pct']:>8.2f}% {worst_equity_hit:>12.2f}% {liquidated:>12}")


if __name__ == "__main__":
    main()
