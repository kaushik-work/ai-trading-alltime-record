"""
v5.5 LIVE-config backtest with the user's actual capital
=========================================================
Matches the production bot's settings exactly:
  - Start: ₹40,000 INR  (₹/USD rate = 86  →  $465.12 USD)
  - Strategy: v5.5 (PERSIST_HOURS=1, gate 0.6%, exits unchanged)
  - Sizing: 50% of live pool per cycle × size_mult (0.5–3×)
  - Compounding: PnL added to pool, next trade sizes off new pool

Reports BTC, ETH, and combined portfolio in both INR and USD.

Run:
    python backtest_user_capital_june.py
"""

from __future__ import annotations

import os, sys
sys.stdout.reconfigure(encoding="utf-8")

import math
from pathlib import Path
import numpy as np
import pandas as pd

# ── User's actual setup ──────────────────────────────────────────────────────
START_INR        = 40_000.0
USD_INR_RATE     = float(os.environ.get("USD_INR_RATE", "86"))
CAPITAL_USE_PCT  = 0.50       # matches live default
PERSIST_HOURS    = 1          # v5.5
ENTRY_PCT        = 0.006      # 0.6% gate (unchanged from v5)
MIN_STRIKES      = 3
TT_MIN_HOURS     = 6
TT_MAX_HOURS     = 72
MONEYNESS        = 0.05

# Exit logic (identical to live)
STOP_LOSS_PCT      = 0.015
PARTIAL_TP_PCT     = 0.010
TRAIL_PEAK_PCT     = 0.005
TRAIL_GIVEBACK_PCT = 0.0025
MAX_HOLD_HOURS     = 72

# Sizing — size_mult clamped 0.5..3 based on signal strength
SIZE_BASE_PCT  = 0.005
SIZE_MIN_MULT  = 0.5
SIZE_MAX_MULT  = 3.0
MAX_CONCURRENT = 2

# Per-trade costs
PERP_FEE_BPS   = 5.0
SLIPPAGE_BPS   = 2.0


def parse_symbol(sym: str):
    parts = sym.split("-")
    side, strike = parts[0], int(parts[2])
    dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
    expiry = pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")
    return side, strike, expiry


def load_data(subdir: str, perp_symbol: str) -> tuple[pd.Series, dict, pd.DataFrame]:
    base = Path(__file__).parent / "data" / subdir
    perp = pd.read_csv(base / "perp" / f"{perp_symbol}_mark_1m.csv")
    perp["timestamp"] = pd.to_datetime(perp["time"], unit="s", utc=True)
    perp = perp.set_index("timestamp")["close"].sort_index()
    marks = {}
    rows = []
    for p in sorted((base / "options").glob("*_mark_1h.csv")):
        sym = p.name.replace("_mark_1h.csv", "")
        df = pd.read_csv(p)
        if df.empty: continue
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        marks[sym] = df.set_index("timestamp")["close"].sort_index()
        try:
            side, strike, exp = parse_symbol(sym)
            rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": exp})
        except Exception: pass
    return perp, marks, pd.DataFrame(rows)


def compute_pred(t, spot, catalogue, marks):
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
            cp = float(c.loc[t]); pp = float(p.loc[t])
            if cp <= 0 or pp <= 0: continue
            devs.append(((cp - pp + K) - spot) / spot)
        if len(devs) < MIN_STRIKES: continue
        pos = sum(1 for d in devs if d > 0); neg = sum(1 for d in devs if d < 0)
        if pos < MIN_STRIKES and neg < MIN_STRIKES: continue
        out.append({"expiry": exp, "pred": float(np.median(devs)), "n_strikes": len(devs)})
    return out


def run(asset: str, subdir: str, start_usd: float, label: str) -> dict:
    perp_sym = f"{asset}USD"
    perp, marks, catalogue = load_data(subdir, perp_sym)
    if catalogue.empty or perp.empty:
        return {"asset": asset, "trades": [], "equity_final": start_usd, "equity_curve": []}

    equity = start_usd
    sig_history: dict = {}
    open_positions: list = []
    trades: list = []
    equity_curve: list = [(perp.index[0], equity)]
    hours = perp.index[(perp.index.minute == 0) & (perp.index.second == 0)]

    for t in hours:
        spot = float(perp.loc[t])
        equity_curve.append((t, equity))

        preds = compute_pred(t, spot, catalogue, marks)
        for p in preds:
            sig_history.setdefault(p["expiry"], []).append((t, p["pred"]))
        for exp in list(sig_history.keys()):
            sig_history[exp] = [(ti, pi) for ti, pi in sig_history[exp]
                                 if (t - ti).total_seconds() <= 6 * 3600]

        # Manage open
        still_open: list = []
        for pos in open_positions:
            held_h = (t - pos["entry_t"]).total_seconds() / 3600
            side = pos["side"]; entry_px = pos["entry_px"]
            unreal = side * (spot - entry_px) / entry_px
            pos["peak"] = max(pos.get("peak", 0.0), unreal)
            if (not pos.get("tp_taken")) and unreal >= PARTIAL_TP_PCT:
                half = pos["notional"] * 0.5
                fill = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fill - entry_px) / entry_px
                pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
                pnl_usd = half * pnl_pct
                equity += pnl_usd
                pos["notional"] -= half
                pos["tp_taken"] = True
                trades.append({**pos, "exit_t": t, "exit_px": fill, "pnl_usd": pnl_usd,
                               "exit_reason": "partial_tp", "equity_after": equity})
            exit_now, reason = False, ""
            if t >= pos["expiry"]: exit_now, reason = True, "expiry"
            elif held_h >= MAX_HOLD_HOURS: exit_now, reason = True, "max_hold"
            elif unreal < -STOP_LOSS_PCT: exit_now, reason = True, "stop"
            elif pos["peak"] >= TRAIL_PEAK_PCT and (pos["peak"] - unreal) > TRAIL_GIVEBACK_PCT:
                exit_now, reason = True, "trail"
            if exit_now:
                fill = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fill - entry_px) / entry_px
                pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
                pnl_usd = pos["notional"] * pnl_pct
                equity += pnl_usd
                trades.append({**pos, "exit_t": t, "exit_px": fill, "pnl_usd": pnl_usd,
                               "exit_reason": reason, "equity_after": equity})
                continue
            still_open.append(pos)
        open_positions = still_open

        if len(open_positions) >= MAX_CONCURRENT: continue
        already = {p["expiry"] for p in open_positions}
        candidates = sorted(preds, key=lambda p: -abs(p["pred"]))
        chosen = None
        for c in candidates:
            if c["expiry"] in already: continue
            if abs(c["pred"]) < ENTRY_PCT: break
            hist = sig_history.get(c["expiry"], [])
            recent = [pi for ti, pi in hist if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
            if len(recent) < PERSIST_HOURS: continue
            same_sign = sum(1 for pi in recent if np.sign(pi) == np.sign(c["pred"]))
            if same_sign < PERSIST_HOURS: continue
            chosen = c; break
        if chosen is None: continue

        pred = chosen["pred"]; side = 1 if pred > 0 else -1
        # LIVE sizing: CAPITAL_USE_PCT × current equity × size_mult
        effective_equity = equity * CAPITAL_USE_PCT
        size_mult = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(pred) / SIZE_BASE_PCT))
        notional = effective_equity * size_mult
        fill = spot * (1 + side * SLIPPAGE_BPS / 1e4)
        open_positions.append({
            "entry_t": t, "entry_px": fill, "side": side,
            "expiry": chosen["expiry"], "notional": notional,
            "size_mult": size_mult, "pred": pred, "peak": 0.0,
        })

    return {"asset": asset, "trades": trades, "equity_final": equity,
            "equity_curve": equity_curve, "n_trades": len(trades)}


def fmt_inr(usd: float) -> str:
    inr = usd * USD_INR_RATE
    return f"₹{inr:,.0f}"


def print_trade_log(label: str, trades: list, start_usd: float):
    print(f"\n  ── {label} trade log ──")
    print(f"  {'entry':<10} {'side':<5} {'pred':>7} {'notional':>10} "
          f"{'PnL USD':>10} {'PnL INR':>10} {'equity USD':>12} {'equity INR':>12} {'reason':<12}")
    print("  " + "─" * 100)
    running = start_usd
    for tr in trades:
        running = tr["equity_after"]
        side_str = "LONG" if tr["side"] == 1 else "SHORT"
        print(f"  {tr['entry_t'].strftime('%m-%d %H:%M'):<10} {side_str:<5} "
              f"{tr.get('pred', 0)*100:>+6.3f}% "
              f"${tr['notional']:>8.2f} ${tr['pnl_usd']:>+9.2f} "
              f"{fmt_inr(tr['pnl_usd']):>10} "
              f"${running:>10.2f} {fmt_inr(running):>12} {tr['exit_reason']:<12}")


def run_combined(start_usd: float, leverage: float = 10.0) -> dict:
    """Realistic: BOTH BTC and ETH strategies trade off the SAME pool.
    Each trade reads the live pool, sizes 50% of it × size_mult, PnL grows
    the pool for both strategies' next sizing decisions (matches live bot).

    `leverage` caps the notional per trade to (available_margin × leverage)
    so we can compare 1×/2×/3×/4×/5× vs the bot's default 10×.
    """
    btc_perp, btc_marks, btc_cat = load_data("june_btc", "BTCUSD")
    eth_perp, eth_marks, eth_cat = load_data("june_eth", "ETHUSD")

    equity = start_usd
    sig_hist = {"BTC": {}, "ETH": {}}
    open_pos: list = []
    trades: list = []
    hours = sorted(set(btc_perp.index[(btc_perp.index.minute == 0) & (btc_perp.index.second == 0)])
                  | set(eth_perp.index[(eth_perp.index.minute == 0) & (eth_perp.index.second == 0)]))

    for t in hours:
        # Update signal histories
        if t in btc_perp.index:
            spot_b = float(btc_perp.loc[t])
            for p in compute_pred(t, spot_b, btc_cat, btc_marks):
                sig_hist["BTC"].setdefault(p["expiry"], []).append((t, p["pred"]))
        if t in eth_perp.index:
            spot_e = float(eth_perp.loc[t])
            for p in compute_pred(t, spot_e, eth_cat, eth_marks):
                sig_hist["ETH"].setdefault(p["expiry"], []).append((t, p["pred"]))
        for asset_key in ("BTC", "ETH"):
            for exp in list(sig_hist[asset_key].keys()):
                sig_hist[asset_key][exp] = [(ti, pi) for ti, pi in sig_hist[asset_key][exp]
                                             if (t - ti).total_seconds() <= 6 * 3600]

        # Manage open positions (both assets in same list)
        still_open: list = []
        for pos in open_pos:
            asset_key = pos["asset"]
            perp = btc_perp if asset_key == "BTC" else eth_perp
            if t not in perp.index:
                still_open.append(pos); continue
            spot = float(perp.loc[t])
            held_h = (t - pos["entry_t"]).total_seconds() / 3600
            side = pos["side"]; entry_px = pos["entry_px"]
            unreal = side * (spot - entry_px) / entry_px
            pos["peak"] = max(pos.get("peak", 0.0), unreal)
            if (not pos.get("tp_taken")) and unreal >= PARTIAL_TP_PCT:
                half = pos["notional"] * 0.5
                fill = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fill - entry_px) / entry_px
                pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
                pnl_usd = half * pnl_pct
                equity += pnl_usd
                pos["notional"] -= half; pos["tp_taken"] = True
                trades.append({**pos, "exit_t": t, "exit_px": fill, "pnl_usd": pnl_usd,
                               "exit_reason": "partial_tp", "equity_after": equity})
            exit_now, reason = False, ""
            if t >= pos["expiry"]: exit_now, reason = True, "expiry"
            elif held_h >= MAX_HOLD_HOURS: exit_now, reason = True, "max_hold"
            elif unreal < -STOP_LOSS_PCT: exit_now, reason = True, "stop"
            elif pos["peak"] >= TRAIL_PEAK_PCT and (pos["peak"] - unreal) > TRAIL_GIVEBACK_PCT:
                exit_now, reason = True, "trail"
            if exit_now:
                fill = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fill - entry_px) / entry_px
                pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
                pnl_usd = pos["notional"] * pnl_pct
                equity += pnl_usd
                trades.append({**pos, "exit_t": t, "exit_px": fill, "pnl_usd": pnl_usd,
                               "exit_reason": reason, "equity_after": equity})
                continue
            still_open.append(pos)
        open_pos = still_open

        if len(open_pos) >= MAX_CONCURRENT: continue

        # Try entries on both assets
        for asset_key in ("BTC", "ETH"):
            if len(open_pos) >= MAX_CONCURRENT: break
            already = {p["expiry"] for p in open_pos if p["asset"] == asset_key}
            if asset_key not in [p["asset"] for p in open_pos]:
                pass  # OK to enter
            else: continue  # already have one position on this asset
            perp = btc_perp if asset_key == "BTC" else eth_perp
            cat  = btc_cat  if asset_key == "BTC" else eth_cat
            marks_ = btc_marks if asset_key == "BTC" else eth_marks
            if t not in perp.index: continue
            spot = float(perp.loc[t])
            preds = compute_pred(t, spot, cat, marks_)
            candidates = sorted(preds, key=lambda p: -abs(p["pred"]))
            chosen = None
            for c in candidates:
                if c["expiry"] in already: continue
                if abs(c["pred"]) < ENTRY_PCT: break
                hist = sig_hist[asset_key].get(c["expiry"], [])
                recent = [pi for ti, pi in hist if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
                if len(recent) < PERSIST_HOURS: continue
                same_sign = sum(1 for pi in recent if np.sign(pi) == np.sign(c["pred"]))
                if same_sign < PERSIST_HOURS: continue
                chosen = c; break
            if chosen is None: continue
            pred = chosen["pred"]; side = 1 if pred > 0 else -1
            effective_equity = equity * CAPITAL_USE_PCT
            size_mult = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(pred) / SIZE_BASE_PCT))
            desired_notional = effective_equity * size_mult

            # Margin cap: total notional across all open positions can't
            # exceed leverage × equity. Scale down the new entry's notional
            # if it would breach this; skip entirely if no margin is left.
            margin_used = sum(p["notional"] / leverage for p in open_pos)
            margin_avail = max(0.0, equity - margin_used)
            max_new_notional = margin_avail * leverage
            notional = min(desired_notional, max_new_notional)
            if notional <= 0: continue

            fill = spot * (1 + side * SLIPPAGE_BPS / 1e4)
            open_pos.append({
                "asset": asset_key,
                "entry_t": t, "entry_px": fill, "side": side,
                "expiry": chosen["expiry"], "notional": notional,
                "size_mult": size_mult, "pred": pred, "peak": 0.0,
                "leverage": leverage,
            })

    return {"trades": trades, "equity_final": equity, "n_trades": len(trades)}


def main():
    print("=" * 100)
    print(f"  v5.5 LIVE-config Backtest — June 2026 (9 days)")
    print(f"  Start: ₹{START_INR:,.0f} INR  (USD@{USD_INR_RATE} = ${START_INR/USD_INR_RATE:.2f})")
    print(f"  Per-cycle deploy: {CAPITAL_USE_PCT*100:.0f}% of pool × size_mult (0.5–3×)")
    print(f"  v5.5 dials: gate 0.6%, persist {PERSIST_HOURS}h, exits 1.5%/1%/0.25%")
    print(f"  COMPOUNDING: each trade's PnL grows the pool for the next sizing")
    print("=" * 100)

    start_usd = START_INR / USD_INR_RATE

    # Run BTC and ETH separately, each starting with the same INR pool.
    # (The user has ONE pool, but each strategy independently sizes off
    #  current equity. We simulate each in isolation to show per-asset edge,
    #  then combine for portfolio result.)
    btc = run("BTC", "june_btc", start_usd, "BTC")
    eth = run("ETH", "june_eth", start_usd, "ETH")

    print()
    print(f"  BTC: {btc['n_trades']} trades → final ${btc['equity_final']:.2f} "
          f"({fmt_inr(btc['equity_final'])})  pnl ${btc['equity_final']-start_usd:+.2f}  "
          f"{(btc['equity_final']/start_usd-1)*100:+.2f}%")
    print(f"  ETH: {eth['n_trades']} trades → final ${eth['equity_final']:.2f} "
          f"({fmt_inr(eth['equity_final'])})  pnl ${eth['equity_final']-start_usd:+.2f}  "
          f"{(eth['equity_final']/start_usd-1)*100:+.2f}%")
    print()

    # LEVERAGE SWEEP — shared pool, compounding, 1x..10x
    print("  ── Leverage sweep (₹40k shared pool, BTC + ETH, compounding) ──")
    print(f"  {'leverage':<10} {'trades':>7} {'final USD':>12} {'final INR':>14} "
          f"{'net PnL INR':>14} {'return%':>10}")
    print("  " + "─" * 78)
    results = {}
    for lev in (1, 2, 3, 4, 5, 10):
        r = run_combined(start_usd, leverage=float(lev))
        results[lev] = r
        ret_pct = (r["equity_final"] / start_usd - 1) * 100
        pnl_inr = (r["equity_final"] - start_usd) * USD_INR_RATE
        print(f"  {lev}x{'':<8} {r['n_trades']:>7} "
              f"${r['equity_final']:>10.2f} "
              f"{fmt_inr(r['equity_final']):>14} "
              f"{('+' if pnl_inr >= 0 else '') + format(pnl_inr, ',.0f'):>14} "
              f"{ret_pct:>+9.2f}%")
    print()

    print_trade_log("BTC (₹40k single-asset)", btc["trades"], start_usd)
    print_trade_log("ETH (₹40k single-asset)", eth["trades"], start_usd)


if __name__ == "__main__":
    main()
