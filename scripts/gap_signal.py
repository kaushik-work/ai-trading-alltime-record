"""
Gap Signal — works any day, any supported index.

Supported:
  NIFTY     -> Tuesday expiry  (lot=65)
  BANKNIFTY -> Wednesday expiry (lot=15)
  SENSEX    -> Friday expiry   (lot=20, needs BSE data -- not yet implemented)

On expiry day  : maximum gamma, Rs30-60 option can 3-5x on a 200pt move
On other days  : same gap signal, weaker multiplier (lower gamma, more T remaining)

Usage:
  python scripts/gap_signal.py                          # NIFTY, today
  python scripts/gap_signal.py --symbol BANKNIFTY       # BANKNIFTY, today
  python scripts/gap_signal.py --date 2026-04-07        # specific date
  python scripts/gap_signal.py --symbol BANKNIFTY --date 2026-04-08
"""

import argparse
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.greeks import bs_price, implied_vol, days_to_expiry

# ── Config per instrument ─────────────────────────────────────────────────────
INSTRUMENTS = {
    "NIFTY": {
        "lot":         65,
        "expiry_day":  1,          # Tuesday = 1
        "expiry_name": "Tuesday",
        "cache":       "NIFTY_5m_180d.csv",
        "chain":       "option_chain_history.csv",
        "gap_thresh":  50,         # min gap pts to classify
        "mom_thresh":  75,         # min net15m for flat-open signal
        "rev_thresh":  200,        # min net15m reversal for gap-up-reversal signal
    },
    "BANKNIFTY": {
        "lot":         15,
        "expiry_day":  2,          # Wednesday = 2
        "expiry_name": "Wednesday",
        "cache":       "BANKNIFTY_5m_180d.csv",
        "chain":       None,       # no separate chain yet, use NIFTY IV as proxy
        "gap_thresh":  100,        # BANKNIFTY moves more, higher threshold
        "mom_thresh":  150,
        "rev_thresh":  400,
    },
}

p = argparse.ArgumentParser()
p.add_argument("--symbol",  default="NIFTY",   choices=list(INSTRUMENTS.keys()))
p.add_argument("--date",    default=None)
p.add_argument("--lots",    type=int, default=1)
p.add_argument("--sl-pct",  type=float, default=0.40)
p.add_argument("--tp-mult", type=float, default=3.0)
args = p.parse_args()

cfg  = INSTRUMENTS[args.symbol]
LOT  = cfg["lot"]
QTY  = args.lots * LOT
RF   = 0.065
SLIP = 2.0

BASE   = Path(__file__).parent.parent
F5M    = BASE / "backtest_cache" / cfg["cache"]
CHAIN  = BASE / "db" / "option_chain_history.csv"

if not F5M.exists():
    print(f"\n  No 5m data for {args.symbol}. Run:")
    cache_name = cfg['cache']
    print(f"  python -c \"from data.angel_fetcher import AngelFetcher; "
          f"df=AngelFetcher.get().fetch_historical_df('{args.symbol}','5m',days=180); "
          f"df.to_csv('backtest_cache/{cache_name}')\"")
    sys.exit(1)

# ── Load data ─────────────────────────────────────────────────────────────────
n5 = pd.read_csv(F5M, index_col=0, parse_dates=True)
n5.index   = pd.to_datetime(n5.index, utc=True).tz_localize(None)
n5.columns = [c.capitalize() for c in n5.columns]
n5["_date"] = n5.index.date

today = date.fromisoformat(args.date) if args.date else date.today()

# ── Previous close ────────────────────────────────────────────────────────────
daily_close = {dt: float(grp["Close"].iloc[-1]) for dt, grp in n5.groupby("_date")}
prev_days   = sorted(d for d in daily_close if d < today)
if not prev_days:
    print("  No previous day data found in cache.")
    sys.exit(1)
prev_close = daily_close[prev_days[-1]]

# ── Today's bars ──────────────────────────────────────────────────────────────
day_bars = n5[n5["_date"] == today]
if day_bars.empty:
    print(f"  No 5m data for {today}. Fetch latest data first.")
    sys.exit(1)

open_p   = float(day_bars.iloc[0]["Open"])
gap_raw  = open_p - prev_close
gap      = min(max(gap_raw, -600), 600)

bars_925 = day_bars[day_bars.index.time <= time(9, 25)]
bars_930 = day_bars[day_bars.index.time <= time(9, 30)]
closes   = bars_925["Close"].astype(float).values

net15m   = closes[min(2, len(closes)-1)] - open_p if len(closes) >= 2 else 0
spot_930 = float(bars_930["Close"].iloc[-1]) if not bars_930.empty else open_p

# ── Nearest expiry ────────────────────────────────────────────────────────────
expiry_weekday = cfg["expiry_day"]
days_to_exp    = (expiry_weekday - today.weekday()) % 7
if days_to_exp == 0:
    days_to_exp = 0   # today IS expiry day
expiry_date = today + timedelta(days=days_to_exp)
is_expiry   = (today.weekday() == expiry_weekday)

# T remaining from 9:30 AM today to 15:30 expiry day
now_dt      = datetime(today.year, today.month, today.day, 9, 30)
exp_close   = datetime(expiry_date.year, expiry_date.month, expiry_date.day, 15, 30)
T_hours     = (exp_close - now_dt).total_seconds() / 3600
T_entry     = max(T_hours / (365 * 24), 1/365/24)

# ── IV from NIFTY chain (proxy for BANKNIFTY too) ────────────────────────────
sigma = 0.15
chain = pd.read_csv(CHAIN)
chain["date"] = pd.to_datetime(chain["date"]).dt.date
ref_days = sorted(d for d in chain["date"].unique() if d <= today)
if ref_days:
    ref_date = ref_days[-1]
    dc  = chain[chain["date"] == ref_date]
    atm_c = int(round(float(dc["spot"].median()) / 50)) * 50
    row = dc[(dc["strike"] == atm_c) & (dc["option_type"] == "CE")]
    if not row.empty:
        px = float(row["settle"].iloc[0])
        exp_s = dc["expiry"].iloc[0]
        T_ = days_to_expiry(date.fromisoformat(exp_s), today=ref_date)
        if px > 0 and T_ > 0:
            iv_ = implied_vol(px, float(dc["spot"].median()), atm_c, T_, RF, "CE")
            if iv_ and 0.05 < iv_ < 3.0:
                sigma = iv_

# ── Signal logic ──────────────────────────────────────────────────────────────
G = cfg["gap_thresh"]
M = cfg["mom_thresh"]
R = cfg["rev_thresh"]

if gap < -G and net15m > (G * 0.6):
    direction, signal_type = "CE", "GAP DOWN FILL  (gap filled -> rally)"
elif gap < -G and net15m < -G:
    direction, signal_type = "PE", "GAP DOWN CONT  (gap continuing down)"
elif gap > G and net15m < -R:
    direction, signal_type = "PE", "GAP UP REVERSAL (gap collapsed hard)"
elif gap > G and net15m > G:
    direction, signal_type = "CE", "GAP UP CONT    (gap holding, move up)"
elif abs(gap) <= G and net15m < -M:
    direction, signal_type = "PE", "FLAT + MOMENTUM DOWN"
elif abs(gap) <= G and net15m > M:
    direction, signal_type = "CE", "FLAT + MOMENTUM UP"
else:
    direction, signal_type = None, "NO SIGNAL — skip this week"

# ── Option price (ATM at 9:30) ────────────────────────────────────────────────
strike  = int(round(spot_930 / 50)) * 50
entry_p = tp_p = sl_p = None

if direction:
    try:
        raw_p   = bs_price(spot_930, strike, T_entry, RF, sigma, direction)
        entry_p = round(raw_p + SLIP, 1)
        sl_p    = round(entry_p * args.sl_pct, 1)
        tp_p    = round(entry_p * args.tp_mult, 1)
    except:
        pass

# ── Expected return table ─────────────────────────────────────────────────────
def expected_mult(move_pts, T_h, sig, opt):
    try:
        e  = bs_price(spot_930, strike, T_entry, RF, sig, opt)
        S2 = spot_930 - move_pts if opt == "PE" else spot_930 + move_pts
        T2 = max((T_h - 4) / (365 * 24), 0.00001)  # 4h later
        ex = bs_price(S2, strike, T2, RF, sig, opt)
        return round(ex / e, 2) if e > 0 else 0
    except:
        return 0

# ── Print ─────────────────────────────────────────────────────────────────────
SEP = "=" * 64
sep = "-" * 64

print(f"\n{SEP}")
print(f"  GAP SIGNAL | {args.symbol} | {today} ({today.strftime('%A')})")
print(f"{SEP}")
print(f"  Prev close  : {prev_close:>9.0f}")
print(f"  Open 9:15   : {open_p:>9.0f}   Gap     = {gap:>+.0f} pts")
print(f"  Spot 9:25   : {closes[min(2,len(closes)-1)]:>9.0f}   Net15m  = {net15m:>+.0f} pts")
print(f"  Spot 9:30   : {spot_930:>9.0f}   Strike  = {strike} (ATM)")
print(f"  Expiry day  : {cfg['expiry_name']} {expiry_date}  ({'TODAY' if is_expiry else f'in {days_to_exp} days'})")
print(f"  T to expiry : {T_hours:.1f} hours  ({T_hours/24:.1f} days)")
print(f"  IV (sigma)  : {sigma:.3f}  ({sigma*100:.1f}%)")

print(f"\n{sep}")
print(f"  SIGNAL : {signal_type}")

if direction is None:
    print(f"\n  VERDICT: NO-GO — conditions not met, skip this week.")
else:
    print(f"  VERDICT: GO  |  Buy {direction} {strike}  |  {args.lots} lot")
    print()

    if entry_p:
        print(f"  Entry  ~Rs{entry_p:.0f}  |  SL Rs{sl_p:.0f}  |  TP Rs{tp_p:.0f}")
        print(f"  Capital : Rs{entry_p * QTY:,.0f}  |  Max loss: Rs{(entry_p-sl_p)*QTY:,.0f}")

    print(f"\n  Expected return if NIFTY moves [direction] from 9:30:")
    print(f"  {'Move':>8}  {'~Option price':>14}  {'Mult':>6}  {'Rs gain/loss (1lot)':>20}")
    print(f"  {'-'*54}")

    moves = [50, 100, 150, 200, 300]
    if entry_p:
        for mv in moves:
            S2 = spot_930 - mv if direction == "PE" else spot_930 + mv
            T2 = max(T_entry - 4/(365*24), 0.00001)
            try:
                ex  = bs_price(S2, strike, T2, RF, sigma, direction)
                pct = (ex - entry_p) / entry_p * 100
                gain = (ex - entry_p) * QTY
                print(f"  {mv:>6}pts  Rs{ex:>12.1f}  {ex/entry_p:>5.2f}x  Rs{gain:>+18,.0f}")
            except:
                pass

    print(f"\n  {'WRONG direction':>8} (if market goes against you):")
    if entry_p:
        for mv in [50, 100, 150]:
            S2 = spot_930 + mv if direction == "PE" else spot_930 - mv
            T2 = max(T_entry - 4/(365*24), 0.00001)
            try:
                ex   = bs_price(S2, strike, T2, RF, sigma, direction)
                pct  = (ex - entry_p) / entry_p * 100
                gain = (ex - entry_p) * QTY
                sl_hit = "SL HIT" if ex <= sl_p else ""
                print(f"  {mv:>6}pts  Rs{ex:>12.1f}  {ex/entry_p:>5.2f}x  Rs{gain:>+18,.0f}  {sl_hit}")
            except:
                pass

    print()
    if is_expiry:
        print(f"  *** EXPIRY DAY — maximum gamma. Small moves = big % gains. ***")
        print(f"  *** Monitor every 5 min. Exit at 15:15 latest.            ***")
    else:
        print(f"  Non-expiry day. T={T_hours:.0f}h remaining. Returns ~{T_hours/6*100:.0f}% of expiry-day returns.")
        print(f"  Can hold overnight if move is developing — not forced to exit today.")

    print(f"\n  STEPS:")
    print(f"  1. At 9:30 — Buy {direction} {strike} at market price (~Rs{entry_p:.0f})")
    print(f"  2. Check every 5 min:")
    print(f"     Option < Rs{sl_p:.0f} -> SELL (SL hit)")
    print(f"     Option > Rs{tp_p:.0f} -> SELL (TP hit)")
    print(f"  3. At {'15:15' if is_expiry else '15:20'} -> SELL whatever price (time exit)")

print(f"\n{SEP}")

if args.symbol == "SENSEX":
    print(f"  NOTE: SENSEX support requires BSE bhavcopy data.")
    print(f"  Currently using NIFTY IV as proxy. BSE integration = future work.")
print()
