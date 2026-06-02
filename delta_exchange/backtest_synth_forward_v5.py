"""
Synthetic-Forward Strategy V5 — R:R-optimized
==============================================
v4.3 (the +29% / 3-month strategy) had a clean 2:1 R:R but only 25 trades.
v5 keeps the core signal but improves R:R by:

  * Lower gate (0.4%) but ONLY enter when signal has persisted ≥2 hours
    (filters out 1-hour spikes that mean-revert immediately).
  * Allow up to 2 concurrent positions on DIFFERENT expiries.
  * Partial profit at +1% (close half, let half run).
  * Tighter trail-lock after +0.5% (give back at most 0.25%).
  * Pyramid: if signal strengthens after entry by ≥30%, add 50% size.
  * Same stop loss at -1.5%.
  * Signal-strength-weighted sizing (0.5×–3× base).
"""

import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
from pathlib import Path
import numpy as np
import pandas as pd

from guards import (
    TradeIntent, PortfolioState, pipeline,
    max_concurrent_positions, cooldown_after_consecutive_losses,
    min_signal_strength, underlying_whitelist,
)

UNDERLYING  = os.environ.get("UNDERLYING", "BTC").upper()
PERP_SYMBOL = f"{UNDERLYING}USD"
# DATA_SUBDIR override → point at OOS / alt windows without rewiring
_data_override = os.environ.get("DATA_SUBDIR", "").strip()
if _data_override:
    DATA = Path(__file__).parent / "data" / _data_override
elif UNDERLYING == "BTC":
    DATA = Path(__file__).parent / "data"
else:
    DATA = Path(__file__).parent / "data" / UNDERLYING.lower()

# ── Config ────────────────────────────────────────────────────────────────────
ENTRY_PCT       = float(os.environ.get("ENTRY_PCT", "0.004"))   # override via env
PERSIST_HOURS   = 2           # require signal above gate for this many hours
MIN_STRIKES     = 3
PERP_FEE_BPS    = 5.0
SLIPPAGE_BPS    = 2.0
MAX_HOLD_HOURS  = 72
MIN_TT_HOURS    = 6
MAX_TT_HOURS    = 72

STOP_LOSS_PCT       = 0.015
PARTIAL_TP_PCT      = 0.010       # close half at +1%
TRAIL_PEAK_PCT      = 0.005       # once up 0.5%, trail
TRAIL_GIVEBACK_PCT  = 0.0025      # exit if we give back more than 0.25% from peak

PYRAMID_TRIGGER_PCT = 0.30        # signal must strengthen by 30% for pyramid add
PYRAMID_MULT        = 0.5         # add 50% more size

SIZE_BASE_PCT   = 0.005
SIZE_MAX_MULT   = 3.0
SIZE_MIN_MULT   = 0.5

MAX_CONCURRENT  = 2


# ── Data plumbing ─────────────────────────────────────────────────────────────
def load_perp() -> pd.Series:
    df = pd.read_csv(DATA / "perp" / f"{PERP_SYMBOL}_mark_1m.csv")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


def parse_symbol(sym: str):
    parts = sym.split("-")
    side = parts[0]
    strike = int(parts[2])
    dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
    expiry = pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")
    return side, strike, expiry


def load_option_marks() -> dict:
    out = {}
    for p in sorted((DATA / "options").glob("*_mark_1h.csv")):
        sym = p.name.replace("_mark_1h.csv", "")
        df = pd.read_csv(p)
        if df.empty: continue
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        out[sym] = df.set_index("timestamp")["close"].sort_index()
    return out


def build_index(option_marks: dict) -> pd.DataFrame:
    rows = []
    for sym in option_marks:
        try:
            side, strike, expiry = parse_symbol(sym)
        except Exception:
            continue
        rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": expiry})
    return pd.DataFrame(rows)


# ── Signal ────────────────────────────────────────────────────────────────────
def compute_pred_per_expiry(t: pd.Timestamp, spot: float,
                            catalogue: pd.DataFrame, marks: dict) -> list[dict]:
    """Return a list of {expiry, pred, n_strikes} for ALL eligible expiries at t."""
    tt_min = t + pd.Timedelta(hours=MIN_TT_HOURS)
    tt_max = t + pd.Timedelta(hours=MAX_TT_HOURS)
    eligible = catalogue[(catalogue["expiry"] > tt_min) & (catalogue["expiry"] <= tt_max)]
    out = []
    for exp in sorted(eligible["expiry"].unique()):
        same = eligible[eligible["expiry"] == exp]
        calls = same[same["side"] == "C"].set_index("strike")
        puts  = same[same["side"] == "P"].set_index("strike")
        common = sorted(set(calls.index) & set(puts.index))
        common_near = [K for K in common if abs(K - spot) / spot <= 0.05]
        if len(common_near) < MIN_STRIKES:
            continue
        devs = []
        for K in common_near:
            c = marks.get(calls.loc[K, "symbol"])
            p = marks.get(puts.loc[K, "symbol"])
            if c is None or p is None: continue
            if t not in c.index or t not in p.index: continue
            c_px = float(c.loc[t]); p_px = float(p.loc[t])
            if c_px <= 0 or p_px <= 0: continue
            syn_F = c_px - p_px + K
            devs.append((syn_F - spot) / spot)
        if len(devs) < MIN_STRIKES:
            continue
        pos = sum(1 for d in devs if d > 0)
        neg = sum(1 for d in devs if d < 0)
        if pos < MIN_STRIKES and neg < MIN_STRIKES:
            continue
        out.append({"expiry": exp, "pred": float(np.median(devs)), "n_strikes": len(devs)})
    return out


# ── Backtest engine ───────────────────────────────────────────────────────────
def run() -> None:
    print("Loading data...")
    perp = load_perp()
    marks = load_option_marks()
    catalogue = build_index(marks)
    print(f"  perp 1m bars : {len(perp):,}")
    print(f"  option marks : {len(marks):,} symbols, {catalogue['expiry'].nunique()} expiries")
    hours = perp.index[(perp.index.minute == 0) & (perp.index.second == 0)]
    print(f"  decision pts : {len(hours):,} hourly samples")
    print()

    equity_usd  = 10_000.0
    state = PortfolioState(equity_usd=equity_usd)
    guards = [
        underlying_whitelist({UNDERLYING}),
        max_concurrent_positions(MAX_CONCURRENT),
        cooldown_after_consecutive_losses(3, cooldown_hours=24),
        min_signal_strength(min_gap_pp=ENTRY_PCT * 100),
    ]

    open_positions = []   # list of dicts
    trades = []
    equity_curve = []
    rejections = {"none_eligible": 0, "below_gate": 0, "no_persistence": 0,
                  "guard": 0, "already_in_expiry": 0}

    # signal history per expiry — used for persistence + pyramid checks
    sig_history = {}   # expiry → list[(t, pred)]

    print("Walking decision points...")
    last_pct = -1
    for i, t in enumerate(hours):
        pct = i * 100 // len(hours)
        if pct != last_pct and pct % 20 == 0 and pct > 0:
            print(f"  ... {pct}%  open: {len(open_positions)}  closed: {len(trades)}", flush=True)
            last_pct = pct

        spot = float(perp.loc[t])
        equity_curve.append((t, equity_usd))

        # ── Update signal history ──────────────────────────────────────────
        preds_now = compute_pred_per_expiry(t, spot, catalogue, marks)
        for p in preds_now:
            sig_history.setdefault(p["expiry"], []).append((t, p["pred"]))
        # trim each history to last 6 hours
        for exp in list(sig_history.keys()):
            sig_history[exp] = [(ti, pi) for ti, pi in sig_history[exp]
                                 if (t - ti).total_seconds() <= 6 * 3600]

        # ── Manage open positions ──────────────────────────────────────────
        still_open = []
        for pos in open_positions:
            held_h = (t - pos["entry_t"]).total_seconds() / 3600
            side = pos["side"]
            entry_px = pos["entry_px"]
            unreal_ret = side * (spot - entry_px) / entry_px
            pos["peak_ret"] = max(pos.get("peak_ret", 0.0), unreal_ret)

            # partial profit-take at +1% (only once)
            if (not pos.get("tp_taken")) and unreal_ret >= PARTIAL_TP_PCT:
                half_notional = pos["notional"] * 0.5
                fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fill_px - entry_px) / entry_px
                pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
                pnl_usd = half_notional * pnl_pct
                equity_usd += pnl_usd
                state.equity_usd = equity_usd
                pos["notional"] -= half_notional
                pos["tp_taken"] = True
                # log as a separate trade-leg
                trades.append({**pos, "exit_t": t, "exit_px": fill_px,
                               "ret": ret, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                               "notional": half_notional,
                               "exit_reason": "partial_tp", "equity_after": equity_usd})

            # decide full exit
            exit_now = False
            reason = ""
            if t >= pos["expiry"]:
                exit_now = True; reason = "expiry"
            elif held_h >= MAX_HOLD_HOURS:
                exit_now = True; reason = "max_hold"
            elif unreal_ret < -STOP_LOSS_PCT:
                exit_now = True; reason = "stop_loss"
            elif pos["peak_ret"] >= TRAIL_PEAK_PCT and \
                 (pos["peak_ret"] - unreal_ret) > TRAIL_GIVEBACK_PCT:
                exit_now = True; reason = "trail"

            if exit_now:
                fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fill_px - entry_px) / entry_px
                pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
                pnl_usd = pos["notional"] * pnl_pct
                equity_usd += pnl_usd
                state.equity_usd = equity_usd
                state.last_n_pnls.append(pnl_usd)
                trades.append({**pos, "exit_t": t, "exit_px": fill_px,
                               "ret": ret, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                               "exit_reason": reason, "equity_after": equity_usd})
                continue   # don't keep this position

            # pyramid check
            cur_pred_for_exp = next((p["pred"] for p in preds_now if p["expiry"] == pos["expiry"]), None)
            if (cur_pred_for_exp is not None and not pos.get("pyramided")
                and abs(cur_pred_for_exp) >= abs(pos["pred_pct"]) * (1 + PYRAMID_TRIGGER_PCT)
                and np.sign(cur_pred_for_exp) == side):
                add_notional = pos["notional"] * PYRAMID_MULT
                pos["notional"] += add_notional
                pos["pyramided"] = True
                # entry_px on the added portion is the current price — average up
                # we keep entry_px conservatively unchanged (under-counts upside on the add)

            still_open.append(pos)

        open_positions = still_open
        state.open_positions = len(open_positions)

        # ── Entry consideration ────────────────────────────────────────────
        if not preds_now:
            rejections["none_eligible"] += 1
            continue
        already_in = {p["expiry"] for p in open_positions}
        if len(open_positions) >= MAX_CONCURRENT:
            continue

        # pick best signal: strongest |pred| not already held
        candidates = sorted(preds_now, key=lambda p: -abs(p["pred"]))
        chosen = None
        for cand in candidates:
            if cand["expiry"] in already_in:
                continue
            if abs(cand["pred"]) < ENTRY_PCT:
                rejections["below_gate"] += 1
                break  # sorted by strength; lower ones won't pass either
            # persistence check: signal for this expiry must have been above gate
            # for PERSIST_HOURS in the recent history
            hist = sig_history.get(cand["expiry"], [])
            recent = [pi for ti, pi in hist if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
            if len(recent) < PERSIST_HOURS:
                rejections["no_persistence"] += 1
                continue
            same_sign = sum(1 for pi in recent if np.sign(pi) == np.sign(cand["pred"]))
            if same_sign < PERSIST_HOURS:
                rejections["no_persistence"] += 1
                continue
            chosen = cand
            break

        if chosen is None:
            continue

        pred = chosen["pred"]
        side = 1 if pred > 0 else -1
        intent = TradeIntent(
            timestamp=t, structure="synth_forward_perp_v5", underlying=UNDERLYING,
            risk_usd=equity_usd * 0.05,
            notional_usd=equity_usd,
            iv_rv_gap_pp=pred * 100,
        )
        reason = pipeline(intent, state, guards)
        if reason is not None:
            rejections["guard"] += 1
            continue
        fill_px = spot * (1 + side * SLIPPAGE_BPS / 1e4)
        size_mult = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(pred) / SIZE_BASE_PCT))
        open_positions.append({
            "entry_t": t, "entry_px": fill_px, "side": side,
            "expiry": chosen["expiry"],
            "notional": equity_usd * size_mult,
            "size_mult": size_mult,
            "pred_pct": pred,
            "n_strikes": chosen["n_strikes"],
            "peak_ret": 0.0,
        })
        state.open_positions = len(open_positions)

    # close any leftover open
    for pos in open_positions:
        side = pos["side"]; entry_px = pos["entry_px"]
        t_end = perp.index[-1]; spot = float(perp.loc[t_end])
        fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
        ret = side * (fill_px - entry_px) / entry_px
        pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
        pnl_usd = pos["notional"] * pnl_pct
        equity_usd += pnl_usd
        trades.append({**pos, "exit_t": t_end, "exit_px": fill_px,
                       "ret": ret, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                       "exit_reason": "data_end", "equity_after": equity_usd})

    if not trades:
        print("\nNo trades produced. Rejections:", rejections)
        return

    df = pd.DataFrame(trades)
    df["entry_t"] = pd.to_datetime(df["entry_t"], utc=True)
    df["exit_t"]  = pd.to_datetime(df["exit_t"], utc=True)
    df = df.sort_values("exit_t").reset_index(drop=True)

    n = len(df)
    wins   = (df["pnl_usd"] > 0).sum()
    losses = (df["pnl_usd"] <= 0).sum()
    avg_win  = df.loc[df["pnl_usd"] > 0, "pnl_usd"].mean() if wins  else 0
    avg_loss = df.loc[df["pnl_usd"] <= 0, "pnl_usd"].mean() if losses else 0
    rr = abs(avg_win / avg_loss) if avg_loss else float("nan")
    total_pnl = df["pnl_usd"].sum()
    eq = pd.Series([e for _, e in equity_curve], index=[t for t, _ in equity_curve])
    eq_change = eq.iloc[-1] - eq.iloc[0]
    dd = (eq - eq.cummax()).min()
    daily = eq.resample("1D").last().dropna().diff().dropna()
    sharpe = daily.mean() / daily.std() * math.sqrt(365) if daily.std() > 0 else 0.0

    print()
    print("=" * 100)
    print("  Synthetic-Forward V5 — R:R-optimized")
    print(f"  Gate {ENTRY_PCT*100:.2f}%  persistence ≥{PERSIST_HOURS}h  max_conc {MAX_CONCURRENT}  "
          f"stop {STOP_LOSS_PCT*100:.1f}%  partial_tp {PARTIAL_TP_PCT*100:.1f}%  trail {TRAIL_GIVEBACK_PCT*100:.2f}%")
    print(f"  Pyramid +{PYRAMID_MULT*100:.0f}% when signal strengthens {PYRAMID_TRIGGER_PCT*100:.0f}%   "
          f"Sizing {SIZE_MIN_MULT}–{SIZE_MAX_MULT}× by strength")
    print("=" * 100)
    print(f"  trade-legs    : {n}     wins {wins}   losses {losses}   "
          f"win rate {wins/n*100:.1f}%")
    print(f"  avg win       : ${avg_win:+,.0f}   avg loss: ${avg_loss:+,.0f}   "
          f"R:R = {rr:.2f}")
    print(f"  total PnL     : ${total_pnl:+,.0f}   equity: ${eq.iloc[-1]:,.0f}   "
          f"({eq_change/10_000*100:+.1f}% on $10k)")
    print(f"  Sharpe (daily): {sharpe:.2f}     max DD: ${dd:+,.0f}   "
          f"({dd/10_000*100:.1f}%)")
    print()
    print(f"  rejections    : {rejections}")
    print()

    monthly = eq.resample("ME").last()
    print("  Monthly equity:")
    for dt, v in monthly.items():
        print(f"    {dt.strftime('%Y-%m'):<8} ${v:>10,.0f}")
    print()

    # exit-reason breakdown
    print("  Exits by reason:")
    print(df["exit_reason"].value_counts().to_string())
    print()

    out = DATA / "v5_trades.csv"
    df.to_csv(out, index=False)
    print(f"  trade log → {out.relative_to(DATA.parent)}")


if __name__ == "__main__":
    run()
