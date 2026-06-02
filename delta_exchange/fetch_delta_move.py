"""
Delta Exchange India — MOVE Options Historical Fetcher
=======================================================
MOVE options pay |S_settle − K|. Each contract is listed ~24h before expiry
and decays into intrinsic value. The full universe over the last 90 days
is ~2,700 BTC MOVE contracts — vastly more granular than the weekly call/put
universe we already pulled.

Output:
  data/move/<SYMBOL>_mark_15m.csv
  data/move/<SYMBOL>_oi_15m.csv
  data/meta/move_contracts.csv

Usage:
  STAGE=options ./.venv/Scripts/python.exe fetch_delta_move.py
"""

import sys
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import pandas as pd
import requests

# share env loader, signing, and _get with the main fetcher
sys.path.insert(0, str(Path(__file__).parent))
from fetch_delta_history import _get, fetch_candles, REQ_PAUSE  # noqa: E402

DATA          = Path(__file__).parent / "data"
START_DT      = datetime(2026, 3, 1, tzinfo=timezone.utc)
END_DT        = datetime(2026, 6, 2, tzinfo=timezone.utc)
RESOLUTION    = "15m"
WORKER_COUNT  = 6
PRINT_LOCK    = Lock()


def fetch_expired_move() -> pd.DataFrame:
    """Paginate /v2/products for all expired BTC MOVE contracts."""
    rows_all = []
    after = None
    while True:
        params = {"contract_types": "move_options", "states": "expired"}
        if after: params["after"] = after
        data = _get("/v2/products", params)
        rows = data.get("result", [])
        rows_all.extend(rows)
        after = data.get("meta", {}).get("after")
        if not after or not rows: break
        time.sleep(REQ_PAUSE)
    df = pd.DataFrame(rows_all)
    if df.empty: return df
    # MOVE symbols begin with "MV-BTC-" — filter even though API filter should suffice
    df = df[df["symbol"].str.startswith("MV-BTC-")].copy()
    df["settlement_dt"] = pd.to_datetime(df["settlement_time"], utc=True, errors="coerce")
    df["strike_price"] = pd.to_numeric(df["strike_price"], errors="coerce")
    return df


def main():
    start_ts = int(START_DT.timestamp())
    end_ts   = int(END_DT.timestamp())
    print(f"Window: {START_DT.date()} → {END_DT.date()}")
    out_dir = DATA / "move"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Discovering MOVE BTC contracts...")
    df = fetch_expired_move()
    print(f"  total expired MV-BTC: {len(df):,}")
    if df.empty:
        return
    in_window = df[(df["settlement_dt"] >= START_DT) & (df["settlement_dt"] <= END_DT)]
    print(f"  settling in window: {len(in_window):,}")
    in_window.to_csv(DATA / "meta" / "move_contracts.csv", index=False)
    syms = sorted(in_window["symbol"].unique())

    print(f"\nPulling MARK + OI for {len(syms):,} MOVE contracts @ {RESOLUTION} "
          f"({WORKER_COUNT} workers)")

    def fetch_one(sym: str) -> str:
        mark_path = out_dir / f"{sym}_mark_{RESOLUTION}.csv"
        oi_path   = out_dir / f"{sym}_oi_{RESOLUTION}.csv"
        try:
            if not mark_path.exists():
                m = fetch_candles(f"MARK:{sym}", RESOLUTION, start_ts, end_ts)
                if not m.empty: m.to_csv(mark_path, index=False)
            if not oi_path.exists():
                o = fetch_candles(f"OI:{sym}", RESOLUTION, start_ts, end_ts)
                if not o.empty: o.to_csv(oi_path, index=False)
            return "ok"
        except Exception as e:
            return f"err: {e!r}"

    t0 = time.time()
    done = 0
    errs = 0
    total = len(syms)
    with ThreadPoolExecutor(max_workers=WORKER_COUNT) as ex:
        futures = {ex.submit(fetch_one, s): s for s in syms}
        for fut in as_completed(futures):
            done += 1
            result = fut.result()
            if result.startswith("err"):
                errs += 1
            if done % 50 == 0 or done == total:
                rate = done / (time.time() - t0)
                eta_s = (total - done) / max(rate, 0.01)
                with PRINT_LOCK:
                    print(f"  {done:>4}/{total}  rate {rate:.1f}/s  "
                          f"ETA {eta_s/60:.1f} min  errs {errs}", flush=True)
    print("\nDone.")


if __name__ == "__main__":
    main()
