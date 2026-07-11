"""Analyze market conditions during ETH losing weeks."""
import sys
sys.path.insert(0, '.')
import pandas as pd
import numpy as np
from backtest_eth_risk_capped import run_backtest

trades = run_backtest()
trades_df = pd.DataFrame(trades)
trades_df['year_week'] = trades_df['exit_time'].dt.isocalendar().year.astype(str) + '-W' + trades_df['exit_time'].dt.isocalendar().week.astype(str).str.zfill(2)

print('=== Losing weeks detail ===')
weekly = trades_df.groupby('year_week').agg(
    trades=('pnl_pct', 'count'),
    wins=('pnl_pct', lambda s: (s > 0).sum()),
    total_pnl=('pnl_pct', 'sum'),
    avg_pnl=('pnl_pct', 'mean'),
)
weekly = weekly[weekly['total_pnl'] < 0].sort_values('total_pnl')
print(weekly)

# Load price data for those weeks
from backtest_price_action_sweep import load_perp
df_eth = load_perp('eth', 'ETHUSD')
df_july = load_perp('july_eth', 'ETHUSD')
start = pd.Timestamp('2026-04-01', tz='UTC')
cut = pd.Timestamp('2026-06-21', tz='UTC')
df = pd.concat([df_eth[df_eth.index >= start], df_july[df_july.index >= cut]])
df = df[~df.index.duplicated(keep='first')].sort_index()

# Compute daily realized volatility (1h returns, annualized)
returns = df['close'].pct_change()
df['vol_24h'] = returns.rolling(24*60).std() * np.sqrt(365*24*60)
df['atr_24h'] = (df['high'] - df['low']).rolling(24*60).mean() / df['close']
df['trend_24h'] = (df['close'] - df['close'].shift(24*60)) / df['close'].shift(24*60)

print('\n=== Market conditions during losing weeks ===')
for yw in weekly.index:
    # Get ISO week start/end
    year, week = int(yw[:4]), int(yw[6:])
    week_start = pd.Timestamp.fromisocalendar(year, week, 1).tz_localize('UTC')
    week_end = week_start + pd.Timedelta(days=7)
    sub = df[(df.index >= week_start) & (df.index < week_end)]
    if sub.empty:
        continue
    print(f'\n{yw}:')
    print(f'  Price range: {sub["close"].min():.0f} - {sub["close"].max():.0f}')
    print(f'  Weekly return: {(sub["close"].iloc[-1] / sub["close"].iloc[0] - 1)*100:.2f}%')
    print(f'  Avg 24h vol: {sub["vol_24h"].mean()*100:.1f}%')
    print(f'  Max 24h vol: {sub["vol_24h"].max()*100:.1f}%')
    print(f'  Avg ATR(24h): {sub["atr_24h"].mean()*100:.2f}%')
    print(f'  24h trend at start: {sub["trend_24h"].iloc[0]*100:.2f}%')
