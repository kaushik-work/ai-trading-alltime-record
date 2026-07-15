"""
Generic option parity fetcher for any Delta India underlying.

For each expiry in the window, finds the ATM call+put pair at the target
entry DTE (default 5 days before expiry) and downloads 1m mark history from
that entry time to expiry. Also fetches the underlying perp 1m history if
missing.

Usage:
  UNDERLYING=BTC TARGET_DTE=5 RESOLUTION=1h .venv/Scripts/python fetch_options_for_parity.py
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
TARGET_DTE = int(os.environ.get("TARGET_DTE", "5"))
RESOLUTION = os.environ.get("RESOLUTION", "1m")
MIN_HISTORY_DAYS = float(os.environ.get("MIN_HISTORY_DAYS", "2.0"))


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
    print(f"Target entry DTE: {TARGET_DTE}, resolution: {RESOLUTION}, min history: {MIN_HISTORY_DAYS} days")
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
    skipped: dict[str, int] = {}

    def skip(exp, reason: str):
        skipped[reason] = skipped.get(reason, 0) + 1

    for exp, grp in chain.groupby("settlement_dt"):
        # Target entry is TARGET_DTE days before expiry. Pick the ATM strike
        # using spot at entry time, not at expiry, to avoid look-ahead bias.
        entry_dt = exp - timedelta(days=TARGET_DTE)
        if entry_dt < perp_ts.index.min() or entry_dt > perp_ts.index.max():
            skip(exp, "entry outside perp history")
            continue
        spot_entry = perp_ts.asof(entry_dt)
        if pd.isna(spot_entry):
            skip(exp, "no perp mark at entry")
            continue

        strikes = grp["strike_price"].dropna().unique()
        if len(strikes) == 0:
            skip(exp, "no strikes")
            continue
        atm = min(strikes, key=lambda k: abs(k - spot_entry))
        calls = grp[(grp["contract_type"] == "call_options") & (grp["strike_price"] == atm)]
        puts = grp[(grp["contract_type"] == "put_options") & (grp["strike_price"] == atm)]
        if calls.empty or puts.empty:
            skip(exp, "missing call or put")
            continue

        atm_pairs.append({
            "expiry": exp,
            "entry_dt": entry_dt,
            "strike": atm,
            "call_sym": calls.iloc[0]["symbol"],
            "put_sym": puts.iloc[0]["symbol"],
            "spot_entry": spot_entry,
            "spot_exp": perp_ts.reindex([exp], method="nearest").iloc[0],
        })

    if skipped:
        print(f"  skipped {sum(skipped.values())} expiries:")
        for reason, n in sorted(skipped.items(), key=lambda x: -x[1]):
            print(f"    {reason}: {n}")

    print(f"Found {len(atm_pairs)} ATM expiry pairs (entry-time selection, {TARGET_DTE} DTE).")
    if not atm_pairs:
        return

    for pair in atm_pairs:
        exp = pair["expiry"]
        print(f"  entry {pair['entry_dt'].date()} expiry {exp.date()} K={pair['strike']} "
              f"spot@entry={pair['spot_entry']:.2f} spot@exp={pair['spot_exp']:.2f} "
              f"call={pair['call_sym']} put={pair['put_sym']}")

    print(f"\nDownloading 1m mark history (entry {TARGET_DTE} DTE → expiry)...")
    empty_or_short = []
    for pair in atm_pairs:
        exp = pair["expiry"]
        entry_dt = pair["entry_dt"]
        end_ts = int(exp.timestamp())
        start_ts = int(entry_dt.timestamp())
        for side, sym in [("C", pair["call_sym"]), ("P", pair["put_sym"])]:
            out_path = OUT_DIR / f"{sym}_mark_{RESOLUTION}.csv"
            if out_path.exists():
                print(f"    cached {out_path.name}")
                continue
            df = fetch_candles(f"MARK:{sym}", RESOLUTION, start_ts, end_ts)
            if df.empty:
                print(f"    empty {sym}")
                empty_or_short.append((sym, 0))
                continue
            history_days = (df["timestamp"].max() - df["timestamp"].min()).total_seconds() / 86400
            if history_days < MIN_HISTORY_DAYS:
                print(f"    short {sym}: {history_days:.2f} days < {MIN_HISTORY_DAYS}")
                empty_or_short.append((sym, history_days))
                continue
            df.to_csv(out_path, index=False)
            print(f"    saved {out_path.name} ({len(df)} rows, {history_days:.2f} days)")

    if empty_or_short:
        print(f"\n  {len(empty_or_short)} symbols had empty or short history and were not cached")

    print("\nDone.")


if __name__ == "__main__":
    main()
