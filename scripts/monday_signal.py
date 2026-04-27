"""
Monday Morning Signal Checker — GO / NO-GO for next-expiry options trade.

Reads today's option chain (bhavcopy or last available) and prints:
  - OI wall levels (CE and PE)
  - Max Pain strike
  - PCR (weekly)
  - Directional bias (CE or PE)
  - Delta of the Rs110-target strike (next expiry T+8d)
  - IV/RV ratio (from 5m bars if available)
  - Final verdict: GO or NO-GO with reason

Run every Monday morning before 9:30:
  python scripts/monday_signal.py
  python scripts/monday_signal.py --target-prem 120 --delta-min 0.22
"""

import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.greeks import bs_price, days_to_expiry, greeks, implied_vol

import argparse
p = argparse.ArgumentParser()
p.add_argument("--target-prem", type=float, default=110)
p.add_argument("--delta-min",   type=float, default=0.22)
p.add_argument("--pcr-lo-ce",   type=float, default=1.40, help="skip CE if PCR > this")
p.add_argument("--pcr-hi-pe",   type=float, default=0.80, help="skip PE if PCR < this")
p.add_argument("--wall-prox",   type=int,   default=500)
p.add_argument("--oi-mult",     type=float, default=1.2)
p.add_argument("--rv-bars",     type=int,   default=20)
p.add_argument("--date",        type=str,   default=None, help="override date YYYY-MM-DD")
args = p.parse_args()

RF = 0.065

CHAIN = Path(__file__).parent.parent / "db" / "option_chain_history.csv"
PCR_F = Path(__file__).parent.parent / "db" / "pcr_historical.csv"
F5M_A = Path(__file__).parent.parent / "backtest_cache" / "NIFTY_5m_365d.csv"
F5M_B = Path(__file__).parent.parent / "backtest_cache" / "NIFTY_5m_180d.csv"
D1    = Path(__file__).parent.parent / "backtest_cache" / "NIFTY_1d.csv"

# ── Load data ──────────────────────────────────────────────────────────────────
chain = pd.read_csv(CHAIN)
chain["date"] = pd.to_datetime(chain["date"]).dt.date

pcr_hist = {}
if PCR_F.exists():
    _pdf = pd.read_csv(PCR_F, usecols=["date", "pcr_weekly"])
    pcr_hist = {r["date"]: float(r["pcr_weekly"]) for _, r in _pdf.iterrows()}

# Use most recent chain day as reference
today = date.today() if not args.date else date.fromisoformat(args.date)
available_dates = sorted(chain["date"].unique())
ref_date = max(d for d in available_dates if d <= today) if any(d <= today for d in available_dates) else available_dates[-1]

dc = chain[chain["date"] == ref_date]
if dc.empty:
    print(f"No chain data for {ref_date}. Run fetch_option_chain_history.py first.")
    sys.exit(1)

spot    = float(dc["spot"].median())
exp_str = dc["expiry"].iloc[0]
exp_dt  = date.fromisoformat(exp_str)
T_curr  = max(days_to_expiry(exp_dt, today=ref_date), 1 / 365)
T_next  = T_curr + 7 / 365

# ── ATM IV ────────────────────────────────────────────────────────────────────
def get_iv(dc, spot, exp_str, as_of):
    try:
        atm = int(round(spot / 50)) * 50
        row = dc[(dc["strike"] == atm) & (dc["option_type"] == "CE")]
        if row.empty: return 0.15
        px  = float(row["settle"].iloc[0])
        if px <= 0: return 0.15
        T   = days_to_expiry(date.fromisoformat(exp_str), today=as_of)
        if T <= 0: return 0.15
        iv  = implied_vol(px, spot, atm, T, RF, "CE")
        return iv if iv and 0.05 < iv < 3.0 else 0.15
    except:
        return 0.15

sigma = get_iv(dc, spot, exp_str, ref_date)
atm   = int(round(spot / 50)) * 50

# ── OI Walls ──────────────────────────────────────────────────────────────────
ce = dc[dc["option_type"] == "CE"]
pe = dc[dc["option_type"] == "PE"]

cw = pw = None
if not ce.empty:
    near = ce[(ce["strike"] > spot) & (ce["strike"] <= spot + args.wall_prox)]
    if not near.empty:
        thr = float(ce["oi"].median()) * args.oi_mult
        act = near[near["oi"] > thr].sort_values("strike")
        if not act.empty: cw = int(act.iloc[0]["strike"])

if not pe.empty:
    near = pe[(pe["strike"] < spot) & (pe["strike"] >= spot - args.wall_prox)]
    if not near.empty:
        thr = float(pe["oi"].median()) * args.oi_mult
        act = near[near["oi"] > thr].sort_values("strike", ascending=False)
        if not act.empty: pw = int(act.iloc[0]["strike"])

# ── Max Pain ──────────────────────────────────────────────────────────────────
strikes = sorted(dc["strike"].unique())
pain = {s: (((s - ce["strike"]) * ce["oi"]).clip(lower=0).sum()
            + ((pe["strike"] - s) * pe["oi"]).clip(lower=0).sum())
        for s in strikes}
mp = min(pain, key=pain.get) if pain else atm

# ── PCR ───────────────────────────────────────────────────────────────────────
pcr = pcr_hist.get(str(ref_date), 1.0)

# ── Wall strength (% of total OI at wall) ────────────────────────────────────
def wall_strength(dc, wall, opt_type):
    if not wall: return 0
    wall_oi  = dc[(dc["strike"] == wall) & (dc["option_type"] == opt_type)]["oi"].sum()
    total_oi = dc[dc["option_type"] == opt_type]["oi"].sum()
    return wall_oi / total_oi if total_oi > 0 else 0

cw_str = wall_strength(dc, cw, "CE")
pw_str = wall_strength(dc, pw, "PE")

# ── Top 5 OI strikes ──────────────────────────────────────────────────────────
top_ce = ce.nlargest(5, "oi")[["strike", "oi"]].values.tolist()
top_pe = pe.nlargest(5, "oi")[["strike", "oi"]].values.tolist()

# ── Direction bias ────────────────────────────────────────────────────────────
mp_bias = "UP" if spot < mp else "DOWN" if spot > mp else "NEUTRAL"

# Simple directional signal: price vs max pain + CE/PE wall proximity
ce_dist = abs(spot - cw) if cw else 99999
pe_dist = abs(spot - pw) if pw else 99999

# Which wall is closer and in what direction?
if cw and (not pw or ce_dist <= pe_dist) and spot < cw:
    signal_dir = "CE"
    signal_wall = cw
    signal_dist = cw - spot
elif pw and spot > pw:
    signal_dir = "PE"
    signal_wall = pw
    signal_dist = spot - pw
else:
    signal_dir = "CE" if cw else "PE" if pw else None
    signal_wall = cw or pw
    signal_dist = ce_dist if signal_dir == "CE" else pe_dist

# ── Target strike and Greeks ──────────────────────────────────────────────────
def find_target_strike(spot, opt_type, T, sigma, target_prem):
    atm_ = int(round(spot / 50)) * 50
    best_stk, best_diff = atm_, float("inf")
    for k in range(-15, 16):
        stk = atm_ + k * 50
        if stk <= 0: continue
        try:
            px   = bs_price(spot, stk, T, RF, sigma, opt_type)
            diff = abs(px - target_prem)
            if diff < best_diff:
                best_diff = diff; best_stk = stk
        except: continue
    return best_stk, round(bs_price(spot, best_stk, T, RF, sigma, opt_type), 1)

stk_n = prem_n = delta_n = gamma_n = theta_n = None
if signal_dir:
    stk_n, prem_n = find_target_strike(spot, signal_dir, T_next, sigma, args.target_prem)
    gk = greeks(spot, stk_n, T_next, RF, sigma, signal_dir)
    delta_n = gk["delta"]; gamma_n = gk["gamma"]; theta_n = gk["theta"]

# ── IV/RV from 5m data ────────────────────────────────────────────────────────
iv_rv = None
F5M = F5M_A if F5M_A.exists() else (F5M_B if F5M_B.exists() else None)
if F5M:
    n5 = pd.read_csv(F5M, index_col=0, parse_dates=True)
    n5.index = pd.to_datetime(n5.index, utc=True).tz_localize(None)
    n5.columns = [c.capitalize() for c in n5.columns]
    day_bars = n5[n5.index.date == ref_date]
    if len(day_bars) >= args.rv_bars + 2:
        closes  = day_bars["Close"].astype(float).values
        log_ret = np.log(closes[1:] / closes[:-1])
        rv_w    = log_ret[-args.rv_bars:]
        RV      = float(np.std(rv_w, ddof=1)) * math.sqrt(252 * 75)
        iv_rv   = round(sigma / RV, 2) if RV > 0 else None

# ── SMA daily trend ───────────────────────────────────────────────────────────
sma_trend = None
if D1.exists():
    d1 = pd.read_csv(D1, index_col=0, parse_dates=True)
    d1.columns = [c.capitalize() for c in d1.columns]
    d1.index = pd.to_datetime(d1.index, utc=True).tz_localize(None)
    d1["_date"] = d1.index.date
    dg = d1.groupby("_date").last().sort_index()
    dg["sma20"] = dg["Close"].astype(float).rolling(20, min_periods=5).mean()
    last = dg[dg.index <= ref_date]
    if not last.empty:
        row = last.iloc[-1]
        sma_trend = "UP" if float(row["Close"]) > float(row["sma20"]) else "DOWN"

# ── Print report ──────────────────────────────────────────────────────────────
SEP = "=" * 60
sep = "-" * 60

print(f"\n{SEP}")
print(f"  MONDAY SIGNAL CHECK  |  ref date: {ref_date}")
print(f"{SEP}")
print(f"  Spot        : {spot:>8.0f}   ATM={atm}")
print(f"  Expiry      : {exp_str}   T_curr={T_curr*365:.0f}d  T_next={T_next*365:.0f}d")
print(f"  IV (ATM CE) : {sigma:.3f}  ({sigma*100:.1f}%)")
print(f"  IV/RV ratio : {iv_rv if iv_rv else 'n/a (no 5m data for today)'}")
print(f"  SMA20 trend : {sma_trend or 'n/a'}")
print(f"\n{sep}")
print(f"  OI WALLS")
print(f"  CE wall : {cw or 'none':>6}  ({cw_str*100:.1f}% of total CE OI)  dist={spot-cw if cw else 'n/a':.0f}pts" if cw else f"  CE wall : none")
print(f"  PE wall : {pw or 'none':>6}  ({pw_str*100:.1f}% of total PE OI)  dist={pw-spot if pw else 'n/a':.0f}pts" if pw else f"  PE wall : none")
print(f"  Max Pain: {mp:>6}  (spot {'ABOVE' if spot>mp else 'BELOW' if spot<mp else 'AT'} max pain by {abs(spot-mp):.0f}pts -> gravity {mp_bias})")
print(f"  PCR     : {pcr:.3f}")

print(f"\n{sep}")
print(f"  TOP OI STRIKES")
print(f"  CE (resistance) : {' | '.join(f'{int(s)}@{int(o):,}' for s,o in top_ce)}")
print(f"  PE (support)    : {' | '.join(f'{int(s)}@{int(o):,}' for s,o in top_pe)}")

print(f"\n{sep}")
print(f"  NEXT-EXPIRY TRADE SETUP  (target ~Rs{args.target_prem})")
if signal_dir and stk_n:
    print(f"  Direction  : {signal_dir}")
    print(f"  Wall target: {signal_wall}  (spot {signal_dist:.0f}pts away)")
    print(f"  Strike     : {stk_n}  (next expiry @ T={T_next*365:.0f}d)")
    print(f"  BS price   : Rs{prem_n:.1f}")
    print(f"  Delta      : {delta_n:.3f}  |delta|={abs(delta_n):.3f}")
    print(f"  Gamma      : {gamma_n:.5f}")
    print(f"  Theta/day  : Rs{theta_n:.2f}  ({abs(theta_n)/prem_n*100:.1f}% of premium)")
else:
    print(f"  No wall detected within {args.wall_prox}pts of spot.")

# ── GO / NO-GO ───────────────────────────────────────────────────────────────
print(f"\n{SEP}")
blocks = []
passes = []

if not signal_dir:
    blocks.append("NO OI WALL found")
else:
    if signal_dir == "PE" and pcr < args.pcr_hi_pe:
        blocks.append(f"PCR={pcr:.2f} < {args.pcr_hi_pe} (market is bullish-positioned, skip PE)")
    elif signal_dir == "CE" and pcr > args.pcr_lo_ce:
        blocks.append(f"PCR={pcr:.2f} > {args.pcr_lo_ce} (market is bearish-positioned, skip CE)")
    else:
        passes.append(f"PCR={pcr:.2f} OK for {signal_dir}")

    if stk_n and delta_n is not None:
        if abs(delta_n) < args.delta_min:
            blocks.append(f"|delta|={abs(delta_n):.2f} < {args.delta_min} (option too far OTM)")
        else:
            passes.append(f"|delta|={abs(delta_n):.2f} >= {args.delta_min} OK")

    if prem_n and (prem_n < 60 or prem_n > 200):
        blocks.append(f"Premium Rs{prem_n:.0f} outside Rs60-200 range")
    elif prem_n:
        passes.append(f"Premium Rs{prem_n:.0f} in range")

if blocks:
    print(f"  VERDICT: NO-GO")
    for b in blocks:
        print(f"    BLOCK: {b}")
else:
    lots = 3
    lot_size = 65
    entry_total = (prem_n or 0) * lots * lot_size
    sl_loss     = entry_total * 0.45
    tp_gain     = entry_total * 1.00
    print(f"  VERDICT: GO")
    for p_ in passes:
        print(f"    OK: {p_}")
    print(f"\n  TRADE DETAILS (3 lots):")
    print(f"    Buy  {signal_dir} {stk_n} @ ~Rs{prem_n:.0f}  (next expiry {exp_str[:10]} +7d)")
    print(f"    Capital needed : Rs{entry_total:,.0f}")
    print(f"    SL (45% drop)  : option hits Rs{(prem_n or 0)*0.55:.0f}  -> loss Rs{sl_loss:,.0f}")
    print(f"    TP (2x)        : option hits Rs{(prem_n or 0)*2:.0f}  -> gain Rs{tp_gain:,.0f}")
    print(f"    Exit by        : 15:20 (or SL/TP whichever first)")

print(f"{SEP}\n")
