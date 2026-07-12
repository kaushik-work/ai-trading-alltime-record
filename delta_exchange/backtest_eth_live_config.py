"""
Backtest the exact live config:
  ETH-only, fixed Rs 50k notional per trade, 15x leverage, vol filter 34%.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path
import numpy as np
import pandas as pd
from backtest_price_action_sweep import load_perp, prepare

SYMBOL = 'ETHUSD'
BUDGET = 50000
FIXED_RISK_PCT = 1.0   # full budget per trade (matches fixed-capital live config)
SL_PCT = 0.007
RR = 7.0
TP_PCT = SL_PCT * RR
LEVERAGE = 15
VOL_FILTER_MAX = 0.34


def load_eth_df():
    dfs = []
    for subdir in ['eth', 'july_eth']:
        try:
            dfs.append(load_perp(subdir, SYMBOL))
        except Exception as e:
            print(f"Warning: {e}")
    df = pd.concat(dfs).sort_index()
    df = df[~df.index.duplicated(keep='first')]
    df = df[df.index >= pd.Timestamp('2026-04-01', tz='UTC')]
    return df


def run_signals(df):
    s = prepare(df, use_trend=True, retest_mode='wick_touch',
                body_pos_threshold=0.70, wick_touch_tol=0.0007,
                vol_filter_max=VOL_FILTER_MAX)
    o, h, l, c = s['o'], s['h'], s['l'], s['c']
    ts = df.index
    n = len(df)
    long_sig, short_sig = s['retest_long'], s['retest_short']

    trades = []
    pos = None
    cooldown = -1
    start_i = max(240, 1440) + 10

    for i in range(start_i, n - 1):
        if pos is not None:
            sign = 1 if pos['side'] == 'long' else -1
            hi, lo = h[i], l[i]
            if (sign > 0 and hi >= pos['tp']) or (sign < 0 and lo <= pos['tp']):
                pnl = sign * (pos['tp'] - pos['entry']) / pos['entry']
                trades.append({**pos, 'pnl': pnl, 'win': pnl > 0})
                pos = None; cooldown = i + 60; continue
            if (sign > 0 and lo <= pos['sl']) or (sign < 0 and hi >= pos['sl']):
                pnl = sign * (pos['sl'] - pos['entry']) / pos['entry']
                trades.append({**pos, 'pnl': pnl, 'win': pnl > 0})
                pos = None; cooldown = i + 60; continue
            if i - pos['entry_idx'] >= 240:
                pnl = sign * (c[i] - pos['entry']) / pos['entry']
                trades.append({**pos, 'pnl': pnl, 'win': pnl > 0})
                pos = None; cooldown = i + 60; continue
            continue
        if i < cooldown: continue
        if long_sig[i]:
            entry = o[i+1]
            pos = {'side':'long', 'entry':entry, 'sl':entry*(1-SL_PCT), 'tp':entry*(1+TP_PCT), 'entry_idx':i+1}
            continue
        if short_sig[i]:
            entry = o[i+1]
            pos = {'side':'short', 'entry':entry, 'sl':entry*(1+SL_PCT), 'tp':entry*(1-TP_PCT), 'entry_idx':i+1}
            continue
    return trades


def run_fixed_capital(trades, budget, leverage):
    capital_per_trade = budget * FIXED_RISK_PCT
    equity = budget
    peak = budget
    gross = 0
    wins = 0
    max_dd = 0
    for t in trades:
        pnl = capital_per_trade * leverage * t['pnl']
        gross += pnl
        equity += pnl
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd: max_dd = dd
        if pnl > 0: wins += 1
    return {'trades':len(trades), 'wins':wins, 'gross':gross, 'max_dd':max_dd}


def main():
    df = load_eth_df()
    trades = run_signals(df)
    res = run_fixed_capital(trades, BUDGET, LEVERAGE)
    print(f"ETH live-config backtest: {df.index[0].date()} to {df.index[-1].date()}")
    print(f"  Trades: {res['trades']}")
    print(f"  Wins: {res['wins']} ({100*res['wins']/res['trades']:.1f}%)")
    print(f"  Gross P&L: Rs {res['gross']:,.0f}")
    print(f"  MaxDD: Rs {res['max_dd']:,.0f} ({100*res['max_dd']/BUDGET:.1f}% of budget)")
    print(f"  Profit/Risk: {res['gross']/max(res['max_dd'],1):.1f}x")


if __name__ == '__main__':
    main()
