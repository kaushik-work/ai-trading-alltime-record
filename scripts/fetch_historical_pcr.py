"""
Download NSE F&O Bhavcopy for past N months and build a daily PCR dataset.

Usage:
  python scripts/fetch_historical_pcr.py            # last 6 months
  python scripts/fetch_historical_pcr.py --months 3
  python scripts/fetch_historical_pcr.py --from 2025-10-01 --to 2026-04-23

Output: db/pcr_historical.csv
Columns: date, pcr, ce_oi, pe_oi, ce_oi_weekly, pe_oi_weekly, pcr_weekly, spot, total_strikes
"""

import argparse
import io
import os
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

OUT_FILE   = Path(__file__).parent.parent / "db" / "pcr_historical.csv"
CACHE_DIR  = Path(__file__).parent.parent / "db" / "bhavcopy_cache"
HEADERS    = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
URL_TMPL   = "https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{dt}_F_0000.csv.zip"


def _fetch_bhavcopy(d: date) -> pd.DataFrame | None:
    """Download and parse F&O bhavcopy for a single date. Caches the raw CSV."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{d.strftime('%Y%m%d')}.csv"

    if cache.exists():
        return pd.read_csv(cache)

    url = URL_TMPL.format(dt=d.strftime("%Y%m%d"))
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 404:
            return None   # holiday / weekend
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            df = pd.read_csv(z.open(z.namelist()[0]))
        df.to_csv(cache, index=False)
        return df
    except Exception as e:
        print(f"  WARN {d}: {e}")
        return None


def _compute_pcr(df: pd.DataFrame, d: date) -> dict | None:
    """Extract NIFTY CE/PE OI and compute PCR from one day's bhavcopy."""
    nifty = df[df["TckrSymb"] == "NIFTY"].copy()
    if nifty.empty:
        return None

    nifty["XpryDt"] = pd.to_datetime(nifty["XpryDt"])
    nifty["OpnIntrst"] = pd.to_numeric(nifty["OpnIntrst"], errors="coerce").fillna(0)

    # ── All expiries (full chain PCR) ─────────────────────────────────────────
    ce_all = nifty[nifty["OptnTp"] == "CE"]["OpnIntrst"].sum()
    pe_all = nifty[nifty["OptnTp"] == "PE"]["OpnIntrst"].sum()
    pcr_all = round(pe_all / ce_all, 4) if ce_all > 0 else 1.0

    # ── Nearest weekly expiry (what the bot trades) ───────────────────────────
    future_expiries = nifty[nifty["XpryDt"].dt.date >= d]["XpryDt"].unique()
    nearest_expiry  = min(future_expiries) if len(future_expiries) else None

    pcr_weekly = pcr_all
    ce_weekly = pe_weekly = 0
    if nearest_expiry is not None:
        weekly = nifty[nifty["XpryDt"] == nearest_expiry]
        ce_weekly = int(weekly[weekly["OptnTp"] == "CE"]["OpnIntrst"].sum())
        pe_weekly = int(weekly[weekly["OptnTp"] == "PE"]["OpnIntrst"].sum())
        pcr_weekly = round(pe_weekly / ce_weekly, 4) if ce_weekly > 0 else 1.0

    # Spot price (UndrlygPric column)
    spot = 0.0
    if "UndrlygPric" in nifty.columns:
        s = pd.to_numeric(nifty["UndrlygPric"], errors="coerce").dropna()
        spot = round(float(s.median()), 2) if not s.empty else 0.0

    return {
        "date":         str(d),
        "pcr":          pcr_all,
        "ce_oi":        int(ce_all),
        "pe_oi":        int(pe_all),
        "pcr_weekly":   pcr_weekly,
        "ce_oi_weekly": ce_weekly,
        "pe_oi_weekly": pe_weekly,
        "spot":         spot,
        "total_strikes": int(nifty["StrkPric"].nunique()),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=6, help="Months back from today (default 6)")
    parser.add_argument("--from",   dest="date_from", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--to",     dest="date_to",   default=None, help="End date YYYY-MM-DD (default today)")
    args = parser.parse_args()

    today    = date.today()
    date_to  = datetime.strptime(args.date_to,   "%Y-%m-%d").date() if args.date_to   else today
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else (today - timedelta(days=args.months * 31))

    # Load existing rows so we can skip already-fetched dates
    existing = set()
    if OUT_FILE.exists():
        existing = set(pd.read_csv(OUT_FILE)["date"].tolist())

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    rows = []

    d = date_from
    total_days = (date_to - date_from).days + 1
    fetched = skipped = errors = 0

    print(f"Fetching {date_from} to {date_to} ({total_days} calendar days)")
    print(f"Output: {OUT_FILE}\n")

    while d <= date_to:
        if d.weekday() >= 5:      # skip weekends
            d += timedelta(days=1)
            continue

        if str(d) in existing:
            d += timedelta(days=1)
            skipped += 1
            continue

        print(f"  {d} ... ", end="", flush=True)
        bhavcopy = _fetch_bhavcopy(d)

        if bhavcopy is None:
            print("holiday/no data")
            errors += 1
        else:
            row = _compute_pcr(bhavcopy, d)
            if row:
                rows.append(row)
                print(f"PCR={row['pcr']:.3f} weekly={row['pcr_weekly']:.3f} spot={row['spot']:.0f} strikes={row['total_strikes']}")
                fetched += 1
            else:
                print("parse error")
                errors += 1

        time.sleep(0.3)   # polite rate limit
        d += timedelta(days=1)

    # Append new rows to CSV
    if rows:
        new_df = pd.DataFrame(rows)
        if OUT_FILE.exists():
            old_df  = pd.read_csv(OUT_FILE)
            combined = pd.concat([old_df, new_df]).drop_duplicates("date").sort_values("date")
        else:
            combined = new_df
        combined.to_csv(OUT_FILE, index=False)
        print(f"\nSaved {len(combined)} total rows to {OUT_FILE}")
    else:
        print("\nNo new rows to save.")

    print(f"Done — fetched={fetched} skipped(existing)={skipped} errors/holidays={errors}")


if __name__ == "__main__":
    main()
