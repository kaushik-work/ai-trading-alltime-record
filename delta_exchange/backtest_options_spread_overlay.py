"""
S/R retest with options-spread execution overlay + R:R sweep.

For every S/R retest signal that the perp strategy would take, this backtest
also simulates buying a defined-risk call/put spread:
  LONG signal  -> Long ATM call, short +X% OTM call
  SHORT signal -> Long ATM put,  short -X% OTM put

The spread is held with the same exit trigger as the perp trade (SL / TP /
max-hold) and marked to market using actual Delta option 1m marks.

Usage:
    .venv/Scripts/python backtest_options_spread_overlay.py
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path
import numpy as np
import pandas as pd

from backtest_price_action_sweep import (
    load_perp, prepare, START_USD, LEVERAGE, CAPITAL_USE_PCT,
    LOOKBACK, TREND_LOOKBACK, MAX_HOLD_CANDLES, COOLDOWN_CANDLES,
)

SLIPPAGE_BPS   = 5.0            # per leg
OPTION_FEE_BPS = 5.0            # per leg entry + exit
MIN_DTE        = 2
MAX_DTE        = 14


def _parse_option_symbol(sym: str):
    parts = sym.split("-")
    side = parts[0]
    underlying = parts[1]
    strike = int(parts[2])
    dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
    exp = pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")
    return side, underlying, strike, exp


def _load_option_catalog(subdir: str):
    base = Path(__file__).parent / "data" / subdir / "options"
    rows = []
    for p in sorted(base.glob("*_mark_1m.csv")):
        sym = p.name.replace("_mark_1m.csv", "")
        try:
            side, underlying, strike, exp = _parse_option_symbol(sym)
        except Exception:
            continue
        df = pd.read_csv(p, usecols=["time"])
        if df.empty:
            continue
        tmin = pd.to_datetime(df["time"].min(), unit="s", utc=True)
        tmax = pd.to_datetime(df["time"].max(), unit="s", utc=True)
        rows.append({
            "symbol": sym, "side": side, "underlying": underlying,
            "strike": strike, "expiry": exp,
            "t_min": tmin, "t_max": tmax,
        })
    return pd.DataFrame(rows)


def _nearest_strike(available: np.ndarray, target: float) -> int:
    return int(available[np.argmin(np.abs(available - target))])


def _find_spread_legs(catalog: pd.DataFrame, side: str, underlying: str,
                      t_entry: pd.Timestamp, spot: float, otm_pct: float) -> tuple[str, str] | None:
    df = catalog[(catalog["side"] == side) &
                 (catalog["underlying"] == underlying) &
                 (catalog["t_min"] <= t_entry) &
                 (catalog["t_max"] >= t_entry) &
                 (catalog["expiry"] > t_entry + pd.Timedelta(days=MIN_DTE)) &
                 (catalog["expiry"] <= t_entry + pd.Timedelta(days=MAX_DTE))]
    if df.empty:
        return None

    atm = _nearest_strike(df["strike"].values, spot)
    if side == "C":
        otm_target = spot * (1 + otm_pct)
    else:
        otm_target = spot * (1 - otm_pct)
    otm = _nearest_strike(df["strike"].values, otm_target)
    if otm == atm:
        return None

    exp_candidates = df[df["strike"] == atm]["expiry"]
    if exp_candidates.empty:
        return None
    expiry = exp_candidates.iloc[np.argmin(np.abs(exp_candidates - (t_entry + pd.Timedelta(days=7))))]

    long_candidates = df[(df["strike"] == atm) & (df["expiry"] == expiry)]
    short_candidates = df[(df["strike"] == otm) & (df["expiry"] == expiry)]
    if long_candidates.empty or short_candidates.empty:
        return None
    return long_candidates.iloc[0]["symbol"], short_candidates.iloc[0]["symbol"]


def _load_option_series(subdir: str, sym: str) -> pd.Series:
    p = Path(__file__).parent / "data" / subdir / "options" / f"{sym}_mark_1m.csv"
    df = pd.read_csv(p)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


def _mark_at(series: pd.Series, t: pd.Timestamp) -> float | None:
    if series.empty:
        return None
    try:
        if t in series.index:
            return float(series.loc[t])
        idx = series.index.get_indexer([t], method="nearest")[0]
        if idx < 0:
            return None
        nearest_t = series.index[idx]
        if abs((nearest_t - t).total_seconds()) <= 120:
            return float(series.iloc[idx])
    except Exception:
        pass
    return None


def _spread_pnl_at_entry_exit(subdir: str, long_sym: str, short_sym: str,
                               t_entry: pd.Timestamp, t_exit: pd.Timestamp) -> tuple[float, float, float] | None:
    long_s = _load_option_series(subdir, long_sym)
    short_s = _load_option_series(subdir, short_sym)

    long_entry = _mark_at(long_s, t_entry)
    short_entry = _mark_at(short_s, t_entry)
    long_exit = _mark_at(long_s, t_exit)
    short_exit = _mark_at(short_s, t_exit)

    if any(v is None or v <= 0 for v in [long_entry, short_entry, long_exit, short_exit]):
        return None

    entry_cost = long_entry - short_entry
    exit_value = long_exit - short_exit
    if entry_cost <= 0:
        return None
    gross_pnl = exit_value - entry_cost
    gross_pnl_ratio = gross_pnl / entry_cost
    return entry_cost, exit_value, gross_pnl_ratio


def _close_spread(pos, t_exit, skipped, reason, subdir, underlying):
    legs = pos.get("legs")
    if not legs:
        skipped.append({
            "entry_time": pos["entry_time"], "side": pos["side"],
            "reason": "no_options", "spot": pos["entry"],
        })
        return None
    long_sym, short_sym = legs
    res = _spread_pnl_at_entry_exit(subdir, long_sym, short_sym,
                                    pos["entry_time"], t_exit)
    if res is None:
        skipped.append({
            "entry_time": pos["entry_time"], "side": pos["side"],
            "reason": "price_missing", "spot": pos["entry"],
            "legs": legs,
        })
        return None
    entry_cost, exit_value, gross_pnl_ratio = res
    net_pnl_ratio = gross_pnl_ratio - 4 * OPTION_FEE_BPS / 1e4
    return net_pnl_ratio


def run_overlay(subdir: str, sym: str, sl_pct: float, rr: float,
                otm_pct: float,
                date_start: pd.Timestamp | None = None,
                date_end: pd.Timestamp | None = None):
    underlying = sym.replace("USD", "")
    df = load_perp(subdir, sym)
    if date_start:
        df = df[df.index >= date_start]
    if date_end:
        df = df[df.index < date_end]
    if len(df) < max(LOOKBACK, TREND_LOOKBACK) + 100:
        return [], [], []

    catalog = _load_option_catalog(subdir)
    if catalog.empty:
        return [], [], []

    s = prepare(df, use_trend=True, retest_mode="wick_touch",
                body_pos_threshold=0.70, wick_touch_tol=0.0007)
    o, h, l, c = s["o"], s["h"], s["l"], s["c"]
    ts = df.index
    n = len(df)
    long_sig, short_sig = s["retest_long"], s["retest_short"]

    perp_rets = []
    spread_rets = []
    skipped = []

    pos = None
    cooldown = -1
    block_long_until = -1
    block_short_until = -1

    start_i = max(LOOKBACK, TREND_LOOKBACK) + 10
    for i in range(start_i, n - 1):
        t = ts[i]
        ci = c[i]

        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            hi, lo = h[i], l[i]
            reason = None
            exit_px = None

            if (sign > 0 and hi >= pos["tp"]) or (sign < 0 and lo <= pos["tp"]):
                reason = "tp"; exit_px = pos["tp"]
            else:
                stop = pos["sl"]
                if (sign > 0 and lo <= stop) or (sign < 0 and hi >= stop):
                    reason = "sl"; exit_px = stop
                elif i - pos["entry_idx"] >= MAX_HOLD_CANDLES:
                    reason = "hold"; exit_px = ci

            if reason:
                pnl = sign * (exit_px - pos["entry"]) / pos["entry"]
                perp_ret = pnl * LEVERAGE * CAPITAL_USE_PCT
                perp_rets.append(perp_ret)
                if pos.get("legs"):
                    spread_pnl = _close_spread(pos, t, skipped, reason, subdir, underlying)
                    if spread_pnl is not None:
                        spread_rets.append(spread_pnl * CAPITAL_USE_PCT)
                else:
                    _close_spread(pos, t, skipped, reason, subdir, underlying)
                pos = None
                cooldown = i + COOLDOWN_CANDLES
                if pnl <= 0:
                    block_long_until = i + 180 if sign > 0 else block_long_until
                    block_short_until = i + 180 if sign < 0 else block_short_until
            continue

        if i < cooldown:
            continue

        tp_pct = sl_pct * rr
        next_open = o[i + 1]
        t_entry = ts[i + 1]

        if long_sig[i] and i >= block_long_until:
            entry = next_open * (1 + SLIPPAGE_BPS / 10_000)
            stop_level = l[i] * (1 - SLIPPAGE_BPS / 10_000)
            sl_dist = max(sl_pct, (entry - stop_level) / entry)
            sl = entry * (1 - sl_dist)
            tp = entry * (1 + tp_pct)
            legs = _find_spread_legs(catalog, "C", underlying, t_entry, entry, otm_pct)
            pos = {
                "side": "long", "entry": entry, "sl": sl, "tp": tp,
                "entry_idx": i + 1, "entry_time": t_entry,
                "sl_pct": sl_dist, "tp_pct": tp_pct,
                "legs": legs, "spread_side": "C",
            }
            continue

        if short_sig[i] and i >= block_short_until:
            entry = next_open * (1 - SLIPPAGE_BPS / 10_000)
            stop_level = h[i] * (1 + SLIPPAGE_BPS / 10_000)
            sl_dist = max(sl_pct, (stop_level - entry) / entry)
            sl = entry * (1 + sl_dist)
            tp = entry * (1 - tp_pct)
            legs = _find_spread_legs(catalog, "P", underlying, t_entry, entry, otm_pct)
            pos = {
                "side": "short", "entry": entry, "sl": sl, "tp": tp,
                "entry_idx": i + 1, "entry_time": t_entry,
                "sl_pct": sl_dist, "tp_pct": tp_pct,
                "legs": legs, "spread_side": "P",
            }
            continue

    if pos:
        sign = 1 if pos["side"] == "long" else -1
        pnl = sign * (c[-1] - pos["entry"]) / pos["entry"]
        perp_ret = pnl * LEVERAGE * CAPITAL_USE_PCT
        perp_rets.append(perp_ret)
        if pos.get("legs"):
            spread_pnl = _close_spread(pos, ts[-1], skipped, "eof", subdir, underlying)
            if spread_pnl is not None:
                spread_rets.append(spread_pnl * CAPITAL_USE_PCT)
        else:
            _close_spread(pos, ts[-1], skipped, "eof", subdir, underlying)

    return perp_rets, spread_rets, skipped


def _equity_curve(returns):
    eq = [START_USD]
    for r in returns:
        eq.append(eq[-1] * (1 + r))
    return np.array(eq)


def _summarize(returns):
    if not returns:
        return {"trades": 0}
    eq = _equity_curve(returns)
    total_ret = (eq[-1] - START_USD) / START_USD * 100
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    wr = len(wins) / len(returns) * 100
    gp, gl = sum(wins), abs(sum(losses))
    pf = gp / gl if gl > 0 else float("inf")
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / peak
    max_dd = dd.max() * 100
    avg_win = np.mean(wins) * 100 if wins else 0
    avg_loss = np.mean(losses) * 100 if losses else 0
    max_cl = 0
    cl = 0
    for r in returns:
        if r <= 0:
            cl += 1
            max_cl = max(max_cl, cl)
        else:
            cl = 0
    return {
        "trades": len(returns), "wr": wr, "pf": pf, "ret": total_ret,
        "max_dd": max_dd, "max_cl": max_cl,
        "avg_win": avg_win, "avg_loss": avg_loss,
    }


def _print_row(label, m):
    if m["trades"] == 0:
        print(f"{label}: no trades")
        return
    print(f"{label}: trades={m['trades']:2d} WR={m['wr']:5.1f}% PF={m['pf']:5.2f} "
          f"ret={m['ret']:+.2f}% MaxDD={m['max_dd']:5.2f}% MaxCL={m['max_cl']} "
          f"avg_win={m['avg_win']:+.2f}% avg_loss={m['avg_loss']:+.2f}%")


if __name__ == "__main__":
    configs = [
        ("june_btc", "BTCUSD", 0.006),
        ("june_eth", "ETHUSD", 0.007),
    ]
    rr_list = [4, 5, 7, 10]

    for subdir, sym, sl in configs:
        print(f"\n{'='*80}")
        print(f"=== {sym} ({subdir}) SL={sl*100:.2f}% | R:R sweep ===")
        print(f"{'='*80}")
        for rr in rr_list:
            otm_pct = sl * rr  # wing distance matches perp target distance
            perp_rets, spread_rets, skipped = run_overlay(subdir, sym, sl, rr, otm_pct)
            mp = _summarize(perp_rets)
            ms = _summarize(spread_rets)
            print(f"\nR:R 1:{rr} (spread wing {otm_pct*100:.2f}%)")
            _print_row("Perp  ", mp)
            _print_row("Spread", ms)
            if skipped:
                by_reason = {}
                for s in skipped:
                    by_reason[s["reason"]] = by_reason.get(s["reason"], 0) + 1
                print("  skipped:", by_reason)
