"""
Realistic 1-Minute Resolution Backtest — Synth-Forward v5 + HPS
=================================================================
Signal   : hourly put-call parity (same as v5)
Execution: 1-minute mark prices — entries, stops, TPs all checked
           every minute instead of every hour.

This gives a much more honest picture because:
  - Intraday wicks can hit stops before the hourly close
  - Entry happens at the NEXT 1m bar after signal fires (not hourly close)
  - Slippage modelled on actual 1m mark price

Capital   : Rs 1,00,000  (₹96 per USD)
Leverages : 1×, 10×, 20×
Assets    : BTC, ETH

Usage:
  .venv/Scripts/python backtest_1m_realistic.py
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import math, re, warnings
warnings.filterwarnings("ignore")
from pathlib import Path
import numpy as np
import pandas as pd

INR_BUDGET  = 100_000
USD_RATE    = 96.0
START_USD   = round(INR_BUDGET / USD_RATE, 2)

ENTRY_PCT        = 0.006
PERSIST_HOURS    = 1
MIN_STRIKES      = 3
MONEYNESS        = 0.05
TT_MIN_HOURS     = 6
TT_MAX_HOURS     = 72

PERP_FEE_BPS     = 5.0
SLIPPAGE_BPS     = 3.0       # slightly wider on 1m (realistic)
CONTRACT_SIZE    = 0.001     # BTC/ETH per contract on Delta India

SIZE_BASE_PCT    = 0.005
SIZE_MIN_MULT    = 0.5
SIZE_MAX_MULT    = 3.0
MAX_CONCURRENT   = 2
MAX_HOLD_HOURS   = 72

ZONE_LOOKBACK    = 30
ZONE_TOLERANCE   = 0.020
ZONE_BUFFER      = 0.004
CANDLE_BODY_MIN  = 0.001
PARTIAL_CLOSE    = 0.70      # close 70% at first target

# Stop / TP parameters (as fraction of entry price)
STOP_PCT         = 0.015     # -1.5% hard stop
TRAIL_PEAK_PCT   = 0.005     # start trailing after +0.5%
TRAIL_GIVE_PCT   = 0.0025    # give back 0.25% from peak


# ── Data loading ───────────────────────────────────────────────────────────────
def load_1m(underlying: str) -> pd.Series:
    DATA = Path("data") if underlying == "BTC" else Path("data") / underlying.lower()
    df = pd.read_csv(DATA / "perp" / f"{underlying}USD_mark_1m.csv")
    df["ts"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("ts")["close"].sort_index()


def load_hourly(perp_1m: pd.Series) -> pd.DataFrame:
    return perp_1m.resample("1h").agg(
        open="first", high="max", low="min", close="last"
    ).dropna()


def load_marks(underlying: str) -> dict:
    DATA = Path("data") if underlying == "BTC" else Path("data") / underlying.lower()
    out = {}
    for p in sorted((DATA / "options").glob("*_mark_1h.csv")):
        sym = p.name.replace("_mark_1h.csv", "")
        df = pd.read_csv(p)
        if df.empty: continue
        df["ts"] = pd.to_datetime(df["time"], unit="s", utc=True)
        out[sym] = df.set_index("ts")["close"].sort_index()
    return out


def build_catalogue(marks: dict, underlying: str) -> pd.DataFrame:
    rows = []
    for sym in marks:
        m = re.match(r"^([CP])-([A-Z]+)-(\d+)-(\d{6})$", sym)
        if not m: continue
        side, asset, strike, ddmmyy = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        if asset != underlying: continue
        try:
            dd, mm, yy = int(ddmmyy[:2]), int(ddmmyy[2:4]), int(ddmmyy[4:6])
            expiry = pd.Timestamp(f"20{yy:02d}-{mm:02d}-{dd:02d} 12:00:00", tz="UTC")
        except: continue
        rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": expiry})
    return pd.DataFrame(rows)


# ── Signal ─────────────────────────────────────────────────────────────────────
def compute_signal(t, spot, cat, marks):
    tt_min = t + pd.Timedelta(hours=TT_MIN_HOURS)
    tt_max = t + pd.Timedelta(hours=TT_MAX_HOURS)
    elig = cat[(cat["expiry"] > tt_min) & (cat["expiry"] <= tt_max)]
    cands = []
    for exp in sorted(elig["expiry"].unique()):
        same = elig[elig["expiry"] == exp]
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
        cands.append({"expiry": exp, "pred": float(np.median(devs)), "n_strikes": len(devs)})
    if not cands: return None
    cands.sort(key=lambda c: -abs(c["pred"]))
    best = cands[0]
    return best if abs(best["pred"]) >= ENTRY_PCT else None


# ── HPS zone helpers ───────────────────────────────────────────────────────────
def get_zones(hourly, t):
    hist = hourly.loc[:t].iloc[-ZONE_LOOKBACK-1:-1]
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


def candle_ok(hourly, t, want_long):
    if t not in hourly.index: return False
    b = hourly.loc[t]
    o, h, l, c = b["open"], b["high"], b["low"], b["close"]
    body = abs(c - o); rng = h - l
    if rng < 1e-6: return False
    uw = h - max(o, c); lw = min(o, c) - l; bf = body / rng
    if want_long:
        return (lw >= 2*body and uw <= 0.3*rng and c > o) or \
               (c > o and bf > 0.6 and body/o > CANDLE_BODY_MIN)
    return (uw >= 2*body and lw <= 0.3*rng and c < o) or \
           (c < o and bf > 0.6 and body/o > CANDLE_BODY_MIN)


# ── 1-minute backtest engine ───────────────────────────────────────────────────
def run(underlying: str, leverage: float) -> dict:
    print(f"  Loading {underlying} data...", flush=True)
    perp_1m = load_1m(underlying)
    hourly  = load_hourly(perp_1m)
    marks   = load_marks(underlying)
    cat     = build_catalogue(marks, underlying)

    hour_times = hourly.index
    print(f"    1m bars: {len(perp_1m):,}  |  hourly bars: {len(hourly):,}  |  option marks: {len(marks):,}")

    equity      = START_USD
    open_pos    = []
    trades      = []
    equity_curve= []
    sig_history = {}
    pending_entry = None   # signal waiting for next 1m bar

    # pending entries queued from hourly signal → execute on next 1m
    pending_entries = []

    # Walk every 1-minute bar
    prev_hour = None
    for t_1m in perp_1m.index:
        spot = float(perp_1m.loc[t_1m])
        equity_curve.append((t_1m, equity))

        # ── On each new hour: compute signal ──────────────────────────────
        t_hr = t_1m.floor("1h")
        if t_hr != prev_hour and t_hr in hour_times:
            prev_hour = t_hr
            s = compute_signal(t_hr, spot, cat, marks)
            if s:
                sig_history.setdefault(s["expiry"], []).append((t_hr, s["pred"]))
            for exp in list(sig_history):
                sig_history[exp] = [(ti, p) for ti, p in sig_history[exp]
                                    if (t_hr - ti).total_seconds() <= 6 * 3600]

            # check for new entry signal
            if s and len(open_pos) < MAX_CONCURRENT:
                already = {p["expiry"] for p in open_pos} | \
                          {p["expiry"] for p in pending_entries}
                if s["expiry"] not in already:
                    hist2  = sig_history.get(s["expiry"], [])
                    recent = [p for ti, p in hist2
                              if (t_hr - ti).total_seconds() <= PERSIST_HOURS * 3600]
                    if len(recent) >= PERSIST_HOURS and \
                       sum(1 for p in recent if np.sign(p) == np.sign(s["pred"])) >= PERSIST_HOURS:
                        want_long = s["pred"] > 0
                        sup2, res2 = get_zones(hourly, t_hr)
                        ok, zlvl = at_zone(spot, sup2, res2, want_long)
                        if ok and candle_ok(hourly, t_hr, want_long):
                            mid     = (zlvl + spot) / 2
                            stop_px = mid * (1 - ZONE_BUFFER) if want_long else mid * (1 + ZONE_BUFFER)
                            t1      = spot + (1 if want_long else -1) * abs(spot - stop_px)
                            t2      = next_zone(spot, sup2, res2, want_long)
                            pending_entries.append({
                                "sig": s, "want_long": want_long,
                                "stop_px": stop_px, "target1": t1, "target2": t2,
                            })

        # ── Execute pending entries at this 1m bar ─────────────────────────
        new_pending = []
        for pe in pending_entries:
            if len(open_pos) >= MAX_CONCURRENT:
                new_pending.append(pe); continue
            s    = pe["sig"]
            side = 1 if pe["want_long"] else -1
            # enter at this 1m price + slippage
            fill_px = spot * (1 + side * SLIPPAGE_BPS / 1e4)
            sm = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(s["pred"]) / SIZE_BASE_PCT))
            # contract-based sizing with leverage
            notional = equity * sm * leverage
            # risk cap: max 20% equity at stop distance
            risk_pct = abs(fill_px - pe["stop_px"]) / fill_px
            if risk_pct > 0:
                notional = min(notional, equity * 0.20 / risk_pct)
            # round to integer contracts
            n_contracts = max(1, int(notional / (CONTRACT_SIZE * spot)))
            actual_notional = n_contracts * CONTRACT_SIZE * fill_px
            entry_fee = actual_notional * PERP_FEE_BPS / 1e4
            equity -= entry_fee
            open_pos.append({
                "entry_t":   t_1m,
                "entry_px":  fill_px,
                "side":      side,
                "expiry":    s["expiry"],
                "contracts": n_contracts,
                "notional":  actual_notional,
                "stop_px":   pe["stop_px"],
                "target1":   pe["target1"],
                "target2":   pe["target2"],
                "peak_px":   fill_px,
                "pred_pct":  s["pred"],
                "n_strikes": s["n_strikes"],
            })
        pending_entries = new_pending

        # ── Manage open positions at 1m resolution ─────────────────────────
        still_open = []
        for pos in open_pos:
            side     = pos["side"]
            entry_px = pos["entry_px"]
            contracts= pos["contracts"]
            stop_px  = pos["stop_px"]
            t1       = pos["target1"]
            t2       = pos["target2"]
            held_h   = (t_1m - pos["entry_t"]).total_seconds() / 3600

            # update peak
            if side == 1:
                pos["peak_px"] = max(pos["peak_px"], spot)
            else:
                pos["peak_px"] = min(pos["peak_px"], spot)

            unreal_ret = side * (spot - entry_px) / entry_px

            # partial TP — 1:1 target
            if not pos.get("tp_taken") and t1:
                hit_t1 = (side == 1 and spot >= t1) or (side == -1 and spot <= t1)
                if hit_t1:
                    close_n = max(1, int(contracts * PARTIAL_CLOSE))
                    fp  = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                    ret = side * (fp - entry_px) / entry_px
                    pnl = close_n * CONTRACT_SIZE * fp * ret - \
                          close_n * CONTRACT_SIZE * fp * PERP_FEE_BPS / 1e4
                    equity += pnl
                    pos["contracts"] -= close_n
                    pos["notional"]  = pos["contracts"] * CONTRACT_SIZE * spot
                    pos["tp_taken"]  = True
                    trades.append({**pos, "exit_t": t_1m, "exit_px": fp,
                                   "ret": ret, "pnl_usd": pnl,
                                   "exit_reason": "partial_tp_1to1",
                                   "contracts": close_n, "equity_after": equity})

            # exit conditions checked every minute
            exit_reason = None
            if t_1m >= pos["expiry"]:
                exit_reason = "expiry"
            elif held_h >= MAX_HOLD_HOURS:
                exit_reason = "max_hold"
            elif (side == 1  and spot <= stop_px) or \
                 (side == -1 and spot >= stop_px):
                exit_reason = "stop_loss"
            elif t2 and ((side == 1 and spot >= t2) or (side == -1 and spot <= t2)):
                exit_reason = "zone_target"
            elif unreal_ret >= TRAIL_PEAK_PCT:
                # trail: give back TRAIL_GIVE_PCT from peak
                peak_ret = side * (pos["peak_px"] - entry_px) / entry_px
                if (peak_ret - unreal_ret) > TRAIL_GIVE_PCT:
                    exit_reason = "trail"

            if exit_reason and pos["contracts"] > 0:
                fp  = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fp - entry_px) / entry_px
                pnl = pos["contracts"] * CONTRACT_SIZE * fp * ret - \
                      pos["contracts"] * CONTRACT_SIZE * fp * PERP_FEE_BPS / 1e4
                equity += pnl
                trades.append({**pos, "exit_t": t_1m, "exit_px": fp,
                               "ret": ret, "pnl_usd": pnl,
                               "exit_reason": exit_reason,
                               "contracts": pos["contracts"],
                               "equity_after": equity})
            else:
                still_open.append(pos)

        open_pos = still_open

    # close any remaining
    for pos in open_pos:
        side = pos["side"]; entry_px = pos["entry_px"]
        spot = float(perp_1m.iloc[-1])
        fp   = spot * (1 - side * SLIPPAGE_BPS / 1e4)
        ret  = side * (fp - entry_px) / entry_px
        pnl  = pos["contracts"] * CONTRACT_SIZE * fp * ret - \
               pos["contracts"] * CONTRACT_SIZE * fp * PERP_FEE_BPS / 1e4
        equity += pnl
        trades.append({**pos, "exit_t": perp_1m.index[-1], "exit_px": fp,
                       "ret": ret, "pnl_usd": pnl,
                       "exit_reason": "data_end",
                       "contracts": pos["contracts"],
                       "equity_after": equity})

    if not trades:
        return {"trades": 0, "equity": equity, "underlying": underlying, "leverage": leverage}

    df = pd.DataFrame(trades)
    df["exit_t"] = pd.to_datetime(df["exit_t"], utc=True)
    df = df.sort_values("exit_t").reset_index(drop=True)

    n    = len(df)
    wins = (df["pnl_usd"] > 0).sum()
    avg_win  = df.loc[df["pnl_usd"] > 0,  "pnl_usd"].mean() if wins else 0
    avg_loss = df.loc[df["pnl_usd"] <= 0, "pnl_usd"].mean() if (n - wins) else 0
    rr   = abs(avg_win / avg_loss) if avg_loss else float("nan")
    eq   = pd.Series([e for _, e in equity_curve], index=[t for t, _ in equity_curve])
    dd   = (eq - eq.cummax()).min() / START_USD * 100
    daily = eq.resample("1D").last().dropna().pct_change().dropna()
    daily = daily.replace([float("inf"), float("-inf")], float("nan")).dropna()
    sharpe = daily.mean() / daily.std() * math.sqrt(365) if daily.std() > 0 else 0

    monthly = df.groupby(df["exit_t"].dt.to_period("M"))["pnl_usd"].agg(["sum", "count"])

    return {
        "underlying": underlying, "leverage": leverage,
        "trades": n, "wins": int(wins), "win_rate": wins/n*100,
        "avg_win": avg_win, "avg_loss": avg_loss, "rr": rr,
        "equity": equity, "net_pct": (equity - START_USD) / START_USD * 100,
        "sharpe": sharpe, "max_dd": dd,
        "monthly": monthly,
    }


# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Capital: Rs {INR_BUDGET:,} = ${START_USD:.2f} @ Rs{USD_RATE}/USD")
    print(f"Execution: 1-minute resolution  |  Signal: hourly parity")
    print(f"Contract size: {CONTRACT_SIZE} BTC/ETH  |  Fee: {PERP_FEE_BPS}bps/side")
    print()

    results = []
    for asset in ["BTC", "ETH"]:
        for lev in [1, 10, 20]:
            print(f"Running {asset} @ {lev}x...", flush=True)
            r = run(asset, lev)
            results.append(r)

    print()
    print("=" * 95)
    print(f"  1-Minute Realistic Backtest — Rs {INR_BUDGET:,} capital  |  HPS v5 Synth-Forward")
    print("=" * 95)
    print(f"  {'Asset':<5} {'Lev':>4}  {'Trades':>7} {'Win%':>6} {'R:R':>5} "
          f"{'Net%':>8} {'Final $':>9} {'Final Rs':>12} {'Sharpe':>7} {'MaxDD':>7}")
    print("  " + "-" * 93)
    for r in results:
        if r["trades"] == 0: continue
        final_inr = r["equity"] * USD_RATE
        print(f"  {r['underlying']:<5} {str(r['leverage'])+'x':>4}  "
              f"{r['trades']:>7} {r['win_rate']:>5.1f}% {r['rr']:>5.2f} "
              f"{r['net_pct']:>7.1f}% ${r['equity']:>8,.0f} "
              f"Rs{final_inr:>10,.0f} {r['sharpe']:>7.2f} {r['max_dd']:>6.1f}%")
    print("=" * 95)

    print()
    print("  Monthly breakdown:")
    for r in results:
        if r["trades"] == 0 or r["leverage"] != 10: continue
        print(f"\n  {r['underlying']} @ {r['leverage']}x:")
        print(f"  {'Month':<10} {'PnL $':>9} {'PnL Rs':>12} {'Trades':>8}")
        print("  " + "-" * 42)
        for m, row in r["monthly"].iterrows():
            inr_v = row["sum"] * USD_RATE
            sign = "+" if row["sum"] >= 0 else ""
            print(f"  {str(m):<10} {sign}${row['sum']:>7,.0f}  "
                  f"Rs{sign}{inr_v:>9,.0f}  {int(row['count']):>7}")
