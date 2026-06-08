"""
Synthetic-Forward Signal → Options Execution Backtest
======================================================
Same signal as v5 (put-call parity dislocation > ENTRY_PCT).
Execution: buy ATM call (bullish) or ATM put (bearish) instead of perp.

P&L is tracked on option mark price changes — theta decay is implicit.

Exit rules (on option value, not underlying):
  stop_loss   : option value drops below STOP_LOSS_PCT of premium paid
  partial_tp  : option value reaches PARTIAL_TP_MULT × entry — close half
  trail       : after TRAIL_TRIGGER_MULT, trail at TRAIL_GIVEBACK_MULT from peak
  expiry      : settle at intrinsic (mark at last available bar)
  max_hold    : force exit after MAX_HOLD_HOURS

Costs: OPT_FEE_BPS per leg (entry + exit).

Usage:
  .venv/Scripts/python backtest_synth_forward_options.py
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
import os
from pathlib import Path
import numpy as np
import pandas as pd

UNDERLYING   = os.environ.get("UNDERLYING", "BTC").upper()
PERP_SYMBOL  = f"{UNDERLYING}USD"
DATA = (Path(__file__).parent / "data") if UNDERLYING == "BTC" \
       else (Path(__file__).parent / "data" / UNDERLYING.lower())

# ── Config ────────────────────────────────────────────────────────────────────
ENTRY_PCT       = float(os.environ.get("ENTRY_PCT", "0.006"))
PERSIST_HOURS   = 2
MIN_STRIKES     = 3
MONEYNESS       = 0.05
TT_MIN_HOURS    = 6
TT_MAX_HOURS    = 72
MAX_HOLD_HOURS  = 48          # shorter than perp — theta eats value fast
OPT_FEE_BPS     = 25.0        # Delta India options taker fee

# Exit rules on option VALUE (multiples of premium paid)
STOP_LOSS_MULT      = 0.40    # exit if option worth < 40% of what we paid
PARTIAL_TP_MULT     = 2.00    # close half when option doubles
TRAIL_TRIGGER_MULT  = 1.50    # start trailing after 50% gain
TRAIL_GIVEBACK_MULT = 0.20    # exit if option gives back 20% from peak value

START_EQUITY = 10_000.0
SIZE_BASE_PCT = 0.005
SIZE_MIN_MULT = 0.5
SIZE_MAX_MULT = 3.0


# ── Data loading ──────────────────────────────────────────────────────────────
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


def build_index(marks: dict) -> pd.DataFrame:
    import re
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


# ── Signal (identical to v5) ──────────────────────────────────────────────────
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
        candidates.append({"expiry": exp, "pred": float(np.median(devs)),
                           "n_strikes": len(devs)})
    if not candidates: return None
    candidates.sort(key=lambda c: -abs(c["pred"]))
    best = candidates[0]
    if abs(best["pred"]) < ENTRY_PCT: return None
    return best


def find_atm_option(t, spot, catalogue, marks, expiry, want_call: bool):
    """Find ATM call or put for this expiry with a valid mark at time t."""
    side_filter = "C" if want_call else "P"
    same = catalogue[(catalogue["expiry"] == expiry) & (catalogue["side"] == side_filter)]
    if same.empty: return None, None, None
    # find nearest strike with a mark price at t
    same = same.copy()
    same["dist"] = (same["strike"] - spot).abs()
    for _, row in same.sort_values("dist").iterrows():
        sym = row["symbol"]
        series = marks.get(sym)
        if series is None: continue
        if t not in series.index: continue
        px = float(series.loc[t])
        if px <= 0: continue
        return sym, int(row["strike"]), px
    return None, None, None


# ── Backtest ──────────────────────────────────────────────────────────────────
def run():
    print("Loading data...")
    perp    = load_perp()
    marks   = load_option_marks()
    cat     = build_index(marks)
    hours   = perp.index[(perp.index.minute == 0) & (perp.index.second == 0)]
    print(f"  perp 1m bars : {len(perp):,}")
    print(f"  option marks : {len(marks):,} symbols, {cat['expiry'].nunique()} expiries")
    print(f"  decision pts : {len(hours):,} hourly samples\n")

    equity       = START_EQUITY
    open_pos     = []
    trades       = []
    equity_curve = []
    sig_history  = {}

    print("Walking decision points...")
    for i, t in enumerate(hours):
        if i % 500 == 0 and i > 0:
            print(f"  ... {i}/{len(hours)}  open={len(open_pos)}  closed={len(trades)}")

        spot = float(perp.loc[t])
        equity_curve.append((t, equity))

        # update signal history
        sig = compute_signal(t, spot, cat, marks)
        if sig:
            sig_history.setdefault(sig["expiry"], []).append((t, sig["pred"]))
        for exp in list(sig_history):
            sig_history[exp] = [(ti, p) for ti, p in sig_history[exp]
                                if (t - ti).total_seconds() <= 6 * 3600]

        # manage open positions
        still_open = []
        for pos in open_pos:
            opt_series = marks.get(pos["symbol"])
            if opt_series is None or t not in opt_series.index:
                still_open.append(pos); continue

            cur_val = float(opt_series.loc[t])
            entry_val = pos["entry_val"]
            peak_val  = pos.get("peak_val", entry_val)
            pos["peak_val"] = max(peak_val, cur_val)
            held_h = (t - pos["entry_t"]).total_seconds() / 3600

            # partial TP — option doubled, close half
            if not pos.get("tp_taken") and cur_val >= entry_val * PARTIAL_TP_MULT:
                half_notional = pos["notional"] * 0.5
                fee  = half_notional * OPT_FEE_BPS / 1e4
                pnl  = half_notional * (cur_val - entry_val) / entry_val - fee
                equity += pnl
                pos["notional"] -= half_notional
                pos["tp_taken"] = True
                trades.append({**pos, "exit_t": t, "exit_val": cur_val,
                               "pnl_usd": pnl, "exit_reason": "partial_tp",
                               "notional": half_notional, "equity_after": equity})

            exit_reason = None
            if t >= pos["expiry"]:
                exit_reason = "expiry"
            elif held_h >= MAX_HOLD_HOURS:
                exit_reason = "max_hold"
            elif cur_val < entry_val * STOP_LOSS_MULT:
                exit_reason = "stop_loss"
            elif pos["peak_val"] >= entry_val * TRAIL_TRIGGER_MULT and \
                 cur_val < pos["peak_val"] * (1 - TRAIL_GIVEBACK_MULT):
                exit_reason = "trail"

            if exit_reason:
                fee  = pos["notional"] * OPT_FEE_BPS / 1e4
                pnl  = pos["notional"] * (cur_val - entry_val) / entry_val - fee
                equity += pnl
                trades.append({**pos, "exit_t": t, "exit_val": cur_val,
                               "pnl_usd": pnl, "exit_reason": exit_reason,
                               "equity_after": equity})
            else:
                still_open.append(pos)

        open_pos = still_open

        # entry
        if sig is None or len(open_pos) >= 2: continue
        already_in = {p["expiry"] for p in open_pos}
        if sig["expiry"] in already_in: continue

        # persistence check
        hist   = sig_history.get(sig["expiry"], [])
        recent = [p for ti, p in hist if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
        if len(recent) < PERSIST_HOURS: continue
        if sum(1 for p in recent if np.sign(p) == np.sign(sig["pred"])) < PERSIST_HOURS:
            continue

        want_call = sig["pred"] > 0
        sym, strike, entry_val = find_atm_option(t, spot, cat, marks, sig["expiry"], want_call)
        if sym is None: continue

        size_mult = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(sig["pred"]) / SIZE_BASE_PCT))
        notional  = equity * size_mult
        entry_fee = notional * OPT_FEE_BPS / 1e4
        equity   -= entry_fee   # pay entry fee upfront
        open_pos.append({
            "entry_t": t, "symbol": sym, "strike": strike,
            "expiry": sig["expiry"], "want_call": want_call,
            "entry_val": entry_val, "peak_val": entry_val,
            "notional": notional, "size_mult": size_mult,
            "pred_pct": sig["pred"], "n_strikes": sig["n_strikes"],
        })

    # force-close remaining
    t_end = hours[-1]
    spot_end = float(perp.iloc[-1])
    for pos in open_pos:
        opt_series = marks.get(pos["symbol"])
        cur_val = float(opt_series.iloc[-1]) if opt_series is not None and len(opt_series) else 0
        fee = pos["notional"] * OPT_FEE_BPS / 1e4
        pnl = pos["notional"] * (cur_val - pos["entry_val"]) / pos["entry_val"] - fee
        equity += pnl
        trades.append({**pos, "exit_t": t_end, "exit_val": cur_val,
                       "pnl_usd": pnl, "exit_reason": "data_end",
                       "equity_after": equity})

    if not trades:
        print("No trades produced.")
        return

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
    print(f"  Synth-Forward → OPTIONS Execution ({UNDERLYING}, gate {ENTRY_PCT*100:.1f}%)")
    print(f"  Buy ATM call/put  |  Stop {STOP_LOSS_MULT:.0%} of premium  |"
          f"  TP at {PARTIAL_TP_MULT:.0f}x  |  Fee {OPT_FEE_BPS}bps/leg")
    print("=" * 80)
    print(f"  Trades      : {n}  (wins {wins}  losses {n-wins}  win rate {wins/n*100:.1f}%)")
    print(f"  Avg win/loss: ${avg_win:+,.0f} / ${avg_loss:+,.0f}  R:R {rr:.2f}")
    print(f"  Total PnL   : ${df['pnl_usd'].sum():+,.0f}")
    print(f"  Final equity: ${equity:,.0f}  ({(equity-START_EQUITY)/START_EQUITY*100:+.1f}%)")
    print(f"  Sharpe      : {sharpe:.2f}   Max DD: ${dd:+,.0f} ({dd/START_EQUITY*100:.1f}%)")
    print()

    monthly = df.groupby(df["exit_t"].dt.to_period("M"))["pnl_usd"].agg(["sum", "count"])
    print("  Monthly breakdown:")
    for m, row in monthly.iterrows():
        print(f"    {m}  trades={int(row['count']):>3}  pnl=${row['sum']:>+8,.0f}")
    print()

    print("  Exit reasons:")
    for reason, grp in df.groupby("exit_reason"):
        wr = (grp["pnl_usd"] > 0).mean() * 100
        print(f"    {reason:<12} {len(grp):>3} trades  win {wr:>5.1f}%  "
              f"total ${grp['pnl_usd'].sum():>+8,.0f}")
    print()

    out = DATA / "options_exec_trades.csv"
    df.to_csv(out, index=False)
    print(f"  trade log -> {out}")


if __name__ == "__main__":
    run()
