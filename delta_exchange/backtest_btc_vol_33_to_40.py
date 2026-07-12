"""
Granular BTC vol filter sweep: 33% to 40% in 1% steps.
Uses fixed Rs 50k budget, no compounding, Apr 1 - Jul 7 2026.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path
import numpy as np
import pandas as pd
from backtest_price_action_sweep import prepare

SYMBOL = 'BTCUSD'
BUDGET = 50000
FIXED_RISK_PCT = 0.50
SL_PCT = 0.006
RR = 7.0
TP_PCT = SL_PCT * RR
LEVERAGE_DEFAULT = 15
VOL_THRESHOLDS = [0.33, 0.34, 0.35, 0.36, 0.37, 0.38, 0.39, 0.40]


def load_btc_df():
    """Load BTC from main data/perp + july_btc/perp."""
    base = Path(__file__).parent / "data"
    frames = []
    for folder in [base / "perp", base / "july_btc" / "perp"]:
        path = folder / f"{SYMBOL}_mark_1m.csv"
        if not path.exists():
            print(f"Warning: {path} not found")
            continue
        df = pd.read_csv(path)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("timestamp").sort_index()
        if df["volume"].isna().all():
            try:
                px = pd.read_csv(folder / f"{SYMBOL}_price_1m.csv")
                px["timestamp"] = pd.to_datetime(px["time"], unit="s", utc=True)
                px = px.set_index("timestamp").sort_index()
                df = df.join(px[["volume"]].rename(columns={"volume": "real_volume"}), how="left")
            except Exception:
                df["real_volume"] = np.nan
        else:
            df["real_volume"] = df["volume"]
        frames.append(df)
    if not frames:
        raise RuntimeError("No BTC data loaded")
    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated(keep='first')]
    df = df[df.index >= pd.Timestamp('2026-04-01', tz='UTC')]
    return df


def compute_vol_24h(close: pd.Series):
    returns = close.pct_change()
    return returns.rolling(24 * 60, min_periods=24 * 60).std() * np.sqrt(365 * 24 * 60)


def run_signals(df, vol_filter_max=0.0):
    s = prepare(df, use_trend=True, retest_mode='wick_touch',
                body_pos_threshold=0.70,
                wick_touch_tol=0.0007,
                vol_filter_max=vol_filter_max)
    o, h, l, c = s['o'], s['h'], s['l'], s['c']
    ts = df.index
    n = len(df)
    long_sig, short_sig = s['retest_long'], s['retest_short']

    vol_24h = compute_vol_24h(pd.Series(c, index=ts))
    vol_arr = vol_24h.values

    trades = []
    pos = None
    cooldown = -1
    start_i = max(240, 1440) + 10

    for i in range(start_i, n - 1):
        t = ts[i]

        if pos is not None:
            sign = 1 if pos['side'] == 'long' else -1
            hi, lo = h[i], l[i]

            if (sign > 0 and hi >= pos['tp']) or (sign < 0 and lo <= pos['tp']):
                pnl = sign * (pos['tp'] - pos['entry']) / pos['entry']
                trades.append({**pos, 'exit': pos['tp'], 'exit_time': t, 'pnl': pnl,
                               'win': pnl > 0, 'reason': 'tp'})
                pos = None
                cooldown = i + 60
                continue

            if (sign > 0 and lo <= pos['sl']) or (sign < 0 and hi >= pos['sl']):
                pnl = sign * (pos['sl'] - pos['entry']) / pos['entry']
                trades.append({**pos, 'exit': pos['sl'], 'exit_time': t, 'pnl': pnl,
                               'win': pnl > 0, 'reason': 'sl'})
                pos = None
                cooldown = i + 60
                continue

            if i - pos['entry_idx'] >= 240:
                pnl = sign * (c[i] - pos['entry']) / pos['entry']
                trades.append({**pos, 'exit': c[i], 'exit_time': t, 'pnl': pnl,
                               'win': pnl > 0, 'reason': 'hold'})
                pos = None
                cooldown = i + 60
                continue
            continue

        if i < cooldown:
            continue

        if long_sig[i]:
            entry = o[i + 1]
            sl = entry * (1 - SL_PCT)
            tp = entry * (1 + TP_PCT)
            pos = {
                'side': 'long', 'entry': entry, 'sl': sl, 'tp': tp,
                'entry_idx': i + 1, 'entry_time': ts[i + 1],
                'vol_24h': float(vol_arr[i]) if not np.isnan(vol_arr[i]) else 0.0,
                'week': int(ts[i].isocalendar().week),
            }
            continue

        if short_sig[i]:
            entry = o[i + 1]
            sl = entry * (1 + SL_PCT)
            tp = entry * (1 - TP_PCT)
            pos = {
                'side': 'short', 'entry': entry, 'sl': sl, 'tp': tp,
                'entry_idx': i + 1, 'entry_time': ts[i + 1],
                'vol_24h': float(vol_arr[i]) if not np.isnan(vol_arr[i]) else 0.0,
                'week': int(ts[i].isocalendar().week),
            }
            continue

    return trades


def run_fixed_capital(trades, budget, leverage):
    capital_per_trade = budget * FIXED_RISK_PCT
    equity = budget
    peak = budget
    gross_pnl = 0.0
    wins = 0
    max_ongoing_dd = 0.0
    for t in trades:
        pnl = capital_per_trade * leverage * t['pnl']
        gross_pnl += pnl
        equity += pnl
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_ongoing_dd:
            max_ongoing_dd = dd
        if pnl > 0:
            wins += 1
    return {
        'trades': len(trades),
        'wins': wins,
        'gross_pnl': gross_pnl,
        'max_dd': max_ongoing_dd,
    }


def main():
    df = load_btc_df()
    print("=" * 110)
    print(f"BTC VOL FILTER GRANULAR SWEEP — 33% to 40%")
    print(f"Fixed Rs {BUDGET:,} budget, no compounding, {df.index[0].date()} to {df.index[-1].date()}")
    print("=" * 110)

    baseline_trades = run_signals(df, vol_filter_max=0.0)
    baseline_res = run_fixed_capital(baseline_trades, BUDGET, LEVERAGE_DEFAULT)

    print(f"{'Threshold':>12} {'Trades':>8} {'Wins':>7} {'Win%':>8} "
          f"{'Gross Rs':>14} {'MaxDD Rs':>12} {'DD%':>8} {'Profit/Risk':>12}")
    print("-" * 110)
    print(f"{'No filter':>12} {baseline_res['trades']:>8} {baseline_res['wins']:>7} "
          f"{100*baseline_res['wins']/baseline_res['trades'] if baseline_res['trades'] else 0:>7.1f}% "
          f"Rs {baseline_res['gross_pnl']:>10,.0f}  Rs {baseline_res['max_dd']:>9,.0f} "
          f"{100*baseline_res['max_dd']/BUDGET:>7.1f}% "
          f"{baseline_res['gross_pnl']/max(baseline_res['max_dd'], 1):>11.1f}x")

    for vol in VOL_THRESHOLDS:
        trades = run_signals(df, vol_filter_max=vol)
        res = run_fixed_capital(trades, BUDGET, LEVERAGE_DEFAULT)
        wr = 100 * res['wins'] / res['trades'] if res['trades'] else 0
        dd_pct = 100 * res['max_dd'] / BUDGET
        profit_risk = res['gross_pnl'] / max(res['max_dd'], 1)
        print(f"{vol:>11.0%}  {res['trades']:>8} {res['wins']:>7} {wr:>7.1f}% "
              f"Rs {res['gross_pnl']:>10,.0f}  Rs {res['max_dd']:>9,.0f} {dd_pct:>7.1f}% "
              f"{profit_risk:>11.1f}x")

    print("=" * 110)


if __name__ == '__main__':
    main()
