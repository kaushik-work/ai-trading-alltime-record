"""Fetch N months of 1m perp mark candles from Delta India.

Writes the layout backtest_price_action_sweep.py expects:
    data/<subdir>/perp/<SYMBOL>_mark_1m.csv

/v2/history/candles is public, so no signing is needed. Pages backwards from
`end` in CANDLE_LIMIT-sized chunks.

Usage:
    python fetch_perp_6m.py                       # 6 months, BTC + ETH + XAUT
    python fetch_perp_6m.py --months 3 --symbols ETHUSD
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

API_BASE = "https://api.india.delta.exchange"
CANDLE_LIMIT = 2000
REQ_PAUSE = 0.05
HEADERS = {"User-Agent": "tgc-bot-python/1.0"}


def _get(path: str, params: dict) -> dict:
    url = f"{API_BASE}{path}"
    for attempt in range(5):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("X-RATE-LIMIT-RESET", 5))
                print(f"    rate-limited, sleeping {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def fetch_candles(symbol: str, resolution: str, start_ts: int, end_ts: int) -> pd.DataFrame:
    step = 60 * CANDLE_LIMIT if resolution == "1m" else 3600 * CANDLE_LIMIT
    out: list[dict] = []
    cur_end = end_ts
    calls = 0
    while cur_end > start_ts:
        cur_start = max(start_ts, cur_end - step)
        data = _get("/v2/history/candles", {
            "symbol": symbol, "resolution": resolution,
            "start": cur_start, "end": cur_end,
        })
        rows = data.get("result", [])
        calls += 1
        if not rows:
            break
        out.extend(rows)
        oldest = min(r["time"] for r in rows)
        cur_end = oldest - 1 if oldest <= cur_start else cur_start
        if calls % 20 == 0:
            print(f"    {symbol}: {len(out):>7,} bars, back to "
                  f"{datetime.fromtimestamp(oldest, timezone.utc):%Y-%m-%d}")
        time.sleep(REQ_PAUSE)
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out).drop_duplicates("time").sort_values("time")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.reset_index(drop=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--months", type=int, default=6)
    p.add_argument("--symbols", nargs="+", default=["BTCUSD", "ETHUSD", "XAUTUSD"])
    p.add_argument("--subdir", default="6m")
    args = p.parse_args()

    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=args.months * 31)
    out_dir = Path(__file__).parent / "data" / args.subdir / "perp"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching {args.months} months of 1m marks: "
          f"{start:%Y-%m-%d} -> {end:%Y-%m-%d}")
    print(f"Output: {out_dir}\n")

    for sym in args.symbols:
        t0 = time.time()
        print(f"{sym} ...")
        df = fetch_candles(sym, "1m", int(start.timestamp()), int(end.timestamp()))
        if df.empty:
            print(f"  {sym}: NO DATA returned\n")
            continue
        path = out_dir / f"{sym}_mark_1m.csv"
        df.to_csv(path, index=False)
        span = (df["timestamp"].max() - df["timestamp"].min()).days
        expected = span * 1440
        gaps = expected - len(df)
        print(f"  {sym}: {len(df):,} bars | {df['timestamp'].min():%Y-%m-%d} -> "
              f"{df['timestamp'].max():%Y-%m-%d} ({span}d) | "
              f"missing ~{gaps:,} ({gaps / max(expected, 1) * 100:.1f}%) | "
              f"{time.time() - t0:.0f}s")
        print(f"  -> {path}\n")


if __name__ == "__main__":
    main()
