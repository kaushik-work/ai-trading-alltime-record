"""Report option expiry / DTE distribution and performance by bucket."""
import sys
sys.path.insert(0, '.')
import pandas as pd
import numpy as np
from backtest_june_detailed import run_detailed, summarize

ASSET = sys.argv[1] if len(sys.argv) > 1 else 'ETH'
BA_PCT = float(sys.argv[2]) if len(sys.argv) > 2 else 0.01

print(f'\n=== {ASSET} spread backtest expiry/DTE report (BA={BA_PCT:.1%}) ===')

rets = run_detailed(ASSET, sl_pct=0.006 if ASSET=='BTC' else 0.007, rr=7.0,
                    ba_spread=BA_PCT, ba_filter=0.10)

taken = [r for r in rets if r['status'] == 'taken' and r['legs']]
filtered = [r for r in rets if r['status'] == 'spread_filtered']

print(f'Raw signals: {len(rets)} | Spread taken: {len(taken)} | Filtered out: {len(filtered)}')

if not taken:
    print('No spread trades taken.')
    sys.exit()

def parse_expiry(symbol):
    # e.g. C-BTC-89000-260626 -> 26-06-26
    tail = symbol.split('-')[-1]
    dd, mm, yy = int(tail[:2]), int(tail[2:4]), int(tail[4:6]) + 2000
    return pd.Timestamp(f'{yy}-{mm:02d}-{dd:02d} 12:00:00', tz='UTC')

rows = []
for t in taken:
    exp = parse_expiry(t['legs'][0])
    dte = (exp - t['bt_entry_time']).total_seconds() / 86400
    rows.append({
        'entry_time': t['bt_entry_time'],
        'side': t['side'],
        'expiry': exp,
        'dte': dte,
        'perp_ret': t['perp_ret_pct'] / 100,
        'spread_ret': t['spread_ret_pct'] / 100,
        'exit_reason': t['exit_reason'],
        'ba_cost': t['ba_cost_pct'],
    })

df = pd.DataFrame(rows)

print('\n=== Expiry distribution ===')
print(df.groupby(df['expiry'].dt.date).agg(
    trades=('side', 'count'),
    wins=('spread_ret', lambda s: (s > 0).sum()),
    avg_dte=('dte', 'mean'),
    avg_spread_ret=('spread_ret', 'mean'),
    sum_spread_ret=('spread_ret', 'sum'),
).to_string())

print('\n=== Performance by DTE bucket (spread return) ===')
df['dte_bucket'] = pd.cut(df['dte'], bins=[0, 3, 6, 9, 15, 50], labels=['0-3d', '3-6d', '6-9d', '9-15d', '15d+'])
print(df.groupby('dte_bucket').agg(
    trades=('spread_ret', 'count'),
    wins=('spread_ret', lambda s: (s > 0).sum()),
    avg=('spread_ret', 'mean'),
    total=('spread_ret', 'sum'),
).to_string())

print('\n=== Overall spread metrics ===')
sum_ret = df['spread_ret'].sum()
win = (df['spread_ret'] > 0).sum()
print(f"Trades: {len(df)} | Wins: {win} ({win/len(df):.1%}) | Sum return: {sum_ret:.2%} | Avg: {df['spread_ret'].mean():.2%}")
print(f"DTE range: {df['dte'].min():.1f} - {df['dte'].max():.1f} days | Avg DTE: {df['dte'].mean():.1f}")
