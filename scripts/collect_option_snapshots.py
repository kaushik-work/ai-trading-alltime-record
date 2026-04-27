"""
Option Chain Snapshot Collector.

Saves real option market data (LTP, bid, ask, OI, volume) every 5 minutes
during market hours. Runs continuously — start before 9:15, stop after 15:30.

This is the data collection layer that makes a real backtest possible.
Bhavcopy gives only one price per day (15:30 settlement).
This gives you every 5-minute interval — entry, SL, TP, exit, all real prices.

Output: db/oi_snapshots/YYYY-MM-DD.csv
Columns: timestamp, symbol, expiry, strike, option_type, ltp, bid, ask, volume, oi, spot

Usage:
  python scripts/collect_option_snapshots.py             # NIFTY, 5min
  python scripts/collect_option_snapshots.py --symbol BANKNIFTY
  python scripts/collect_option_snapshots.py --interval 1  # 1-minute snapshots
  python scripts/collect_option_snapshots.py --strikes 8   # ATM +/- 8 strikes

Start this BEFORE 9:15 on any trading day. It runs until 15:35 then exits.
"""

import argparse
import csv
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

p = argparse.ArgumentParser()
p.add_argument("--symbol",   default="NIFTY",      choices=["NIFTY", "BANKNIFTY"])
p.add_argument("--interval", type=int, default=5,  help="snapshot interval in minutes")
p.add_argument("--strikes",  type=int, default=6,  help="ATM +/- N strikes to capture")
args = p.parse_args()

IST       = ZoneInfo("Asia/Kolkata")
MARKET_START = datetime.now(IST).replace(hour=9, minute=10, second=0, microsecond=0)
MARKET_END   = datetime.now(IST).replace(hour=15, minute=35, second=0, microsecond=0)
OUT_DIR   = Path(__file__).parent.parent / "db" / "oi_snapshots"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SYMBOL    = args.symbol
STEP      = 50 if SYMBOL == "NIFTY" else 100   # strike step
INTERVAL  = args.interval * 60                  # seconds

print(f"\n  Option Snapshot Collector | {SYMBOL} | every {args.interval} min")
print(f"  Capturing ATM +/- {args.strikes} strikes")
print(f"  Output: {OUT_DIR}")
print(f"  Running until 15:35. Press Ctrl+C to stop early.\n")

# ── Login ─────────────────────────────────────────────────────────────────────
from data.angel_fetcher import AngelFetcher
af = AngelFetcher.get()
if not af._ensure_logged_in():
    print("  Login failed. Check .env credentials.")
    sys.exit(1)
print("  Logged in to Angel One OK")

# ── Instrument master (all NFO options) ───────────────────────────────────────
instruments = af._nfo_instruments()
if not instruments:
    print("  Could not load instrument master.")
    sys.exit(1)
print(f"  Loaded {len(instruments)} NFO instruments from master")

def _parse_expiry(s: str):
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try: return datetime.strptime(s.strip(), fmt).date()
        except: pass
    return None

def get_expiry_tokens(symbol: str, expiry: date, atm: int, n_strikes: int):
    """Return list of {token, strike, option_type} for ATM +/- n strikes."""
    tokens = []
    for k in range(-n_strikes, n_strikes + 1):
        strike = atm + k * STEP
        for ot in ("CE", "PE"):
            match = next((
                i for i in instruments
                if i.get("name") == symbol
                and int(float(i.get("strike", 0))) // 100 == strike
                and i.get("instrumenttype") == "OPTIDX"
                and i.get("symbol", "").endswith(ot)
                and _parse_expiry(i.get("expiry", "")) == expiry
            ), None)
            if match:
                tokens.append({
                    "token": match["token"],
                    "tradingsymbol": match["symbol"],
                    "strike": strike,
                    "option_type": ot,
                })
    return tokens

def get_spot(symbol: str) -> float:
    ltp = af.get_index_ltp(symbol)
    return ltp if ltp else 0.0

def fetch_quotes(token_list: list) -> dict:
    """
    Call getMarketData FULL for a list of tokens.
    Returns {token: {ltp, bid, ask, volume, oi}} or empty on failure.
    """
    if not token_list:
        return {}
    try:
        exchange_tokens = {"NFO": [t["token"] for t in token_list]}
        resp = af._api.getMarketData("FULL", exchange_tokens)
        if not resp or not resp.get("status"):
            return {}
        data = resp.get("data", {}).get("fetched", [])
        result = {}
        for row in data:
            tok = str(row.get("symbolToken", ""))
            result[tok] = {
                "ltp":    float(row.get("ltp", 0) or 0),
                "bid":    float(row.get("bidPrice", 0) or 0),
                "ask":    float(row.get("askPrice", 0) or 0),
                "volume": int(row.get("tradeVolume", 0) or 0),
                "oi":     int(row.get("opnInterest", 0) or 0),
            }
        return result
    except Exception as e:
        print(f"    getMarketData error: {e}")
        return {}

# ── Main loop ─────────────────────────────────────────────────────────────────
today     = date.today()
out_file  = OUT_DIR / f"{today}.csv"
write_hdr = not out_file.exists()

expiry    = None
token_map = []           # rebuilt when ATM changes by >STEP
last_atm  = None

def nearest_expiry(symbol: str, from_date: date) -> date:
    expiries = sorted({
        _parse_expiry(i["expiry"])
        for i in instruments
        if i.get("name") == symbol
        and i.get("expiry")
        and _parse_expiry(i["expiry"]) is not None
        and _parse_expiry(i["expiry"]) >= from_date
    })
    return expiries[0] if expiries else from_date + timedelta(days=7)

print(f"  Writing to {out_file}")
print(f"  {'Time':<10}  {'Spot':>8}  {'Expiry':<12}  {'Strikes captured':>18}  {'Rows'}")
print(f"  {'-'*65}")

with open(out_file, "a", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    if write_hdr:
        writer.writerow([
            "timestamp", "symbol", "expiry", "strike", "option_type",
            "ltp", "bid", "ask", "volume", "oi", "spot"
        ])

    while True:
        now = datetime.now(IST)

        # Stop after market close
        if now > MARKET_END:
            print(f"\n  15:35 reached — collection complete for {today}.")
            break

        # Wait until market opens
        if now < MARKET_START:
            wait = (MARKET_START - now).seconds
            print(f"  Market opens in {wait//60}m {wait%60}s — waiting...")
            time.sleep(min(wait, 60))
            continue

        # Get spot and refresh token list if ATM has moved
        spot = get_spot(SYMBOL)
        if spot <= 0:
            print(f"  {now.strftime('%H:%M:%S')}  spot fetch failed, retrying in 30s")
            time.sleep(30)
            continue

        atm = int(round(spot / STEP)) * STEP

        if expiry is None:
            expiry = nearest_expiry(SYMBOL, today)
            print(f"  Nearest expiry: {expiry}")

        if last_atm is None or abs(atm - last_atm) >= STEP:
            token_map = get_expiry_tokens(SYMBOL, expiry, atm, args.strikes)
            last_atm  = atm
            if not token_map:
                print(f"  No tokens found for {SYMBOL} expiry={expiry} ATM={atm}")
                time.sleep(30)
                continue

        # Fetch quotes for all strikes
        quotes = fetch_quotes(token_map)
        ts     = now.strftime("%Y-%m-%d %H:%M:%S")
        rows_written = 0

        for t in token_map:
            q = quotes.get(str(t["token"]), {})
            if not q or q.get("ltp", 0) <= 0:
                continue
            writer.writerow([
                ts, SYMBOL, expiry,
                t["strike"], t["option_type"],
                q["ltp"], q["bid"], q["ask"],
                q["volume"], q["oi"], round(spot, 2)
            ])
            rows_written += 1

        f.flush()
        print(f"  {now.strftime('%H:%M:%S')}  spot={spot:.0f}  expiry={expiry}"
              f"  strikes=ATM{atm}+/-{args.strikes}  rows={rows_written}")

        # Sleep until next interval
        next_tick = now + timedelta(seconds=INTERVAL)
        sleep_sec = (next_tick - datetime.now(IST)).total_seconds()
        if sleep_sec > 0:
            time.sleep(sleep_sec)

print(f"\n  Saved to {out_file}")
print(f"  To use in backtest: load with pd.read_csv('{out_file}')")
print(f"  Columns: timestamp, symbol, expiry, strike, option_type, ltp, bid, ask, volume, oi, spot\n")
