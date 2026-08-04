"""When is "add up the Greeks" a valid way to predict an option's P&L?

THE CLAIM UNDER TEST
    The standard desk shortcut estimates tomorrow's premium as

        new = old + delta*dS + 0.5*gamma*dS^2 + theta*dt + vega*dIV

    This is a second-order Taylor expansion of the Black-Scholes surface. Like
    every Taylor expansion it is accurate for SMALL steps and diverges for
    large ones. Nobody states the range of validity, so this measures it.

WHY IT MATTERS HERE
    Greek-sum P&L is used for live risk (fast, no repricing) and for sizing.
    If it is wrong by 100% in exactly the conditions we trade — 0-1 DTE NIFTY
    weeklies — then the risk number on the screen is fiction. That is worth
    knowing before it is worth optimising.

Usage:
    python -m nse.quant.greeks_taylor_check
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np

from nse.quant.black_scholes import price, greeks

R = 0.07


def taylor(p0, g, dS, dIV, dt_days):
    """Greek-sum estimate. vega is per vol POINT, theta per DAY."""
    return (p0 + g.delta * dS + 0.5 * g.gamma * dS ** 2
            + g.theta * dt_days + g.vega * dIV)


def worked_example() -> None:
    """The textbook example, checked line by line."""
    S, K, sig, T = 24000.0, 24200.0, 0.1587, 1 / 365
    g = greeks(S, K, T, R, sig, "C")
    p0 = float(price(S, K, T, R, sig, "C"))

    print("=" * 92)
    print("A WORKED EXAMPLE FROM A QUANT NOTE, CHECKED AGAINST BLACK-SCHOLES")
    print("=" * 92)
    print("  Setup as stated: NIFTY 24000 spot, 24200 CE, 1 DTE, VIX 15.87%,")
    print("  premium Rs 60, delta 0.30, gamma 0.005, theta -15/day, vega 8.\n")

    print(f"  {'quantity':>10}{'as stated':>12}{'Black-Scholes':>15}   verdict")
    for lab, stated, actual in (
        ("premium", 60.0, p0), ("delta", 0.30, g.delta),
        ("gamma", 0.005, g.gamma), ("theta", -15.0, g.theta),
        ("vega", 8.0, g.vega),
    ):
        off = abs(stated - actual) / max(abs(actual), 1e-9)
        print(f"  {lab:>10}{stated:>12.4f}{actual:>15.4f}   "
              + ("consistent" if off < 0.25 else f"{off:.0%} off"))
    print("\n  ERROR 1: the stated Greeks do not belong to the stated contract.")
    print("  No single (S,K,T,sigma) produces that set — they were assembled by")
    print("  hand. Greeks are not free parameters; they are all determined by")
    print("  the same five inputs, so they must be generated together.")

    print("\n  ERROR 2: the gamma term is mis-multiplied.")
    print("    0.5 * gamma * dS^2  =  0.5 * 0.005 * 100^2")
    print(f"                        =  {0.5 * 0.005 * 100 ** 2:.2f}, "
          "but the note carries 2.50")
    stated_sum = 60 + 30 + 2.50 - 15 - 8
    fixed_sum = 60 + 30 + 25.0 - 15 - 8
    print(f"    note's total  Rs {stated_sum:.2f}      corrected  Rs {fixed_sum:.2f}")

    print("\n  ERROR 3 (the expensive one): the step takes T to ZERO.")
    print("  The contract has 1 day left and the example advances one day, so")
    print("  the option EXPIRES. At 24100 a 24200 call is out of the money.")
    exact = max(24100.0 - K, 0.0)
    print(f"    Greek-sum says       Rs {fixed_sum:.2f}")
    print(f"    true expiry value    Rs {exact:.2f}")
    print(f"    error                Rs {fixed_sum - exact:.2f}  "
          "— the entire position")
    print("\n  Taylor expansions describe a LOCAL slope. Expiry is not local:")
    print("  gamma and theta are singular at T=0, so no number of correction")
    print("  terms rescues it. At expiry you do not approximate — you settle.")


def validity_map() -> None:
    """Where does the Greek-sum actually hold? Reprice and compare."""
    S, K, sig = 24000.0, 24000.0, 0.15
    print("\n" + "=" * 92)
    print("VALIDITY MAP — Greek-sum error vs exact reprice, ATM NIFTY call")
    print("=" * 92)
    print("  One session elapses (dt = 1 day), IV unchanged. Error as % of the")
    print("  true repriced premium.\n")

    moves = [10, 25, 50, 100, 200, 400]
    print(f"  {'DTE':>5}{'premium':>10}" + "".join(f"{m:>9}" for m in moves))
    for dte in (30, 15, 7, 3, 2, 1):
        T = dte / 365
        g = greeks(S, K, T, R, sig, "C")
        p0 = float(price(S, K, T, R, sig, "C"))
        cells = []
        for m in moves:
            Tn = max(T - 1 / 365, 0.0)
            ex = (float(price(S + m, K, Tn, R, sig, "C")) if Tn > 0
                  else max(S + m - K, 0.0))
            est = taylor(p0, g, m, 0.0, 1.0)
            cells.append(np.nan if ex < 1e-9 else (est - ex) / ex * 100)
        print(f"  {dte:>5}{p0:>10.2f}"
              + "".join(f"{c:>8.1f}%" if np.isfinite(c) else f"{'inf':>9}"
                        for c in cells))

    print("\n  The pattern is NOT 'bigger move = worse'. Read the DTE-1 row:")
    print("    move  25 -> +104%    move 100 -> -0.7%    move 400 -> +2.9%")
    print("  Error is driven by how much of the premium is OPTIONALITY, not by")
    print("  the size of the move. Far from the strike an option is nearly")
    print("  linear (delta pinned at 0 or 1) and the expansion is exact again.")
    print("  The blow-up is near the strike with little time left — where the")
    print("  true value collapses to intrinsic but the Greek-sum keeps paying")
    print("  for time value that no longer exists.")
    print("\n  Nor is the error one-signed: it is +104% at one cell and -0.7% at")
    print("  the next. You cannot treat it as a conservative bias in either")
    print("  direction, which rules out 'add a safety margin' as a fix.")

    print("\n  RULE derived from the table:")
    print("    DTE >= 7    error < 0.4% even on a 400-point move — Greek-sum fine")
    print("    DTE 2-3     error a few % near the money — usable for risk, not P&L")
    print("    DTE <= 1    error up to 100%+ near the money — ALWAYS reprice")
    print("  Our NIFTY trading sits at 0-2 DTE, i.e. entirely inside the region")
    print("  where the shortcut fails. Live Greeks must come from a repricer.")


def vix_daily_move() -> None:
    """The VIX/sqrt(252) rule — correct, but only per TRADING day."""
    print("\n" + "=" * 92)
    print("THE VIX -> DAILY MOVE RULE, CHECKED")
    print("=" * 92)
    vix = 16.0
    print(f"  India VIX {vix}%: quoted as an ANNUALISED 30-CALENDAR-DAY vol.\n")
    cal = vix * np.sqrt(30 / 365)
    td = 30 * 252 / 365
    print(f"    over 30 calendar days   {vix} * sqrt(30/365)       = {cal:.3f}%")
    print(f"    trading days in those   30 * 252/365              = {td:.1f}")
    print(f"    per trading day         {cal:.3f} / sqrt({td:.1f})       "
          f"= {cal / np.sqrt(td):.3f}%")
    print(f"    shortcut                {vix} / sqrt(252)          "
          f"= {vix / np.sqrt(252):.3f}%")
    print("\n  These agree exactly — the calendar/trading-day factors cancel:")
    print("    sqrt(30/365) / sqrt(30*252/365) == 1/sqrt(252)")
    print("  So VIX/15.87 is right, PROVIDED the answer is read as a move per")
    print("  TRADING day. Applying it to a calendar day, or comparing it with a")
    print("  realised vol computed on calendar days, double-counts weekends.")
    print("\n  The 68% band it implies assumes NORMAL returns. Index returns are")
    print("  fat-tailed, so that coverage is an assumption, not a fact — it is")
    print("  measured against our own data in test_vix_coverage.py.")


if __name__ == "__main__":
    worked_example()
    validity_map()
    vix_daily_move()
