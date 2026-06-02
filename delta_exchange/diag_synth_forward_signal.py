"""
Synthetic-Forward Predictive Signal — Delta Exchange BTC
=========================================================
From the parity diagnostic we noticed the options market sometimes prices
BTC at a meaningful premium / discount to spot via the synthetic forward
(C - P + K). On 2026-03-10 the implied forward was +1.6% over spot, and
the actual settlement 3 days later was +3.3%.

Test the generality: for every (entry_t, expiry) sample with a meaningful
synthetic-forward deviation, check whether perp return from entry → expiry
moves in the predicted direction. If it does on average, we have a new
directional signal.

Read data/diag_parity.csv (produced by diag_parity.py) and pair each row
with the realized spot return to its expiry.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"


def main():
    print("Loading parity samples + perp...")
    par = pd.read_csv(DATA / "diag_parity.csv", parse_dates=["t", "expiry"])
    if par.empty:
        print("no parity data — run diag_parity.py first")
        return

    perp = pd.read_csv(DATA / "perp" / "BTCUSD_mark_1m.csv")
    perp["timestamp"] = pd.to_datetime(perp["time"], unit="s", utc=True)
    perp = perp.set_index("timestamp")["close"].sort_index()

    # for each row: future spot at expiry
    par["spot_at_expiry"] = perp.reindex(par["expiry"], method="nearest").values
    par["fwd_return_pct"] = (par["spot_at_expiry"] - par["spot"]) / par["spot"] * 100.0
    par["pred_dev_pct"]   = par["dev_bps"] / 100.0    # bps → percent

    # collapse multiple strikes per (t, expiry) into one row (median)
    g = par.groupby(["t", "expiry"]).agg(
        pred=("pred_dev_pct", "median"),
        real=("fwd_return_pct", "first"),
        spot=("spot", "first"),
        tt_hours=("tt_hours", "first"),
    ).reset_index()
    print(f"  total (t, expiry) samples: {len(g):,}")
    print(f"  perp range: {perp.index[0]} → {perp.index[-1]}")
    print()

    # decile analysis
    g_sorted = g.sort_values("pred").reset_index(drop=True)
    g_sorted["decile"] = pd.qcut(g_sorted["pred"], q=10, labels=False, duplicates="drop")
    decile_stats = g_sorted.groupby("decile").agg(
        n=("real", "count"),
        median_pred=("pred", "median"),
        mean_real=("real", "mean"),
        median_real=("real", "median"),
        hit_rate=("real", lambda x: (np.sign(x) == np.sign(g_sorted.loc[x.index, "pred"])).mean()),
    )
    print("Decile analysis — pred = synthetic-forward deviation %, real = perp return % to expiry")
    print("=" * 78)
    print(decile_stats.to_string())
    print()

    # by abs(pred) magnitude (the BIG dislocations)
    g["abs_pred"] = g["pred"].abs()
    big = g[g["abs_pred"] > 0.5]    # >50 bps
    medium = g[(g["abs_pred"] > 0.2) & (g["abs_pred"] <= 0.5)]
    small  = g[g["abs_pred"] <= 0.2]

    def line(label, sub):
        if sub.empty:
            print(f"  {label:<20} 0 samples")
            return
        directional_hit = (np.sign(sub["real"]) == np.sign(sub["pred"])).mean()
        avg_when_pred_pos = sub.loc[sub["pred"] > 0, "real"].mean()
        avg_when_pred_neg = sub.loc[sub["pred"] < 0, "real"].mean()
        print(f"  {label:<20} n={len(sub):,}  "
              f"hit_rate={directional_hit*100:.1f}%  "
              f"avg_realised_when_pred_+={avg_when_pred_pos:+.3f}%   "
              f"when_pred_-={avg_when_pred_neg:+.3f}%")

    print("By absolute prediction magnitude:")
    print("=" * 78)
    line("|dev| > 0.5%",  big)
    line("0.2% < |dev| ≤ 0.5%", medium)
    line("|dev| ≤ 0.2%",  small)
    print()

    # what would simple following give us?
    # strategy: at each sample, position = sign(pred). Realized return per unit = sign(pred) * real
    g["pnl_pct"] = np.sign(g["pred"]) * g["real"]
    # gate on size
    gated = g[g["abs_pred"] > 0.3]
    print(f"Simple signal-follower (gate: |pred| > 0.3%):")
    print(f"  trades: {len(gated):,}  "
          f"win rate: {(gated['pnl_pct'] > 0).mean()*100:.1f}%   "
          f"avg PnL/trade: {gated['pnl_pct'].mean():+.3f}%   "
          f"sum PnL: {gated['pnl_pct'].sum():+.1f}%")
    if not gated.empty:
        print(f"  median pred when entered: {gated['pred'].median():+.2f}%   "
              f"median realized: {gated['real'].median():+.2f}%")
    print()

    # samples per expiry
    print("Top 5 expiries by # of big (|dev|>0.5%) samples:")
    print((big.groupby("expiry").size().sort_values(ascending=False).head(5)).to_string())
    print()
    print("Big dislocations (|dev| > 1%) — possible alpha events:")
    huge = g[g["abs_pred"] > 1.0].sort_values("t")
    if not huge.empty:
        print(huge[["t", "expiry", "pred", "real", "spot"]].to_string(index=False))


if __name__ == "__main__":
    main()
