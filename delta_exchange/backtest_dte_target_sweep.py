"""Sweep option DTE target using available weekly expiries."""
import sys
sys.path.insert(0, '.')
import pandas as pd
import numpy as np
from pathlib import Path
from backtest_june_detailed import run_detailed, _spread_pnl_with_ba
from backtest_options_spread_overlay import (
    _load_option_catalog, _nearest_strike, _load_option_series, _mark_at,
    SLIPPAGE_BPS, OPTION_FEE_BPS,
)

ASSET = sys.argv[1] if len(sys.argv) > 1 else 'ETH'
BA_PCT = float(sys.argv[2]) if len(sys.argv) > 2 else 0.01

def find_legs(catalog, side, underlying, t_entry, spot, otm_pct, target_dte):
    df = catalog[(catalog["side"] == side) &
                 (catalog["underlying"] == underlying) &
                 (catalog["t_min"] <= t_entry) &
                 (catalog["t_max"] >= t_entry) &
                 (catalog["expiry"] > t_entry + pd.Timedelta(days=2)) &
                 (catalog["expiry"] <= t_entry + pd.Timedelta(days=14))]
    if df.empty:
        return None
    atm = _nearest_strike(df["strike"].values, spot)
    if side == "C":
        otm_target = spot * (1 + otm_pct)
    else:
        otm_target = spot * (1 - otm_pct)
    otm = _nearest_strike(df["strike"].values, otm_target)
    if otm == atm:
        return None
    exp_candidates = df[df["strike"] == atm]["expiry"]
    if exp_candidates.empty:
        return None
    expiry = exp_candidates.iloc[np.argmin(np.abs(exp_candidates - (t_entry + pd.Timedelta(days=target_dte))))]
    long_candidates = df[(df["strike"] == atm) & (df["expiry"] == expiry)]
    short_candidates = df[(df["strike"] == otm) & (df["expiry"] == expiry)]
    if long_candidates.empty or short_candidates.empty:
        return None
    return long_candidates.iloc[0]["symbol"], short_candidates.iloc[0]["symbol"]

print(f'\n=== {ASSET} DTE target sweep (BA={BA_PCT:.1%}) ===')
for target in [3, 5, 7, 10, 12]:
    rets = run_detailed(ASSET, sl_pct=0.006 if ASSET=='BTC' else 0.007, rr=7.0,
                        ba_spread=BA_PCT, ba_filter=0.10)
    taken = [r for r in rets if r['status'] == 'taken' and r['legs']]
    if taken:
        sum_ret = sum(r['spread_ret_pct'] for r in taken) / 100
        wins = sum(1 for r in taken if r['spread_ret_pct'] > 0)
        print(f' target {target:2d}d: {len(taken):2d} trades, WR {wins/len(taken):.1%}, sum ret {sum_ret:+.2%}')
    else:
        print(f' target {target:2d}d: no trades')
