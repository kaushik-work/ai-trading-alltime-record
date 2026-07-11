"""Test drawdown-control filters on ETH price-action strategy."""
import sys
sys.path.insert(0, '.')
import pandas as pd
import numpy as np
from backtest_price_action_sweep import load_perp, prepare, LOOKBACK, TREND_LOOKBACK, MAX_HOLD_CANDLES, COOLDOWN_CANDLES, SLIPPAGE_BPS

START_CAPITAL_INR = 50_000.0
SL_PCT = 0.007
RR = 7.0
COOLDOWN = 60
BLOCK_AFTER_LOSS = 180

def load_data():
    df_eth = load_perp('eth', 'ETHUSD')
    df_july = load_perp('july_eth', 'ETHUSD')
    start = pd.Timestamp('2026-04-01', tz='UTC')
    cut = pd.Timestamp('2026-06-21', tz='UTC')
    df = pd.concat([df_eth[df_eth.index >= start], df_july[df_july.index >= cut]])
    df = df[~df.index.duplicated(keep='first')].sort_index()
    return df

def compute_market_features(df):
    r = df['close'].pct_change()
    df = df.copy()
    df['vol_24h'] = r.rolling(24*60).std() * np.sqrt(365*24*60)
    df['vol_7d'] = r.rolling(7*24*60).std() * np.sqrt(365*24*60)
    df['atr_24h'] = ((df['high'] - df['low']).rolling(24*60).mean()) / df['close']
    df['trend_24h'] = (df['close'] - df['close'].shift(24*60)) / df['close'].shift(24*60)
    df['trend_7d'] = (df['close'] - df['close'].shift(7*24*60)) / df['close'].shift(7*24*60)
    df['range_4h'] = (df['high'].rolling(4*60).max() - df['low'].rolling(4*60).min()) / df['close']
    return df

def run_strategy(df, eff_lev=15.0, vol_filter=None, trend_filter=None, atr_filter=None,
                 consecutive_loss_filter=None, cppi_floor=None, cppi_mult=None):
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
    consecutive_losses = 0
    capital = START_CAPITAL_INR
    peak = capital
    max_dd = 0.0
    floor = cppi_floor * START_CAPITAL_INR if cppi_floor else 0.0

    start_i = max(LOOKBACK, TREND_LOOKBACK) + 10
    for i in range(start_i, n - 1):
        t = ts[i]
        ci = c[i]

        # CPPI dynamic sizing
        if cppi_floor is not None:
            cushion = max(0, capital - floor)
            risk_capital = cppi_mult * cushion
            dynamic_eff_lev = min(eff_lev, risk_capital / START_CAPITAL_INR)
            if dynamic_eff_lev <= 0:
                continue
        else:
            dynamic_eff_lev = eff_lev

        # Filters
        if vol_filter and not pd.isna(df['vol_24h'].iloc[i]) and df['vol_24h'].iloc[i] > vol_filter:
            continue
        if atr_filter and not pd.isna(df['atr_24h'].iloc[i]) and df['atr_24h'].iloc[i] > atr_filter:
            continue
        if trend_filter is not None and not pd.isna(df['trend_7d'].iloc[i]):
            # Require trend alignment: skip if no clear trend
            if abs(df['trend_7d'].iloc[i]) < trend_filter:
                continue
        if consecutive_loss_filter and consecutive_losses >= consecutive_loss_filter:
            continue

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
                pnl_inr = pnl * capital * dynamic_eff_lev
                capital += pnl_inr
                peak = max(peak, capital)
                max_dd = max(max_dd, (peak - capital) / peak)
                if pnl <= 0:
                    consecutive_losses += 1
                    block_long_until = i + BLOCK_AFTER_LOSS if sign > 0 else block_long_until
                    block_short_until = i + BLOCK_AFTER_LOSS if sign < 0 else block_short_until
                else:
                    consecutive_losses = 0
                trades.append({'pnl_pct': pnl, 'entry_time': pos['entry_time'], 'exit_time': t, 'side': pos['side']})
                pos = None
                cooldown = i + COOLDOWN
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
            pos = {'side': 'long', 'entry': entry, 'sl': sl, 'tp': tp,
                   'entry_idx': i + 1, 'entry_time': t_entry}
            continue

        if short_sig[i] and i >= block_short_until:
            entry = next_open * (1 - SLIPPAGE_BPS / 10_000)
            stop_level = h[i] * (1 + SLIPPAGE_BPS / 10_000)
            sl_dist = max(SL_PCT, (stop_level - entry) / entry)
            sl = entry * (1 + sl_dist)
            tp = entry * (1 - tp_pct)
            pos = {'side': 'short', 'entry': entry, 'sl': sl, 'tp': tp,
                   'entry_idx': i + 1, 'entry_time': t_entry}
            continue

    if pos:
        sign = 1 if pos['side'] == 'long' else -1
        pnl = sign * (c[-1] - pos['entry']) / pos['entry']
        pnl_inr = pnl * capital * dynamic_eff_lev
        capital += pnl_inr
        trades.append({'pnl_pct': pnl, 'entry_time': pos['entry_time'], 'exit_time': ts[-1], 'side': pos['side']})

    return capital, max_dd, trades


def weekly_stats(trades):
    if not trades:
        return pd.Series(dtype=float), 0
    df = pd.DataFrame(trades)
    df['year_week'] = df['exit_time'].dt.isocalendar().year.astype(str) + '-W' + df['exit_time'].dt.isocalendar().week.astype(str).str.zfill(2)
    return df.groupby('year_week')['pnl_pct'].sum(), len(df)


df = compute_market_features(load_data())

print('=== Baseline: 15x effective exposure, no filters ===')
cap, dd, trades = run_strategy(df, eff_lev=15.0)
weekly, n = weekly_stats(trades)
print(f'Trades: {n} | Final: Rs{cap:,.0f} | Return: {(cap/START_CAPITAL_INR-1)*100:.1f}% | MaxDD: {dd*100:.1f}% | Worst week: {weekly.min()*100:.1f}%')

print('\n=== Volatility filter sweep (skip if 24h vol > X) at 15x ===')
for v in [0.30, 0.40, 0.50, 0.60, 0.70, 0.80]:
    cap, dd, trades = run_strategy(df, eff_lev=15.0, vol_filter=v)
    weekly, n = weekly_stats(trades)
    print(f'vol < {v*100:.0f}% | Trades: {n:3d} | Final: Rs{cap:>10,.0f} | Return: {(cap/START_CAPITAL_INR-1)*100:>+6.1f}% | MaxDD: {dd*100:>5.1f}% | Worst week: {weekly.min()*100:>6.1f}%')

print('\n=== ATR filter sweep at 15x ===')
for a in [0.0005, 0.0010, 0.0015, 0.0020]:
    cap, dd, trades = run_strategy(df, eff_lev=15.0, atr_filter=a)
    weekly, n = weekly_stats(trades)
    print(f'atr < {a*100:.2f}% | Trades: {n:3d} | Final: Rs{cap:>10,.0f} | Return: {(cap/START_CAPITAL_INR-1)*100:>+6.1f}% | MaxDD: {dd*100:>5.1f}% | Worst week: {weekly.min()*100:>6.1f}%')

print('\n=== Consecutive loss pause (pause after N losses) at 15x ===')
for nloss in [2, 3, 4, 5]:
    cap, dd, trades = run_strategy(df, eff_lev=15.0, consecutive_loss_filter=nloss)
    weekly, n = weekly_stats(trades)
    print(f'pause after {nloss} losses | Trades: {n:3d} | Final: Rs{cap:>10,.0f} | Return: {(cap/START_CAPITAL_INR-1)*100:>+6.1f}% | MaxDD: {dd*100:>5.1f}% | Worst week: {weekly.min()*100:>6.1f}%')

print('\n=== CPPI-style dynamic sizing (floor=80%, mult sweep) at max 15x ===')
for mult in [1, 2, 3, 5, 10]:
    cap, dd, trades = run_strategy(df, eff_lev=15.0, cppi_floor=0.80, cppi_mult=mult)
    weekly, n = weekly_stats(trades)
    print(f'CPPI m={mult
:2d} | Trades: {n:3d} | Final: Rs{cap:>10,.0f} | Return: {(cap/START_CAPITAL_INR-1)*100:>+6.1f}% | MaxDD: {dd*100:>5.1f}% | Worst week: {weekly.min()*100:>6.1f}%')
