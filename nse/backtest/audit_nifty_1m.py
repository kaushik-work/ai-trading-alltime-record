"""Audit the NIFTY 1-minute option dataset before trusting any backtest on it.

Answers, per year: how many trading days, what strike span, how complete the
session is, and where the data will silently lie to a strategy (zero prices,
missing IV, stale bars, thin strikes).

Usage:
    python -m nse.backtest.audit_nifty_1m --root "C:/Users/anura/Downloads/Nifty_option_historical/Week_1min"
    python -m nse.backtest.audit_nifty_1m --deep 2025      # per-file detail for one year
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd

SESSION_MINUTES = 375          # 09:15–15:30 inclusive
EXPECTED_COLS = {"datetime", "strike_label", "option_type", "open", "high",
                 "low", "close", "volume", "oi", "iv", "strike_price", "spot"}


def audit_file(path: Path) -> dict:
    df = pd.read_csv(path)
    missing_cols = EXPECTED_COLS - set(df.columns)
    if missing_cols:
        return {"file": path.name, "error": f"missing cols {sorted(missing_cols)}"}

    dt = pd.to_datetime(df["datetime"], errors="coerce")
    n = len(df)
    minutes = dt.dt.floor("min").nunique()
    contracts = df.groupby(["strike_label", "option_type"]).ngroups

    zero_close = int((df["close"] <= 0).sum())
    nan_iv = int(df["iv"].isna().sum())
    zero_vol = int((df["volume"] == 0).sum())
    # A bar where O=H=L=C is a print-repeat: no trading happened in that minute.
    flat = int(((df["open"] == df["high"]) & (df["high"] == df["low"]) &
                (df["low"] == df["close"])).sum())
    bad_ohlc = int(((df["high"] < df["low"]) |
                    (df["close"] > df["high"]) | (df["close"] < df["low"]) |
                    (df["open"] > df["high"]) | (df["open"] < df["low"])).sum())

    return {
        "file": path.name,
        "date": str(dt.dt.date.iloc[0]) if n else "",
        "rows": n,
        "minutes": minutes,
        "contracts": contracts,
        "spot_lo": float(df["spot"].min()), "spot_hi": float(df["spot"].max()),
        "zero_close_pct": zero_close / n * 100 if n else 0,
        "nan_iv_pct": nan_iv / n * 100 if n else 0,
        "zero_vol_pct": zero_vol / n * 100 if n else 0,
        "flat_bar_pct": flat / n * 100 if n else 0,
        "bad_ohlc": bad_ohlc,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default="C:/Users/anura/Downloads/Nifty_option_historical/Week_1min")
    p.add_argument("--deep", default=None, help="year to show per-file detail for")
    p.add_argument("--sample", type=int, default=0, help="audit only N files per year (0 = all)")
    args = p.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"root not found: {root}")
        return

    year_dirs = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith("__"))
    print("=" * 108)
    print("NIFTY 1-MINUTE OPTION DATA — AUDIT")
    print("=" * 108)
    print(f"{'period':26}{'days':>6}{'rows/day':>10}{'mins/day':>9}{'contracts':>10}"
          f"{'zeroC%':>8}{'nanIV%':>8}{'zeroV%':>8}{'flat%':>8}{'badOHLC':>9}")

    all_rows: list[dict] = []
    for d in year_dirs:
        files = sorted(d.glob("*.csv"))
        if args.sample:
            files = files[:: max(1, len(files) // args.sample)][: args.sample]
        recs = []
        for f in files:
            try:
                recs.append(audit_file(f))
            except Exception as e:
                recs.append({"file": f.name, "error": str(e)})
        ok = [r for r in recs if "error" not in r]
        errs = [r for r in recs if "error" in r]
        all_rows.extend(ok)
        if not ok:
            print(f"{d.name:26}{'ALL FAILED':>6}")
            continue
        g = pd.DataFrame(ok)
        print(f"{d.name:26}{len(ok):>6}{g['rows'].mean():>10,.0f}{g['minutes'].mean():>9.0f}"
              f"{g['contracts'].mean():>10.0f}{g['zero_close_pct'].mean():>8.2f}"
              f"{g['nan_iv_pct'].mean():>8.2f}{g['zero_vol_pct'].mean():>8.1f}"
              f"{g['flat_bar_pct'].mean():>8.1f}{int(g['bad_ohlc'].sum()):>9}")
        if errs:
            print(f"{'':26}  {len(errs)} unreadable, e.g. {errs[0]['file']}: {errs[0]['error']}")

    if not all_rows:
        return
    df = pd.DataFrame(all_rows)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values("date")

    print("\n" + "=" * 108)
    print("COVERAGE + GAPS")
    print("=" * 108)
    print(f"  span            {df['date'].min():%Y-%m-%d} -> {df['date'].max():%Y-%m-%d}")
    print(f"  trading days    {len(df):,}")
    print(f"  spot range      {df['spot_lo'].min():,.0f} -> {df['spot_hi'].max():,.0f}")

    # Weekday gaps longer than a normal weekend = missing sessions or holidays.
    gaps = df["date"].diff().dt.days
    big = df[gaps > 4]
    print(f"  gaps > 4 days   {len(big)}")
    for _, r in big.head(8).iterrows():
        print(f"      around {r['date']:%Y-%m-%d}")

    short = df[df["minutes"] < SESSION_MINUTES * 0.9]
    print(f"  short sessions  {len(short)} days below 90% of {SESSION_MINUTES}m")
    for _, r in short.head(8).iterrows():
        print(f"      {r['date']:%Y-%m-%d}  only {int(r['minutes'])}m")

    print("\n" + "=" * 108)
    print("WHAT THIS DATA CAN AND CANNOT SUPPORT")
    print("=" * 108)
    print("  CAN:  true 1m OHLC per contract -> candle/price-action strategies on")
    print("        option premium itself, realistic intrabar stop/target fills,")
    print("        IV and OI conditioning, ATM-relative strike selection.")
    print("  CANNOT: bid/ask spread (no quote columns) -> execution cost must be")
    print("        assumed, not measured. Flat bars mark minutes with no trade;")
    print("        treating them as fillable is the main way this data flatters a")
    print("        backtest.")
    print()
    print("  KNOWN CONTAMINATION: some files carry rows from ANOTHER index.")
    print("        NIFTY_2021-08-30 has 16 rows at spot ~36,000 / strike 37,000")
    print("        (BANKNIFTY levels) on a day NIFTY traded 16,798-16,948.")
    print("        Any loader MUST drop rows whose spot deviates from the day's")
    print("        median - see clean_day() below.")


def clean_day(df: pd.DataFrame, max_dev: float = 0.10) -> pd.DataFrame:
    """Drop rows contaminated by another index.

    Some files mix in rows from a different underlying (BANKNIFTY levels inside
    a NIFTY file). Spot cannot move 10% intraday, so anything that far from the
    day's median spot is not this index.
    """
    if df.empty or "spot" not in df.columns:
        return df
    med = df["spot"].median()
    keep = (df["spot"] - med).abs() / med <= max_dev
    return df[keep]

    if args.deep:
        d = next((x for x in year_dirs if args.deep in x.name), None)
        if d:
            print(f"\nPer-file detail — {d.name}")
            sub = pd.DataFrame([audit_file(f) for f in sorted(d.glob('*.csv'))])
            print(sub.to_string(index=False, max_rows=40))


if __name__ == "__main__":
    main()
