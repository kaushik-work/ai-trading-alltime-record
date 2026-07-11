"""ETH strategy with high-probability ICT/confluence filters."""
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


def detect_fvg(df_15m):
    """Detect Fair Value Gaps on 15m candles."""
    o = df_15m['open']
    h = df_15m['high']
    l = df_15m['low']
    c = df_15m['close']
    n = len(df_15m)
    bull_top = pd.Series(np.nan, index=df_15m.index)
    bull_bot = pd.Series(np.nan, index=df_15m.index)
    bear_top = pd.Series(np.nan, index=df_15m.index)
    bear_bot = pd.Series(np.nan, index=df_15m.index)
    for i in range(1, n - 1):
        if l.iloc[i + 1] > h.iloc[i - 1]:
            bull_top.iloc[i] = l.iloc[i + 1]
            bull_bot.iloc[i] = h.iloc[i - 1]
        if h.iloc[i + 1] < l.iloc[i - 1]:
            bear_top.iloc[i] = l.iloc[i - 1]
            bear_bot.iloc[i] = h.iloc[i + 1]
    return bull_top, bull_bot, bear_top, bear_bot


def run_strategy(df, s, eff_lev=15.0, kill_zone=None, require_fvg=False,
                 htf_trend_strength=None, use_vol_filter=None):
    o, h, l, c = s['o'], s['h'], s['l'], s['c']
    ts = df.index
    n = len(df)
    long_sig, short_sig = s['retest_long'], s['retest_short']

    df_15m = df.resample('15min').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
    bull_top, bull_bot, bear_top, bear_bot = detect_fvg(df_15m)
    bull_top_1m = bull_top.reindex(df.index, method='ffill')
    bull_bot_1m = bull_bot.reindex(df.index, method='ffill')
    bear_top_1m = bear_top.reindex(df.index, method='ffill')
    bear_bot_1m = bear_bot.reindex(df.index, method='ffill')

    df_1h = df.resample('1h').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
    ema20_1h = df_1h['close'].ewm(span=20, min_periods=10).mean()
    htf_close = df_1h['close'].reindex(df.index, method='ffill').values
    htf_ema = ema20_1h.reindex(df.index, method='ffill').values
    htf_slope = pd.Series(htf_ema).diff(20).values

    r = df['close'].pct_change()
    vol_24h = r.rolling(24 * 60).std() * np.sqrt(365 * 24 * 60)

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

        if kill_zone:
            hour = t.hour
            start_h, end_h = kill_zone
            if start_h < end_h:
                if not (start_h <= hour < end_h):
                    continue
            else:
                if not (hour >= start_h or hour < end_h):
                    continue

        if htf_trend_strength:
            if long_sig[i]:
                if not (htf_close[i] > htf_ema[i] and htf_slope[i] > htf_trend_strength * htf_close[i]):
                    continue
            if short_sig[i]:
                if not (htf_close[i] < htf_ema[i] and htf_slope[i] < -htf_trend_strength * htf_close[i]):
                    continue

        if require_fvg:
            in_bull_fvg = (ci >= bull_bot_1m.iloc[i]) & (ci <= bull_top_1m.iloc[i])
            in_bear_fvg = (ci >= bear_bot_1m.iloc[i]) & (ci <= bear_top_1m.iloc[i])
            if long_sig[i] and not in_bull_fvg:
                continue
            if short_sig[i] and not in_bear_fvg:
                continue

        if pos is not None:
            sign = 1 if pos['side'] == 'long' else -1
            hi, lo = h[i], l[i]
            reason = None
            exit_px = None
            if (sign > 0 and hi >= pos['tp']) or (sign < 0 and lo <= pos['tp']):
                reason = 'tp'
                exit_px = pos['tp']
            else:
                stop = pos['sl']
                if (sign > 0 and lo <= stop) or (sign < 0 and hi >= stop):
                    reason = 'sl'
                    exit_px = stop
                elif i - pos['entry_idx'] >= MAX_HOLD_CANDLES:
                    reason = 'hold'
                    exit_px = ci
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
            pos = {'side': 'long', 'entry': entry, 'sl': entry * (1 - sl_dist), 'tp': entry * (1 + tp_pct),
                   'entry_idx': i + 1, 'entry_time': t_entry}
            continue
        if short_sig[i] and i >= block_short_until:
            entry = next_open * (1 - SLIPPAGE_BPS / 10_000)
            stop_level = h[i] * (1 + SLIPPAGE_BPS / 10_000)
            sl_dist = max(SL_PCT, (stop_level - entry) / entry)
            pos = {'side': 'short', 'entry': entry, 'sl': entry * (1 + sl_dist), 'tp': entry * (1 - tp_pct),
                   'entry_idx': i + 1, 'entry_time': t_entry}
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
    df['year_week'] = (df['exit_time'].dt.isocalendar().year.astype(str) + '-W' +
                       df['exit_time'].dt.isocalendar().week.astype(str).str.zfill(2))
    weekly_ret = df.groupby('year_week').apply(
        lambda g: g['capital'].iloc[-1] / (g['capital'].iloc[0] - g['pnl_inr'].iloc[0]) - 1)
    return len(df), weekly_ret


def print_result(label, cap, dd, trades):
    n, weekly = stats(trades)
    worst = weekly.min() if len(weekly) else 0
    print(f'{label:50s} | Trades: {n:3d} | Final: Rs{cap:>10,.0f} | '
          f'Return: {(cap / START_CAPITAL_INR - 1) * 100:>+6.1f}% | MaxDD: {dd * 100:>5.1f}% | '
          f'Worst week: {worst * 100:>6.1f}%')


if __name__ == '__main__':
    df = load_data()
    s = prepare(df, use_trend=True, retest_mode='wick_touch',
                body_pos_threshold=0.70, wick_touch_tol=0.0007)

    print('=== Baseline ===')
    cap, dd, trades = run_strategy(df, s, eff_lev=15.0)
    print_result('15x no filters', cap, dd, trades)

    print('\n=== Kill Zone filters (UTC) ===')
    for name, kz in [('London 7-10', (7, 10)), ('NY 12-15', (12, 15)),
                     ('Both 7-15', (7, 15)), ('Exclude 7-15', (15, 7))]:
        cap, dd, trades = run_strategy(df, s, eff_lev=15.0, kill_zone=kz)
        print_result(name, cap, dd, trades)

    print('\n=== HTF trend strength filter ===')
    for strength in [0.0001, 0.0003, 0.0005, 0.0010]:
        cap, dd, trades = run_strategy(df, s, eff_lev=15.0, htf_trend_strength=strength)
        print_result(f'HTF slope >{strength * 100:.2f}%', cap, dd, trades)

    print('\n=== FVG filter ===')
    cap, dd, trades = run_strategy(df, s, eff_lev=15.0, require_fvg=True)
    print_result('require FVG', cap, dd, trades)

    print('\n=== Combined high-probability ===')
    cap, dd, trades = run_strategy(df, s, eff_lev=15.0, kill_zone=(7, 15), use_vol_filter=0.33)
    print_result('kill zone + vol<33%', cap, dd, trades)

    cap, dd, trades = run_strategy(df, s, eff_lev=15.0, kill_zone=(7, 15),
                                   require_fvg=True, use_vol_filter=0.33)
    print_result('kill zone + FVG + vol<33%', cap, dd, trades)
