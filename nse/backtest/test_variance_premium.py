"""Is NIFTY option implied vol systematically above what actually happens?

Directional intraday edge on the index measured ~1bp (scan_hypotheses), which
is not tradeable. The other place an edge can live in options is structural:
the variance risk premium. If implied vol sits persistently above subsequent
realized vol, selling premium has positive expectancy regardless of direction.

Method
  For each sampled day, take ATM implied vol at a reference time, then measure
  the realized vol actually delivered over the following session from 1m index
  returns. The spread IV - RV is the premium. Reported per period so it faces
  the same TRAIN / VALIDATE / TEST discipline as everything else.

This is a measurement, not a strategy. A positive premium says premium-selling
is worth designing; it does not say any particular structure captures it, and
it says nothing about the tail risk of being short vol.

Usage:
    python -m nse.backtest.test_variance_premium --days 400
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

from nse.backtest.nifty_loader import DEFAULT_ROOT, clean_day, load_spot

MINUTES_PER_YEAR = 375 * 252          # trading minutes in a year
REF_TIME = "10:00"                    # after the open settles, before expiry games


def atm_iv_for_day(path: Path, ref: str = REF_TIME) -> float | None:
    """Mean ATM call/put IV at the reference minute."""
    try:
        df = pd.read_csv(path, usecols=["datetime", "strike_label", "option_type",
                                        "iv", "spot", "close", "volume"])
    except Exception:
        return None
    df = clean_day(df)
    if df.empty:
        return None
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])
    t = df["datetime"].dt.strftime("%H:%M")
    at = df[(t == ref) & (df["strike_label"].astype(str) == "ATM")]
    if at.empty:
        return None
    iv = pd.to_numeric(at["iv"], errors="coerce").dropna()
    iv = iv[(iv > 0) & (iv < 200)]
    return float(iv.mean()) if len(iv) else None


def realized_vol(spot: pd.DataFrame, day, horizon_days: int = 1) -> float | None:
    """Annualised realised vol from 1m returns over the next `horizon_days`."""
    days = sorted(spot["date"].unique())
    try:
        i = days.index(day)
    except ValueError:
        return None
    window = days[i: i + horizon_days + 1]
    seg = spot[spot["date"].isin(window)]
    if len(seg) < 200:
        return None
    r = seg.groupby("date", sort=False)["close"].pct_change().dropna()
    if len(r) < 100:
        return None
    return float(r.std() * np.sqrt(MINUTES_PER_YEAR) * 100)


def period_of(d) -> str:
    y = d.year
    if y <= 2023:
        return "TRAIN 21-23"
    if y == 2024:
        return "VALID 24"
    return "TEST  25-26"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400, help="days sampled across the span")
    ap.add_argument("--horizon", type=int, default=1, help="realised-vol horizon in days")
    args = ap.parse_args()

    spot = load_spot()
    files = sorted(DEFAULT_ROOT.glob("*/NIFTY_*_1m.csv"))
    step = max(1, len(files) // args.days)
    sample = files[::step][: args.days]

    rows = []
    for i, f in enumerate(sample, 1):
        iv = atm_iv_for_day(f)
        if iv is None:
            continue
        day = pd.to_datetime(f.name[6:16]).date()
        rv = realized_vol(spot, day, args.horizon)
        if rv is None:
            continue
        rows.append({"date": day, "iv": iv, "rv": rv, "premium": iv - rv,
                     "period": period_of(day)})
        if i % 100 == 0:
            print(f"  ... {i}/{len(sample)}", flush=True)

    if not rows:
        print("no usable observations")
        return
    r = pd.DataFrame(rows).sort_values("date")

    print("=" * 92)
    print(f"NIFTY VARIANCE RISK PREMIUM — ATM IV at {REF_TIME} vs next "
          f"{args.horizon}-day realised vol")
    print("=" * 92)
    print(f"  {len(r):,} observations, {r['date'].min()} -> {r['date'].max()}\n")
    print(f"  {'period':14}{'n':>6}{'mean IV':>10}{'mean RV':>10}{'premium':>10}"
          f"{'t':>8}{'p':>9}{'IV>RV':>9}")

    for p in ("TRAIN 21-23", "VALID 24", "TEST  25-26"):
        g = r[r["period"] == p]
        if len(g) < 10:
            continue
        t, pv = stats.ttest_1samp(g["premium"], 0.0)
        print(f"  {p:14}{len(g):>6}{g['iv'].mean():>10.2f}{g['rv'].mean():>10.2f}"
              f"{g['premium'].mean():>10.2f}{t:>8.2f}{pv:>9.4f}"
              f"{(g['premium'] > 0).mean() * 100:>8.0f}%")

    t, pv = stats.ttest_1samp(r["premium"], 0.0)
    print(f"  {'ALL':14}{len(r):>6}{r['iv'].mean():>10.2f}{r['rv'].mean():>10.2f}"
          f"{r['premium'].mean():>10.2f}{t:>8.2f}{pv:>9.4f}"
          f"{(r['premium'] > 0).mean() * 100:>8.0f}%")

    print("\n  By year:")
    r["year"] = pd.to_datetime(r["date"]).dt.year
    for y, g in r.groupby("year"):
        print(f"    {y}  n={len(g):>4}  IV {g['iv'].mean():>6.2f}  "
              f"RV {g['rv'].mean():>6.2f}  premium {g['premium'].mean():>+6.2f}  "
              f"IV>RV {(g['premium'] > 0).mean() * 100:>3.0f}%")

    worst = r.nsmallest(5, "premium")
    print("\n  Worst 5 days for a premium seller (RV blew through IV):")
    for _, x in worst.iterrows():
        print(f"    {x['date']}  IV {x['iv']:>6.2f}  RV {x['rv']:>6.2f}  "
              f"premium {x['premium']:>+7.2f}")
    print("\n  Those tails are the whole risk of short vol: the mean can be")
    print("  reliably positive while a single day removes months of it.")


if __name__ == "__main__":
    main()
