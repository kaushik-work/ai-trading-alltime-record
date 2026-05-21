"""
Backtest the live signal_log against ACTUAL option premiums.

For every signal where the bot said will_trade=True, replay the trade
using real 5-min option prices from option_snapshots and compute real P&L.

This is the "does the strategy actually have edge?" test — uses the same
SL/TP / RR logic as live trading. Single position at a time per day, re-entry
allowed only after the previous trade closes.

Usage (inside the api container, on the droplet):
    docker compose exec api python scripts/backtest_signal_log.py

Optional flags:
    --sl-dist 15      SL distance in premium points (default 15)
    --rr 3.0          R:R ratio for TP (default 3.0)
    --lots 1          Number of lots (default 1)
    --csv out.csv     Write per-trade ledger to a CSV
    --since 2026-05-01  Only signals on/after this date

Output:
    Per-trade ledger (printed)
    Daily summary (printed)
    Aggregate stats (printed) — win rate, profit factor, expectancy, max DD
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import mongo  # noqa: E402

LOT_SIZE = 65          # NIFTY lot size
EOD_LIMIT = dtime(15, 20)


def _parse_dt(ts: str) -> Optional[datetime]:
    """Best-effort parse of timestamps stored as strings in Mongo."""
    if isinstance(ts, datetime):
        return ts
    if not isinstance(ts, str):
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            return datetime.strptime(ts.split("+")[0].rstrip("Z"), fmt.replace("%z", "").rstrip("%f").rstrip("."))
        except ValueError:
            continue
    # Last resort — fromisoformat
    try:
        return datetime.fromisoformat(ts.replace("Z", "").split("+")[0])
    except Exception:
        return None


def _strike_key(d: dict) -> tuple:
    return (d["date"], int(d["strike"]), d["option_type"])


def _load_snapshots_indexed(db, date_filter: dict) -> dict:
    """
    Build {(date, strike, option_type): [(dt, ltp), ...]} sorted by dt.

    Loaded into memory once so the walk-forward per-trade is O(N).
    """
    print("Loading option_snapshots ...", flush=True)
    cursor = db.option_snapshots.find(
        date_filter,
        projection={"_id": 0, "date": 1, "strike": 1, "option_type": 1,
                    "timestamp": 1, "ltp": 1},
    )
    by_key: dict = defaultdict(list)
    n = 0
    for d in cursor:
        dt = _parse_dt(d.get("timestamp"))
        ltp = d.get("ltp")
        if dt is None or ltp is None or ltp <= 0:
            continue
        key = (d["date"], int(d["strike"]), d["option_type"])
        by_key[key].append((dt, float(ltp)))
        n += 1
    for key in by_key:
        by_key[key].sort()
    print(f"  loaded {n:,} snapshots into {len(by_key):,} (date,strike,side) buckets")
    return by_key


def _walk_forward(series: list, entry_dt: datetime, entry_premium: float,
                  sl_dist: float, rr: float) -> tuple:
    """Walk subsequent snapshots until SL/TP/EOD, return (exit_dt, exit_premium, reason)."""
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
    # No subsequent snapshots — assume last known price
    if series:
        last_dt, last_ltp = series[-1]
        if last_dt > entry_dt:
            return last_dt, last_ltp, "EOD-last"
    return entry_dt, entry_premium, "no-data"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sl-dist", type=float, default=15.0,
                    help="SL distance in premium points (default 15)")
    ap.add_argument("--rr", type=float, default=3.0,
                    help="R:R ratio for TP (default 3.0)")
    ap.add_argument("--lots", type=int, default=1,
                    help="Number of lots (default 1)")
    ap.add_argument("--csv", type=str, default=None,
                    help="Write per-trade ledger to this CSV path")
    ap.add_argument("--since", type=str, default=None,
                    help="Only signals on/after this date (YYYY-MM-DD)")
    args = ap.parse_args()

    qty = LOT_SIZE * args.lots

    db = mongo.get_db()
    if db is None:
        print("Mongo not configured / unreachable. Aborting.")
        sys.exit(1)

    # ── Load all signals where the bot said will_trade=True
    sig_filter = {"will_trade": True, "direction": {"$in": ["BUY", "SELL"]}}
    if args.since:
        sig_filter["date"] = {"$gte": args.since}
    signals = list(db.signal_log.find(sig_filter, projection={"_id": 0}).sort("timestamp", 1))
    print(f"\nSignals where will_trade=True: {len(signals):,}")
    if not signals:
        print("Nothing to backtest. Either signal_log is empty or no signals crossed threshold.")
        return

    # ── Date range
    dates = sorted({s.get("date") for s in signals if s.get("date")})
    print(f"Date range: {dates[0]} → {dates[-1]}  ({len(dates)} days with signals)")

    # ── Pre-index option snapshots once (much faster than per-trade queries)
    date_filter = {"date": {"$in": dates}}
    by_key = _load_snapshots_indexed(db, date_filter)

    # ── Replay state machine
    trades = []
    # per-day state: last position exit time. New signals before this are skipped.
    last_exit_by_day: dict = {}

    for sig in signals:
        date = sig.get("date")
        sig_dt = _parse_dt(sig.get("timestamp"))
        strike = sig.get("strike")
        option_type = sig.get("option_type")
        direction = sig.get("direction")
        score = sig.get("score")
        if not date or sig_dt is None or not strike or option_type not in ("CE", "PE"):
            continue

        # Skip if a position is still "open" from an earlier signal today
        last_exit = last_exit_by_day.get(date)
        if last_exit and sig_dt < last_exit:
            continue

        key = (date, int(strike), option_type)
        series = by_key.get(key)
        if not series:
            # No snapshots for this strike that day — skip
            continue

        # Find entry premium: snapshot closest to sig_dt at or after it
        entry_premium = None
        entry_dt = None
        for dt, ltp in series:
            if dt >= sig_dt:
                entry_premium = ltp
                entry_dt = dt
                break
        if entry_premium is None:
            # Signal too late in the day — no forward snapshot
            continue

        exit_dt, exit_premium, reason = _walk_forward(
            series, entry_dt, entry_premium, args.sl_dist, args.rr
        )

        pnl = round((exit_premium - entry_premium) * qty, 2)

        trades.append({
            "date":          date,
            "entry_time":    entry_dt.strftime("%H:%M:%S"),
            "exit_time":     exit_dt.strftime("%H:%M:%S") if exit_dt else "",
            "duration_min":  round((exit_dt - entry_dt).total_seconds() / 60, 1) if exit_dt else 0,
            "direction":     direction,
            "strike":        strike,
            "option_type":   option_type,
            "score":         score,
            "entry_premium": round(entry_premium, 2),
            "exit_premium":  round(exit_premium, 2),
            "reason":        reason,
            "pnl":           pnl,
        })
        last_exit_by_day[date] = exit_dt

    # ── Output: per-trade ledger
    if not trades:
        print("\nNo replayable trades. (Signals had no matching snapshots, or all signals were skipped by duplicate guard.)")
        return

    print(f"\n=== Trades replayed: {len(trades)} ===")
    print(f"{'date':10}  {'entry':8}  {'exit':8}  {'dur':>5}  {'dir':4}  {'strike':>6}  {'side':3}  "
          f"{'score':>5}  {'entry₹':>8}  {'exit₹':>8}  {'reason':8}  {'pnl':>9}")
    print("-" * 110)
    for t in trades:
        print(f"{t['date']:10}  {t['entry_time']:8}  {t['exit_time']:8}  {t['duration_min']:>5}  "
              f"{t['direction']:4}  {t['strike']:>6}  {t['option_type']:3}  "
              f"{t['score']:>+5.0f}  ₹{t['entry_premium']:>7.2f}  ₹{t['exit_premium']:>7.2f}  "
              f"{t['reason']:8}  ₹{t['pnl']:>+8.0f}")

    # ── Daily summary
    by_day: dict = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        d = by_day[t["date"]]
        d["trades"] += 1
        d["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            d["wins"] += 1
        elif t["pnl"] < 0:
            d["losses"] += 1

    print(f"\n=== Daily summary ===")
    print(f"{'date':10}  {'trades':>6}  {'wins':>4}  {'losses':>6}  {'pnl':>11}")
    print("-" * 50)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for d, s in sorted(by_day.items()):
        cum += s["pnl"]
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
        print(f"{d:10}  {s['trades']:>6}  {s['wins']:>4}  {s['losses']:>6}  ₹{s['pnl']:>+10.0f}")

    # ── Aggregate stats
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = sum(pnls)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    win_rate = len(wins) / len(pnls) * 100 if pnls else 0
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    avg_win = (gross_win / len(wins)) if wins else 0
    avg_loss = (gross_loss / len(losses)) if losses else 0
    expectancy = total / len(pnls) if pnls else 0

    # Exit reason breakdown
    reasons: dict = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    for t in trades:
        reasons[t["reason"]]["count"] += 1
        reasons[t["reason"]]["pnl"] += t["pnl"]

    print(f"\n=== Aggregate ===")
    print(f"  Params:        SL_dist=₹{args.sl_dist}  RR={args.rr}  qty={qty}  lots={args.lots}")
    print(f"  Total trades:  {len(trades)}")
    print(f"  Wins / Losses: {len(wins)} / {len(losses)}")
    print(f"  Win rate:      {win_rate:.1f}%")
    print(f"  Gross win:     ₹{gross_win:+,.0f}")
    print(f"  Gross loss:    ₹{-gross_loss:+,.0f}")
    print(f"  Net P&L:       ₹{total:+,.0f}")
    print(f"  Profit factor: {pf:.2f}")
    print(f"  Avg win:       ₹{avg_win:+,.0f}")
    print(f"  Avg loss:      ₹{-avg_loss:+,.0f}")
    print(f"  Expectancy:    ₹{expectancy:+,.0f} per trade")
    print(f"  Max drawdown:  ₹{max_dd:+,.0f}")
    print()
    print(f"  Exit reason breakdown:")
    for r, v in sorted(reasons.items(), key=lambda x: -x[1]["pnl"]):
        share = v["count"] / len(trades) * 100
        print(f"    {r:10}  {v['count']:>3}  ({share:>4.1f}%)  pnl ₹{v['pnl']:+,.0f}")

    # ── Optional CSV
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader()
            w.writerows(trades)
        print(f"\nLedger written to {args.csv}")


if __name__ == "__main__":
    main()
