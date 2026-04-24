"""
Indicator Combination Sweep — ATR Strategy.

Tests stripping down the scorer section by section to find the minimal
set that actually generates edge. Runs the full backtest for each combo.

Usage:
  python scripts/sweep_indicators.py
  python scripts/sweep_indicators.py --months 2026-01,2026-02,2026-03,2026-04
  python scripts/sweep_indicators.py --lots 3 --slippage 1
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--months",   default="2026-01,2026-02,2026-03,2026-04")
parser.add_argument("--lots",     type=int, default=3)
parser.add_argument("--slippage", type=float, default=1.0)
parser.add_argument("--threshold",type=int, default=6)
args = parser.parse_args()

PYTHON   = sys.executable
BACKTEST = str(Path(__file__).parent / "backtest_live_atr.py")

# All skippable sections
ALL = {"sma", "rsi", "macd", "volume", "bb", "patterns",
       "pcr", "herd", "atr_filter",
       "vwap", "orb", "trend_15m", "rsi_15m", "pdh_pdl", "sr_levels"}

# Combinations to test — (label, set of sections to SKIP)
COMBOS = [
    # ── Baselines ────────────────────────────────────────────────────────────
    ("FULL (current)",          set()),
    ("NO daily MAs",            {"sma", "rsi", "macd", "volume", "bb"}),
    ("NO noise filters",        {"sma", "rsi", "macd", "volume", "bb", "patterns", "herd"}),

    # ── Intraday-only cores ───────────────────────────────────────────────────
    ("INTRADAY CORE\n"
     "  vwap+orb+trend+pdh+atr",
                                ALL - {"vwap", "orb", "trend_15m", "pdh_pdl", "atr_filter"}),

    ("INTRADAY + PCR",          ALL - {"vwap", "orb", "trend_15m", "pdh_pdl", "atr_filter", "pcr"}),
    ("INTRADAY + SR",           ALL - {"vwap", "orb", "trend_15m", "pdh_pdl", "atr_filter", "sr_levels"}),
    ("INTRADAY + RSI_15m",      ALL - {"vwap", "orb", "trend_15m", "pdh_pdl", "atr_filter", "rsi_15m"}),
    ("INTRADAY + PCR + SR",     ALL - {"vwap", "orb", "trend_15m", "pdh_pdl", "atr_filter", "pcr", "sr_levels"}),

    # ── Minimal: ORB only ─────────────────────────────────────────────────────
    ("ORB + VWAP only",         ALL - {"orb", "vwap"}),
    ("ORB + VWAP + PDH",        ALL - {"orb", "vwap", "pdh_pdl"}),
    ("ORB + VWAP + 15m trend",  ALL - {"orb", "vwap", "trend_15m"}),
    ("ORB + PCR",               ALL - {"orb", "pcr"}),
]


def run_combo(label: str, skip: set) -> dict:
    skip_arg = ",".join(sorted(skip)) if skip else ""
    cmd = [
        PYTHON, BACKTEST,
        "--months",    args.months,
        "--lots",      str(args.lots),
        "--slippage",  str(args.slippage),
        "--threshold", str(args.threshold),
        "--max-daily-trades", "3",
    ]
    if skip_arg:
        cmd += ["--skip", skip_arg]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = result.stdout

    # Parse summary line — use flexible regex; ₹ may encode oddly on Windows
    trades = 0; wr = pf = net = exp = 0.0
    for line in out.splitlines():
        if "Overall:" in line:
            # e.g. "Overall: 61 trades | WR=41.0% | PF=1.21 | Exp=Rs+473/trade"
            m = re.search(r"(\d+) trades.*?WR=([\d.]+)%.*?PF=([\d.]+|inf).*?Exp=\D*([+-]?[\d,]+)", line)
            if m:
                trades = int(m.group(1))
                wr     = float(m.group(2))
                pf     = float(m.group(3)) if m.group(3) != "inf" else 99.9
                exp    = float(m.group(4).replace(",", ""))
        if "total)" in line:
            m2 = re.search(r"\(([+-]?[\d.]+)%", line)
            if m2:
                net = float(m2.group(1))

    return {"label": label, "skip": skip_arg or "(none)", "trades": trades,
            "wr": wr, "pf": pf, "net": net, "exp": exp}


print(f"\nIndicator Sweep — {args.months} | {args.lots} lots | slip={args.slippage}pt | threshold={args.threshold}\n")
print(f"{'Combo':<35} {'#Tr':>5} {'WR%':>6} {'PF':>6} {'Net%':>7} {'Exp/tr':>8}  Sections skipped")
print("-" * 110)

results = []
for label, skip in COMBOS:
    short = label.split("\n")[0]
    print(f"  {short:<33} running...", end="\r", flush=True)
    r = run_combo(label, skip)
    results.append(r)
    print(f"  {short:<33} {r['trades']:>5} {r['wr']:>6.1f} {r['pf']:>6.2f} {r['net']:>6.1f}%  {r['exp']:>+8.0f}  {r['skip']}")

# Sort by PF descending
print("\n\n=== RANKED BY PROFIT FACTOR ===")
print(f"{'Combo':<35} {'PF':>6} {'Net%':>7} {'WR%':>6} {'#Tr':>5}")
print("-" * 65)
for r in sorted(results, key=lambda x: x["pf"], reverse=True):
    short = r["label"].split("\n")[0]
    print(f"  {short:<33} {r['pf']:>6.2f} {r['net']:>6.1f}%  {r['wr']:>5.1f}%  {r['trades']:>5}")
