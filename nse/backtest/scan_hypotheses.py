"""Scan many NIFTY intraday hypotheses at once, with a real hold-out.

Rationale: fitting a pattern to past data is trivial and worthless. What
matters is whether an effect survives data it was never chosen on. So every
hypothesis is measured on three disjoint periods:

    TRAIN     2021-2023   where you are allowed to look and iterate
    VALIDATE  2024        where you check a shortlist
    TEST      2025-2026   touched ONCE, at the end, never for selection

A hypothesis is only interesting if the sign and rough magnitude hold across
all three. Anything that only works in TRAIN is noise, and with ~40 hypotheses
some will look significant in TRAIN by pure chance - roughly two at p<0.05
even if every one is worthless. That is why the hold-out exists.

Metric is the same one that settled the crypto question: SIGNED forward
return (+ret for longs, -ret for shorts) against a baseline of all bars using
the same long/short mix, so directional drift cannot flatter a signal.

Usage:
    python -m nse.backtest.scan_hypotheses
    python -m nse.backtest.scan_hypotheses --horizons 15 30 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd
from scipy import stats

from nse.backtest.nifty_loader import load_spot

SPLITS = {
    "TRAIN 21-23": (2021, 2023),
    "VALID 24":    (2024, 2024),
    "TEST  25-26": (2025, 2026),
}


# ── indicators ────────────────────────────────────────────────────────────────
def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, min_periods=n).mean()
    dn = (-d).clip(lower=0).ewm(alpha=1 / n, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Everything is computed WITHIN a day to avoid overnight contamination."""
    g = df.groupby("date", sort=False)["close"]
    df["ret1"] = g.pct_change()
    df["bar"] = df.groupby("date", sort=False).cumcount()
    df["ma20"] = g.transform(lambda x: x.rolling(20, min_periods=20).mean())
    df["ma60"] = g.transform(lambda x: x.rolling(60, min_periods=60).mean())
    df["sd20"] = g.transform(lambda x: x.rolling(20, min_periods=20).std())
    df["rsi14"] = g.transform(lambda x: rsi(x, 14))
    df["hh30"] = g.transform(lambda x: x.rolling(30, min_periods=30).max())
    df["ll30"] = g.transform(lambda x: x.rolling(30, min_periods=30).min())
    df["dayopen"] = df.groupby("date", sort=False)["close"].transform("first")
    # Opening range = first 15 minutes.
    first15 = df[df["bar"] < 15].groupby("date")["close"]
    df = df.merge(first15.max().rename("or_hi"), on="date", how="left")
    df = df.merge(first15.min().rename("or_lo"), on="date", how="left")
    df["z20"] = (df["close"] - df["ma20"]) / df["sd20"].replace(0, np.nan)
    df["from_open"] = df["close"] / df["dayopen"] - 1
    return df


def hypotheses(d: pd.DataFrame) -> dict[str, pd.Series]:
    """name -> signal series: +1 long, -1 short, 0/NaN no position."""
    bar, z, r = d["bar"], d["z20"], d["rsi14"]
    H: dict[str, pd.Series] = {}

    # Mean reversion on stretch from the intraday mean
    for k in (1.5, 2.0, 2.5):
        H[f"MR z20 >{k}"] = np.where(z > k, -1, np.where(z < -k, 1, 0))
    for lo, hi in ((25, 75), (20, 80), (15, 85)):
        H[f"MR rsi {lo}/{hi}"] = np.where(r > hi, -1, np.where(r < lo, 1, 0))

    # Momentum / breakout of the rolling 30m range
    brk_up = (d["close"] >= d["hh30"]) & (d["hh30"].notna())
    brk_dn = (d["close"] <= d["ll30"]) & (d["ll30"].notna())
    H["MOM 30m breakout"] = np.where(brk_up, 1, np.where(brk_dn, -1, 0))
    H["FADE 30m breakout"] = np.where(brk_up, -1, np.where(brk_dn, 1, 0))

    # Opening-range break, only after the range is formed
    orb_up = (bar >= 15) & (d["close"] > d["or_hi"])
    orb_dn = (bar >= 15) & (d["close"] < d["or_lo"])
    H["ORB break"] = np.where(orb_up, 1, np.where(orb_dn, -1, 0))
    H["ORB fade"] = np.where(orb_up, -1, np.where(orb_dn, 1, 0))

    # Trend alignment with the slower intraday mean
    up = (d["close"] > d["ma60"]) & d["ma60"].notna()
    dn = (d["close"] < d["ma60"]) & d["ma60"].notna()
    H["TREND ma60"] = np.where(up, 1, np.where(dn, -1, 0))
    H["TREND ma60 fade"] = np.where(up, -1, np.where(dn, 1, 0))

    # Intraday drift from the open — continuation vs reversal
    fo = d["from_open"]
    H["DAY continuation"] = np.where(fo > 0.003, 1, np.where(fo < -0.003, -1, 0))
    H["DAY reversal"] = np.where(fo > 0.003, -1, np.where(fo < -0.003, 1, 0))

    # Time-of-day, direction-neutral tests carried long and short
    for lab, m in (("first 30m", bar < 30), ("mid session", (bar >= 30) & (bar < 300)),
                   ("last 45m", bar >= 330)):
        H[f"TOD {lab} long"] = np.where(m, 1, 0)
        H[f"TOD {lab} short"] = np.where(m, -1, 0)

    # Conditioned combinations: mean reversion only against the slower trend
    H["MR z>2 with trend"] = np.where((z > 2) & dn, -1, np.where((z < -2) & up, 1, 0))
    H["MR z>2 vs trend"] = np.where((z > 2) & up, -1, np.where((z < -2) & dn, 1, 0))
    H["MOM brk with trend"] = np.where(brk_up & up, 1, np.where(brk_dn & dn, -1, 0))

    return {k: pd.Series(v, index=d.index) for k, v in H.items()}


def edge(sig: pd.Series, fwd: pd.Series) -> tuple[int, float, float]:
    """(n, edge_bps_vs_baseline, p). Signed so a working signal is positive."""
    m = sig.ne(0) & sig.notna() & fwd.notna()
    if m.sum() < 50:
        return int(m.sum()), np.nan, np.nan
    r = (sig[m] * fwd[m]).to_numpy(float)
    valid = fwd.dropna().to_numpy(float)
    lw = float((sig[m] > 0).mean())
    base = np.average(np.concatenate([valid, -valid]),
                      weights=np.concatenate([np.full(len(valid), lw),
                                              np.full(len(valid), 1 - lw)]))
    t, p = stats.ttest_1samp(r, base)
    return int(m.sum()), float((r.mean() - base) * 1e4), float(p)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", type=int, nargs="+", default=[15, 30, 60])
    args = ap.parse_args()

    spot = load_spot()
    spot["year"] = spot["datetime"].dt.year
    print("Building features ...", flush=True)
    spot = build_features(spot)

    for H in args.horizons:
        # Forward return must not cross a day boundary.
        spot[f"f{H}"] = (spot.groupby("date", sort=False)["close"].shift(-H)
                         / spot["close"] - 1)

    sigs = hypotheses(spot)
    print(f"Scanning {len(sigs)} hypotheses x {len(args.horizons)} horizons "
          f"on {len(spot):,} bars\n")

    for H in args.horizons:
        fwd_col = f"f{H}"
        print("=" * 104)
        print(f"HORIZON {H} MINUTES     edge in bps vs matched baseline (p in brackets)")
        print("=" * 104)
        print(f"{'hypothesis':24}" + "".join(f"{k:>26}" for k in SPLITS))
        rows = []
        for name, sig in sigs.items():
            cells, vals = [], {}
            for lab, (y0, y1) in SPLITS.items():
                m = spot["year"].between(y0, y1)
                n, e, p = edge(sig[m], spot.loc[m, fwd_col])
                vals[lab] = (n, e, p)
                cells.append("            n/a           " if np.isnan(e)
                             else f"{e:>15.1f} ({p:>5.3f})  ")
            print(f"{name:24}" + "".join(cells))
            rows.append((name, vals))

        # Survivors: same sign in all three splits, significant in the hold-out.
        print("\n  SURVIVORS — consistent sign across all three, p<0.05 in TEST:")
        any_ok = False
        for name, v in rows:
            es = [v[k][1] for k in SPLITS]
            ps = [v[k][2] for k in SPLITS]
            if any(np.isnan(x) for x in es):
                continue
            if len(set(np.sign(es))) == 1 and es[0] > 0 and ps[-1] < 0.05:
                print(f"    {name:24} train {es[0]:+7.1f}  valid {es[1]:+7.1f}  "
                      f"TEST {es[2]:+7.1f} bps (p={ps[-1]:.3f}, n={v['TEST  25-26'][0]:,})")
                any_ok = True
        if not any_ok:
            print("    none")
        print()


if __name__ == "__main__":
    main()
