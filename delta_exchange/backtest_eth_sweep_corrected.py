"""
Corrected ETH parameter sweep.
Uses the live-config engine: fixed Rs 50k, 15x, fees, slippage,
block-after-loss, 1m entry grid.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from backtest_eth_live_config import load_eth_df, run_signals, run_fixed_capital


def run_variant(vol_filter_max, max_hold_candles, sl_pct, rr):
    # Monkey-patch globals in the imported module
    import backtest_eth_live_config as cfg
    cfg.VOL_FILTER_MAX = vol_filter_max
    cfg.MAX_HOLD_CANDLES = max_hold_candles
    cfg.SL_PCT = sl_pct
    cfg.TP_PCT = sl_pct * rr
    cfg.BLOCK_AFTER_LOSS_CANDLES = 180
    cfg.COOLDOWN_CANDLES = 60

    df = load_eth_df()
    trades = run_signals(df)
    res = run_fixed_capital(trades, cfg.BUDGET_INR, cfg.LEVERAGE)
    return trades, res


def main():
    df = load_eth_df()
    print(f"ETH corrected sweep: {df.index[0].date()} to {df.index[-1].date()}")
    print("Assumptions: fixed Rs 50k, 15x, 5bps/side fee, 2bps slippage both sides,")
    print("180m block-after-loss, 60m cooldown, 1m entry grid\n")
    print(f"{'vol':>5} {'hold':>5} {'SL%':>6} {'R:R':>5} {'Trades':>7} {'Wins':>5} {'WR%':>6} "
          f"{'Gross Rs':>12} {'MaxDD Rs':>12} {'DD%':>6} {'P/R':>6}")
    print("-" * 95)

    for vol in [0.0, 0.30, 0.34, 0.38, 0.50]:
        for hold in [240, 480]:
            for sl in [0.005, 0.007]:
                for rr in [5.0, 7.0]:
                    trades, res = run_variant(vol, hold, sl, rr)
                    wr = 100 * res["wins"] / res["trades"] if res["trades"] else 0
                    dd_pct = 100 * res["max_dd"] / 50000
                    pr = res["gross"] / max(res["max_dd"], 1)
                    print(f"{vol*100:>5.0f} {hold:>5} {sl*100:>6.2f} {rr:>5.1f} "
                          f"{res['trades']:>7} {res['wins']:>5} {wr:>6.1f} "
                          f"Rs{res['gross']:>11,.0f} Rs{res['max_dd']:>11,.0f} "
                          f"{dd_pct:>6.1f} {pr:>6.1f}")


if __name__ == "__main__":
    main()
