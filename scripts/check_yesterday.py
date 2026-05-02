"""
Daily snapshot health check — eyeball this each evening.

Shows what the collector captured today (or any past date):
  - Snapshots taken vs expected
  - Row count
  - Time range covered
  - Any gaps > 10 minutes

  python scripts/check_yesterday.py              # today
  python scripts/check_yesterday.py --date 2026-04-29
  python scripts/check_yesterday.py --all        # full summary table
"""
import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

BASE     = Path(__file__).parent.parent
SNAP_DIR = BASE / "db" / "oi_snapshots"
SUMMARY  = BASE / "db" / "collector_summary.csv"

p = argparse.ArgumentParser()
p.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
p.add_argument("--all",  action="store_true", help="show full summary table")
args = p.parse_args()

SEP = "=" * 58

# ── Full summary table ────────────────────────────────────────────────────────
if args.all:
    if not SUMMARY.exists():
        print("No collector_summary.csv yet. Run the collector first.")
        sys.exit(0)
    df = pd.read_csv(SUMMARY)
    print(f"\n{SEP}")
    print(f"  COLLECTOR SUMMARY — all days")
    print(f"{SEP}")
    print(f"  {'Date':<12} {'Symbol':<12} {'Status':<10} {'Snaps':>6} {'Rows':>8} {'Errors':>7} {'Relogins':>9}")
    print(f"  {'-'*56}")
    for _, row in df.iterrows():
        status_icon = "OK" if row["status"] == "OK" else ("!!" if row["status"] == "PARTIAL" else "XX")
        print(f"  {row['date']:<12} {row['symbol']:<12} {status_icon} {row['status']:<8} "
              f"{int(row['snapshots']):>6} {int(row['rows_written']):>8} "
              f"{int(row['errors']):>7} {int(row['relogins']):>9}")
    print(f"{SEP}\n")
    sys.exit(0)

# ── Single day check ──────────────────────────────────────────────────────────
check_date = args.date or date.today().isoformat()

print(f"\n{SEP}")
print(f"  SNAPSHOT CHECK — {check_date}")
print(f"{SEP}")

for symbol in ["NIFTY", "BANKNIFTY"]:
    snap_file = SNAP_DIR / f"{check_date}_{symbol}.csv"
    print(f"\n  {symbol}")

    if not snap_file.exists():
        print(f"    NO FILE — {snap_file.name} not found")
        print(f"    Collector did not run for {symbol} on {check_date}")
        continue

    try:
        df = pd.read_csv(snap_file, parse_dates=["timestamp"])
    except Exception as e:
        print(f"    ERROR reading file: {e}")
        continue

    if df.empty:
        print(f"    EMPTY FILE — 0 rows")
        continue

    total_rows     = len(df)
    snapshots      = df.groupby("timestamp").ngroups
    first_snap     = df["timestamp"].min()
    last_snap      = df["timestamp"].max()
    expected_snaps = 75  # 9:15 to 15:30 at 5min intervals

    print(f"    Rows        : {total_rows:,}")
    print(f"    Snapshots   : {snapshots}  (expected ~{expected_snaps})")
    print(f"    First snap  : {first_snap.strftime('%H:%M:%S')}")
    print(f"    Last snap   : {last_snap.strftime('%H:%M:%S')}")

    # Gap detection — find gaps > 10 min between snapshots
    times = sorted(df["timestamp"].unique())
    gaps  = []
    for i in range(1, len(times)):
        diff = (times[i] - times[i-1]).total_seconds() / 60
        if diff > 10:
            gaps.append((times[i-1], times[i], diff))

    if gaps:
        print(f"    Gaps > 10min: {len(gaps)}")
        for g_start, g_end, g_min in gaps[:5]:
            print(f"      {g_start.strftime('%H:%M')} -> {g_end.strftime('%H:%M')} "
                  f"({g_min:.0f} min gap)")
    else:
        print(f"    Gaps        : none")

    # Coverage rating
    if snapshots >= 65:
        rating = "GOOD"
    elif snapshots >= 40:
        rating = "PARTIAL"
    else:
        rating = "BAD — likely token failure"

    print(f"    Rating      : {rating}")

# ── Summary file row ──────────────────────────────────────────────────────────
if SUMMARY.exists():
    df_s = pd.read_csv(SUMMARY)
    day_rows = df_s[df_s["date"] == check_date]
    if not day_rows.empty:
        print(f"\n  Collector summary for {check_date}:")
        for _, row in day_rows.iterrows():
            print(f"    {row['symbol']}: status={row['status']}  "
                  f"snaps={int(row['snapshots'])}  errors={int(row['errors'])}  "
                  f"relogins={int(row['relogins'])}")

print(f"\n{SEP}\n")
