"""Test the "5K to 50K in 30 days" breakout-and-retest setup as specified.

The setup, taken literally from the notes:
    timeframe   5 minute
    index       NIFTY
    entry       breakout of the recent range, then a RETEST that holds,
                with price on the correct side of BOTH EMA20 and VWAP,
                and confirming volume
    stop        below the retest low / above the retest high (20-30 pts)
    target      2R to 3R
    window      09:30 - 11:30 only
    frequency   1-2 trades per day, no trade in a choppy market

Tested on the index itself, not on the option. That is deliberate: if the
DIRECTIONAL call has no edge, buying a decaying CE/PE on top of it can only be
worse, because theta and spread are pure subtractions. Establish direction
first, add the option layer only if direction survives.

Held to the same discipline as everything else: TRAIN 2021-23, VALIDATE 2024,
TEST 2025-26 touched once at the end. Reported per quarter.

Costs excluded by instruction — these are GROSS index points.

Usage:
    python -m nse.backtest.test_breakout_retest
    python -m nse.backtest.test_breakout_retest --sl 25 --rr 2.0
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

ENTRY_START, ENTRY_END = "09:30", "11:30"
SQUARE_OFF = "15:10"


def prepare(spot: pd.DataFrame) -> pd.DataFrame:
    """Resample to 5m and build EMA20, session VWAP and volume baseline."""
    s = spot.set_index("datetime")
    bars = s["close"].resample("5min").ohlc().dropna()
    vol = s["volume"].resample("5min").sum()
    bars["volume"] = vol.reindex(bars.index).fillna(0)
    bars = bars.reset_index()
    bars["date"] = bars["datetime"].dt.date
    bars["hhmm"] = bars["datetime"].dt.strftime("%H:%M")

    g = bars.groupby("date", sort=False)
    bars["ema20"] = g["close"].transform(lambda x: x.ewm(span=20, min_periods=5).mean())
    # Session-anchored VWAP on the typical price.
    tp = (bars["high"] + bars["low"] + bars["close"]) / 3
    bars["cum_pv"] = (tp * bars["volume"]).groupby(bars["date"]).cumsum()
    bars["cum_v"] = bars["volume"].groupby(bars["date"]).cumsum()
    bars["vwap"] = np.where(bars["cum_v"] > 0, bars["cum_pv"] / bars["cum_v"], tp)
    bars["vol_ma"] = g["volume"].transform(lambda x: x.rolling(10, min_periods=3).mean())
    # Rolling range for the breakout, excluding the current bar.
    bars["hh"] = g["high"].transform(lambda x: x.rolling(6, min_periods=6).max().shift(1))
    bars["ll"] = g["low"].transform(lambda x: x.rolling(6, min_periods=6).min().shift(1))
    bars["bar"] = g.cumcount()
    return bars


def run(bars: pd.DataFrame, sl_pts: float, rr: float,
        vol_mult: float, max_trades: int) -> pd.DataFrame:
    trades: list[dict] = []
    for date, d in bars.groupby("date", sort=True):
        d = d.reset_index(drop=True)
        pos = None
        taken = 0
        pending = None          # a breakout awaiting its retest
        for i in range(len(d)):
            row = d.iloc[i]
            t = row["hhmm"]

            if pos is not None:
                hi, lo = row["high"], row["low"]
                out = None
                if pos["dir"] > 0:
                    if lo <= pos["sl"]:   out = (pos["sl"], "stop")
                    elif hi >= pos["tp"]: out = (pos["tp"], "target")
                else:
                    if hi >= pos["sl"]:   out = (pos["sl"], "stop")
                    elif lo <= pos["tp"]: out = (pos["tp"], "target")
                if out is None and t >= SQUARE_OFF:
                    out = (row["close"], "eod")
                if out:
                    px, why = out
                    trades.append({"date": date, "dir": pos["dir"],
                                   "pts": pos["dir"] * (px - pos["entry"]),
                                   "reason": why})
                    pos = None
                continue

            if taken >= max_trades or t >= SQUARE_OFF:
                continue
            if not (ENTRY_START <= t <= ENTRY_END):
                continue
            if not np.isfinite(row["hh"]) or not np.isfinite(row["ll"]):
                continue

            # Stage 1 — breakout of the prior range on volume.
            vol_ok = row["volume"] >= vol_mult * (row["vol_ma"] or 0)
            if pending is None:
                if row["close"] > row["hh"] and vol_ok:
                    pending = {"dir": 1, "level": row["hh"], "bar": i}
                elif row["close"] < row["ll"] and vol_ok:
                    pending = {"dir": -1, "level": row["ll"], "bar": i}
                continue

            # Stage 2 — retest of the broken level that HOLDS, with price on
            # the right side of both EMA20 and VWAP. Give it 6 bars.
            if i - pending["bar"] > 6:
                pending = None
                continue
            dirn = pending["dir"]
            lvl = pending["level"]
            retested = (row["low"] <= lvl <= row["high"]) if dirn > 0 else \
                       (row["low"] <= lvl <= row["high"])
            held = (row["close"] > lvl) if dirn > 0 else (row["close"] < lvl)
            aligned = ((row["close"] > row["ema20"] and row["close"] > row["vwap"])
                       if dirn > 0 else
                       (row["close"] < row["ema20"] and row["close"] < row["vwap"]))
            if retested and held and aligned:
                entry = row["close"]
                sl = entry - dirn * sl_pts
                pos = {"dir": dirn, "entry": entry, "sl": sl,
                       "tp": entry + dirn * sl_pts * rr}
                taken += 1
                pending = None

        if pos is not None:
            trades.append({"date": date, "dir": pos["dir"],
                           "pts": pos["dir"] * (d.iloc[-1]["close"] - pos["entry"]),
                           "reason": "eod"})
    return pd.DataFrame(trades)


def period_of(d) -> str:
    y = pd.Timestamp(d).year
    return "TRAIN 21-23" if y <= 2023 else ("VALID 24" if y == 2024 else "TEST 25-26")


def report(t: pd.DataFrame, label: str) -> None:
    if t.empty:
        print(f"  {label}: no trades")
        return
    t = t.copy()
    t["period"] = t["date"].map(period_of)
    print(f"\n  {label}")
    print(f"    {'period':14}{'trades':>8}{'WR':>8}{'net pts':>11}{'avg':>9}{'expectancy':>13}")
    for p in ("TRAIN 21-23", "VALID 24", "TEST 25-26"):
        g = t[t["period"] == p]
        if g.empty:
            continue
        wr = (g["pts"] > 0).mean() * 100
        print(f"    {p:14}{len(g):>8}{wr:>7.1f}%{g['pts'].sum():>11,.0f}"
              f"{g['pts'].mean():>9.1f}{g['pts'].mean():>12.2f}p")
    wr = (t["pts"] > 0).mean() * 100
    print(f"    {'ALL':14}{len(t):>8}{wr:>7.1f}%{t['pts'].sum():>11,.0f}"
          f"{t['pts'].mean():>9.1f}{t['pts'].mean():>12.2f}p")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sl", type=float, default=25.0, help="stop in index points")
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--vol-mult", type=float, default=1.2)
    ap.add_argument("--max-trades", type=int, default=2)
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    print("Loading and preparing 5m bars ...", flush=True)
    bars = prepare(load_spot())
    print(f"  {len(bars):,} 5m bars, {bars['date'].nunique():,} sessions\n")

    print("=" * 92)
    print('BREAKOUT + RETEST ("5K to 50K" setup) — index points, GROSS of costs')
    print("=" * 92)
    print(f"  window {ENTRY_START}-{ENTRY_END} · EMA20 + VWAP alignment · "
          f"volume >= {args.vol_mult}x · max {args.max_trades} trades/day")

    if args.sweep:
        print(f"\n  {'SL':>5}{'RR':>6}{'trades':>8}{'WR':>8}"
              f"{'TRAIN pts':>12}{'VALID pts':>12}{'TEST pts':>12}")
        for sl in (20, 25, 30):
            for rr in (1.5, 2.0, 3.0):
                t = run(bars, sl, rr, args.vol_mult, args.max_trades)
                if t.empty:
                    continue
                t["period"] = t["date"].map(period_of)
                cells = [t[t["period"] == p]["pts"].sum()
                         for p in ("TRAIN 21-23", "VALID 24", "TEST 25-26")]
                print(f"  {sl:>5}{rr:>6.1f}{len(t):>8}{(t['pts'] > 0).mean() * 100:>7.1f}%"
                      + "".join(f"{c:>12,.0f}" for c in cells))
        return

    t = run(bars, args.sl, args.rr, args.vol_mult, args.max_trades)
    report(t, f"SL {args.sl:.0f}pts · target {args.rr:.1f}R")
    if t.empty:
        return
    print(f"\n    exits: {t['reason'].value_counts().to_dict()}")
    print(f"    direction: {t['dir'].map({1: 'long', -1: 'short'}).value_counts().to_dict()}")
    print(f"    trades/day: {len(t) / t['date'].nunique():.2f}")

    t["q"] = pd.to_datetime(t["date"]).dt.to_period("Q").astype(str)
    print(f"\n  QUARTERLY\n    {'quarter':10}{'trades':>8}{'WR':>8}{'net pts':>11}")
    for q, g in t.groupby("q"):
        print(f"    {q:10}{len(g):>8}{(g['pts'] > 0).mean() * 100:>7.1f}%"
              f"{g['pts'].sum():>11,.0f}")

    print("\n  Index points only. Executed as same-day CE/PE these are further")
    print("  reduced by theta, spread and the option's delta being below 1.")


if __name__ == "__main__":
    main()
