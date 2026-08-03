"""Measure real NIFTY option bid-ask spreads from the Mongo snapshot archive.

The 5-year 1m CSV dataset has no quote columns, so any backtest on it must
model execution cost rather than measure it. Mongo's option_snapshots DO carry
bid/ask, and the two sources overlap around 2026-05. This calibrates a spread
curve there so it can be applied across the full CSV history.

Assuming a flat spread is exactly the mistake that made the crypto backtest
look profitable, so the output is bucketed by moneyness and premium - spreads
on a 2-rupee wing are nothing like spreads at the money.

Usage:
    python -m nse.backtest.calibrate_spread
    python -m nse.backtest.calibrate_spread --symbol BANKNIFTY
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd


def _load_env() -> None:
    root = Path(__file__).resolve().parents[2]
    env = root / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k, v)


def fetch(symbol: str, limit: int) -> pd.DataFrame:
    from pymongo import MongoClient
    cli = MongoClient(os.environ["MONGODB_URL"], serverSelectionTimeoutMS=20000)
    db = cli[os.environ["MONGODB_DB_NAME"]]
    cur = db["option_snapshots"].find(
        {"symbol": symbol},
        {"_id": 0, "timestamp": 1, "strike": 1, "option_type": 1,
         "ltp": 1, "bid": 1, "ask": 1, "spot": 1, "oi": 1, "volume": 1},
    ).limit(limit)
    return pd.DataFrame(list(cur))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="NIFTY")
    p.add_argument("--limit", type=int, default=200_000)
    args = p.parse_args()

    _load_env()
    df = fetch(args.symbol, args.limit)
    if df.empty:
        print(f"No snapshots for {args.symbol}")
        return

    n_raw = len(df)
    df = df[(df["bid"] > 0) & (df["ask"] > 0) & (df["ltp"] > 0) & (df["spot"] > 0)]
    df = df[df["ask"] >= df["bid"]]
    df["spread"] = df["ask"] - df["bid"]
    df["mid"] = (df["ask"] + df["bid"]) / 2
    df["spread_pct"] = df["spread"] / df["mid"] * 100
    # Half-spread is what one leg actually pays crossing from mid.
    df["half_pct"] = df["spread_pct"] / 2
    df["moneyness"] = (df["strike"] - df["spot"]) / df["spot"] * 100
    df.loc[df["option_type"].str.upper().str.startswith("P"), "moneyness"] *= -1

    print("=" * 96)
    print(f"NIFTY OPTION SPREAD CALIBRATION — {args.symbol}")
    print("=" * 96)
    print(f"  rows {n_raw:,} raw -> {len(df):,} with usable two-sided quotes "
          f"({len(df) / n_raw * 100:.1f}%)")
    print(f"  window {df['timestamp'].min()} -> {df['timestamp'].max()}")
    print()

    print("By PREMIUM bucket — the dominant driver:")
    print(f"  {'premium (Rs)':>16}{'n':>9}{'median spread':>15}{'half-spread %':>16}{'p90 half %':>12}")
    bins = [0, 2, 5, 10, 25, 50, 100, 250, 10_000]
    labels = ["<2", "2-5", "5-10", "10-25", "25-50", "50-100", "100-250", ">250"]
    df["pbucket"] = pd.cut(df["mid"], bins=bins, labels=labels)
    for lab in labels:
        g = df[df["pbucket"] == lab]
        if len(g) < 30:
            continue
        print(f"  {lab:>16}{len(g):>9,}{g['spread'].median():>15.2f}"
              f"{g['half_pct'].median():>15.2f}%{g['half_pct'].quantile(.9):>11.2f}%")

    print("\nBy MONEYNESS (negative = OTM):")
    print(f"  {'moneyness %':>16}{'n':>9}{'median premium':>16}{'half-spread %':>16}")
    mbins = [-100, -5, -3, -2, -1, 0, 1, 2, 100]
    mlabels = ["<-5", "-5:-3", "-3:-2", "-2:-1", "-1:0", "0:1", "1:2", ">2"]
    df["mbucket"] = pd.cut(df["moneyness"], bins=mbins, labels=mlabels)
    for lab in mlabels:
        g = df[df["mbucket"] == lab]
        if len(g) < 30:
            continue
        print(f"  {lab:>16}{len(g):>9,}{g['mid'].median():>16.2f}{g['half_pct'].median():>15.2f}%")

    atm = df[df["moneyness"].abs() <= 1]
    print("\n" + "=" * 96)
    print("MODEL TO APPLY TO THE 1m CSV DATA")
    print("=" * 96)
    print(f"  ATM (+/-1%): median half-spread {atm['half_pct'].median():.2f}% of premium, "
          f"p90 {atm['half_pct'].quantile(.9):.2f}%")
    print(f"  A round trip crosses the spread TWICE: "
          f"~{atm['half_pct'].median() * 2:.2f}% of premium at ATM.")
    print()
    print("  Suggested SPREAD_HALF_PCT_BY_PREMIUM for the backtest loader:")
    out = {}
    for lab in labels:
        g = df[df["pbucket"] == lab]
        if len(g) >= 30:
            out[lab] = round(float(g["half_pct"].median()), 2)
    print(f"    {out}")


if __name__ == "__main__":
    main()
