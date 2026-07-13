"""
Higher-frequency microstructure prototype — ETHUSD 1m VWAP mean reversion.

Uses 1-minute candles as a proxy for tick/order-book data. The strategy enters
when price deviates from an anchored VWAP by k standard deviations and the next
candle shows rejection. Tight SL/TP and very short max hold keep per-trade risk
small.

Dials:
  anchor_bars   : VWAP lookback in 1m bars
  entry_z       : required deviation in VWAP standard deviations
  sl_pct        : stop loss as fraction of entry price
  tp_pct        : take profit as fraction of entry price
  max_hold_min  : force exit after N minutes
  cooldown_min  : minimum minutes between trades
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
SYMBOL = "ETHUSD"

ANCHOR_BARS = 30
ENTRY_Z = 1.5
SL_PCT = 0.003
TP_PCT = 0.006
MAX_HOLD_MIN = 15
COOLDOWN_MIN = 5


def load_eth():
    dfs = []
    for subdir in ["eth", "july_eth"]:
        dfs.append(load_perp(subdir, SYMBOL))
    df = pd.concat(dfs).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[df.index >= pd.Timestamp("2026-04-01", tz="UTC")]
    return df


def vwap_and_std(h, l, c, v, anchor_bars):
    n = len(c)
    vwap = np.full(n, np.nan)
    std = np.full(n, np.nan)
    for i in range(anchor_bars, n):
        j = i - anchor_bars + 1
        tp = (h[j:i+1] + l[j:i+1] + c[j:i+1]) / 3
        vol = v[j:i+1]
        if vol.sum() <= 0:
            continue
        vwap[i] = (tp * vol).sum() / vol.sum()
        std[i] = np.sqrt(np.average((tp - vwap[i])**2, weights=vol))
    return vwap, std


def run():
    df = load_eth()
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    o = df["open"].values
    v = df["real_volume"].fillna(0).values
    ts = df.index
    n = len(df)

    vwap, std = vwap_and_std(h, l, c, v, ANCHOR_BARS)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(std > 0, (c - vwap) / std, 0)

    equity = BUDGET_INR
    peak = equity
    trades = []
    pos = None
    cooldown_until = -1

    for i in range(ANCHOR_BARS + 1, n - 1):
        if np.isnan(z[i]):
            continue

        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            reason = None
            exit_px = None

            # intrabar SL/TP
            if sign > 0:
                if l[i] <= pos["sl"]:
                    exit_px = max(l[i], pos["sl"])
                    reason = "sl"
                elif h[i] >= pos["tp"]:
                    exit_px = min(h[i], pos["tp"])
                    reason = "tp"
            else:
                if h[i] >= pos["sl"]:
                    exit_px = min(h[i], pos["sl"])
                    reason = "sl"
                elif l[i] <= pos["tp"]:
                    exit_px = max(l[i], pos["tp"])
                    reason = "tp"

            if reason is None and i - pos["entry_idx"] >= MAX_HOLD_MIN:
                exit_px = c[i]
                reason = "hold"

            if reason is not None:
                fill = exit_px * (1 - sign * SLIP_BPS / 1e4)
                gross = sign * (fill - pos["entry"]) / pos["entry"]
                net = gross - 2 * FEE_BPS / 1e4
                pnl = BUDGET_INR * LEVERAGE * net
                equity += pnl
                peak = max(peak, equity)
                trades.append({
                    "side": pos["side"], "entry": pos["entry"], "exit": fill,
                    "reason": reason, "pnl": pnl,
                    "entry_time": ts[pos["entry_idx"]], "exit_time": ts[i],
                    "entry_z": pos["entry_z"],
                })
                pos = None
                cooldown_until = i + COOLDOWN_MIN
            continue

        if i < cooldown_until:
            continue

        # Require deviation; enter at current close
        if z[i] < -ENTRY_Z and c[i] > o[i]:
            entry = c[i] * 1.0002
            pos = {
                "side": "long", "entry": entry,
                "sl": entry * (1 - SL_PCT),
                "tp": entry * (1 + TP_PCT),
                "entry_idx": i, "entry_z": z[i],
            }
        elif z[i] > ENTRY_Z and c[i] < o[i]:
            entry = c[i] * 0.9998
            pos = {
                "side": "short", "entry": entry,
                "sl": entry * (1 + SL_PCT),
                "tp": entry * (1 - TP_PCT),
                "entry_idx": i, "entry_z": z[i],
            }

    max_dd = peak - equity
    wins = sum(1 for t in trades if t["pnl"] > 0)

    print("=" * 80)
    print("HF microstructure prototype — VWAP mean reversion")
    print(f"Anchor {ANCHOR_BARS}m | entry |z|>{ENTRY_Z} | SL {SL_PCT*100:.1f}% | TP {TP_PCT*100:.1f}% | hold≤{MAX_HOLD_MIN}m")
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
            print(f"    {t['side']:5} entry {t['entry_time']} z={t['entry_z']:+.2f} "
                  f"→ exit {t['exit_time']} reason={t['reason']:<6} pnl=₹{t['pnl']:>+7,.0f}")


if __name__ == "__main__":
    run()
