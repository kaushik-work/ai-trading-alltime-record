"""
Lightweight ETH option fetcher for parity backtests.

For each expiry in the window, finds the ATM call+put pair and downloads 1h
mark history. This avoids pulling every strike/minute and keeps the dataset
small enough to backtest quickly.

Output layout:
  data/eth/options/C-ETH-<STRIKE>-<DDMMYY>_mark_1h.csv
  data/eth/options/P-ETH-<STRIKE>-<DDMMYY>_mark_1h.csv
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import hashlib
import hmac
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

DATA = Path(__file__).parent / "data" / "eth"
OUT_DIR = DATA / "options"
OUT_DIR.mkdir(parents=True, exist_ok=True)

API_BASE = os.environ.get("DELTA_BASE_URL", "https://api.india.delta.exchange")
API_KEY = os.environ.get("DELTA_API_KEY", "").strip()
API_SECRET = os.environ.get("DELTA_API_SECRET", "").strip()

START_DT = datetime.fromisoformat(os.environ.get("START_DT", "2026-04-01")).replace(tzinfo=timezone.utc)
END_DT   = datetime.fromisoformat(os.environ.get("END_DT",   "2026-07-07")).replace(tzinfo=timezone.utc)
start_ts = int(START_DT.timestamp())
end_ts   = int(END_DT.timestamp())


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
        r = requests.get(url, params=params, timeout=30, headers=headers)
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
    """All expired options from /v2/products."""
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


def parse_symbol(sym: str):
    parts = sym.split("-")
    side, asset, strike, ddmmyy = parts[0], parts[1], int(parts[2]), parts[3]
    expiry = pd.Timestamp(f"20{ddmmyy[4:6]}-{ddmmyy[2:4]}-{ddmmyy[0:2]} 12:00:00", tz="UTC")
    return side, strike, expiry


def main():
    print(f"Fetching ETH option ATM pairs: {START_DT.date()} → {END_DT.date()}")
    chain = fetch_expired_products("ETH")
    if chain.empty:
        print("No expired option chain found.")
        return

    # Keep only expiries in window
    chain = chain[
        (chain["settlement_dt"] >= START_DT) &
        (chain["settlement_dt"] <= END_DT)
    ].copy()
    print(f"  expired contracts in window: {len(chain)}")

    # Load perp to determine spot at settlement
    perp_path = DATA / "perp" / "ETHUSD_mark_1m.csv"
    if not perp_path.exists():
        print(f"Perp data not found at {perp_path}")
        return
    perp = pd.read_csv(perp_path, parse_dates=["timestamp"])
    perp = perp.set_index("timestamp").sort_index()

    # Group by expiry, find ATM strike using perp price at settlement
    atm_pairs = []
    for exp, grp in chain.groupby("settlement_dt"):
        spot = perp["close"].reindex([exp], method="nearest").iloc[0]
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

    # Download 1h mark history for each leg
    print("\nDownloading 1h mark history...")
    for pair in atm_pairs:
        for side, sym in [("C", pair["call_sym"]), ("P", pair["put_sym"])]:
            out_path = OUT_DIR / f"{sym}_mark_1h.csv"
            if out_path.exists():
                print(f"    cached {out_path.name}")
                continue
            df = fetch_candles(f"MARK:{sym}", "1h", start_ts, end_ts)
            if df.empty:
                print(f"    empty {sym}")
                continue
            df.to_csv(out_path, index=False)
            print(f"    saved {out_path.name} ({len(df)} rows)")

    print("\nDone.")


if __name__ == "__main__":
    main()
