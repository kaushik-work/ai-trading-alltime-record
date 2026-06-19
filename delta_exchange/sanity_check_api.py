"""Sanity check: compute pred directly from Delta's REST archive at the
two decision points we backtested. Compare to the cached-CSV pred.

If API-pred ≠ cached-pred by a wide margin, the cached _mark_1h.csv files
are corrupted and our 38-trade backtest is based on phantom dislocations.
"""
from __future__ import annotations
import re
import sys
import time
from datetime import datetime, timezone
import statistics
import requests
sys.stdout.reconfigure(encoding="utf-8")

API_BASE = "https://api.india.delta.exchange"
TT_MIN_HOURS = 6
TT_MAX_HOURS = 72
MONEYNESS = 0.05
MIN_STRIKES = 3


def to_unix(dt_str: str) -> int:
    """'2026-06-04 12:00 UTC' → unix seconds"""
    s = dt_str.replace(" UTC", "")
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M")
                       .replace(tzinfo=timezone.utc).timestamp())


def fetch_perp_close_at(symbol: str, ts: int) -> float | None:
    """Return MARK perp close at the 1m bar containing ts."""
    r = requests.get(f"{API_BASE}/v2/history/candles", params={
        "symbol": f"MARK:{symbol}", "resolution": "1m",
        "start": ts - 60, "end": ts + 120,
    }, timeout=10)
    rows = r.json().get("result", [])
    if not rows: return None
    candle = max((c for c in rows if int(c["time"]) <= ts),
                 key=lambda c: int(c["time"]), default=None)
    return float(candle["close"]) if candle else None


def fetch_all_settled_options(underlying: str):
    """Get all (currently-settled) options for the underlying. Each has
    settlement_time + symbol. Paginated."""
    all_rows = []
    after = None
    while True:
        params = {
            "contract_types": "call_options,put_options",
            "underlying_asset_symbols": underlying,
            "states": "expired",
        }
        if after: params["after"] = after
        r = requests.get(f"{API_BASE}/v2/products", params=params, timeout=15)
        data = r.json()
        rows = data.get("result", [])
        all_rows.extend(rows)
        after = data.get("meta", {}).get("after")
        if not after or not rows: break
        time.sleep(0.05)
    return all_rows


def filter_eligible(products: list, ts: int, expiry_dt: datetime, spot: float):
    """From the products list, keep only those whose:
       - settlement_time == expiry_dt
       - strike within ±5% of spot
       - has both call AND put versions
    Returns dict {strike: {'C': symbol, 'P': symbol}}."""
    by_strike = {}
    expiry_unix = int(expiry_dt.timestamp())
    for p in products:
        sym = p.get("symbol", "")
        m = re.match(r"^([CP])-[A-Z]+-(\d+)-(\d{6})$", sym)
        if not m: continue
        side, strike_str, ddmmyy = m.group(1), m.group(2), m.group(3)
        strike = int(strike_str)
        settle = pd_to_unix(p.get("settlement_time"))
        if abs(settle - expiry_unix) > 60: continue   # different expiry
        if abs(strike - spot) / spot > MONEYNESS: continue
        by_strike.setdefault(strike, {})[side] = sym
    return {k: v for k, v in by_strike.items() if "C" in v and "P" in v}


def pd_to_unix(iso: str) -> int:
    """Delta returns '2026-06-05T12:00:00Z' (ISO 8601)."""
    if not iso: return 0
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def fetch_mark(symbol: str, ts: int) -> float | None:
    r = requests.get(f"{API_BASE}/v2/history/candles", params={
        "symbol": f"MARK:{symbol}", "resolution": "1m",
        "start": ts - 60, "end": ts + 120,
    }, timeout=10)
    rows = r.json().get("result", [])
    if not rows: return None
    candle = max((c for c in rows if int(c["time"]) <= ts),
                 key=lambda c: int(c["time"]), default=None)
    return float(candle["close"]) if candle else None


def compute_pred_from_api(underlying: str, perp_sym: str,
                           decision_dt: datetime, expiry_dt: datetime,
                           cached_pred_pct: float, cached_spot: float):
    ts = int(decision_dt.timestamp())
    print(f"\n{'='*90}")
    print(f"  {underlying} decision @ {decision_dt.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Chosen expiry: {expiry_dt.strftime('%Y-%m-%d %H:%M UTC')}  "
          f"(TTE {(expiry_dt - decision_dt).total_seconds()/3600:.1f}h)")
    print(f"{'='*90}")

    # 1. perp spot from API
    print(f"\n  STEP 1: Fetching perp spot at decision time...")
    api_spot = fetch_perp_close_at(perp_sym, ts)
    if api_spot is None:
        print(f"    API returned NO perp candle at this time"); return
    print(f"    Cached spot: ${cached_spot:,.4f}")
    print(f"    API spot:    ${api_spot:,.4f}")
    spot_diff = abs(api_spot - cached_spot) / cached_spot * 100
    print(f"    Diff:        {spot_diff:.3f}%   {'(OK — within 0.5%)' if spot_diff < 0.5 else '(LARGE DIFF — investigate)'}")

    # 2. List options for the chosen expiry
    print(f"\n  STEP 2: Fetching options chain for {underlying}...")
    products = fetch_all_settled_options(underlying)
    print(f"    {len(products)} {underlying} options known to Delta's archive")
    by_strike = filter_eligible(products, ts, expiry_dt, api_spot)
    print(f"    {len(by_strike)} strikes match expiry {expiry_dt.date()} and ±5% of spot")
    if len(by_strike) < MIN_STRIKES:
        print(f"    Too few strikes — can't compute pred."); return

    # 3. Fetch mark for each call + put at decision time
    print(f"\n  STEP 3: Fetching MARK at {decision_dt.strftime('%H:%M UTC')} for each strike...")
    print(f"    {'strike':>8}  {'call':>10}  {'put':>10}  {'C-P+K':>10}  {'SF−Spot':>9}  {'dev %':>8}")
    print(f"    {'─'*72}")
    devs = []
    for K in sorted(by_strike):
        c_sym = by_strike[K]["C"]; p_sym = by_strike[K]["P"]
        cp = fetch_mark(c_sym, ts); time.sleep(0.05)
        pp = fetch_mark(p_sym, ts); time.sleep(0.05)
        if cp is None or pp is None or cp <= 0 or pp <= 0:
            print(f"    {K:>8}   missing mark data"); continue
        sf = cp - pp + K
        dev = (sf - api_spot) / api_spot * 100
        devs.append(dev)
        print(f"    {K:>8}  ${cp:>9.4f}  ${pp:>9.4f}  ${sf:>9.2f}  "
              f"${sf-api_spot:>+8.2f}  {dev:>+7.3f}%")

    if not devs:
        print(f"    No usable strikes."); return

    api_pred = statistics.median(devs)
    print(f"\n    API median pred: {api_pred:+.3f}%")
    print(f"    Cached pred:     {cached_pred_pct:+.3f}%")
    diff = abs(api_pred - cached_pred_pct)
    if diff < 0.1:
        print(f"    ✓ MATCH (diff {diff:.3f}%)")
    else:
        print(f"    ✗ MISMATCH (diff {diff:.3f}% — cached data is wrong)")


if __name__ == "__main__":
    # BTC: Jun 4 12:00 UTC, cached pred=+1.946%, cached spot=$62,559.10, expiry Jun 5 12:00 UTC
    compute_pred_from_api(
        underlying="BTC",
        perp_sym="BTCUSD",
        decision_dt=datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc),
        expiry_dt=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
        cached_pred_pct=1.946,
        cached_spot=62559.10,
    )
    # ETH: Jun 5 05:00 UTC, cached pred=-3.274%, cached spot=$1,741.28, expiry Jun 5 12:00 UTC
    compute_pred_from_api(
        underlying="ETH",
        perp_sym="ETHUSD",
        decision_dt=datetime(2026, 6, 5, 5, 0, tzinfo=timezone.utc),
        expiry_dt=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
        cached_pred_pct=-3.274,
        cached_spot=1741.28,
    )
