"""
Synthetic-Forward V6 — Leveraged v5 with vol-regime gating
==========================================================
Same signal + execution as v5, with two new dials:

  1. LEVERAGE_MULT: base leverage applied on top of v5's signal-strength
     sizing. Cap total open notional via MAX_TOTAL_EXPOSURE_MULT to
     prevent runaway concentration.

  2. Vol-regime cap: when 7d realized vol is in the top tercile,
     halve effective leverage (signal-to-noise degrades in chop).

Stops and trail are still % of position, so absolute $ risk scales linearly
with leverage. v5 had max DD = 3.9% at 1× → 2× ≈ 7.8%, 3× ≈ 11.7%.

Run modes:
  Just call run(leverage=X) — default sweeps 1×, 2×, 3×.
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
DATA = (Path(__file__).parent / "data") if UNDERLYING == "BTC" \
       else (Path(__file__).parent / "data" / UNDERLYING.lower())

# ── Same v5 dials ─────────────────────────────────────────────────────────────
ENTRY_PCT       = float(os.environ.get("ENTRY_PCT", "0.004"))
PERSIST_HOURS   = 2
MIN_STRIKES     = 3
PERP_FEE_BPS    = 5.0
SLIPPAGE_BPS    = 2.0
MAX_HOLD_HOURS  = 72
MIN_TT_HOURS    = 6
MAX_TT_HOURS    = 72

STOP_LOSS_PCT       = 0.015
PARTIAL_TP_PCT      = 0.010
TRAIL_PEAK_PCT      = 0.005
TRAIL_GIVEBACK_PCT  = 0.0025

PYRAMID_TRIGGER_PCT = 0.30
PYRAMID_MULT        = 0.5

SIZE_BASE_PCT   = 0.005
SIZE_MAX_MULT   = 3.0
SIZE_MIN_MULT   = 0.5
MAX_CONCURRENT  = 2

# ── New v6 dials ─────────────────────────────────────────────────────────────
RV_LOOKBACK_MIN          = 7 * 24 * 60
RV_HALVE_PERCENTILE      = 0.67    # halve leverage when 7d RV > this percentile
MAX_TOTAL_EXPOSURE_MULT  = 5.0     # cap total open notional / equity


# ── Data plumbing ─────────────────────────────────────────────────────────────
def load_perp() -> pd.DataFrame:
    df = pd.read_csv(DATA / "perp" / f"{PERP_SYMBOL}_mark_1m.csv")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df[["timestamp", "close"]].set_index("timestamp").sort_index()
    df["logret"] = np.log(df["close"]).diff()
    df["rv7d"]   = df["logret"].rolling(RV_LOOKBACK_MIN).std() * math.sqrt(365 * 24 * 60)
    return df


def parse_symbol(sym: str):
    parts = sym.split("-")
    side = parts[0]; strike = int(parts[2])
    dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
    return side, strike, pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")


def load_option_marks() -> dict:
    out = {}
    for p in sorted((DATA / "options").glob("*_mark_1h.csv")):
        sym = p.name.replace("_mark_1h.csv", "")
        df = pd.read_csv(p)
        if df.empty: continue
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        out[sym] = df.set_index("timestamp")["close"].sort_index()
    return out


def build_index(option_marks):
    rows = []
    for sym in option_marks:
        try: side, strike, expiry = parse_symbol(sym)
        except Exception: continue
        rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": expiry})
    return pd.DataFrame(rows)


def compute_pred_per_expiry(t, spot, catalogue, marks):
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
        if len(common_near) < MIN_STRIKES: continue
        devs = []
        for K in common_near:
            c = marks.get(calls.loc[K, "symbol"])
            p = marks.get(puts.loc[K, "symbol"])
            if c is None or p is None: continue
            if t not in c.index or t not in p.index: continue
            cp = float(c.loc[t]); pp = float(p.loc[t])
            if cp <= 0 or pp <= 0: continue
            syn_F = cp - pp + K
            devs.append((syn_F - spot) / spot)
        if len(devs) < MIN_STRIKES: continue
        pos = sum(1 for d in devs if d > 0); neg = sum(1 for d in devs if d < 0)
        if pos < MIN_STRIKES and neg < MIN_STRIKES: continue
        out.append({"expiry": exp, "pred": float(np.median(devs)), "n_strikes": len(devs)})
    return out


# ── Backtest engine ───────────────────────────────────────────────────────────
def run(leverage: float = 1.0, verbose: bool = True):
    perp = load_perp()
    marks = load_option_marks()
    catalogue = build_index(marks)
    hours = perp.index[(perp.index.minute == 0) & (perp.index.second == 0)]

    # precompute RV regime threshold
    rv_threshold = perp["rv7d"].quantile(RV_HALVE_PERCENTILE)

    equity_usd = 10_000.0
    state = PortfolioState(equity_usd=equity_usd)
    guards = [
        underlying_whitelist({UNDERLYING}),
        max_concurrent_positions(MAX_CONCURRENT),
        cooldown_after_consecutive_losses(3, cooldown_hours=24),
        min_signal_strength(min_gap_pp=ENTRY_PCT * 100),
    ]

    open_positions = []
    trades = []
    equity_curve = []
    sig_history = {}

    for i, t in enumerate(hours):
        spot = float(perp.loc[t, "close"])
        rv   = perp.loc[t, "rv7d"]
        equity_curve.append((t, equity_usd))

        # signal history
        preds_now = compute_pred_per_expiry(t, spot, catalogue, marks)
        for p in preds_now:
            sig_history.setdefault(p["expiry"], []).append((t, p["pred"]))
        for exp in list(sig_history.keys()):
            sig_history[exp] = [(ti, pi) for ti, pi in sig_history[exp]
                                 if (t - ti).total_seconds() <= 6 * 3600]

        # manage open positions (same as v5)
        still_open = []
        for pos in open_positions:
            held_h = (t - pos["entry_t"]).total_seconds() / 3600
            side = pos["side"]
            entry_px = pos["entry_px"]
            unreal_ret = side * (spot - entry_px) / entry_px
            pos["peak_ret"] = max(pos.get("peak_ret", 0.0), unreal_ret)

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
                trades.append({**pos, "exit_t": t, "exit_px": fill_px,
                               "ret": ret, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                               "notional": half_notional,
                               "exit_reason": "partial_tp", "equity_after": equity_usd})

            exit_now, reason = False, ""
            if t >= pos["expiry"]:
                exit_now, reason = True, "expiry"
            elif held_h >= MAX_HOLD_HOURS:
                exit_now, reason = True, "max_hold"
            elif unreal_ret < -STOP_LOSS_PCT:
                exit_now, reason = True, "stop_loss"
            elif pos["peak_ret"] >= TRAIL_PEAK_PCT and \
                 (pos["peak_ret"] - unreal_ret) > TRAIL_GIVEBACK_PCT:
                exit_now, reason = True, "trail"

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
                continue

            still_open.append(pos)

        open_positions = still_open
        state.open_positions = len(open_positions)

        # entry consideration
        if not preds_now: continue
        if len(open_positions) >= MAX_CONCURRENT: continue
        already_in = {p["expiry"] for p in open_positions}

        candidates = sorted(preds_now, key=lambda p: -abs(p["pred"]))
        chosen = None
        for cand in candidates:
            if cand["expiry"] in already_in: continue
            if abs(cand["pred"]) < ENTRY_PCT: break
            hist = sig_history.get(cand["expiry"], [])
            recent = [pi for ti, pi in hist if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
            if len(recent) < PERSIST_HOURS: continue
            if sum(1 for pi in recent if np.sign(pi) == np.sign(cand["pred"])) < PERSIST_HOURS:
                continue
            chosen = cand; break
        if chosen is None: continue

        pred = chosen["pred"]
        side = 1 if pred > 0 else -1
        intent = TradeIntent(timestamp=t, structure="synth_forward_perp_v6",
                              underlying=UNDERLYING, risk_usd=equity_usd * 0.05,
                              notional_usd=equity_usd, iv_rv_gap_pp=pred * 100)
        reason = pipeline(intent, state, guards)
        if reason is not None: continue

        # ── leverage + vol-regime adjustment ────────────────────────────────
        eff_leverage = leverage
        if pd.notna(rv) and rv > rv_threshold:
            eff_leverage = leverage * 0.5    # halve in high-vol regime

        fill_px = spot * (1 + side * SLIPPAGE_BPS / 1e4)
        size_mult = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(pred) / SIZE_BASE_PCT))
        proposed_notional = equity_usd * size_mult * eff_leverage

        # cap total open notional
        existing_notional = sum(p["notional"] for p in open_positions)
        room = max(0.0, equity_usd * MAX_TOTAL_EXPOSURE_MULT - existing_notional)
        notional = min(proposed_notional, room)
        if notional < 0.1 * equity_usd:    # too small to bother
            continue

        open_positions.append({
            "entry_t": t, "entry_px": fill_px, "side": side,
            "expiry": chosen["expiry"], "notional": notional,
            "size_mult": size_mult, "eff_leverage": eff_leverage,
            "pred_pct": pred, "n_strikes": chosen["n_strikes"],
            "peak_ret": 0.0,
        })
        state.open_positions = len(open_positions)

    # close leftover open
    for pos in open_positions:
        side = pos["side"]; entry_px = pos["entry_px"]
        t_end = perp.index[-1]; spot = float(perp.loc[t_end, "close"])
        fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
        ret = side * (fill_px - entry_px) / entry_px
        pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
        pnl_usd = pos["notional"] * pnl_pct
        equity_usd += pnl_usd
        trades.append({**pos, "exit_t": t_end, "exit_px": fill_px,
                       "ret": ret, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                       "exit_reason": "data_end", "equity_after": equity_usd})

    if not trades:
        return {"trades": 0, "leverage": leverage, "net_pct": 0.0,
                "sharpe": 0.0, "max_dd_pct": 0.0, "win_rate": 0.0}

    df = pd.DataFrame(trades)
    df["entry_t"] = pd.to_datetime(df["entry_t"], utc=True)
    df["exit_t"]  = pd.to_datetime(df["exit_t"], utc=True)
    n = len(df); wins = (df["pnl_usd"] > 0).sum()
    avg_win = df.loc[df["pnl_usd"] > 0, "pnl_usd"].mean() if wins else 0
    avg_loss = df.loc[df["pnl_usd"] <= 0, "pnl_usd"].mean() if (n - wins) else 0
    rr = abs(avg_win / avg_loss) if avg_loss else float("nan")
    eq = pd.Series([e for _, e in equity_curve], index=[t for t, _ in equity_curve])
    daily = eq.resample("1D").last().dropna().diff().dropna()
    sharpe = daily.mean() / daily.std() * math.sqrt(365) if daily.std() > 0 else 0.0
    dd = (eq - eq.cummax()).min()

    monthly = eq.resample("ME").last()

    res = {
        "leverage": leverage, "trades": n, "wins": int(wins),
        "win_rate": wins/n*100, "rr": rr,
        "net_pct": (equity_usd - 10_000) / 10_000 * 100,
        "final_equity": equity_usd, "sharpe": sharpe,
        "max_dd_pct": dd / 10_000 * 100,
        "monthly": {k.strftime("%Y-%m"): float(v) for k, v in monthly.items()},
    }
    return res


def main():
    print("Loading data + running 1×, 2×, 3× leverage sweep on v6...")
    print()
    results = {}
    for lev in [1.0, 2.0, 3.0]:
        print(f"  running leverage {lev}×...", flush=True)
        results[lev] = run(leverage=lev)
        print(f"    → {results[lev]['trades']} trades  "
              f"net {results[lev]['net_pct']:+.1f}%  "
              f"Sharpe {results[lev]['sharpe']:.2f}  "
              f"max DD {results[lev]['max_dd_pct']:.1f}%")

    print()
    print("=" * 100)
    print("  V6 LEVERAGE SWEEP — v5 signal + vol-regime gated leverage")
    print(f"  Vol-halve trigger: 7d RV > {RV_HALVE_PERCENTILE*100:.0f}th percentile")
    print(f"  Max total open notional / equity: {MAX_TOTAL_EXPOSURE_MULT}×")
    print("=" * 100)
    print(f"  {'Leverage':<10} {'Trades':>7} {'Win%':>6} {'R:R':>5} "
          f"{'Net%':>8} {'FinalEq':>10} {'Sharpe':>7} {'MaxDD%':>7}")
    print("  " + "-" * 90)
    for lev, r in results.items():
        print(f"  {lev}×{'':<8} {r['trades']:>7} {r['win_rate']:>5.1f}% "
              f"{r['rr']:>5.2f} {r['net_pct']:>+7.1f}% "
              f"${r['final_equity']:>9,.0f} {r['sharpe']:>7.2f} {r['max_dd_pct']:>+6.1f}%")
    print()
    print("  Monthly equity progression (each leverage tier):")
    months = sorted(set().union(*[r["monthly"].keys() for r in results.values()]))
    print(f"  {'Month':<10} " + " ".join(f"{f'{lev}×':>12}" for lev in results))
    for m in months:
        row = f"  {m:<10} "
        for lev, r in results.items():
            v = r["monthly"].get(m, np.nan)
            row += f" ${v:>10,.0f} " if np.isfinite(v) else f" {'-':>12}"
        print(row)
    print()


if __name__ == "__main__":
    main()
