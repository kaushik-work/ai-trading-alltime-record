"""Loader for the 5-year NIFTY 1-minute option dataset.

Two products, cached so the 1.8GB source is read once:

  spot series    1m NIFTY index closes across all 1,255 trading days.
                 This is what index-level signal research runs on.

  option day     per-contract 1m OHLC for one date, cleaned.

Data caveats this loader enforces, all found by audit_nifty_1m:

  contamination  some files carry rows from another index (NIFTY_2021-08-30
                 holds BANKNIFTY-level rows). Dropped by session-median filter.

  flat bars      O=H=L=C with volume 0 means NO TRADE happened that minute;
                 the row just repeats the last print. Treating those as
                 tradeable is the main way this data flatters a backtest, so
                 they are flagged rather than silently kept.

  spot is a CLOSE, not OHLC. The `spot` column carries one index value per
  minute, so resampling to 5m/15m gives highs and lows that are the max/min of
  1m CLOSES, not true intrabar extremes. Any strategy that depends on wicks
  touching a level will see fewer touches here than reality - the same
  under-sampling that broke the crypto backtest. Use `sample_note()` before
  building a wick-based rule on it.

Usage:
    from nse.backtest.nifty_loader import load_spot, load_option_day
    spot = load_spot()                      # cached DataFrame, 1m index closes
    day  = load_option_day("2026-02-13")    # per-contract 1m OHLC
"""
from __future__ import annotations

import glob
import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_ROOT = Path("C:/Users/anura/Downloads/Nifty_option_historical/Week_1min")
CACHE_DIR = Path(__file__).resolve().parents[2] / "db" / "nse_cache"
SPOT_CACHE = CACHE_DIR / "nifty_spot_1m.csv"
MAX_SPOT_DEV = 0.10


def _day_files(root: Path = DEFAULT_ROOT) -> list[Path]:
    return sorted(root.glob("*/NIFTY_*_1m.csv"))


def _date_of(path: Path) -> str:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    return m.group(1) if m else ""


def clean_day(df: pd.DataFrame, max_dev: float = MAX_SPOT_DEV) -> pd.DataFrame:
    """Drop rows whose spot is impossibly far from the session median.

    Spot cannot move 10% intraday, so anything beyond that is another index
    leaking into the file.
    """
    if df.empty or "spot" not in df.columns:
        return df
    med = df["spot"].median()
    if not np.isfinite(med) or med <= 0:
        return df
    return df[(df["spot"] - med).abs() / med <= max_dev]


def load_option_day(day: str, root: Path = DEFAULT_ROOT) -> pd.DataFrame:
    """Per-contract 1m OHLC for one trading date, cleaned and typed."""
    hits = list(root.glob(f"*/NIFTY_{day}_1m.csv"))
    if not hits:
        raise FileNotFoundError(f"no file for {day}")
    df = clean_day(pd.read_csv(hits[0]))
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"]).sort_values("datetime")
    # A no-trade minute repeats the previous print. Flag, never silently drop:
    # some analyses want them for continuity, entries must not use them.
    df["no_trade"] = (
        (df["volume"] == 0)
        | ((df["open"] == df["high"]) & (df["high"] == df["low"])
           & (df["low"] == df["close"]))
    )
    return df.reset_index(drop=True)


def build_spot_cache(root: Path = DEFAULT_ROOT, force: bool = False) -> pd.DataFrame:
    """Extract the 1m index series from every day file and cache it."""
    if SPOT_CACHE.exists() and not force:
        c = pd.read_csv(SPOT_CACHE, parse_dates=["datetime"])
        c["date"] = c["datetime"].dt.date
        return c

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    files = _day_files(root)
    if not files:
        raise FileNotFoundError(f"no day files under {root}")

    frames = []
    for i, f in enumerate(files, 1):
        try:
            df = pd.read_csv(f, usecols=["datetime", "spot"])
        except Exception:
            continue
        med = df["spot"].median()
        if np.isfinite(med) and med > 0:
            df = df[(df["spot"] - med).abs() / med <= MAX_SPOT_DEV]
        # One index value per minute; all contracts repeat it.
        g = df.groupby("datetime", as_index=False)["spot"].first()
        frames.append(g)
        if i % 200 == 0:
            print(f"  ... {i}/{len(files)} files", flush=True)

    out = pd.concat(frames, ignore_index=True)
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = (out.dropna(subset=["datetime"])
              .drop_duplicates("datetime")
              .sort_values("datetime")
              .reset_index(drop=True))
    out = out.rename(columns={"spot": "close"})
    out["date"] = out["datetime"].dt.date
    out.to_csv(SPOT_CACHE, index=False)
    print(f"cached {len(out):,} 1m index bars -> {SPOT_CACHE}")
    return out


def load_spot(root: Path = DEFAULT_ROOT) -> pd.DataFrame:
    return build_spot_cache(root)


def resample_spot(spot: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample the 1m index CLOSE series to higher timeframes.

    High and low are the max/min of 1m closes, NOT true intrabar extremes.
    They understate real range, so wick-touch rules will fire less often here
    than live. Documented rather than hidden.
    """
    s = spot.set_index("datetime")["close"]
    o = s.resample(rule).ohlc().dropna()
    return o.reset_index()


def sample_note() -> str:
    return (
        "NIFTY bars here are built from 1-minute index CLOSES. Highs and lows "
        "are extremes of closes, not true intrabar extremes, so any rule that "
        "needs a wick to touch a level will under-fire relative to live. For "
        "wick-sensitive rules, pull true OHLC from Angel getCandleData instead."
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    force = "--force" in sys.argv
    print("Building NIFTY 1m spot cache ...")
    spot = build_spot_cache(force=force)
    print()
    print(f"  bars       {len(spot):,}")
    print(f"  span       {spot['datetime'].min()} -> {spot['datetime'].max()}")
    print(f"  days       {spot['date'].nunique():,}")
    print(f"  level      {spot['close'].min():,.0f} -> {spot['close'].max():,.0f}")
    per_day = spot.groupby("date").size()
    print(f"  bars/day   median {per_day.median():.0f}, "
          f"min {per_day.min()}, max {per_day.max()}")
    print()
    for rule in ("5min", "15min", "60min"):
        r = resample_spot(spot, rule)
        print(f"  {rule:>6} bars: {len(r):,}")
    print()
    print("  NOTE:", sample_note())
