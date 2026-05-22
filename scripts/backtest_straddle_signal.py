"""
Backtest the atm_straddle Q5 signal against real option premiums.

Entry rule:
  trailing 5-day rolling Q5 — compute the 80th-percentile atm_straddle across
  bars from the previous N (default 5) trading days, excluding today. When
  today's atm_straddle exceeds that threshold AND no position is open, enter.

Direction:
  --side CE   buy ATM CE
  --side PE   buy ATM PE
  --side both run both back-to-back so we can sanity-check that CE has edge
              and PE doesn't (or vice versa).

Exit:
  SL/TP on premium with --sl-dist (default 15) and --rr (default 3.0). Same
  walk-forward as backtest_signal_log.py.

One position at a time per day. Re-entry allowed after exit.

Usage:
  python scripts/backtest_straddle_signal.py --side both
  python scripts/backtest_straddle_signal.py --side CE --csv ce_trades.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: F401  -- triggers load_dotenv()
import numpy as np
import pandas as pd
from core import mongo  # noqa: E402

LOT_SIZE  = 65
EOD_LIMIT = dtime(15, 20)


def _load_snapshots(db, symbol: str) -> pd.DataFrame:
    print(f"Loading option_snapshots for {symbol} ...", flush=True)
    cur = db.option_snapshots.find(
        {"symbol": symbol},
        projection={"_id": 0, "date": 1, "timestamp": 1, "strike": 1,
                    "option_type": 1, "ltp": 1, "spot": 1},
    )
    df = pd.DataFrame(list(cur))
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["ts", "ltp", "spot"])
    df["strike"] = df["strike"].astype(int)
    df["ltp"]    = df["ltp"].astype(float)
    df["spot"]   = df["spot"].astype(float)
    print(f"  loaded {len(df):,} rows across {df['date'].nunique()} days", flush=True)
    return df


def _atm_straddle_per_bar(df: pd.DataFrame) -> pd.DataFrame:
    """For each (date, ts): atm_strike and atm_straddle premium."""
    out = []
    for (date, ts), g in df.groupby(["date", "ts"], sort=True):
        spot = g["spot"].iloc[0]
        atm = int(g.iloc[(g["strike"] - spot).abs().argsort()[:1]]["strike"].iloc[0])
        ce = g[(g["strike"] == atm) & (g["option_type"] == "CE")]["ltp"].mean()
        pe = g[(g["strike"] == atm) & (g["option_type"] == "PE")]["ltp"].mean()
        if pd.isna(ce) or pd.isna(pe):
            continue
        out.append({"date": date, "ts": ts, "spot": spot,
                    "atm_strike": atm, "atm_straddle": ce + pe})
    return pd.DataFrame(out).sort_values(["date", "ts"]).reset_index(drop=True)


def _trailing_thresholds(bar_tbl: pd.DataFrame, n_days: int, pct: float) -> dict:
    """{date: trailing_Nday_percentile_threshold} — computed without look-ahead."""
    dates_in_order = sorted(bar_tbl["date"].unique())
    thresholds = {}
    for i, d in enumerate(dates_in_order):
        if i < n_days:
            thresholds[d] = None  # not enough history
            continue
        prior_dates = dates_in_order[i - n_days:i]
        sample = bar_tbl[bar_tbl["date"].isin(prior_dates)]["atm_straddle"]
        thresholds[d] = sample.quantile(pct)
    return thresholds


def _walk_forward(series: list, entry_dt: datetime, entry_premium: float,
                  sl_dist: float, rr: float) -> tuple:
    sl_price = round(entry_premium - sl_dist, 1)
    tp_price = round(entry_premium + sl_dist * rr, 1)
    for dt, ltp in series:
        if dt <= entry_dt:
            continue
        if dt.time() >= EOD_LIMIT:
            return dt, ltp, "EOD"
        if ltp <= sl_price:
            return dt, sl_price, "SL"
        if ltp >= tp_price:
            return dt, tp_price, "TP"
    if series:
        last_dt, last_ltp = series[-1]
        if last_dt > entry_dt:
            return last_dt, last_ltp, "EOD-last"
    return entry_dt, entry_premium, "no-data"


def _index_premium_series(df: pd.DataFrame) -> dict:
    """{(date, strike, option_type): [(dt, ltp), ...]}"""
    by_key: dict = defaultdict(list)
    for r in df.itertuples(index=False):
        by_key[(r.date, r.strike, r.option_type)].append((r.ts.to_pydatetime(), r.ltp))
    for k in by_key:
        by_key[k].sort()
    return by_key


def _run_backtest(bar_tbl: pd.DataFrame, premium_series: dict,
                  thresholds: dict, side: str,
                  sl_dist: float, rr: float, qty: int,
                  one_per_day: bool = False,
                  max_per_day: int = 4) -> list:
    trades = []
    open_until: dict = {}  # date -> exit_dt (re-entry blocked before this)
    fired_today: set = set()
    count_today: dict = {}
    for r in bar_tbl.itertuples(index=False):
        thr = thresholds.get(r.date)
        if thr is None or r.atm_straddle < thr:
            continue
        if r.date in open_until and r.ts.to_pydatetime() < open_until[r.date]:
            continue
        if one_per_day and r.date in fired_today:
            continue
        if count_today.get(r.date, 0) >= max_per_day:
            continue

        key = (r.date, int(r.atm_strike), side)
        series = premium_series.get(key)
        if not series:
            continue
        # entry premium: first snapshot at-or-after this bar's ts
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
            "date":          r.date,
            "entry_time":    entry_dt.strftime("%H:%M:%S"),
            "exit_time":     exit_dt.strftime("%H:%M:%S") if exit_dt else "",
            "duration_min":  round((exit_dt - entry_dt).total_seconds() / 60, 1) if exit_dt else 0,
            "side":          side,
            "strike":        int(r.atm_strike),
            "spot":          round(r.spot, 2),
            "straddle":      round(r.atm_straddle, 2),
            "threshold":     round(thr, 2),
            "entry_premium": round(entry_premium, 2),
            "exit_premium":  round(exit_premium, 2),
            "reason":        reason,
            "pnl":           pnl,
        })
        open_until[r.date] = exit_dt
        fired_today.add(r.date)
        count_today[r.date] = count_today.get(r.date, 0) + 1
    return trades


def _print_summary(trades: list, label: str, sl: float, rr: float, qty: int) -> None:
    print(f"\n{'='*72}")
    print(f"=== {label} ===")
    print(f"{'='*72}")
    if not trades:
        print("  no trades")
        return
    pnls   = [t["pnl"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total  = sum(pnls)
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    wr = len(wins) / len(pnls) * 100
    pf = (gross_w / gross_l) if gross_l > 0 else float("inf")
    expectancy = total / len(pnls)

    reasons: dict = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    for t in trades:
        reasons[t["reason"]]["count"] += 1
        reasons[t["reason"]]["pnl"]   += t["pnl"]

    print(f"  Params:     SL={sl}  RR={rr}  qty={qty}")
    print(f"  Trades:     {len(trades)}    Wins {len(wins)}  /  Losses {len(losses)}")
    print(f"  Win rate:   {wr:.1f}%")
    print(f"  Net P&L:    Rs {total:+,.0f}")
    print(f"  Gross win:  Rs {gross_w:+,.0f}    Gross loss:  Rs {-gross_l:+,.0f}")
    print(f"  Profit factor: {pf:.2f}    Expectancy: Rs {expectancy:+,.0f} / trade")
    print(f"  Avg duration: {np.mean([t['duration_min'] for t in trades]):.1f} min")
    print(f"  Exit reasons:")
    for r, v in sorted(reasons.items(), key=lambda x: -x[1]["pnl"]):
        share = v["count"] / len(trades) * 100
        print(f"    {r:10}  {v['count']:>3}  ({share:>4.1f}%)  pnl Rs {v['pnl']:+,.0f}")

    # Per-day P&L
    by_day: dict = defaultdict(float)
    for t in trades:
        by_day[t["date"]] += t["pnl"]
    print(f"\n  Per-day P&L:")
    for d, p in sorted(by_day.items()):
        print(f"    {d}   Rs {p:+,.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--side",   default="both", choices=["CE", "PE", "both"])
    ap.add_argument("--sl-dist", type=float, default=15.0)
    ap.add_argument("--rr",      type=float, default=3.0)
    ap.add_argument("--lots",    type=int, default=1)
    ap.add_argument("--n-days",  type=int, default=5,
                    help="rolling window of prior days for the Q5 threshold")
    ap.add_argument("--pct",     type=float, default=0.80,
                    help="percentile cutoff (0.80 = Q5 / top 20%%)")
    ap.add_argument("--csv",     default=None)
    ap.add_argument("--one-per-day", action="store_true",
                    help="Only fire ONE trade per day (no re-entries after exit)")
    ap.add_argument("--max-per-day", type=int, default=4,
                    help="Hard cap on trades per day per strategy (default 4)")
    args = ap.parse_args()

    qty = LOT_SIZE * args.lots

    db = mongo.get_db()
    if db is None:
        print("Mongo not configured / unreachable. Aborting.")
        sys.exit(1)

    df = _load_snapshots(db, args.symbol)
    if df.empty:
        print("No snapshots.")
        return

    print("Building per-bar ATM straddle ...", flush=True)
    bar_tbl = _atm_straddle_per_bar(df)
    print(f"  {len(bar_tbl):,} bars across {bar_tbl['date'].nunique()} days")

    thresholds = _trailing_thresholds(bar_tbl, args.n_days, args.pct)
    eligible_days = sum(1 for v in thresholds.values() if v is not None)
    print(f"  trailing-{args.n_days}-day Q{int(args.pct*100)} thresholds ready for "
          f"{eligible_days} days")

    print("Indexing premium series ...", flush=True)
    premium_series = _index_premium_series(df)

    sides = ["CE", "PE"] if args.side == "both" else [args.side]
    all_results = {}
    for s in sides:
        print(f"\nRunning backtest: side={s} ...", flush=True)
        trades = _run_backtest(bar_tbl, premium_series, thresholds, s,
                               args.sl_dist, args.rr, qty,
                               one_per_day=args.one_per_day,
                               max_per_day=args.max_per_day)
        all_results[s] = trades
        _print_summary(trades, f"side={s}", args.sl_dist, args.rr, qty)

    # Combined comparison if both sides were run
    if args.side == "both":
        print(f"\n{'='*72}")
        print("=== CE vs PE comparison ===")
        print(f"{'='*72}")
        for s in ("CE", "PE"):
            t = all_results[s]
            if not t:
                continue
            total = sum(x["pnl"] for x in t)
            wins  = sum(1 for x in t if x["pnl"] > 0)
            print(f"  {s}:  trades={len(t):>3}   wins={wins:>3}   "
                  f"P&L=Rs {total:+,.0f}   WR={wins/len(t)*100:>4.1f}%")
        net = sum(x["pnl"] for t in all_results.values() for x in t)
        print(f"  Net (both sides combined): Rs {net:+,.0f}")
        print("\n  If CE makes money and PE doesn't (or v.v.), the signal is directional.")
        print("  If both lose, edge is fake / eaten by SL.")
        print("  If both win, signal is volatility-only (would profit from straddle, not directional).")

    if args.csv:
        # Write all trades to one CSV with a `side` column
        flat = [t for trades in all_results.values() for t in trades]
        if flat:
            with open(args.csv, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(flat[0].keys()))
                w.writeheader()
                w.writerows(flat)
            print(f"\nTrade ledger written to {args.csv}")


if __name__ == "__main__":
    main()
