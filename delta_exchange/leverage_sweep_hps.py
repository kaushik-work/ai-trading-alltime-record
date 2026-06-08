"""
HPS Strategy — Leverage Sweep
Runs HPS backtest at 1x, 5x, 10x, 20x, 50x leverage for BTC and ETH.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import math, re
from pathlib import Path
import numpy as np
import pandas as pd

ENTRY_PCT=0.006; PERSIST_HOURS=1; MIN_STRIKES=3; MONEYNESS=0.05
TT_MIN_HOURS=6; TT_MAX_HOURS=72; PERP_FEE_BPS=5.0; SLIPPAGE_BPS=2.0
SIZE_BASE_PCT=0.005; SIZE_MIN_MULT=0.5; SIZE_MAX_MULT=3.0; MAX_CONCURRENT=2
MAX_HOLD_HOURS=72; ZONE_LOOKBACK=30; ZONE_TOLERANCE=0.020; ZONE_BUFFER=0.004
CANDLE_BODY_MIN=0.001; PARTIAL_CLOSE_FRAC=0.70; START_EQUITY=10_000.0


def run_hps(underlying="BTC", leverage=1):
    DATA = Path("data") if underlying == "BTC" else Path("data") / underlying.lower()
    PERP_SYMBOL = f"{underlying}USD"

    df1m = pd.read_csv(DATA / "perp" / f"{PERP_SYMBOL}_mark_1m.csv")
    df1m["timestamp"] = pd.to_datetime(df1m["time"], unit="s", utc=True)
    df1m = df1m.set_index("timestamp").sort_index()
    hourly = df1m["close"].resample("1h").agg(
        open="first", high="max", low="min", close="last").dropna()

    marks = {}
    for p in sorted((DATA / "options").glob("*_mark_1h.csv")):
        sym = p.name.replace("_mark_1h.csv", "")
        dfo = pd.read_csv(p)
        if dfo.empty: continue
        dfo["timestamp"] = pd.to_datetime(dfo["time"], unit="s", utc=True)
        marks[sym] = dfo.set_index("timestamp")["close"].sort_index()

    cat_rows = []
    for sym in marks:
        m = re.match(r"^([CP])-([A-Z]+)-(\d+)-(\d{6})$", sym)
        if not m: continue
        side, asset, strike, ddmmyy = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        if asset != underlying: continue
        try:
            dd, mm, yy = int(ddmmyy[:2]), int(ddmmyy[2:4]), int(ddmmyy[4:6])
            expiry = pd.Timestamp(f"20{yy:02d}-{mm:02d}-{dd:02d} 12:00:00", tz="UTC")
        except: continue
        cat_rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": expiry})
    cat = pd.DataFrame(cat_rows)

    def compute_sig(t, spot):
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

    def get_zones(t):
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
            above = [r for r in res if r > spot]
            return min(above) if above else None
        below = [s for s in sup if s < spot]
        return max(below) if below else None

    def candle_ok(t, want_long):
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

    equity = START_EQUITY
    open_pos, trades, equity_curve, sig_history = [], [], [], {}

    for t in hourly.index:
        spot = float(df1m.loc[t, "close"]) if t in df1m.index else float(hourly.loc[t, "close"])
        equity_curve.append((t, equity))

        s = compute_sig(t, spot)
        if s:
            sig_history.setdefault(s["expiry"], []).append((t, s["pred"]))
        for exp in list(sig_history):
            sig_history[exp] = [(ti, p) for ti, p in sig_history[exp]
                                if (t - ti).total_seconds() <= 6 * 3600]

        still = []
        for pos in open_pos:
            side = pos["side"]; entry_px = pos["entry_px"]
            notional = pos["notional"]; stop_px = pos["stop_px"]
            t1 = pos["target1"]; t2 = pos["target2"]
            held_h = (t - pos["entry_t"]).total_seconds() / 3600

            if not pos.get("tp_taken") and t1 and \
               ((side == 1 and spot >= t1) or (side == -1 and spot <= t1)):
                pn = notional * PARTIAL_CLOSE_FRAC
                fp = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fp - entry_px) / entry_px
                pnl = pn * (ret - 2 * PERP_FEE_BPS / 1e4)
                equity += pnl
                pos["notional"] -= pn; pos["tp_taken"] = True
                trades.append({**pos, "exit_t": t, "pnl_usd": pnl,
                               "exit_reason": "partial_tp", "notional": pn})

            notional = pos["notional"]
            er = None
            if t >= pos["expiry"]: er = "expiry"
            elif held_h >= MAX_HOLD_HOURS: er = "max_hold"
            elif (side == 1 and spot <= stop_px) or (side == -1 and spot >= stop_px): er = "stop_loss"
            elif t2 and ((side == 1 and spot >= t2) or (side == -1 and spot <= t2)): er = "zone_target"
            if er:
                fp = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fp - entry_px) / entry_px
                pnl = notional * (ret - 2 * PERP_FEE_BPS / 1e4)
                equity += pnl
                trades.append({**pos, "exit_t": t, "pnl_usd": pnl,
                               "exit_reason": er, "notional": notional})
            else:
                still.append(pos)
        open_pos = still

        if equity < START_EQUITY * 0.10: continue  # ruin guard: stop at -90%
        if len(open_pos) >= MAX_CONCURRENT or s is None: continue
        already = {p["expiry"] for p in open_pos}
        if s["expiry"] in already: continue
        hist2 = sig_history.get(s["expiry"], [])
        recent = [p for ti, p in hist2 if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
        if len(recent) < PERSIST_HOURS: continue
        if sum(1 for p in recent if np.sign(p) == np.sign(s["pred"])) < PERSIST_HOURS: continue

        want_long = s["pred"] > 0; side = 1 if want_long else -1
        sup2, res2 = get_zones(t)
        ok, zlvl = at_zone(spot, sup2, res2, want_long)
        if not ok: continue
        if not candle_ok(t, want_long): continue

        mid = (zlvl + spot) / 2
        stop_px = mid * (1 - ZONE_BUFFER) if want_long else mid * (1 + ZONE_BUFFER)
        risk = abs(spot - stop_px)
        t1 = spot + side * risk
        t2 = next_zone(spot, sup2, res2, want_long)
        fp = spot * (1 + side * SLIPPAGE_BPS / 1e4)
        sm = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(s["pred"]) / SIZE_BASE_PCT))
        notional = equity * sm * leverage
        # cap: never risk more than 20% of equity on a single stop
        risk_pct = abs(spot - (mid*(1-ZONE_BUFFER) if want_long else mid*(1+ZONE_BUFFER))) / spot
        if risk_pct > 0:
            max_notional = equity * 0.20 / risk_pct
            notional = min(notional, max_notional)

        open_pos.append({
            "entry_t": t, "entry_px": fp, "side": side,
            "expiry": s["expiry"], "notional": notional,
            "size_mult": sm, "pred_pct": s["pred"],
            "n_strikes": s["n_strikes"], "stop_px": stop_px,
            "target1": t1, "target2": t2, "zone_lvl": zlvl,
        })

    for pos in open_pos:
        side = pos["side"]; entry_px = pos["entry_px"]
        spot = float(hourly.iloc[-1]["close"])
        fp = spot * (1 - side * SLIPPAGE_BPS / 1e4)
        ret = side * (fp - entry_px) / entry_px
        pnl = pos["notional"] * (ret - 2 * PERP_FEE_BPS / 1e4)
        equity += pnl
        trades.append({**pos, "exit_t": hourly.index[-1], "pnl_usd": pnl,
                       "exit_reason": "data_end", "notional": pos["notional"]})

    if not trades:
        return {"trades": 0, "win_rate": 0, "net_pct": 0, "sharpe": 0, "max_dd_pct": 0}

    df = pd.DataFrame(trades)
    n = len(df); wins = (df["pnl_usd"] > 0).sum()
    eq = pd.Series([e for _, e in equity_curve], index=[t for t, _ in equity_curve])
    dd = (eq - eq.cummax()).min()
    eq_pos = eq.clip(lower=0.01)  # prevent log/div errors on negative equity
    daily = eq_pos.resample("1D").last().dropna().pct_change().dropna()
    daily = daily.replace([np.inf, -np.inf], np.nan).dropna()
    sharpe = daily.mean() / daily.std() * math.sqrt(365) if daily.std() > 0 else 0
    return {
        "trades": n, "wins": int(wins), "win_rate": wins / n * 100,
        "net_pct": (equity - START_EQUITY) / START_EQUITY * 100,
        "sharpe": sharpe, "max_dd_pct": dd / START_EQUITY * 100,
    }


if __name__ == "__main__":
    print("Running HPS leverage sweep (BTC + ETH)...")
    print()
    print("=" * 80)
    print("  HPS Synth-Forward — Leverage Sweep  |  $10k equity  |  1h candles")
    print("  Candle timeframe: 1h  |  Zone: 1h swing highs/lows  |  Fee: 0.05%/side")
    print("=" * 80)
    print(f"  {'Lev':<6}  {'BTC':^36}  {'ETH':^36}")
    print(f"  {'':6}  {'Trades':>6} {'Win%':>6} {'Net%':>8} {'Sharpe':>7} {'MaxDD':>7}  "
          f"{'Trades':>6} {'Win%':>6} {'Net%':>8} {'Sharpe':>7} {'MaxDD':>7}")
    print("  " + "-" * 78)

    for lev in [1, 5, 10, 20, 50]:
        rb   = run_hps("BTC", lev)
        reth = run_hps("ETH", lev)
        safe = "" if lev <= 66 else " !"
        print(f"  {str(lev)+'x'+safe:<6}  "
              f"{rb['trades']:>6} {rb['win_rate']:>5.1f}% {rb['net_pct']:>7.1f}% "
              f"{rb['sharpe']:>7.2f} {rb['max_dd_pct']:>6.1f}%  "
              f"{reth['trades']:>6} {reth['win_rate']:>5.1f}% {reth['net_pct']:>7.1f}% "
              f"{reth['sharpe']:>7.2f} {reth['max_dd_pct']:>6.1f}%")

    print("=" * 80)
    print("  ! = liquidation at 1/leverage % — dangerous above 66x with -1.5% stop")
    print()
    print("  Candle note: 1h bars used for zones + confirmation.")
    print("  15min would sharpen entries but needs 15min option mark data (not available).")
