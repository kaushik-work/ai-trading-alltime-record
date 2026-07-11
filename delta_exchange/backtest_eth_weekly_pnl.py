"""ETH weekly P&L simulation starting from 50,000 INR across Apr-Jun-Jul 2026."""
import sys
sys.path.insert(0, '.')
import pandas as pd
import numpy as np
from pathlib import Path
from backtest_price_action_sweep import load_perp, prepare, LOOKBACK, TREND_LOOKBACK, MAX_HOLD_CANDLES, COOLDOWN_CANDLES, SLIPPAGE_BPS
from backtest_june_detailed import next_live_entry_time

# Config
START_CAPITAL_INR = 50_000.0
SL_PCT = 0.007
RR = 7.0
COOLDOWN = 60
BLOCK_AFTER_LOSS = 180

# Load and stitch data
print('Loading ETH data...')
df_eth = load_perp('eth', 'ETHUSD')
df_july = load_perp('july_eth', 'ETHUSD')
# Use eth from Apr 1 to June 20, then july_eth from June 21 onwards
start = pd.Timestamp('2026-04-01', tz='UTC')
cut = pd.Timestamp('2026-06-21', tz='UTC')
df = pd.concat([df_eth[df_eth.index >= start], df_july[df_july.index >= cut]])
df = df[~df.index.duplicated(keep='first')].sort_index()
print(f'Combined range: {df.index.min()} to {df.index.max()} ({len(df)} rows)')

s = prepare(df, use_trend=True, retest_mode='wick_touch',
            body_pos_threshold=0.70, wick_touch_tol=0.0007)
o, h, l, c = s['o'], s['h'], s['l'], s['c']
ts = df.index
n = len(df)
long_sig, short_sig = s['retest_long'], s['retest_short']

tp_pct = SL_PCT * RR
trades = []
pos = None
cooldown = -1
block_long_until = -1
block_short_until = -1

start_i = max(LOOKBACK, TREND_LOOKBACK) + 10
for i in range(start_i, n - 1):
    t = ts[i]
    ci = c[i]

    if pos is not None:
        sign = 1 if pos['side'] == 'long' else -1
        hi, lo = h[i], l[i]
        reason = None
        exit_px = None

        if (sign > 0 and hi >= pos['tp']) or (sign < 0 and lo <= pos['tp']):
            reason = 'tp'; exit_px = pos['tp']
        else:
            stop = pos['sl']
            if (sign > 0 and lo <= stop) or (sign < 0 and hi >= stop):
                reason = 'sl'; exit_px = stop
            elif i - pos['entry_idx'] >= MAX_HOLD_CANDLES:
                reason = 'hold'; exit_px = ci

        if reason:
            pnl = sign * (exit_px - pos['entry']) / pos['entry']
            trades.append({
                'signal_time': pos['signal_time'],
                'entry_time': pos['entry_time'],
                'exit_time': t,
                'side': pos['side'],
                'entry': pos['entry'],
                'exit': exit_px,
                'pnl_pct': pnl,
                'exit_reason': reason,
                'sl_pct': pos['sl_pct'],
                'tp_pct': pos['tp_pct'],
            })
            pos = None
            cooldown = i + COOLDOWN
            if pnl <= 0:
                block_long_until = i + BLOCK_AFTER_LOSS if sign > 0 else block_long_until
                block_short_until = i + BLOCK_AFTER_LOSS if sign < 0 else block_short_until
        continue

    if i < cooldown:
        continue

    next_open = o[i + 1]
    t_entry = ts[i + 1]

    if long_sig[i] and i >= block_long_until:
        entry = next_open * (1 + SLIPPAGE_BPS / 10_000)
        stop_level = l[i] * (1 - SLIPPAGE_BPS / 10_000)
        sl_dist = max(SL_PCT, (entry - stop_level) / entry)
        sl = entry * (1 - sl_dist)
        tp = entry * (1 + tp_pct)
        pos = {
            'side': 'long', 'entry': entry, 'sl': sl, 'tp': tp,
            'entry_idx': i + 1, 'entry_time': t_entry,
            'signal_time': t, 'sl_pct': sl_dist, 'tp_pct': tp_pct,
        }
        continue

    if short_sig[i] and i >= block_short_until:
        entry = next_open * (1 - SLIPPAGE_BPS / 10_000)
        stop_level = h[i] * (1 + SLIPPAGE_BPS / 10_000)
        sl_dist = max(SL_PCT, (stop_level - entry) / entry)
        sl = entry * (1 + sl_dist)
        tp = entry * (1 - tp_pct)
        pos = {
            'side': 'short', 'entry': entry, 'sl': sl, 'tp': tp,
            'entry_idx': i + 1, 'entry_time': t_entry,
            'signal_time': t, 'sl_pct': sl_dist, 'tp_pct': tp_pct,
        }
        continue

# Open position at end
if pos:
    sign = 1 if pos['side'] == 'long' else -1
    pnl = sign * (c[-1] - pos['entry']) / pos['entry']
    trades.append({
        'signal_time': pos['signal_time'],
        'entry_time': pos['entry_time'],
        'exit_time': ts[-1],
        'side': pos['side'],
        'entry': pos['entry'],
        'exit': c[-1],
        'pnl_pct': pnl,
        'exit_reason': 'eof',
        'sl_pct': pos['sl_pct'],
        'tp_pct': pos['tp_pct'],
    })

trades_df = pd.DataFrame(trades)
print(f'\nTotal trades: {len(trades_df)}')
print(f'Win rate: {(trades_df["pnl_pct"] > 0).sum()}/{len(trades_df)} = {(trades_df["pnl_pct"] > 0).mean():.1%}')

# Weekly P&L for different leverage scenarios
scenarios = [
    ('Conservative 1×', 1.0),       # 10x lev, 10% capital
    ('Moderate 5×', 5.0),           # e.g. 10x lev, 50% capital
    ('Live 15×', 15.0),             # 30x lev, 50% capital
]

# Group trades by ISO calendar week
trades_df['week'] = trades_df['exit_time'].dt.isocalendar().week
trades_df['year'] = trades_df['exit_time'].dt.isocalendar().year
trades_df['year_week'] = trades_df['year'].astype(str) + '-W' + trades_df['week'].astype(str).str.zfill(2)

print('\n=== Week-by-week P&L ===')
for name, eff_lev in scenarios:
    capital = START_CAPITAL_INR
    print(f'\n{name} exposure (effective leverage {eff_lev:.0f}×)')
    print(f'Starting capital: ₹{START_CAPITAL_INR:,.0f}')
    print('')
    print(f'{"Week":<10} {"Trades":>8} {"Wins":>6} {"Gross ₹":>12} {"Net ₹":>12} {"Return":>10} {"Capital":>14}')
    print('-' * 80)
    peak = capital
    max_dd = 0.0
    for yw, g in trades_df.groupby('year_week', sort=True):
        gross = (g['pnl_pct'] * capital * eff_lev).sum()
        net = gross
        ret = net / capital
        capital += net
        peak = max(peak, capital)
        dd = (peak - capital) / peak
        max_dd = max(max_dd, dd)
        print(f'{yw:<10} {len(g):>8} {(g["pnl_pct"] > 0).sum():>6} {gross:>+12,.0f} {net:>+12,.0f} {ret:>+9.1%} ₹{capital:>13,.0f}')
    print('-' * 80)
    total_ret = (capital - START_CAPITAL_INR) / START_CAPITAL_INR
    print(f'Total: {len(trades_df)} trades | Final capital: ₹{capital:,.0f} | Return: {total_ret:+.1%} | MaxDD: {max_dd:.1%}')
