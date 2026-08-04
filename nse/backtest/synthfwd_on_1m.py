"""Backtest the LIVE NSE synthetic-forward strategy on the 5-year 1m dataset.

Runs nse.strategies.synthetic_forward.SyntheticForwardStrategy unchanged - the
same compute() and gate() the runner calls - against real per-contract 1m data,
and reports quarterly.

Adapter notes, because the two data shapes differ:
  option_type CALL/PUT -> side CE/PE
  strike_price         -> strike
  close                -> mark
  expiry               DERIVED: the files carry no expiry column, so the weekly
                       rule is applied (Thursday through 2025-08-28, Tuesday
                       from 2025-09-01; if the rule weekday is a holiday, the
                       previous trading day of that ISO week).
  bid/ask              ABSENT in this dataset. Entry and exit both use the
                       traded price, i.e. ZERO spread. Costs are excluded by
                       instruction, so these are GROSS results - the real
                       figure is worse by roughly four half-spreads per combo
                       round trip plus charges.

P&L is the change in the combo's net premium (CE - PE for long, reversed for
short) x lots x lot size, which is what the live runner actually holds.

Usage:
    python -m nse.backtest.synthfwd_on_1m
    python -m nse.backtest.synthfwd_on_1m --years 2025 2026
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from nse.backtest.nifty_loader import DEFAULT_ROOT, clean_day
from nse.config import (
    ENTRY_PCT, LOT_SIZES, MAX_HOLD_HOURS, MIN_STRIKES, MONEYNESS,
    PERSIST_HOURS, STOP_LOSS_PCT, TARGET_PCT, TRAIL_GIVEBACK_PCT, TRAIL_PEAK_PCT,
)
from nse.strategies.synthetic_forward import SyntheticForwardStrategy

EXPIRY_SWITCH = date(2025, 9, 1)
DECISION_MINUTES = 5          # runner's TICK_ENTRY_MINUTES
ENTRY_CUTOFF = dtime(14, 45)
SQUARE_OFF = dtime(15, 15)


def expiry_map(days: list[date]) -> dict[date, date]:
    """Trading day -> that week's expiry date."""
    by_week: dict[tuple[int, int], list[date]] = {}
    for d in sorted(days):
        iso = d.isocalendar()
        by_week.setdefault((iso[0], iso[1]), []).append(d)
    out: dict[date, date] = {}
    for wk in by_week.values():
        rule_wd = 3 if wk[-1] < EXPIRY_SWITCH else 1
        cands = [d for d in wk if d.weekday() <= rule_wd]
        exp = cands[-1] if cands else wk[-1]
        for d in wk:
            out[d] = exp
    return out


def adapt(df: pd.DataFrame, expiry: date) -> pd.DataFrame:
    """1m option file -> the frame SyntheticForwardStrategy.compute expects."""
    d = df.copy()
    d["side"] = np.where(d["option_type"].astype(str).str.upper().str.startswith("C"),
                         "CE", "PE")
    d["strike"] = pd.to_numeric(d["strike_price"], errors="coerce")
    d["mark"] = pd.to_numeric(d["close"], errors="coerce")
    d["expiry"] = pd.Timestamp(datetime.combine(expiry, dtime(15, 30)), tz="Asia/Kolkata")
    return d[["datetime", "side", "strike", "mark", "spot", "expiry", "no_trade"]]


def run_day(path: Path, expiry: date, strat: SyntheticForwardStrategy,
            hist: dict, lot: int) -> list[dict]:
    raw = clean_day(pd.read_csv(path))
    if raw.empty:
        return []
    raw["datetime"] = pd.to_datetime(raw["datetime"], errors="coerce")
    raw = raw.dropna(subset=["datetime"])
    raw["no_trade"] = (raw["volume"] == 0) | (
        (raw["open"] == raw["high"]) & (raw["high"] == raw["low"])
        & (raw["low"] == raw["close"]))
    d = adapt(raw, expiry)
    d = d[~d["no_trade"]]                     # never trade a no-trade print
    if d.empty:
        return []

    d["slot"] = d["datetime"].dt.floor(f"{DECISION_MINUTES}min")
    trades: list[dict] = []
    pos = None

    for slot, snap in d.groupby("slot", sort=True):
        t = slot.to_pydatetime().replace(tzinfo=timezone.utc)
        tod = slot.time()

        if pos is not None:
            cur = combo_value(snap, pos["strike"], pos["side"])
            if cur is not None:
                spot_now = float(snap["spot"].median())
                sign = 1 if pos["side"] == "long" else -1
                unreal = sign * (spot_now - pos["spot0"]) / pos["spot0"]
                pos["peak"] = max(pos.get("peak", 0.0), unreal)
                held_h = (slot - pos["t0"]).total_seconds() / 3600
                why = None
                if tod >= SQUARE_OFF:                       why = "eod"
                elif held_h >= MAX_HOLD_HOURS:              why = "max_hold"
                elif unreal < -STOP_LOSS_PCT:               why = "stop"
                elif (pos["peak"] >= TRAIL_PEAK_PCT
                      and pos["peak"] - unreal > TRAIL_GIVEBACK_PCT): why = "trail"
                elif unreal >= TARGET_PCT:                  why = "target"
                if why:
                    pnl = (cur - pos["entry_val"]) * lot
                    trades.append({"date": slot.date(), "side": pos["side"],
                                   "pnl": pnl, "reason": why,
                                   "held_h": held_h, "pred": pos["pred"]})
                    pos = None
            if pos is not None:
                continue

        if tod >= ENTRY_CUTOFF:
            continue
        sigs = strat.compute(snap, t)
        if not sigs:
            continue
        for s in sigs:
            hist.setdefault(s.expiry, []).append((t, s.pred))
        chosen = next((c for c in sorted(sigs, key=lambda x: abs(x.pred), reverse=True)
                       if strat.gate(c, hist)), None)
        if chosen is None:
            continue
        atm = int(round(chosen.spot / 50)) * 50
        val = combo_value(snap, atm, chosen.side)
        if val is None:
            continue
        pos = {"strike": atm, "side": chosen.side, "entry_val": val,
               "spot0": chosen.spot, "t0": slot, "peak": 0.0,
               "pred": chosen.pred * 100}

    if pos is not None:      # force flat at end of session
        last = d[d["slot"] == d["slot"].max()]
        cur = combo_value(last, pos["strike"], pos["side"])
        if cur is not None:
            trades.append({"date": d["slot"].max().date(), "side": pos["side"],
                           "pnl": (cur - pos["entry_val"]) * lot, "reason": "eod",
                           "held_h": 0.0, "pred": pos["pred"]})
    return trades


def combo_value(snap: pd.DataFrame, strike: int, side: str) -> float | None:
    ce = snap[(snap["side"] == "CE") & (snap["strike"] == strike)]["mark"]
    pe = snap[(snap["side"] == "PE") & (snap["strike"] == strike)]["mark"]
    if ce.empty or pe.empty:
        return None
    c, p = float(ce.iloc[-1]), float(pe.iloc[-1])
    if c <= 0 or p <= 0:
        return None
    return (c - p) if side == "long" else (p - c)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", default=None)
    ap.add_argument("--lot", type=int, default=None)
    args = ap.parse_args()

    files = sorted(DEFAULT_ROOT.glob("*/NIFTY_*_1m.csv"))
    if args.years:
        files = [f for f in files if any(y in f.name for y in args.years)]
    days = [pd.to_datetime(f.name[6:16]).date() for f in files]
    emap = expiry_map(days)
    lot = args.lot or LOT_SIZES.get("NIFTY", 75)

    print("=" * 96)
    print("LIVE NSE SYNTHETIC-FORWARD STRATEGY on 1m data — GROSS (no costs)")
    print("=" * 96)
    print(f"  gate {ENTRY_PCT * 100:.2f}% · persist {PERSIST_HOURS}h · "
          f"min strikes {MIN_STRIKES} · moneyness +/-{MONEYNESS * 100:.0f}%")
    print(f"  stop {STOP_LOSS_PCT * 100:.1f}% · target {TARGET_PCT * 100:.1f}% · "
          f"max hold {MAX_HOLD_HOURS}h · lot {lot}")
    print(f"  {len(files):,} sessions, decisions every {DECISION_MINUTES}m\n")

    strat = SyntheticForwardStrategy("NIFTY")
    all_trades: list[dict] = []
    for i, (f, day) in enumerate(zip(files, days), 1):
        try:
            all_trades += run_day(f, emap[day], strat, {}, lot)
        except Exception as e:
            print(f"  {f.name}: {e}")
        if i % 250 == 0:
            print(f"  ... {i}/{len(files)} sessions, {len(all_trades)} trades", flush=True)

    if not all_trades:
        print("\nNO TRADES — the live gate never triggered on this data.")
        return

    t = pd.DataFrame(all_trades)
    t["date"] = pd.to_datetime(t["date"])
    t["q"] = t["date"].dt.to_period("Q").astype(str)

    print("\n" + "=" * 96)
    print("QUARTERLY")
    print("=" * 96)
    print(f"  {'quarter':10}{'trades':>8}{'wins':>7}{'WR':>8}{'gross Rs':>14}{'avg Rs':>11}")
    for q, g in t.groupby("q"):
        w = int((g["pnl"] > 0).sum())
        print(f"  {q:10}{len(g):>8}{w:>7}{w / len(g) * 100:>7.0f}%"
              f"{g['pnl'].sum():>14,.0f}{g['pnl'].mean():>11,.0f}")
    w = int((t["pnl"] > 0).sum())
    print(f"  {'TOTAL':10}{len(t):>8}{w:>7}{w / len(t) * 100:>7.0f}%"
          f"{t['pnl'].sum():>14,.0f}{t['pnl'].mean():>11,.0f}")
    print(f"\n  exits: {t['reason'].value_counts().to_dict()}")
    print(f"  sides: {t['side'].value_counts().to_dict()}")
    print("\n  GROSS of all costs and with zero spread — this dataset has no")
    print("  bid/ask. Real results are worse by ~4 half-spreads per combo plus charges.")


if __name__ == "__main__":
    main()
