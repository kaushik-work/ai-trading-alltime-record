"""
Build historical option chain OHLCV database from bhavcopy + Angel One.

Produces: db/option_chain_history.csv
Columns: date, expiry, strike, option_type, open, high, low, close, oi, oi_chng, volume, spot

Two data sources combined:
  1. NSE bhavcopy (db/bhavcopy_cache/) — has EOD OI per strike, volume, prices
  2. Angel One _candle_data() — used only if bhavcopy doesn't have what we need

This file is the foundation for:
  - IV rank computation (compute IV from close price + spot + time)
  - CE/PE wall identification per day
  - OI Wall Capture strategy backtest

Usage:
  python scripts/fetch_option_chain_history.py               # builds from cached bhavcopy
  python scripts/fetch_option_chain_history.py --days 7      # also fetch recent from Angel One
"""
import argparse
import io
import os
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

OUT_FILE      = Path(__file__).parent.parent / "db" / "option_chain_history.csv"
BHAV_CACHE    = Path(__file__).parent.parent / "db" / "bhavcopy_cache"
HEADERS       = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
URL_TMPL      = "https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{dt}_F_0000.csv.zip"

parser = argparse.ArgumentParser()
parser.add_argument("--days",  type=int, default=0,
                    help="Also fetch most recent N days directly from Angel One")
parser.add_argument("--symbol", default="NIFTY")
args = parser.parse_args()

SYMBOL = args.symbol


def _bhavcopy_to_chain(df: pd.DataFrame, d: date, symbol: str) -> pd.DataFrame:
    """Extract per-strike rows for one symbol from a bhavcopy DataFrame."""
    nifty = df[df["TckrSymb"] == symbol].copy()
    if nifty.empty:
        return pd.DataFrame()

    # Find nearest weekly expiry (smallest future expiry date)
    nifty["XpryDt"] = pd.to_datetime(nifty["XpryDt"])
    future = nifty[nifty["XpryDt"].dt.date >= d]
    if future.empty:
        return pd.DataFrame()

    nearest_expiry = future["XpryDt"].min()
    weekly = future[future["XpryDt"] == nearest_expiry].copy()

    weekly["date"]        = str(d)
    weekly["expiry"]      = nearest_expiry.date().isoformat()
    weekly["strike"]      = pd.to_numeric(weekly["StrkPric"], errors="coerce").fillna(0).astype(int)
    weekly["option_type"] = weekly["OptnTp"].str.strip()
    weekly["open"]        = pd.to_numeric(weekly["OpnPric"],     errors="coerce").fillna(0)
    weekly["high"]        = pd.to_numeric(weekly["HghPric"],     errors="coerce").fillna(0)
    weekly["low"]         = pd.to_numeric(weekly["LwPric"],      errors="coerce").fillna(0)
    weekly["close"]       = pd.to_numeric(weekly["ClsPric"],     errors="coerce").fillna(0)
    weekly["settle"]      = pd.to_numeric(weekly["SttlmPric"],   errors="coerce").fillna(0)
    weekly["oi"]          = pd.to_numeric(weekly["OpnIntrst"],   errors="coerce").fillna(0).astype(int)
    weekly["oi_chng"]     = pd.to_numeric(weekly["ChngInOpnIntrst"], errors="coerce").fillna(0).astype(int)
    weekly["volume"]      = pd.to_numeric(weekly["TtlTradgVol"], errors="coerce").fillna(0).astype(int)
    weekly["spot"]        = pd.to_numeric(weekly["UndrlygPric"], errors="coerce").fillna(0)

    return weekly[["date","expiry","strike","option_type","open","high","low","close","settle","oi","oi_chng","volume","spot"]]


def load_from_bhavcopy(symbol: str) -> pd.DataFrame:
    """Parse all cached bhavcopy files and build the option chain history."""
    BHAV_CACHE.mkdir(parents=True, exist_ok=True)
    rows = []
    for cache_file in sorted(BHAV_CACHE.glob("*.csv")):
        try:
            d = date(int(cache_file.stem[:4]), int(cache_file.stem[4:6]), int(cache_file.stem[6:8]))
            df = pd.read_csv(cache_file)
            chain = _bhavcopy_to_chain(df, d, symbol)
            if not chain.empty:
                rows.append(chain)
        except Exception as e:
            print(f"  WARN {cache_file.name}: {e}")
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def main():
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    print(f"Building {SYMBOL} option chain history from bhavcopy cache...")
    chain = load_from_bhavcopy(SYMBOL)

    if chain.empty:
        print("  No bhavcopy data found. Run fetch_historical_pcr.py first to populate cache.")
        return

    # Sort and deduplicate
    chain = chain.sort_values(["date","strike","option_type"]).drop_duplicates(["date","strike","option_type"])
    chain.to_csv(OUT_FILE, index=False)

    dates = chain["date"].nunique()
    strikes = chain["strike"].nunique()
    print(f"  Saved {len(chain):,} rows | {dates} trading days | {strikes} unique strikes")
    print(f"  Date range: {chain['date'].min()} to {chain['date'].max()}")
    print(f"  Output: {OUT_FILE}")

    # Show CE/PE walls per day (preview)
    print("\n  CE/PE wall preview (last 5 days):")
    for d in sorted(chain["date"].unique())[-5:]:
        day = chain[chain["date"] == d]
        spot = float(day["spot"].median())
        ce = day[day["option_type"] == "CE"]
        pe = day[day["option_type"] == "PE"]
        ce_wall = int(ce.loc[ce["oi"].idxmax(), "strike"]) if not ce.empty else 0
        pe_wall = int(pe.loc[pe["oi"].idxmax(), "strike"]) if not pe.empty else 0
        pcr = pe["oi"].sum() / ce["oi"].sum() if ce["oi"].sum() > 0 else 1.0
        print(f"    {d}  spot={spot:.0f}  CE_wall={ce_wall}  PE_wall={pe_wall}  PCR={pcr:.2f}")


if __name__ == "__main__":
    main()
