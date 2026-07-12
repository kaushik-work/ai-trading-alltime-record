"""
Quick XAUTUSD volatility analysis to choose sensible SL/TP for backtest.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path
import numpy as np
import pandas as pd
from backtest_price_action_sweep import load_perp

df = load_perp('xaut', 'XAUTUSD')
returns = df['close'].pct_change()
vol_24h = returns.rolling(24*60).std() * np.sqrt(365*24*60)
print(f"XAUTUSD data: {df.index[0]} to {df.index[-1]}")
print(f"Mean 24h vol: {vol_24h.mean():.2%}")
print(f"Median 24h vol: {vol_24h.median():.2%}")
print(f"Max 24h vol: {vol_24h.max():.2%}")
print(f"Min 24h vol: {vol_24h.min():.2%}")
print(f"Daily return std: {returns.resample('1D').sum().std():.4%}")
print(f"Mean daily range: {(df['high']-df['low']).mean()/df['close'].mean():.4%}")
print(f"Mean hourly range: {((df['high']-df['low']).resample('1h').sum() / df['close'].resample('1h').last()).mean():.4%}")
