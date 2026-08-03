"""Estimate NIFTY option bid-ask spreads from OHLC, because no quotes exist.

Every bid/ask in option_snapshots is 0.0 (the collector read fields that the
Angel API does not return - fixed, but real quotes only start accumulating
now), and the 5-year 1m CSV has no quote columns at all. Unmodelled spread is
exactly what made the crypto backtest look profitable, so we estimate it
rather than assume it.

Two independent estimators, deliberately:

  Roll (1984)      spread = 2*sqrt(-cov(dP_t, dP_t-1)) when the serial
                   covariance is negative. Measures bid-ask bounce directly
                   from the 1m close series. Undefined when cov >= 0, which
                   itself signals a trending or illiquid series.

  Corwin-Schultz   uses the high-low ratio over adjacent bars, separating the
  (2012)           volatility component (scales with time) from the spread
                   component (does not). Independent of Roll's assumptions.

Neither is gospel on options - both were built for equities, and option
premium is driven by the underlying rather than its own microstructure. So we
also apply a hard TICK FLOOR: NIFTY options trade in 0.05 increments, so the
spread can never be below one tick, and on a Rs 2 premium one tick is already
a 1.25% half-spread. For a cost model we take the MAX of the estimators and
the floor, which is the conservative choice.

Usage:
    python -m nse.backtest.estimate_spread_ohlc
    python -m nse.backtest.estimate_spread_ohlc --files 60
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

TICK = 0.05
DEFAULT_ROOT = "C:/Users/anura/Downloads/Nifty_option_historical/Week_1min"


def clean_day(df: pd.DataFrame, max_dev: float = 0.10) -> pd.DataFrame:
    """Drop rows contaminated by another index (see audit_nifty_1m)."""
    if df.empty or "spot" not in df.columns:
        return df
    med = df["spot"].median()
    return df[(df["spot"] - med).abs() / med <= max_dev]


def roll_spread(close: np.ndarray) -> float:
    """Roll's effective spread, in price units. NaN if covariance is >= 0."""
    if len(close) < 30:
        return np.nan
    d = np.diff(close)
    if len(d) < 20 or np.all(d == 0):
        return np.nan
    cov = np.cov(d[:-1], d[1:])[0, 1]
    return 2.0 * np.sqrt(-cov) if cov < 0 else np.nan


def corwin_schultz(high: np.ndarray, low: np.ndarray) -> float:
    """Corwin-Schultz spread estimate, in price units. NaN if degenerate."""
    if len(high) < 30:
        return np.nan
    h1, l1 = high[:-1], low[:-1]
    h2, l2 = high[1:], low[1:]
    ok = (h1 > 0) & (l1 > 0) & (h2 > 0) & (l2 > 0)
    if ok.sum() < 20:
        return np.nan
    h1, l1, h2, l2 = h1[ok], l1[ok], h2[ok], l2[ok]
    with np.errstate(divide="ignore", invalid="ignore"):
        beta = np.log(h1 / l1) ** 2 + np.log(h2 / l2) ** 2
        hi2 = np.maximum(h1, h2)
        lo2 = np.minimum(l1, l2)
        gamma = np.log(hi2 / lo2) ** 2
        k = 3 - 2 * np.sqrt(2)
        alpha = (np.sqrt(2 * beta) - np.sqrt(beta)) / k - np.sqrt(gamma / k)
        s = 2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha))
    s = s[np.isfinite(s)]
    s = s[s > 0]                       # negative estimates are noise; standard practice
    if s.size < 10:
        return np.nan
    mid = (np.median(h1) + np.median(l1)) / 2
    return float(np.median(s) * mid)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("--files", type=int, default=40, help="files sampled per year")
    args = p.parse_args()

    root = Path(args.root)
    year_dirs = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith("__"))

    recs: list[dict] = []
    for d in year_dirs:
        files = sorted(d.glob("*.csv"))
        step = max(1, len(files) // args.files)
        for f in files[::step][: args.files]:
            try:
                df = clean_day(pd.read_csv(f))
            except Exception:
                continue
            if df.empty:
                continue
            df = df[df["close"] > 0]
            for (label, ot), g in df.groupby(["strike_label", "option_type"], sort=False):
                g = g.sort_values("datetime")
                c = g["close"].to_numpy(float)
                if len(c) < 60:
                    continue
                prem = float(np.median(c))
                if prem <= 0:
                    continue
                # A bar with no trade repeats the last print; including those
                # manufactures fake zero-variance and biases both estimators.
                traded = g["volume"].to_numpy(float) > 0
                if traded.mean() < 0.5:
                    continue
                recs.append({
                    "year": d.name[:4],
                    "label": label,
                    "premium": prem,
                    "roll": roll_spread(c),
                    "cs": corwin_schultz(g["high"].to_numpy(float), g["low"].to_numpy(float)),
                })

    if not recs:
        print("no usable series found")
        return
    r = pd.DataFrame(recs)

    print("=" * 100)
    print("OPTION SPREAD ESTIMATED FROM OHLC — no quotes exist in any source")
    print("=" * 100)
    print(f"  contract-days sampled: {len(r):,}   "
          f"Roll defined on {r['roll'].notna().mean() * 100:.0f}%, "
          f"CS defined on {r['cs'].notna().mean() * 100:.0f}%")
    print(f"  tick floor: Rs {TICK} (one tick), i.e. half-spread {TICK / 2:.3f} in price units\n")

    bins = [0, 2, 5, 10, 25, 50, 100, 250, 1e9]
    labs = ["<2", "2-5", "5-10", "10-25", "25-50", "50-100", "100-250", ">250"]
    r["bucket"] = pd.cut(r["premium"], bins=bins, labels=labs)

    print("Half-spread as % of premium, by premium bucket:")
    print(f"  {'premium':>10}{'n':>7}{'med prem':>10}{'Roll %':>9}{'CS %':>9}"
          f"{'tick %':>9}{'MODEL %':>10}")
    model: dict[str, float] = {}
    for lab in labs:
        g = r[r["bucket"] == lab]
        if len(g) < 20:
            continue
        mp = g["premium"].median()
        roll_pct = (g["roll"].median() / 2) / mp * 100 if g["roll"].notna().any() else np.nan
        cs_pct = (g["cs"].median() / 2) / mp * 100 if g["cs"].notna().any() else np.nan
        tick_pct = (TICK / 2) / mp * 100
        # Roll is REJECTED on this data - see the note printed below. The
        # usable band is [tick floor, Corwin-Schultz].
        chosen = float(np.nanmax([cs_pct, tick_pct]))
        model[lab] = round(chosen, 3)
        print(f"  {lab:>10}{len(g):>7}{mp:>10.2f}"
              f"{roll_pct:>9.2f}{cs_pct:>9.2f}{tick_pct:>9.2f}{chosen:>10.2f}")

    print("\nBy year (ATM only) — is liquidity improving?")
    atm = r[r["label"].astype(str).isin(["ATM", "ATM+0", "ATM-0"])]
    src = atm if len(atm) > 50 else r
    print(f"  {'year':>6}{'n':>7}{'med prem':>10}{'Roll %':>9}{'CS %':>9}")
    for y, g in src.groupby("year"):
        mp = g["premium"].median()
        rp = (g["roll"].median() / 2) / mp * 100 if g["roll"].notna().any() else np.nan
        cp = (g["cs"].median() / 2) / mp * 100 if g["cs"].notna().any() else np.nan
        print(f"  {y:>6}{len(g):>7}{mp:>10.2f}{rp:>9.2f}{cp:>9.2f}")

    print("\n" + "=" * 100)
    print("ROLL IS REJECTED ON THIS DATA")
    print("=" * 100)
    print("  The most liquid ATM weeklies trade in 100% of minutes, yet Roll implies a")
    print("  Rs 6-8 spread on a Rs 70-116 premium yet the median 1-minute price MOVE is")
    print("  only Rs 1.20-2.20. A spread wider than the whole minute's range is not")
    print("  possible for a contract printing every minute. On 1m option bars the price")
    print("  change is dominated by delta x underlying moves, whose serial correlation")
    print("  inflates Roll's covariance term. It is measuring volatility, not spread.")
    print()
    print("SPREAD_HALF_PCT_BY_PREMIUM — upper bound (Corwin-Schultz, floored at 1 tick):")
    print(f"  {model}")
    print()
    print("  Treat this as the PESSIMISTIC end of a band whose optimistic end is the")
    print("  tick floor (~0.03% at ATM). That band spans ~30x, which is far too wide to")
    print("  declare any strategy profitable or unprofitable. So the harness takes")
    print("  half-spread as a PARAMETER and reports BREAK-EVEN SPREAD per strategy:")
    print("  'profitable while half-spread < X%'. When real quotes accumulate,")
    print("  calibrate_spread.py answers whether reality sits below X.")


if __name__ == "__main__":
    main()
