"""
Real-Price Backtest — uses actual option LTP from snapshots, not Black-Scholes.

This is the honest version. Replaces BS-estimated prices with real market data
collected by collect_option_snapshots.py.

Without this, every P&L number in the BS backtest is fiction.
With this, entry price, SL hits, TP hits, and exit prices are all real.

Usage:
  python scripts/backtest_real_prices.py               # all available dates
  python scripts/backtest_real_prices.py --date 2026-04-07
  python scripts/backtest_real_prices.py --sl-pct 0.40 --tp-mult 3.0

Requires: db/oi_snapshots/YYYY-MM-DD.csv files from collect_option_snapshots.py
"""

import argparse
import sys
from datetime import date, time
from pathlib import Path

import pandas as pd

p = argparse.ArgumentParser()
p.add_argument("--sl-pct",    type=float, default=0.40)
p.add_argument("--tp-mult",   type=float, default=3.0)
p.add_argument("--lots",      type=int,   default=1)
p.add_argument("--date",      type=str,   default=None, help="single date YYYY-MM-DD")
p.add_argument("--symbol",    default="NIFTY")
p.add_argument("--gap-thresh",type=float, default=50)
p.add_argument("--mom-thresh",type=float, default=75)
p.add_argument("--rev-thresh",type=float, default=200)
args = p.parse_args()

LOT      = 65 if args.symbol == "NIFTY" else 15
QTY      = args.lots * LOT
SNAP_DIR = Path(__file__).parent.parent / "db" / "oi_snapshots"
F5M      = Path(__file__).parent.parent / "backtest_cache" / f"{args.symbol}_5m_180d.csv"

if not SNAP_DIR.exists() or not any(SNAP_DIR.glob("*.csv")):
    print(f"\n  No snapshot data found in {SNAP_DIR}")
    print(f"  Run first: python scripts/collect_option_snapshots.py --symbol {args.symbol}")
    sys.exit(1)

if not F5M.exists():
    print(f"  No 5m spot data at {F5M}")
    sys.exit(1)

# ── Load 5m spot ──────────────────────────────────────────────────────────────
n5 = pd.read_csv(F5M, index_col=0, parse_dates=True)
n5.index   = pd.to_datetime(n5.index, utc=True).tz_localize(None)
n5.columns = [c.capitalize() for c in n5.columns]
n5["_date"] = n5.index.date
daily_close = {dt: float(grp["Close"].iloc[-1]) for dt, grp in n5.groupby("_date")}

# ── Load all snapshots ────────────────────────────────────────────────────────
snap_files = sorted(SNAP_DIR.glob("*.csv"))
if args.date:
    snap_files = [f for f in snap_files if args.date in f.stem]

if not snap_files:
    print(f"  No snapshot files found for date {args.date}")
    sys.exit(1)

snaps = pd.concat([pd.read_csv(f) for f in snap_files], ignore_index=True)
snaps["timestamp"] = pd.to_datetime(snaps["timestamp"])
snaps["_date"]     = snaps["timestamp"].dt.date
snaps["_time"]     = snaps["timestamp"].dt.time

print(f"\n{'='*76}")
print(f"  REAL-PRICE BACKTEST | {args.symbol} | {len(snap_files)} days of snapshot data")
print(f"  SL={args.sl_pct*100:.0f}% | TP={args.tp_mult:.0f}x | {args.lots} lot = {QTY} units")
print(f"{'='*76}")

def get_ltp(snaps_day: pd.DataFrame, strike: int, opt: str, at_time: time) -> float:
    """Get real option LTP from snapshot closest to at_time."""
    subset = snaps_day[
        (snaps_day["strike"] == strike) &
        (snaps_day["option_type"] == opt)
    ].copy()
    if subset.empty:
        return 0.0
    # Find closest snapshot at or after at_time
    subset = subset[subset["_time"] >= at_time]
    if subset.empty:
        subset = snaps_day[(snaps_day["strike"] == strike) & (snaps_day["option_type"] == opt)]
    row = subset.sort_values("timestamp").iloc[0]
    return float(row["ltp"])

# ── Signal logic (same as gap_signal.py) ────────────────────────────────────
def get_signal(gap, net15m):
    G, M, R = args.gap_thresh, args.mom_thresh, args.rev_thresh
    if gap < -G and net15m > G * 0.6:   return "CE", "GAP DOWN FILL"
    if gap < -G and net15m < -G:        return "PE", "GAP DOWN CONT"
    if gap > G  and net15m < -R:        return "PE", "GAP UP REVERSAL"
    if gap > G  and net15m > G:         return "CE", "GAP UP CONT"
    if abs(gap) <= G and net15m < -M:   return "PE", "FLAT MOM DOWN"
    if abs(gap) <= G and net15m > M:    return "CE", "FLAT MOM UP"
    return None, "NO SIGNAL"

# ── Backtest each snapshot day ─────────────────────────────────────────────────
trades  = []
capital = 40_000

print(f"\n  {'Date':<12} {'Signal':<18} {'Dir':<3} {'Strike':>7} {'Entry':>7} {'Exit':>7} {'PnL':>9}  Rsn")
print(f"  {'-'*74}")

for snap_date, day_snaps in snaps.groupby("_date"):
    # Need 5m data for gap calculation
    day_bars = n5[n5["_date"] == snap_date]
    if day_bars.empty:
        continue

    prev_days = sorted(d for d in daily_close if d < snap_date)
    if not prev_days:
        continue
    prev_close = daily_close[prev_days[-1]]

    open_p   = float(day_bars.iloc[0]["Open"])
    gap      = min(max(open_p - prev_close, -600), 600)

    bars_925 = day_bars[day_bars.index.time <= time(9, 25)]
    closes   = bars_925["Close"].astype(float).values
    net15m   = closes[min(2, len(closes)-1)] - open_p if len(closes) >= 2 else 0

    bars_930 = day_bars[day_bars.index.time <= time(9, 30)]
    spot_930 = float(bars_930["Close"].iloc[-1]) if not bars_930.empty else open_p

    direction, signal_type = get_signal(gap, net15m)
    if direction is None:
        print(f"  {snap_date}  {signal_type:<18}  ---  SKIP")
        continue

    strike = int(round(spot_930 / 50)) * 50

    # Get REAL entry price from snapshot at 9:30
    entry_p = get_ltp(day_snaps, strike, direction, time(9, 30))
    if entry_p <= 0:
        print(f"  {snap_date}  {signal_type:<18}  {direction}  {strike:>7}  NO LTP DATA")
        continue

    sl_p = entry_p * args.sl_pct
    tp_p = entry_p * args.tp_mult

    # Walk through snapshots to find SL/TP/TIME exit
    exit_p = exit_rsn = None
    exit_snaps = day_snaps[
        (day_snaps["strike"] == strike) &
        (day_snaps["option_type"] == direction) &
        (day_snaps["_time"] > time(9, 30))
    ].sort_values("timestamp")

    for _, row in exit_snaps.iterrows():
        ltp = float(row["ltp"])
        t   = row["_time"]
        if ltp <= sl_p:
            exit_p, exit_rsn = ltp, "SL"
            break
        if ltp >= tp_p:
            exit_p, exit_rsn = ltp, "TP"
            break
        if t >= time(15, 15):
            exit_p, exit_rsn = ltp, "EOD"
            break

    if exit_p is None and not exit_snaps.empty:
        exit_p   = float(exit_snaps.iloc[-1]["ltp"])
        exit_rsn = "EOD"

    if exit_p is None:
        print(f"  {snap_date}  {signal_type:<18}  {direction}  {strike:>7}  Rs{entry_p:>5.0f}  NO EXIT DATA")
        continue

    pnl = round((exit_p - entry_p) * QTY, 0)
    capital += pnl

    trades.append({
        "date": str(snap_date), "signal": signal_type, "dir": direction,
        "strike": strike, "entry": round(entry_p, 1), "exit": round(exit_p, 1),
        "pnl": pnl, "reason": exit_rsn
    })

    print(f"  {snap_date}  {signal_type:<18}  {direction}  {strike:>7}  "
          f"Rs{entry_p:>5.0f}  Rs{exit_p:>5.0f}  Rs{pnl:>+8.0f}  {exit_rsn}")

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*76}")
if not trades:
    print(f"  No completed trades in snapshot data.")
    print(f"  Collect more data: python scripts/collect_option_snapshots.py")
    sys.exit(0)

df   = pd.DataFrame(trades)
wins = df[df["pnl"] > 0]
loss = df[df["pnl"] <= 0]
wr   = len(wins) / len(df) * 100
gl   = abs(loss["pnl"].sum()) if len(loss) else 1
pf   = round(wins["pnl"].sum() / gl, 2) if len(loss) else 99.9
net  = capital - 40_000

print(f"\n  REAL-PRICE RESULTS (NOT BS-estimated)")
print(f"  Trades     : {len(df)}  ({len(wins)} wins / {len(loss)} losses)")
print(f"  Win rate   : {wr:.1f}%")
print(f"  Profit F   : {pf}")
print(f"  Net P&L    : Rs{net:+,.0f}")
print(f"  Capital    : Rs40,000 -> Rs{capital:,.0f}")
print(f"\n  THIS is your real edge. Compare to the BS backtest result.")
print(f"  If WR and PF are similar, the BS model was a reasonable proxy.")
print(f"  If they diverge significantly, the BS backtest was misleading.\n")
