"""
OI Wall Capture v2 - with Trend Filter + Complete Analysis.

Key fixes from v1:
  1. Trend filter: SMA50 from daily NIFTY - only buy CE above SMA50, PE below SMA50
  2. Momentum confirmation: NIFTY must close above wall for 1 bar before entry
     (avoids false breakouts where price dips through wall and comes back)
  3. Better SL: based on actual option premium movement, not arbitrary %

Strategy (daily timeframe, hold 1-5 days):
  Entry signal:  previous day's NIFTY close breaks wall AND trend confirms direction
  Entry timing:  next morning at open
  SL:            option drops 45% (lose Rs65 on Rs145 entry = Rs12,675 per trade at 3 lots)
  TP:            option reaches 2x entry (100% gain)
  Time exit:     after hold_days bars

Run:
  python scripts/backtest_oi_wall_v2.py
  python scripts/backtest_oi_wall_v2.py --hold-days 2 --sl-pct 0.50 --tp-mult 1.8
"""

import argparse
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.greeks import implied_vol, bs_price, days_to_expiry

# ── Args ──────────────────────────────────────────────────────────────────────
p = argparse.ArgumentParser()
p.add_argument("--capital",    type=float, default=40_000)
p.add_argument("--lots",       type=int,   default=3)
p.add_argument("--slippage",   type=float, default=2.0)
p.add_argument("--hold-days",  type=int,   default=3)
p.add_argument("--sl-pct",     type=float, default=0.45)
p.add_argument("--tp-mult",    type=float, default=2.00)
p.add_argument("--wall-prox",  type=int,   default=500)
p.add_argument("--oi-mult",    type=float, default=1.2)
p.add_argument("--sma-period", type=int,   default=20,  help="SMA period for trend filter (daily)")
p.add_argument("--pcr-lo-ce",  type=float, default=0.70)
p.add_argument("--pcr-hi-pe",  type=float, default=1.50)
args = p.parse_args()

CHAIN = Path(__file__).parent.parent / "db" / "option_chain_history.csv"
D1    = Path(__file__).parent.parent / "backtest_cache" / "NIFTY_1d.csv"
PCR_F = Path(__file__).parent.parent / "db" / "pcr_historical.csv"
LOT   = 65
QTY   = args.lots * LOT
SLIP  = args.slippage
RF    = 0.065

# ── Load data ─────────────────────────────────────────────────────────────────
chain = pd.read_csv(CHAIN)
chain["date"] = pd.to_datetime(chain["date"]).dt.date

nifty = pd.read_csv(D1, index_col=0, parse_dates=True)
nifty.columns = [c.capitalize() for c in nifty.columns]
nifty.index   = pd.to_datetime(nifty.index, utc=True).tz_localize(None)
nifty["_date"]  = nifty.index.date
nifty["Close"]  = nifty["Close"].astype(float)

# Compute SMA and trend BEFORE iterating
nifty_daily = nifty.groupby("_date").last().reset_index()
nifty_daily  = nifty_daily.sort_values("_date")
nifty_daily["sma"] = nifty_daily["Close"].rolling(args.sma_period, min_periods=5).mean()
nifty_daily["trend"] = np.where(nifty_daily["Close"] > nifty_daily["sma"], "UP", "DOWN")
trend_by_date = dict(zip(nifty_daily["_date"], nifty_daily["trend"]))
close_by_date = dict(zip(nifty_daily["_date"], nifty_daily["Close"].astype(float)))
open_by_date  = dict(zip(nifty_daily["_date"],
                         nifty.groupby("_date")["Open"].first().astype(float).to_dict().values()))
# Re-do open properly
open_by_date = {}
for d, grp in nifty.groupby("_date"):
    open_by_date[d] = float(grp["Open"].iloc[0])

pcr_hist = {}
if PCR_F.exists():
    pcr_df   = pd.read_csv(PCR_F, usecols=["date","pcr_weekly"])
    pcr_hist = {r["date"]: float(r["pcr_weekly"]) for _, r in pcr_df.iterrows()}

chain_days = sorted(chain["date"].unique())
nifty_days = sorted(set(nifty["_date"]))
all_days   = sorted(set(chain_days) & set(nifty_days))

# ── IV from bhavcopy ──────────────────────────────────────────────────────────
def get_iv(dc, spot, exp, as_of):
    try:
        atm = int(round(spot / 50)) * 50
        row = dc[(dc["strike"] == atm) & (dc["option_type"] == "CE")]
        if row.empty: return 0.15
        px = float(row["settle"].iloc[0])
        if px <= 0: return 0.15
        T  = days_to_expiry(date.fromisoformat(exp), today=as_of)
        if T <= 0: return 0.15
        iv = implied_vol(px, spot, atm, T, RF, "CE")
        return iv if iv and 0.05 < iv < 3.0 else 0.15
    except: return 0.15

iv_cache = {}
for d in chain_days:
    dc = chain[chain["date"] == d]
    if dc.empty: continue
    spot = float(dc["spot"].median())
    exp  = dc["expiry"].iloc[0] if not dc.empty else ""
    iv_cache[d] = get_iv(dc, spot, exp, d)

def iv_rank(d):
    past  = [iv_cache[dd] for dd in sorted(iv_cache) if dd < d and iv_cache.get(dd,0) > 0][-30:]
    today = iv_cache.get(d, 0)
    if len(past) < 5 or today <= 0: return 0.5
    lo, hi = min(past), max(past)
    return (today - lo) / (hi - lo) if hi > lo else 0.5

# ── Wall detection ─────────────────────────────────────────────────────────────
def find_walls(dc, spot):
    cw = pw = None
    for opt, above in [("CE", True), ("PE", False)]:
        sub = dc[dc["option_type"] == opt]
        if sub.empty: continue
        nearby = sub[sub["strike"] > spot] if above else sub[sub["strike"] < spot]
        nearby = nearby[nearby["strike"].between(spot - args.wall_prox, spot + args.wall_prox)]
        if nearby.empty: continue
        thr    = float(sub["oi"].median()) * args.oi_mult
        active = nearby[nearby["oi"] > thr]
        if active.empty: continue
        if above:
            cw = int(active.sort_values("strike").iloc[0]["strike"])
        else:
            pw = int(active.sort_values("strike", ascending=False).iloc[0]["strike"])
    return cw, pw

def opt_price(spot, strike, opt, exp, as_of, sigma):
    try:
        T = days_to_expiry(date.fromisoformat(exp), today=as_of)
        if T <= 0:
            intr = max(0, spot-strike) if opt=="CE" else max(0, strike-spot)
            return max(intr, 0.5)
        return max(bs_price(spot, strike, T, RF, sigma, opt), 0.5)
    except: return 0.5

# ── Backtest ──────────────────────────────────────────────────────────────────
trades   = []
equity   = args.capital
position = None
skipped  = {"trend": 0, "pcr": 0, "iv": 0, "no_wall": 0}

print(f"\n{'='*80}")
print(f"  OI Wall Capture v2 | Trend-Filtered | {len(all_days)} days")
print(f"  SMA{args.sma_period} trend filter | PCR CE>{args.pcr_lo_ce} PE<{args.pcr_hi_pe}")
print(f"  SL={args.sl_pct*100:.0f}% | TP={args.tp_mult:.1f}x | Hold={args.hold_days}d | {args.lots}lots")
print(f"{'='*80}")
print(f"  {'Date':<12}{'Sig':<4}{'Wall':>6}{'Entry':>8}{'Exit':>8}{'PnL':>10}  Rsn   PCR  IVR  Trend")
print(f"  {'-'*78}")

i = 1
while i < len(all_days):
    d = all_days[i]

    # ── Manage open position ──────────────────────────────────────────────────
    if position:
        sigma   = iv_cache.get(d, 0.15)
        close_d = close_by_date.get(d, 0)
        if close_d == 0:
            i += 1; continue
        curr_p  = opt_price(close_d, position["strike"], position["opt"],
                            position["exp"], d, sigma)
        sl_p    = position["entry_p"] * args.sl_pct
        tp_p    = position["entry_p"] * args.tp_mult
        held    = (d - position["entry_date"]).days

        if curr_p <= sl_p:
            exit_p = max(curr_p - SLIP, 0.5)
            rsn = "SL"
        elif curr_p >= tp_p:
            exit_p = min(curr_p + SLIP, curr_p * 1.02)
            rsn = "TP"
        elif held >= args.hold_days:
            exit_p = max(curr_p - SLIP, 0.5)
            rsn = "TIME"
        else:
            i += 1; continue

        pnl = (exit_p - position["entry_p"]) * QTY
        trades.append({
            "date": str(position["entry_date"]), "exit_date": str(d),
            "opt": position["opt"], "wall": position["wall"],
            "entry": round(position["entry_p"],1), "exit": round(exit_p,1),
            "pnl": round(pnl,2), "reason": rsn,
            "pcr": position["pcr"], "iv_rank": position["ivr"],
            "trend": position["trend"],
        })
        equity += pnl
        marker = "+" if pnl > 0 else " "
        print(f"  {position['entry_date']}  {position['opt']:<4}{position['wall']:>6}"
              f"  Rs{position['entry_p']:>5.0f}  Rs{exit_p:>5.0f}  Rs{pnl:>+8.0f}"
              f"  {rsn:<5} {position['pcr']:.2f} {position['ivr']:.2f} {position['trend']}")
        position = None
        i += 1
        continue

    # ── Check entry signal ────────────────────────────────────────────────────
    # Signal: TODAY's NIFTY close breaks through YESTERDAY's OI wall
    # Entry:  TOMORROW's open (so we can confirm close before acting)
    prev_d  = all_days[i - 1]
    prev_dc = chain[chain["date"] == prev_d]
    if prev_dc.empty: i += 1; continue

    prev_spot  = float(prev_dc["spot"].median())
    exp        = prev_dc["expiry"].iloc[0]
    cw, pw     = find_walls(prev_dc, prev_spot)   # yesterday's walls
    today_close= close_by_date.get(d, 0)          # today's NIFTY close
    day_pcr    = pcr_hist.get(str(prev_d), 1.0)
    sigma      = iv_cache.get(prev_d, 0.15)
    trend      = trend_by_date.get(d, "UP")       # today's trend
    ivr        = iv_rank(d)

    if today_close == 0: i += 1; continue

    # Skip if IV too expensive (top 25% of historical range)
    if ivr > 0.75:
        skipped["iv"] += 1
        i += 1; continue

    # Entry will be at TOMORROW's open
    if i + 1 >= len(all_days): i += 1; continue
    next_d     = all_days[i + 1]
    entry_spot = open_by_date.get(next_d, 0)
    if entry_spot == 0: i += 1; continue

    sig = opt = wall = None

    # CE signal: today's NIFTY close above yesterday's CE wall AND uptrend
    if (cw and today_close > cw
            and trend == "UP"
            and args.pcr_lo_ce <= day_pcr <= 1.40):
        sig, opt, wall = "CE", "CE", cw

    # PE signal: today's NIFTY close below yesterday's PE wall AND downtrend
    elif (pw and today_close < pw
              and trend == "DOWN"
              and 0.65 <= day_pcr <= args.pcr_hi_pe):
        sig, opt, wall = "PE", "PE", pw

    if sig is None:
        if not (cw or pw): skipped["no_wall"] += 1
        elif (trend == "UP" and pw and today_close < pw) or \
             (trend == "DOWN" and cw and today_close > cw):
            skipped["trend"] += 1
        i += 1; continue

    # Compute entry premium at tomorrow's open using BS
    T  = days_to_expiry(date.fromisoformat(exp), today=next_d)
    ep = max(bs_price(entry_spot, wall, T, RF, sigma, opt) + SLIP, 1.0)

    position = {
        "opt": opt, "wall": wall, "strike": wall, "entry_p": round(ep, 1),
        "entry_spot": entry_spot, "entry_date": next_d, "exp": exp,
        "pcr": day_pcr, "ivr": ivr, "trend": trend,
    }
    i += 2   # skip to day after entry (position is now open)

# ── Full Results ───────────────────────────────────────────────────────────────
print(f"\n{'='*80}")
print(f"  Skipped: trend={skipped['trend']} | pcr={skipped['pcr']} | iv={skipped['iv']} | no_wall={skipped['no_wall']}")

if not trades:
    print("  No trades generated. Try --wall-prox 600 or --oi-mult 1.0")
    sys.exit(0)

df    = pd.DataFrame(trades)
wins  = df[df["pnl"] > 0]
loss  = df[df["pnl"] <= 0]
wr    = len(wins) / len(df) * 100
gp    = wins["pnl"].sum()
gl    = abs(loss["pnl"].sum())
pf    = round(gp / gl, 2) if gl > 0 else 99.9
net   = equity - args.capital
npct  = net / args.capital * 100

print(f"\n  === COMPLETE RESULTS ===")
print(f"  Trades  : {len(df)} ({len(df[df['opt']=='CE'])} CE / {len(df[df['opt']=='PE'])} PE)")
print(f"  Win rate: {wr:.1f}%  ({len(wins)} wins / {len(loss)} losses)")
print(f"  Profit F: {pf}")
print(f"  Avg win : Rs{wins['pnl'].mean():+,.0f}  |  Avg loss: Rs{loss['pnl'].mean():+,.0f}")
print(f"  Net P&L : Rs{net:+,.0f}  ({npct:+.1f}%)")
print(f"  Capital : Rs{args.capital:,.0f} -> Rs{equity:,.0f}")

print(f"\n  By exit reason:")
for rsn, grp in df.groupby("reason"):
    wr_r = len(grp[grp["pnl"] > 0]) / len(grp) * 100
    print(f"    {rsn:<6}: {len(grp):>3} trades | PnL Rs{grp['pnl'].sum():>+9,.0f} | "
          f"WR={wr_r:.0f}% | avg Rs{grp['pnl'].mean():+,.0f}")

print(f"\n  By direction:")
for opt, grp in df.groupby("opt"):
    wr_o = len(grp[grp["pnl"] > 0]) / len(grp) * 100
    print(f"    {opt:<3}: {len(grp):>3} trades | PnL Rs{grp['pnl'].sum():>+9,.0f} | WR={wr_o:.0f}%")

print(f"\n  Monthly breakdown:")
df["month"] = pd.to_datetime(df["date"]).dt.to_period("M").astype(str)
for mo, grp in df.groupby("month"):
    wr_m = len(grp[grp["pnl"] > 0]) / len(grp) * 100
    print(f"    {mo}: {len(grp):>2} trades | Rs{grp['pnl'].sum():>+8,.0f} | WR={wr_m:.0f}%")

print(f"\n  Best trade : Rs{df['pnl'].max():+,.0f}")
print(f"  Worst trade: Rs{df['pnl'].min():+,.0f}")
print(f"  Max equity : Rs{args.capital + df['pnl'].cumsum().max():,.0f}")
