"""
Check pairwise overlap between candidate signals — would iv_rv_low @ 0.90
add diversification to the existing Q5 ensemble, or just re-trigger on the
same bars?

For each bar, computes which of these signals fired (gross — ignoring caps,
regime filter, and any risk gates so we see the *signal's view* of the bar):

    q5_straddle_level    atm_straddle > trailing-5d P70
    q5_straddle_mom3     mom3 of atm_straddle > trailing-5d P70
    q5_pcr_mom3          mom3 of PCR_OI > trailing-5d P70
    iv_rv_low_090        iv_atm / rv_60m < 0.90

Reports:
    - count of bars each signal fired on
    - pairwise overlap counts
    - Jaccard similarity = |A ∩ B| / |A ∪ B|  (1.0 = identical, 0.0 = disjoint)
    - which pairs co-fire frequently (high redundancy)
    - which pairs rarely co-fire (high diversification value)
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: F401
import numpy as np
import pandas as pd
from core import mongo

# Re-use feature math from the IV mispricing script (post bug-fixes), but
# override the loader to include OI (which the IV-only script doesn't need).
from scripts.backtest_iv_mispricing import (
    _build_bar_table, _compute_features,
)


def _load_snapshots(db, symbol: str) -> pd.DataFrame:
    """Like backtest_iv_mispricing._load_snapshots but INCLUDES oi (for PCR)."""
    print(f"Loading option_snapshots for {symbol} ...", flush=True)
    cur = db.option_snapshots.find(
        {"symbol": symbol},
        projection={"_id": 0, "date": 1, "timestamp": 1, "strike": 1,
                    "option_type": 1, "ltp": 1, "oi": 1, "spot": 1, "expiry": 1},
    )
    df = pd.DataFrame(list(cur))
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["ts", "ltp", "spot"])
    df["strike"] = df["strike"].astype(int)
    df["oi"]     = df["oi"].fillna(0).astype(int)
    df["ltp"]    = df["ltp"].astype(float)
    df["spot"]   = df["spot"].astype(float)
    print(f"  loaded {len(df):,} rows across {df['date'].nunique()} days "
          f"(with OI)", flush=True)
    return df

# And the existing Q5 signal feature functions
from strategies.feature_signals import (
    _atm_strike_for, _atm_straddle_for_bar, _pcr_oi_for_bar,
)


def _q5_features_per_bar(df: pd.DataFrame) -> pd.DataFrame:
    """Build {(date, ts) -> {atm_straddle, mom3_straddle, mom3_pcr}}."""
    rows = []
    # Group all snapshot rows by (date, ts) so we have the full chain per bar
    bars_grouped = df.groupby(["date", "ts"], sort=True)
    bar_keys = []
    bar_rows = {}
    for (date, ts), g in bars_grouped:
        bar_rows[(date, ts)] = g.to_dict("records")
        bar_keys.append((date, ts))

    # Compute features per bar (need 3-bar history for momentum)
    by_day = defaultdict(list)
    for (date, ts) in bar_keys:
        by_day[date].append(ts)
    for d in by_day:
        by_day[d].sort()

    for (date, ts) in bar_keys:
        rows_here = bar_rows[(date, ts)]
        spot = rows_here[0].get("spot")
        if not spot:
            continue
        atm = _atm_strike_for(float(spot))
        atm_straddle = _atm_straddle_for_bar(rows_here, atm)
        pcr_oi       = _pcr_oi_for_bar(rows_here)

        # 3-bar history
        ts_list = by_day[date]
        idx     = ts_list.index(ts)
        if idx >= 3:
            past_ts   = ts_list[idx - 3]
            past_rows = bar_rows[(date, past_ts)]
            past_spot = past_rows[0].get("spot")
            past_atm  = _atm_strike_for(float(past_spot)) if past_spot else None
            past_straddle = (_atm_straddle_for_bar(past_rows, past_atm)
                             if past_atm else None)
            past_pcr      = _pcr_oi_for_bar(past_rows)
            mom3_straddle = (atm_straddle - past_straddle
                              if atm_straddle is not None and past_straddle is not None
                              else None)
            mom3_pcr      = (pcr_oi - past_pcr
                              if pcr_oi is not None and past_pcr is not None
                              else None)
        else:
            mom3_straddle = mom3_pcr = None

        rows.append({
            "date":         date,
            "ts":           ts,
            "spot":         spot,
            "atm_straddle": atm_straddle,
            "mom3_straddle": mom3_straddle,
            "mom3_pcr":     mom3_pcr,
        })
    return pd.DataFrame(rows)


def _thresholds_per_day(df: pd.DataFrame, feature_col: str,
                        n_days: int = 5, pct: float = 0.70) -> dict:
    """{date: trailing-n_days P-pct threshold}. None for warmup."""
    dates_sorted = sorted(df["date"].unique())
    out = {}
    for i, d in enumerate(dates_sorted):
        if i < n_days:
            out[d] = None
            continue
        prior_dates = dates_sorted[i - n_days:i]
        sample = df.loc[df["date"].isin(prior_dates), feature_col].dropna().values
        if len(sample) < 30:
            out[d] = None
        else:
            sample = np.sort(sample)
            idx = int(pct * (len(sample) - 1))
            out[d] = float(sample[idx])
    return out


def main():
    db = mongo.get_db()
    if db is None:
        print("Mongo unreachable")
        return

    df = _load_snapshots(db, "NIFTY")
    if df.empty:
        print("No data")
        return

    # ── Compute Q5 features (level + mom3 + pcr_mom3)
    print("Computing Q5 features ...", flush=True)
    q5 = _q5_features_per_bar(df)
    print(f"  {len(q5):,} bars with Q5 features")

    # ── Compute IV features
    print("Computing IV/RV features ...", flush=True)
    iv_bars = _build_bar_table(df)
    iv_bars = _compute_features(iv_bars)
    iv_lookup = {(r.date, r.ts): r.iv_rv_ratio
                  for r in iv_bars.itertuples(index=False)
                  if not pd.isna(r.iv_rv_ratio)}
    print(f"  {len(iv_lookup):,} bars with IV/RV computed")

    # ── Compute per-day thresholds (P70 for Q5, fixed 0.90 for IV)
    print("Computing per-day thresholds ...", flush=True)
    thr_level   = _thresholds_per_day(q5, "atm_straddle")
    thr_mom3    = _thresholds_per_day(q5, "mom3_straddle")
    thr_pcr     = _thresholds_per_day(q5, "mom3_pcr")
    iv_threshold = 0.90

    # ── Evaluate each signal per bar (no caps, no regime — just raw fires)
    fires = defaultdict(set)   # signal_name -> set of (date, ts) bars
    for r in q5.itertuples(index=False):
        key = (r.date, r.ts)
        thr_l = thr_level.get(r.date)
        thr_m = thr_mom3.get(r.date)
        thr_p = thr_pcr.get(r.date)

        if thr_l is not None and r.atm_straddle is not None and r.atm_straddle > thr_l:
            fires["q5_straddle_level"].add(key)
        if thr_m is not None and r.mom3_straddle is not None and r.mom3_straddle > thr_m:
            fires["q5_straddle_mom3"].add(key)
        if thr_p is not None and r.mom3_pcr is not None and r.mom3_pcr > thr_p:
            fires["q5_pcr_mom3"].add(key)
        ivrv = iv_lookup.get(key)
        if ivrv is not None and ivrv < iv_threshold:
            fires["iv_rv_low_090"].add(key)

    signal_names = ["q5_straddle_level", "q5_straddle_mom3", "q5_pcr_mom3", "iv_rv_low_090"]
    print(f"\nSignal fire counts (raw, no caps):")
    for s in signal_names:
        print(f"  {s:<25}  {len(fires[s]):>5} bars")

    # ── Pairwise overlap matrix
    print(f"\nPairwise overlap (intersection count / Jaccard / "
          f"% of smaller set):")
    print(f"  {'Signal A':<25} {'Signal B':<25} {'Both':>5}  "
          f"{'A only':>6}  {'B only':>6}  {'Jaccard':>8}  {'% of min':>8}")
    print("  " + "-" * 92)
    for i, a in enumerate(signal_names):
        for b in signal_names[i+1:]:
            A, B = fires[a], fires[b]
            inter = A & B
            union = A | B
            jacc = len(inter) / len(union) if union else 0
            min_size = min(len(A), len(B)) if (A and B) else 0
            pct_min = len(inter) / min_size if min_size else 0
            print(f"  {a:<25} {b:<25} {len(inter):>5}  "
                  f"{len(A - B):>6}  {len(B - A):>6}  "
                  f"{jacc:>7.3f}  {pct_min*100:>7.1f}%")

    # ── Verdict for iv_rv_low_090 vs each Q5 signal
    print(f"\nVerdict for adding iv_rv_low_090 to the ensemble:")
    iv_set = fires["iv_rv_low_090"]
    for q in ("q5_straddle_level", "q5_straddle_mom3", "q5_pcr_mom3"):
        q_set = fires[q]
        if not iv_set or not q_set:
            continue
        overlap = len(iv_set & q_set) / len(iv_set) * 100
        print(f"  vs {q}: {overlap:.1f}% of iv_rv_low fires are ALSO {q} fires")

    iv_unique = iv_set - (fires["q5_straddle_level"] |
                          fires["q5_straddle_mom3"] |
                          fires["q5_pcr_mom3"])
    print(f"\n  iv_rv_low fires on {len(iv_set)} bars total.")
    print(f"  Of those, {len(iv_unique)} bars ({len(iv_unique)/max(1,len(iv_set))*100:.1f}%) "
          f"are NOT fired by any existing Q5 signal — TRULY ORTHOGONAL")


if __name__ == "__main__":
    main()
