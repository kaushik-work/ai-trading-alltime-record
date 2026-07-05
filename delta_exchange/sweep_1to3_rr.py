"""Proper 1:3 R:R sweep — risk 0.5%, reward 1.5%.
Need >25% WR just to break even, >30% to actually profit after fees.

Tests every reasonable combination on corrected Jun 1-20 data:
  - INVERTED direction (synth-forward has anti-edge in our data)
  - 4 gate thresholds
  - 5 filter combinations
  - Total 20 variants
"""
from __future__ import annotations
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from sweep_greeks_leverage import run_variant


def main():
    USD_INR = 86.0
    start_usd = 40_000.0 / USD_INR
    print("=" * 130)
    print(f"  PROPER 1:3 R:R SWEEP — corrected Jun 1-20 data")
    print(f"  Hard constraint: SL=0.5% (risk), TP=1.5% (reward), 3× leverage")
    print(f"  Break-even WR: 25% (pre-fee), ~30% (after ~14bps fees+slip per round)")
    print(f"  Direction: INVERTED (original synth-forward has anti-edge on this data)")
    print("=" * 130)

    BASE = dict(sl_pct=0.005, tp_pct=0.015, invert=True, leverage=3.0,
                start_usd=start_usd)

    VARIANTS = []
    for gate in (0.0005, 0.001, 0.0015, 0.002):
        gate_str = f"{gate*100:.2f}%"
        for label_suffix, extra in [
            ("no filter",                {}),
            ("RV<60%",                   dict(rv_max=0.60)),
            ("RV<50%",                   dict(rv_max=0.50)),
            ("IV<80%",                   dict(iv_max=0.80)),
            ("TTE<24h",                  dict(tte_max_h=24)),
        ]:
            VARIANTS.append((f"gate {gate_str:<6}  {label_suffix:<14}", dict(gate_pct=gate, **extra)))

    print(f"\n  {'VARIANT':<35}  {'trades':>6}  {'wins':>4}  {'WR':>5}  "
          f"{'avg win':>8}  {'avg loss':>8}  {'PnL INR':>10}  {'EV/trade':>9}")
    print("  " + "─" * 110)
    results = []
    for label, params in VARIANTS:
        r = run_variant(**{**BASE, **params})
        n = r["n_trades"]
        if n == 0:
            print(f"  {label:<35}  {'0':>6}  {'—':>4}  {'—':>5}  {'—':>8}  {'—':>8}  {'0':>10}  {'—':>9}")
            results.append((label, 0, 0, 0, params)); continue
        wins = [t["pnl_usd"] for t in r["trades"] if t["pnl_usd"] > 0]
        losses = [t["pnl_usd"] for t in r["trades"] if t["pnl_usd"] <= 0]
        pnl_inr = (r["equity_final"] - start_usd) * USD_INR
        wr = len(wins) / n * 100
        avg_w = sum(wins) / len(wins) if wins else 0
        avg_l = sum(losses) / len(losses) if losses else 0
        ev_per_trade = (r["equity_final"] - start_usd) / n * USD_INR
        flag = " ★" if pnl_inr > 0 else "  "
        print(f"  {label:<35}  {n:>6}  {len(wins):>4}  {wr:>4.1f}%  "
              f"${avg_w:>+6.2f}  ${avg_l:>+6.2f}  "
              f"{'+' if pnl_inr>=0 else ''}{pnl_inr:>+8,.0f}  "
              f"₹{ev_per_trade:>+6.0f}{flag}")
        results.append((label, n, len(wins), pnl_inr, params))

    print("\n" + "=" * 130)
    profitable = [r for r in results if r[3] > 0]
    if profitable:
        print(f"  ★ PROFITABLE VARIANTS at proper 1:3 R:R  ({len(profitable)} of {len(VARIANTS)}):")
        for label, n, w, pnl, p in sorted(profitable, key=lambda x: -x[3]):
            print(f"    +₹{pnl:>6,.0f}  ({n:>3} trades, {w} wins, {w/n*100:.1f}% WR)  {label}")
    else:
        print(f"  No variants profitable at proper 1:3 R:R across {len(VARIANTS)} configurations.")
        print(f"  → This means the strategy lacks the ~30% WR needed to overcome fees at 1:3 R:R.")
    print("=" * 130)


if __name__ == "__main__":
    main()
