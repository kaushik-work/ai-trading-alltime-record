"""ETH P&L with weekly 10% and monthly 20% loss caps."""
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

def run_backtest():
    df_eth = load_perp('eth', 'ETHUSD')
    df_july = load_perp('july_eth', 'ETHUSD')
    start = pd.Timestamp('2026-04-01', tz='UTC')
    cut = pd.Timestamp('2026-06-21', tz='UTC')
    df = pd.concat([df_eth[df_eth.index >= start], df_july[df_july.index >= cut]])
    df = df[~df.index.duplicated(keep='first')].sort_index()

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
            pos = {'side': 'long', 'entry': entry, 'sl': sl, 'tp': tp,
                   'entry_idx': i + 1, 'entry_time': t_entry, 'signal_time': t}
            continue

        if short_sig[i] and i >= block_short_until:
            entry = next_open * (1 - SLIPPAGE_BPS / 10_000)
            stop_level = h[i] * (1 + SLIPPAGE_BPS / 10_000)
            sl_dist = max(SL_PCT, (stop_level - entry) / entry)
            sl = entry * (1 + sl_dist)
            tp = entry * (1 - tp_pct)
            pos = {'side': 'short', 'entry': entry, 'sl': sl, 'tp': tp,
                   'entry_idx': i + 1, 'entry_time': t_entry, 'signal_time': t}
            continue

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
        })

    return pd.DataFrame(trades)


def simulate_no_circuit_breaker(trades_df, eff_lev):
    capital = START_CAPITAL_INR
    peak = capital
    max_dd = 0.0
    trades_df = trades_df.copy()
    trades_df['year_week'] = trades_df['exit_time'].dt.isocalendar().year.astype(str) + '-W' + trades_df['exit_time'].dt.isocalendar().week.astype(str).str.zfill(2)
    trades_df['year_month'] = trades_df['exit_time'].dt.strftime('%Y-%m')

    weekly = trades_df.groupby('year_week').apply(lambda g: (g['pnl_pct'] * eff_lev).sum())
    monthly = trades_df.groupby('year_month').apply(lambda g: (g['pnl_pct'] * eff_lev).sum())

    for _, t in trades_df.iterrows():
        pnl_inr = t['pnl_pct'] * capital * eff_lev
        capital += pnl_inr
        peak = max(peak, capital)
        max_dd = max(max_dd, (peak - capital) / peak)

    return capital, max_dd, weekly, monthly


trades = run_backtest()
print(f'ETH trades: {len(trades)} | WR: {(trades["pnl_pct"] > 0).mean():.1%}\n')

print('=== Max leverage for 10% weekly / 20% monthly loss limits ===')
for eff_lev in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15]:
    final, max_dd, weekly, monthly = simulate_no_circuit_breaker(trades, eff_lev)
    worst_week = weekly.min()
    worst_month = monthly.min()
    ok = worst_week >= -0.10 and worst_month >= -0.20
    print(f'Eff lev {eff_lev:2.0f}x | Worst week {worst_week:+.1%} | Worst month {worst_month:+.1%} | Final {final:>12,.0f} | MaxDD {max_dd:.1%} | {"OK" if ok else "EXCEEDS"}')

print('\n=== Recommended: 3x effective exposure ===')
final, max_dd, weekly, monthly = simulate_no_circuit_breaker(trades, 3.0)
print(f'Final capital: Rs{final:,.0f} | Total return: {(final-START_CAPITAL_INR)/START_CAPITAL_INR:+.1%} | MaxDD: {max_dd:.1%}')
print('\nWeek-by-week:')
for yw, ret in weekly.items():
    print(f'  {yw}: {ret:+.1%}')
print('\nMonth-by-month:')
for ym, ret in monthly.items():
    print(f'  {ym}: {ret:+.1%}')


def simulate_with_caps(trades_df, eff_lev, weekly_cap=-0.10, monthly_cap=-0.20):
    """Apply weekly and monthly loss circuit breakers."""
    capital = START_CAPITAL_INR
    peak = capital
    max_dd = 0.0
    trades_df = trades_df.copy()
    trades_df['year_week'] = trades_df['exit_time'].dt.isocalendar().year.astype(str) + '-W' + trades_df['exit_time'].dt.isocalendar().week.astype(str).str.zfill(2)
    trades_df['year_month'] = trades_df['exit_time'].dt.strftime('%Y-%m')

    week_running_capital = capital
    month_running_capital = capital
    current_week = None
    current_month = None
    week_start_capital = capital
    month_start_capital = capital
    week_halted = False
    month_halted = False
    skipped = 0

    for _, t in trades_df.iterrows():
        yw = t['year_week']
        ym = t['year_month']

        if yw != current_week:
            current_week = yw
            week_start_capital = week_running_capital
            week_halted = False

        if ym != current_month:
            current_month = ym
            month_start_capital = month_running_capital
            month_halted = False

        if week_halted or month_halted:
            skipped += 1
            continue

        pnl_inr = t['pnl_pct'] * capital * eff_lev
        capital += pnl_inr
        week_running_capital += pnl_inr
        month_running_capital += pnl_inr
        peak = max(peak, capital)
        max_dd = max(max_dd, (peak - capital) / peak)

        week_ret = (week_running_capital - week_start_capital) / week_start_capital
        month_ret = (month_running_capital - month_start_capital) / month_start_capital

        if week_ret <= weekly_cap:
            week_halted = True
        if month_ret <= monthly_cap:
            month_halted = True

    return capital, max_dd, skipped


print('\n=== With 10% weekly / 20% monthly circuit breakers at 3x ===')
final_cb, max_dd_cb, skipped = simulate_with_caps(trades, 3.0)
print(f'Final capital: Rs{final_cb:,.0f} | Return: {(final_cb-START_CAPITAL_INR)/START_CAPITAL_INR:+.1%} | MaxDD: {max_dd_cb:.1%} | Trades skipped: {skipped}')
