"""Unified backtest engine — runs BOTH exit regimes on every call.

Two paradigms, same entry side (v5.5 gate 0.6% + persist 1h + ≥3 strikes):

  1. trail_partial — current live config: partial TP at +1% (closes half),
     trail arm at peak ≥0.5%, giveback >0.25% exits the rest.

  2. pure_sltp     — bracket order: full exit on SL (1.5%) or TP (1.0%).
     No trail, no partial. Tracks liquidation distance at the configured
     leverage so we can see the safety margin per trade.

Public API:
  - load_data(asset_subdir, symbol)        → (perp_series, marks_dict, catalogue_df)
  - load_hl(asset_subdir, symbol)          → DataFrame of 1m high/low/close
  - run_backtest(regime, **kwargs)         → result dict for one regime
  - run_both(**kwargs)                     → {'trail_partial': r, 'pure_sltp': r}
  - print_comparison(both, today_ist=...)  → side-by-side summary + per-trade
  - filter_trades(trades, today_ist)       → slice trades to one IST day

Usage:
  from backtest_engine import run_both, print_comparison
  results = run_both(start_usd=465.12, leverage=3.0)
  print_comparison(results, today_ist="2026-06-10")
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ── v5.5 production dials (entry-side identical for BOTH regimes) ────────────
USD_INR_RATE   = 86.0
CAPITAL_USE_PCT = 0.50
PERSIST_HOURS  = 1
ENTRY_PCT      = 0.006
MIN_STRIKES    = 3
TT_MIN_HOURS   = 6
TT_MAX_HOURS   = 72
MONEYNESS      = 0.05
MAX_HOLD_HOURS = 72
SIZE_BASE_PCT  = 0.005
SIZE_MIN_MULT  = 0.5
SIZE_MAX_MULT  = 3.0
MAX_CONCURRENT = 2
PERP_FEE_BPS   = 5.0
SLIPPAGE_BPS   = 2.0
MAINT_MARGIN_PCT = 0.005    # Delta India BTCUSD/ETHUSD maintenance margin

# Exit dials (per-regime; defaults baked into run_backtest signature)
DEFAULT_STOP_PCT       = 0.015
DEFAULT_TP_PCT         = 0.010
DEFAULT_TRAIL_PEAK_PCT = 0.005
DEFAULT_TRAIL_GIVEBACK = 0.0025


def parse_symbol(sym: str):
    parts = sym.split("-")
    side, strike = parts[0], int(parts[2])
    dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
    return side, strike, pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")


def load_data(subdir: str, perp_symbol: str):
    """Returns (perp_close_series, option_marks_dict, catalogue_df). Times UTC."""
    base = Path(__file__).parent / "data" / subdir
    perp = pd.read_csv(base / "perp" / f"{perp_symbol}_mark_1m.csv")
    perp["timestamp"] = pd.to_datetime(perp["time"], unit="s", utc=True)
    perp = perp.set_index("timestamp")["close"].sort_index()
    marks, rows = {}, []
    # Option marks are now stored at 1m resolution (Delta's 1h archive is
    # broken — see fetch_delta_history.py OPT_RESOLUTION comment). The
    # backtest only needs HH:00:00 UTC samples for its hourly decisions,
    # so we look up exactly those rows from the 1m series.
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
    """1m high/low/close — required by pure_sltp for intra-hour SL/TP checks."""
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


# ─────────────────────────────────────────────────────────────────────────────
#  EXIT REGIMES — both walk the open position at each hourly tick.
#  Each returns a list of trade events (partial_tp, stop, target, trail,
#  max_hold, expiry) and returns whether the position has fully closed.
# ─────────────────────────────────────────────────────────────────────────────

def _check_exit_trail_partial(pos, t, hl_slice, perp_close_at_t, sl_pct, tp_pct,
                               trail_peak_pct, trail_giveback, equity_ref):
    """v5.5 baseline. Uses HOURLY closes (not intra-hour) to match the
    existing backtest_user_capital_june.py behavior exactly. Partial TP
    closes half; trail arms at peak then exits the rest on giveback."""
    side = pos["side"]; entry_px = pos["entry_px"]
    unreal = side * (perp_close_at_t - entry_px) / entry_px
    pos["peak"] = max(pos.get("peak", 0.0), unreal)
    events = []
    # Partial TP — close half once
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
    if t >= pos["expiry"]:                  reason = "expiry"
    elif held_h >= MAX_HOLD_HOURS:          reason = "max_hold"
    elif unreal < -sl_pct:                  reason = "stop"
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
    """Pure bracket: walks INTRA-HOUR 1m high/low. Liquidation also tracked
    (overrides SL if a 1m bar wicks past the liq distance — won't happen at
    3× with 1.5% SL but the check is in place)."""
    side = pos["side"]; entry_px = pos["entry_px"]
    sl_price = entry_px * (1 - side * sl_pct)
    tp_price = entry_px * (1 + side * tp_pct)
    liq_pct = (1.0 / leverage) - MAINT_MARGIN_PCT
    liq_price = entry_px * (1 - side * liq_pct)
    pos["liq_price"] = liq_price
    max_adverse = pos.get("max_adverse_pct", 0.0)
    exit_t = None; exit_px = None; reason = None
    for mt, row in hl_slice.iterrows():
        adv = -side * ((row["low" if side == 1 else "high"]) - entry_px) / entry_px
        if adv > max_adverse: max_adverse = adv
        if side == 1:
            if row["low"] <= liq_price:
                exit_t = mt; exit_px = liq_price; reason = "LIQUIDATION"; break
            if row["low"] <= sl_price:
                exit_t = mt; exit_px = sl_price; reason = "stop"; break
            if row["high"] >= tp_price:
                exit_t = mt; exit_px = tp_price; reason = "target"; break
        else:
            if row["high"] >= liq_price:
                exit_t = mt; exit_px = liq_price; reason = "LIQUIDATION"; break
            if row["high"] >= sl_price:
                exit_t = mt; exit_px = sl_price; reason = "stop"; break
            if row["low"] <= tp_price:
                exit_t = mt; exit_px = tp_price; reason = "target"; break
    pos["max_adverse_pct"] = max_adverse
    if reason is None:
        held_h = (t - pos["entry_t"]).total_seconds() / 3600
        if t >= pos["expiry"]:           reason = "expiry";   exit_t = t; exit_px = perp_close_at_t
        elif held_h >= MAX_HOLD_HOURS:   reason = "max_hold"; exit_t = t; exit_px = perp_close_at_t
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


# ─────────────────────────────────────────────────────────────────────────────
#  CORE RUNNER — single function, regime-switched
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(regime: str, start_usd: float, leverage: float = 3.0,
                 sl_pct: float = DEFAULT_STOP_PCT, tp_pct: float = DEFAULT_TP_PCT,
                 trail_peak_pct: float = DEFAULT_TRAIL_PEAK_PCT,
                 trail_giveback: float = DEFAULT_TRAIL_GIVEBACK) -> dict:
    """regime ∈ {'trail_partial', 'pure_sltp'}. Returns:
        {trades, equity_final, n_trades, regime, dials}.
    Trades carry the exit event (partial_tp/stop/target/trail/etc.) per entry."""
    assert regime in ("trail_partial", "pure_sltp"), f"unknown regime: {regime}"
    btc_perp, btc_marks, btc_cat = load_data("june_btc", "BTCUSD")
    eth_perp, eth_marks, eth_cat = load_data("june_eth", "ETHUSD")
    btc_hl = load_hl("june_btc", "BTCUSD") if regime == "pure_sltp" else None
    eth_hl = load_hl("june_eth", "ETHUSD") if regime == "pure_sltp" else None

    equity = [start_usd]   # boxed for in-place mutation
    sig_hist = {"BTC": {}, "ETH": {}}
    open_pos: list = []
    trades: list = []
    hours = sorted(set(btc_perp.index[(btc_perp.index.minute == 0) & (btc_perp.index.second == 0)])
                  | set(eth_perp.index[(eth_perp.index.minute == 0) & (eth_perp.index.second == 0)]))

    for t in hours:
        # build sig_hist
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

        # manage opens
        still_open = []
        for pos in open_pos:
            ak = pos["asset"]
            perp = btc_perp if ak == "BTC" else eth_perp
            hl = btc_hl if ak == "BTC" else eth_hl
            if t not in perp.index:
                still_open.append(pos); continue
            close_px = float(perp.loc[t])
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

        # consider entries
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

    return {"regime": regime, "trades": trades, "equity_final": equity[0],
            "n_trades": len(trades),
            "dials": {"leverage": leverage, "sl_pct": sl_pct, "tp_pct": tp_pct,
                      "trail_peak_pct": trail_peak_pct, "trail_giveback": trail_giveback}}


def run_both(start_usd: float, leverage: float = 3.0, **kwargs) -> dict:
    """Runs BOTH regimes with the same dials. Returns dict keyed by regime."""
    return {
        "trail_partial": run_backtest("trail_partial", start_usd, leverage, **kwargs),
        "pure_sltp":     run_backtest("pure_sltp",     start_usd, leverage, **kwargs),
    }


def filter_trades(trades: list, ist_date: str) -> list:
    """Return only trades whose ENTRY falls on the given IST date (YYYY-MM-DD)."""
    start_ist = pd.Timestamp(f"{ist_date} 00:00:00", tz="Asia/Kolkata")
    end_ist   = start_ist + pd.Timedelta(days=1)
    start_utc = start_ist.tz_convert("UTC")
    end_utc   = end_ist.tz_convert("UTC")
    return [t for t in trades if start_utc <= t["entry_t"] < end_utc]


def _summary_line(label: str, result: dict, today_ist: Optional[str], start_usd: float):
    trades = result["trades"]
    if today_ist:
        trades = filter_trades(trades, today_ist)
    n = len(trades)
    if n == 0:
        return f"  {label:<26} 0 trades"
    pnls = [t["pnl_usd"] for t in trades]
    wins = [p for p in pnls if p > 0]
    total = sum(pnls)
    wr = len(wins) / n * 100
    inr = total * USD_INR_RATE
    by_reason = {}
    for t in trades:
        by_reason[t["event"]] = by_reason.get(t["event"], 0) + 1
    rs = ", ".join(f"{k}:{v}" for k, v in sorted(by_reason.items()))
    return (f"  {label:<26} n={n:>3}  win%={wr:>5.1f}  "
            f"total=${total:>+7.2f} ({'+' if inr>=0 else ''}{inr:>+7,.0f} INR)  "
            f"reasons=[{rs}]")


def print_comparison(both: dict, today_ist: Optional[str] = None, start_usd: float = 0.0):
    """Side-by-side comparison of trail_partial vs pure_sltp, full window + today."""
    print("=" * 110)
    print("  BACKTEST — both regimes (entry side identical, exit logic differs)")
    print("=" * 110)
    print(f"\n  ── FULL WINDOW ──")
    print(_summary_line("v5.5 trail+partial", both["trail_partial"], None, start_usd))
    print(_summary_line("pure SL/TP",        both["pure_sltp"],     None, start_usd))
    if today_ist:
        print(f"\n  ── {today_ist} IST ONLY ──")
        print(_summary_line("v5.5 trail+partial", both["trail_partial"], today_ist, start_usd))
        print(_summary_line("pure SL/TP",        both["pure_sltp"],     today_ist, start_usd))
        print(f"\n  ── per-trade audit, {today_ist} IST ──")
        for label, key in [("v5.5 trail+partial", "trail_partial"), ("pure SL/TP", "pure_sltp")]:
            trades = filter_trades(both[key]["trades"], today_ist)
            if not trades:
                print(f"\n  {label}: no trades fired")
                continue
            print(f"\n  ── {label} ──")
            for t in trades:
                ist = t["entry_t"].tz_convert("Asia/Kolkata").strftime("%H:%M IST")
                xist = t["exit_t"].tz_convert("Asia/Kolkata").strftime("%H:%M IST")
                side = "LONG " if t["side"] == 1 else "SHORT"
                held = (t["exit_t"] - t["entry_t"]).total_seconds() / 3600
                extra = ""
                if "liq_price" in t and t.get("max_adverse_pct") is not None:
                    extra = (f"\n           LIQ=${t['liq_price']:>10,.2f} "
                             f"max_adverse={t['max_adverse_pct']*100:.3f}% "
                             f"(buffer {(1/t['leverage']-MAINT_MARGIN_PCT)*100:.2f}%)")
                print(f"    {t['asset']:<3} {side}  entry {ist} ${t['entry_px']:>10,.2f}  →  "
                      f"exit {xist} ${t['exit_px']:>10,.2f}  net {t['net_ret']*100:>+6.3f}%  "
                      f"({held:>5.1f}h)  reason={t['event']:<11}  "
                      f"PnL=${t['pnl_usd']:>+6.2f} ({'+' if t['pnl_usd']>=0 else ''}{t['pnl_usd']*USD_INR_RATE:>+5.0f} INR){extra}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    # Default smoke run: today's date in IST
    today = sys.argv[1] if len(sys.argv) > 1 else pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d")
    start_usd = 40_000.0 / USD_INR_RATE
    print(f"  Seed: ₹40,000  (${start_usd:.2f} @ {USD_INR_RATE} INR/USD)")
    print(f"  Today filter: {today} IST\n")
    both = run_both(start_usd=start_usd, leverage=3.0)
    print_comparison(both, today_ist=today, start_usd=start_usd)
