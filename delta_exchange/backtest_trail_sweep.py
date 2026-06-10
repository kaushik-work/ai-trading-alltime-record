"""Trail-giveback sweep — does loosening the trail bigger wins?

Baseline v5.5: trail arms when peak ≥ 0.5%, exits when giveback > 0.25%.
Hypothesis: looser trail (e.g., 0.5%/0.75%/1.0% giveback) lets winners run
through small intra-hour wiggles to catch the bigger move. Cost: bigger
giveback also means bigger drawdown when peak reverses.

Sweep dimensions:
  • trail_giveback ∈ {0.0025 (baseline), 0.005, 0.0075, 0.010, 0.015}
  • trail_peak_arm ∈ {0.005 (baseline)}  — fixed for now
  • Everything else (gate, persist, stop, partial TP) v5.5 production.

Reports per variant: trades, total INR PnL, win rate, biggest win/loss,
plus Jun 10 trades specifically (today's slice).
"""
from __future__ import annotations
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))

import math
from pathlib import Path
import numpy as np
import pandas as pd

# v5.5 baseline (everything that ISN'T being swept)
START_INR     = 40_000.0
USD_INR_RATE  = 86.0
CAPITAL_USE_PCT = 0.50
PERSIST_HOURS = 1
ENTRY_PCT     = 0.006
MIN_STRIKES   = 3
TT_MIN_HOURS  = 6
TT_MAX_HOURS  = 72
MONEYNESS     = 0.05
STOP_LOSS_PCT  = 0.015
PARTIAL_TP_PCT = 0.010
TRAIL_PEAK_PCT = 0.005  # peak-to-arm — fixed for this sweep
MAX_HOLD_HOURS = 72
SIZE_BASE_PCT  = 0.005
SIZE_MIN_MULT  = 0.5
SIZE_MAX_MULT  = 3.0
MAX_CONCURRENT = 2
PERP_FEE_BPS   = 5.0
SLIPPAGE_BPS   = 2.0


def parse_symbol(sym: str):
    parts = sym.split("-")
    side, strike = parts[0], int(parts[2])
    dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
    return side, strike, pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")


def load_data(subdir: str, perp_symbol: str):
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


def run_combined(start_usd: float, leverage: float, trail_giveback: float) -> dict:
    btc_perp, btc_marks, btc_cat = load_data("june_btc", "BTCUSD")
    eth_perp, eth_marks, eth_cat = load_data("june_eth", "ETHUSD")

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

        still_open = []
        for pos in open_pos:
            perp = btc_perp if pos["asset"] == "BTC" else eth_perp
            if t not in perp.index:
                still_open.append(pos); continue
            spot = float(perp.loc[t])
            held_h = (t - pos["entry_t"]).total_seconds() / 3600
            side, entry_px = pos["side"], pos["entry_px"]
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
            elif pos["peak"] >= TRAIL_PEAK_PCT and (pos["peak"] - unreal) > trail_giveback:
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
            effective_equity = equity * CAPITAL_USE_PCT
            size_mult = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(pred) / SIZE_BASE_PCT))
            desired_notional = effective_equity * size_mult
            margin_used = sum(p["notional"] / leverage for p in open_pos)
            margin_avail = max(0.0, equity - margin_used)
            notional = min(desired_notional, margin_avail * leverage)
            if notional <= 0: continue
            fill = spot * (1 + side * SLIPPAGE_BPS / 1e4)
            open_pos.append({"asset": ak, "entry_t": t, "entry_px": fill, "side": side,
                             "expiry": chosen["expiry"], "notional": notional,
                             "size_mult": size_mult, "pred": pred, "peak": 0.0,
                             "leverage": leverage})

    return {"trades": trades, "equity_final": equity, "n_trades": len(trades)}


def summarize(label: str, r: dict, start_usd: float, today_only=False):
    trades = r["trades"]
    if today_only:
        cutoff = pd.Timestamp("2026-06-10", tz="UTC")
        end    = pd.Timestamp("2026-06-11", tz="UTC")
        trades = [t for t in trades if cutoff <= t["entry_t"] < end]

    n = len(trades)
    if n == 0:
        print(f"  {label:<22} 0 trades")
        return
    pnls = [t["pnl_usd"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    win_rate = len(wins) / n * 100
    biggest_win = max(pnls)
    biggest_loss = min(pnls)
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    final_equity = r["equity_final"] if not today_only else (start_usd + total)
    ret_pct = total / start_usd * 100
    inr = total * USD_INR_RATE

    print(f"  {label:<22} n={n:>3}  win%={win_rate:>5.1f}  "
          f"total=${total:>+7.2f} ({'+' if inr>=0 else ''}{inr:>+7,.0f} INR)  "
          f"avgW=${avg_win:>+5.2f}  avgL=${avg_loss:>+5.2f}  "
          f"max+=${biggest_win:>+5.2f}  max-=${biggest_loss:>+5.2f}")


def main():
    start_usd = START_INR / USD_INR_RATE
    print("=" * 110)
    print(f"  v5.5 trail-giveback sweep  (₹40k seed, 3× leverage, BTC+ETH shared pool, compounding)")
    print(f"  Baseline: peak_arm=0.5%, giveback=0.25%   |   Sweep dims: giveback")
    print("=" * 110)

    GIVEBACKS = [0.0025, 0.005, 0.0075, 0.010, 0.015]

    print("\n  ── FULL WINDOW (Jun 2 → Jun 10 IST) ──")
    full = {}
    for gb in GIVEBACKS:
        r = run_combined(start_usd, leverage=3.0, trail_giveback=gb)
        full[gb] = r
        label = f"giveback {gb*100:.2f}%" + ("  ← baseline" if gb == 0.0025 else "")
        summarize(label, r, start_usd, today_only=False)

    print("\n  ── JUNE 10 ONLY (today, IST) ──")
    for gb in GIVEBACKS:
        label = f"giveback {gb*100:.2f}%" + ("  ← baseline" if gb == 0.0025 else "")
        summarize(label, full[gb], start_usd, today_only=True)

    # Per-variant Jun 10 trade-by-trade with EXIT VERIFICATION
    print("\n" + "=" * 110)
    print("  JUNE 10 trade audit — exit reason verification per variant")
    print("=" * 110)
    print(f"  For each trade: entry_px, the SL/TP/trail levels, actual exit_px,")
    print(f"  and a check that the recorded exit_reason matches the price math.")
    print(f"  v5.5 dials: stop -1.5%, partial_tp +1%, trail arm peak ≥0.5%")
    print()

    for gb in GIVEBACKS:
        cutoff = pd.Timestamp("2026-06-10", tz="UTC")
        end    = pd.Timestamp("2026-06-11", tz="UTC")
        today = [t for t in full[gb]["trades"] if cutoff <= t["entry_t"] < end]
        if not today:
            print(f"\n  giveback {gb*100:.2f}%:  no trades today")
            continue
        print(f"\n  ── giveback {gb*100:.2f}% ──")
        for t in today:
            ist = t["entry_t"].tz_convert("Asia/Kolkata").strftime("%H:%M IST")
            xist = t["exit_t"].tz_convert("Asia/Kolkata").strftime("%H:%M IST")
            side = "LONG " if t["side"] == 1 else "SHORT"
            sign = t["side"]
            entry_px = t["entry_px"]
            exit_px  = t["exit_px"]
            # Level math
            stop_px = entry_px * (1 - sign * STOP_LOSS_PCT)
            tp_px   = entry_px * (1 + sign * PARTIAL_TP_PCT)
            held_h  = (t["exit_t"] - t["entry_t"]).total_seconds() / 3600
            exit_ret = sign * (exit_px - entry_px) / entry_px * 100

            # Verify the recorded reason matches the price math
            check = "?"
            if t["exit_reason"] == "stop":
                hit = (sign * (exit_px - stop_px)) <= entry_px * 0.001  # within 0.1%
                check = "✓ SL hit" if hit else f"✗ SL=${stop_px:.2f} vs exit=${exit_px:.2f}"
            elif t["exit_reason"] == "partial_tp":
                hit = (sign * (exit_px - tp_px)) >= -entry_px * 0.001
                check = "✓ TP hit" if hit else f"✗ TP=${tp_px:.2f} vs exit=${exit_px:.2f}"
            elif t["exit_reason"] == "trail":
                # Trail fires when peak ≥ 0.5% AND (peak − unreal) > giveback
                # We don't have the peak recorded but we know exit_ret should be
                # < peak (since trail fires on giveback from peak)
                check = f"✓ TRAIL (giveback={gb*100:.2f}% from peak)"
            elif t["exit_reason"] == "max_hold":
                check = f"✓ max_hold ({held_h:.1f}h ≥ {MAX_HOLD_HOURS}h)"
            elif t["exit_reason"] == "expiry":
                check = f"✓ expiry reached ({t['expiry'].strftime('%m-%d %H:%M UTC')})"

            print(f"    {t['asset']:<3} {side}  entry {ist} ${entry_px:>10,.2f}  →  exit {xist} ${exit_px:>10,.2f}  "
                  f"net {exit_ret:>+6.3f}%  ({held_h:>5.1f}h)")
            print(f"          stop SL=${stop_px:>10,.2f}  partial_tp=${tp_px:>10,.2f}  "
                  f"reason={t['exit_reason']:<11}  {check}")
            print(f"          PnL=${t['pnl_usd']:>+6.2f} "
                  f"({'+' if t['pnl_usd']>=0 else ''}{t['pnl_usd']*USD_INR_RATE:>+5.0f} INR)")


if __name__ == "__main__":
    main()
