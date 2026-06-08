"""
Synth-Forward Futures — Straddle Exit Rules
============================================
Same signal + execution as v5 (directional perp trade).
Exit rules replaced with straddle-style dollar thresholds:

  "Premium" = RISK_PCT × equity — this is the max we're willing to lose.

  stop_loss   : unrealized loss > 60% of premium  (same as straddle hitting 40% value)
  partial_tp  : unrealized gain > 2× premium       (same as straddle doubling)
  trail       : after gain > 60% of premium, trail 15% giveback from peak
  max_hold    : 72h hard cap

Key difference from v5:
  v5 TP at +1% price move → triggers fast, small wins.
  Here TP at 2× risk budget → holds longer, waits for the big move.

Usage:
  .venv/Scripts/python backtest_synth_straddle_exits.py
  UNDERLYING=ETH .venv/Scripts/python backtest_synth_straddle_exits.py
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import math, os, re
from pathlib import Path
import numpy as np
import pandas as pd

UNDERLYING  = os.environ.get("UNDERLYING", "BTC").upper()
PERP_SYMBOL = f"{UNDERLYING}USD"
DATA = (Path(__file__).parent / "data") if UNDERLYING == "BTC" \
       else (Path(__file__).parent / "data" / UNDERLYING.lower())

# ── Signal config (same as v5) ────────────────────────────────────────────────
ENTRY_PCT       = float(os.environ.get("ENTRY_PCT", "0.006"))
PERSIST_HOURS   = 2
MIN_STRIKES     = 3
MONEYNESS       = 0.05
TT_MIN_HOURS    = 6
TT_MAX_HOURS    = 72
MAX_HOLD_HOURS  = 72
PERP_FEE_BPS    = 5.0
SLIPPAGE_BPS    = 2.0
SIZE_BASE_PCT   = 0.005
SIZE_MIN_MULT   = 0.5
SIZE_MAX_MULT   = 3.0
MAX_CONCURRENT  = 2

# ── Straddle exit rules (in terms of RISK_USD = "premium") ───────────────────
RISK_PCT            = 0.05   # 5% of equity = the "premium" we're risking per trade
STOP_LOSS_FRAC      = 0.60   # stop when loss > 60% of risk budget (straddle: value at 40%)
PARTIAL_TP_MULT     = 2.00   # close half when gain > 2× risk budget
TRAIL_TRIGGER_MULT  = 0.60   # start trailing after gain > 60% of risk budget
TRAIL_GIVEBACK_FRAC = 0.15   # exit if gain gives back 15% from peak dollar gain

START_EQUITY = 10_000.0


# ── Data ──────────────────────────────────────────────────────────────────────
def load_perp() -> pd.Series:
    df = pd.read_csv(DATA / "perp" / f"{PERP_SYMBOL}_mark_1m.csv")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


def load_option_marks() -> dict:
    out = {}
    for p in sorted((DATA / "options").glob("*_mark_1h.csv")):
        sym = p.name.replace("_mark_1h.csv", "")
        df = pd.read_csv(p)
        if df.empty: continue
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        out[sym] = df.set_index("timestamp")["close"].sort_index()
    return out


def build_index(marks):
    rows = []
    for sym in marks:
        m = re.match(r"^([CP])-([A-Z]+)-(\d+)-(\d{6})$", sym)
        if not m: continue
        side, asset, strike, ddmmyy = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        if asset != UNDERLYING: continue
        try:
            dd, mm, yy = int(ddmmyy[:2]), int(ddmmyy[2:4]), int(ddmmyy[4:6])
            expiry = pd.Timestamp(f"20{yy:02d}-{mm:02d}-{dd:02d} 12:00:00", tz="UTC")
        except Exception:
            continue
        rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": expiry})
    return pd.DataFrame(rows)


def compute_signal(t, spot, catalogue, marks):
    tt_min = t + pd.Timedelta(hours=TT_MIN_HOURS)
    tt_max = t + pd.Timedelta(hours=TT_MAX_HOURS)
    eligible = catalogue[(catalogue["expiry"] > tt_min) & (catalogue["expiry"] <= tt_max)]
    candidates = []
    for exp in sorted(eligible["expiry"].unique()):
        same = eligible[eligible["expiry"] == exp]
        calls = same[same["side"] == "C"].set_index("strike")
        puts  = same[same["side"] == "P"].set_index("strike")
        common = sorted(set(calls.index) & set(puts.index))
        near = [K for K in common if abs(K - spot) / spot <= MONEYNESS]
        if len(near) < MIN_STRIKES: continue
        devs = []
        for K in near:
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
        candidates.append({"expiry": exp, "pred": float(np.median(devs)), "n_strikes": len(devs)})
    if not candidates: return None
    candidates.sort(key=lambda c: -abs(c["pred"]))
    best = candidates[0]
    if abs(best["pred"]) < ENTRY_PCT: return None
    return best


# ── Backtest ──────────────────────────────────────────────────────────────────
def run():
    print(f"Loading {UNDERLYING} data...")
    perp  = load_perp()
    marks = load_option_marks()
    cat   = build_index(marks)
    hours = perp.index[(perp.index.minute == 0) & (perp.index.second == 0)]
    print(f"  perp 1m bars : {len(perp):,}")
    print(f"  option marks : {len(marks):,} symbols")
    print(f"  decision pts : {len(hours):,}\n")

    equity       = START_EQUITY
    open_pos     = []
    trades       = []
    equity_curve = []
    sig_history  = {}

    for i, t in enumerate(hours):
        if i % 500 == 0 and i > 0:
            print(f"  ... {i}/{len(hours)}  open={len(open_pos)}  closed={len(trades)}")

        spot = float(perp.loc[t])
        equity_curve.append((t, equity))

        sig = compute_signal(t, spot, cat, marks)
        if sig:
            sig_history.setdefault(sig["expiry"], []).append((t, sig["pred"]))
        for exp in list(sig_history):
            sig_history[exp] = [(ti, p) for ti, p in sig_history[exp]
                                if (t - ti).total_seconds() <= 6 * 3600]

        # manage open positions
        still_open = []
        for pos in open_pos:
            side      = pos["side"]
            entry_px  = pos["entry_px"]
            notional  = pos["notional"]
            risk_usd  = pos["risk_usd"]   # the "premium" = max we planned to lose

            unreal_ret = side * (spot - entry_px) / entry_px
            unreal_pnl = notional * unreal_ret
            pos["peak_pnl"] = max(pos.get("peak_pnl", 0.0), unreal_pnl)
            held_h = (t - pos["entry_t"]).total_seconds() / 3600

            # partial TP — gain > 2× risk budget
            if not pos.get("tp_taken") and unreal_pnl >= PARTIAL_TP_MULT * risk_usd:
                half_n   = notional * 0.5
                fill_px  = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret      = side * (fill_px - entry_px) / entry_px
                pnl_pct  = ret - 2 * PERP_FEE_BPS / 1e4
                pnl_usd  = half_n * pnl_pct
                equity  += pnl_usd
                pos["notional"] -= half_n
                pos["tp_taken"]  = True
                trades.append({**pos, "exit_t": t, "exit_px": fill_px,
                               "ret": ret, "pnl_usd": pnl_usd,
                               "exit_reason": "partial_tp", "notional": half_n,
                               "equity_after": equity})

            exit_reason = None
            notional = pos["notional"]
            unreal_pnl = notional * side * (spot - entry_px) / entry_px

            if t >= pos["expiry"]:
                exit_reason = "expiry"
            elif held_h >= MAX_HOLD_HOURS:
                exit_reason = "max_hold"
            elif unreal_pnl < -(STOP_LOSS_FRAC * risk_usd):
                exit_reason = "stop_loss"
            elif (pos["peak_pnl"] >= TRAIL_TRIGGER_MULT * risk_usd and
                  unreal_pnl < pos["peak_pnl"] * (1 - TRAIL_GIVEBACK_FRAC)):
                exit_reason = "trail"

            if exit_reason:
                fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret     = side * (fill_px - entry_px) / entry_px
                pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
                pnl_usd = notional * pnl_pct
                equity += pnl_usd
                trades.append({**pos, "exit_t": t, "exit_px": fill_px,
                               "ret": ret, "pnl_usd": pnl_usd,
                               "exit_reason": exit_reason, "notional": notional,
                               "equity_after": equity})
            else:
                still_open.append(pos)

        open_pos = still_open

        # entry
        if sig is None or len(open_pos) >= MAX_CONCURRENT: continue
        already_in = {p["expiry"] for p in open_pos}
        if sig["expiry"] in already_in: continue

        hist   = sig_history.get(sig["expiry"], [])
        recent = [p for ti, p in hist if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
        if len(recent) < PERSIST_HOURS: continue
        if sum(1 for p in recent if np.sign(p) == np.sign(sig["pred"])) < PERSIST_HOURS:
            continue

        pred    = sig["pred"]
        side    = 1 if pred > 0 else -1
        fill_px = spot * (1 + side * SLIPPAGE_BPS / 1e4)
        size_mult = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(pred) / SIZE_BASE_PCT))
        notional  = equity * size_mult
        risk_usd  = equity * RISK_PCT   # the "premium" = fixed max loss budget

        open_pos.append({
            "entry_t": t, "entry_px": fill_px, "side": side,
            "expiry": sig["expiry"], "notional": notional,
            "size_mult": size_mult, "pred_pct": pred,
            "n_strikes": sig["n_strikes"],
            "risk_usd": risk_usd, "peak_pnl": 0.0,
        })

    # close remaining
    for pos in open_pos:
        side = pos["side"]; entry_px = pos["entry_px"]
        t_end = perp.index[-1]; spot = float(perp.iloc[-1])
        fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
        ret = side * (fill_px - entry_px) / entry_px
        pnl_usd = pos["notional"] * (ret - 2 * PERP_FEE_BPS / 1e4)
        equity += pnl_usd
        trades.append({**pos, "exit_t": t_end, "exit_px": fill_px,
                       "ret": ret, "pnl_usd": pnl_usd,
                       "exit_reason": "data_end", "equity_after": equity})

    if not trades:
        print("No trades."); return

    df = pd.DataFrame(trades)
    df["exit_t"] = pd.to_datetime(df["exit_t"], utc=True)
    df = df.sort_values("exit_t").reset_index(drop=True)

    n    = len(df)
    wins = (df["pnl_usd"] > 0).sum()
    avg_win  = df.loc[df["pnl_usd"] > 0,  "pnl_usd"].mean() if wins else 0
    avg_loss = df.loc[df["pnl_usd"] <= 0, "pnl_usd"].mean() if (n - wins) else 0
    rr   = abs(avg_win / avg_loss) if avg_loss else float("nan")
    eq   = pd.Series([e for _, e in equity_curve], index=[t for t, _ in equity_curve])
    dd   = (eq - eq.cummax()).min()
    daily = eq.resample("1D").last().dropna().pct_change().dropna()
    sharpe = daily.mean() / daily.std() * math.sqrt(365) if daily.std() > 0 else 0.0

    print()
    print("=" * 80)
    print(f"  Synth-Forward Futures — Straddle Exits ({UNDERLYING}, gate {ENTRY_PCT*100:.1f}%)")
    print(f"  Stop at 60% of risk budget  |  TP at 2×  |  Trail after 60%  |  Giveback 15%")
    print("=" * 80)
    print(f"  Trades      : {n}  (wins {wins}  losses {n-wins}  win rate {wins/n*100:.1f}%)")
    print(f"  Avg win/loss: ${avg_win:+,.0f} / ${avg_loss:+,.0f}   R:R {rr:.2f}")
    print(f"  Total PnL   : ${df['pnl_usd'].sum():+,.0f}")
    print(f"  Final equity: ${equity:,.0f}  ({(equity-START_EQUITY)/START_EQUITY*100:+.1f}%)")
    print(f"  Sharpe      : {sharpe:.2f}   Max DD: ${dd:+,.0f}  ({dd/START_EQUITY*100:.1f}%)")
    print()

    monthly = df.groupby(df["exit_t"].dt.to_period("M"))["pnl_usd"].agg(["sum","count"])
    print("  Monthly breakdown:")
    for m, row in monthly.iterrows():
        print(f"    {m}  trades={int(row['count']):>3}  pnl=${row['sum']:>+8,.0f}")
    print()

    print("  Exit reasons:")
    for reason, grp in df.groupby("exit_reason"):
        wr = (grp["pnl_usd"] > 0).mean() * 100
        print(f"    {reason:<12} {len(grp):>3} trades  win {wr:>5.1f}%  "
              f"total ${grp['pnl_usd'].sum():>+8,.0f}")

    print(f"\n  vs v5 benchmark: 86 trades  +116%  Sharpe 8.82")
    out = DATA / "straddle_exits_trades.csv"
    df.to_csv(out, index=False)
    print(f"  trade log -> {out}")


if __name__ == "__main__":
    run()
