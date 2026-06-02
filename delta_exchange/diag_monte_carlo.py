"""
Monte Carlo Strategy Projection — v5 forward simulation
========================================================
Same idea as the Nasdaq example you shared (500 paths over 60 days from
the apr-2 close): bootstrap from the strategy's empirical per-trade return
distribution and project N future paths.

Difference vs typical MC of an asset:
  - Asset MC samples from log-return distribution of PRICES.
  - Strategy MC samples from per-trade PnL distribution of the STRATEGY.

Output (terminal-graphic): histogram of terminal equities, percentile bands
over the trajectory, win-probability of ending above the starting capital.

Usage:
  UNDERLYING=BTC ./.venv/Scripts/python diag_monte_carlo.py
  UNDERLYING=ETH ./.venv/Scripts/python diag_monte_carlo.py
"""

import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
from pathlib import Path
import numpy as np
import pandas as pd

UNDERLYING = os.environ.get("UNDERLYING", "BTC").upper()
DATA = (Path(__file__).parent / "data") if UNDERLYING == "BTC" \
       else (Path(__file__).parent / "data" / UNDERLYING.lower())
TRADE_LOG  = DATA / "v5_trades.csv"

START_EQUITY  = 10_000.0
N_PATHS       = 500       # like the Nasdaq study — 500 simulated paths
N_FUTURE_TRADES = 129     # ~ a 3-month window of trade legs at v5 cadence
SEED          = 42
LEVERAGE      = float(os.environ.get("LEVERAGE", "1.0"))   # override via env


def sparkline(values, width=60, height=14):
    """Render a simple text histogram."""
    if len(values) == 0: return "(empty)"
    lo, hi = min(values), max(values)
    if lo == hi: return f"all {lo:.2f}"
    bins = np.linspace(lo, hi, width + 1)
    counts, _ = np.histogram(values, bins=bins)
    max_c = max(counts) or 1
    lines = []
    for row in range(height, 0, -1):
        cutoff = max_c * row / height
        line = "".join("█" if c >= cutoff else " " for c in counts)
        lines.append(line)
    return "\n".join(lines), bins


def main():
    if not TRADE_LOG.exists():
        print(f"No trade log at {TRADE_LOG}. Run v5 first with UNDERLYING={UNDERLYING}.")
        return
    df = pd.read_csv(TRADE_LOG)
    pct = df["pnl_pct"].to_numpy()    # per-trade % return on notional (not equity)

    print(f"Loaded {len(pct):,} historical trade legs for {UNDERLYING}")
    print(f"  mean per-trade %    : {pct.mean()*100:+.3f}%")
    print(f"  std  per-trade %    : {pct.std()*100:.3f}%")
    print(f"  skew                : {pd.Series(pct).skew():+.2f}")
    print(f"  excess kurtosis     : {pd.Series(pct).kurt():+.2f}")
    print()
    print(f"Projecting {N_PATHS} paths × {N_FUTURE_TRADES} future trades  "
          f"(leverage {LEVERAGE}×) from ${START_EQUITY:,.0f}...")
    print()

    rng = np.random.default_rng(SEED)
    paths = np.empty((N_PATHS, N_FUTURE_TRADES + 1))
    paths[:, 0] = START_EQUITY
    # bootstrap each step IID from historical per-trade % returns, applying leverage
    samples = rng.choice(pct, size=(N_PATHS, N_FUTURE_TRADES), replace=True)
    samples *= LEVERAGE
    paths[:, 1:] = START_EQUITY * np.cumprod(1 + samples, axis=1)
    terminals = paths[:, -1]

    # ── stats ─────────────────────────────────────────────────────────────
    p5  = np.percentile(terminals, 5)
    p25 = np.percentile(terminals, 25)
    p50 = np.percentile(terminals, 50)
    p75 = np.percentile(terminals, 75)
    p95 = np.percentile(terminals, 95)
    win_prob = (terminals > START_EQUITY).mean()
    big_win  = (terminals > START_EQUITY * 2).mean()
    huge_win = (terminals > START_EQUITY * 5).mean()
    ruin     = (terminals < START_EQUITY * 0.5).mean()

    # path-level max drawdown
    cummax = np.maximum.accumulate(paths, axis=1)
    dd = (paths - cummax) / cummax
    max_dd = dd.min(axis=1)

    print("=" * 70)
    print(f"  MONTE CARLO RESULTS  —  {UNDERLYING}  @ leverage {LEVERAGE}×")
    print("=" * 70)
    print(f"  starting equity      : ${START_EQUITY:,.0f}")
    print(f"  terminal P5 (worst 5%)  : ${p5:,.0f}   ({(p5/START_EQUITY-1)*100:+.1f}%)")
    print(f"  terminal P25            : ${p25:,.0f}   ({(p25/START_EQUITY-1)*100:+.1f}%)")
    print(f"  terminal MEDIAN (P50)   : ${p50:,.0f}   ({(p50/START_EQUITY-1)*100:+.1f}%)")
    print(f"  terminal P75            : ${p75:,.0f}   ({(p75/START_EQUITY-1)*100:+.1f}%)")
    print(f"  terminal P95 (best 5%)  : ${p95:,.0f}   ({(p95/START_EQUITY-1)*100:+.1f}%)")
    print()
    print(f"  P(terminal > start)     : {win_prob*100:.1f}%")
    print(f"  P(terminal > 2× start)  : {big_win*100:.1f}%")
    print(f"  P(terminal > 5× start)  : {huge_win*100:.1f}%")
    print(f"  P(terminal < 0.5× start): {ruin*100:.1f}%   (capital halved)")
    print()
    print(f"  median path max DD      : {np.median(max_dd)*100:.2f}%")
    print(f"  worst 5% path max DD    : {np.percentile(max_dd, 5)*100:.2f}%")
    print()

    # ── trajectory percentile bands (text plot) ───────────────────────────
    print("  TRAJECTORY (percentile bands across paths over N future trades):")
    print(f"  {'step':<6} {'P5':>9} {'P25':>9} {'MEDIAN':>9} {'P75':>9} {'P95':>9}")
    print("  " + "-" * 60)
    sample_steps = [0, N_FUTURE_TRADES // 8, N_FUTURE_TRADES // 4,
                    N_FUTURE_TRADES // 2, 3 * N_FUTURE_TRADES // 4,
                    N_FUTURE_TRADES]
    for step in sample_steps:
        col = paths[:, step]
        ps = np.percentile(col, [5, 25, 50, 75, 95])
        print(f"  {step:<6} ${ps[0]:>8,.0f} ${ps[1]:>8,.0f} ${ps[2]:>8,.0f} "
              f"${ps[3]:>8,.0f} ${ps[4]:>8,.0f}")
    print()

    # ── ASCII histogram of terminals ──────────────────────────────────────
    print("  TERMINAL EQUITY HISTOGRAM (text density):")
    hist, bins = sparkline(terminals, width=60)
    print()
    print("  " + hist.replace("\n", "\n  "))
    print(f"  └{'─' * 60}┘")
    print(f"   ${bins[0]:<10,.0f}{'':>40}${bins[-1]:>15,.0f}")
    print()

    out = DATA / f"mc_paths_{int(LEVERAGE)}x.csv"
    pd.DataFrame(paths).to_csv(out, index=False)
    print(f"  all {N_PATHS} paths saved → {out.relative_to(DATA.parent)}")


if __name__ == "__main__":
    main()
