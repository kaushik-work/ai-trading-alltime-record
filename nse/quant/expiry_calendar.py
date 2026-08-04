"""Derive NIFTY expiry dates EMPIRICALLY from the option data itself.

WHY THIS EXISTS
    Every Greek is a function of T. Get the expiry date wrong and delta, gamma,
    theta and IV are all wrong together — silently, and in the same direction.
    So the expiry calendar is not a convenience, it is an input to every
    calculation downstream.

    The trap is that NIFTY's weekly expiry WEEKDAY CHANGED during our data
    window. Hardcoding "Thursday" is the same class of error as hardcoding
    LOT = 75: a constant that was true when written and silently wrong later.
    See docs/RESEARCH_LEARNINGS.md 1.8.

HOW IT IS DERIVED (no hardcoded calendar, no assumed weekday)
    On expiry day an ATM straddle has no time value left, so near the close it
    collapses. The straddle series is therefore a sawtooth, and expiry is its
    local minimum. We detect the SHAPE, not a level:

        expiry  <=>  straddle/spot is below 60% of BOTH neighbouring sessions

    A ratio test is scale-free, so it survives IV regimes and the 14k -> 26k
    move in spot. It also picks up holiday-shifted expiries with no
    special-casing, because it never refers to a weekday.

    WHY NOT A SIMPLE THRESHOLD: the first version of this file used
    "straddle/spot < 0.50%" and found 318 expiries — 1.27 per week, which is
    impossible. The extra ones were 1-DTE Wednesdays: in a low-IV regime a
    Wednesday straddle falls to ~0.5% and collides with the expiry population.
    The two populations genuinely overlap in level, so no threshold separates
    them. They do not overlap in shape.

    CONFIRMATION: the ratio rule is stable at 0.40/0.50/0.60 (261/264/264
    sessions) and an independent absolute rule at 0.30% picks the SAME 264.
    Two unrelated detectors agreeing is the evidence; a tuned constant is not.
    Only after the fix does a real gap appear — max detected 0.299% vs min
    rejected 0.324%.

Usage:
    python -m nse.quant.expiry_calendar --build     # scan + cache
    python -m nse.quant.expiry_calendar             # report the calendar

    from nse.quant.expiry_calendar import load_expiries, dte_for, is_expiry
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from nse.backtest.nifty_loader import DEFAULT_ROOT, CACHE_DIR, clean_day

EXPIRY_CACHE = CACHE_DIR / "nifty_expiry_calendar.csv"

# Expiry = local minimum of the straddle sawtooth. Scale-free on purpose:
# a LEVEL cannot separate expiry days from 1-DTE days (see module docstring).
NEIGHBOUR_RATIO = 0.60
# Cross-check only, never the primary rule. Must agree with the ratio rule.
EXPIRY_STRADDLE_PCT = 0.30
CLOSE_FROM = "15:20"


def _atm_straddle_pct(path: Path) -> tuple[float, float] | None:
    """(straddle as % of spot near the close, spot). None if unusable."""
    try:
        d = clean_day(pd.read_csv(path, usecols=[
            "datetime", "option_type", "close", "strike_price", "spot"]))
    except Exception:
        return None
    if d.empty:
        return None
    d["dt"] = pd.to_datetime(d["datetime"], errors="coerce")
    d = d.dropna(subset=["dt"])
    late = d[d["dt"].dt.strftime("%H:%M") >= CLOSE_FROM]
    if late.empty:
        return None
    spot = float(late["spot"].median())
    if not np.isfinite(spot) or spot <= 0:
        return None

    k = late.loc[(late["strike_price"] - spot).abs().idxmin(), "strike_price"]
    atm = late[late["strike_price"] == k]
    is_c = atm["option_type"].astype(str).str.upper().str.startswith("C")
    c, p = atm[is_c]["close"].median(), atm[~is_c]["close"].median()
    if not (np.isfinite(c) and np.isfinite(p)):
        return None
    return (c + p) / spot * 100.0, spot


def build(force: bool = False) -> pd.DataFrame:
    if EXPIRY_CACHE.exists() and not force:
        return pd.read_csv(EXPIRY_CACHE, parse_dates=["date"])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    files = sorted(DEFAULT_ROOT.glob("*/NIFTY_*_1m.csv"))
    for i, f in enumerate(files, 1):
        got = _atm_straddle_pct(f)
        if got:
            rows.append({"date": pd.Timestamp(f.name[6:16]),
                         "straddle_pct": got[0], "spot": got[1]})
        if i % 250 == 0:
            print(f"  ... {i}/{len(files)} sessions", flush=True)

    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    s = df["straddle_pct"].to_numpy()
    prv, nxt = np.r_[np.nan, s[:-1]], np.r_[s[1:], np.nan]
    df["is_expiry"] = (s < NEIGHBOUR_RATIO * prv) & (s < NEIGHBOUR_RATIO * nxt)
    df["weekday"] = df["date"].dt.day_name()

    # DTE = trading sessions until the next expiry INCLUSIVE (0 on expiry day).
    # Trading sessions, not calendar days: theta is realised per session, and
    # the market prices the weekend as roughly one session, not two.
    nxt = np.full(len(df), np.nan)
    pending: list[int] = []
    for i, exp in enumerate(df["is_expiry"]):
        pending.append(i)
        if exp:
            for j in pending:
                nxt[j] = i - j
            pending = []
    df["dte_sessions"] = nxt
    df.to_csv(EXPIRY_CACHE, index=False)
    print(f"cached {len(df):,} sessions -> {EXPIRY_CACHE}")
    return df


def load_expiries() -> pd.DataFrame:
    return build()


def is_expiry(date) -> bool:
    df = load_expiries()
    hit = df[df["date"] == pd.Timestamp(date)]
    return bool(hit["is_expiry"].iloc[0]) if len(hit) else False


def dte_for(date) -> float:
    """Trading sessions to expiry, 0 on expiry day. NaN if unknown."""
    df = load_expiries()
    hit = df[df["date"] == pd.Timestamp(date)]
    return float(hit["dte_sessions"].iloc[0]) if len(hit) else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    args = ap.parse_args()

    df = build(force=args.build)
    exp = df[df["is_expiry"]]

    print("=" * 88)
    print("NIFTY EXPIRY CALENDAR — derived from ATM straddle collapse, not assumed")
    print("=" * 88)
    print(f"  {len(df):,} sessions, {len(exp):,} detected expiries "
          f"({df['date'].min():%Y-%m-%d} -> {df['date'].max():%Y-%m-%d})")

    non = df[~df["is_expiry"]]
    print(f"  rate: {len(exp) / (len(df) / 5):.2f} expiries per trading week "
          + ("(weekly — plausible)" if 0.9 <= len(exp) / (len(df) / 5) <= 1.15
             else "(IMPLAUSIBLE — detector is misfiring)"))

    print("\n  Cross-check — two independent detectors must agree")
    alt = set(df[df["straddle_pct"] < EXPIRY_STRADDLE_PCT]["date"])
    got = set(exp["date"])
    print(f"    shape rule (<{NEIGHBOUR_RATIO:.0%} of both neighbours) : {len(got)}")
    print(f"    level rule (<{EXPIRY_STRADDLE_PCT}% of spot)            : {len(alt)}")
    print(f"    disagreement: {len(got ^ alt)} sessions  "
          + ("— agreed" if len(got ^ alt) <= 3 else "— INVESTIGATE"))

    print("\n  Separation AFTER the fix (a gap that exists, not one we imposed)")
    print(f"    max straddle/spot among detected  {exp['straddle_pct'].max():.3f}%")
    print(f"    min straddle/spot among rejected  {non['straddle_pct'].min():.3f}%")

    print("\n  Expiry WEEKDAY by year — the constant that changed mid-dataset")
    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    print(f"    {'year':>6}{'n':>6}" + "".join(f"{d[:3]:>9}" for d in order))
    for y, g in exp.groupby(exp["date"].dt.year):
        vc = g["weekday"].value_counts()
        print(f"    {y:>6}{len(g):>6}" + "".join(f"{vc.get(d, 0):>9}" for d in order))

    # The weekday changed mid-dataset. Locate the switch by measurement so the
    # date is a fact from the data, not a recollection about an NSE circular.
    e = exp.sort_values("date")
    thu = e[e["weekday"] == "Thursday"]["date"]
    tue = e[e["weekday"] == "Tuesday"]["date"]
    if len(thu) and len(tue):
        print(f"\n  CHANGEOVER (measured): last Thursday expiry {thu.max():%Y-%m-%d}"
              f" -> first Tuesday expiry {tue[tue > thu.max()].min():%Y-%m-%d}")
    print("  Any code that assumes a fixed expiry weekday is wrong for part of")
    print("  this dataset. Use dte_for(date) — it is measured per session.")

    print("\n  Sessions-to-expiry distribution (0 = expiry day itself)")
    vc = df["dte_sessions"].value_counts().sort_index()
    for k, v in vc.items():
        if np.isfinite(k):
            print(f"    {int(k):>3} sessions   {v:>5}  {'#' * int(v / 8)}")

    print("\n  Weekly expiries and monthly expiries land on the same weekday, so")
    print("  'is it monthly?' cannot be answered from the weekday alone.")


if __name__ == "__main__":
    main()
