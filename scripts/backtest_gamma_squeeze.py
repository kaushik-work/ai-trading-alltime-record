"""
Gamma Squeeze Backtest - Target-Premium Entry, Current vs Next Expiry.

Concept:
  Instead of buying at the OI wall strike (which may be deep OTM = Rs35 or
  already ITM = Rs400), buy the option whose premium is closest to Rs110.
  Compare current weekly expiry (T~1d on Monday) vs next weekly (T~8d).

  Key differences:
    Current expiry (T=1d): near-ATM strike, high gamma, binary by tomorrow.
    Next expiry   (T=8d): slightly OTM strike, lower gamma, more time buffer.
                          Theta hurts less on day-1. Better chance of avoiding SL.

Direction: OI wall signal. CE if NIFTY trending UP near CE wall.
           PE if NIFTY trending DOWN near PE wall.
           Uses 2-bar momentum confirmation (not 3-bar — earlier entry).

Exits: SL (option drops to sl_pct of entry), TP (option reaches tp_mult * entry),
       or EOD square-off at 15:20. No 11:30 cap.

One trade per day per expiry.

Run:
  python scripts/backtest_gamma_squeeze.py
  python scripts/backtest_gamma_squeeze.py --day 0 --target-prem 110
  python scripts/backtest_gamma_squeeze.py --day 0 --target-prem 150 --sl-pct 0.50
"""

import math
import sys
from datetime import date, time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.greeks import bs_price, days_to_expiry, implied_vol, greeks

import argparse
p = argparse.ArgumentParser()
p.add_argument("--lots",        type=int,   default=3)
p.add_argument("--slippage",    type=float, default=2.0)
p.add_argument("--sl-pct",      type=float, default=0.45)
p.add_argument("--tp-mult",     type=float, default=2.00)
p.add_argument("--target-prem", type=float, default=110,  help="target entry premium Rs")
p.add_argument("--prem-tol",    type=float, default=40,   help="accept strikes within +-Rs of target")
p.add_argument("--iv-rv-thr",   type=float, default=1.40, help="IV/RV threshold for early signal")
p.add_argument("--rv-bars",     type=int,   default=20,   help="rolling bars for RV")
p.add_argument("--wall-prox",   type=int,   default=500,  help="max pts from OI wall for direction")
p.add_argument("--oi-mult",     type=float, default=1.2)
p.add_argument("--day",         type=int,   default=0,    help="weekday: 0=Mon 3=Thu")
p.add_argument("--capital",     type=float, default=40_000)
# ── Quant filters (Greeks + OI) ───────────────────────────────────────────────
p.add_argument("--delta-min",   type=float, default=0.22, help="min |delta| at entry (quality gate)")
p.add_argument("--pcr-lo-ce",   type=float, default=1.40, help="CE entry: PCR must be <= this (skip CE when market is heavily put-biased)")
p.add_argument("--pcr-hi-pe",   type=float, default=0.80, help="PE entry: PCR must be >= this (skip PE when market is heavily call-biased)")
p.add_argument("--mp-align",    action="store_true",      help="require max-pain direction to align")
p.add_argument("--sma-trend",   action="store_true",      help="require SMA20 daily trend to align with direction")
p.add_argument("--sma-period",  type=int,   default=20,   help="SMA period for trend filter")
p.add_argument("--no-filters",  action="store_true",      help="disable all filters (baseline)")
args = p.parse_args()

LOT     = 65
QTY     = args.lots * LOT
SLIP    = args.slippage
RF      = 0.065
T_START = time(9, 30)
T_EXIT  = time(15, 20)
DAY_NAMES = {0:"Monday",1:"Tuesday",2:"Wednesday",3:"Thursday",4:"Friday"}

CHAIN = Path(__file__).parent.parent / "db" / "option_chain_history.csv"
F5M   = Path(__file__).parent.parent / "backtest_cache" / "NIFTY_5m_180d.csv"
PCR_F = Path(__file__).parent.parent / "db" / "pcr_historical.csv"
D1    = Path(__file__).parent.parent / "backtest_cache" / "NIFTY_1d.csv"

chain   = pd.read_csv(CHAIN)
chain["date"] = pd.to_datetime(chain["date"]).dt.date

pcr_hist = {}
if PCR_F.exists():
    _pdf = pd.read_csv(PCR_F, usecols=["date", "pcr_weekly"])
    pcr_hist = {r["date"]: float(r["pcr_weekly"]) for _, r in _pdf.iterrows()}

nifty5m = pd.read_csv(F5M, index_col=0, parse_dates=True)
nifty5m.index = pd.to_datetime(nifty5m.index, utc=True).tz_localize(None)
nifty5m.columns = [c.capitalize() for c in nifty5m.columns]

# Daily NIFTY with SMA for trend filter
nifty1d = pd.read_csv(D1, index_col=0, parse_dates=True)
nifty1d.columns = [c.capitalize() for c in nifty1d.columns]
nifty1d.index   = pd.to_datetime(nifty1d.index, utc=True).tz_localize(None)
nifty1d["_date"] = nifty1d.index.date
_d1g = nifty1d.groupby("_date").last().reset_index().sort_values("_date")
_d1g["sma"] = _d1g["Close"].astype(float).rolling(args.sma_period, min_periods=5).mean()
_d1g["trend"] = ["UP" if float(c) > float(s) else "DOWN"
                  for c, s in zip(_d1g["Close"], _d1g["sma"])]
trend_by_date = dict(zip(_d1g["_date"], _d1g["trend"]))

chain_days = set(chain["date"].unique())
nifty_days = set(nifty5m.index.date)
overlap    = sorted(chain_days & nifty_days)
trade_days = [d for d in overlap if d.weekday() == args.day]
day_label  = DAY_NAMES.get(args.day, f"day-{args.day}")

# ── ATM IV from bhavcopy ──────────────────────────────────────────────────────
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

iv_cache = {}
for d in sorted(chain["date"].unique()):
    dc = chain[chain["date"] == d]
    if dc.empty: continue
    iv_cache[d] = get_iv(dc, float(dc["spot"].median()), dc["expiry"].iloc[0], d)

# ── OI Wall direction ─────────────────────────────────────────────────────────
def find_walls(dc, spot):
    cw = pw = None
    ce = dc[dc["option_type"] == "CE"]
    pe = dc[dc["option_type"] == "PE"]
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
    return cw, pw

# ── Max Pain ─────────────────────────────────────────────────────────────────
def max_pain(dc):
    strikes = sorted(dc["strike"].unique())
    if not strikes:
        return 0
    ce = dc[dc["option_type"] == "CE"][["strike", "oi"]].copy()
    pe = dc[dc["option_type"] == "PE"][["strike", "oi"]].copy()
    pain = {s: (((s - ce["strike"]) * ce["oi"]).clip(lower=0).sum()
                + ((pe["strike"] - s) * pe["oi"]).clip(lower=0).sum())
            for s in strikes}
    return min(pain, key=pain.get)

# ── Find strike closest to target premium ─────────────────────────────────────
def find_target_strike(spot, opt_type, T, sigma, target_prem, step=50):
    if T <= 0:
        return int(round(spot / 50)) * 50, 0.0
    atm = int(round(spot / 50)) * 50
    best_strike = atm
    best_diff   = float("inf")
    for k in range(-15, 16):
        strike = atm + k * step
        if strike <= 0: continue
        try:
            px   = bs_price(spot, strike, T, RF, sigma, opt_type)
            diff = abs(px - target_prem)
            if diff < best_diff:
                best_diff   = diff
                best_strike = strike
        except:
            continue
    actual_px = bs_price(spot, best_strike, T, RF, sigma, opt_type)
    return best_strike, round(actual_px, 1)

# ── Simulate trade from entry bar ─────────────────────────────────────────────
def sim_trade(bars, entry_idx, strike, opt_type, T_base, sigma):
    entry_bar = bars.iloc[entry_idx]
    spot_e    = float(entry_bar["Close"])
    entry_p   = max(bs_price(spot_e, strike, max(T_base, 1/365), RF, sigma, opt_type) + SLIP, 1.0)
    entry_p   = round(entry_p, 1)
    sl_p      = entry_p * args.sl_pct
    tp_p      = entry_p * args.tp_mult

    for j in range(entry_idx + 1, len(bars)):
        bar      = bars.iloc[j]
        bar_time = bar.name.time()
        spot_c   = float(bar["Close"])
        curr_p   = bs_price(spot_c, strike, max(T_base, 1/365), RF, sigma, opt_type)

        if curr_p <= sl_p:
            return entry_p, round(max(curr_p - SLIP, 0.5), 1), "SL"
        if curr_p >= tp_p:
            return entry_p, round(curr_p + SLIP, 1), "TP"
        if bar_time >= T_EXIT:
            return entry_p, round(max(curr_p - SLIP, 0.5), 1), "EOD"

    last_p = bs_price(float(bars.iloc[-1]["Close"]), strike, max(T_base, 1/365), RF, sigma, opt_type)
    return entry_p, round(max(last_p - SLIP, 0.5), 1), "EOD"

# ── Main ──────────────────────────────────────────────────────────────────────
results_curr = []
results_next = []

print(f"\n{'='*90}")
print(f"  Gamma Squeeze | {day_label}s | Target premium Rs{args.target_prem}±{args.prem_tol}"
      f" | SL={args.sl_pct*100:.0f}% TP={args.tp_mult:.1f}x")
print(f"  Comparing: CURRENT expiry (T~1d) vs NEXT expiry (T~8d)")
print(f"{'='*90}")
print(f"  {'Date':<12}  {'D':<2}  {'CURRENT EXPIRY (T=1d)':^38}  {'NEXT EXPIRY (T=8d)':^38}")
print(f"  {'':12}  {'':2}  {'Stk | Entry | Exit | PnL | Rsn':^38}  {'Stk | Entry | Exit | PnL | Rsn':^38}")
print(f"  {'-'*88}")

for d in trade_days:
    dc = chain[chain["date"] == d]
    if dc.empty: continue

    spot    = float(dc["spot"].median())
    exp_str = dc["expiry"].iloc[0]
    exp_dt  = date.fromisoformat(exp_str)
    sigma   = iv_cache.get(d, 0.15)
    cw, pw  = find_walls(dc, spot)

    T_curr = max(days_to_expiry(exp_dt, today=d), 1/365)
    T_next = T_curr + 7/365  # simulate next weekly +7 calendar days

    day_bars = nifty5m[nifty5m.index.date == d].copy()
    if len(day_bars) < args.rv_bars + 3:
        continue

    closes  = day_bars["Close"].astype(float).values
    log_ret = np.log(closes[1:] / closes[:-1])

    # Build RV series across bars
    signal_fired = False
    c_row = n_row = None

    for i, (bar_ts, bar_row) in enumerate(day_bars.iterrows()):
        bt = bar_ts.time()
        if bt < T_START or bt >= T_EXIT: continue
        if i < args.rv_bars + 2: continue

        c    = float(bar_row["Close"])
        rv_w = log_ret[max(0, i - args.rv_bars): i]
        if len(rv_w) < 5: continue
        RV   = float(np.std(rv_w, ddof=1)) * math.sqrt(252 * 75)
        iv_rv = sigma / RV if RV > 0 else 0

        # 2-bar consecutive momentum (early signal)
        up2   = i >= 2 and closes[i] > closes[i-1] > closes[i-2]
        down2 = i >= 2 and closes[i] < closes[i-1] < closes[i-2]

        # Direction gate: must have OI wall and IV/RV >= threshold
        ce_ok = up2   and cw and abs(c - cw) <= args.wall_prox and iv_rv >= args.iv_rv_thr
        pe_ok = down2 and pw and abs(c - pw) <= args.wall_prox and iv_rv >= args.iv_rv_thr

        if not signal_fired and (ce_ok or pe_ok):
            opt_type = "CE" if ce_ok else "PE"
            signal_fired = True

            # ── Quant filters (Greeks + OI positioning) ───────────────────────
            skip_reason = None
            if not args.no_filters:
                pcr = pcr_hist.get(str(d), 1.0)
                mp  = max_pain(dc)

                # PCR alignment: don't fight institutional positioning
                if opt_type == "CE" and pcr > args.pcr_lo_ce:
                    skip_reason = f"PCR={pcr:.2f}>{args.pcr_lo_ce} (bearish OI, skip CE)"
                elif opt_type == "PE" and pcr < args.pcr_hi_pe:
                    skip_reason = f"PCR={pcr:.2f}<{args.pcr_hi_pe} (bullish OI, skip PE)"

                # Delta gate: need meaningful directional exposure at T=8d
                if skip_reason is None:
                    stk_test, _ = find_target_strike(c, opt_type, T_next, sigma, args.target_prem)
                    gk = greeks(c, stk_test, T_next, RF, sigma, opt_type)
                    if abs(gk["delta"]) < args.delta_min:
                        skip_reason = f"delta={gk['delta']:.3f} |{abs(gk['delta']):.2f}|<{args.delta_min}"

                # SMA daily trend alignment
                if skip_reason is None and args.sma_trend:
                    daily_trend = trend_by_date.get(d, "UP")
                    if opt_type == "CE" and daily_trend != "UP":
                        skip_reason = f"SMA{args.sma_period} trend=DOWN, skip CE"
                    elif opt_type == "PE" and daily_trend != "DOWN":
                        skip_reason = f"SMA{args.sma_period} trend=UP, skip PE"

                # Max pain alignment (optional)
                if skip_reason is None and args.mp_align:
                    mp_bias = "UP" if c < mp else "DOWN"
                    if (opt_type == "CE" and mp_bias != "UP") or (opt_type == "PE" and mp_bias != "DOWN"):
                        skip_reason = f"MaxPain={mp} bias={mp_bias}, skip {opt_type}"

            if skip_reason:
                skip_label = skip_reason[:36]
                print(f"  {d}  {opt_type:<2}  SKIP: {skip_label}")
                break

            # ── Find target-premium strike for each expiry ────────────────────
            stk_c, prem_c = find_target_strike(c, opt_type, T_curr, sigma, args.target_prem)
            stk_n, prem_n = find_target_strike(c, opt_type, T_next, sigma, args.target_prem)

            if abs(prem_c - args.target_prem) <= args.prem_tol:
                ep_c, xp_c, rsn_c = sim_trade(day_bars, i, stk_c, opt_type, T_curr, sigma)
                pnl_c = round((xp_c - ep_c) * QTY, 0)
                results_curr.append({
                    "date": str(d), "dir": opt_type, "strike": stk_c,
                    "ep": ep_c, "xp": xp_c, "pnl": pnl_c, "rsn": rsn_c, "iv_rv": round(iv_rv, 2)
                })
                c_row = (stk_c, ep_c, xp_c, pnl_c, rsn_c)

            if abs(prem_n - args.target_prem) <= args.prem_tol:
                ep_n, xp_n, rsn_n = sim_trade(day_bars, i, stk_n, opt_type, T_next, sigma)
                pnl_n = round((xp_n - ep_n) * QTY, 0)
                results_next.append({
                    "date": str(d), "dir": opt_type, "strike": stk_n,
                    "ep": ep_n, "xp": xp_n, "pnl": pnl_n, "rsn": rsn_n, "iv_rv": round(iv_rv, 2)
                })
                n_row = (stk_n, ep_n, xp_n, pnl_n, rsn_n)

            break

    # Print row
    dstr = f"{d}"
    opt_lbl = (results_curr[-1]["dir"] if c_row else (results_next[-1]["dir"] if n_row else "--"))
    c_str = (f"{c_row[0]:>5} Rs{c_row[1]:>5.0f}->Rs{c_row[2]:>5.0f} Rs{c_row[3]:>+7.0f} {c_row[4]}"
             if c_row else "  (no signal / out of range)      ")
    n_str = (f"{n_row[0]:>5} Rs{n_row[1]:>5.0f}->Rs{n_row[2]:>5.0f} Rs{n_row[3]:>+7.0f} {n_row[4]}"
             if n_row else "  (no signal / out of range)      ")
    print(f"  {dstr:<12}  {opt_lbl:<2}  {c_str}  {n_str}")

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*90}")
print(f"  RESULTS SUMMARY  |  {len(trade_days)} {day_label}s in data")
print(f"{'='*90}")

for label, rows in [("CURRENT EXPIRY (T=1d)", results_curr), ("NEXT EXPIRY (T=8d)", results_next)]:
    if not rows:
        print(f"\n  {label}: no trades")
        continue
    df   = pd.DataFrame(rows)
    wins = df[df["pnl"] > 0]
    loss = df[df["pnl"] <= 0]
    wr   = len(wins) / len(df) * 100
    gp   = wins["pnl"].sum() if len(wins) > 0 else 0
    gl   = abs(loss["pnl"].sum()) if len(loss) > 0 else 0
    pf   = round(gp / gl, 2) if gl > 0 else 99.9
    net  = df["pnl"].sum()
    avg_ep = df["ep"].mean()

    print(f"\n  {label}")
    print(f"    Trades   : {len(df)}  ({len(df[df['dir']=='CE'])} CE / {len(df[df['dir']=='PE'])} PE)")
    print(f"    Win rate : {wr:.1f}%  ({len(wins)} wins / {len(loss)} losses)")
    print(f"    PF       : {pf}")
    print(f"    Net PnL  : Rs{net:+,.0f}  on Rs{args.capital:,.0f}")
    print(f"    Avg entry: Rs{avg_ep:.1f}")
    if len(wins) > 0 and len(loss) > 0:
        print(f"    Avg win  : Rs{wins['pnl'].mean():+,.0f}  |  Avg loss: Rs{loss['pnl'].mean():+,.0f}")
    print(f"    By reason:")
    for rsn, grp in df.groupby("rsn"):
        wr_r = len(grp[grp["pnl"] > 0]) / len(grp) * 100
        print(f"      {rsn:<5}: {len(grp):>2} trades | Rs{grp['pnl'].sum():>+9,.0f} | WR={wr_r:.0f}%")
    print(f"    Avg IV/RV: {df['iv_rv'].mean():.2f}")

print(f"\n  Gamma payoff table (Rs{args.target_prem} entry, IV={0.12:.2f}, 300pt move to wall):")
print(f"  T(days)  entry    +100pt   +200pt   +300pt(ATM)   mult")
from core.greeks import bs_price as _bsp
spot_ref, wall_ref, sig_ref = 25800, 25500, 0.12
for T_d in [1, 3, 5, 8]:
    T = T_d / 365
    e = _bsp(spot_ref, wall_ref, T, RF, sig_ref, "PE")
    p100 = _bsp(spot_ref-100, wall_ref, T, RF, sig_ref, "PE")
    p200 = _bsp(spot_ref-200, wall_ref, T, RF, sig_ref, "PE")
    p300 = _bsp(spot_ref-300, wall_ref, T, RF, sig_ref, "PE")
    mult = p300/e if e > 0 else 0
    print(f"  T={T_d:>2}d   Rs{e:>5.1f}   Rs{p100:>5.1f}   Rs{p200:>5.1f}   Rs{p300:>5.1f}        {mult:.1f}x")
print()
