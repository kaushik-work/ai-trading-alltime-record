"""Does the price-action S/R entry signal actually predict anything?

Exit tuning can only harvest an edge that exists in the entry. This measures
the entry in isolation: for every signal the strategy would fire, what is the
forward return over the next N minutes, and is it distinguishable from a
random bar in the same data?

Method
  - Build signals with the live dials (same prepare() the backtest uses).
  - For each signal, take the SIGNED forward return: +ret for longs, -ret for
    shorts, so a working signal produces positive numbers either way.
  - Baseline: the same signed-return computation over ALL bars, using the
    signal's own long/short mix, so we compare like with like.
  - Welch t-test on the difference, plus the edge in basis points.

Read it as: the signal needs to beat baseline by more than round-trip costs
(~10bps fees + ~4bps slippage) for any exit rule to make money.

Usage:
    python diag_signal_edge.py --subdir 6m
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from scipy import stats

import backtest_price_action_sweep as sweep
import backtest_live_config as B
from strategies import price_action_sr as live

HORIZONS = [30, 60, 120, 240, 480]
COST_BPS = 14.0   # 10bps round-trip fees + ~4bps slippage


def signals_for(df: pd.DataFrame, vol_filter_max: float):
    B.sync_sweep_to_live()
    return sweep.prepare(
        df, use_trend=True,
        retest_mode=live.RETEST_MODE,
        body_pos_threshold=live.BODY_POS_THRESHOLD,
        wick_touch_tol=live.WICK_TOUCH_TOL,
        min_volume_mult=live.MIN_VOLUME_MULT,
        rsi_period=live.RSI_PERIOD,
        rsi_long_max=live.RSI_LONG_MAX,
        rsi_short_min=live.RSI_SHORT_MIN,
        trend_slope_candles=live.TREND_SLOPE_CANDLES,
        trend_slope_min_pct=live.TREND_SLOPE_MIN_PCT,
        range_pct_min=live.RANGE_PCT_MIN,
        trading_hours=live.TRADING_HOURS,
        htf_align=live.HTF_ALIGN,
        htf_1h_slope_min_pct=live.HTF_1H_SLOPE_MIN_PCT,
        vol_filter_max=vol_filter_max,
        require_engulfing=live.REQUIRE_ENGULFING,
        pin_bar_wick_ratio=live.PIN_BAR_WICK_RATIO,
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--subdir", default="6m")
    p.add_argument("--symbols", nargs="+", default=["BTCUSD", "ETHUSD", "XAUTUSD"])
    args = p.parse_args()

    print("=" * 100)
    print("ENTRY SIGNAL EDGE — signed forward return vs a matched random baseline")
    print("=" * 100)
    print(f"Signal must beat baseline by more than ~{COST_BPS:.0f}bps for any exit rule to profit.\n")

    for sym in args.symbols:
        try:
            df = sweep.load_perp(args.subdir, sym)
        except Exception as e:
            print(f"{sym}: no data ({e})\n")
            continue
        d = B.live_dials_for(sym)
        s = signals_for(df, d["vol_filter_max"])
        c = s["c"]
        n = len(c)
        longs = np.where(s["retest_long"])[0]
        shorts = np.where(s["retest_short"])[0]
        n_sig = len(longs) + len(shorts)
        if n_sig < 10:
            print(f"{sym}: only {n_sig} signals — too few to test\n")
            continue

        long_share = len(longs) / n_sig
        print("-" * 100)
        print(f"{sym}   {n_sig} signals ({len(longs)} long / {len(shorts)} short)   "
              f"vol_filter {d['vol_filter_max'] or 'off'}")
        print(f"{'horizon':>9}{'signal bps':>13}{'baseline bps':>15}{'edge bps':>11}"
              f"{'t':>8}{'p':>10}{'signal WR':>12}{'verdict':>26}")

        for H in HORIZONS:
            fwd = np.full(n, np.nan)
            fwd[:n - H] = (c[H:] - c[:n - H]) / c[:n - H]

            sig_ret = np.concatenate([
                fwd[longs[longs < n - H]],
                -fwd[shorts[shorts < n - H]],
            ])
            sig_ret = sig_ret[~np.isnan(sig_ret)]

            # Baseline: every bar, mixed long/short in the signal's own ratio,
            # so direction bias cannot flatter the signal.
            valid = fwd[~np.isnan(fwd)]
            base = np.concatenate([valid, -valid])
            base_w = np.concatenate([
                np.full(len(valid), long_share),
                np.full(len(valid), 1 - long_share),
            ])
            base_mean = np.average(base, weights=base_w)

            if len(sig_ret) < 10:
                continue
            t, pval = stats.ttest_1samp(sig_ret, base_mean)
            edge_bps = (sig_ret.mean() - base_mean) * 1e4
            wr = (sig_ret > 0).mean() * 100

            if pval < 0.05 and edge_bps > COST_BPS:
                verdict = "EDGE, beats costs"
            elif pval < 0.05 and edge_bps > 0:
                verdict = "real but < costs"
            elif pval < 0.05:
                verdict = "significant NEGATIVE"
            else:
                verdict = "indistinguishable"

            print(f"{H:>8}m{sig_ret.mean() * 1e4:>13.1f}{base_mean * 1e4:>15.1f}"
                  f"{edge_bps:>11.1f}{t:>8.2f}{pval:>10.3f}{wr:>11.1f}%{verdict:>26}")
        print()

    print("=" * 100)
    print("An entry with no statistically significant edge above costs cannot be rescued")
    print("by tuning stops, targets or hold time — those only reshape the same distribution.")


if __name__ == "__main__":
    main()
