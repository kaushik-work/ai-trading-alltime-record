"""Comprehensive sweep — gate × R:R × persistence × MIN_STRIKES × direction.

Tries:
  - Different gates (0.05% to 0.6%)
  - Different R:R ratios (1:1 to 1:5)
  - Different persistence (v5=2h, v5.5=1h, none=0h)
  - Different consensus requirements (3, 5 strikes)
  - INVERTED direction (short when pred positive, long when pred negative)
    — if the signal has anti-edge, inverting captures it.

Reports each variant on the corrected Jun 1-20 data.
"""
from __future__ import annotations
import sys, os
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from backtest_engine import (
    load_data, load_hl, compute_pred,
    USD_INR_RATE, MIN_STRIKES as DEFAULT_MIN_STRIKES,
    TT_MIN_HOURS, TT_MAX_HOURS, MONEYNESS,
    SIZE_BASE_PCT, SIZE_MIN_MULT, SIZE_MAX_MULT,
    CAPITAL_USE_PCT, MAX_CONCURRENT, MAX_HOLD_HOURS,
    PERP_FEE_BPS, SLIPPAGE_BPS, MAINT_MARGIN_PCT,
)


def compute_pred_with_strikes(t, spot, catalogue, marks, min_strikes):
    tt_min = t + pd.Timedelta(hours=TT_MIN_HOURS)
    tt_max = t + pd.Timedelta(hours=TT_MAX_HOURS)
    eligible = catalogue[(catalogue["expiry"] > tt_min) & (catalogue["expiry"] <= tt_max)]
    out = []
    for exp in sorted(eligible["expiry"].unique()):
        same = eligible[eligible["expiry"] == exp]
        calls = same[same["side"] == "C"].set_index("strike")
        puts = same[same["side"] == "P"].set_index("strike")
        common = sorted(set(calls.index) & set(puts.index))
        near = [K for K in common if abs(K - spot) / spot <= MONEYNESS]
        if len(near) < min_strikes: continue
        devs = []
        for K in near:
            c = marks.get(calls.loc[K, "symbol"]); p = marks.get(puts.loc[K, "symbol"])
            if c is None or p is None: continue
            if t not in c.index or t not in p.index: continue
            cp, pp = float(c.loc[t]), float(p.loc[t])
            if cp <= 0 or pp <= 0: continue
            devs.append(((cp - pp + K) - spot) / spot)
        if len(devs) < min_strikes: continue
        pos = sum(1 for d in devs if d > 0); neg = sum(1 for d in devs if d < 0)
        if pos < min_strikes and neg < min_strikes: continue
        out.append({"expiry": exp, "pred": float(np.median(devs)), "n_strikes": len(devs)})
    return out


def run_variant(gate_pct, sl_pct, tp_pct, persist_h, min_strikes, invert,
                start_usd, leverage=3.0):
    btc_perp, btc_marks, btc_cat = load_data("june_btc", "BTCUSD")
    eth_perp, eth_marks, eth_cat = load_data("june_eth", "ETHUSD")
    btc_hl = load_hl("june_btc", "BTCUSD")
    eth_hl = load_hl("june_eth", "ETHUSD")

    equity = start_usd
    sig_hist = {"BTC": {}, "ETH": {}}
    open_pos: list = []
    trades: list = []
    hours = sorted(set(btc_perp.index[(btc_perp.index.minute == 0) & (btc_perp.index.second == 0)])
                  | set(eth_perp.index[(eth_perp.index.minute == 0) & (eth_perp.index.second == 0)]))

    for t in hours:
        if t in btc_perp.index:
            for p in compute_pred_with_strikes(t, float(btc_perp.loc[t]), btc_cat, btc_marks, min_strikes):
                sig_hist["BTC"].setdefault(p["expiry"], []).append((t, p["pred"]))
        if t in eth_perp.index:
            for p in compute_pred_with_strikes(t, float(eth_perp.loc[t]), eth_cat, eth_marks, min_strikes):
                sig_hist["ETH"].setdefault(p["expiry"], []).append((t, p["pred"]))
        for ak in ("BTC", "ETH"):
            for exp in list(sig_hist[ak].keys()):
                sig_hist[ak][exp] = [(ti, pi) for ti, pi in sig_hist[ak][exp]
                                     if (t - ti).total_seconds() <= 6 * 3600]

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

        # entries
        if len(open_pos) >= MAX_CONCURRENT: continue
        for ak in ("BTC", "ETH"):
            if len(open_pos) >= MAX_CONCURRENT: break
            if ak in [p["asset"] for p in open_pos]: continue
            perp = btc_perp if ak == "BTC" else eth_perp
            cat = btc_cat if ak == "BTC" else eth_cat
            marks_ = btc_marks if ak == "BTC" else eth_marks
            if t not in perp.index: continue
            spot = float(perp.loc[t])
            preds = compute_pred_with_strikes(t, spot, cat, marks_, min_strikes)
            candidates = sorted(preds, key=lambda p: -abs(p["pred"]))
            chosen = None
            for c in candidates:
                if abs(c["pred"]) < gate_pct: break
                if persist_h > 0:
                    hist = sig_hist[ak].get(c["expiry"], [])
                    recent = [pi for ti, pi in hist if (t - ti).total_seconds() <= persist_h * 3600]
                    if len(recent) < persist_h: continue
                    if sum(1 for pi in recent if np.sign(pi) == np.sign(c["pred"])) < persist_h:
                        continue
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

    return {"trades": trades, "equity_final": equity, "n_trades": len(trades)}


def main():
    start_usd = 40_000.0 / USD_INR_RATE
    print("=" * 130)
    print(f"  FULL MATRIX SWEEP — corrected Jun 1-20 data")
    print(f"  Seed ₹40,000, 3× leverage, BTC + ETH shared pool")
    print("=" * 130)

    # gate, sl, tp, persist_h, min_strikes, invert, label
    VARIANTS = [
        # ── baseline (no fires expected) ──
        (0.006,  0.015,  0.010,  1, 3, False, "BASELINE v5.5 gate 0.6% SL 1.5% TP 1.0%"),

        # ── gate sweep, 1:1 R:R ──
        (0.0005, 0.005,  0.005,  1, 3, False, "gate 0.05% SL 0.5%  TP 0.5%  R:R 1:1"),
        (0.001,  0.005,  0.005,  1, 3, False, "gate 0.1%  SL 0.5%  TP 0.5%  R:R 1:1"),
        (0.002,  0.005,  0.005,  1, 3, False, "gate 0.2%  SL 0.5%  TP 0.5%  R:R 1:1"),

        # ── gate sweep, 1:2 R:R ──
        (0.0005, 0.005,  0.010,  1, 3, False, "gate 0.05% SL 0.5%  TP 1.0%  R:R 1:2"),
        (0.001,  0.005,  0.010,  1, 3, False, "gate 0.1%  SL 0.5%  TP 1.0%  R:R 1:2"),

        # ── gate sweep, 1:3 R:R ──
        (0.0005, 0.005,  0.015,  1, 3, False, "gate 0.05% SL 0.5%  TP 1.5%  R:R 1:3"),
        (0.001,  0.005,  0.015,  1, 3, False, "gate 0.1%  SL 0.5%  TP 1.5%  R:R 1:3"),

        # ── 1:4 R:R (user's idea) ──
        (0.0005, 0.005,  0.020,  1, 3, False, "gate 0.05% SL 0.5%  TP 2.0%  R:R 1:4"),
        (0.0005, 0.0025, 0.010,  1, 3, False, "gate 0.05% SL 0.25% TP 1.0%  R:R 1:4 (tight)"),

        # ── REVERSE: 4:1 R:R (write-options style) ──
        (0.0005, 0.020,  0.005,  1, 3, False, "gate 0.05% SL 2.0%  TP 0.5%  R:R 4:1 (write-style)"),
        (0.0005, 0.010,  0.0025, 1, 3, False, "gate 0.05% SL 1.0%  TP 0.25% R:R 4:1 (tight write)"),

        # ── v5 (persist=2h) ──
        (0.0005, 0.005,  0.005,  2, 3, False, "v5  persist=2h gate 0.05% SL 0.5%  TP 0.5%"),
        (0.001,  0.005,  0.010,  2, 3, False, "v5  persist=2h gate 0.1%  SL 0.5%  TP 1.0%"),

        # ── No persistence ──
        (0.0005, 0.005,  0.005,  0, 3, False, "no persist  gate 0.05% SL 0.5%  TP 0.5%"),
        (0.001,  0.005,  0.005,  0, 3, False, "no persist  gate 0.1%  SL 0.5%  TP 0.5%"),

        # ── MIN_STRIKES = 5 (stronger consensus) ──
        (0.0005, 0.005,  0.005,  1, 5, False, "strikes=5  gate 0.05% SL 0.5%  TP 0.5%"),
        (0.001,  0.005,  0.005,  1, 5, False, "strikes=5  gate 0.1%  SL 0.5%  TP 0.5%"),

        # ── INVERTED direction (if strategy has anti-edge) ──
        (0.0005, 0.005,  0.005,  1, 3, True,  "INVERTED  gate 0.05% SL 0.5%  TP 0.5%  R:R 1:1"),
        (0.001,  0.005,  0.005,  1, 3, True,  "INVERTED  gate 0.1%  SL 0.5%  TP 0.5%  R:R 1:1"),
        (0.0005, 0.005,  0.010,  1, 3, True,  "INVERTED  gate 0.05% SL 0.5%  TP 1.0%  R:R 1:2"),
        (0.0005, 0.020,  0.005,  1, 3, True,  "INVERTED  gate 0.05% SL 2.0%  TP 0.5%  R:R 4:1"),
        (0.0005, 0.0025, 0.010,  1, 3, True,  "INVERTED  gate 0.05% SL 0.25% TP 1.0%  R:R 1:4"),
    ]

    print(f"\n  {'#':>3}  {'label':<56}  {'trades':>6}  {'win%':>6}  {'PnL INR':>10}  {'reasons':<22}")
    print("  " + "─" * 120)
    results = []
    for i, (gate, sl, tp, persist_h, min_strikes, invert, label) in enumerate(VARIANTS, 1):
        r = run_variant(gate, sl, tp, persist_h, min_strikes, invert, start_usd)
        n = r["n_trades"]
        if n > 0:
            wins = sum(1 for t in r["trades"] if t["pnl_usd"] > 0)
            pnl_inr = (r["equity_final"] - start_usd) * USD_INR_RATE
            by = {}
            for t in r["trades"]: by[t["event"]] = by.get(t["event"], 0) + 1
            rs = ", ".join(f"{k}:{v}" for k, v in sorted(by.items()))
            wr_str = f"{wins/n*100:>5.1f}%"
            pnl_str = f"{'+' if pnl_inr>=0 else ''}{pnl_inr:>+9,.0f}"
            label_color = label
        else:
            wr_str = "—"; pnl_str = "0"; rs = "no trades"; pnl_inr = 0
            label_color = label
        flag = " ★" if pnl_inr > 0 else ("  " if pnl_inr == 0 else "")
        print(f"  {i:>3}  {label_color:<56}  {n:>6}  {wr_str:>6}  {pnl_str:>10}{flag}  {rs:<22}")
        results.append((label, n, pnl_inr, r))

    # Identify winners
    profitable = [r for r in results if r[2] > 0]
    print("\n" + "=" * 130)
    if profitable:
        print(f"  ★ PROFITABLE VARIANTS  ({len(profitable)} of {len(VARIANTS)}):")
        for label, n, pnl, _ in sorted(profitable, key=lambda x: -x[2]):
            print(f"    +₹{pnl:>7,.0f}  ({n:>3} trades)  {label}")
    else:
        print(f"  No profitable variants. Every gate/R:R/direction combination loses.")
    print("=" * 130)


if __name__ == "__main__":
    main()
