"""
Multi-asset S/R retest backtest: BTC + ETH + XAUT.

Uses the same price-action S/R engine as the live ETH bot, but runs it on all
three perps independently. Costs and capital assumptions mirror live:
  - ₹50,000 fixed capital per trade per asset
  - 15× leverage
  - 5 bps/side fee, 2 bps entry + exit slippage
  - 1-minute entry grid

Optimized dials from the ETH sweep:
  SL 1.0%, R:R 1:7, vol filter 34%, wick_touch retest.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import numpy as np
import pandas as pd
from backtest_price_action_sweep import load_perp, prepare

BUDGET_INR = 50_000.0
LEVERAGE = 15.0
PERP_FEE_BPS = 5.0
SLIPPAGE_BPS = 2.0

ASSET_DIALS = {
    "BTCUSD": {"sl_pct": 0.006, "rr": 7.0, "vol_filter_max": 0.34},
    "ETHUSD": {"sl_pct": 0.007, "rr": 7.0, "vol_filter_max": 0.34},
    "XAUTUSD": {"sl_pct": 0.010, "rr": 5.0, "vol_filter_max": 0.25},
}
RETEST_MODE = "wick_touch"
BODY_POS_THRESHOLD = 0.70
WICK_TOUCH_TOL = 0.0007

COOLDOWN_CANDLES = 60
BLOCK_AFTER_LOSS_CANDLES = 180
MAX_HOLD_CANDLES = 240

ASSETS = {
    "BTCUSD": ["perp", "june_btc", "july_btc"],
    "ETHUSD": ["eth", "july_eth"],
    "XAUTUSD": ["xaut"],
}


def load_asset(symbol):
    dfs = []
    for subdir in ASSETS[symbol]:
        try:
            dfs.append(load_perp(subdir, symbol))
        except Exception as e:
            pass
    if not dfs:
        raise RuntimeError(f"No data for {symbol}")
    df = pd.concat(dfs).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[df.index >= pd.Timestamp("2026-04-01", tz="UTC")]
    return df


def run_asset(df, symbol):
    dials = ASSET_DIALS[symbol]
    sl_pct = dials["sl_pct"]
    rr = dials["rr"]
    tp_pct = sl_pct * rr
    vol_filter_max = dials["vol_filter_max"]
    s = prepare(df, use_trend=True, retest_mode=RETEST_MODE,
                body_pos_threshold=BODY_POS_THRESHOLD, wick_touch_tol=WICK_TOUCH_TOL,
                vol_filter_max=vol_filter_max)
    o, h, l, c = s["o"], s["h"], s["l"], s["c"]
    ts = df.index
    n = len(df)
    long_sig, short_sig = s["retest_long"], s["retest_short"]

    trades = []
    pos = None
    cooldown = -1
    block_long_until = -1
    block_short_until = -1
    start_i = max(240, 1440) + 10

    for i in range(start_i, n - 1):
        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            reason = None
            exit_px = None
            if (sign > 0 and h[i] >= pos["tp"]) or (sign < 0 and l[i] <= pos["tp"]):
                reason = "tp"
                exit_px = pos["tp"] * (1 - sign * SLIPPAGE_BPS / 1e4)
            elif (sign > 0 and l[i] <= pos["sl"]) or (sign < 0 and h[i] >= pos["sl"]):
                reason = "sl"
                exit_px = pos["sl"] * (1 - sign * SLIPPAGE_BPS / 1e4)
            elif i - pos["entry_idx"] >= MAX_HOLD_CANDLES:
                reason = "hold"
                exit_px = c[i] * (1 - sign * SLIPPAGE_BPS / 1e4)

            if reason is not None:
                gross = sign * (exit_px - pos["entry"]) / pos["entry"]
                net = gross - 2 * PERP_FEE_BPS / 1e4
                trades.append({**pos, "exit_px": exit_px, "exit_reason": reason,
                               "net_pnl_pct": net, "exit_time": ts[i]})
                pos = None
                cooldown = i + COOLDOWN_CANDLES
                if net <= 0:
                    if sign > 0:
                        block_long_until = i + BLOCK_AFTER_LOSS_CANDLES
                    else:
                        block_short_until = i + BLOCK_AFTER_LOSS_CANDLES
            continue

        if i < cooldown:
            continue

        next_open = o[i + 1]
        t_entry = ts[i + 1]

        if long_sig[i] and i >= block_long_until:
            entry = next_open * (1 + SLIPPAGE_BPS / 1e4)
            stop_level = l[i] * (1 - SLIPPAGE_BPS / 1e4)
            sl_dist = max(sl_pct, (entry - stop_level) / entry)
            pos = {
                "side": "long", "entry": entry,
                "sl": entry * (1 - sl_dist),
                "tp": entry * (1 + tp_pct),
                "entry_idx": i + 1, "entry_time": t_entry,
                "decision_time": ts[i], "sl_dist": sl_dist,
            }
            continue

        if short_sig[i] and i >= block_short_until:
            entry = next_open * (1 - SLIPPAGE_BPS / 1e4)
            stop_level = h[i] * (1 + SLIPPAGE_BPS / 1e4)
            sl_dist = max(sl_pct, (stop_level - entry) / entry)
            pos = {
                "side": "short", "entry": entry,
                "sl": entry * (1 + sl_dist),
                "tp": entry * (1 - tp_pct),
                "entry_idx": i + 1, "entry_time": t_entry,
                "decision_time": ts[i], "sl_dist": sl_dist,
            }

    return trades


def evaluate(trades):
    equity = BUDGET_INR
    peak = equity
    gross = 0.0
    wins = 0
    max_dd = 0.0
    for t in trades:
        pnl = BUDGET_INR * LEVERAGE * t["net_pnl_pct"]
        gross += pnl
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if pnl > 0:
            wins += 1
    n = len(trades)
    return {
        "trades": n, "wins": wins,
        "win_pct": 100 * wins / n if n else 0,
        "gross": gross, "max_dd": max_dd,
        "equity": equity,
        "ret_pct": 100 * (equity - BUDGET_INR) / BUDGET_INR,
    }


def main():
    print("Multi-asset S/R retest backtest")
    print(f"Dials: asset-specific SL/R:R/vol, {RETEST_MODE}")
    print(f"Capital: ₹{BUDGET_INR:,.0f}/asset, {LEVERAGE:.0f}x leverage\n")

    all_trades = []
    for symbol in ASSETS:
        df = load_asset(symbol)
        print(f"{symbol}: {len(df)} bars from {df.index[0].date()} to {df.index[-1].date()}")
        trades = run_asset(df, symbol)
        res = evaluate(trades)
        print(f"  Trades: {res['trades']}  Wins: {res['wins']} ({res['win_pct']:.1f}%)  "
              f"Gross: ₹{res['gross']:+.0f}  MaxDD: ₹{res['max_dd']:.0f}  "
              f"Return: {res['ret_pct']:+.1f}%")
        for t in trades:
            t["symbol"] = symbol
        all_trades.extend(trades)

    # Combined: assume independent ₹50k per asset = total deployed ₹150k
    combined = evaluate(all_trades)
    print("\n" + "=" * 80)
    print("Combined (₹50k per asset, up to 3 concurrent positions)")
    print("=" * 80)
    print(f"  Total trades: {combined['trades']}")
    print(f"  Wins: {combined['wins']} ({combined['win_pct']:.1f}%)")
    print(f"  Gross P&L: ₹{combined['gross']:+.0f}")
    print(f"  MaxDD: ₹{combined['max_dd']:.0f}")
    print(f"  Return on total deployed capital: {combined['ret_pct']:+.1f}%")
    print(f"  Profit/Risk: {combined['gross']/max(combined['max_dd'],1):.2f}")

    # Also show per-month combined
    if all_trades:
        monthly = {}
        for t in all_trades:
            m = t["entry_time"].strftime("%Y-%m")
            monthly.setdefault(m, 0.0)
            monthly[m] += BUDGET_INR * LEVERAGE * t["net_pnl_pct"]
        print("\n  Monthly combined P&L:")
        for m in sorted(monthly):
            print(f"    {m}: ₹{monthly[m]:>+10,.0f}")


if __name__ == "__main__":
    main()
