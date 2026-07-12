"""
XAUTUSD price-action S/R backtest (Jun 1 - Jul 7 2026).
Tests multiple SL/TP combinations because gold volatility differs from ETH/BTC.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from backtest_price_action_sweep import load_perp, prepare

SYMBOL = 'XAUTUSD'
BUDGET = 50000
FIXED_RISK_PCT = 1.0  # full budget per trade in fixed-capital interpretation
LEVERAGE = 15

# XAUT is less volatile than ETH; test tighter SL/TP combos
CONFIGS = [
    (0.004, 7.0),   # 0.4% SL / 2.8% TP
    (0.005, 7.0),   # 0.5% SL / 3.5% TP
    (0.006, 7.0),   # 0.6% SL / 4.2% TP
    (0.007, 7.0),   # 0.7% SL / 4.9% TP (ETH settings)
    (0.008, 7.0),   # 0.8% SL / 5.6% TP
]

VOL_THRESHOLDS = [0.0, 0.25, 0.30, 0.35]


def run_signals(df, sl_pct, rr, vol_filter_max=0.0):
    s = prepare(df, use_trend=True, retest_mode='wick_touch',
                body_pos_threshold=0.70,
                wick_touch_tol=0.0007,
                vol_filter_max=vol_filter_max)
    o, h, l, c = s['o'], s['h'], s['l'], s['c']
    ts = df.index
    n = len(df)
    long_sig, short_sig = s['retest_long'], s['retest_short']

    trades = []
    pos = None
    cooldown = -1
    start_i = max(240, 1440) + 10
    tp_pct = sl_pct * rr

    for i in range(start_i, n - 1):
        t = ts[i]

        if pos is not None:
            sign = 1 if pos['side'] == 'long' else -1
            hi, lo = h[i], l[i]

            if (sign > 0 and hi >= pos['tp']) or (sign < 0 and lo <= pos['tp']):
                pnl = sign * (pos['tp'] - pos['entry']) / pos['entry']
                trades.append({**pos, 'exit': pos['tp'], 'exit_time': t, 'pnl': pnl, 'win': pnl > 0})
                pos = None
                cooldown = i + 60
                continue

            if (sign > 0 and lo <= pos['sl']) or (sign < 0 and hi >= pos['sl']):
                pnl = sign * (pos['sl'] - pos['entry']) / pos['entry']
                trades.append({**pos, 'exit': pos['sl'], 'exit_time': t, 'pnl': pnl, 'win': pnl > 0})
                pos = None
                cooldown = i + 60
                continue

            if i - pos['entry_idx'] >= 240:
                pnl = sign * (c[i] - pos['entry']) / pos['entry']
                trades.append({**pos, 'exit': c[i], 'exit_time': t, 'pnl': pnl, 'win': pnl > 0})
                pos = None
                cooldown = i + 60
                continue
            continue

        if i < cooldown:
            continue

        if long_sig[i]:
            entry = o[i + 1]
            pos = {
                'side': 'long', 'entry': entry,
                'sl': entry * (1 - sl_pct), 'tp': entry * (1 + tp_pct),
                'entry_idx': i + 1, 'entry_time': ts[i + 1],
            }
            continue

        if short_sig[i]:
            entry = o[i + 1]
            pos = {
                'side': 'short', 'entry': entry,
                'sl': entry * (1 + sl_pct), 'tp': entry * (1 - tp_pct),
                'entry_idx': i + 1, 'entry_time': ts[i + 1],
            }
            continue

    return trades


def run_fixed_capital(trades, budget, leverage):
    capital_per_trade = budget * FIXED_RISK_PCT
    equity = budget
    peak = budget
    gross_pnl = 0.0
    wins = 0
    max_dd = 0.0
    for t in trades:
        pnl = capital_per_trade * leverage * t['pnl']
        gross_pnl += pnl
        equity += pnl
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd: max_dd = dd
        if pnl > 0: wins += 1
    return {
        'trades': len(trades),
        'wins': wins,
        'gross_pnl': gross_pnl,
        'max_dd': max_dd,
    }


def main():
    df = load_perp('xaut', SYMBOL)
    df = df[df.index >= pd.Timestamp('2026-06-01', tz='UTC')]
    print("=" * 110)
    print(f"XAUTUSD PRICE-ACTION S/R BACKTEST — {df.index[0].date()} to {df.index[-1].date()}")
    print(f"Fixed Rs {BUDGET:,} budget, {LEVERAGE}x leverage, 1:7 R:R")
    print("=" * 110)

    print(f"{'SL/TP':>12} {'Vol Cap':>10} {'Trades':>8} {'Wins':>7} {'Win%':>8} "
          f"{'Gross Rs':>14} {'MaxDD Rs':>12} {'DD%':>8} {'Profit/Risk':>12}")
    print("-" * 110)

    for sl_pct, rr in CONFIGS:
        tp_pct = sl_pct * rr
        for vol in VOL_THRESHOLDS:
            trades = run_signals(df, sl_pct, rr, vol_filter_max=vol)
            res = run_fixed_capital(trades, BUDGET, LEVERAGE)
            wr = 100 * res['wins'] / res['trades'] if res['trades'] else 0
            dd_pct = 100 * res['max_dd'] / BUDGET
            pr = res['gross_pnl'] / max(res['max_dd'], 1)
            print(f"{sl_pct:>6.2%}/{tp_pct:>5.2%}  {vol:>9.0%}  {res['trades']:>8} {res['wins']:>7} {wr:>7.1f}% "
                  f"Rs {res['gross_pnl']:>10,.0f}  Rs {res['max_dd']:>9,.0f} {dd_pct:>7.1f}% "
                  f"{pr:>11.1f}x")


if __name__ == '__main__':
    main()
