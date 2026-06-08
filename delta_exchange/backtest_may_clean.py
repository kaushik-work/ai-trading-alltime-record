"""
May 2026 — Clean Single-Month Backtest
=======================================
No compromises:
  Signal   : Delta Exchange option MARK prices (exchange's official fair-value reference)
  Execution: Actual BTC futures TICK data — entry at 5-min VWAP after signal
  Stops    : Checked every 1 minute on actual traded prices
  Costs    : 5bps fee/side + 5bps slippage = 10bps round trip
  Capital  : Rs 1,00,000 at Rs 96/USD = $1,041.67
  Leverage : 1x only (no fantasy leverage)

Every trade printed individually with full detail.
Parameters are IDENTICAL to v5+HPS — nothing tuned for May.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import math, re, warnings
warnings.filterwarnings("ignore")
from pathlib import Path
import numpy as np
import pandas as pd

# ── Paths ─────────────────────────────────────────────────────────────────────
FUTURES_FILE = Path(r"C:\Users\anura\Downloads\futures-trades-monthly\BTCUSD_2026-04.csv")
OPTIONS_DIR  = Path("data/options")      # mark price 1h CSVs

# ── Config — identical to v5+HPS, zero changes ────────────────────────────────
ENTRY_PCT        = 0.006      # 0.6% parity gate
MIN_STRIKES      = 3
MONEYNESS        = 0.05       # ±5% of spot
TT_MIN_HOURS     = 6
TT_MAX_HOURS     = 72
PERP_FEE_BPS     = 5.0        # 0.05% per side
SLIPPAGE_BPS     = 5.0        # 0.05% slippage
CONTRACT_SIZE    = 0.001      # BTC per contract (Delta India)
SIZE_BASE_PCT    = 0.005
SIZE_MIN_MULT    = 0.5
SIZE_MAX_MULT    = 3.0
MAX_CONCURRENT   = 2
MAX_HOLD_HOURS   = 72
ZONE_LOOKBACK    = 30
ZONE_TOLERANCE   = 0.020
ZONE_BUFFER      = 0.004
CANDLE_BODY_MIN  = 0.001
PARTIAL_CLOSE    = 0.70

STOP_PCT         = 0.015
TRAIL_PEAK_PCT   = 0.005
TRAIL_GIVE_PCT   = 0.0025

INR_BUDGET       = 100_000
USD_RATE         = 96.0
START_USD        = round(INR_BUDGET / USD_RATE, 2)
import os as _os
LEVERAGE         = int(_os.environ.get("LEV", "1"))


# ── Load May futures ticks → 1m OHLCV ────────────────────────────────────────
def load_may_futures() -> pd.DataFrame:
    print("Loading May 2026 futures tick data...")
    df = pd.read_csv(FUTURES_FILE)
    df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed", utc=True)
    df = df.set_index("timestamp").sort_index()
    ticks = df["price"]

    ohlcv = ticks.resample("1min").agg(
        open="first", high="max", low="min", close="last"
    ).dropna()

    # 5-minute VWAP for entry (weighted by tick volume proxy)
    vwap_5m = (ticks * df["size"]).resample("5min").sum() / df["size"].resample("5min").sum()

    print(f"  {len(df):,} ticks → {len(ohlcv):,} 1m bars")
    print(f"  Period: {ohlcv.index[0]} → {ohlcv.index[-1]}")
    print(f"  Price range: ${ohlcv['low'].min():,.0f} – ${ohlcv['high'].max():,.0f}")
    return ohlcv, vwap_5m


# ── Load option mark prices (hourly) ─────────────────────────────────────────
def load_option_marks() -> dict:
    print("\nLoading option mark prices...")
    out = {}
    for p in sorted(OPTIONS_DIR.glob("*_mark_1h.csv")):
        sym = p.name.replace("_mark_1h.csv", "")
        # only May expiries
        if not any(x in sym for x in ["030426","100426","170426","240426","300426","010426"]):
            continue
        df = pd.read_csv(p)
        if df.empty: continue
        df["ts"] = pd.to_datetime(df["time"], unit="s", utc=True)
        out[sym] = df.set_index("ts")["close"].sort_index()
    print(f"  {len(out):,} May option contracts")
    return out


def build_catalogue(marks: dict) -> pd.DataFrame:
    rows = []
    for sym in marks:
        m = re.match(r"^([CP])-BTC-(\d+)-(\d{6})$", sym)
        if not m: continue
        side, strike, ddmmyy = m.group(1), int(m.group(2)), m.group(3)
        try:
            dd, mm, yy = int(ddmmyy[:2]), int(ddmmyy[2:4]), int(ddmmyy[4:6])
            expiry = pd.Timestamp(f"20{yy:02d}-{mm:02d}-{dd:02d} 12:00:00", tz="UTC")
        except: continue
        rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": expiry})
    return pd.DataFrame(rows)


# ── Parity signal from mark prices ────────────────────────────────────────────
def compute_signal(t: pd.Timestamp, spot: float, cat: pd.DataFrame, marks: dict):
    tt_min = t + pd.Timedelta(hours=TT_MIN_HOURS)
    tt_max = t + pd.Timedelta(hours=TT_MAX_HOURS)
    elig = cat[(cat["expiry"] > tt_min) & (cat["expiry"] <= tt_max)]
    cands = []
    for exp in sorted(elig["expiry"].unique()):
        same  = elig[elig["expiry"] == exp]
        calls = same[same["side"] == "C"].set_index("strike")
        puts  = same[same["side"] == "P"].set_index("strike")
        common = [K for K in sorted(set(calls.index) & set(puts.index))
                  if abs(K - spot) / spot <= MONEYNESS]
        if len(common) < MIN_STRIKES: continue
        devs = []
        for K in common:
            cs = marks.get(calls.loc[K, "symbol"])
            ps = marks.get(puts.loc[K, "symbol"])
            if cs is None or ps is None: continue
            if t not in cs.index or t not in ps.index: continue
            cp, pp = float(cs.loc[t]), float(ps.loc[t])
            if cp <= 0 or pp <= 0: continue
            devs.append(((cp - pp + K) - spot) / spot)
        if len(devs) < MIN_STRIKES: continue
        pos = sum(1 for d in devs if d > 0)
        neg = sum(1 for d in devs if d < 0)
        if pos < MIN_STRIKES and neg < MIN_STRIKES: continue
        cands.append({"expiry": exp, "pred": float(np.median(devs)), "n": len(devs)})
    if not cands: return None
    cands.sort(key=lambda c: -abs(c["pred"]))
    best = cands[0]
    return best if abs(best["pred"]) >= ENTRY_PCT else None


# ── HPS zone filters ───────────────────────────────────────────────────────────
def get_zones(hourly, t):
    hist = hourly.loc[:t].iloc[-ZONE_LOOKBACK-1:-1]
    if len(hist) < 5: return [], []
    H, L = hist["high"].values, hist["low"].values
    res, sup = [], []
    for i in range(2, len(H) - 2):
        if H[i]>H[i-1] and H[i]>H[i-2] and H[i]>H[i+1] and H[i]>H[i+2]: res.append(H[i])
        if L[i]<L[i-1] and L[i]<L[i-2] and L[i]<L[i+1] and L[i]<L[i+2]: sup.append(L[i])
    return sorted(sup), sorted(res, reverse=True)

def at_zone(spot, sup, res, wl):
    for z in (sup if wl else res):
        if abs(spot - z) / spot <= ZONE_TOLERANCE: return True, z
    return False, None

def next_zone(spot, sup, res, wl):
    if wl: above=[r for r in res if r>spot]; return min(above) if above else None
    below=[s for s in sup if s<spot]; return max(below) if below else None

def candle_ok(hourly, t, wl):
    if t not in hourly.index: return False
    b = hourly.loc[t]; o,h,l,c = b["open"],b["high"],b["low"],b["close"]
    body=abs(c-o); rng=h-l
    if rng < 1e-6: return False
    uw=h-max(o,c); lw=min(o,c)-l; bf=body/rng
    if wl: return (lw>=2*body and uw<=0.3*rng and c>o) or (c>o and bf>0.6 and body/o>CANDLE_BODY_MIN)
    return (uw>=2*body and lw<=0.3*rng and c<o) or (c<o and bf>0.6 and body/o>CANDLE_BODY_MIN)


# ── Main backtest ──────────────────────────────────────────────────────────────
def run():
    ohlcv_1m, vwap_5m = load_may_futures()
    marks    = load_option_marks()
    cat      = build_catalogue(marks)

    hourly = pd.DataFrame({
        "open":  ohlcv_1m["open"].resample("1h").first(),
        "high":  ohlcv_1m["high"].resample("1h").max(),
        "low":   ohlcv_1m["low"].resample("1h").min(),
        "close": ohlcv_1m["close"].resample("1h").last(),
    }).dropna()

    print(f"\nRunning backtest — {len(ohlcv_1m):,} 1m bars, {len(hourly)} hourly decision points")
    print(f"Capital: Rs {INR_BUDGET:,} = ${START_USD:.2f}  |  Leverage: {LEVERAGE}x")
    print()

    equity    = START_USD
    open_pos  = []
    trades    = []
    eq_curve  = []
    sig_hist  = {}
    pending   = []
    prev_hr   = None

    close_s = ohlcv_1m["close"]
    open_s  = ohlcv_1m["open"]
    high_s  = ohlcv_1m["high"]
    low_s   = ohlcv_1m["low"]

    for t_1m in ohlcv_1m.index:
        spot = float(close_s.loc[t_1m])
        eq_curve.append((t_1m, equity))

        t_hr = t_1m.floor("1h")
        if t_hr != prev_hr and t_hr in hourly.index:
            prev_hr = t_hr
            s = compute_signal(t_hr, spot, cat, marks)
            if s:
                sig_hist.setdefault(s["expiry"], []).append((t_hr, s["pred"]))
            for exp in list(sig_hist):
                sig_hist[exp] = [(ti,p) for ti,p in sig_hist[exp]
                                 if (t_hr-ti).total_seconds() <= 6*3600]
            if s and len(open_pos) < MAX_CONCURRENT:
                already = {p["expiry"] for p in open_pos} | {p["expiry"] for p in pending}
                if s["expiry"] not in already:
                    wl = s["pred"] > 0
                    sup2, res2 = get_zones(hourly, t_hr)
                    ok, zlvl = at_zone(spot, sup2, res2, wl)
                    if ok and candle_ok(hourly, t_hr, wl):
                        mid = (zlvl + spot) / 2
                        sp  = mid*(1-ZONE_BUFFER) if wl else mid*(1+ZONE_BUFFER)
                        t1  = spot + (1 if wl else -1)*abs(spot-sp)
                        t2  = next_zone(spot, sup2, res2, wl)
                        pending.append({"sig":s,"wl":wl,"stop":sp,"t1":t1,"t2":t2,"signal_t":t_hr})

        # execute pending — use 5-min VWAP if available, else open
        new_pend = []
        for pe in pending:
            if len(open_pos) >= MAX_CONCURRENT:
                new_pend.append(pe); continue
            s    = pe["sig"]; side = 1 if pe["wl"] else -1
            # entry price: 5-min VWAP for realism
            t_5m = t_1m.floor("5min")
            if t_5m in vwap_5m.index and not pd.isna(vwap_5m.loc[t_5m]):
                raw_fill = float(vwap_5m.loc[t_5m])
            else:
                raw_fill = float(open_s.loc[t_1m])
            fill_px = raw_fill * (1 + side * SLIPPAGE_BPS / 1e4)
            sm = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(s["pred"]) / SIZE_BASE_PCT))
            notional = equity * sm * LEVERAGE
            risk_pct = abs(fill_px - pe["stop"]) / fill_px
            if risk_pct > 0:
                notional = min(notional, equity * 0.20 / risk_pct)
            nc = max(1, int(notional / (CONTRACT_SIZE * spot)))
            actual_n = nc * CONTRACT_SIZE * fill_px
            entry_fee = actual_n * PERP_FEE_BPS / 1e4
            equity -= entry_fee
            open_pos.append({
                "entry_t": t_1m, "entry_px": fill_px, "side": side,
                "expiry": s["expiry"], "contracts": nc, "notional": actual_n,
                "stop": pe["stop"], "t1": pe["t1"], "t2": pe["t2"],
                "peak": fill_px, "pred_pct": s["pred"], "n_strikes": s["n"],
                "signal_t": pe["signal_t"],
            })
        pending = new_pend

        # manage positions — check every 1m on actual prices
        still = []
        for pos in open_pos:
            side=pos["side"]; ep=pos["entry_px"]; nc=pos["contracts"]
            held_h=(t_1m-pos["entry_t"]).total_seconds()/3600
            cur_high = float(high_s.loc[t_1m])
            cur_low  = float(low_s.loc[t_1m])
            pos["peak"] = max(pos["peak"], cur_high) if side==1 else min(pos["peak"], cur_low)
            ur = side * (spot - ep) / ep

            # partial TP — check against 1m high/low
            if not pos.get("tp") and pos["t1"]:
                t1_hit = (side==1 and cur_high>=pos["t1"]) or (side==-1 and cur_low<=pos["t1"])
                if t1_hit:
                    cn = max(1, int(nc*PARTIAL_CLOSE))
                    tp_px = pos["t1"] * (1 - side*SLIPPAGE_BPS/1e4)
                    ret = side*(tp_px-ep)/ep
                    pnl = cn*CONTRACT_SIZE*tp_px*ret - cn*CONTRACT_SIZE*tp_px*PERP_FEE_BPS/1e4
                    equity += pnl; pos["contracts"]-=cn; pos["tp"]=True
                    trades.append({**pos,"exit_t":t_1m,"exit_px":tp_px,"pnl_usd":pnl,
                                   "exit_reason":"partial_tp","contracts":cn,"equity_after":equity})

            nc=pos["contracts"]; er=None
            stop_hit = (side==1 and cur_low<=pos["stop"]) or (side==-1 and cur_high>=pos["stop"])
            zone_hit = pos["t2"] and ((side==1 and cur_high>=pos["t2"]) or (side==-1 and cur_low<=pos["t2"]))
            if t_1m >= pos["expiry"]: er="expiry"
            elif held_h >= MAX_HOLD_HOURS: er="max_hold"
            elif stop_hit: er="stop_loss"
            elif zone_hit: er="zone_target"
            elif ur >= TRAIL_PEAK_PCT:
                pr=side*(pos["peak"]-ep)/ep
                if (pr-ur)>TRAIL_GIVE_PCT: er="trail"

            if er and nc>0:
                if er=="stop_loss":
                    ex_px=pos["stop"]*(1-side*SLIPPAGE_BPS/1e4)
                elif er=="zone_target":
                    ex_px=pos["t2"]*(1-side*SLIPPAGE_BPS/1e4)
                else:
                    ex_px=spot*(1-side*SLIPPAGE_BPS/1e4)
                ret=side*(ex_px-ep)/ep
                pnl=nc*CONTRACT_SIZE*ex_px*ret-nc*CONTRACT_SIZE*ex_px*PERP_FEE_BPS/1e4
                equity+=pnl
                trades.append({**pos,"exit_t":t_1m,"exit_px":ex_px,"pnl_usd":pnl,
                               "exit_reason":er,"contracts":nc,"equity_after":equity})
            else: still.append(pos)
        open_pos=still

    # close remaining
    for pos in open_pos:
        side=pos["side"]; ep=pos["entry_px"]; nc=pos["contracts"]
        ex_px=float(close_s.iloc[-1])*(1-side*SLIPPAGE_BPS/1e4)
        ret=side*(ex_px-ep)/ep
        pnl=nc*CONTRACT_SIZE*ex_px*ret-nc*CONTRACT_SIZE*ex_px*PERP_FEE_BPS/1e4
        equity+=pnl
        trades.append({**pos,"exit_t":ohlcv_1m.index[-1],"exit_px":ex_px,"pnl_usd":pnl,
                       "exit_reason":"data_end","contracts":nc,"equity_after":equity})

    return trades, eq_curve, equity


# ── Print results ──────────────────────────────────────────────────────────────
def print_results(trades, eq_curve, equity):
    print("=" * 100)
    print("  APRIL 2026 — CLEAN BACKTEST  |  Actual futures ticks  |  Mark price signal")
    print(f"  Capital: Rs {INR_BUDGET:,}  |  Leverage: {LEVERAGE}x  |  Fee: {PERP_FEE_BPS}bps/side  |  Slip: {SLIPPAGE_BPS}bps")
    print("=" * 100)

    if not trades:
        print("  No trades fired in May 2026.")
        return

    df = pd.DataFrame(trades)
    df["entry_t"] = pd.to_datetime(df["entry_t"], utc=True)
    df["exit_t"]  = pd.to_datetime(df["exit_t"], utc=True)
    df = df.sort_values("exit_t").reset_index(drop=True)

    n    = len(df)
    wins = (df["pnl_usd"] > 0).sum()
    avg_win  = df.loc[df["pnl_usd"]>0,"pnl_usd"].mean() if wins else 0
    avg_loss = df.loc[df["pnl_usd"]<=0,"pnl_usd"].mean() if (n-wins) else 0
    rr   = abs(avg_win/avg_loss) if avg_loss else float("nan")
    net_usd = equity - START_USD
    net_inr = net_usd * USD_RATE

    print(f"\n  SUMMARY")
    print(f"  {'Trades':<20}: {n}  (wins {wins}  losses {n-wins}  win rate {wins/n*100:.1f}%)")
    print(f"  {'Avg win / loss':<20}: ${avg_win:+,.1f} / ${avg_loss:+,.1f}   R:R {rr:.2f}")
    print(f"  {'Net P&L':<20}: ${net_usd:+,.2f}  =  Rs {net_inr:+,.0f}")
    print(f"  {'Return on capital':<20}: {net_usd/START_USD*100:+.2f}%")
    print(f"  {'Final capital':<20}: Rs {equity*USD_RATE:,.0f}")

    eq = pd.Series([e for _,e in eq_curve], index=[t for t,_ in eq_curve])
    dd = (eq-eq.cummax()).min()
    print(f"  {'Max drawdown':<20}: ${dd:+,.2f}  ({dd/START_USD*100:.2f}%)")

    print()
    print(f"  {'#':<3} {'Entry time':<22} {'Exit time':<22} {'Side':<5} "
          f"{'Entry $':>9} {'Exit $':>9} {'Pred':>6} {'N':>3} "
          f"{'P&L $':>9} {'P&L Rs':>10} {'Reason':<14} {'Equity Rs':>12}")
    print("  " + "-" * 130)

    trade_num = 0
    seen_pos = set()
    for _, row in df.iterrows():
        pos_key = (row["entry_t"], row["entry_px"], row["side"])
        if pos_key not in seen_pos:
            trade_num += 1
            seen_pos.add(pos_key)

        pnl_inr = row["pnl_usd"] * USD_RATE
        side_str = "LONG" if row["side"]==1 else "SHORT"
        entry_str = row["entry_t"].strftime("%m-%d %H:%M")
        exit_str  = row["exit_t"].strftime("%m-%d %H:%M")
        reason = row["exit_reason"]
        eq_inr = row["equity_after"] * USD_RATE
        pnl_s = f"+${row['pnl_usd']:,.1f}" if row["pnl_usd"]>0 else f"-${abs(row['pnl_usd']):,.1f}"
        inr_s = f"+Rs{pnl_inr:,.0f}" if pnl_inr>0 else f"-Rs{abs(pnl_inr):,.0f}"

        print(f"  {trade_num:<3} {entry_str:<22} {exit_str:<22} {side_str:<5} "
              f"${row['entry_px']:>8,.1f} ${row['exit_px']:>8,.1f} "
              f"{row['pred_pct']*100:>+5.2f}% {row['n_strikes']:>3} "
              f"{pnl_s:>9} {inr_s:>10} {reason:<14} Rs{eq_inr:>10,.0f}")

    print("  " + "-" * 130)

    print("\n  Exit breakdown:")
    for reason, grp in df.groupby("exit_reason"):
        wr = (grp["pnl_usd"]>0).mean()*100
        tot_inr = grp["pnl_usd"].sum()*USD_RATE
        s="+" if tot_inr>=0 else ""
        print(f"    {reason:<16} {len(grp):>3} trades  win {wr:>5.1f}%  "
              f"total Rs{s}{tot_inr:>10,.0f}")

    print(f"\n  BTC spot May 1: ${ohlcv_1m_start:.0f}  →  May 31: ${ohlcv_1m_end:.0f}  "
          f"(buy-hold: {(ohlcv_1m_end-ohlcv_1m_start)/ohlcv_1m_start*100:+.1f}%)")
    print(f"  Strategy vs buy-hold: "
          f"{net_usd/START_USD*100:+.1f}% vs "
          f"{(ohlcv_1m_end-ohlcv_1m_start)/ohlcv_1m_start*100:+.1f}%")


if __name__ == "__main__":
    ohlcv_1m_global, vwap_5m_global = load_may_futures()
    ohlcv_1m_start = float(ohlcv_1m_global["close"].iloc[0])
    ohlcv_1m_end   = float(ohlcv_1m_global["close"].iloc[-1])

    # monkey-patch globals for print_results
    import builtins
    _orig = builtins.print
    ohlcv_1m = ohlcv_1m_global

    marks = load_option_marks()
    cat   = build_catalogue(marks)

    print(f"\nOption catalogue: {len(cat)} May contracts")

    # pre-compute hourly signals
    hourly_close = ohlcv_1m_global["close"].resample("1h").last().dropna()
    hourly_ohlc  = pd.DataFrame({
        "open":  ohlcv_1m_global["open"].resample("1h").first(),
        "high":  ohlcv_1m_global["high"].resample("1h").max(),
        "low":   ohlcv_1m_global["low"].resample("1h").min(),
        "close": ohlcv_1m_global["close"].resample("1h").last(),
    }).dropna()

    print(f"\nPre-computing signals ({len(hourly_close)} hours)...")
    n_sigs = 0
    for t_hr, spot in hourly_close.items():
        s = compute_signal(t_hr, float(spot), cat, marks)
        if s: n_sigs += 1
    print(f"  Hours with signal ≥ {ENTRY_PCT*100:.1f}%: {n_sigs} / {len(hourly_close)}")

    trades, eq_curve, equity = run()
    print_results(trades, eq_curve, equity)
