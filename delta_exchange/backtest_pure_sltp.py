"""Pure SL/TP sweep — no trail, no partial TP. Position either hits stop
or target. Tests several reward:risk ratios.

Entry logic: identical to v5.5 (gate 0.6%, persist 1h, ≥3 strike consensus).
Exit logic (this script):
  - Full exit if price hits stop (entry × (1 − sign · SL%))
  - Full exit if price hits target (entry × (1 + sign · TP%))
  - Full exit at expiry or max hold (72h) — same as baseline
  - NO trail, NO partial TP

Compares against baseline v5.5 (trail + partial TP) on the Jun 2 → Jun 10
data, then drills down to today's (Jun 10) trades with full exit-price
verification.
"""
from __future__ import annotations
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path
import numpy as np
import pandas as pd

# v5.5 baseline dials (entry side unchanged)
START_INR     = 40_000.0
USD_INR_RATE  = 86.0
CAPITAL_USE_PCT = 0.50
PERSIST_HOURS = 1
ENTRY_PCT     = 0.006
MIN_STRIKES   = 3
TT_MIN_HOURS  = 6
TT_MAX_HOURS  = 72
MONEYNESS     = 0.05
MAX_HOLD_HOURS = 72
SIZE_BASE_PCT  = 0.005
SIZE_MIN_MULT  = 0.5
SIZE_MAX_MULT  = 3.0
MAX_CONCURRENT = 2
PERP_FEE_BPS   = 5.0
SLIPPAGE_BPS   = 2.0


def parse_symbol(sym):
    parts = sym.split("-")
    side, strike = parts[0], int(parts[2])
    dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
    return side, strike, pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")


def load_data(subdir, perp_symbol):
    base = Path(__file__).parent / "data" / subdir
    perp = pd.read_csv(base / "perp" / f"{perp_symbol}_mark_1m.csv")
    perp["timestamp"] = pd.to_datetime(perp["time"], unit="s", utc=True)
    perp = perp.set_index("timestamp")["close"].sort_index()
    marks, rows = {}, []
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
            cp, pp = float(c.loc[t]), float(p.loc[t])
            if cp <= 0 or pp <= 0: continue
            devs.append(((cp - pp + K) - spot) / spot)
        if len(devs) < MIN_STRIKES: continue
        pos = sum(1 for d in devs if d > 0); neg = sum(1 for d in devs if d < 0)
        if pos < MIN_STRIKES and neg < MIN_STRIKES: continue
        out.append({"expiry": exp, "pred": float(np.median(devs)), "n_strikes": len(devs)})
    return out


def run_pure_sltp(start_usd, leverage, sl_pct, tp_pct):
    """No trail. No partial TP. Position exits ONLY on:
       - stop (price moves SL adversely)
       - target (price moves TP favorably)
       - max_hold / expiry
    The intra-hour check uses minute-bar HIGH/LOW so an SL or TP hit anywhere
    during the hour is captured — this matches what a real bracket-order does
    on Delta (Delta executes stop/target at intra-hour fills, not hourly close).
    """
    btc_perp, btc_marks, btc_cat = load_data("june_btc", "BTCUSD")
    eth_perp, eth_marks, eth_cat = load_data("june_eth", "ETHUSD")

    # Pre-load minute high/low for intra-hour SL/TP checks
    def load_hl(subdir, sym):
        p = Path(__file__).parent / "data" / subdir / "perp" / f"{sym}_mark_1m.csv"
        df = pd.read_csv(p)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df.set_index("timestamp")[["high", "low", "close"]].sort_index()
    btc_hl = load_hl("june_btc", "BTCUSD")
    eth_hl = load_hl("june_eth", "ETHUSD")

    equity = start_usd
    sig_hist = {"BTC": {}, "ETH": {}}
    open_pos, trades = [], []
    hours = sorted(set(btc_perp.index[(btc_perp.index.minute == 0) & (btc_perp.index.second == 0)])
                  | set(eth_perp.index[(eth_perp.index.minute == 0) & (eth_perp.index.second == 0)]))

    for t in hours:
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

        # ── Manage open positions: walk the minute bars in [prev_hour, t]
        # and check for SL/TP/LIQ triggers. This is the realistic intra-hour
        # check using actual high/low — same as a Delta bracket order would see.
        # LIQUIDATION CHECK: at 3× leverage, isolated margin liquidates at
        # roughly entry ± (1/leverage − 0.005) — i.e. ~32.8% adverse for 3×.
        # Delta India keeps a 0.5% maintenance margin on perps.
        # If SL fires first (it should at 1.5% SL), liq never triggers.
        # But we check it anyway — a 33% wick on a 1m bar is theoretically
        # possible during a flash crash and would override any pending SL fill.
        MAINT_MARGIN_PCT = 0.005   # Delta India BTCUSD / ETHUSD maint margin
        still_open = []
        for pos in open_pos:
            ak = pos["asset"]
            hl = btc_hl if ak == "BTC" else eth_hl
            side = pos["side"]
            entry_px = pos["entry_px"]
            lev = pos["leverage"]
            sl_price = entry_px * (1 - side * sl_pct)
            tp_price = entry_px * (1 + side * tp_pct)
            # Approx isolated-margin liquidation price (no funding fees in this calc)
            liq_pct = (1.0 / lev) - MAINT_MARGIN_PCT     # 3× → ~32.83%
            liq_price = entry_px * (1 - side * liq_pct)
            # Track worst adverse move so we can confirm we never came close
            max_adverse_pct = pos.get("max_adverse_pct", 0.0)
            check_from = pos.get("last_check_t", pos["entry_t"])
            minute_slice = hl[(hl.index > check_from) & (hl.index <= t)]
            exit_now = False
            exit_t = None
            exit_px_actual = None
            reason = ""
            for mt, row in minute_slice.iterrows():
                # Update worst-case adverse for liquidation tracking
                adverse = -side * ((row["low" if side == 1 else "high"]) - entry_px) / entry_px
                if adverse > max_adverse_pct:
                    max_adverse_pct = adverse
                if side == 1:  # LONG
                    # LIQUIDATION precedes SL if the 1m bar wicks far enough
                    if row["low"] <= liq_price:
                        exit_now = True; exit_t = mt; exit_px_actual = liq_price
                        reason = "LIQUIDATION"; break
                    if row["low"] <= sl_price:
                        exit_now = True; exit_t = mt; exit_px_actual = sl_price
                        reason = "stop"; break
                    if row["high"] >= tp_price:
                        exit_now = True; exit_t = mt; exit_px_actual = tp_price
                        reason = "target"; break
                else:           # SHORT
                    if row["high"] >= liq_price:
                        exit_now = True; exit_t = mt; exit_px_actual = liq_price
                        reason = "LIQUIDATION"; break
                    if row["high"] >= sl_price:
                        exit_now = True; exit_t = mt; exit_px_actual = sl_price
                        reason = "stop"; break
                    if row["low"] <= tp_price:
                        exit_now = True; exit_t = mt; exit_px_actual = tp_price
                        reason = "target"; break
            pos["max_adverse_pct"] = max_adverse_pct
            pos["liq_price"] = liq_price
            held_h = (t - pos["entry_t"]).total_seconds() / 3600
            if not exit_now:
                if t >= pos["expiry"]:
                    exit_now = True; exit_t = t; exit_px_actual = float(hl.loc[t]["close"]) if t in hl.index else entry_px
                    reason = "expiry"
                elif held_h >= MAX_HOLD_HOURS:
                    exit_now = True; exit_t = t; exit_px_actual = float(hl.loc[t]["close"]) if t in hl.index else entry_px
                    reason = "max_hold"
            if exit_now:
                fill = exit_px_actual * (1 - side * SLIPPAGE_BPS / 1e4)
                raw = side * (fill - entry_px) / entry_px
                net = raw - 2 * PERP_FEE_BPS / 1e4
                pnl = pos["notional"] * net
                equity += pnl
                trades.append({**pos, "exit_t": exit_t, "exit_px": fill,
                               "pnl_usd": pnl, "exit_reason": reason,
                               "equity_after": equity, "raw_ret": raw, "net_ret": net,
                               "sl_price": sl_price, "tp_price": tp_price,
                               "liq_price": liq_price, "max_adverse_pct": max_adverse_pct})
                continue
            pos["last_check_t"] = t
            still_open.append(pos)
        open_pos = still_open

        if len(open_pos) >= MAX_CONCURRENT: continue
        for ak in ("BTC", "ETH"):
            if len(open_pos) >= MAX_CONCURRENT: break
            if ak in [p["asset"] for p in open_pos]: continue
            perp = btc_perp if ak == "BTC" else eth_perp
            cat  = btc_cat  if ak == "BTC" else eth_cat
            marks_ = btc_marks if ak == "BTC" else eth_marks
            if t not in perp.index: continue
            spot = float(perp.loc[t])
            preds = compute_pred(t, spot, cat, marks_)
            candidates = sorted(preds, key=lambda p: -abs(p["pred"]))
            chosen = None
            already = {p["expiry"] for p in open_pos if p["asset"] == ak}
            for c in candidates:
                if c["expiry"] in already: continue
                if abs(c["pred"]) < ENTRY_PCT: break
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
            open_pos.append({"asset": ak, "entry_t": t, "entry_px": fill, "side": side,
                             "expiry": chosen["expiry"], "notional": notional,
                             "size_mult": sm, "pred": pred,
                             "leverage": leverage, "last_check_t": t})

    return {"trades": trades, "equity_final": equity, "n_trades": len(trades)}


def summarize(label, r, start_usd, today_only=False):
    trades = r["trades"]
    if today_only:
        cutoff = pd.Timestamp("2026-06-10", tz="UTC")
        end    = pd.Timestamp("2026-06-11", tz="UTC")
        trades = [t for t in trades if cutoff <= t["entry_t"] < end]
    n = len(trades)
    if n == 0:
        print(f"  {label:<28} 0 trades")
        return
    pnls = [t["pnl_usd"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    by_reason = {}
    for t in trades:
        by_reason[t["exit_reason"]] = by_reason.get(t["exit_reason"], 0) + 1
    rs = ", ".join(f"{k}:{v}" for k, v in sorted(by_reason.items()))
    total = sum(pnls)
    wr = len(wins) / n * 100
    inr = total * USD_INR_RATE
    print(f"  {label:<28} n={n:>3}  win%={wr:>5.1f}  "
          f"total=${total:>+7.2f} ({'+' if inr>=0 else ''}{inr:>+7,.0f} INR)  "
          f"avgW=${(sum(wins)/len(wins) if wins else 0):>+5.2f}  "
          f"avgL=${(sum(losses)/len(losses) if losses else 0):>+5.2f}  "
          f"reasons={rs}")


def main():
    start_usd = START_INR / USD_INR_RATE
    print("=" * 110)
    print(f"  v5.5 PURE SL/TP sweep (no trail, no partial TP)")
    print(f"  ₹40k seed, 3× leverage, BTC+ETH shared pool, compounding")
    print(f"  Entry: gate 0.6%, persist 1h, ≥3 strikes — UNCHANGED from v5.5")
    print(f"  Exit:  SL or TP hit on intra-hour 1m high/low — full position, no trail")
    print("=" * 110)

    # (SL%, TP%, label) — sweep both symmetric and asymmetric ratios
    VARIANTS = [
        (0.015, 0.010, "SL 1.5% / TP 1.0%  (R:R 1.5:1, low TP)"),
        (0.015, 0.015, "SL 1.5% / TP 1.5%  (R:R 1:1)"),
        (0.015, 0.020, "SL 1.5% / TP 2.0%  (R:R 1:1.33)"),
        (0.015, 0.030, "SL 1.5% / TP 3.0%  (R:R 1:2)"),
        (0.010, 0.020, "SL 1.0% / TP 2.0%  (R:R 1:2, tight stop)"),
    ]

    print("\n  ── FULL WINDOW (Jun 2 → Jun 10 IST) ──")
    results = {}
    for sl, tp, label in VARIANTS:
        r = run_pure_sltp(start_usd, leverage=3.0, sl_pct=sl, tp_pct=tp)
        results[(sl, tp)] = r
        summarize(label, r, start_usd, today_only=False)

    print("\n  ── For reference: BASELINE v5.5 (trail) full window ──")
    print("  v5.5 baseline (trail+partial) n= 42  win%= 90.5  total=$+156.31 (+13,443 INR)")
    print()

    print("\n  ── JUNE 10 ONLY (today, IST) ──")
    for sl, tp, label in VARIANTS:
        summarize(label, results[(sl, tp)], start_usd, today_only=True)

    print("\n" + "=" * 110)
    print("  JUNE 10 trade audit — pure SL/TP exits verified at the price level")
    print("=" * 110)
    for sl, tp, label in VARIANTS:
        cutoff = pd.Timestamp("2026-06-10", tz="UTC")
        end    = pd.Timestamp("2026-06-11", tz="UTC")
        today = [t for t in results[(sl, tp)]["trades"] if cutoff <= t["entry_t"] < end]
        if not today:
            print(f"\n  {label}: no trades")
            continue
        print(f"\n  ── {label} ──")
        for t in today:
            ist = t["entry_t"].tz_convert("Asia/Kolkata").strftime("%H:%M IST")
            xist = t["exit_t"].tz_convert("Asia/Kolkata").strftime("%H:%M IST")
            held = (t["exit_t"] - t["entry_t"]).total_seconds() / 3600
            side = "LONG " if t["side"] == 1 else "SHORT"
            sgn = "+" if t["side"] == 1 else "-"
            print(f"    {t['asset']:<3} {side}  entry {ist} ${t['entry_px']:>10,.2f}  →  "
                  f"exit {xist} ${t['exit_px']:>10,.2f}  net {t['net_ret']*100:>+6.3f}%  ({held:>5.1f}h)")
            print(f"          SL=${t['sl_price']:>10,.2f}  TP=${t['tp_price']:>10,.2f}  "
                  f"LIQ=${t['liq_price']:>10,.2f} (3× lev)")
            print(f"          worst adverse move during trade: {t.get('max_adverse_pct',0)*100:>6.3f}%  "
                  f"(liq distance: {(1/3 - 0.005)*100:.2f}%)")
            print(f"          reason={t['exit_reason']:<11}  "
                  f"PnL=${t['pnl_usd']:>+6.2f} "
                  f"({'+' if t['pnl_usd']>=0 else ''}{t['pnl_usd']*USD_INR_RATE:>+5.0f} INR)")


if __name__ == "__main__":
    main()
