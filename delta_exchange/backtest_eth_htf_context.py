"""ETH strategy with higher-timeframe context."""
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

def run_strategy(df, s, eff_lev=15.0, htf_lookback_min=None, htf_zone_pct=None, use_vol_filter=None):
    o, h, l, c = s['o'], s['h'], s['l'], s['c']
    ts = df.index
    n = len(df)
    long_sig, short_sig = s['retest_long'], s['retest_short']
    
    if htf_lookback_min:
        htf_high = df['high'].rolling(htf_lookback_min, min_periods=htf_lookback_min).max()
        htf_low = df['low'].rolling(htf_lookback_min, min_periods=htf_lookback_min).min()
        htf_mid = (htf_high + htf_low) / 2
    else:
        htf_high = htf_low = htf_mid = pd.Series(np.nan, index=df.index)
    
    r = df['close'].pct_change()
    vol_24h = r.rolling(24*60).std() * np.sqrt(365*24*60)

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
        
        if use_vol_filter and not pd.isna(vol_24h.iloc[i]) and vol_24h.iloc[i] > use_vol_filter:
            continue

        if htf_lookback_min:
            if htf_zone_pct:
                supply_bot = htf_high * (1 - htf_zone_pct)
                demand_top = htf_low * (1 + htf_zone_pct)
                if long_sig[i] and ci >= supply_bot.iloc[i]:
                    continue
                if short_sig[i] and ci <= demand_top.iloc[i]:
                    continue
            else:
                if long_sig[i] and ci >= htf_mid.iloc[i]:
                    continue
                if short_sig[i] and ci <= htf_mid.iloc[i]:
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
                pnl_inr = pnl * capital * eff_lev
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
        pnl_inr = pnl * capital * eff_lev
        capital += pnl_inr
        trades.append({'pnl_inr': pnl_inr, 'capital': capital, 'exit_time': ts[-1]})

    return capital, max_dd, trades


def stats(trades):
    if not trades:
        return 0, pd.Series(dtype=float)
    df = pd.DataFrame(trades)
    df['year_week'] = df['exit_time'].dt.isocalendar().year.astype(str) + '-W' + df['exit_time'].dt.isocalendar().week.astype(str).str.zfill(2)
    weekly_ret = df.groupby('year_week').apply(lambda g: g['capital'].iloc[-1] / (g['capital'].iloc[0] - g['pnl_inr'].iloc[0]) - 1)
    return len(df), weekly_ret


def print_result(label, cap, dd, trades):
    n, weekly = stats(trades)
    worst = weekly.min() if len(weekly) else 0
    print(f'{label:55s} | Trades: {n:3d} | Final: Rs{cap:>10,.0f} | Return: {(cap/START_CAPITAL_INR-1)*100:>+6.1f}% | MaxDD: {dd*100:>5.1f}% | Worst week: {worst*100:>6.1f}%')


if __name__ == '__main__':
    df = load_data()
    s = prepare(df, use_trend=True, retest_mode='wick_touch', body_pos_threshold=0.70, wick_touch_tol=0.0007)
    
    print('=== Baseline ===')
    cap, dd, trades = run_strategy(df, s, eff_lev=15.0)
    print_result('15x no filters', cap, dd, trades)
    
    print('\n=== HTF midpoint filter: buy only lower half, sell only upper half ===')
    for days in [1, 3, 7, 14]:
        cap, dd, trades = run_strategy(df, s, eff_lev=15.0, htf_lookback_min=days*24*60)
        print_result(f'HTF {days}d midpoint', cap, dd, trades)
    
    print('\n=== Combine HTF 7d midpoint + vol<33% ===')
    cap, dd, trades = run_strategy(df, s, eff_lev=15.0, htf_lookback_min=7*24*60, use_vol_filter=0.33)
    print_result('7d HTF midpoint + vol<33%', cap, dd, trades)
