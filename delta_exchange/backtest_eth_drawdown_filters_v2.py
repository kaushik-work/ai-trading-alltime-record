"""Test drawdown-control filters on ETH price-action strategy with proper compounding."""
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

def compute_features(df):
    r = df['close'].pct_change()
    df = df.copy()
    df['vol_24h'] = r.rolling(24*60).std() * np.sqrt(365*24*60)
    df['vol_7d'] = r.rolling(7*24*60).std() * np.sqrt(365*24*60)
    df['atr_24h'] = ((df['high'] - df['low']).rolling(24*60).mean()) / df['close']
    df['trend_24h'] = (df['close'] - df['close'].shift(24*60)) / df['close'].shift(24*60)
    df['trend_7d'] = (df['close'] - df['close'].shift(7*24*60)) / df['close'].shift(7*24*60)
    return df

def run_strategy(df, eff_lev=15.0, vol_filter=None, atr_filter=None,
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

        if cppi_floor is not None:
            cushion = max(0, capital - floor)
            risk_capital = cppi_mult * cushion
            dynamic_eff_lev = min(eff_lev, risk_capital / START_CAPITAL_INR)
            if dynamic_eff_lev <= 0:
                continue
        else:
            dynamic_eff_lev = eff_lev

        if vol_filter and not pd.isna(df['vol_24h'].iloc[i]) and df['vol_24h'].iloc[i] > vol_filter:
            continue
        if atr_filter and not pd.isna(df['atr_24h'].iloc[i]) and df['atr_24h'].iloc[i] > atr_filter:
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
                trades.append({'pnl_inr': pnl_inr, 'capital': capital, 'exit_time': t})
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
            pos = {'side': 'long', 'entry': entry, 'sl': entry*(1-sl_dist), 'tp': entry*(1+tp_pct),
                   'entry_idx': i+1, 'entry_time': t_entry}
            continue
        if short_sig[i] and i >= block_short_until:
            entry = next_open * (1 - SLIPPAGE_BPS / 10_000)
            stop_level = h[i] * (1 + SLIPPAGE_BPS / 10_000)
            sl_dist = max(SL_PCT, (stop_level - entry) / entry)
            pos = {'side': 'short', 'entry': entry, 'sl': entry*(1+sl_dist), 'tp': entry*(1-tp_pct),
                   'entry_idx': i+1, 'entry_time': t_entry}
            continue

    if pos:
        sign = 1 if pos['side'] == 'long' else -1
        pnl = sign * (c[-1] - pos['entry']) / pos['entry']
        pnl_inr = pnl * capital * dynamic_eff_lev
        capital += pnl_inr
        trades.append({'pnl_inr': pnl_inr, 'capital': capital, 'exit_time': ts[-1]})

    return capital, max_dd, trades


def stats(trades):
    df = pd.DataFrame(trades)
    df['year_week'] = df['exit_time'].dt.isocalendar().year.astype(str) + '-W' + df['exit_time'].dt.isocalendar().week.astype(str).str.zfill(2)
    weekly = df.groupby('year_week').agg(week_ret=('capital', lambda g: (g.iloc[-1] / g.shift(1).fillna(START_CAPITAL_INR).iloc[0]) - 1 if len(g) == 1 else (g.iloc[-1] / g.iloc[0]) - 1))
    # Better: compute start capital per week
    df_sorted = df.sort_values('exit_time')
    weekly_ret = df_sorted.groupby('year_week').apply(lambda g: g['capital'].iloc[-1] / (g['capital'].iloc[0] - g['pnl_inr'].iloc[0]) - 1)
    return len(df), weekly_ret


df = compute_features(load_data())

def run_and_print(label, **kwargs):
    cap, dd, trades = run_strategy(df, **kwargs)
    n, weekly = stats(trades)
    print(f'{label:50s} | Trades: {n:3d} | Final: Rs{cap:>10,.0f} | Return: {(cap/START_CAPITAL_INR-1)*100:>+6.1f}% | MaxDD: {dd*100:>5.1f}% | Worst week: {weekly.min()*100:>6.1f}%')
    return cap, dd, trades, weekly

print('=== Baseline ===')
run_and_print('15x no filters', eff_lev=15.0)
run_and_print('3x no filters', eff_lev=3.0)

print('\n=== Volatility filters at 15x ===')
for v in [0.30, 0.40, 0.50, 0.60]:
    run_and_print(f'vol < {v*100:.0f}%', eff_lev=15.0, vol_filter=v)

print('\n=== ATR filters at 15x ===')
for a in [0.0005, 0.0008, 0.0010]:
    run_and_print(f'atr < {a*100:.2f}%', eff_lev=15.0, atr_filter=a)

print('\n=== Consecutive loss pause at 15x ===')
for nloss in [2, 3, 4]:
    run_and_print(f'pause after {nloss} losses', eff_lev=15.0, consecutive_loss_filter=nloss)

print('\n=== CPPI at 15x max ===')
for mult in [3, 5, 10]:
    run_and_print(f'CPPI floor 80% m={mult}', eff_lev=15.0, cppi_floor=0.80, cppi_mult=mult)

print('\n=== Combined filters at 15x ===')
run_and_print('vol < 40% + pause after 3 losses', eff_lev=15.0, vol_filter=0.40, consecutive_loss_filter=3)
run_and_print('vol < 50% + pause after 3 losses', eff_lev=15.0, vol_filter=0.50, consecutive_loss_filter=3)
run_and_print('vol < 40% + atr < 0.08%', eff_lev=15.0, vol_filter=0.40, atr_filter=0.0008)
run_and_print('vol < 50% + atr < 0.10%', eff_lev=15.0, vol_filter=0.50, atr_filter=0.0010)

print('\n=== Volatility-scaled sizing (no hard filter) ===')
# Size inversely to vol: eff_lev = base / (vol / target_vol)
# Need to implement inside run_strategy; doing quick hack here
def run_vol_scaled(df, base_lev=15.0, target_vol=0.40):
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
    capital = START_CAPITAL_INR
    peak = capital
    max_dd = 0.0
    start_i = max(LOOKBACK, TREND_LOOKBACK) + 10
    for i in range(start_i, n - 1):
        t = ts[i]
        ci = c[i]
        vol = df['vol_24h'].iloc[i]
        eff = base_lev if pd.isna(vol) else base_lev * (target_vol / max(vol, target_vol/3))
        eff = max(0.5, min(base_lev, eff))
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
                pnl_inr = pnl * capital * eff
                capital += pnl_inr
                peak = max(peak, capital)
                max_dd = max(max_dd, (peak - capital) / peak)
                trades.append({'pnl_inr': pnl_inr, 'capital': capital, 'exit_time': t})
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
            pos = {'side': 'long', 'entry': entry, 'sl': entry*(1-sl_dist), 'tp': entry*(1+tp_pct),
                   'entry_idx': i+1, 'entry_time': t_entry}
            continue
        if short_sig[i] and i >= block_short_until:
            entry = next_open * (1 - SLIPPAGE_BPS / 10_000)
            stop_level = h[i] * (1 + SLIPPAGE_BPS / 10_000)
            sl_dist = max(SL_PCT, (stop_level - entry) / entry)
            pos = {'side': 'short', 'entry': entry, 'sl': entry*(1+sl_dist), 'tp': entry*(1-tp_pct),
                   'entry_idx': i+1, 'entry_time': t_entry}
            continue
    if pos:
        sign = 1 if pos['side'] == 'long' else -1
        pnl = sign * (c[-1] - pos['entry']) / pos['entry']
        pnl_inr = pnl * capital * eff
        capital += pnl_inr
        trades.append({'pnl_inr': pnl_inr, 'capital': capital, 'exit_time': ts[-1]})
    n, weekly = stats(trades)
    print(f'vol-scaled base={base_lev} target={target_vol*100:.0f}% | Trades: {n:3d} | Final: Rs{capital:>10,.0f} | Return: {(capital/START_CAPITAL_INR-1)*100:>+6.1f}% | MaxDD: {max_dd*100:>5.1f}% | Worst week: {weekly.min()*100:>6.1f}%')

run_vol_scaled(df, base_lev=15.0, target_vol=0.40)
run_vol_scaled(df, base_lev=15.0, target_vol=0.50)
run_vol_scaled(df, base_lev=10.0, target_vol=0.40)
