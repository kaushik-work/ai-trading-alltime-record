"""
Meta-labeling on the Q5 atm_straddle signal (López de Prado approach).

Primary model: the Q5 atm_straddle trigger (already known to fire).
Secondary (meta) model: classifier predicting, for each Q5 fire,
whether the trade will be a TP (1) or SL/EOD/no-data (0).

Features at entry time:
  hour                     entry hour (9-15)
  minute_of_session        minutes since 09:15
  spot_return_30m          NIFTY return over last 30 min
  spot_return_60m          NIFTY return over last 60 min
  straddle_over_threshold  (atm_straddle - threshold) / threshold
  regime                   one-hot: trend_up / trend_down / chop
  day_of_week              0-4

We use temporal cross-validation (train on early days, test on later days)
to avoid look-ahead, and report:
  - Baseline (no meta) precision = 41% (the raw Q5 win rate)
  - Meta-model precision at decision threshold 0.5
  - Meta-model precision at decision threshold 0.6 (more selective)
  - Lift over baseline
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: F401
import numpy as np
import pandas as pd
from core import mongo  # noqa: E402
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

from backtest_straddle_signal import (
    _load_snapshots, _atm_straddle_per_bar, _trailing_thresholds,
    _index_premium_series, _walk_forward, LOT_SIZE,
)
from regime_filter import classify_regime, WINDOW_BARS


def _features_for_bar(g_today: pd.DataFrame, i: int, threshold: float) -> dict:
    """Compute features at bar i of today's DataFrame g_today."""
    row = g_today.iloc[i]
    ts = row["ts"]
    spots = g_today["spot"].values
    ret_30 = (spots[i] / spots[max(i - 6, 0)] - 1) if i >= 6 else 0.0
    ret_60 = (spots[i] / spots[max(i - 12, 0)] - 1) if i >= 12 else 0.0
    minute_of_session = (ts.hour - 9) * 60 + (ts.minute - 15)
    return {
        "hour":                    ts.hour,
        "minute_of_session":       minute_of_session,
        "spot_return_30m":         ret_30,
        "spot_return_60m":         ret_60,
        "straddle_over_threshold": (row["atm_straddle"] - threshold) / threshold,
        "regime_up":               1 if row["regime"] == "trend_up" else 0,
        "regime_down":             1 if row["regime"] == "trend_down" else 0,
        "regime_chop":             1 if row["regime"] == "chop" else 0,
        "day_of_week":             pd.Timestamp(row["date"]).dayofweek,
    }


def _build_dataset(bar_tbl: pd.DataFrame, premium_series: dict,
                   thresholds: dict, sl_dist: float, rr: float) -> pd.DataFrame:
    rows = []
    open_until: dict = {}
    bar_tbl = bar_tbl.sort_values(["date", "ts"]).reset_index(drop=True)

    for date, g in bar_tbl.groupby("date", sort=True):
        g = g.reset_index(drop=True)
        thr = thresholds.get(date)
        if thr is None:
            continue
        for i in range(len(g)):
            r = g.iloc[i]
            if r["atm_straddle"] < thr:
                continue
            if date in open_until and r["ts"].to_pydatetime() < open_until[date]:
                continue
            if r["regime"] == "warmup":
                continue
            key = (date, int(r["atm_strike"]), "CE")
            series = premium_series.get(key)
            if not series:
                continue
            entry_dt = entry_prem = None
            for dt, ltp in series:
                if dt >= r["ts"].to_pydatetime():
                    entry_dt = dt
                    entry_prem = ltp
                    break
            if entry_prem is None:
                continue
            exit_dt, exit_prem, reason = _walk_forward(series, entry_dt, entry_prem,
                                                       sl_dist, rr)
            feats = _features_for_bar(g, i, thr)
            feats["date"] = date
            feats["pnl"] = round((exit_prem - entry_prem) * LOT_SIZE, 2)
            feats["label"] = 1 if reason == "TP" else 0
            rows.append(feats)
            open_until[date] = exit_dt
    return pd.DataFrame(rows)


def main():
    db = mongo.get_db()
    if db is None:
        print("Mongo unreachable.")
        sys.exit(1)

    df = _load_snapshots(db, "NIFTY")
    bar_tbl = _atm_straddle_per_bar(df)
    bar_tbl = classify_regime(bar_tbl)
    thresholds = _trailing_thresholds(bar_tbl, 5, 0.70)
    premium_series = _index_premium_series(df)

    ds = _build_dataset(bar_tbl, premium_series, thresholds, 10.0, 3.0)
    if ds.empty:
        print("No Q5 trades — can't meta-label.")
        return

    print(f"=== Q5 trade dataset ===")
    print(f"  total trades: {len(ds)}")
    print(f"  TPs:          {ds['label'].sum()}  ({ds['label'].mean()*100:.1f}%)")
    print(f"  days:         {ds['date'].nunique()}")

    feature_cols = ["hour", "minute_of_session", "spot_return_30m",
                    "spot_return_60m", "straddle_over_threshold",
                    "regime_up", "regime_down", "regime_chop", "day_of_week"]

    # Temporal CV: train on first 70% of days, test on last 30%
    days_sorted = sorted(ds["date"].unique())
    cutoff = days_sorted[int(len(days_sorted) * 0.7)]
    train = ds[ds["date"] < cutoff]
    test  = ds[ds["date"] >= cutoff]
    print(f"  train days:   {train['date'].nunique()}  ({len(train)} trades)")
    print(f"  test days:    {test['date'].nunique()}   ({len(test)} trades)")

    if len(test) < 5 or train["label"].nunique() < 2:
        print(f"  insufficient data for meta-labeling — skipping ML.")
        # Do a univariate analysis instead
        print(f"\n=== Univariate WR by feature quartile (all trades) ===")
        for feat in feature_cols:
            if ds[feat].nunique() < 4:
                continue
            try:
                ds["_q"] = pd.qcut(ds[feat], 4, labels=["Q1", "Q2", "Q3", "Q4"],
                                    duplicates="drop")
                g = ds.groupby("_q", observed=True)["label"].agg(["count", "mean"])
                spread = g["mean"].max() - g["mean"].min()
                if spread > 0.15:
                    print(f"\n  {feat}  (max-min WR spread = {spread*100:.1f} pts)")
                    for q, v in g.iterrows():
                        print(f"    {q}  n={int(v['count']):>2}  WR={v['mean']*100:>5.1f}%")
            except Exception:
                continue
        return

    X_train, y_train = train[feature_cols].values, train["label"].values
    X_test,  y_test  = test[feature_cols].values,  test["label"].values

    # Train two classifiers — pick whichever generalises better
    results = {}
    for name, model in [
        ("LogReg",  LogisticRegression(max_iter=1000, class_weight="balanced")),
        ("GBM",     GradientBoostingClassifier(n_estimators=50, max_depth=2,
                                               learning_rate=0.05, random_state=42)),
    ]:
        model.fit(X_train, y_train)
        probs = model.predict_proba(X_test)[:, 1]
        # Evaluate at multiple thresholds
        rows = []
        for thr in (0.4, 0.5, 0.6, 0.7):
            picked = probs >= thr
            n_picked = picked.sum()
            if n_picked == 0:
                rows.append((thr, 0, 0, 0, 0))
                continue
            wr_picked = y_test[picked].mean()
            pnl_picked = test["pnl"].values[picked].sum()
            rows.append((thr, int(n_picked), wr_picked, pnl_picked,
                         len(picked) - n_picked))
        results[name] = rows

    print(f"\n=== Test-set baseline (no meta) ===")
    baseline_wr  = y_test.mean()
    baseline_pnl = test["pnl"].sum()
    print(f"  trades taken: {len(y_test)}")
    print(f"  WR:           {baseline_wr*100:.1f}%")
    print(f"  Net P&L:      Rs {baseline_pnl:+,.0f}")

    for name, rows in results.items():
        print(f"\n=== {name} meta-classifier (on test set) ===")
        print(f"  {'thr':>5}  {'taken':>6}  {'skipped':>7}  {'WR%':>5}  "
              f"{'Net Rs':>10}  {'lift WR':>8}")
        for thr, n, wr, pnl, skipped in rows:
            lift = (wr - baseline_wr) * 100 if n > 0 else 0
            wr_str = f"{wr*100:.1f}" if n > 0 else "  -  "
            print(f"  {thr:>5.2f}  {n:>6}  {skipped:>7}  {wr_str:>5}  "
                  f"{pnl:>+10,.0f}  {lift:>+7.1f}pp")


if __name__ == "__main__":
    main()
