"""
ETH vol regime analyzer.
Tests:
  1. Higher vol thresholds (filter OUT when vol > X%).
  2. High-vol-only regime (filter IN only when vol >= X%).
  3. Reduced leverage on high-vol signals.
  4. Direction inversion on high-vol signals (counter-trend perp fade).

Uses fixed Rs 50k budget, no compounding, Apr 1 - Jul 7 2026.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path
import numpy as np
import pandas as pd
from backtest_price_action_sweep import load_perp, prepare

SYMBOL = 'ETHUSD'
BUDGET = 50000
FIXED_RISK_PCT = 0.50
SL_PCT = 0.007
RR = 7.0
TP_PCT = SL_PCT * RR
LEVERAGE_DEFAULT = 15
VOL_THRESHOLDS = [0.33, 0.40, 0.50, 0.60, 0.75, 1.00]
HIGH_VOL_LEVERAGES = [3, 5, 10, 15]
DATA_SUBDIRS = ['eth', 'july_eth']


def load_eth_df():
    dfs = []
    for subdir in DATA_SUBDIRS:
        try:
            df = load_perp(subdir, SYMBOL)
            dfs.append(df)
        except Exception as e:
            print(f"Warning: could not load {subdir}: {e}")
    if not dfs:
        raise RuntimeError("No ETH data loaded")
    df = pd.concat(dfs).sort_index()
    df = df[~df.index.duplicated(keep='first')]
    return df


def run_signals(df, vol_filter_max=0.0):
    s = prepare(df, use_trend=True, retest_mode='wick_touch',
                body_pos_threshold=0.70,
                wick_touch_tol=0.0007,
                vol_filter_max=vol_filter_max)
    o, h, l, c = s['o'], s['h'], s['l'], s['c']
    ts = df.index
    n = len(df)
    long_sig, short_sig = s['retest_long'], s['retest_short']

    # compute vol_24h series
    returns = pd.Series(c).pct_change()
    vol_24h = returns.rolling(24 * 60, min_periods=24 * 60).std() * np.sqrt(365 * 24 * 60)
    vol_arr = vol_24h.values

    trades = []
    pos = None
    cooldown = -1
    start_i = max(240, 1440) + 10

    for i in range(start_i, n - 1):
        t = ts[i]
        ci = c[i]

        if pos is not None:
            sign = 1 if pos['side'] == 'long' else -1
            hi, lo = h[i], l[i]

            if (sign > 0 and hi >= pos['tp']) or (sign < 0 and lo <= pos['tp']):
                pnl = sign * (pos['tp'] - pos['entry']) / pos['entry']
                trades.append({**pos, 'exit': pos['tp'], 'exit_time': t, 'pnl': pnl,
                               'win': pnl > 0, 'reason': 'tp'})
                pos = None
                cooldown = i + 60
                continue

            if (sign > 0 and lo <= pos['sl']) or (sign < 0 and hi >= pos['sl']):
                pnl = sign * (pos['sl'] - pos['entry']) / pos['entry']
                trades.append({**pos, 'exit': pos['sl'], 'exit_time': t, 'pnl': pnl,
                               'win': pnl > 0, 'reason': 'sl'})
                pos = None
                cooldown = i + 60
                continue

            if i - pos['entry_idx'] >= 240:
                pnl = sign * (ci - pos['entry']) / pos['entry']
                trades.append({**pos, 'exit': ci, 'exit_time': t, 'pnl': pnl,
                               'win': pnl > 0, 'reason': 'hold'})
                pos = None
                cooldown = i + 60
                continue
            continue

        if i < cooldown:
            continue

        if long_sig[i]:
            entry = o[i + 1]
            sl = entry * (1 - SL_PCT)
            tp = entry * (1 + TP_PCT)
            pos = {
                'side': 'long', 'entry': entry, 'sl': sl, 'tp': tp,
                'entry_idx': i + 1, 'entry_time': ts[i + 1],
                'vol_24h': float(vol_arr[i]) if not np.isnan(vol_arr[i]) else 0.0,
                'week': ts[i].isocalendar().week,
            }
            continue

        if short_sig[i]:
            entry = o[i + 1]
            sl = entry * (1 + SL_PCT)
            tp = entry * (1 - TP_PCT)
            pos = {
                'side': 'short', 'entry': entry, 'sl': sl, 'tp': tp,
                'entry_idx': i + 1, 'entry_time': ts[i + 1],
                'vol_24h': float(vol_arr[i]) if not np.isnan(vol_arr[i]) else 0.0,
                'week': ts[i].isocalendar().week,
            }
            continue

    return trades


def run_fixed_capital(trades, budget, leverage):
    capital_per_trade = budget * FIXED_RISK_PCT
    equity = budget
    peak = budget
    gross_pnl = 0.0
    wins = 0
    weekly_pnl = {}
    for t in trades:
        pnl = capital_per_trade * leverage * t['pnl']
        gross_pnl += pnl
        equity += pnl
        peak = max(peak, equity)
        if pnl > 0:
            wins += 1
        weekly_pnl[t['week']] = weekly_pnl.get(t['week'], 0.0) + pnl
    max_dd = peak - min(equity, peak)
    return {
        'trades': len(trades),
        'wins': wins,
        'gross_pnl': gross_pnl,
        'max_dd': max_dd,
        'weekly_pnl': weekly_pnl,
    }


def invert_direction(trades):
    inv = []
    for t in trades:
        new = dict(t)
        new['side'] = 'short' if t['side'] == 'long' else 'long'
        new['pnl'] = -t['pnl']
        new['win'] = new['pnl'] > 0
        inv.append(new)
    return inv


def print_result(label, res):
    t = res['trades']
    wr = 100 * res['wins'] / t if t else 0
    dd_pct = 100 * res['max_dd'] / BUDGET
    print(f"{label:>22}  {t:>6} {res['wins']:>6} {wr:>7.1f}% "
          f"Rs {res['gross_pnl']:>11,.0f}  Rs {res['max_dd']:>10,.0f}  {dd_pct:>6.1f}%")


def main():
    df = load_eth_df()
    print("=" * 100)
    print(f"ETH VOL REGIME ANALYSIS — Fixed Rs {BUDGET:,} budget, no compounding, "
          f"{df.index[0].date()} to {df.index[-1].date()}")
    print("=" * 100)

    # Baseline: no vol filter
    baseline_trades = run_signals(df, vol_filter_max=0.0)
    baseline_res = run_fixed_capital(baseline_trades, BUDGET, LEVERAGE_DEFAULT)
    print("\n--- BASELINE (no vol filter, 15x) ---")
    print(f"{'label':>22}  {'Trades':>6} {'Wins':>6} {'Win%':>7} {'Gross':>14} {'MaxDD':>13} {'DD%':>6}")
    print_result("baseline", baseline_res)

    # 1. Higher vol ceilings (filter OUT high vol)
    print("\n--- FILTER OUT: trade only when 24h vol <= X ---")
    print(f"{'Vol Cap':>22}  {'Trades':>6} {'Wins':>6} {'Win%':>7} {'Gross':>14} {'MaxDD':>13} {'DD%':>6}")
    for vol in VOL_THRESHOLDS:
        trades = run_signals(df, vol_filter_max=vol)
        res = run_fixed_capital(trades, BUDGET, LEVERAGE_DEFAULT)
        print_result(f"vol <= {vol:.0%}", res)

    # 2. High-vol-only: trade only when vol >= X
    print("\n--- FILTER IN: trade only when 24h vol >= X (15x) ---")
    print(f"{'Vol Floor':>22}  {'Trades':>6} {'Wins':>6} {'Win%':>7} {'Gross':>14} {'MaxDD':>13} {'DD%':>6}")
    all_trades = run_signals(df, vol_filter_max=0.0)
    for vol in VOL_THRESHOLDS:
        trades = [t for t in all_trades if t['vol_24h'] >= vol]
        res = run_fixed_capital(trades, BUDGET, LEVERAGE_DEFAULT)
        print_result(f"vol >= {vol:.0%}", res)

    # 3. High-vol-only with reduced leverage
    print("\n--- HIGH VOL >= 33% WITH LOWER LEVERAGE ---")
    print(f"{'Leverage':>22}  {'Trades':>6} {'Wins':>6} {'Win%':>7} {'Gross':>14} {'MaxDD':>13} {'DD%':>6}")
    high_vol_trades = [t for t in all_trades if t['vol_24h'] >= 0.33]
    for lev in HIGH_VOL_LEVERAGES:
        res = run_fixed_capital(high_vol_trades, BUDGET, lev)
        print_result(f"{lev}x vol>=33%", res)

    # 4. Invert high-vol signals (counter-trend)
    print("\n--- INVERT DIRECTION on high vol >= 33% (counter-trend fade) ---")
    inv_trades = invert_direction(high_vol_trades)
    res = run_fixed_capital(inv_trades, BUDGET, LEVERAGE_DEFAULT)
    print_result("inverted", res)

    print("\n" + "=" * 100)
    print("Interpretation:")
    print("  - If vol <= X filtering keeps improving as X rises, ETH hates low-vol chop.")
    print("  - If vol >= X filtering loses money, high-vol momentum is NOT our friend.")
    print("  - If inverted high-vol wins, option-selling / mean-reversion may have edge.")
    print("=" * 100)


if __name__ == '__main__':
    main()
