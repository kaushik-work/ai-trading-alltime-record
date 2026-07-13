"""
Market-making grid prototype — ETHUSD 1m.

Places a symmetric ladder of buy/sell orders around an EMA reference. When price
touches a buy grid level, open a long; when it touches a sell grid level, open a
short. Close at the next grid level in profit. Inventory limit prevents
runaway directional exposure.

Uses 1m candles as a proxy for tick/order-book fills.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
from backtest_price_action_sweep import load_perp

BUDGET_INR = 50_000.0
LEVERAGE = 15.0
FEE_BPS = 5.0
SLIP_BPS = 2.0
SYMBOL = "ETHUSD"

EMA_BARS = 120
GRID_PCT = 0.005       # 0.5% spacing
MAX_POSITIONS = 1      # one leg per side
INVENTORY_STOP_PCT = 0.010
MAX_HOLD_MIN = 120


def load_eth():
    dfs = [load_perp(s, SYMBOL) for s in ["eth", "july_eth"]]
    df = pd.concat(dfs).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[df.index >= pd.Timestamp("2026-04-01", tz="UTC")]
    return df


def run():
    df = load_eth()
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    ts = df.index
    n = len(df)

    ema = pd.Series(c).ewm(span=EMA_BARS, min_periods=EMA_BARS).mean().values

    equity = BUDGET_INR
    peak = equity
    trades = []
    positions = []  # list of open legs

    for i in range(EMA_BARS, n - 1):
        ref = ema[i]
        if np.isnan(ref) or ref <= 0:
            continue

        # Determine current grid levels
        buy_level = ref * (1 - GRID_PCT)
        sell_level = ref * (1 + GRID_PCT)

        # Manage open legs
        still_open = []
        for pos in positions:
            sign = 1 if pos["side"] == "long" else -1
            reason = None
            exit_px = None
            target = pos["entry"] * (1 + sign * GRID_PCT)
            stop = pos["entry"] * (1 - sign * INVENTORY_STOP_PCT)

            if sign > 0:
                if h[i] >= target:
                    exit_px = min(h[i], target)
                    reason = "grid_tp"
                elif l[i] <= stop:
                    exit_px = max(l[i], stop)
                    reason = "inventory_stop"
            else:
                if l[i] <= target:
                    exit_px = max(l[i], target)
                    reason = "grid_tp"
                elif h[i] >= stop:
                    exit_px = min(h[i], stop)
                    reason = "inventory_stop"

            if reason is None and i - pos["entry_idx"] >= MAX_HOLD_MIN:
                exit_px = c[i]
                reason = "max_hold"

            if reason is not None:
                fill = exit_px * (1 - sign * SLIP_BPS / 1e4)
                gross = sign * (fill - pos["entry"]) / pos["entry"]
                net = gross - 2 * FEE_BPS / 1e4
                pnl = BUDGET_INR * LEVERAGE * net
                equity += pnl
                peak = max(peak, equity)
                trades.append({**pos, "exit": fill, "reason": reason, "pnl": pnl,
                               "exit_time": ts[i]})
            else:
                still_open.append(pos)
        positions = still_open

        # Enter new legs if grid touched and inventory allows
        long_count = sum(1 for p in positions if p["side"] == "long")
        short_count = sum(1 for p in positions if p["side"] == "short")

        if l[i] <= buy_level and long_count < MAX_POSITIONS:
            entry = buy_level * 1.0002
            positions.append({
                "side": "long", "entry": entry,
                "entry_idx": i, "entry_time": ts[i],
            })
        if h[i] >= sell_level and short_count < MAX_POSITIONS:
            entry = sell_level * 0.9998
            positions.append({
                "side": "short", "entry": entry,
                "entry_idx": i, "entry_time": ts[i],
            })

    # Close remaining at last close
    for pos in positions:
        sign = 1 if pos["side"] == "long" else -1
        fill = c[-1] * (1 - sign * SLIP_BPS / 1e4)
        gross = sign * (fill - pos["entry"]) / pos["entry"]
        net = gross - 2 * FEE_BPS / 1e4
        pnl = BUDGET_INR * LEVERAGE * net
        equity += pnl
        trades.append({**pos, "exit": fill, "reason": "eof", "pnl": pnl,
                       "exit_time": ts[-1]})

    max_dd = peak - equity
    wins = sum(1 for t in trades if t["pnl"] > 0)

    print("=" * 80)
    print("Market-making grid prototype")
    print(f"EMA {EMA_BARS}m reference | grid spacing {GRID_PCT*100:.2f}% | max {MAX_POSITIONS} legs/side")
    print(f"Inventory stop {INVENTORY_STOP_PCT*100:.1f}% | max hold {MAX_HOLD_MIN}m")
    print(f"Budget ₹{BUDGET_INR:,.0f} | {LEVERAGE:.0f}x | {FEE_BPS}bps fee + {SLIP_BPS}bps slip")
    print("=" * 80)
    print(f"  Trades: {len(trades)}")
    if trades:
        print(f"  Wins: {wins} ({100*wins/len(trades):.1f}%)")
    print(f"  Gross P&L: ₹{equity - BUDGET_INR:+.0f}")
    print(f"  Return: {100*(equity-BUDGET_INR)/BUDGET_INR:+.1f}%")
    print(f"  MaxDD: ₹{max_dd:,.0f} ({100*max_dd/BUDGET_INR:.1f}%)")

    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    if reasons:
        print(f"  Exit reasons: {reasons}")


if __name__ == "__main__":
    run()
