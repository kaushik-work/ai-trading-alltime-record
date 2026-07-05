"""Sweep with Greek-based filters + leverage 1x-5x.

Strategy: keep the INVERTED 4:1 R:R approach (the only thing that came
close to profit). Filter entries on regime conditions:
  - Realized vol (low RV = range trading favored, SL less likely to hit)
  - Implied vol from ATM straddle (Black-Scholes inversion)
  - Theta proxy (1/TTE — close to expiry = higher decay)
  - OI direction (1h OI change for the chosen expiry's strikes)
Then sweep leverage 1x..5x.

Goal: 80-85% WR with positive net edge after fees.
"""
from __future__ import annotations
import sys, os
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from scipy.stats import norm
from scipy.optimize import brentq

from backtest_engine import (
    load_data, load_hl, compute_pred,
    USD_INR_RATE, PERSIST_HOURS, MIN_STRIKES,
    TT_MIN_HOURS, TT_MAX_HOURS, MONEYNESS,
    SIZE_BASE_PCT, SIZE_MIN_MULT, SIZE_MAX_MULT,
    CAPITAL_USE_PCT, MAX_CONCURRENT, MAX_HOLD_HOURS,
    PERP_FEE_BPS, SLIPPAGE_BPS, MAINT_MARGIN_PCT,
)


# ── Black-Scholes (no dividend, r=0 for crypto perps) ────────────────────────
def bs_call(S, K, T, sigma, r=0.0):
    if T <= 0 or sigma <= 0: return max(S - K, 0)
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    return S*norm.cdf(d1) - K*np.exp(-r*T)*norm.cdf(d2)


def implied_vol(price, S, K, T, r=0.0, is_call=True):
    """Solve sigma from option price. Returns None on failure."""
    if price <= 0 or T <= 0: return None
    intrinsic = max(S - K, 0) if is_call else max(K - S, 0)
    if price < intrinsic - 0.01: return None
    target = lambda s: (bs_call(S, K, T, s, r) if is_call
                        else bs_call(S, K, T, s, r) - S + K*np.exp(-r*T)) - price
    try:
        return brentq(target, 1e-4, 5.0, maxiter=50, xtol=1e-4)
    except Exception:
        return None


def compute_atm_iv(t, spot, marks_dict, strike_calls, strike_puts, expiry):
    """Pick ATM strike, average call & put IV."""
    atm_K = min(set(strike_calls.index) & set(strike_puts.index),
                key=lambda K: abs(K - spot), default=None)
    if atm_K is None: return None
    c_series = marks_dict.get(strike_calls.loc[atm_K, "symbol"])
    p_series = marks_dict.get(strike_puts.loc[atm_K, "symbol"])
    if c_series is None or p_series is None: return None
    if t not in c_series.index or t not in p_series.index: return None
    cp, pp = float(c_series.loc[t]), float(p_series.loc[t])
    tte_years = (expiry - t).total_seconds() / (3600 * 24 * 365)
    iv_c = implied_vol(cp, spot, atm_K, tte_years, is_call=True)
    iv_p = implied_vol(pp, spot, atm_K, tte_years, is_call=False)
    if iv_c is None and iv_p is None: return None
    if iv_c is None: return iv_p
    if iv_p is None: return iv_c
    return (iv_c + iv_p) / 2


def compute_realized_vol(perp_series, t, window_hours=24):
    """Annualized realized vol over last window_hours of 1m bars."""
    cutoff = t - pd.Timedelta(hours=window_hours)
    window = perp_series[(perp_series.index >= cutoff) & (perp_series.index <= t)]
    if len(window) < 30: return None
    rets = window.pct_change().dropna()
    if len(rets) < 30: return None
    # annualize 1m vol: sqrt(365 * 24 * 60)
    return float(rets.std() * np.sqrt(365 * 24 * 60))


def compute_pred_extended(t, spot, catalogue, marks):
    """Same as compute_pred but also returns per-expiry call/put dicts for IV."""
    tt_min = t + pd.Timedelta(hours=TT_MIN_HOURS)
    tt_max = t + pd.Timedelta(hours=TT_MAX_HOURS)
    eligible = catalogue[(catalogue["expiry"] > tt_min) & (catalogue["expiry"] <= tt_max)]
    out = []
    for exp in sorted(eligible["expiry"].unique()):
        same = eligible[eligible["expiry"] == exp]
        calls = same[same["side"] == "C"].set_index("strike")
        puts  = same[same["side"] == "P"].set_index("strike")
        common = sorted(set(calls.index) & set(puts.index))
        near = [K for K in common if abs(K - spot) / spot <= MONEYNESS]
        if len(near) < MIN_STRIKES: continue
        devs = []
        for K in near:
            c = marks.get(calls.loc[K, "symbol"]); p = marks.get(puts.loc[K, "symbol"])
            if c is None or p is None: continue
            if t not in c.index or t not in p.index: continue
            cp, pp = float(c.loc[t]), float(p.loc[t])
            if cp <= 0 or pp <= 0: continue
            devs.append(((cp - pp + K) - spot) / spot)
        if len(devs) < MIN_STRIKES: continue
        pos = sum(1 for d in devs if d > 0); neg = sum(1 for d in devs if d < 0)
        if pos < MIN_STRIKES and neg < MIN_STRIKES: continue
        out.append({"expiry": exp, "pred": float(np.median(devs)),
                     "n_strikes": len(devs), "calls": calls, "puts": puts})
    return out


def run_variant(gate_pct, sl_pct, tp_pct, leverage, invert=True,
                rv_max=None, iv_max=None, tte_max_h=None, min_theta_proxy=None,
                start_usd=465.12):
    """Run with optional regime filters."""
    btc_perp, btc_marks, btc_cat = load_data("june_btc", "BTCUSD")
    eth_perp, eth_marks, eth_cat = load_data("june_eth", "ETHUSD")
    btc_hl = load_hl("june_btc", "BTCUSD")
    eth_hl = load_hl("june_eth", "ETHUSD")

    equity = start_usd
    open_pos: list = []
    trades: list = []
    hours = sorted(set(btc_perp.index[(btc_perp.index.minute == 0) & (btc_perp.index.second == 0)])
                  | set(eth_perp.index[(eth_perp.index.minute == 0) & (eth_perp.index.second == 0)]))

    for t in hours:
        # manage open positions
        still_open = []
        for pos in open_pos:
            ak = pos["asset"]
            hl = btc_hl if ak == "BTC" else eth_hl
            side = pos["side"]; entry_px = pos["entry_px"]
            sl_price = entry_px * (1 - side * sl_pct)
            tp_price = entry_px * (1 + side * tp_pct)
            check_from = pos.get("last_check_t", pos["entry_t"])
            hl_slice = hl[(hl.index > check_from) & (hl.index <= t)]
            exit_t = exit_px = reason = None
            for mt, row in hl_slice.iterrows():
                if side == 1:
                    if row["low"] <= sl_price:  exit_t, exit_px, reason = mt, sl_price, "stop"; break
                    if row["high"] >= tp_price: exit_t, exit_px, reason = mt, tp_price, "target"; break
                else:
                    if row["high"] >= sl_price: exit_t, exit_px, reason = mt, sl_price, "stop"; break
                    if row["low"] <= tp_price:  exit_t, exit_px, reason = mt, tp_price, "target"; break
            held_h = (t - pos["entry_t"]).total_seconds() / 3600
            if reason is None and held_h >= MAX_HOLD_HOURS:
                reason = "max_hold"; exit_t = t
                exit_px = float(hl.loc[t]["close"]) if t in hl.index else entry_px
            if reason:
                fill = exit_px * (1 - side * SLIPPAGE_BPS / 1e4)
                raw = side * (fill - entry_px) / entry_px
                net = raw - 2 * PERP_FEE_BPS / 1e4
                pnl = pos["notional"] * net
                equity += pnl
                trades.append({**pos, "exit_t": exit_t, "exit_px": fill,
                               "pnl_usd": pnl, "net_ret": net, "event": reason})
            else:
                pos["last_check_t"] = t
                still_open.append(pos)
        open_pos = still_open

        if len(open_pos) >= MAX_CONCURRENT: continue
        for ak in ("BTC", "ETH"):
            if len(open_pos) >= MAX_CONCURRENT: break
            if ak in [p["asset"] for p in open_pos]: continue
            perp = btc_perp if ak == "BTC" else eth_perp
            cat = btc_cat if ak == "BTC" else eth_cat
            marks_ = btc_marks if ak == "BTC" else eth_marks
            if t not in perp.index: continue
            spot = float(perp.loc[t])
            preds = compute_pred_extended(t, spot, cat, marks_)
            candidates = sorted(preds, key=lambda p: -abs(p["pred"]))
            chosen = None
            for c in candidates:
                if abs(c["pred"]) < gate_pct: break
                tte_h = (c["expiry"] - t).total_seconds() / 3600
                if tte_max_h is not None and tte_h > tte_max_h: continue
                # Filter: realized vol
                if rv_max is not None:
                    rv = compute_realized_vol(perp, t, window_hours=24)
                    if rv is None or rv > rv_max: continue
                # Filter: implied vol from ATM straddle
                if iv_max is not None:
                    iv = compute_atm_iv(t, spot, marks_, c["calls"], c["puts"], c["expiry"])
                    if iv is None or iv > iv_max: continue
                # Filter: theta-proxy (closer to expiry preferred)
                if min_theta_proxy is not None:
                    proxy = 1.0 / max(tte_h, 1)   # higher when TTE smaller
                    if proxy < min_theta_proxy: continue
                chosen = c; break
            if chosen is None: continue
            pred = chosen["pred"]
            base_side = 1 if pred > 0 else -1
            side = -base_side if invert else base_side
            eq_eff = equity * CAPITAL_USE_PCT
            sm = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(pred) / SIZE_BASE_PCT))
            desired = eq_eff * sm
            margin_used = sum(p["notional"] / leverage for p in open_pos)
            margin_avail = max(0.0, equity - margin_used)
            notional = min(desired, margin_avail * leverage)
            if notional <= 0: continue
            fill = spot * (1 + side * SLIPPAGE_BPS / 1e4)
            open_pos.append({"asset": ak, "entry_t": t, "entry_px": fill, "side": side,
                             "expiry": chosen["expiry"], "notional": notional,
                             "size_mult": sm, "pred": pred,
                             "leverage": leverage, "last_check_t": t})

    return {"trades": trades, "equity_final": equity, "n_trades": len(trades),
            "leverage": leverage}


def main():
    start_usd = 40_000.0 / USD_INR_RATE
    print("=" * 130)
    print(f"  GREEKS + LEVERAGE SWEEP — corrected Jun 1-20 data")
    print(f"  Base config: INVERTED gate 0.05% SL 2.0% TP 0.5% (4:1 against, was profitable)")
    print(f"  Sweeping filters: realized vol, implied vol, TTE, theta proxy")
    print(f"  Sweeping leverage: 1x, 2x, 3x, 4x, 5x")
    print("=" * 130)

    BASE = dict(gate_pct=0.0005, sl_pct=0.020, tp_pct=0.005, invert=True,
                start_usd=start_usd)

    VARIANTS = [
        # (label, extra_filters)
        ("baseline INVERTED 4:1 (no filter)",        {}),
        ("+ RV < 60% annualized",                    dict(rv_max=0.60)),
        ("+ RV < 50% annualized",                    dict(rv_max=0.50)),
        ("+ RV < 40% annualized (calm only)",        dict(rv_max=0.40)),
        ("+ IV < 80% (low IV regime)",               dict(iv_max=0.80)),
        ("+ IV < 60% (very low IV)",                 dict(iv_max=0.60)),
        ("+ TTE < 24h (close to expiry, hi theta)",  dict(tte_max_h=24)),
        ("+ TTE < 12h (very close to expiry)",       dict(tte_max_h=12)),
        ("+ RV<50% AND TTE<24h (combined filter)",   dict(rv_max=0.50, tte_max_h=24)),
        ("+ RV<40% AND IV<80% AND TTE<24h",          dict(rv_max=0.40, iv_max=0.80, tte_max_h=24)),
    ]

    print(f"\n  {'FILTER':<52}  {'1x':>8} {'2x':>8} {'3x':>8} {'4x':>8} {'5x':>8}  {'trades':>6} {'WR':>5}")
    print("  " + "─" * 120)
    all_results = []
    for label, extra in VARIANTS:
        row_pnls = []
        n_trades = 0; wins = 0
        for lev in (1, 2, 3, 4, 5):
            r = run_variant(leverage=lev, **{**BASE, **extra})
            pnl_inr = (r["equity_final"] - start_usd) * USD_INR_RATE
            row_pnls.append(pnl_inr)
            if lev == 3:  # only count trades once
                n_trades = r["n_trades"]
                wins = sum(1 for t in r["trades"] if t["pnl_usd"] > 0)
        wr = f"{wins/n_trades*100:.0f}%" if n_trades else "—"
        cells = "  ".join(f"{p:>+7,.0f}" if abs(p) >= 1 else "      —"
                          for p in row_pnls)
        flags = " ★" if any(p > 0 for p in row_pnls) else "  "
        print(f"  {label:<52}  {cells}  {n_trades:>5}  {wr:>4}{flags}")
        all_results.append((label, row_pnls, n_trades, wins))

    # Identify profitable cells
    print("\n" + "=" * 130)
    print("  ★ PROFITABLE (label, leverage, PnL INR):")
    found = False
    for label, pnls, n, w in all_results:
        for lev, p in zip([1, 2, 3, 4, 5], pnls):
            if p > 0:
                print(f"    {lev}x leverage  +₹{p:>6,.0f}  ({n} trades, WR {w/n*100:.0f}%)  {label}")
                found = True
    if not found:
        print(f"    No profitable cells across {len(VARIANTS) * 5} combinations.")
    print("=" * 130)


if __name__ == "__main__":
    main()
