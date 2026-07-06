"""Leverage-aware backtest that models liquidation on intra-candle gaps.

The standard backtest assumes stops always fill at the stop price.  In live
high-leverage trading, a gap beyond the liquidation price wipes the position
before the stop order executes.  This script models that.

Liquidation price:
  - Long: entry * (1 - 1/LEVERAGE)
  - Short: entry * (1 + 1/LEVERAGE)

If any 1m candle during the trade touches the liquidation price, the trade
closes at that price and the account loses the allocated margin
(CAPITAL_USE_PCT * current equity).  If equity drops to zero, the run ends.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import argparse
import numpy as np
import pandas as pd
from backtest_price_action_sweep import (
    load_perp, prepare, START_USD, CAPITAL_USE_PCT, MAX_HOLD_CANDLES,
    SLIPPAGE_BPS, BREAKEVEN_R,
)


def run_with_liquidation(subdir, sym, sl_pct, rr, leverage, capital_pct=0.50,
                         date_start=None, date_end=None, **prep_kw):
    df = load_perp(subdir, sym)
    if date_start:
        df = df[df.index >= date_start]
    if date_end:
        df = df[df.index < date_end]
    if len(df) < 2440:
        return [], START_USD, np.array([START_USD]), False

    cooldown_candles = prep_kw.pop("cooldown_candles", 60)
    block_after_loss_candles = prep_kw.pop("block_after_loss_candles", 0)
    trail_be = prep_kw.pop("trail_be", True)
    s = prepare(df, **prep_kw)
    o, h, l, c = s["o"], s["h"], s["l"], s["c"]
    ts = df.index
    n = len(df)
    long_sig, short_sig = s["retest_long"], s["retest_short"]

    equity = START_USD
    trades = []
    pos = None
    cooldown = -1
    block_long_until = -1
    block_short_until = -1
    equity_curve = [equity]
    liquidated = False

    start_i = 2440
    for i in range(start_i, n - 1):
        if liquidated:
            break

        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            hi, lo = h[i], l[i]

            # Check liquidation first (gap through stop)
            liq_price = pos["liq_price"]
            if (sign > 0 and lo <= liq_price) or (sign < 0 and hi >= liq_price):
                pnl = sign * (liq_price - pos["entry"]) / pos["entry"]
                trades.append({**pos, "exit": liq_price, "exit_time": ts[i],
                               "pnl": pnl, "reason": "liquidated"})
                equity *= (1 - capital_pct)
                pos = None
                equity_curve.append(equity)
                if equity <= 0.01:
                    liquidated = True
                continue

            if (sign > 0 and hi >= pos["tp"]) or (sign < 0 and lo <= pos["tp"]):
                pnl = sign * (pos["tp"] - pos["entry"]) / pos["entry"]
                trades.append({**pos, "exit": pos["tp"], "exit_time": ts[i],
                               "pnl": pnl, "reason": "tp"})
                equity *= (1 + pnl * leverage * capital_pct)
                pos = None
                cooldown = i + cooldown_candles
                if pnl <= 0:
                    block = block_after_loss_candles
                    if sign > 0:
                        block_long_until = i + block
                    else:
                        block_short_until = i + block
                equity_curve.append(equity)
                continue

            stop = pos["be_sl"] if pos.get("trail_be", False) else pos["sl"]
            if (sign > 0 and lo <= stop) or (sign < 0 and hi >= stop):
                pnl = sign * (stop - pos["entry"]) / pos["entry"]
                trades.append({**pos, "exit": stop, "exit_time": ts[i],
                               "pnl": pnl, "reason": "sl"})
                equity *= (1 + pnl * leverage * capital_pct)
                pos = None
                cooldown = i + cooldown_candles
                if pnl <= 0:
                    block = block_after_loss_candles
                    if sign > 0:
                        block_long_until = i + block
                    else:
                        block_short_until = i + block
                equity_curve.append(equity)
                continue

            if i - pos["entry_idx"] >= MAX_HOLD_CANDLES:
                pnl = sign * (c[i] - pos["entry"]) / pos["entry"]
                trades.append({**pos, "exit": c[i], "exit_time": ts[i],
                               "pnl": pnl, "reason": "hold"})
                equity *= (1 + pnl * leverage * capital_pct)
                pos = None
                cooldown = i + cooldown_candles
                if pnl <= 0:
                    block = block_after_loss_candles
                    if sign > 0:
                        block_long_until = i + block
                    else:
                        block_short_until = i + block
                equity_curve.append(equity)
                continue

            if trail_be and not pos.get("trail_be", False):
                be_price = pos["entry"] * (1 + sign * pos["sl_pct"] * BREAKEVEN_R)
                if (sign > 0 and c[i] >= be_price) or (sign < 0 and c[i] <= be_price):
                    pos["trail_be"] = True
                    pos["be_sl"] = pos["entry"]
            continue

        if i < cooldown:
            continue

        tp_pct = sl_pct * rr

        if long_sig[i] and i >= block_long_until:
            entry = o[i + 1] * (1 + SLIPPAGE_BPS / 10_000)
            stop_level = l[i] * (1 - SLIPPAGE_BPS / 10_000)
            sl_dist = max(sl_pct, (entry - stop_level) / entry)
            sl = entry * (1 - sl_dist)
            tp = entry * (1 + tp_pct)
            liq = entry * (1 - 1 / leverage)
            pos = {
                "side": "long", "entry": entry, "sl": sl, "tp": tp,
                "liq_price": liq, "entry_idx": i + 1, "entry_time": ts[i + 1],
                "rr": rr, "sl_pct": sl_dist, "tp_pct": tp_pct,
                "trail_be": False, "be_sl": None,
            }
            continue

        if short_sig[i] and i >= block_short_until:
            entry = o[i + 1] * (1 - SLIPPAGE_BPS / 10_000)
            stop_level = h[i] * (1 + SLIPPAGE_BPS / 10_000)
            sl_dist = max(sl_pct, (stop_level - entry) / entry)
            sl = entry * (1 + sl_dist)
            tp = entry * (1 - tp_pct)
            liq = entry * (1 + 1 / leverage)
            pos = {
                "side": "short", "entry": entry, "sl": sl, "tp": tp,
                "liq_price": liq, "entry_idx": i + 1, "entry_time": ts[i + 1],
                "rr": rr, "sl_pct": sl_dist, "tp_pct": tp_pct,
                "trail_be": False, "be_sl": None,
            }
            continue

    if pos and not liquidated:
        sign = 1 if pos["side"] == "long" else -1
        # final liquidation check
        liq = pos["liq_price"]
        if (sign > 0 and l[-1] <= liq) or (sign < 0 and h[-1] >= liq):
            pnl = sign * (liq - pos["entry"]) / pos["entry"]
            trades.append({**pos, "exit": liq, "exit_time": ts[-1],
                           "pnl": pnl, "reason": "liquidated"})
            equity *= (1 - capital_pct)
        else:
            pnl = sign * (c[-1] - pos["entry"]) / pos["entry"]
            trades.append({**pos, "exit": c[-1], "exit_time": ts[-1],
                           "pnl": pnl, "reason": "eof"})
            equity *= (1 + pnl * leverage * capital_pct)
        equity_curve.append(equity)

    return trades, equity, np.array(equity_curve), liquidated


def metrics(trades, equity, equity_curve):
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp, gl = sum(t["pnl"] for t in wins), abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else float("inf")
    wr = len(wins) / len(trades) * 100
    peak = np.maximum.accumulate(equity_curve)
    dd = (peak - equity_curve) / peak
    max_dd = float(np.max(dd)) if len(equity_curve) else 0.0
    liqs = sum(1 for t in trades if t["reason"] == "liquidated")
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "wr": wr,
        "pf": pf,
        "pnl": equity - START_USD,
        "ret_pct": (equity / START_USD - 1) * 100,
        "max_dd_pct": max_dd * 100,
        "liquidations": liqs,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--btc-subdir", default=".")
    parser.add_argument("--eth-subdir", default="eth")
    args = parser.parse_args()

    date_start = pd.Timestamp("2026-04-01", tz="UTC")
    date_end = pd.Timestamp("2026-06-21", tz="UTC")

    prep_kw = dict(
        use_trend=True,
        retest_mode="wick_touch",
        body_pos_threshold=0.70,
        wick_touch_tol=0.0007,
        min_volume_mult=1.0,
        rsi_period=14, rsi_long_max=100, rsi_short_min=0,
        trend_slope_candles=0, trend_slope_min_pct=0.0,
        range_pct_min=0.0,
        trading_hours="all",
        htf_align=False,
        require_engulfing=False,
        pin_bar_wick_ratio=0.0,
        cooldown_candles=60,
        block_after_loss_candles=180,
        trail_be=True,
    )

    configs = [
        (args.btc_subdir, "BTCUSD", 0.006, 7),
        (args.eth_subdir, "ETHUSD", 0.007, 7),
    ]

    leverages = [3, 5, 10, 15, 20, 30, 40, 50, 75, 100, 150, 200]
    capital_pct = 0.50
    months = 2.67

    print("=" * 130)
    print("Leverage-aware backtest WITH liquidation modeling")
    print(f"Capital_use_pct = {capital_pct}, stop-losses are checked against 1m high/low")
    print("If a candle touches the liquidation price, position is closed and equity loses the allocated margin.")
    print("=" * 130)

    for subdir, sym, sl, rr in configs:
        print(f"\n--- {sym} SL={sl*100:.2f}% RR=1:{rr} ---")
        print(f"{'Lev':>5} {'Ret%':>9} {'Ret/mo':>9} {'MaxDD%':>9} {'Trades':>8} {'WR':>7} {'Liquidations':>13} {'Wiped?':>7}")
        for lev in leverages:
            trades, equity, curve, wiped = run_with_liquidation(
                subdir, sym, sl, rr, lev, capital_pct=capital_pct,
                date_start=date_start, date_end=date_end, **prep_kw
            )
            m = metrics(trades, equity, curve)
            if m["trades"] == 0:
                continue
            ret_mo = m["ret_pct"] / months if equity > 0.01 else -100.0
            print(f"{lev:>5}x {m['ret_pct']:>+8.2f}% {ret_mo:>+8.2f}% {m['max_dd_pct']:>8.2f}% "
                  f"{m['trades']:>8d} {m['wr']:>6.1f}% {m['liquidations']:>13d} {'YES' if wiped else 'no':>7}")


if __name__ == "__main__":
    main()
