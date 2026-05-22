"""
Parameter sensitivity sweep + leave-one-day-out cross-validation for the
atm_straddle Q5 backtest.

Phase 1 — sweep:
  Grid over (n_days, pct, sl_dist, rr) for CE side only. Print PF / Net / WR
  for every combo, then highlight the best by PF.

Phase 2 — LOO:
  Take the BEST combo from phase 1. Re-run it 4 times, each time excluding one
  of the eligible trading days. If PF holds across all drops, no single day is
  carrying the result. If dropping one day collapses everything, the signal
  is brittle.

Usage:
  python scripts/sweep_straddle.py
  python scripts/sweep_straddle.py --side CE
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: F401
from core import mongo  # noqa: E402

# Re-use the internals from the main backtest script
from backtest_straddle_signal import (  # noqa: E402
    _load_snapshots, _atm_straddle_per_bar, _trailing_thresholds,
    _index_premium_series, _run_backtest, LOT_SIZE,
)


def _stats(trades: list) -> dict:
    if not trades:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "net": 0, "exp": 0}
    pnls = [t["pnl"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    return {
        "n":   len(trades),
        "wr":  len(wins) / len(pnls) * 100,
        "pf":  (gross_w / gross_l) if gross_l > 0 else float("inf"),
        "net": int(sum(pnls)),
        "exp": int(sum(pnls) / len(pnls)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--side",   default="CE", choices=["CE", "PE"])
    ap.add_argument("--lots",   type=int, default=1)
    args = ap.parse_args()
    qty = LOT_SIZE * args.lots

    db = mongo.get_db()
    if db is None:
        print("Mongo unreachable.")
        sys.exit(1)

    df = _load_snapshots(db, args.symbol)
    if df.empty:
        print("No snapshots.")
        return
    bar_tbl = _atm_straddle_per_bar(df)
    premium_series = _index_premium_series(df)
    print(f"  {len(bar_tbl):,} bars across {bar_tbl['date'].nunique()} days\n")

    # ── Phase 1: sweep ────────────────────────────────────────────────────────
    grid = [
        (nd, pct, sl, rr)
        for nd  in (3, 5, 7)
        for pct in (0.70, 0.80, 0.90)
        for sl  in (10.0, 15.0, 20.0)
        for rr  in (2.0, 3.0)
    ]

    print(f"=== Sweep: {len(grid)} combos | side={args.side} ===")
    print(f"{'n_days':>6}  {'pct':>5}  {'sl':>5}  {'rr':>4}  "
          f"{'trades':>6}  {'WR%':>5}  {'PF':>6}  {'Net Rs':>10}  {'Exp Rs':>8}")
    print("-" * 78)

    results = []
    for (nd, pct, sl, rr) in grid:
        thresholds = _trailing_thresholds(bar_tbl, nd, pct)
        trades = _run_backtest(bar_tbl, premium_series, thresholds,
                               args.side, sl, rr, qty)
        s = _stats(trades)
        s.update({"nd": nd, "pct": pct, "sl": sl, "rr": rr, "trades": trades})
        results.append(s)
        pf_str = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"{nd:>6}  {pct:>5.2f}  {sl:>5.0f}  {rr:>4.1f}  "
              f"{s['n']:>6}  {s['wr']:>5.1f}  {pf_str:>6}  "
              f"{s['net']:>+10,d}  {s['exp']:>+8,d}")

    # Robustness summary
    profitable = [r for r in results if r["net"] > 0 and r["n"] >= 10]
    pf_above_1_5 = [r for r in results if r["pf"] >= 1.5 and r["n"] >= 10]
    pf_above_2_0 = [r for r in results if r["pf"] >= 2.0 and r["n"] >= 10]

    print(f"\n  Robustness:")
    print(f"    Combos with Net > 0 (n>=10):    {len(profitable)} / {len(results)}")
    print(f"    Combos with PF >= 1.5 (n>=10):  {len(pf_above_1_5)} / {len(results)}")
    print(f"    Combos with PF >= 2.0 (n>=10):  {len(pf_above_2_0)} / {len(results)}")

    # Best by PF (with min sample) — "best by pure PF" can be a tiny-sample fluke,
    # so we also pick a "best robust" = highest net P&L with substantial trade count.
    candidates = [r for r in results if r["n"] >= 10]
    if not candidates:
        print("\nNo combos with >=10 trades. Sample too thin for LOO.")
        return
    best_pf  = max(candidates, key=lambda r: r["pf"])
    big_n    = [r for r in candidates if r["n"] >= 30]
    best_net = max(big_n, key=lambda r: r["net"]) if big_n else best_pf
    print(f"\n  Best by PF (n>=10):       "
          f"n_days={best_pf['nd']}  pct={best_pf['pct']}  sl={best_pf['sl']}  rr={best_pf['rr']}  "
          f"-> PF={best_pf['pf']:.2f}  Net=Rs {best_pf['net']:+,d}  trades={best_pf['n']}")
    print(f"  Best by Net P&L (n>=30):  "
          f"n_days={best_net['nd']}  pct={best_net['pct']}  sl={best_net['sl']}  rr={best_net['rr']}  "
          f"-> PF={best_net['pf']:.2f}  Net=Rs {best_net['net']:+,d}  trades={best_net['n']}")
    # Use the "robust" one for LOO
    best = best_net

    # ── Phase 2: leave-one-day-out on best combo ──────────────────────────────
    print(f"\n=== Phase 2: Leave-one-day-out on best combo ===")
    best_trades = best["trades"]
    days = sorted({t["date"] for t in best_trades})
    print(f"  Eligible days with trades: {days}")

    print(f"\n  {'drop':>10}  {'trades':>6}  {'WR%':>5}  {'PF':>6}  {'Net Rs':>10}")
    print("  " + "-" * 50)

    # baseline (no drop)
    s = _stats(best_trades)
    pf_str = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    print(f"  {'(none)':>10}  {s['n']:>6}  {s['wr']:>5.1f}  {pf_str:>6}  {s['net']:>+10,d}")

    loo_results = []
    for d in days:
        subset = [t for t in best_trades if t["date"] != d]
        s = _stats(subset)
        s["drop"] = d
        loo_results.append(s)
        pf_str = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"  {d:>10}  {s['n']:>6}  {s['wr']:>5.1f}  {pf_str:>6}  {s['net']:>+10,d}")

    # Verdict
    print(f"\n  Verdict:")
    min_pf = min(r["pf"] for r in loo_results)
    max_pf = max(r["pf"] for r in loo_results)
    if min_pf >= 1.5:
        print(f"    PF stays >= 1.5 across all drops (min={min_pf:.2f}). Robust.")
    elif min_pf >= 1.0:
        print(f"    PF stays profitable but drops below 1.5 (min={min_pf:.2f}). Fragile.")
    else:
        print(f"    PF collapses below 1.0 on at least one drop (min={min_pf:.2f}). "
              f"Single-day driven — NOT real edge.")


if __name__ == "__main__":
    main()
