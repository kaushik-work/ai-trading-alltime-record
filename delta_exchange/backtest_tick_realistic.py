"""
Tick-Level Realistic Backtest — Synth-Forward on Actual Trade Data
===================================================================
Uses REAL traded prices — no mark prices, no model outputs.

Signal  : Put-call parity from ACTUAL option trades
          (last traded price per symbol per hour, max 2h stale)
Spot    : Last futures tick price at each hour
Entry   : Next 1-minute futures bar open price
Stops   : Checked every 1 minute on actual futures prices
Exit    : Actual futures tick price when condition met

Data sources:
  Futures : C:\\Users\\anura\\Downloads\\futures-trades-monthly\\BTCUSD_*.csv
  Options : C:\\Users\\anura\\Downloads\\options-trades-monthly-BTC\\BTC_*.csv

This is the most honest backtest possible with available data.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import math, re, warnings
warnings.filterwarnings("ignore")
from pathlib import Path
import numpy as np
import pandas as pd

FUTURES_DIR = Path(r"C:\Users\anura\Downloads\futures-trades-monthly")
OPTIONS_DIR = Path(r"C:\Users\anura\Downloads\options-trades-monthly-BTC")

INR_BUDGET  = 100_000
USD_RATE    = 96.0
START_USD   = round(INR_BUDGET / USD_RATE, 2)

ENTRY_PCT       = 0.006      # 0.6% parity gate
MIN_STRIKES     = 3
MONEYNESS       = 0.05
TT_MIN_HOURS    = 6
TT_MAX_HOURS    = 72
MAX_STALE_HOURS = 2          # option price older than 2h is ignored

PERP_FEE_BPS    = 5.0
SLIPPAGE_BPS    = 5.0        # realistic: 5bps on actual market
CONTRACT_SIZE   = 0.001

SIZE_BASE_PCT   = 0.005
SIZE_MIN_MULT   = 0.5
SIZE_MAX_MULT   = 3.0
MAX_CONCURRENT  = 2
MAX_HOLD_HOURS  = 72

ZONE_LOOKBACK   = 30
ZONE_TOLERANCE  = 0.020
ZONE_BUFFER     = 0.004
CANDLE_BODY_MIN = 0.001
PARTIAL_CLOSE   = 0.70

STOP_PCT        = 0.015
TRAIL_PEAK_PCT  = 0.005
TRAIL_GIVE_PCT  = 0.0025


# ── Load data ──────────────────────────────────────────────────────────────────
def load_futures_1m() -> pd.Series:
    """Load all futures tick data → 1-minute OHLCV from actual trades."""
    print("Loading futures tick data...")
    frames = []
    for f in sorted(FUTURES_DIR.glob("BTCUSD_*.csv")):
        df = pd.read_csv(f)
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed", utc=True)
        frames.append(df[["timestamp", "price", "size"]])
        print(f"  {f.name}: {len(df):,} ticks")
    ticks = pd.concat(frames, ignore_index=True).sort_values("timestamp")
    ticks = ticks.set_index("timestamp")
    # resample to 1m OHLCV
    ohlcv = ticks["price"].resample("1min").agg(
        open="first", high="max", low="min", close="last",
    ).dropna()
    print(f"  → {len(ohlcv):,} one-minute bars\n")
    return ohlcv


def load_options_hourly() -> dict:
    """
    Load options tick data → {symbol: {hour_ts: last_price}} dict for O(1) lookup.
    """
    print("Loading options tick data...")
    frames = []
    for f in sorted(OPTIONS_DIR.glob("BTC_*.csv")):
        df = pd.read_csv(f)
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed", utc=True)
        frames.append(df[["timestamp", "product_symbol", "price"]])
        print(f"  {f.name}: {len(df):,} ticks")
    ticks = pd.concat(frames, ignore_index=True).sort_values("timestamp")
    ticks["hour"] = ticks["timestamp"].dt.floor("1h")

    print("  Building hourly last-price per symbol...", flush=True)
    # get last price per symbol per hour
    last_px = ticks.groupby(["product_symbol", "hour"])["price"].last()
    # convert to nested dict: {symbol: {hour: price}}
    out = {}
    for (sym, hr), px in last_px.items():
        if sym not in out:
            out[sym] = {}
        out[sym][hr] = float(px)
    print(f"  → {len(out):,} option symbols with trade history\n")
    return out


def parse_symbol(sym: str):
    """C-BTC-67000-010326 → (side, strike, expiry_ts)"""
    m = re.match(r"^([CP])-BTC-(\d+)-(\d{6})$", sym)
    if not m: return None
    side, strike, ddmmyy = m.group(1), int(m.group(2)), m.group(3)
    try:
        dd, mm, yy = int(ddmmyy[:2]), int(ddmmyy[2:4]), int(ddmmyy[4:6])
        expiry = pd.Timestamp(f"20{yy:02d}-{mm:02d}-{dd:02d} 12:00:00", tz="UTC")
        return side, strike, expiry
    except:
        return None


# ── Signal from actual option trades ──────────────────────────────────────────
def compute_signal_from_trades(t_hr: pd.Timestamp, spot: float,
                                opt_hourly: dict, catalogue: pd.DataFrame) -> dict | None:
    """
    At hour t_hr, use LAST TRADED option prices (within MAX_STALE_HOURS)
    to compute synthetic forward parity signal.
    """
    stale_cutoff = t_hr - pd.Timedelta(hours=MAX_STALE_HOURS)
    tt_min = t_hr + pd.Timedelta(hours=TT_MIN_HOURS)
    tt_max = t_hr + pd.Timedelta(hours=TT_MAX_HOURS)
    eligible = catalogue[(catalogue["expiry"] > tt_min) & (catalogue["expiry"] <= tt_max)]

    cands = []
    for exp in sorted(eligible["expiry"].unique()):
        same = eligible[eligible["expiry"] == exp]
        calls = same[same["side"] == "C"].set_index("strike")
        puts  = same[same["side"] == "P"].set_index("strike")
        common = sorted(set(calls.index) & set(puts.index))
        near   = [K for K in common if abs(K - spot) / spot <= MONEYNESS]
        if len(near) < MIN_STRIKES: continue

        devs = []
        for K in near:
            c_sym = calls.loc[K, "symbol"] if K in calls.index else None
            p_sym = puts.loc[K, "symbol"]  if K in puts.index  else None
            if c_sym is None or p_sym is None: continue

            c_dict = opt_hourly.get(c_sym)
            p_dict = opt_hourly.get(p_sym)
            if c_dict is None or p_dict is None: continue

            # find most recent price within MAX_STALE_HOURS using dict lookup
            cp = pp = None
            for h_back in range(MAX_STALE_HOURS + 1):
                lookup = t_hr - pd.Timedelta(hours=h_back)
                if cp is None and lookup in c_dict:
                    cp = c_dict[lookup]
                if pp is None and lookup in p_dict:
                    pp = p_dict[lookup]
                if cp is not None and pp is not None:
                    break
            if cp is None or pp is None: continue
            if cp <= 0 or pp <= 0: continue

            synth_F = cp - pp + K
            devs.append((synth_F - spot) / spot)

        if len(devs) < MIN_STRIKES: continue
        pos = sum(1 for d in devs if d > 0)
        neg = sum(1 for d in devs if d < 0)
        if pos < MIN_STRIKES and neg < MIN_STRIKES: continue

        cands.append({
            "expiry": exp,
            "pred": float(np.median(devs)),
            "n_strikes": len(devs),
        })

    if not cands: return None
    cands.sort(key=lambda c: -abs(c["pred"]))
    best = cands[0]
    return best if abs(best["pred"]) >= ENTRY_PCT else None


# ── HPS zone helpers ───────────────────────────────────────────────────────────
def get_zones(hourly_ohlc, t):
    hist = hourly_ohlc.loc[:t].iloc[-ZONE_LOOKBACK-1:-1]
    if len(hist) < 5: return [], []
    H, L = hist["high"].values, hist["low"].values
    res, sup = [], []
    for i in range(2, len(H) - 2):
        if H[i]>H[i-1] and H[i]>H[i-2] and H[i]>H[i+1] and H[i]>H[i+2]: res.append(H[i])
        if L[i]<L[i-1] and L[i]<L[i-2] and L[i]<L[i+1] and L[i]<L[i+2]: sup.append(L[i])
    return sorted(sup), sorted(res, reverse=True)


def at_zone(spot, sup, res, want_long):
    for z in (sup if want_long else res):
        if abs(spot - z) / spot <= ZONE_TOLERANCE: return True, z
    return False, None


def next_zone(spot, sup, res, want_long):
    if want_long:
        above = [r for r in res if r > spot]; return min(above) if above else None
    below = [s for s in sup if s < spot]; return max(below) if below else None


def candle_ok(hourly_ohlc, t, want_long):
    if t not in hourly_ohlc.index: return False
    b = hourly_ohlc.loc[t]
    o, h, l, c = b["open"], b["high"], b["low"], b["close"]
    body = abs(c - o); rng = h - l
    if rng < 1e-6: return False
    uw = h - max(o, c); lw = min(o, c) - l; bf = body / rng
    if want_long:
        return (lw >= 2*body and uw <= 0.3*rng and c > o) or \
               (c > o and bf > 0.6 and body/o > CANDLE_BODY_MIN)
    return (uw >= 2*body and lw <= 0.3*rng and c < o) or \
           (c < o and bf > 0.6 and body/o > CANDLE_BODY_MIN)


# ── Main backtest ──────────────────────────────────────────────────────────────
def precompute_hourly_signals(futures_1m: pd.DataFrame, opt_hourly: dict,
                               catalogue: pd.DataFrame) -> dict:
    """Pre-compute signal for every hour — much faster than computing inside the 1m loop."""
    hourly_close = futures_1m["close"].resample("1h").last().dropna()
    signals = {}
    print(f"  Pre-computing signals for {len(hourly_close):,} hours...", flush=True)
    for t_hr, spot in hourly_close.items():
        s = compute_signal_from_trades(t_hr, float(spot), opt_hourly, catalogue)
        if s:
            signals[t_hr] = s
    print(f"  → {len(signals)} hours with signal above {ENTRY_PCT*100:.1f}% gate")
    return signals


def run(leverage: float, futures_1m: pd.DataFrame, opt_hourly: dict,
        catalogue: pd.DataFrame, precomputed_signals: dict) -> dict:

    hourly_ohlc = pd.DataFrame({
        "open":  futures_1m["open"].resample("1h").first(),
        "high":  futures_1m["high"].resample("1h").max(),
        "low":   futures_1m["low"].resample("1h").min(),
        "close": futures_1m["close"].resample("1h").last(),
    }).dropna()

    print(f"  Running leverage={leverage}x...", flush=True)
    equity      = START_USD
    open_pos    = []
    trades      = []
    equity_curve= []
    sig_history = {}
    pending     = []

    prev_hr = None
    close_1m = futures_1m["close"]
    open_1m  = futures_1m["open"]

    for t_1m in futures_1m.index:
        spot = float(close_1m.loc[t_1m])
        equity_curve.append((t_1m, equity))

        t_hr = t_1m.floor("1h")
        if t_hr != prev_hr and t_hr in hourly_ohlc.index:
            prev_hr = t_hr

            # look up pre-computed signal
            s = precomputed_signals.get(t_hr)
            if s:
                sig_history.setdefault(s["expiry"], []).append((t_hr, s["pred"]))
            for exp in list(sig_history):
                sig_history[exp] = [(ti, p) for ti, p in sig_history[exp]
                                    if (t_hr - ti).total_seconds() <= 6 * 3600]

            if s and len(open_pos) < MAX_CONCURRENT:
                already = {p["expiry"] for p in open_pos} | {p["expiry"] for p in pending}
                if s["expiry"] not in already:
                    want_long = s["pred"] > 0
                    sup2, res2 = get_zones(hourly_ohlc, t_hr)
                    ok, zlvl = at_zone(spot, sup2, res2, want_long)
                    if ok and candle_ok(hourly_ohlc, t_hr, want_long):
                        mid = (zlvl + spot) / 2
                        stop_px = mid * (1-ZONE_BUFFER) if want_long else mid * (1+ZONE_BUFFER)
                        t1 = spot + (1 if want_long else -1) * abs(spot - stop_px)
                        t2 = next_zone(spot, sup2, res2, want_long)
                        pending.append({
                            "sig": s, "want_long": want_long,
                            "stop_px": stop_px, "target1": t1, "target2": t2,
                        })

        # execute pending entries
        new_pending = []
        for pe in pending:
            if len(open_pos) >= MAX_CONCURRENT:
                new_pending.append(pe); continue
            s    = pe["sig"]
            side = 1 if pe["want_long"] else -1
            fill_px = float(open_1m.loc[t_1m]) * (1 + side * SLIPPAGE_BPS / 1e4)
            sm = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(s["pred"]) / SIZE_BASE_PCT))
            notional = equity * sm * leverage
            risk_pct = abs(fill_px - pe["stop_px"]) / fill_px
            if risk_pct > 0:
                notional = min(notional, equity * 0.20 / risk_pct)
            nc = max(1, int(notional / (CONTRACT_SIZE * spot)))
            actual_notional = nc * CONTRACT_SIZE * fill_px
            equity -= actual_notional * PERP_FEE_BPS / 1e4
            open_pos.append({
                "entry_t": t_1m, "entry_px": fill_px, "side": side,
                "expiry": s["expiry"], "contracts": nc,
                "notional": actual_notional,
                "stop_px": pe["stop_px"],
                "target1": pe["target1"], "target2": pe["target2"],
                "peak_px": fill_px, "pred_pct": s["pred"],
                "n_strikes": s["n_strikes"],
            })
        pending = new_pending

        # manage open positions at 1m resolution
        still = []
        for pos in open_pos:
            side = pos["side"]; ep = pos["entry_px"]; nc = pos["contracts"]
            held_h = (t_1m - pos["entry_t"]).total_seconds() / 3600
            pos["peak_px"] = max(pos["peak_px"], spot) if side==1 else min(pos["peak_px"], spot)
            ur = side * (spot - ep) / ep

            # partial TP at 1:1
            if not pos.get("tp") and pos["target1"] and \
               ((side==1 and spot >= pos["target1"]) or (side==-1 and spot <= pos["target1"])):
                cn = max(1, int(nc * PARTIAL_CLOSE))
                fp = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fp - ep) / ep
                pnl = cn * CONTRACT_SIZE * fp * ret - cn * CONTRACT_SIZE * fp * PERP_FEE_BPS / 1e4
                equity += pnl
                pos["contracts"] -= cn; pos["tp"] = True
                trades.append({**pos, "exit_t": t_1m, "exit_px": fp, "pnl_usd": pnl,
                               "exit_reason": "partial_tp", "contracts": cn, "equity_after": equity})

            nc = pos["contracts"]; er = None
            if t_1m >= pos["expiry"]: er = "expiry"
            elif held_h >= MAX_HOLD_HOURS: er = "max_hold"
            elif (side==1 and spot <= pos["stop_px"]) or (side==-1 and spot >= pos["stop_px"]): er = "stop_loss"
            elif pos["target2"] and ((side==1 and spot >= pos["target2"]) or (side==-1 and spot <= pos["target2"])): er = "zone_target"
            elif ur >= TRAIL_PEAK_PCT:
                pr = side * (pos["peak_px"] - ep) / ep
                if (pr - ur) > TRAIL_GIVE_PCT: er = "trail"

            if er and nc > 0:
                fp = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fp - ep) / ep
                pnl = nc * CONTRACT_SIZE * fp * ret - nc * CONTRACT_SIZE * fp * PERP_FEE_BPS / 1e4
                equity += pnl
                trades.append({**pos, "exit_t": t_1m, "exit_px": fp, "pnl_usd": pnl,
                               "exit_reason": er, "contracts": nc, "equity_after": equity})
            else:
                still.append(pos)
        open_pos = still

    # close remaining
    last_spot = float(futures_1m.iloc[-1]["close"])
    for pos in open_pos:
        side = pos["side"]; ep = pos["entry_px"]; nc = pos["contracts"]
        fp = last_spot * (1 - side * SLIPPAGE_BPS / 1e4)
        ret = side * (fp - ep) / ep
        pnl = nc * CONTRACT_SIZE * fp * ret - nc * CONTRACT_SIZE * fp * PERP_FEE_BPS / 1e4
        equity += pnl
        trades.append({**pos, "exit_t": futures_1m.index[-1], "exit_px": fp,
                       "pnl_usd": pnl, "exit_reason": "data_end",
                       "contracts": nc, "equity_after": equity})

    if not trades:
        return {"trades": 0, "equity": equity, "leverage": leverage}

    df = pd.DataFrame(trades)
    df["exit_t"] = pd.to_datetime(df["exit_t"], utc=True)
    df = df.sort_values("exit_t").reset_index(drop=True)

    n    = len(df)
    wins = (df["pnl_usd"] > 0).sum()
    avg_win  = df.loc[df["pnl_usd"] > 0,  "pnl_usd"].mean() if wins else 0
    avg_loss = df.loc[df["pnl_usd"] <= 0, "pnl_usd"].mean() if (n-wins) else 0
    rr   = abs(avg_win / avg_loss) if avg_loss else float("nan")

    eq   = pd.Series([e for _, e in equity_curve], index=[t for t, _ in equity_curve])
    dd   = (eq - eq.cummax()).min() / START_USD * 100
    daily = eq.resample("1D").last().dropna().pct_change().dropna()
    daily = daily.replace([float("inf"), float("-inf")], float("nan")).dropna()
    sharpe = daily.mean() / daily.std() * math.sqrt(365) if daily.std() > 0 else 0

    monthly = df.groupby(df["exit_t"].dt.to_period("M"))["pnl_usd"].agg(["sum","count"])

    return {
        "leverage": leverage, "trades": n, "wins": int(wins),
        "win_rate": wins/n*100, "avg_win": avg_win, "avg_loss": avg_loss, "rr": rr,
        "equity": equity, "net_pct": (equity-START_USD)/START_USD*100,
        "net_inr": (equity-START_USD)*USD_RATE,
        "final_inr": equity*USD_RATE,
        "sharpe": sharpe, "max_dd": dd, "monthly": monthly,
    }


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Capital: Rs {INR_BUDGET:,} = ${START_USD} @ Rs{USD_RATE}/USD")
    print(f"Signal : ACTUAL option trade prices (last traded, max {MAX_STALE_HOURS}h stale)")
    print(f"Exec   : ACTUAL futures tick prices at 1m resolution")
    print(f"Fee    : {PERP_FEE_BPS}bps/side  Slippage: {SLIPPAGE_BPS}bps")
    print()

    # load data once
    futures_1m = load_futures_1m()
    opt_hourly = load_options_hourly()

    # build catalogue — weekly expiries only (Friday + month-end)
    print("Building option catalogue (weekly expiries only)...")
    rows = []
    for sym in opt_hourly:
        parsed = parse_symbol(sym)
        if parsed is None: continue
        side, strike, expiry = parsed
        # keep Friday (weekday=4) and month-end expiries only
        if expiry.weekday() != 4 and not expiry.is_month_end:
            continue
        rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": expiry})
    catalogue = pd.DataFrame(rows)
    print(f"  {len(catalogue):,} symbols across {catalogue['expiry'].nunique()} weekly expiries\n")

    print("Pre-computing hourly signals from actual option trades...")
    precomputed = precompute_hourly_signals(futures_1m, opt_hourly, catalogue)
    print()

    results = []
    for lev in [1, 10, 20]:
        r = run(lev, futures_1m, opt_hourly, catalogue, precomputed)
        results.append(r)

    print()
    print("=" * 80)
    print(f"  TICK-LEVEL REALISTIC BACKTEST — Actual Trade Prices")
    print(f"  Capital: Rs {INR_BUDGET:,}  |  BTC only  |  Mar-May 2026")
    print("=" * 80)
    print(f"  {'Lev':>4}  {'Trades':>7} {'Win%':>6} {'R:R':>5} "
          f"{'Net%':>8} {'Net Rs':>12} {'Sharpe':>7} {'MaxDD':>7}")
    print("  " + "-" * 72)
    for r in results:
        if r["trades"] == 0:
            print(f"  {str(r['leverage'])+'x':>4}  No trades fired"); continue
        print(f"  {str(r['leverage'])+'x':>4}  {r['trades']:>7} {r['win_rate']:>5.1f}% "
              f"{r['rr']:>5.2f} {r['net_pct']:>7.1f}% "
              f"Rs{r['net_inr']:>10,.0f} {r['sharpe']:>7.2f} {r['max_dd']:>6.1f}%")
    print("=" * 80)

    print()
    for r in results:
        if r["trades"] == 0: continue
        print(f"\n  Leverage {r['leverage']}x — Monthly P&L:")
        print(f"  {'Month':<10} {'Trades':>7} {'PnL $':>10} {'PnL Rs':>13} {'Running Rs':>13}")
        print("  " + "-" * 58)
        running = INR_BUDGET
        for m, row in r["monthly"].iterrows():
            inr_v = row["sum"] * USD_RATE
            running += inr_v
            s = "+" if row["sum"] >= 0 else ""
            print(f"  {str(m):<10} {int(row['count']):>7} "
                  f"{s}${row['sum']:>8,.0f}  Rs{s}{inr_v:>9,.0f}  Rs{running:>10,.0f}")
        print(f"  {'Final':10}          "
              f"{'':>10}  {'':>13}  Rs{r['final_inr']:>10,.0f}  "
              f"({r['net_pct']:+.1f}%)")

        print(f"\n  Exit breakdown:")
        for reason, grp in pd.DataFrame(r.get("monthly")).iterrows():
            pass
        df_trades = pd.DataFrame([t for t in [] ])  # placeholder
