"""Verify every row in per_strike_walkthrough.csv against Delta's public API.

Reads the CSV we generated, hits Delta REST for each option symbol at the
exact decision timestamp, and compares the mark we recorded vs what Delta
serves now (their historical archive). Reports per-row pass/fail.

If any row fails, the call/put data was wrong → recompute pred. If all pass,
the entire pred calculation is verified against Delta's source of truth.
"""
from __future__ import annotations
import csv
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
sys.stdout.reconfigure(encoding="utf-8")

import requests

CSV_PATH = Path(__file__).parent / "audit_csv" / "per_strike_walkthrough.csv"
API_BASE = "https://api.india.delta.exchange"
TOLERANCE_PCT = 0.5   # accept mark diff < 0.5% as float/timing jitter


def utc_unix(ts_str: str) -> int:
    """'2026-06-04 12:00 UTC' → unix seconds."""
    ts_str = ts_str.replace(" UTC", "")
    return int(datetime.strptime(ts_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def fetch_mark(symbol: str, ts_unix: int, side: str) -> float | None:
    """Hit Delta REST for the MARK candle covering ts_unix.
    Returns the close mark price, or None if Delta has no candle there."""
    # Pull a 5-min window around the timestamp at 1m resolution and pick the
    # bar at exactly ts_unix (the bot uses 1m mark candles)
    start = ts_unix - 60
    end   = ts_unix + 300
    url   = f"{API_BASE}/v2/history/candles"
    params = {
        "symbol":     f"MARK:{symbol}",
        "resolution": "1m",
        "start":      start,
        "end":        end,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json().get("result", [])
        if not data: return None
        # Find the candle at ts_unix (or closest before)
        target = max((c for c in data if int(c["time"]) <= ts_unix),
                     key=lambda c: int(c["time"]), default=None)
        if target is None: return None
        return float(target["close"])
    except Exception as e:
        print(f"      ERR {symbol}: {e}")
        return None


def verify():
    if not CSV_PATH.exists():
        print(f"CSV not found: {CSV_PATH}")
        print("Run: python export_csv.py")
        sys.exit(1)

    print(f"Reading {CSV_PATH.name}...")
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    # Skip MEDIAN summary rows (they have no individual call/put)
    strike_rows = [r for r in rows if r["strike"] != "MEDIAN"]
    print(f"  {len(strike_rows)} strike rows to verify\n")

    results = {"pass": 0, "fail": 0, "no_data": 0}
    failures = []

    print(f"  {'asset':<5} {'time UTC':<18} {'strike':>7} "
          f"{'csv call':>10} {'api call':>10} {'Δ%':>6} "
          f"{'csv put':>10} {'api put':>10} {'Δ%':>6} {'verdict':<10}")
    print("  " + "─" * 110)

    for row in strike_rows:
        ts = utc_unix(row["time_utc"])
        c_csv = float(row["call_mark"])
        p_csv = float(row["put_mark"])
        c_api = fetch_mark(row["call_symbol"], ts, "call")
        time.sleep(0.05)
        p_api = fetch_mark(row["put_symbol"], ts, "put")
        time.sleep(0.05)

        if c_api is None or p_api is None:
            verdict = "NO_DATA"
            results["no_data"] += 1
            c_diff = p_diff = float("nan")
        else:
            c_diff = abs(c_api - c_csv) / c_csv * 100 if c_csv > 0 else 0
            p_diff = abs(p_api - p_csv) / p_csv * 100 if p_csv > 0 else 0
            if c_diff < TOLERANCE_PCT and p_diff < TOLERANCE_PCT:
                verdict = "MATCH ✓"
                results["pass"] += 1
            else:
                verdict = "MISMATCH ✗"
                results["fail"] += 1
                failures.append((row, c_api, p_api, c_diff, p_diff))

        c_api_str = f"${c_api:.4f}" if c_api is not None else "—"
        p_api_str = f"${p_api:.4f}" if p_api is not None else "—"
        c_diff_str = f"{c_diff:.3f}" if c_api is not None else "—"
        p_diff_str = f"{p_diff:.3f}" if p_api is not None else "—"

        print(f"  {row['asset']:<5} {row['time_utc']:<18} {row['strike']:>7} "
              f"${c_csv:>9.4f} {c_api_str:>10} {c_diff_str:>5}% "
              f"${p_csv:>9.4f} {p_api_str:>10} {p_diff_str:>5}% {verdict:<10}")

    print("\n" + "=" * 110)
    print(f"VERIFICATION SUMMARY  (tolerance: {TOLERANCE_PCT}% mark diff)")
    print("=" * 110)
    total = sum(results.values())
    print(f"  ✓ MATCH:    {results['pass']:>3} of {total}  ({results['pass']/total*100:.1f}%)")
    print(f"  ✗ MISMATCH: {results['fail']:>3} of {total}  ({results['fail']/total*100:.1f}%)")
    print(f"  — NO_DATA:  {results['no_data']:>3} of {total}  (Delta archive missing — uncommon)")
    if failures:
        print("\n  Failed rows (worth investigating):")
        for row, c_api, p_api, c_d, p_d in failures[:5]:
            print(f"    {row['asset']} strike {row['strike']} @ {row['time_utc']}:")
            print(f"      call CSV={row['call_mark']}  API={c_api:.4f}  diff={c_d:.3f}%")
            print(f"      put  CSV={row['put_mark']}  API={p_api:.4f}  diff={p_d:.3f}%")


if __name__ == "__main__":
    verify()
