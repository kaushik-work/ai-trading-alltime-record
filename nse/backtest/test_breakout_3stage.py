"""Breakout-retest with the exit the notes ACTUALLY specify, plus a chop filter.

The earlier test (test_breakout_retest.py) used a single stop-or-target exit.
The source setup specifies a THREE stage exit and an explicit no-trade rule
that were both omitted:

    "Book Partial at 1R"
    "Move SL to Breakeven"
    "Let Rest Run for 2R-3R"
    "NO TRADE IN CHOPPY MARKET"

Booking half at 1R and moving the remainder's stop to entry converts a large
share of full losers into half-losses or scratches. That changes win rate and
expectancy materially, so testing without it under-tests the strategy.

The chop filter is the other omission: breakout-retest fails precisely in
range-bound conditions, and the notes call it out in a red box. Implemented as
a range-expansion test — the recent range must exceed its own longer-run
median, i.e. the market must be moving before a breakout means anything.

Same discipline as everything else: TRAIN 2021-23, VALIDATE 2024, TEST 2025-26
touched once. Index points, gross.

Usage:
    python -m nse.backtest.test_breakout_3stage
    python -m nse.backtest.test_breakout_3stage --sweep
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from nse.backtest.nifty_loader import load_spot
from nse.backtest.test_breakout_retest import prepare, ENTRY_START, ENTRY_END, SQUARE_OFF

PARTIAL_FRAC = 0.5      # fraction booked at 1R


def add_chop(bars: pd.DataFrame, lookback: int = 12, med_win: int = 60) -> pd.DataFrame:
    """Range expansion. choppy = recent range below its own median."""
    g = bars.groupby("date", sort=False)
    rng = bars["high"] - bars["low"]
    bars["rng_ma"] = g.apply(
        lambda d: (d["high"] - d["low"]).rolling(lookback, min_periods=4).mean()
    ).reset_index(level=0, drop=True)
    bars["rng_med"] = rng.rolling(med_win, min_periods=20).median()
    bars["expansion"] = bars["rng_ma"] / bars["rng_med"].replace(0, np.nan)
    return bars


def run(bars: pd.DataFrame, sl_pts: float, rr: float, *, three_stage: bool,
        chop_min: float, vol_mult: float = 1.2, max_trades: int = 2) -> pd.DataFrame:
    """Returns one row per trade, in index points (1.0 = full size)."""
    trades: list[dict] = []
    for date, d in bars.groupby("date", sort=True):
        d = d.reset_index(drop=True)
        pos, pending, taken = None, None, 0
        for i in range(len(d)):
            row = d.iloc[i]
            t = row["hhmm"]

            if pos is not None:
                hi, lo, dirn = row["high"], row["low"], pos["dir"]
                # Stage 1: book PARTIAL_FRAC at 1R, then stop -> breakeven.
                if three_stage and not pos["partial"]:
                    hit_1r = (hi >= pos["r1"]) if dirn > 0 else (lo <= pos["r1"])
                    if hit_1r:
                        pos["booked"] = PARTIAL_FRAC * sl_pts      # +1R on half
                        pos["partial"] = True
                        pos["sl"] = pos["entry"]                   # breakeven
                remaining = (1 - PARTIAL_FRAC) if (three_stage and pos["partial"]) else 1.0
                out = None
                if dirn > 0:
                    if lo <= pos["sl"]:   out = (pos["sl"], "stop" if not pos["partial"] else "be")
                    elif hi >= pos["tp"]: out = (pos["tp"], "target")
                else:
                    if hi >= pos["sl"]:   out = (pos["sl"], "stop" if not pos["partial"] else "be")
                    elif lo <= pos["tp"]: out = (pos["tp"], "target")
                if out is None and t >= SQUARE_OFF:
                    out = (row["close"], "eod")
                if out:
                    px, why = out
                    pts = pos["booked"] + remaining * dirn * (px - pos["entry"])
                    trades.append({"date": date, "pts": pts, "reason": why,
                                   "partial": pos["partial"]})
                    pos = None
                continue

            if taken >= max_trades or t >= SQUARE_OFF or not (ENTRY_START <= t <= ENTRY_END):
                continue
            if not np.isfinite(row["hh"]) or not np.isfinite(row["ll"]):
                continue
            # NO TRADE IN CHOPPY MARKET — require range expansion.
            if chop_min > 0:
                exp = row.get("expansion")
                if not np.isfinite(exp) or exp < chop_min:
                    continue
            vol_ok = row["volume"] >= vol_mult * (row["vol_ma"] or 0)
            if pending is None:
                if row["close"] > row["hh"] and vol_ok:
                    pending = {"dir": 1, "level": row["hh"], "bar": i}
                elif row["close"] < row["ll"] and vol_ok:
                    pending = {"dir": -1, "level": row["ll"], "bar": i}
                continue
            if i - pending["bar"] > 6:
                pending = None
                continue
            dirn, lvl = pending["dir"], pending["level"]
            retested = row["low"] <= lvl <= row["high"]
            held = (row["close"] > lvl) if dirn > 0 else (row["close"] < lvl)
            aligned = ((row["close"] > row["ema20"] and row["close"] > row["vwap"])
                       if dirn > 0 else
                       (row["close"] < row["ema20"] and row["close"] < row["vwap"]))
            if retested and held and aligned:
                e = row["close"]
                pos = {"dir": dirn, "entry": e, "sl": e - dirn * sl_pts,
                       "r1": e + dirn * sl_pts, "tp": e + dirn * sl_pts * rr,
                       "partial": False, "booked": 0.0}
                taken += 1
                pending = None
        if pos is not None:
            rem = (1 - PARTIAL_FRAC) if (three_stage and pos["partial"]) else 1.0
            trades.append({"date": date,
                           "pts": pos["booked"] + rem * pos["dir"] * (d.iloc[-1]["close"] - pos["entry"]),
                           "reason": "eod", "partial": pos["partial"]})
    return pd.DataFrame(trades)


def split_of(d):
    y = pd.Timestamp(d).year
    return "TRAIN" if y <= 2023 else ("VALID" if y == 2024 else "TEST")


def summarise(t: pd.DataFrame, label: str) -> None:
    if t.empty:
        print(f"  {label:34} no trades")
        return
    t = t.copy()
    t["sp"] = t["date"].map(split_of)
    cells = [t[t["sp"] == k]["pts"].sum() for k in ("TRAIN", "VALID", "TEST")]
    allpos = all(c > 0 for c in cells)
    print(f"  {label:34}{len(t):>7}{(t['pts'] > 0).mean() * 100:>7.0f}%"
          + "".join(f"{c:>11,.0f}" for c in cells)
          + f"{t['pts'].sum():>11,.0f}" + ("   ALL +" if allpos else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--sl", type=float, default=20.0)
    ap.add_argument("--rr", type=float, default=3.0)
    ap.add_argument("--chop", type=float, default=1.0)
    args = ap.parse_args()

    print("Preparing bars ...", flush=True)
    bars = add_chop(prepare(load_spot()))
    print(f"  {len(bars):,} 5m bars, {bars['date'].nunique():,} sessions\n")

    print("=" * 104)
    print("BREAKOUT-RETEST — does the 3-stage exit + chop filter change the answer?")
    print("=" * 104)
    print(f"  {'variant':34}{'trades':>7}{'WR':>8}{'TRAIN':>11}{'VALID':>11}"
          f"{'TEST':>11}{'TOTAL':>11}")

    if args.sweep:
        for sl in (15, 20, 25):
            for rr in (2.0, 3.0, 5.0):
                for chop in (0.0, 1.0, 1.15):
                    t = run(bars, float(sl), float(rr), three_stage=True, chop_min=chop)
                    summarise(t, f"3stage SL{sl} {rr:g}R chop>{chop:g}")
        return

    for label, three, chop in (
        ("1-stage, no chop  (original)", False, 0.0),
        ("1-stage + chop filter", False, args.chop),
        ("3-stage, no chop", True, 0.0),
        ("3-stage + chop filter", True, args.chop),
    ):
        summarise(run(bars, args.sl, args.rr, three_stage=three, chop_min=chop),
                  label)

    print(f"\n  config: SL {args.sl:.0f}pts, target {args.rr:g}R, "
          f"partial {PARTIAL_FRAC:.0%} at 1R then breakeven, chop>{args.chop:g}")
    print("  A variant is only believable if TRAIN, VALID and TEST are all positive.")


if __name__ == "__main__":
    main()
