"""
Sensitivity of ETH live-config backtest to trading costs.
Tests fee + slippage combinations at vol=34 and vol=38.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest_eth_live_config as cfg
from backtest_eth_live_config import load_eth_df, run_signals, run_fixed_capital


def run_variant(vol_filter_max, fee_bps, slip_bps):
    cfg.VOL_FILTER_MAX = vol_filter_max
    cfg.PERP_FEE_BPS = fee_bps
    cfg.SLIPPAGE_BPS = slip_bps
    cfg.MAX_HOLD_CANDLES = 240
    cfg.SL_PCT = 0.007
    cfg.TP_PCT = cfg.SL_PCT * 7.0

    df = load_eth_df()
    trades = run_signals(df)
    res = run_fixed_capital(trades, cfg.BUDGET_INR, cfg.LEVERAGE)
    return trades, res


def main():
    df = load_eth_df()
    print(f"ETH cost sensitivity: {df.index[0].date()} to {df.index[-1].date()}")
    print(f"Fixed Rs 50k, 15x, 0.7% SL, 1:7, 4h hold\n")
    print(f"{'vol':>4} {'fee':>4} {'slip':>5} {'Trades':>7} {'Wins':>5} {'WR%':>6} "
          f"{'Gross Rs':>12} {'MaxDD Rs':>12} {'DD%':>6} {'P/R':>6}")
    print("-" * 80)

    for vol in [0.34, 0.38]:
        for fee in [2.0, 3.0, 4.0, 5.0]:
            for slip in [0.0, 1.0, 2.0]:
                trades, res = run_variant(vol, fee, slip)
                wr = 100 * res["wins"] / res["trades"] if res["trades"] else 0
                dd_pct = 100 * res["max_dd"] / 50000
                pr = res["gross"] / max(res["max_dd"], 1)
                print(f"{vol*100:>4.0f} {fee:>4.0f} {slip:>5.1f} "
                      f"{res['trades']:>7} {res['wins']:>5} {wr:>6.1f} "
                      f"Rs{res['gross']:>11,.0f} Rs{res['max_dd']:>11,.0f} "
                      f"{dd_pct:>6.1f} {pr:>6.1f}")


if __name__ == "__main__":
    main()
