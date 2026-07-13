"""
Multi-asset / inter-market momentum prototype.

Tests the idea that BTC leads ETH. When BTC breaks above its recent 1h high
while ETH is still below its corresponding level, buy ETH expecting catch-up.
Converse for shorts.

Uses only locally available perp 1m data.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import numpy as np
import pandas as pd
from backtest_price_action_sweep import load_perp

BUDGET_INR = 50_000.0
LEVERAGE = 15.0
FEE_BPS = 5.0
SLIP_BPS = 2.0

LOOKBACK_H = 6        # BTC breakout lookback in hours
SL_PCT = 0.007
TP_PCT = 0.021
MAX_HOLD_MIN = 240
COOLDOWN_MIN = 60


def load_eth_btc():
    eth_dfs = [load_perp(s, "ETHUSD") for s in ["eth", "july_eth"]]
    btc_dfs = [load_perp(s, "BTCUSD") for s in ["june_btc", "july_btc"]]
    eth = pd.concat(eth_dfs).sort_index()
    btc = pd.concat(btc_dfs).sort_index()
    eth = eth[~eth.index.duplicated(keep="first")]
    btc = btc[~btc.index.duplicated(keep="first")]
    eth = eth[eth.index >= pd.Timestamp("2026-06-01", tz="UTC")]
    btc = btc[btc.index >= pd.Timestamp("2026-06-01", tz="UTC")]
    return eth, btc


def hourly_signals(close, lookback_h):
    """Return boolean arrays for new high/low breakout on 1h resampled close."""
    s = pd.Series(close).resample("1h").last().dropna()
    upper = s.rolling(lookback_h).max().shift(1)
    lower = s.rolling(lookback_h).min().shift(1)
    high_sig = (s > upper).reindex(close.index, method="ffill").fillna(False).values
    low_sig = (s < lower).reindex(close.index, method="ffill").fillna(False).values
    return high_sig, low_sig


def run():
    eth, btc = load_eth_btc()
    start = max(eth.index[0], btc.index[0])
    eth = eth[eth.index >= start]
    btc = btc[btc.index >= start]

    # Align on ETH timestamps using nearest BTC mark
    btc_close = btc["close"].reindex(eth.index, method="nearest")
    eth_c = eth["close"].values
    eth_h = eth["high"].values
    eth_l = eth["low"].values
    eth_o = eth["open"].values
    btc_c = btc_close.values
    ts = eth.index
    n = len(eth)

    btc_high, btc_low = hourly_signals(btc["close"], LOOKBACK_H)
    eth_high, eth_low = hourly_signals(eth["close"], LOOKBACK_H)

    equity = BUDGET_INR
    peak = equity
    trades = []
    pos = None
    cooldown_until = -1

    for i in range(LOOKBACK_H * 60 + 10, n - 1):
        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            reason = None
            exit_px = None
            if sign > 0:
                if eth_l[i] <= pos["sl"]:
                    exit_px = max(eth_l[i], pos["sl"])
                    reason = "sl"
                elif eth_h[i] >= pos["tp"]:
                    exit_px = min(eth_h[i], pos["tp"])
                    reason = "tp"
            else:
                if eth_h[i] >= pos["sl"]:
                    exit_px = min(eth_h[i], pos["sl"])
                    reason = "sl"
                elif eth_l[i] <= pos["tp"]:
                    exit_px = max(eth_l[i], pos["tp"])
                    reason = "tp"

            if reason is None and i - pos["entry_idx"] >= MAX_HOLD_MIN:
                exit_px = eth_c[i]
                reason = "hold"

            if reason is not None:
                fill = exit_px * (1 - sign * SLIP_BPS / 1e4)
                gross = sign * (fill - pos["entry"]) / pos["entry"]
                net = gross - 2 * FEE_BPS / 1e4
                pnl = BUDGET_INR * LEVERAGE * net
                equity += pnl
                peak = max(peak, equity)
                trades.append({**pos, "exit": fill, "reason": reason, "pnl": pnl,
                               "exit_time": ts[i]})
                pos = None
                cooldown_until = i + COOLDOWN_MIN
            continue

        if i < cooldown_until:
            continue

        # BTC leads: BTC breaks high, ETH has not yet → long ETH catch-up
        if btc_high[i] and not eth_high[i]:
            entry = eth_o[i + 1] * 1.0002
            pos = {
                "side": "long", "entry": entry,
                "sl": entry * (1 - SL_PCT),
                "tp": entry * (1 + TP_PCT),
                "entry_idx": i + 1, "entry_time": ts[i + 1],
            }
        elif btc_low[i] and not eth_low[i]:
            entry = eth_o[i + 1] * 0.9998
            pos = {
                "side": "short", "entry": entry,
                "sl": entry * (1 + SL_PCT),
                "tp": entry * (1 - TP_PCT),
                "entry_idx": i + 1, "entry_time": ts[i + 1],
            }

    max_dd = peak - equity
    wins = sum(1 for t in trades if t["pnl"] > 0)

    print("=" * 80)
    print("Multi-asset / BTC-leads-ETH momentum prototype")
    print(f"BTC {LOOKBACK_H}h breakout → ETH catch-up | SL {SL_PCT*100:.1f}% | TP {TP_PCT*100:.1f}%")
    print(f"Budget ₹{BUDGET_INR:,.0f} | {LEVERAGE:.0f}x | {FEE_BPS}bps fee + {SLIP_BPS}bps slip")
    print("=" * 80)
    print(f"  Trades: {len(trades)}")
    if trades:
        print(f"  Wins: {wins} ({100*wins/len(trades):.1f}%)")
    print(f"  Gross P&L: ₹{equity - BUDGET_INR:+.0f}")
    print(f"  Return: {100*(equity-BUDGET_INR)/BUDGET_INR:+.1f}%")
    print(f"  MaxDD: ₹{max_dd:,.0f} ({100*max_dd/BUDGET_INR:.1f}%)")
    if trades:
        print("\n  First/last 5 trades:")
        for t in trades[:5] + trades[-5:]:
            print(f"    {t['side']:5} entry {t['entry_time']} → exit {t['exit_time']} "
                  f"reason={t['reason']:<6} pnl=₹{t['pnl']:>+8,.0f}")


if __name__ == "__main__":
    run()
