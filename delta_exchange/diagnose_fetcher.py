"""Diagnose what fetch_candles ACTUALLY returns for a known case.

We know the truth from manual API check:
  MARK:C-BTC-62400-050626 at Jun 4 12:00 UTC = $1,080.90

This script:
  1. Calls fetch_candles via the same path the fetcher uses
  2. Compares to direct minimal request
  3. Shows the raw response so we can see the timestamp / value pattern
"""
from __future__ import annotations
import sys, os
from datetime import datetime, timezone
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))

import requests
import pandas as pd

# Import the fetcher's function directly (it uses the same _get + pagination)
import fetch_delta_history as fdh

SYMBOL    = "C-BTC-62400-050626"
DECISION  = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
TS        = int(DECISION.timestamp())

print(f"  Testing symbol: {SYMBOL}")
print(f"  Decision time:  {DECISION.isoformat()} (unix {TS})")
print(f"  Expected MARK close at this time: $1,080.90 (from earlier API verification)")
print()

# ── Test A: 1h resolution (what the OLD fetcher used) ────────────────────────
print(f"  ── Test A: 1h resolution via fetch_candles (OLD fetcher path) ──")
df_1h = fdh.fetch_candles(f"MARK:{SYMBOL}", "1h", TS - 3 * 3600, TS + 3 * 3600)
print(df_1h.to_string(index=False))
print()
if not df_1h.empty:
    target = df_1h[df_1h["time"] == TS]
    if not target.empty:
        print(f"  → close at time={TS} (Jun 4 12:00 UTC): ${float(target['close'].iloc[0]):,.4f}")
    else:
        nearest = df_1h.iloc[(df_1h["time"] - TS).abs().argsort()].iloc[0]
        print(f"  → NO candle exactly at {TS}; nearest is time={int(nearest['time'])} "
              f"(diff {int(nearest['time'])-TS}s), close=${float(nearest['close']):,.4f}")

# ── Test B: 1m resolution (what the new approach SHOULD use) ─────────────────
print(f"\n  ── Test B: 1m resolution via fetch_candles ──")
df_1m = fdh.fetch_candles(f"MARK:{SYMBOL}", "1m", TS - 120, TS + 300)
print(df_1m.head(10).to_string(index=False))
print()
if not df_1m.empty:
    target = df_1m[df_1m["time"] == TS]
    if not target.empty:
        print(f"  → close at time={TS}: ${float(target['close'].iloc[0]):,.4f}")

# ── Test C: direct REST, no helper ───────────────────────────────────────────
print(f"\n  ── Test C: bare REST call to /v2/history/candles ──")
url = "https://api.india.delta.exchange/v2/history/candles"
r = requests.get(url, params={
    "symbol":     f"MARK:{SYMBOL}",
    "resolution": "1m",
    "start":      TS - 120,
    "end":        TS + 300,
}, timeout=10)
print(f"  HTTP {r.status_code}")
data = r.json()
for row in data.get("result", [])[:6]:
    print(f"    time={row['time']}  close=${float(row['close']):,.4f}  "
          f"({datetime.fromtimestamp(row['time'], tz=timezone.utc)})")
