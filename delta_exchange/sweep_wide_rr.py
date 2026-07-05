"""Wide R:R sweep — proper 1:3 to 1:20 (risk 1, reward N).

Synthetic forward is traditionally run at wide R:R to catch fat-tail moves.
Math at 1:10 R:R: only need 9.1% WR to break even.
Math at 1:17 R:R: only need 5.6% WR to break even.

Tests:
  - Original direction (synth-forward famous setup)
  - INVERTED direction (control test on our data)
  - SL fixed at 0.5% (proper risk discipline)
  - TP varies: 1.5%, 2.5%, 5.0%, 7.5%, 10.0% (= 1:3, 1:5, 1:10, 1:15, 1:20)
  - Gate fixed at 0.05% (most trade opportunities)
  - 3x leverage
"""
from __future__ import annotations
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from sweep_greeks_leverage import run_variant


def main():
    USD_INR = 86.0
    start_usd = 40_000.0 / USD_INR
    SL = 0.005   # 0.5% risk — fixed
    LEV = 3.0
    GATE = 0.0005   # most lenient — let all signals through

    print("=" * 130)
    print(f"  WIDE 1:N R:R SWEEP — proper convention (risk 1, reward N)")
    print(f"  Fixed: SL=0.5% (risk), gate=0.05%, 3x leverage, BTC + ETH shared pool")
    print(f"  Direction: testing both ORIGINAL synth-forward and INVERTED")
    print("=" * 130)

    RR_LEVELS = [
        (0.005, 0.015,  "1:3"),
        (0.005, 0.025,  "1:5"),
        (0.005, 0.050,  "1:10"),
        (0.005, 0.075,  "1:15"),
        (0.005, 0.100,  "1:20"),
    ]

    for direction_label, invert in [("ORIGINAL synth-forward", False),
                                     ("INVERTED", True)]:
        print(f"\n  ── {direction_label} ──")
        print(f"  {'R:R':<6}  {'TP %':>6}  {'BE WR':>6}  "
              f"{'trades':>6}  {'wins':>4}  {'WR':>6}  "
              f"{'avg win':>9}  {'avg loss':>9}  {'PnL INR':>10}  {'verdict':<10}")
        print("  " + "─" * 100)
        for sl, tp, label in RR_LEVELS:
            r = run_variant(gate_pct=GATE, sl_pct=sl, tp_pct=tp, leverage=LEV,
                            invert=invert, start_usd=start_usd)
            n = r["n_trades"]
            be_wr = sl / (sl + tp) * 100   # exact geometric break-even
            if n == 0:
                print(f"  {label:<6}  {tp*100:>5.1f}%  {be_wr:>5.1f}%  "
                      f"{'0':>6}  {'—':>4}  {'—':>6}  "
                      f"{'—':>9}  {'—':>9}  {'0':>10}  no trades"); continue
            wins = [t for t in r["trades"] if t["pnl_usd"] > 0]
            losses = [t for t in r["trades"] if t["pnl_usd"] <= 0]
            wr = len(wins) / n * 100
            avg_w = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0
            avg_l = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0
            pnl_inr = (r["equity_final"] - start_usd) * USD_INR
            # verdict: need WR > be_wr * ~1.4 (to overcome fees)
            verdict = "PROFIT ★" if pnl_inr > 0 else ("close" if wr > be_wr else "below BE")
            print(f"  {label:<6}  {tp*100:>5.1f}%  {be_wr:>5.1f}%  "
                  f"{n:>6}  {len(wins):>4}  {wr:>5.1f}%  "
                  f"${avg_w:>+7.2f}  ${avg_l:>+7.2f}  "
                  f"{'+' if pnl_inr>=0 else ''}{pnl_inr:>+8,.0f}  {verdict:<10}")

    print("\n" + "=" * 130)
    print(f"  Read this as: at R:R 1:N, win rate must exceed BE WR (break-even)")
    print(f"  to be profitable. Each TP requires the underlying to move TP% within max-hold (72h).")
    print(f"  In a calm month, wide-TP variants (1:10+) need a big move or they hit max-hold = expire = 0.")
    print("=" * 130)


if __name__ == "__main__":
    main()
