"""
Debug script — find max Raijin scores in historical data.
Shows why Raijin never fires and which conditions block it.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging; logging.disable(logging.CRITICAL)

import pandas as pd
import numpy as np
from strategies.nifty_scalp import score_signal, in_entry_window, SCORE_THRESHOLD
from datetime import time

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "backtest_cache", "NIFTY_5m_60d.csv")

df = pd.read_csv(CACHE, index_col=0, parse_dates=True)
df["_date"] = df.index.date

all_closes = df["Close"].astype(float).values
days = sorted(df["_date"].unique())

max_buy = 0.0
max_sell = 0.0
top_signals = []

global_offset = 0
day_offsets = {}
for d in days:
    day_offsets[d] = global_offset
    global_offset += int((df["_date"] == d).sum())

for day in days:
    day_df = df[df["_date"] == day]
    g_start = day_offsets[day]

    d_opens  = day_df["Open"].astype(float).values
    d_highs  = day_df["High"].astype(float).values
    d_lows   = day_df["Low"].astype(float).values
    d_closes = day_df["Close"].astype(float).values
    d_vols   = day_df["Volume"].astype(float).values

    for pos, (ts, row) in enumerate(day_df.iterrows()):
        bar_time = ts.time()
        g_idx = g_start + pos

        if not in_entry_window(bar_time):
            continue
        if g_idx < 20 or pos < 6:
            continue

        sig = score_signal(
            day_opens=d_opens[:pos+1],
            day_highs=d_highs[:pos+1],
            day_lows=d_lows[:pos+1],
            day_closes=d_closes[:pos+1],
            day_volumes=d_vols[:pos+1],
            all_closes=all_closes[:g_idx+1],
        )

        if sig["buy_score"] > max_buy:
            max_buy = sig["buy_score"]
        if sig["sell_score"] > max_sell:
            max_sell = sig["sell_score"]

        if sig["buy_score"] >= 4.0 or sig["sell_score"] >= 4.0:
            top_signals.append({
                "ts": str(ts),
                "buy": round(sig["buy_score"], 2),
                "sell": round(sig["sell_score"], 2),
                "vwap_band_b": sig["details"].get("vwap_band", 0) if sig["buy_score"] > sig["sell_score"] else 0,
                "ha_rev_b": sig["details"].get("ha_reversal", 0) if sig["buy_score"] > sig["sell_score"] else 0,
                "vwap": round(sig["vwap"], 1),
                "price": round(sig["price"], 1),
                "dist": round(sig["vwap"] - sig["price"], 1),
                "rsi9": round(sig["rsi9"], 1),
                "ha_flip": sig["ha_flipped"],
                "details": sig["details"],
            })

print(f"\nScore threshold: {SCORE_THRESHOLD}")
print(f"Max buy_score seen in entry window:  {max_buy:.2f}")
print(f"Max sell_score seen in entry window: {max_sell:.2f}")
print(f"\nBars with score >= 4.0: {len(top_signals)}")

if top_signals:
    top_signals.sort(key=lambda x: max(x["buy"], x["sell"]), reverse=True)
    print("\nTop 10 signals:")
    for s in top_signals[:10]:
        print(f"  {s['ts']}  buy={s['buy']}  sell={s['sell']}  "
              f"price={s['price']}  vwap={s['vwap']}  dist={s['dist']}  "
              f"rsi={s['rsi9']}  ha_flip={s['ha_flip']}")
        print(f"    details: {s['details']}")
