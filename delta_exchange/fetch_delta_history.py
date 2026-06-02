"""
Delta Exchange India -- Historical Data Fetcher
================================================
Pulls MARK / OI / FUNDING / index history for BTC perp + active and expired
BTC options across the window we have trade tape for (2026-03-01 → 2026-06-01).

Output layout (under data/):
  data/perp/BTCUSD_mark_1m.csv
  data/perp/BTCUSD_funding_8h.csv          (funding posts every 8h; resolution kept native)
  data/perp/BTCUSD_oi_5m.csv
  data/perp/BTCUSD_price_1m.csv
  data/index/DEXBTUSD_1m.csv
  data/options/<SYMBOL>_mark_5m.csv
  data/options/<SYMBOL>_oi_5m.csv
  data/meta/contracts.csv                  (one row per option contract we touched)

Usage:
  .venv/Scripts/python fetch_delta_history.py
"""

import hashlib
import hmac
import os
import sys
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import pandas as pd
import requests

PRINT_LOCK = Lock()


def _load_env():
    """Lightweight .env loader (no python-dotenv dep). Walks up to repo root."""
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        env = parent / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                # strip inline comments + quotes
                v = v.split("#", 1)[0].strip().strip("'\"")
                os.environ.setdefault(k.strip(), v)
            return env
    return None


_ENV_PATH = _load_env()

# stage gate: "discover" runs perp+index+discovery only (fast); "options" adds bulk per-option pull
STAGE = os.environ.get("STAGE", "discover")
API_KEY    = os.environ.get("DELTA_API_KEY", "").strip()
API_SECRET = os.environ.get("DELTA_API_SECRET", "").strip()

# ── Config ────────────────────────────────────────────────────────────────────
API_BASE      = os.environ.get("DELTA_BASE_URL", "https://api.india.delta.exchange")
START_DT      = datetime.fromisoformat(os.environ.get("START_DT", "2026-03-01")).replace(tzinfo=timezone.utc)
END_DT        = datetime.fromisoformat(os.environ.get("END_DT",   "2026-06-01")).replace(tzinfo=timezone.utc)

UNDERLYING    = os.environ.get("UNDERLYING", "BTC").upper()
PERP_SYMBOL   = f"{UNDERLYING}USD"
INDEX_SYMBOL  = f".DE{UNDERLYING}USD"
# BTC stays at data/ for backward compatibility; other assets go in a subdir.
# OUT_SUBDIR env var overrides — useful for OOS pulls to avoid overwriting in-sample.
_out_override = os.environ.get("OUT_SUBDIR", "").strip()
if _out_override:
    OUT_DIR = Path(__file__).parent / "data" / _out_override
elif UNDERLYING == "BTC":
    OUT_DIR = Path(__file__).parent / "data"
else:
    OUT_DIR = Path(__file__).parent / "data" / UNDERLYING.lower()

CANDLE_LIMIT  = 2000          # API page size; one call returns up to this many bars
REQ_PAUSE     = 0.05          # seconds between API calls (per-thread; total throughput managed by WORKER_COUNT)
WORKER_COUNT  = 8             # concurrent threads for per-option pulls

OPT_RESOLUTION  = "1h"        # 1h is plenty for vol-premium / gamma-scalp strategies
PERP_RESOLUTION = "1m"
MONEYNESS_BAND  = 0.05        # keep strikes within ±5% of spot-at-settlement
WEEKLY_PLUS_ONLY = True       # drop daily-expiry contracts (Mon–Thu); keep Fri + month-end


# ── HTTP helper ───────────────────────────────────────────────────────────────
def _sign(method: str, path: str, query: str, body: str, ts: str) -> dict:
    """Build authenticated headers per Delta docs (HMAC-SHA256 over verb+ts+path+query+body)."""
    msg = method + ts + path + query + body
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "api-key": API_KEY,
        "signature": sig,
        "timestamp": ts,
        "User-Agent": "delta-fetch-python",
    }


_PRIVATE_PREFIXES = ("/v2/orders", "/v2/positions", "/v2/fills", "/v2/wallet",
                     "/v2/profile", "/v2/portfolio", "/v2/sub_accounts")


def _is_private(path: str) -> bool:
    return any(path.startswith(p) for p in _PRIVATE_PREFIXES)


def _get(path: str, params: dict) -> dict:
    """GET with retry on 429/5xx. Only signs PRIVATE endpoints (public ones reject auth)."""
    url = f"{API_BASE}{path}"
    for attempt in range(5):
        try:
            headers = {}
            if API_KEY and API_SECRET and _is_private(path):
                ts = str(int(time.time()))
                query = ("?" + requests.compat.urlencode(params)) if params else ""
                headers = _sign("GET", path, query, "", ts)
            r = requests.get(url, params=params, timeout=30, headers=headers)
            if r.status_code == 429:
                wait = int(r.headers.get("X-RATE-LIMIT-RESET", 5))
                print(f"    rate-limit hit, sleep {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == 4:
                raise
            print(f"    retry ({attempt+1}/5): {e}")
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


# ── Candle pagination ─────────────────────────────────────────────────────────
def _resolution_seconds(res: str) -> int:
    n = int(res[:-1])
    unit = res[-1]
    return n * {"m": 60, "h": 3600, "d": 86400}[unit]


def fetch_candles(symbol: str, resolution: str,
                  start_ts: int, end_ts: int) -> pd.DataFrame:
    """Page through /v2/history/candles, end → start direction."""
    step = _resolution_seconds(resolution) * CANDLE_LIMIT
    out = []
    cur_end = end_ts
    while cur_end > start_ts:
        cur_start = max(start_ts, cur_end - step)
        data = _get("/v2/history/candles", {
            "symbol": symbol,
            "resolution": resolution,
            "start": cur_start,
            "end": cur_end,
        })
        rows = data.get("result", [])
        if not rows:
            break
        out.extend(rows)
        oldest = min(r["time"] for r in rows)
        if oldest <= cur_start:
            cur_end = oldest - 1
        else:
            cur_end = cur_start
        time.sleep(REQ_PAUSE)
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out).drop_duplicates("time").sort_values("time")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.reset_index(drop=True)


# ── Discovery ─────────────────────────────────────────────────────────────────
def fetch_settled_options() -> pd.DataFrame:
    """All expired BTC options, paginated via /v2/products. Filter to window afterwards."""
    rows_all = []
    after = None
    while True:
        params = {
            "contract_types": "call_options,put_options",
            "underlying_asset_symbols": UNDERLYING,
            "states": "expired",
        }
        if after:
            params["after"] = after
        data = _get("/v2/products", params)
        rows = data.get("result", [])
        rows_all.extend(rows)
        after = data.get("meta", {}).get("after")
        if not after or not rows:
            break
        time.sleep(REQ_PAUSE)
    df = pd.DataFrame(rows_all)
    if df.empty:
        return df
    keep = [c for c in ["symbol", "id", "state", "settlement_time",
                        "strike_price", "contract_type"] if c in df.columns]
    df = df[keep].copy()
    df["settlement_dt"] = pd.to_datetime(df["settlement_time"], utc=True, errors="coerce")
    df["underlying_asset_symbol"] = UNDERLYING
    return df


def fetch_active_options() -> pd.DataFrame:
    data = _get("/v2/tickers", {
        "contract_types": "call_options,put_options",
        "underlying_asset_symbols": UNDERLYING,
    })
    rows = data.get("result", [])
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    keep = [c for c in ["symbol", "product_id", "strike_price",
                        "contract_type", "mark_iv", "mark_price",
                        "spot_price", "oi", "oi_value_usd"] if c in df.columns]
    return df[keep].copy()


# ── Persistence ───────────────────────────────────────────────────────────────
def save(df: pd.DataFrame, path: Path) -> None:
    if df.empty:
        print(f"    {path.name}: 0 rows (skipped)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    print(f"    {path.name}: {len(df):,} rows")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    start_ts = int(START_DT.timestamp())
    end_ts   = int(END_DT.timestamp())
    print(f"Window: {START_DT.date()} → {END_DT.date()}  ({start_ts}..{end_ts})")
    OUT_DIR.mkdir(exist_ok=True)

    # 1. Perp + index (skip files that already exist)
    print("\n[1/3] Perp + index history")
    for sym, prefix, res, subdir, fname in [
        (PERP_SYMBOL,              "",        PERP_RESOLUTION, "perp",  f"{PERP_SYMBOL}_price_{PERP_RESOLUTION}.csv"),
        (f"MARK:{PERP_SYMBOL}",    "MARK",    PERP_RESOLUTION, "perp",  f"{PERP_SYMBOL}_mark_{PERP_RESOLUTION}.csv"),
        (f"OI:{PERP_SYMBOL}",      "OI",      "5m",            "perp",  f"{PERP_SYMBOL}_oi_5m.csv"),
        (f"FUNDING:{PERP_SYMBOL}", "FUNDING", "1h",            "perp",  f"{PERP_SYMBOL}_funding_1h.csv"),
        (INDEX_SYMBOL,             "",        PERP_RESOLUTION, "index", f"DEXBTUSD_{PERP_RESOLUTION}.csv"),
    ]:
        path = OUT_DIR / subdir / fname
        if path.exists():
            print(f"  {sym} @ {res}: cached ({path.name})")
            continue
        print(f"  {sym} @ {res}")
        df = fetch_candles(sym, res, start_ts, end_ts)
        save(df, path)

    # 2. Discover BTC option contracts that existed in window
    print("\n[2/3] Discovering option contracts in window")
    settled = fetch_settled_options()
    if not settled.empty and "underlying_asset_symbol" in settled.columns:
        settled = settled[settled["underlying_asset_symbol"] == UNDERLYING]
    if not settled.empty and "settlement_dt" in settled.columns:
        in_window = settled[
            (settled["settlement_dt"] >= START_DT) &
            (settled["settlement_dt"] <= END_DT)
        ]
    else:
        in_window = settled
    print(f"  settled BTC options expiring in window: {len(in_window):,}")

    active = fetch_active_options()
    print(f"  currently active BTC options: {len(active):,}")

    all_syms = sorted(set(
        list(in_window["symbol"]) if "symbol" in in_window.columns else []
    ) | set(
        list(active["symbol"]) if "symbol" in active.columns else []
    ))
    print(f"  total unique option symbols: {len(all_syms):,}")

    meta = pd.concat([
        in_window.assign(source="settled")  if not in_window.empty else pd.DataFrame(),
        active.assign(source="active")      if not active.empty   else pd.DataFrame(),
    ], ignore_index=True)
    save(meta, OUT_DIR / "meta" / "contracts.csv")

    # Apply moneyness filter using perp mark history at each contract's settlement_time
    perp_path = OUT_DIR / "perp" / f"{PERP_SYMBOL}_mark_{PERP_RESOLUTION}.csv"
    if perp_path.exists() and "settlement_dt" in meta.columns:
        perp = pd.read_csv(perp_path, parse_dates=["timestamp"])
        perp = perp.set_index("timestamp").sort_index()
        meta["settlement_dt"] = pd.to_datetime(meta["settlement_dt"], utc=True, errors="coerce")
        # for active (no settlement_dt), use latest perp price as proxy
        latest_px = perp["close"].iloc[-1]
        spot_series = perp["close"].reindex(meta["settlement_dt"], method="nearest")
        spot_series = spot_series.fillna(latest_px).values
        meta["spot_ref"] = spot_series
        meta["strike_price"] = pd.to_numeric(meta["strike_price"], errors="coerce")
        meta["moneyness_dev"] = (meta["strike_price"] - meta["spot_ref"]).abs() / meta["spot_ref"]
        keep = meta[meta["moneyness_dev"] <= MONEYNESS_BAND]
        keep = keep.drop_duplicates("symbol").reset_index(drop=True)
        print(f"  after moneyness filter (±{MONEYNESS_BAND:.0%}): {len(keep):,} contracts")
        if WEEKLY_PLUS_ONLY:
            # keep Friday expiries + last day of month
            sdt = pd.to_datetime(keep["settlement_dt"], utc=True)
            is_fri = sdt.dt.dayofweek == 4
            is_month_end = sdt.dt.is_month_end
            keep = keep[is_fri | is_month_end].reset_index(drop=True)
            print(f"  after weekly+month-end filter: {len(keep):,} contracts")
        all_syms = keep["symbol"].tolist()
        save(keep, OUT_DIR / "meta" / "contracts_filtered.csv")
    else:
        print("  WARN: cannot apply moneyness filter (no perp data or no settlement_dt)")

    if STAGE != "options":
        print(f"\n[STAGE=discover] Skipping bulk per-option pull. "
              f"Re-run with STAGE=options to fetch {len(all_syms):,} contracts × 2 streams.")
        print("Done.")
        return

    # 3. Per-option MARK + OI history (parallelized)
    print(f"\n[3/3] Per-option MARK + OI @ {OPT_RESOLUTION} for {len(all_syms):,} contracts "
          f"({WORKER_COUNT} workers)")
    opt_dir = OUT_DIR / "options"
    opt_dir.mkdir(parents=True, exist_ok=True)

    def fetch_one(sym: str) -> str:
        mark_path = opt_dir / f"{sym}_mark_{OPT_RESOLUTION}.csv"
        oi_path   = opt_dir / f"{sym}_oi_{OPT_RESOLUTION}.csv"
        try:
            if not mark_path.exists():
                df = fetch_candles(f"MARK:{sym}", OPT_RESOLUTION, start_ts, end_ts)
                if not df.empty:
                    df.to_csv(mark_path, index=False)
            if not oi_path.exists():
                df = fetch_candles(f"OI:{sym}", OPT_RESOLUTION, start_ts, end_ts)
                if not df.empty:
                    df.to_csv(oi_path, index=False)
            return "ok"
        except Exception as e:
            return f"err: {e!r}"

    t0 = time.time()
    done = 0
    errs = 0
    total = len(all_syms)
    with ThreadPoolExecutor(max_workers=WORKER_COUNT) as ex:
        futures = {ex.submit(fetch_one, sym): sym for sym in all_syms}
        for fut in as_completed(futures):
            done += 1
            result = fut.result()
            if result.startswith("err"):
                errs += 1
                with PRINT_LOCK:
                    print(f"  {futures[fut]}: {result}")
            if done % 50 == 0 or done == total:
                rate = done / (time.time() - t0)
                eta_s = (total - done) / max(rate, 0.01)
                with PRINT_LOCK:
                    print(f"  {done:>4}/{total}  rate {rate:.1f}/s  ETA {eta_s/60:.1f} min  errs {errs}")

    print("\nDone.")


if __name__ == "__main__":
    main()
