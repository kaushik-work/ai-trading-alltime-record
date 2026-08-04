"""Is the live synthetic-forward ENTRY_PCT gate physically reachable?

The live NSE strategy fires when the synthetic forward (K + CE - PE) deviates
from spot by more than ENTRY_PCT. Running it over 2025 produced zero trades,
and Mongo shows nse_signals empty in production too - so this measures the
actual distribution of that deviation and compares it to the gate.

Put-call parity is an arbitrage identity. The deviation is bounded by carry
plus transaction costs, which on a weekly index option is single-digit basis
points - not the 60bps the gate demands. A gate above the physical range of
the quantity it measures can never trigger.

Usage:
    python -m nse.backtest.diag_parity_gate --days 90 --at 11:00
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from nse.backtest.nifty_loader import DEFAULT_ROOT, clean_day
from nse.config import ENTRY_PCT, MONEYNESS


def deviations(days: int, at: str) -> np.ndarray:
    files = sorted(DEFAULT_ROOT.glob("*/NIFTY_*_1m.csv"))
    step = max(1, len(files) // days)
    out: list[float] = []
    for f in files[::step][:days]:
        try:
            d = clean_day(pd.read_csv(f, usecols=["datetime", "option_type", "close",
                                                  "strike_price", "spot"]))
        except Exception:
            continue
        if d.empty:
            continue
        d["dt"] = pd.to_datetime(d["datetime"], errors="coerce")
        d = d.dropna(subset=["dt"])
        d = d[d["dt"].dt.strftime("%H:%M") == at]
        if d.empty:
            continue
        spot = float(d["spot"].median())
        d["isC"] = d["option_type"].astype(str).str.upper().str.startswith("C")
        d = d.drop_duplicates(subset=["isC", "strike_price"], keep="last")
        ce = d[d["isC"]].set_index("strike_price")["close"]
        pe = d[~d["isC"]].set_index("strike_price")["close"]
        for K in ce.index.intersection(pe.index):
            if abs(K - spot) / spot > MONEYNESS:
                continue
            c, p = float(ce.loc[K]), float(pe.loc[K])
            if c <= 0 or p <= 0:
                continue
            out.append(((K + c - p) - spot) / spot)
    return np.array(out) * 100


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--at", default="11:00")
    args = ap.parse_args()

    a = deviations(args.days, args.at)
    if a.size == 0:
        print("no observations")
        return

    gate = ENTRY_PCT * 100
    print("=" * 88)
    print("PUT-CALL PARITY DEVIATION vs THE LIVE ENTRY GATE")
    print("=" * 88)
    print(f"  {a.size:,} strike-observations at {args.at}, ATM +/-{MONEYNESS * 100:.0f}%\n")
    print(f"  mean   {a.mean():+.4f}%      median {np.median(a):+.4f}%")
    print(f"  std     {a.std():.4f}%      max |dev| {np.abs(a).max():.4f}%")
    print(f"  p01    {np.percentile(a, 1):+.4f}%      p99    {np.percentile(a, 99):+.4f}%")
    print()
    hit = int((np.abs(a) >= gate).sum())
    print(f"  LIVE GATE ENTRY_PCT = {gate:.2f}%")
    print(f"  observations reaching it: {hit} / {a.size} ({hit / a.size * 100:.3f}%)")
    print()
    print("  Reachability at other thresholds:")
    for q in (0.05, 0.10, 0.20, 0.30, 0.40, 0.50, gate):
        print(f"    |dev| >= {q:.2f}% : {(np.abs(a) >= q).mean() * 100:6.2f}% of observations")
    print()
    if hit == 0:
        print("  VERDICT: the gate sits ABOVE the physical maximum of the quantity it")
        print("  measures, so the strategy cannot fire - which is why nse_signals is")
        print("  empty in production. The mean deviation is the cost of carry, exactly")
        print("  what put-call parity predicts; the data is fine, the threshold is not.")


if __name__ == "__main__":
    main()
