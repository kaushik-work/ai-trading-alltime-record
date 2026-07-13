"""
Generic option parity fetcher for any Delta India underlying.

For each expiry in the window, finds the ATM call+put pair and downloads 1m
mark history from (expiry - 6 days) to expiry. Also fetches the underlying
perp 1m history if missing.

Usage:
  UNDERLYING=BTC .venv/Scripts/python fetch_options_for_parity.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import hashlib
import hmac
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests

UNDERLYING = os.environ.get("UNDERLYING", "ETH").upper()
PERP_SYMBOL = f"{UNDERLYING}USD"

DATA = Path(__file__).parent / "data" / UNDERLYING.lower()
OUT_DIR = DATA / "options"
OUT_DIR.mkdir(parents=True, exist_ok=True)
PERP_DIR = DATA / "perp"
PERP_DIR.mkdir(parents=True, exist_ok=True)

API_BASE = os.environ.get("DELTA_BASE_URL", "https://api.india.delta.exchange")
API_KEY = os.environ.get("DELTA_API_KEY", "").strip()
API_SECRET = os.environ.get("DELTA_API_SECRET", "").strip()

START_DT = datetime.fromisoformat(os.environ.get("START_DT", "2026-04-01")).replace(tzinfo=timezone.utc)
END_DT   = datetime.fromisoformat(os.environ.get("END_DT",   "2026-07-07")).replace(tzinfo=timezone.utc)


def sign(method: str, path: str, query: str, body: str, ts: str) -> dict:
    msg = method + ts + path + query + body
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {"api-key": API_KEY, "signature": sig, "timestamp": ts, "User-Agent": "delta-fetch-python"}


def get(path: str, params: dict) -> dict:
    url = f"{API_BASE}{path}"
    for attempt in range(5):
        headers = {}
        if API_KEY and API_SECRET:
            ts = str(int(time.time()))
            query = ("?" + requests.compat.urlencode(params)) if params else ""
            headers = sign("GET", path, query, "", ts)
        try:
            r = requests.get(url, params=params, timeout=30, headers=headers)
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
            wait = 2 ** attempt
            print(f"  SSL/connection error ({e}), retry in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code == 429:
            wait = int(r.headers.get("X-RATE-LIMIT-RESET", 5))
            print(f"  rate-limit, sleep {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def fetch_candles(symbol: str, resolution: str, start_ts: int, end_ts: int) -> pd.DataFrame:
    limit = 2000
    step = {"m": 60, "h": 3600, "d": 86400}[resolution[-1]] * limit
    out = []
    cur_end = end_ts
    while cur_end > start_ts:
        cur_start = max(start_ts, cur_end - step)
        data = get("/v2/history/candles", {
            "symbol": symbol, "resolution": resolution,
            "start": cur_start, "end": cur_end,
        })
        rows = data.get("result", [])
        if not rows:
            break
        out.extend(rows)
        oldest = min(r["time"] for r in rows)
        cur_end = oldest - 1 if oldest <= cur_start else cur_start
        time.sleep(0.05)
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out).drop_duplicates("time").sort_values("time")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df


def fetch_expired_products(underlying: str):
    rows_all = []
    after = None
    while True:
        params = {
            "contract_types": "call_options,put_options",
            "underlying_asset_symbols": underlying,
            "states": "expired",
        }
        if after:
            params["after"] = after
        data = get("/v2/products", params)
        rows = data.get("result", [])
        rows_all.extend(rows)
        after = data.get("meta", {}).get("after")
        if not after or not rows:
            break
        time.sleep(0.05)
    df = pd.DataFrame(rows_all)
    if df.empty:
        return df
    df = df[df["symbol"].str.startswith(("C-", "P-"))].copy()
    df["strike_price"] = pd.to_numeric(df["strike_price"], errors="coerce")
    df["settlement_dt"] = pd.to_datetime(df["settlement_time"], utc=True, errors="coerce")
    return df


def fetch_perp() -> pd.DataFrame:
    perp_path = PERP_DIR / f"{PERP_SYMBOL}_mark_1m.csv"
    if perp_path.exists():
        print(f"Perp cached: {perp_path}")
        return pd.read_csv(perp_path, parse_dates=["timestamp"])
    print(f"Fetching {PERP_SYMBOL} 1m mark history...")
    start_ts = int(START_DT.timestamp())
    end_ts = int(END_DT.timestamp())
    df = fetch_candles(f"MARK:{PERP_SYMBOL}", "1m", start_ts, end_ts)
    if df.empty:
        raise RuntimeError("No perp data returned")
    df.to_csv(perp_path, index=False)
    print(f"  saved {perp_path} ({len(df)} rows)")
    return df


def parse_symbol(sym: str):
    parts = sym.split("-")
    side, asset, strike, ddmmyy = parts[0], parts[1], int(parts[2]), parts[3]
    expiry = pd.Timestamp(f"20{ddmmyy[4:6]}-{ddmmyy[2:4]}-{ddmmyy[0:2]} 12:00:00", tz="UTC")
    return side, strike, expiry


def main():
    print(f"Fetching {UNDERLYING} option ATM pairs: {START_DT.date()} → {END_DT.date()}")
    perp = fetch_perp()
    perp_ts = perp.set_index("timestamp")["close"].sort_index()

    chain = fetch_expired_products(UNDERLYING)
    if chain.empty:
        print("No expired option chain found.")
        return

    chain = chain[
        (chain["settlement_dt"] >= START_DT) &
        (chain["settlement_dt"] <= END_DT)
    ].copy()
    print(f"  expired contracts in window: {len(chain)}")

    atm_pairs = []
    for exp, grp in chain.groupby("settlement_dt"):
        spot = perp_ts.reindex([exp], method="nearest").iloc[0]
        if pd.isna(spot):
            continue
        strikes = grp["strike_price"].dropna().unique()
        if len(strikes) == 0:
            continue
        atm = min(strikes, key=lambda k: abs(k - spot))
        calls = grp[(grp["contract_type"] == "call_options") & (grp["strike_price"] == atm)]
        puts = grp[(grp["contract_type"] == "put_options") & (grp["strike_price"] == atm)]
        if not calls.empty and not puts.empty:
            atm_pairs.append({
                "expiry": exp,
                "strike": atm,
                "call_sym": calls.iloc[0]["symbol"],
                "put_sym": puts.iloc[0]["symbol"],
                "spot": spot,
            })

    print(f"Found {len(atm_pairs)} ATM expiry pairs.")
    if not atm_pairs:
        return

    for pair in atm_pairs:
        exp = pair["expiry"]
        print(f"  {exp.date()} K={pair['strike']}  call={pair['call_sym']} put={pair['put_sym']}")

    print("\nDownloading 1m mark history (expiry - 6d → expiry)...")
    for pair in atm_pairs:
        exp = pair["expiry"]
        end_ts = int(exp.timestamp())
        start_ts = int((exp - timedelta(days=6)).timestamp())
        for side, sym in [("C", pair["call_sym"]), ("P", pair["put_sym"])]:
            out_path = OUT_DIR / f"{sym}_mark_1m.csv"
            if out_path.exists():
                print(f"    cached {out_path.name}")
                continue
            df = fetch_candles(f"MARK:{sym}", "1m", start_ts, end_ts)
            if df.empty:
                print(f"    empty {sym}")
                continue
            df.to_csv(out_path, index=False)
            print(f"    saved {out_path.name} ({len(df)} rows)")

    print("\nDone.")


if __name__ == "__main__":
    main()
