"""
Expiry Day Gap Strategy — Live Signal Script.
Run every Tuesday morning between 9:15 and 9:30 AM.

Usage:
  python scripts/expiry_signal.py          # reads today's 5m data
  python scripts/expiry_signal.py --date 2026-04-07   # backtest a specific day
"""

import argparse
import sys
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.greeks import bs_price, implied_vol, days_to_expiry

p = argparse.ArgumentParser()
p.add_argument("--date",      type=str,   default=None)
p.add_argument("--lots",      type=int,   default=1)
p.add_argument("--sl-pct",    type=float, default=0.40)
p.add_argument("--tp-mult",   type=float, default=3.0)
args = p.parse_args()

LOT  = 65
QTY  = args.lots * LOT
RF   = 0.065
SLIP = 2.0

CHAIN = Path(__file__).parent.parent / "db" / "option_chain_history.csv"
F5M_A = Path(__file__).parent.parent / "backtest_cache" / "NIFTY_5m_365d.csv"
F5M_B = Path(__file__).parent.parent / "backtest_cache" / "NIFTY_5m_180d.csv"
PCR_F = Path(__file__).parent.parent / "db" / "pcr_historical.csv"

# -- Load 5m data --------------------------------------------------------------
F5M = F5M_A if F5M_A.exists() else F5M_B
n5  = pd.read_csv(F5M, index_col=0, parse_dates=True)
n5.index   = pd.to_datetime(n5.index, utc=True).tz_localize(None)
n5.columns = [c.capitalize() for c in n5.columns]
n5["_date"] = n5.index.date

today = date.fromisoformat(args.date) if args.date else date.today()

if today.weekday() != 1:  # 1 = Tuesday
    print(f"\n  Today is {today.strftime('%A')}. This strategy only runs on TUESDAY (expiry day).")
    sys.exit(0)

# -- Previous day close --------------------------------------------------------
daily_close = {dt: float(grp["Close"].iloc[-1]) for dt, grp in n5.groupby("_date")}
prev_days   = sorted(d for d in daily_close if d < today)
if not prev_days:
    print("  No previous day data found.")
    sys.exit(1)
prev_close = daily_close[prev_days[-1]]

# -- Today's bars --------------------------------------------------------------
day_bars = n5[n5["_date"] == today]
if day_bars.empty:
    print(f"  No 5m data for {today}. Fetch latest data first.")
    sys.exit(1)

open_p  = float(day_bars.iloc[0]["Open"])
gap     = min(max(open_p - prev_close, -600), 600)

# Read bars up to 9:30
bars_930 = day_bars[day_bars.index.time <= time(9, 30)]
closes   = bars_930["Close"].astype(float).values

net15m = closes[min(2, len(closes)-1)] - open_p if len(closes) >= 3 else 0
spot_930 = float(bars_930.iloc[-1]["Close"]) if not bars_930.empty else open_p

# -- Option chain for IV -------------------------------------------------------
chain = pd.read_csv(CHAIN)
chain["date"] = pd.to_datetime(chain["date"]).dt.date
chain_days = sorted(d for d in chain["date"].unique() if d <= today)
ref_date   = chain_days[-1] if chain_days else None

sigma = 0.15
exp_str = None
if ref_date:
    dc  = chain[chain["date"] == ref_date]
    exp_str = dc["expiry"].iloc[0] if not dc.empty else None
    atm = int(round(spot_930 / 50)) * 50
    row = dc[(dc["strike"] == atm) & (dc["option_type"] == "CE")]
    if not row.empty:
        px = float(row["settle"].iloc[0])
        if px > 0 and exp_str:
            T_ = days_to_expiry(date.fromisoformat(exp_str), today=ref_date)
            if T_ > 0:
                iv_ = implied_vol(px, float(dc["spot"].median()), atm, T_, RF, "CE")
                if iv_ and 0.05 < iv_ < 3.0:
                    sigma = iv_

# T for today's expiry
exp_date = date.fromisoformat(exp_str) if exp_str else today
now_dt   = datetime(today.year, today.month, today.day, 9, 30)
exp_close = datetime(exp_date.year, exp_date.month, exp_date.day, 15, 30)
T_entry  = max((exp_close - now_dt).total_seconds() / (365 * 24 * 3600), 1/365/24)

# -- Signal logic --------------------------------------------------------------
if gap < -50 and net15m > 30:
    direction, signal_type = "CE", "GAP DOWN FILL  (gap filled -> rally)"
elif gap < -50 and net15m < -50:
    direction, signal_type = "PE", "GAP DOWN CONT  (gap continuing down)"
elif gap > 50 and net15m < -200:
    direction, signal_type = "PE", "GAP UP REVERSAL (gap failed hard)"
elif abs(gap) <= 50 and net15m < -75:
    direction, signal_type = "PE", "FLAT + MOMENTUM DOWN"
elif abs(gap) <= 50 and net15m > 75:
    direction, signal_type = "CE", "FLAT + MOMENTUM UP"
else:
    direction, signal_type = None, "NO SIGNAL"

# -- Strike and price ----------------------------------------------------------
strike = int(round(spot_930 / 50)) * 50
entry_p = None
if direction:
    try:
        entry_p = bs_price(spot_930, strike, T_entry, RF, sigma, direction) + SLIP
        entry_p = round(entry_p, 1)
    except:
        entry_p = None

sl_p = round(entry_p * args.sl_pct, 1) if entry_p else None
tp_p = round(entry_p * args.tp_mult, 1) if entry_p else None
capital_at_risk = round((entry_p - (sl_p or 0)) * QTY, 0) if entry_p else 0

# -- Print report --------------------------------------------------------------
SEP = "=" * 62
sep = "-" * 62

print(f"\n{SEP}")
print(f"  EXPIRY DAY SIGNAL  |  {today}  (Tuesday)")
print(f"{SEP}")
print(f"  Prev close : {prev_close:>8.0f}")
print(f"  Open 9:15  : {open_p:>8.0f}   Gap = {gap:>+.0f} pts")
print(f"  Spot 9:25  : {closes[min(2,len(closes)-1)]:>8.0f}   Net15m = {net15m:>+.0f} pts")
print(f"  Spot 9:30  : {spot_930:>8.0f}   (entry reference)")
print(f"  IV (sigma) : {sigma:.3f}  ({sigma*100:.1f}%)")
print(f"  T to expiry: {T_entry*365*24:.1f} hours")
print(f"\n{sep}")
print(f"  SIGNAL TYPE : {signal_type}")

if direction is None:
    print(f"\n  VERDICT: NO-GO — No clear gap/momentum signal today.")
    print(f"  Do NOT trade. Wait for next Tuesday.")
else:
    print(f"  DIRECTION   : {direction}")
    print(f"  STRIKE (ATM): {strike}")
    print(f"  Entry price : Rs{entry_p:.1f}  (BS + Rs{SLIP} slippage)")

    print(f"\n{sep}")
    print(f"  VERDICT: GO")
    print(f"\n  TRADE SETUP ({args.lots} lot = {QTY} units):")
    print(f"  +-------------------------------------------------+")
    print(f"  |  BUY  {direction} {strike} @ ~Rs{entry_p:.0f}             |")
    print(f"  |  Qty    : {QTY} units ({args.lots} lot x {LOT})                  |")
    print(f"  |  Capital: Rs{entry_p * QTY:>7,.0f}                          |")
    print(f"  |                                                 |")
    print(f"  |  SL     : Rs{sl_p:<6.0f}  (option drops {(1-args.sl_pct)*100:.0f}%)        |")
    print(f"  |  TP     : Rs{tp_p:<6.0f}  (option at {args.tp_mult:.0f}x entry)          |")
    print(f"  |  Time   : EXIT at 15:15 if SL/TP not hit       |")
    print(f"  |                                                 |")
    print(f"  |  Max loss: Rs{capital_at_risk:>7,.0f}                          |")
    print(f"  |  Max gain: Rs{(tp_p - entry_p) * QTY:>7,.0f}                          |")
    print(f"  +-------------------------------------------------+")

    print(f"\n  HOW TO MONITOR (every 5 min after entry):")
    print(f"  • If option < Rs{sl_p:.0f}  -> SELL immediately (SL)")
    print(f"  • If option > Rs{tp_p:.0f}  -> SELL immediately (TP)")
    print(f"  • At 15:15 exactly -> SELL whatever price (TIME)")

print(f"\n{SEP}\n")
