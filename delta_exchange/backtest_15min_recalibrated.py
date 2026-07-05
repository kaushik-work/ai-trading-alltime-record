"""
Backtest for the recalibrated v5.5 strategy.

Changes vs production backtest_engine.py:
  - ENTRY_PCT = 0.0007 (was 0.006)
  - Entry decisions every 15 minutes (:00, :15, :30, :45)
  - Signal history sampled every 5 minutes (matches live tick_signal_sample)
  - 1m high/low exits (realistic)
  - Exits: pure SL/TP 1.5% / 1.0% by default; trail_partial optional
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

# ── recalibrated dials ───────────────────────────────────────────────────────
USD_INR_RATE = 86.0
CAPITAL_USE_PCT = 0.50
PERSIST_HOURS = 1
ENTRY_PCT = 0.0007
MIN_STRIKES = 3
TT_MIN_HOURS = 6
TT_MAX_HOURS = 72
MONEYNESS = 0.05
MAX_HOLD_HOURS = 72
SIZE_BASE_PCT = 0.005
SIZE_MIN_MULT = 0.5
SIZE_MAX_MULT = 3.0
MAX_CONCURRENT = 2
PERP_FEE_BPS = 5.0
SLIPPAGE_BPS = 2.0
MAINT_MARGIN_PCT = 0.005

DEFAULT_STOP_PCT = 0.015
DEFAULT_TP_PCT = 0.010
DEFAULT_TRAIL_PEAK_PCT = 0.005
DEFAULT_TRAIL_GIVEBACK = 0.0025


def parse_symbol(sym: str):
    parts = sym.split("-")
    side, strike = parts[0], int(parts[2])
    dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
    return side, strike, pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")


def load_data(subdir: str, perp_symbol: str):
    base = Path(__file__).parent / "data" / subdir
    perp = pd.read_csv(base / "perp" / f"{perp_symbol}_mark_1m.csv")
    perp["timestamp"] = pd.to_datetime(perp["time"], unit="s", utc=True)
    perp = perp.set_index("timestamp").sort_index()
    marks, rows = {}, []
    for p in sorted((base / "options").glob("*_mark_1m.csv")):
        sym = p.name.replace("_mark_1m.csv", "")
        df = pd.read_csv(p)
        if df.empty: continue
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        marks[sym] = df.set_index("timestamp")["close"].sort_index()
        try:
            side, strike, exp = parse_symbol(sym)
            rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": exp})
        except Exception: pass
    return perp, marks, pd.DataFrame(rows)


def load_hl(subdir: str, perp_symbol: str) -> pd.DataFrame:
    p = Path(__file__).parent / "data" / subdir / "perp" / f"{perp_symbol}_mark_1m.csv"
    df = pd.read_csv(p)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")[["high", "low", "close"]].sort_index()


def compute_pred(t, spot, catalogue, marks):
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


def _check_exit_trail_partial(pos, t, hl_slice, perp_close_at_t, sl_pct, tp_pct,
                               trail_peak_pct, trail_giveback, equity_ref):
    side = pos["side"]; entry_px = pos["entry_px"]
    unreal = side * (perp_close_at_t - entry_px) / entry_px
    pos["peak"] = max(pos.get("peak", 0.0), unreal)
    events = []
    if (not pos.get("tp_taken")) and unreal >= tp_pct:
        half = pos["notional"] * 0.5
        fill = perp_close_at_t * (1 - side * SLIPPAGE_BPS / 1e4)
        raw = side * (fill - entry_px) / entry_px
        net = raw - 2 * PERP_FEE_BPS / 1e4
        pnl = half * net
        equity_ref[0] += pnl
        pos["notional"] -= half; pos["tp_taken"] = True
        events.append({"event": "partial_tp", "exit_t": t, "exit_px": fill,
                       "pnl_usd": pnl, "net_ret": net, "equity_after": equity_ref[0]})
    held_h = (t - pos["entry_t"]).total_seconds() / 3600
    reason = None
    if t >= pos["expiry"]: reason = "expiry"
    elif held_h >= MAX_HOLD_HOURS: reason = "max_hold"
    elif unreal < -sl_pct: reason = "stop"
    elif pos["peak"] >= trail_peak_pct and (pos["peak"] - unreal) > trail_giveback:
        reason = "trail"
    if reason:
        fill = perp_close_at_t * (1 - side * SLIPPAGE_BPS / 1e4)
        raw = side * (fill - entry_px) / entry_px
        net = raw - 2 * PERP_FEE_BPS / 1e4
        pnl = pos["notional"] * net
        equity_ref[0] += pnl
        events.append({"event": reason, "exit_t": t, "exit_px": fill,
                       "pnl_usd": pnl, "net_ret": net, "equity_after": equity_ref[0]})
        return events, True
    return events, False


def _check_exit_pure_sltp(pos, t, hl_slice, perp_close_at_t, sl_pct, tp_pct,
                          leverage, equity_ref):
    side = pos["side"]; entry_px = pos["entry_px"]
    sl_price = entry_px * (1 - side * sl_pct)
    tp_price = entry_px * (1 + side * tp_pct)
    liq_pct = (1.0 / leverage) - MAINT_MARGIN_PCT
    liq_price = entry_px * (1 - side * liq_pct)
    max_adverse = pos.get("max_adverse_pct", 0.0)
    exit_t = None; exit_px = None; reason = None
    for mt, row in hl_slice.iterrows():
        adv = -side * ((row["low"] if side == 1 else row["high"]) - entry_px) / entry_px
        if adv > max_adverse: max_adverse = adv
        if side == 1:
            if row["low"] <= liq_price: exit_t = mt; exit_px = liq_price; reason = "LIQUIDATION"; break
            if row["low"] <= sl_price: exit_t = mt; exit_px = sl_price; reason = "stop"; break
            if row["high"] >= tp_price: exit_t = mt; exit_px = tp_price; reason = "target"; break
        else:
            if row["high"] >= liq_price: exit_t = mt; exit_px = liq_price; reason = "LIQUIDATION"; break
            if row["high"] >= sl_price: exit_t = mt; exit_px = sl_price; reason = "stop"; break
            if row["low"] <= tp_price: exit_t = mt; exit_px = tp_price; reason = "target"; break
    pos["max_adverse_pct"] = max_adverse
    if reason is None:
        held_h = (t - pos["entry_t"]).total_seconds() / 3600
        if t >= pos["expiry"]: reason = "expiry"; exit_t = t; exit_px = perp_close_at_t
        elif held_h >= MAX_HOLD_HOURS: reason = "max_hold"; exit_t = t; exit_px = perp_close_at_t
    if reason is None:
        return [], False
    fill = exit_px * (1 - side * SLIPPAGE_BPS / 1e4)
    raw = side * (fill - entry_px) / entry_px
    net = raw - 2 * PERP_FEE_BPS / 1e4
    pnl = pos["notional"] * net
    equity_ref[0] += pnl
    return [{"event": reason, "exit_t": exit_t, "exit_px": fill,
             "pnl_usd": pnl, "net_ret": net, "equity_after": equity_ref[0],
             "sl_price": sl_price, "tp_price": tp_price, "liq_price": liq_price,
             "max_adverse_pct": max_adverse}], True


def run_backtest(regime: str, start_usd: float, leverage: float = 3.0,
                 sl_pct: float = DEFAULT_STOP_PCT, tp_pct: float = DEFAULT_TP_PCT,
                 trail_peak_pct: float = DEFAULT_TRAIL_PEAK_PCT,
                 trail_giveback: float = DEFAULT_TRAIL_GIVEBACK) -> dict:
    btc_perp, btc_marks, btc_cat = load_data("june_daily_btc_focused", "BTCUSD")
    eth_perp, eth_marks, eth_cat = load_data("june_daily_eth_focused", "ETHUSD")
    btc_hl = load_hl("june_daily_btc_focused", "BTCUSD")
    eth_hl = load_hl("june_daily_eth_focused", "ETHUSD")

    equity = [start_usd]
    sig_hist = {"BTC": {}, "ETH": {}}
    open_pos = []
    trades = []

    # 5-minute signal sampling (matches live tick_signal_sample)
    sample_times = pd.date_range(btc_perp.index.min(), btc_perp.index.max(), freq="5min", tz="UTC")
    # 15-minute entry decisions
    decision_times = pd.date_range(btc_perp.index.min(), btc_perp.index.max(), freq="15min", tz="UTC")
    all_times = sorted(set(sample_times) | set(decision_times))

    for t in all_times:
        is_sample = t in sample_times
        is_decision = t in decision_times

        # Build signal history every 5 min
        if is_sample:
            if t in btc_perp.index:
                for p in compute_pred(t, float(btc_perp.loc[t, "close"]), btc_cat, btc_marks):
                    sig_hist["BTC"].setdefault(p["expiry"], []).append((t, p["pred"]))
            if t in eth_perp.index:
                for p in compute_pred(t, float(eth_perp.loc[t, "close"]), eth_cat, eth_marks):
                    sig_hist["ETH"].setdefault(p["expiry"], []).append((t, p["pred"]))
            for ak in ("BTC", "ETH"):
                for exp in list(sig_hist[ak].keys()):
                    sig_hist[ak][exp] = [(ti, pi) for ti, pi in sig_hist[ak][exp]
                                         if (t - ti).total_seconds() <= 6 * 3600]

        # Manage open positions every decision tick
        if is_decision:
            still_open = []
            for pos in open_pos:
                ak = pos["asset"]
                perp = btc_perp if ak == "BTC" else eth_perp
                hl = btc_hl if ak == "BTC" else eth_hl
                if t not in perp.index:
                    still_open.append(pos); continue
                close_px = float(perp.loc[t, "close"])
                if regime == "trail_partial":
                    events, closed = _check_exit_trail_partial(
                        pos, t, None, close_px, sl_pct, tp_pct,
                        trail_peak_pct, trail_giveback, equity)
                else:
                    check_from = pos.get("last_check_t", pos["entry_t"])
                    hl_slice = hl[(hl.index > check_from) & (hl.index <= t)]
                    events, closed = _check_exit_pure_sltp(
                        pos, t, hl_slice, close_px, sl_pct, tp_pct, leverage, equity)
                    pos["last_check_t"] = t
                for ev in events:
                    trades.append({**pos, **ev})
                if not closed:
                    still_open.append(pos)
            open_pos = still_open

            # Consider entries
            if len(open_pos) >= MAX_CONCURRENT: continue
            for ak in ("BTC", "ETH"):
                if len(open_pos) >= MAX_CONCURRENT: break
                if ak in [p["asset"] for p in open_pos]: continue
                perp = btc_perp if ak == "BTC" else eth_perp
                cat = btc_cat if ak == "BTC" else eth_cat
                marks_ = btc_marks if ak == "BTC" else eth_marks
                if t not in perp.index: continue
                spot = float(perp.loc[t, "close"])
                preds = compute_pred(t, spot, cat, marks_)
                candidates = sorted(preds, key=lambda p: -abs(p["pred"]))
                chosen = None
                already = {p["expiry"] for p in open_pos}
                for c in candidates:
                    if c["expiry"] in already: continue
                    if abs(c["pred"]) < ENTRY_PCT: break
                    hist = sig_hist[ak].get(c["expiry"], [])
                    recent = [pi for ti, pi in hist
                              if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
                    if len(recent) < PERSIST_HOURS: continue
                    if sum(1 for pi in recent if np.sign(pi) == np.sign(c["pred"])) < PERSIST_HOURS:
                        continue
                    chosen = c; break
                if chosen is None: continue

                pred = chosen["pred"]; side = 1 if pred > 0 else -1
                eq_eff = equity[0] * CAPITAL_USE_PCT
                sm = min(SIZE_MAX_MULT, max(SIZE_MIN_MULT, abs(pred) / SIZE_BASE_PCT))
                desired = eq_eff * sm
                margin_used = sum(p["notional"] / leverage for p in open_pos)
                margin_avail = max(0.0, equity[0] - margin_used)
                notional = min(desired, margin_avail * leverage)
                if notional <= 0: continue
                fill = spot * (1 + side * SLIPPAGE_BPS / 1e4)
                open_pos.append({"asset": ak, "entry_t": t, "entry_px": fill, "side": side,
                                 "expiry": chosen["expiry"], "notional": notional,
                                 "size_mult": sm, "pred": pred, "peak": 0.0,
                                 "leverage": leverage,
                                 "last_check_t": t if regime == "pure_sltp" else None})

    return {"regime": regime, "trades": trades, "equity_final": equity[0]}


def _summary(result: dict, start_usd: float) -> dict:
    trades = result["trades"]
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "final": start_usd, "pf": 0.0}
    pnls = [t["pnl_usd"] for t in trades]
    wins = [p for p in pnls if p > 0]
    total = sum(pnls)
    pf = sum(wins) / abs(sum(p for p in pnls if p <= 0)) if any(p <= 0 for p in pnls) else float("inf")
    return {"n": n, "wr": len(wins) / n * 100, "pnl": total, "final": result["equity_final"], "pf": pf}


def _print_trade_brief(trades: list):
    if not trades:
        print("  No trades fired")
        return
    print(f"\n  Per-trade summary (first/last 5 of {len(trades)}):")
    for t in trades[:5] + trades[-5:]:
        ist = t["entry_t"].tz_convert("Asia/Kolkata").strftime("%m-%d %H:%M IST")
        side = "LONG " if t["side"] == 1 else "SHORT"
        held = (t["exit_t"] - t["entry_t"]).total_seconds() / 60
        print(f"    {t['asset']:<3} {side}  {ist}  pred={t['pred']*100:+.3f}%  "
              f"net={t['net_ret']*100:+.3f}%  {held:>5.1f}min  {t['event']:<11}  "
              f"PnL=${t['pnl_usd']:>+6.2f}")


if __name__ == "__main__":
    start_usd = 40_000.0 / USD_INR_RATE
    print("=" * 110)
    print("  RECALIBRATED v5.5 BACKTEST")
    print("  Gate=0.07% | Entries every 15 min | 5m signal sampling | 1m realistic exits")
    print("=" * 110)
    print(f"\n  Seed: ${start_usd:.2f} USD ({40_000/USD_INR_RATE*USD_INR_RATE:.0f} INR @ {USD_INR_RATE})")

    for regime in ("pure_sltp", "trail_partial"):
        print(f"\n  --- {regime} ---")
        res = run_backtest(regime, start_usd, leverage=3.0)
        s = _summary(res, start_usd)
        print(f"  n={s['n']:>3}  WR={s['wr']:>5.1f}%  P&L=${s['pnl']:>+7.2f}  "
              f"final=${s['final']:>7.2f}  PF={s['pf']:>6.2f}")
        _print_trade_brief(res["trades"])

        reasons = {}
        for t in res["trades"]:
            reasons[t["event"]] = reasons.get(t["event"], 0) + 1
        if reasons:
            print(f"  Exit reasons: {reasons}")
