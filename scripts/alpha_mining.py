"""
Alpha mining harness — WorldQuant "101 Formulaic Alphas" approach.

Mine many simple formulaic alphas from your existing per-bar option-chain
features, score each by Sharpe of forward NIFTY return when the alpha is
in its top quintile, keep the top survivors.

This is *not* a strategy in itself — it's a signal-discovery tool. Each
"alpha" here is just a numerical formula. The top ones become candidate
signals to forward-test.

Why this matters:
  Renaissance / WorldQuant don't bet on one signal. They bet on dozens of
  weak (Sharpe ~0.5) signals that are uncorrelated. Average them and you
  get Sharpe 2-3. We've found ONE good signal (atm_straddle). We need
  more to ensemble.

What this generates: ~80 candidate alphas — combinations of:
  ts_rank, delta (change), pct (current value), ratio of two features,
  z-score over rolling window.
applied to the existing features (pcr_oi, atm_straddle, max_pain_dist, ...).

Output:
  Top 20 alphas by IC (Information Coefficient = corr with fwd_15m return),
  filtered to those with |IC| > 0.10 AND n >= 200 bars.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: F401
import numpy as np
import pandas as pd
from core import mongo  # noqa: E402

from analyze_option_chain import (
    _load_snapshots, _build_bar_table, _add_forward_returns,
)


def _generate_alphas(bars: pd.DataFrame) -> dict:
    """Generate a dict of {alpha_name: pd.Series}."""
    alphas: dict = {}
    base_feats = [
        "pcr_oi", "pcr_vol", "atm_straddle", "call_wall_dist",
        "put_wall_dist", "max_pain_dist", "oi_skew",
        "total_ce_oi", "total_pe_oi",
    ]

    # ── Level
    for f in base_feats:
        if f in bars.columns:
            alphas[f] = bars[f]

    # ── 1-bar delta
    for f in base_feats:
        if f in bars.columns:
            alphas[f"delta_{f}"] = bars.groupby("date")[f].diff()

    # ── 3-bar momentum (current - 3 bars ago)
    for f in base_feats:
        if f in bars.columns:
            alphas[f"mom3_{f}"] = bars.groupby("date")[f].diff(3)

    # ── Rolling z-score (current vs 10-bar mean / std within day)
    for f in base_feats:
        if f in bars.columns:
            rolling_mean = bars.groupby("date")[f].transform(
                lambda x: x.rolling(10, min_periods=3).mean())
            rolling_std  = bars.groupby("date")[f].transform(
                lambda x: x.rolling(10, min_periods=3).std())
            alphas[f"zscore_{f}"] = (bars[f] - rolling_mean) / rolling_std

    # ── Ratios of pairs (asymmetry signals)
    pairs = [
        ("call_wall_dist", "put_wall_dist"),
        ("total_ce_oi",    "total_pe_oi"),
        ("atm_straddle",   "max_pain_dist"),
    ]
    for a, b in pairs:
        if a in bars.columns and b in bars.columns:
            denom = bars[b].abs().replace(0, np.nan)
            alphas[f"ratio_{a}_{b}"] = bars[a] / denom

    # ── Cross-products (sign-meaningful combos)
    for a in base_feats:
        for b in ("max_pain_dist", "oi_skew"):
            if a == b or a not in bars.columns or b not in bars.columns:
                continue
            alphas[f"prod_{a}_x_{b}"] = bars[a] * bars[b]

    return alphas


def _ic(alpha: pd.Series, target: pd.Series) -> tuple:
    sub = pd.DataFrame({"a": alpha, "t": target}).dropna()
    if len(sub) < 100:
        return np.nan, len(sub)
    return sub["a"].corr(sub["t"]), len(sub)


def _quintile_spread(alpha: pd.Series, target: pd.Series) -> tuple:
    sub = pd.DataFrame({"a": alpha, "t": target}).dropna()
    if len(sub) < 100:
        return np.nan, np.nan, len(sub)
    try:
        sub["q"] = pd.qcut(sub["a"], 5, labels=False, duplicates="drop")
        if sub["q"].nunique() < 5:
            return np.nan, np.nan, len(sub)
    except Exception:
        return np.nan, np.nan, len(sub)
    q5_mean = sub[sub["q"] == 4]["t"].mean()
    q1_mean = sub[sub["q"] == 0]["t"].mean()
    return (q5_mean - q1_mean) * 10_000, q5_mean * 10_000, len(sub)


def _correlation_with(alpha: pd.Series, others: dict, max_corr_threshold: float) -> float:
    """Return max |corr| of `alpha` with any already-accepted alpha."""
    if not others:
        return 0.0
    max_abs = 0.0
    for name, other in others.items():
        sub = pd.DataFrame({"a": alpha, "b": other}).dropna()
        if len(sub) < 100:
            continue
        c = abs(sub["a"].corr(sub["b"]))
        if c > max_abs:
            max_abs = c
        if max_abs > max_corr_threshold:
            return max_abs
    return max_abs


def main():
    db = mongo.get_db()
    if db is None:
        print("Mongo unreachable.")
        sys.exit(1)

    df = _load_snapshots(db, "NIFTY")
    bars = _build_bar_table(df)
    bars = _add_forward_returns(bars, horizons_bars=(1, 3, 6))

    alphas = _generate_alphas(bars)
    print(f"Generated {len(alphas)} candidate alphas. Evaluating IC vs fwd_15m ...")

    target = bars["fwd_15m"]
    rows = []
    for name, series in alphas.items():
        ic, n_ic = _ic(series, target)
        q_spread, q5_mean, n_q = _quintile_spread(series, target)
        rows.append({
            "alpha":     name,
            "ic_15m":    ic,
            "n":         n_ic,
            "q5q1_bps":  q_spread,
            "q5_bps":    q5_mean,
        })
    res = pd.DataFrame(rows).dropna(subset=["ic_15m"]).copy()
    res["abs_ic"] = res["ic_15m"].abs()
    res = res.sort_values("abs_ic", ascending=False)

    # Filter: |IC| > 0.10 AND n >= 200
    strong = res[(res["abs_ic"] > 0.10) & (res["n"] >= 200)].copy()
    print(f"\n=== Alphas with |IC| > 0.10 and n >= 200 ===")
    print(f"  found {len(strong)} survivors out of {len(res)} evaluated")
    if strong.empty:
        print("  none — Q5 atm_straddle remains the best single feature we have.")
        print("\n=== Top 10 by |IC| (no filter) ===")
        for _, r in res.head(10).iterrows():
            print(f"  {r['alpha']:>30}  IC={r['ic_15m']:+.3f}  "
                  f"Q5-Q1={r['q5q1_bps']:+.2f}bps  n={int(r['n'])}")
        return

    print(f"\n  {'alpha':>30}  {'IC':>7}  {'Q5-Q1':>9}  {'n':>5}")
    print("  " + "-" * 60)
    for _, r in strong.iterrows():
        print(f"  {r['alpha']:>30}  {r['ic_15m']:+.3f}  "
              f"{r['q5q1_bps']:>+7.2f}bps  {int(r['n']):>5}")

    # Greedy decorrelation: pick top alpha, then next that's <0.5 correlated, etc.
    print(f"\n=== Decorrelated ensemble candidates (max pairwise |corr| < 0.5) ===")
    accepted: dict = {}
    accepted_rows = []
    for _, r in strong.iterrows():
        a = alphas[r["alpha"]]
        max_corr = _correlation_with(a, accepted, 0.5)
        if max_corr < 0.5:
            accepted[r["alpha"]] = a
            accepted_rows.append({**r.to_dict(), "max_corr_with_others": max_corr})

    if not accepted_rows:
        print("  none")
        return
    print(f"  {'alpha':>30}  {'IC':>7}  {'Q5-Q1':>9}  {'max corr':>9}")
    print("  " + "-" * 65)
    for r in accepted_rows:
        print(f"  {r['alpha']:>30}  {r['ic_15m']:+.3f}  "
              f"{r['q5q1_bps']:>+7.2f}bps  {r['max_corr_with_others']:>9.2f}")
    print(f"\n  -> {len(accepted_rows)} approximately independent signals.")
    print(f"  Ensembling these with equal weight could give Sharpe > the best individual.")


if __name__ == "__main__":
    main()
