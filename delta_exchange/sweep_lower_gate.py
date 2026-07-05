"""Sweep: lower the entry gate + try 1:4 R:R targets.

Rationale: the 0.6% gate never reached in June 2026 (max |pred| 0.130% BTC,
0.194% ETH). If we want any trades, we need to lower the gate. The user
wants asymmetric 1:4 R:R so a 20-25% win rate still produces profit.

Tested variants (all on corrected Jun 1-20 data):

  gate    SL %    TP %    R:R     (notes)
  0.05    0.50    2.00    1:4     aggressive — trade every micro-dislocation
  0.10    0.50    2.00    1:4     looser gate
  0.15    0.50    2.00    1:4     marginal signals only
  0.05    0.25    1.00    1:4     tighter stops
  0.10    0.25    1.00    1:4     tighter stops
  0.10    1.00    4.00    1:4     wider brackets, fewer trades
  0.10    0.50    1.00    1:2     reference 1:2 R:R for comparison

Entry filters (unchanged from v5.5):
  - Persistence ≥ 1h same-sign
  - ≥3 strikes consensus
  - TTE 6-72h, ±5% moneyness
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
    USD_INR_RATE, PERSIST_HOURS, MIN_STRIKES,
    TT_MIN_HOURS, TT_MAX_HOURS, MONEYNESS,
    SIZE_BASE_PCT, SIZE_MIN_MULT, SIZE_MAX_MULT,
    CAPITAL_USE_PCT, MAX_CONCURRENT, MAX_HOLD_HOURS,
    PERP_FEE_BPS, SLIPPAGE_BPS, MAINT_MARGIN_PCT,
)


def run_sweep(gate_pct: float, sl_pct: float, tp_pct: float,
              start_usd: float, leverage: float = 3.0) -> dict:
    """Same engine logic but with custom gate, SL, TP."""
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
        # build signal history
        if t in btc_perp.index:
            for p in compute_pred(t, float(btc_perp.loc[t]), btc_cat, btc_marks):
                sig_hist["BTC"].setdefault(p["expiry"], []).append((t, p["pred"]))
        if t in eth_perp.index:
            for p in compute_pred(t, float(eth_perp.loc[t]), eth_cat, eth_marks):
                sig_hist["ETH"].setdefault(p["expiry"], []).append((t, p["pred"]))
        for ak in ("BTC", "ETH"):
            for exp in list(sig_hist[ak].keys()):
                sig_hist[ak][exp] = [(ti, pi) for ti, pi in sig_hist[ak][exp]
                                     if (t - ti).total_seconds() <= 6 * 3600]

        # manage open positions via intra-bar 1m high/low check
        still_open = []
        for pos in open_pos:
            ak = pos["asset"]
            hl = btc_hl if ak == "BTC" else eth_hl
            side = pos["side"]; entry_px = pos["entry_px"]
            sl_price = entry_px * (1 - side * sl_pct)
            tp_price = entry_px * (1 + side * tp_pct)
            liq_price = entry_px * (1 - side * ((1.0 / leverage) - MAINT_MARGIN_PCT))
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
                               "pnl_usd": pnl, "net_ret": net, "event": reason,
                               "sl_price": sl_price, "tp_price": tp_price,
                               "equity_after": equity})
            else:
                pos["last_check_t"] = t
                still_open.append(pos)
        open_pos = still_open

        # try new entries
        if len(open_pos) >= MAX_CONCURRENT: continue
        for ak in ("BTC", "ETH"):
            if len(open_pos) >= MAX_CONCURRENT: break
            if ak in [p["asset"] for p in open_pos]: continue
            perp = btc_perp if ak == "BTC" else eth_perp
            cat = btc_cat if ak == "BTC" else eth_cat
            marks_ = btc_marks if ak == "BTC" else eth_marks
            if t not in perp.index: continue
            spot = float(perp.loc[t])
            preds = compute_pred(t, spot, cat, marks_)
            candidates = sorted(preds, key=lambda p: -abs(p["pred"]))
            chosen = None
            for c in candidates:
                if abs(c["pred"]) < gate_pct: break
                hist = sig_hist[ak].get(c["expiry"], [])
                recent = [pi for ti, pi in hist if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
                if len(recent) < PERSIST_HOURS: continue
                if sum(1 for pi in recent if np.sign(pi) == np.sign(c["pred"])) < PERSIST_HOURS:
                    continue
                chosen = c; break
            if chosen is None: continue
            pred = chosen["pred"]; side = 1 if pred > 0 else -1
            eq_eff = equity * CAPITAL_USE_PCT
            sm = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(pred) / SIZE_BASE_PCT))
            desired = eq_eff * sm
            margin_used = sum(p["notional"] / leverage for p in open_pos)
            margin_avail = max(0.0, equity - margin_used)
            notional = min(desired, margin_avail * leverage)
            if notional <= 0: continue
            fill = spot * (1 + side * SLIPPAGE_BPS / 1e4)
            open_pos.append({"asset": ak, "entry_t": t, "entry_px": fill,
                             "side": side, "expiry": chosen["expiry"],
                             "notional": notional, "size_mult": sm, "pred": pred,
                             "leverage": leverage, "last_check_t": t})

    return {"trades": trades, "equity_final": equity, "n_trades": len(trades)}


def summarize(label: str, r: dict, start_usd: float):
    trades = r["trades"]
    if not trades:
        print(f"  {label:<46} 0 trades"); return
    pnls = [t["pnl_usd"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    wr = len(wins) / len(trades) * 100
    inr = total * USD_INR_RATE
    by_reason = {}
    for t in trades:
        by_reason[t["event"]] = by_reason.get(t["event"], 0) + 1
    rs = ", ".join(f"{k}:{v}" for k, v in sorted(by_reason.items()))
    print(f"  {label:<46} n={len(trades):>3}  win%={wr:>5.1f}  "
          f"total=${total:>+7.2f} ({'+' if inr>=0 else ''}{inr:>+7,.0f} INR)  [{rs}]")


def main():
    start_usd = 40_000.0 / USD_INR_RATE
    print("=" * 110)
    print(f"  GATE × R:R SWEEP — corrected Jun 1-20 data")
    print(f"  Seed ₹40,000 = ${start_usd:.2f}, 3× leverage, BTC + ETH shared pool")
    print(f"  Variants test lowering the gate (currently 0.6%, never reached)")
    print(f"  and shifting to 1:4 reward:risk")
    print("=" * 110)

    VARIANTS = [
        # (gate, sl, tp, label)
        (0.006, 0.015, 0.010, "BASELINE  gate 0.6%  SL 1.5%  TP 1.0%  R:R 1.5:1 (current live)"),
        (0.003, 0.010, 0.020, "gate 0.3%  SL 1.0%  TP 2.0%  R:R 1:2"),
        (0.002, 0.005, 0.020, "gate 0.2%  SL 0.5%  TP 2.0%  R:R 1:4"),
        (0.001, 0.005, 0.020, "gate 0.1%  SL 0.5%  TP 2.0%  R:R 1:4"),
        (0.0005, 0.005, 0.020, "gate 0.05% SL 0.5%  TP 2.0%  R:R 1:4 (very aggressive)"),
        (0.001, 0.0025, 0.010, "gate 0.1%  SL 0.25% TP 1.0%  R:R 1:4 (tight)"),
        (0.001, 0.010, 0.040, "gate 0.1%  SL 1.0%  TP 4.0%  R:R 1:4 (wide)"),
        (0.0005, 0.0025, 0.010, "gate 0.05% SL 0.25% TP 1.0%  R:R 1:4 (very tight)"),
    ]

    print("\n  ── FULL WINDOW (Jun 1-19, 132 eligible hours) ──\n")
    results = {}
    for gate, sl, tp, label in VARIANTS:
        r = run_sweep(gate, sl, tp, start_usd)
        results[(gate, sl, tp)] = r
        summarize(label, r, start_usd)

    # Per-trade detail for the winning variant
    if not results: return
    best = max(results.items(), key=lambda x: x[1]["equity_final"])
    gate, sl, tp = best[0]
    r = best[1]
    if r["n_trades"] > 0:
        print("\n" + "=" * 110)
        print(f"  Per-trade detail for best variant: gate {gate*100}% SL {sl*100}% TP {tp*100}%")
        print("=" * 110)
        print(f"\n  {'#':>3}  {'entry IST':<13}  {'exit IST':<13}  {'asset':<4}  {'side':<5}  "
              f"{'pred':>7}  {'sz':>4}  {'entry $':>10}  {'exit $':>10}  {'net%':>6}  "
              f"{'PnL $':>7}  {'PnL ₹':>7}  {'event':<8}")
        print("  " + "─" * 134)
        running = start_usd
        for i, t in enumerate(r["trades"], 1):
            ist = t["entry_t"].tz_convert("Asia/Kolkata").strftime("%m-%d %H:%M")
            xist = t["exit_t"].tz_convert("Asia/Kolkata").strftime("%m-%d %H:%M")
            side_s = "LONG" if t["side"] == 1 else "SHORT"
            ret = t["net_ret"] * 100
            inr = t["pnl_usd"] * USD_INR_RATE
            print(f"  {i:>3}  {ist:<13}  {xist:<13}  {t['asset']:<4}  {side_s:<5}  "
                  f"{t.get('pred',0)*100:>+6.3f}%  {t.get('size_mult',1):>3.1f}x  "
                  f"${t['entry_px']:>9,.2f}  ${t['exit_px']:>9,.2f}  {ret:>+5.2f}%  "
                  f"${t['pnl_usd']:>+6.2f}  {inr:>+6,.0f}  {t['event']:<8}")
        print(f"\n  Final: ${r['equity_final']:.2f}  (₹{r['equity_final']*USD_INR_RATE:,.0f})")
        print(f"  Net change: ${r['equity_final']-start_usd:+.2f}  "
              f"({(r['equity_final']/start_usd-1)*100:+.2f}%)")


if __name__ == "__main__":
    main()
