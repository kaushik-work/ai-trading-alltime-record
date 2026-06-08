"""
Signal-Timed Long Straddle Backtest
=====================================
Uses the synth-forward parity MAGNITUDE (not direction) to time straddle entries.
When |pred| > ENTRY_PCT across ≥ MIN_STRIKES near-money strikes, it means
options are mispriced — likely preceding a large move in either direction.

Entry: buy 1 ATM call + 1 ATM put (long straddle)
Profit: if underlying moves beyond breakeven in either direction
Max loss: premium paid (known upfront)

Exit rules (on total straddle value):
  stop_loss   : straddle worth < STOP_MULT × premium paid
  partial_tp  : straddle worth > PARTIAL_TP_MULT × premium → close half
  trail       : after TRAIL_TRIGGER_MULT, trail at TRAIL_GIVEBACK from peak
  expiry      : settle at intrinsic
  max_hold    : force exit after MAX_HOLD_HOURS

Costs: OPT_FEE_BPS per leg (entry + exit, so 4 fills total per straddle).

Usage:
  .venv/Scripts/python backtest_straddle_timed.py
  UNDERLYING=ETH .venv/Scripts/python backtest_straddle_timed.py
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

# ── Config ────────────────────────────────────────────────────────────────────
ENTRY_PCT       = float(os.environ.get("ENTRY_PCT", "0.006"))  # signal gate
PERSIST_HOURS   = 2
MIN_STRIKES     = 3
MONEYNESS       = 0.05
TT_MIN_HOURS    = 24          # need at least 1 day for straddle to play out
TT_MAX_HOURS    = 168         # max 7 days out
MAX_HOLD_HOURS  = 48
OPT_FEE_BPS     = 25.0        # per leg

# Exit rules on TOTAL straddle value (call_val + put_val)
STOP_MULT           = 0.40    # exit if straddle < 40% of premium paid
PARTIAL_TP_MULT     = 2.00    # close half at 2× premium
TRAIL_TRIGGER_MULT  = 1.60    # start trailing at 60% gain
TRAIL_GIVEBACK_MULT = 0.15    # give back 15% from peak before exit

START_EQUITY    = 10_000.0
RISK_PCT        = 0.05        # risk 5% of equity per straddle (premium is known max loss)
MAX_CONCURRENT  = 2


# ── Data ─────────────────────────────────────────────────────────────────────
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


# ── Signal — magnitude only ──────────────────────────────────────────────────
def compute_signal_magnitude(t, spot, catalogue, marks):
    """Returns (magnitude, expiry, n_strikes) or None. Direction ignored."""
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
        mag = abs(float(np.median(devs)))
        candidates.append({"expiry": exp, "mag": mag, "n_strikes": len(devs),
                           "pred": float(np.median(devs))})
    if not candidates: return None
    best = max(candidates, key=lambda c: c["mag"])
    if best["mag"] < ENTRY_PCT: return None
    return best


def find_atm_straddle(t, spot, catalogue, marks, expiry):
    """Find ATM strike with valid call+put marks. Returns (call_sym, put_sym, strike, call_px, put_px)."""
    same = catalogue[catalogue["expiry"] == expiry].copy()
    same["dist"] = (same["strike"] - spot).abs()
    strikes_sorted = same.sort_values("dist")["strike"].unique()
    for K in strikes_sorted:
        c_row = same[(same["side"] == "C") & (same["strike"] == K)]
        p_row = same[(same["side"] == "P") & (same["strike"] == K)]
        if c_row.empty or p_row.empty: continue
        c_sym, p_sym = c_row.iloc[0]["symbol"], p_row.iloc[0]["symbol"]
        cs, ps = marks.get(c_sym), marks.get(p_sym)
        if cs is None or ps is None: continue
        if t not in cs.index or t not in ps.index: continue
        cp, pp = float(cs.loc[t]), float(ps.loc[t])
        if cp <= 0 or pp <= 0: continue
        return c_sym, p_sym, int(K), cp, pp
    return None, None, None, None, None


# ── Backtest ──────────────────────────────────────────────────────────────────
def run():
    print(f"Loading {UNDERLYING} data...")
    perp  = load_perp()
    marks = load_option_marks()
    cat   = build_index(marks)
    hours = perp.index[(perp.index.minute == 0) & (perp.index.second == 0)]
    print(f"  perp 1m bars : {len(perp):,}")
    print(f"  option marks : {len(marks):,} symbols, {cat['expiry'].nunique()} expiries")
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

        sig = compute_signal_magnitude(t, spot, cat, marks)
        if sig:
            sig_history.setdefault(sig["expiry"], []).append((t, sig["mag"]))
        for exp in list(sig_history):
            sig_history[exp] = [(ti, p) for ti, p in sig_history[exp]
                                if (t - ti).total_seconds() <= 6 * 3600]

        # manage open straddles
        still_open = []
        for pos in open_pos:
            cs = marks.get(pos["call_sym"])
            ps = marks.get(pos["put_sym"])
            if cs is None or ps is None or t not in cs.index or t not in ps.index:
                still_open.append(pos); continue

            call_val = float(cs.loc[t])
            put_val  = float(ps.loc[t])
            # intrinsic floor at 0
            call_val = max(call_val, 0.0)
            put_val  = max(put_val,  0.0)
            total_val  = call_val + put_val
            entry_cost = pos["entry_cost"]   # call_px + put_px at entry
            peak_val   = pos.get("peak_val", entry_cost)
            pos["peak_val"] = max(peak_val, total_val)
            held_h = (t - pos["entry_t"]).total_seconds() / 3600

            # partial TP — straddle doubled
            if not pos.get("tp_taken") and total_val >= entry_cost * PARTIAL_TP_MULT:
                half_n  = pos["notional"] * 0.5
                fee     = half_n * 2 * OPT_FEE_BPS / 1e4   # 2 legs
                gain    = half_n * (total_val - entry_cost) / entry_cost
                pnl     = gain - fee
                equity += pnl
                pos["notional"] -= half_n
                pos["tp_taken"]  = True
                trades.append({**pos, "exit_t": t, "total_val": total_val,
                               "pnl_usd": pnl, "exit_reason": "partial_tp",
                               "notional": half_n, "equity_after": equity})

            exit_reason = None
            if t >= pos["expiry"]:
                exit_reason = "expiry"
            elif held_h >= MAX_HOLD_HOURS:
                exit_reason = "max_hold"
            elif total_val < entry_cost * STOP_MULT:
                exit_reason = "stop_loss"
            elif pos["peak_val"] >= entry_cost * TRAIL_TRIGGER_MULT and \
                 total_val < pos["peak_val"] * (1 - TRAIL_GIVEBACK_MULT):
                exit_reason = "trail"

            if exit_reason:
                fee   = pos["notional"] * 2 * OPT_FEE_BPS / 1e4
                gain  = pos["notional"] * (total_val - entry_cost) / entry_cost
                pnl   = gain - fee
                equity += pnl
                trades.append({**pos, "exit_t": t, "total_val": total_val,
                               "pnl_usd": pnl, "exit_reason": exit_reason,
                               "equity_after": equity})
            else:
                still_open.append(pos)

        open_pos = still_open

        # entry
        if sig is None or len(open_pos) >= MAX_CONCURRENT: continue
        already_in = {p["expiry"] for p in open_pos}
        if sig["expiry"] in already_in: continue

        # persistence: signal must have been above gate for PERSIST_HOURS
        hist   = sig_history.get(sig["expiry"], [])
        recent = [p for ti, p in hist if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
        if len(recent) < PERSIST_HOURS: continue

        c_sym, p_sym, strike, cp, pp = find_atm_straddle(t, spot, cat, marks, sig["expiry"])
        if c_sym is None: continue

        entry_cost = cp + pp         # total premium = max loss
        # size so that max loss = RISK_PCT of equity
        notional   = (equity * RISK_PCT) / (entry_cost / (cp + pp + 1e-9))
        notional   = min(notional, equity * 0.15)  # hard cap at 15% of equity
        entry_fee  = notional * 2 * OPT_FEE_BPS / 1e4
        equity    -= entry_fee

        tte_h = (sig["expiry"] - t).total_seconds() / 3600
        open_pos.append({
            "entry_t": t, "call_sym": c_sym, "put_sym": p_sym,
            "strike": strike, "expiry": sig["expiry"],
            "spot_entry": spot, "call_px": cp, "put_px": pp,
            "entry_cost": entry_cost, "peak_val": entry_cost,
            "notional": notional, "mag": sig["mag"],
            "n_strikes": sig["n_strikes"], "tte_h": tte_h,
        })

    # close remaining at last available mark
    for pos in open_pos:
        cs, ps = marks.get(pos["call_sym"]), marks.get(pos["put_sym"])
        cv = float(cs.iloc[-1]) if cs is not None and len(cs) else 0
        pv = float(ps.iloc[-1]) if ps is not None and len(ps) else 0
        total_val = max(cv, 0) + max(pv, 0)
        fee  = pos["notional"] * 2 * OPT_FEE_BPS / 1e4
        gain = pos["notional"] * (total_val - pos["entry_cost"]) / pos["entry_cost"]
        pnl  = gain - fee
        equity += pnl
        trades.append({**pos, "exit_t": hours[-1], "total_val": total_val,
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
    print(f"  Signal-Timed Long Straddle — {UNDERLYING}  (gate {ENTRY_PCT*100:.1f}%)")
    print(f"  Buy ATM call + put  |  Max loss = premium paid  |"
          f"  Risk {RISK_PCT:.0%}/trade  |  Fee {OPT_FEE_BPS}bps/leg")
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

    # avg entry stats
    print(f"\n  Avg premium paid : ${df['entry_cost'].mean():,.0f}")
    print(f"  Avg TTE at entry : {df['tte_h'].mean():.0f}h")
    print(f"  Avg signal mag   : {df['mag'].mean()*100:.2f}%")

    out = DATA / "straddle_timed_trades.csv"
    df.to_csv(out, index=False)
    print(f"\n  trade log -> {out}")


if __name__ == "__main__":
    run()
