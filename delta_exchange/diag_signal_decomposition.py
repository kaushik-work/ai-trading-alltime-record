"""
Signal Decomposition — which conditions make v5's signal predict best?
======================================================================
Reads a v5 trade log and decomposes performance by every signal dimension
we record at entry time. Tells us which subset of trades to KEEP and which
to DROP if we want to maximize Sharpe / win rate / R:R.

Decomposition axes:
  1. |pred%|         — signal magnitude (the strongest predictor in theory)
  2. n_strikes       — how many strikes agreed on direction (confidence)
  3. size_mult       — actual sizing applied (proxy for # 1)
  4. side            — LONG vs SHORT (is there directional asymmetry?)
  5. month / week    — time stability check
  6. day-of-week     — intraday/intraweek seasonality
  7. hour-of-day UTC — same
  8. consecutive PnL — autocorrelation (regime detection)

For each axis × bucket, report: n, win%, mean PnL, R:R, total PnL, Sharpe.

Then propose an optimal subset (top-quartile by each axis combined).

Usage:
  UNDERLYING=BTC  ./.venv/Scripts/python diag_signal_decomposition.py
  UNDERLYING=ETH  ./.venv/Scripts/python diag_signal_decomposition.py
"""

import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
from pathlib import Path
import numpy as np
import pandas as pd

UNDERLYING = os.environ.get("UNDERLYING", "BTC").upper()
DATA = (Path(__file__).parent / "data") if UNDERLYING == "BTC" \
       else (Path(__file__).parent / "data" / UNDERLYING.lower())

TRADE_LOG = DATA / "v5_trades.csv"
ANNUAL_FACTOR = 365


def perf_stats(pnl: np.ndarray) -> dict:
    if len(pnl) == 0:
        return {"n": 0, "win_pct": 0, "mean": 0, "median": 0,
                "rr": float("nan"), "total": 0, "sharpe": 0}
    n = len(pnl)
    wins = (pnl > 0).sum()
    avg_win  = pnl[pnl > 0].mean() if wins else 0
    avg_loss = pnl[pnl <= 0].mean() if wins < n else 0
    rr = abs(avg_win / avg_loss) if avg_loss else float("nan")
    sd = pnl.std(ddof=1) if n > 1 else 0
    sr = pnl.mean() / sd * math.sqrt(ANNUAL_FACTOR) if sd > 0 else 0
    return {"n": n, "win_pct": wins / n * 100,
            "mean": pnl.mean(), "median": np.median(pnl),
            "rr": rr, "total": pnl.sum(), "sharpe": sr}


def print_bucket_table(title: str, buckets: dict):
    print(f"\n  {title}")
    print(f"  {'bucket':<22} {'n':>4} {'win%':>6} {'mean$':>8} "
          f"{'R:R':>5} {'total$':>9} {'Sharpe':>7}")
    print("  " + "-" * 70)
    for label, pnl in buckets.items():
        s = perf_stats(np.asarray(pnl))
        rr_str = f"{s['rr']:>5.2f}" if np.isfinite(s['rr']) else "  —  "
        print(f"  {str(label):<22} {s['n']:>4} {s['win_pct']:>5.1f}% "
              f"{s['mean']:>+8.1f} {rr_str} "
              f"{s['total']:>+9,.0f} {s['sharpe']:>+7.2f}")


def main():
    if not TRADE_LOG.exists():
        print(f"No trade log at {TRADE_LOG} — run backtest_synth_forward_v5.py first "
              f"with UNDERLYING={UNDERLYING}")
        return

    df = pd.read_csv(TRADE_LOG, parse_dates=["entry_t", "exit_t"])
    df = df.sort_values("exit_t").reset_index(drop=True)
    print(f"Loaded {len(df)} trade legs for {UNDERLYING}   "
          f"({df['exit_t'].min().date()} → {df['exit_t'].max().date()})")
    print(f"Overall: net=${df['pnl_usd'].sum():+,.0f}  "
          f"win%={(df['pnl_usd']>0).mean()*100:.1f}  "
          f"mean=${df['pnl_usd'].mean():+.1f}")

    df["abs_pred"] = df["pred_pct"].abs() * 100        # to %
    df["side_label"] = df["side"].map({1: "LONG", -1: "SHORT"})
    df["month"] = df["entry_t"].dt.strftime("%Y-%m")
    df["dow"]   = df["entry_t"].dt.day_name()
    df["hour"]  = df["entry_t"].dt.hour
    df["exit_reason"] = df.get("exit_reason", pd.Series(["?"] * len(df)))

    # ── Axis 1: signal magnitude ───────────────────────────────────────────
    bins = [(0.4, 0.6), (0.6, 0.8), (0.8, 1.0), (1.0, 1.5), (1.5, 99)]
    bs = {f"|pred| {lo:.1f}-{hi:.1f}%":
              df[(df["abs_pred"] >= lo) & (df["abs_pred"] < hi)]["pnl_usd"].values
          for lo, hi in bins}
    print_bucket_table("1. by signal magnitude |pred%|", bs)

    # ── Axis 2: n_strikes confirming ───────────────────────────────────────
    bins2 = [(0, 10), (10, 20), (20, 30), (30, 99)]
    bs2 = {f"n_strikes {lo}-{hi}":
              df[(df["n_strikes"] >= lo) & (df["n_strikes"] < hi)]["pnl_usd"].values
           for lo, hi in bins2}
    print_bucket_table("2. by strike agreement (n_strikes)", bs2)

    # ── Axis 3: size_mult applied ──────────────────────────────────────────
    if "size_mult" in df.columns:
        bins3 = [(0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 3.01)]
        bs3 = {f"size {lo:.1f}-{hi:.1f}×":
                  df[(df["size_mult"] >= lo) & (df["size_mult"] < hi)]["pnl_usd"].values
               for lo, hi in bins3}
        print_bucket_table("3. by sizing multiplier", bs3)

    # ── Axis 4: LONG vs SHORT ──────────────────────────────────────────────
    bs4 = {sl: g["pnl_usd"].values for sl, g in df.groupby("side_label")}
    print_bucket_table("4. by direction", bs4)

    # ── Axis 5: month ──────────────────────────────────────────────────────
    bs5 = {m: g["pnl_usd"].values for m, g in df.groupby("month")}
    print_bucket_table("5. by month", bs5)

    # ── Axis 6: day-of-week ────────────────────────────────────────────────
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]
    bs6 = {d: df[df["dow"] == d]["pnl_usd"].values for d in dow_order}
    print_bucket_table("6. by day-of-week (entry)", bs6)

    # ── Axis 7: hour-of-day (UTC) bucketed ────────────────────────────────
    hour_buckets = [(0, 6), (6, 12), (12, 18), (18, 24)]
    bs7 = {f"UTC {lo:02d}-{hi:02d}":
              df[(df["hour"] >= lo) & (df["hour"] < hi)]["pnl_usd"].values
           for lo, hi in hour_buckets}
    print_bucket_table("7. by hour-of-day (UTC, entry)", bs7)

    # ── Axis 8: exit reason ────────────────────────────────────────────────
    bs8 = {r: g["pnl_usd"].values for r, g in df.groupby("exit_reason")}
    print_bucket_table("8. by exit reason", bs8)

    # ── Axis 9: autocorrelation — does previous trade winning predict next? ──
    df_sorted = df.sort_values("exit_t").reset_index(drop=True)
    prev_win = df_sorted["pnl_usd"].shift(1) > 0
    bs9 = {"prev_was_win": df_sorted[prev_win]["pnl_usd"].values,
           "prev_was_loss": df_sorted[~prev_win.fillna(False)]["pnl_usd"].values}
    print_bucket_table("9. by previous trade outcome", bs9)

    # ── Optimal subset: filter to top quartiles ────────────────────────────
    print("\n" + "=" * 80)
    print("  OPTIMAL SUBSET PROPOSAL")
    print("=" * 80)
    # candidate filters: |pred| > some threshold, n_strikes > some min, size > some min
    rules = [
        ("base v5",                lambda d: d),
        ("|pred| ≥ 0.6%",          lambda d: d[d["abs_pred"] >= 0.6]),
        ("|pred| ≥ 0.6% + n≥15",   lambda d: d[(d["abs_pred"] >= 0.6) & (d["n_strikes"] >= 15)]),
        ("|pred| ≥ 0.8%",          lambda d: d[d["abs_pred"] >= 0.8]),
        ("|pred| ≥ 1.0%",          lambda d: d[d["abs_pred"] >= 1.0]),
    ]
    print(f"  {'filter':<28} {'n':>4} {'win%':>6} {'mean$':>8} "
          f"{'R:R':>5} {'total$':>9} {'Sharpe':>7}")
    print("  " + "-" * 76)
    for name, fn in rules:
        sub = fn(df)
        s = perf_stats(sub["pnl_usd"].values)
        rr_str = f"{s['rr']:>5.2f}" if np.isfinite(s['rr']) else "  —  "
        print(f"  {name:<28} {s['n']:>4} {s['win_pct']:>5.1f}% "
              f"{s['mean']:>+8.1f} {rr_str} "
              f"{s['total']:>+9,.0f} {s['sharpe']:>+7.2f}")
    print()


if __name__ == "__main__":
    main()
