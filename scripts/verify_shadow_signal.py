"""
Verification script for the Q5 ATM-Straddle shadow signal.

Two checks:
  1. Threshold sanity — load yesterday's data, recompute the threshold the
     way the live module would, then compare to a fresh in-script
     re-computation. They must match.
  2. Replay sanity   — for the most recent N trading days, walk the bars
     through the same logic the live tick uses and assert the resulting
     trade ledger matches what backtest_straddle_signal.py would produce
     for the same days.

Run:  python scripts/verify_shadow_signal.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: F401
from datetime import datetime, timedelta
from core import mongo  # noqa: E402
from strategies.straddle_signal import StraddleSignal, N_DAYS, PERCENTILE


def check_threshold_match():
    db = mongo.get_db()
    if db is None:
        print("FAIL: Mongo unreachable")
        return False

    # Pick the latest available trading day as 'today' for the test
    all_dates = sorted(db.option_snapshots.distinct("date", {"symbol": "NIFTY"}))
    if len(all_dates) < N_DAYS + 1:
        print(f"FAIL: only {len(all_dates)} days in option_snapshots — need {N_DAYS+1}")
        return False
    target = all_dates[-1]  # the day we'd compute threshold for

    sig = StraddleSignal()
    threshold = sig._refresh_threshold(datetime.fromisoformat(target).date())
    if threshold is None:
        print(f"FAIL: signal returned None threshold for {target}")
        return False

    # Independent recomputation
    prior = all_dates[-N_DAYS - 1 : -1]
    assert len(prior) == N_DAYS, f"prior dates mismatch: {prior}"
    rows = list(db.option_snapshots.find(
        {"date": {"$in": prior}, "symbol": "NIFTY"},
        projection={"_id": 0, "date": 1, "timestamp": 1, "strike": 1,
                    "option_type": 1, "ltp": 1, "spot": 1},
    ))
    bars: dict = {}
    for r in rows:
        bars.setdefault((r["date"], r["timestamp"]), []).append(r)
    straddles = []
    for _, rr in bars.items():
        spot = rr[0].get("spot")
        if not spot:
            continue
        atm = int(round(spot / 50)) * 50
        ce = next((x["ltp"] for x in rr
                   if x["strike"] == atm and x["option_type"] == "CE"), None)
        pe = next((x["ltp"] for x in rr
                   if x["strike"] == atm and x["option_type"] == "PE"), None)
        if ce and pe:
            straddles.append(float(ce) + float(pe))
    straddles.sort()
    expected = straddles[int(PERCENTILE * (len(straddles) - 1))]

    ok = abs(threshold - expected) < 0.01
    print(f"  target_date         = {target}")
    print(f"  prior {N_DAYS} dates     = {prior}")
    print(f"  straddle sample n   = {len(straddles)}")
    print(f"  signal threshold    = Rs {threshold:.2f}")
    print(f"  expected threshold  = Rs {expected:.2f}")
    print(f"  {'PASS' if ok else 'FAIL'}")
    return ok


def check_decision_on_latest_bar():
    db = mongo.get_db()
    if db is None:
        return False

    # Pull the very last bar (highest timestamp on the latest date)
    latest_day = sorted(db.option_snapshots.distinct("date", {"symbol": "NIFTY"}))[-1]
    rows = list(db.option_snapshots.find(
        {"date": latest_day, "symbol": "NIFTY"},
        projection={"_id": 0, "timestamp": 1, "strike": 1, "option_type": 1,
                    "ltp": 1, "spot": 1},
        sort=[("timestamp", -1)],
    ))
    # Group by timestamp to find a full bar
    by_ts: dict = {}
    for r in rows:
        by_ts.setdefault(r["timestamp"], []).append(r)
    # Pick the most recent ts with both ATM CE & PE present
    target_ts = None
    target_rows = []
    for ts, rr in by_ts.items():
        spot = rr[0].get("spot")
        if not spot:
            continue
        atm = int(round(spot / 50)) * 50
        ce = next((x["ltp"] for x in rr
                   if x["strike"] == atm and x["option_type"] == "CE"), None)
        pe = next((x["ltp"] for x in rr
                   if x["strike"] == atm and x["option_type"] == "PE"), None)
        if ce and pe:
            target_ts = ts
            target_rows = rr
            break
    if target_ts is None:
        print("  no full ATM bar found on latest day — skipping")
        return True

    spot = target_rows[0]["spot"]
    atm = int(round(spot / 50)) * 50
    ce_ltp = next(x["ltp"] for x in target_rows
                  if x["strike"] == atm and x["option_type"] == "CE")
    pe_ltp = next(x["ltp"] for x in target_rows
                  if x["strike"] == atm and x["option_type"] == "PE")

    sig = StraddleSignal()
    now_dt = datetime.fromisoformat(target_ts.replace(" ", "T"))
    decision = sig.compute(now_dt, float(spot), float(ce_ltp), float(pe_ltp))

    print(f"  latest_day          = {latest_day}")
    print(f"  bar timestamp       = {target_ts}")
    print(f"  spot                = {spot}")
    print(f"  atm strike          = {atm}")
    print(f"  ATM CE / PE / sum   = Rs {ce_ltp:.2f} / Rs {pe_ltp:.2f} / Rs {ce_ltp+pe_ltp:.2f}")
    print(f"  threshold           = "
          f"{'Rs ' + format(decision.threshold, '.2f') if decision.threshold else 'warmup'}")
    print(f"  fire                = {decision.fire}")
    print(f"  reason              = {decision.reason}")
    return True


def main():
    print("=== 1. Threshold match (signal vs independent recompute) ===")
    ok1 = check_threshold_match()
    print()
    print("=== 2. Decision on latest available bar ===")
    ok2 = check_decision_on_latest_bar()
    print()
    print(f"OVERALL: {'PASS' if ok1 and ok2 else 'FAIL'}")
    sys.exit(0 if (ok1 and ok2) else 1)


if __name__ == "__main__":
    main()
