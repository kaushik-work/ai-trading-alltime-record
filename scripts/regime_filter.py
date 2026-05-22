"""
Regime-conditional analysis of the Q5 atm_straddle signal.

Renaissance-style insight: a signal that works on average may actually only
work in one regime. Classify every 5-min bar into a regime, then break down
Q5 signal performance by regime. If Q5 only works in 'trending up' regime
and loses money in 'chop' regime, we've cut the SL hit rate by filtering.

Regimes (rule-based, no HMM needed for 13 days of data):
  trend_up    NIFTY spot up > +0.15% over last 30 min (6 bars) AND positive slope
  trend_down  NIFTY spot down < -0.15% over last 30 min   AND negative slope
  chop        everything else (|move| < 0.15% over 30m, low slope magnitude)

For each regime: count Q5 signals, count TPs/SLs, compute PF.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: F401
import numpy as np
import pandas as pd
from core import mongo  # noqa: E402

from backtest_straddle_signal import (
    _load_snapshots, _atm_straddle_per_bar, _trailing_thresholds,
    _index_premium_series, _walk_forward, LOT_SIZE,
)

WINDOW_BARS = 6        # 6 × 5min = 30min lookback for regime
TREND_THRESHOLD = 0.0015   # 0.15% return over WINDOW_BARS defines a trend


def classify_regime(bars: pd.DataFrame) -> pd.DataFrame:
    """Add a 'regime' column to the per-bar table."""
    bars = bars.copy().sort_values(["date", "ts"]).reset_index(drop=True)
    regimes = []
    for date, g in bars.groupby("date", sort=True):
        g = g.reset_index(drop=True)
        # Trailing return: spot[i] / spot[i-WINDOW_BARS] - 1
        spots = g["spot"].values
        for i in range(len(g)):
            if i < WINDOW_BARS:
                regimes.append("warmup")
                continue
            ret = spots[i] / spots[i - WINDOW_BARS] - 1
            # Slope (last 6 bars OLS)
            x = np.arange(WINDOW_BARS)
            y = spots[i - WINDOW_BARS + 1 : i + 1]
            slope = np.polyfit(x, y, 1)[0]
            if ret >= TREND_THRESHOLD and slope > 0:
                regimes.append("trend_up")
            elif ret <= -TREND_THRESHOLD and slope < 0:
                regimes.append("trend_down")
            else:
                regimes.append("chop")
    bars["regime"] = regimes
    return bars


def _run_backtest_with_regime(bar_tbl: pd.DataFrame, premium_series: dict,
                              thresholds: dict, sl_dist: float, rr: float,
                              qty: int) -> list:
    """Run the standard Q5 CE backtest, but tag each trade with the
    entry-bar's regime."""
    trades = []
    open_until: dict = {}
    for r in bar_tbl.itertuples(index=False):
        thr = thresholds.get(r.date)
        if thr is None or r.atm_straddle < thr:
            continue
        if r.date in open_until and r.ts.to_pydatetime() < open_until[r.date]:
            continue
        if r.regime == "warmup":
            continue
        key = (r.date, int(r.atm_strike), "CE")
        series = premium_series.get(key)
        if not series:
            continue
        entry_dt = entry_premium = None
        for dt, ltp in series:
            if dt >= r.ts.to_pydatetime():
                entry_dt = dt
                entry_premium = ltp
                break
        if entry_premium is None:
            continue
        exit_dt, exit_premium, reason = _walk_forward(
            series, entry_dt, entry_premium, sl_dist, rr
        )
        pnl = round((exit_premium - entry_premium) * qty, 2)
        trades.append({
            "date":   r.date,
            "regime": r.regime,
            "reason": reason,
            "pnl":    pnl,
        })
        open_until[r.date] = exit_dt
    return trades


def main():
    db = mongo.get_db()
    if db is None:
        print("Mongo unreachable.")
        sys.exit(1)

    df = _load_snapshots(db, "NIFTY")
    bar_tbl = _atm_straddle_per_bar(df)
    bar_tbl = classify_regime(bar_tbl)
    thresholds = _trailing_thresholds(bar_tbl, 5, 0.70)
    premium_series = _index_premium_series(df)

    # Regime distribution across all bars
    total_dist = bar_tbl["regime"].value_counts().to_dict()
    print(f"\n=== Regime distribution across all {len(bar_tbl):,} bars ===")
    for r, c in sorted(total_dist.items(), key=lambda x: -x[1]):
        print(f"  {r:>11}  {c:>5}  ({c/len(bar_tbl)*100:>4.1f}%)")

    # Backtest with regime tags
    trades = _run_backtest_with_regime(bar_tbl, premium_series, thresholds,
                                        10.0, 3.0, LOT_SIZE)
    if not trades:
        print("No trades.")
        return

    print(f"\n=== Q5 Trade outcome by regime ===")
    by_reg: dict = defaultdict(lambda: {"n": 0, "wins": [], "losses": [], "trades": []})
    for t in trades:
        r = by_reg[t["regime"]]
        r["n"] += 1
        r["trades"].append(t)
        if t["pnl"] > 0:
            r["wins"].append(t["pnl"])
        elif t["pnl"] < 0:
            r["losses"].append(t["pnl"])

    print(f"  {'regime':>11}  {'n':>3}  {'WR%':>5}  {'TP':>3}  {'SL':>3}  "
          f"{'EOD':>3}  {'PF':>6}  {'Net Rs':>9}  {'Exp Rs':>8}")
    print("  " + "-" * 70)
    for reg, v in sorted(by_reg.items(), key=lambda x: -x[1]["n"]):
        n = v["n"]
        if n == 0:
            continue
        wr = len(v["wins"]) / n * 100
        gw = sum(v["wins"])
        gl = abs(sum(v["losses"]))
        pf = (gw / gl) if gl > 0 else float("inf")
        net = sum(t["pnl"] for t in v["trades"])
        tp_n = sum(1 for t in v["trades"] if t["reason"] == "TP")
        sl_n = sum(1 for t in v["trades"] if t["reason"] == "SL")
        eod_n = sum(1 for t in v["trades"] if t["reason"].startswith("EOD"))
        pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"  {reg:>11}  {n:>3}  {wr:>5.1f}  {tp_n:>3}  {sl_n:>3}  {eod_n:>3}  "
              f"{pf_str:>6}  {net:>+9,.0f}  {int(net/n):>+8,d}")

    # Verdict
    print(f"\n  If one regime has PF much higher than others, filtering by regime")
    print(f"  improves the strategy. If PF is roughly equal across regimes, the")
    print(f"  Q5 signal is regime-agnostic and filtering doesn't help.")


if __name__ == "__main__":
    main()
