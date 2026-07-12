"""ETH P&L with fixed trading budget (no compounding)."""
import sys
sys.path.insert(0, '.')
import pandas as pd
import numpy as np
from backtest_price_action_sweep import load_perp, prepare, LOOKBACK, TREND_LOOKBACK, MAX_HOLD_CANDLES, COOLDOWN_CANDLES, SLIPPAGE_BPS

FIXED_CAPITAL_INR = 50_000.0
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

def run_fixed_capital(df, eff_lev=15.0, vol_filter=None):
    s = prepare(df, use_trend=True, retest_mode='wick_touch',
                body_pos_threshold=0.70, wick_touch_tol=0.0007,
                vol_filter_max=vol_filter if vol_filter else 0.0)
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
    running_pnl = 0.0
    peak_pnl = 0.0
    max_dd = 0.0

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
                pnl_inr = pnl * FIXED_CAPITAL_INR * eff_lev
                running_pnl += pnl_inr
                peak_pnl = max(peak_pnl, running_pnl)
                max_dd = max(max_dd, peak_pnl - running_pnl)
                trades.append({'pnl_inr': pnl_inr, 'running_pnl': running_pnl, 'exit_time': t, 'side': pos['side']})
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
        pnl_inr = pnl * FIXED_CAPITAL_INR * eff_lev
        running_pnl += pnl_inr
        trades.append({'pnl_inr': pnl_inr, 'running_pnl': running_pnl, 'exit_time': ts[-1], 'side': pos['side']})

    return running_pnl, max_dd, trades


def weekly_stats(trades):
    df = pd.DataFrame(trades)
    df['year_week'] = df['exit_time'].dt.isocalendar().year.astype(str) + '-W' + df['exit_time'].dt.isocalendar().week.astype(str).str.zfill(2)
    weekly = df.groupby('year_week').agg(trades=('pnl_inr','count'), wins=('pnl_inr', lambda s: (s>0).sum()), pnl=('pnl_inr','sum'))
    return weekly


df = load_data()

print(f'=== Fixed capital ₹{FIXED_CAPITAL_INR:,.0f}, no compounding ===\n')

for eff_lev, vol_f in [(15.0, None), (15.0, 0.33), (5.0, None), (3.0, None)]:
    pnl, dd, trades = run_fixed_capital(df, eff_lev=eff_lev, vol_filter=vol_f)
    weekly = weekly_stats(trades)
    label = f'{eff_lev:.0f}x'
    if vol_f:
        label += f' vol<{vol_f*100:.0f}%'
    print(f'\n{label}:')
    print(f'  Total trades: {len(trades)} | Wins: {(np.array([t["pnl_inr"] for t in trades]) > 0).sum()}')
    print(f'  Gross profit: ₹{pnl:,.0f}')
    print(f'  Return on budget: {pnl/FIXED_CAPITAL_INR*100:.1f}%')
    print(f'  MaxDD (from peak P&L): ₹{dd:,.0f} ({dd/FIXED_CAPITAL_INR*100:.1f}% of budget)')
    print(f'  Worst week: ₹{weekly["pnl"].min():,.0f}')
    print(f'  Best week:  ₹{weekly["pnl"].max():,.0f}')
    print('\n  Week-by-week:')
    running = 0.0
    for yw, row in weekly.iterrows():
        running += row['pnl']
        print(f'    {yw}: {int(row["trades"]):2d} trades, {int(row["wins"]):2d} wins, P&L ₹{row["pnl"]:>+10,.0f}, running ₹{running:>+10,.0f}')
