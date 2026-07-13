"""
ETHUSD perp-only strategy sweep.

Tests several deterministic quant strategies on the same 1-minute ETH data
with identical cost assumptions (5 bps/side fee, 2 bps entry + exit slippage,
₹50k fixed capital, 15× leverage).

Strategies:
  1. price_action_sr  — current live S/R retest (baseline)
  2. rsi2_reversion   — RSI(2) extreme mean reversion with trend filter
  3. donchian_trend   — Donchian channel breakout with ATR-based SL/TP
  4. vwap_reversion   — distance from anchored VWAP standard deviation
  5. atr_squeeze      — low-volatility squeeze + breakout

All strategies use bracket SL/TP and max hold.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List
import numpy as np
import pandas as pd

BUDGET_INR = 50_000.0
LEVERAGE = 15.0
FEE_BPS = 5.0
SLIP_BPS = 2.0


def load_eth_1m():
    base = Path(__file__).parent / "data"
    files = []
    for subdir in ["eth", "july_eth", "fresh_june_eth", "june_eth"]:
        p = base / subdir / "perp" / "ETHUSD_mark_1m.csv"
        if p.exists():
            files.append(p)
    dfs = []
    for f in files:
        df = pd.read_csv(f)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("timestamp").sort_index()
        dfs.append(df)
    df = pd.concat(dfs).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[df.index >= pd.Timestamp("2026-04-01", tz="UTC")]
    return df


def rsi(close, length):
    s = pd.Series(close)
    d = s.diff()
    gain = d.where(d > 0, 0).ewm(alpha=1/length, min_periods=length).mean()
    loss = (-d.where(d < 0, 0)).ewm(alpha=1/length, min_periods=length).mean()
    rs = gain / loss
    return (100 - (100 / (1 + rs))).values


def atr(h, l, c, length):
    tr1 = h[1:] - l[1:]
    tr2 = np.abs(h[1:] - c[:-1])
    tr3 = np.abs(l[1:] - c[:-1])
    tr = np.maximum(np.maximum(tr1, tr2), tr3)
    out = pd.Series(tr).rolling(length).mean().values
    return np.concatenate([[np.nan] * (length + 1), out[1:]])


def vwap_std(h, l, c, v, anchor_bars):
    n = len(c)
    vwap = np.full(n, np.nan)
    std = np.full(n, np.nan)
    for i in range(anchor_bars, n):
        j = i - anchor_bars + 1
        tp = (h[j:i+1] + l[j:i+1] + c[j:i+1]) / 3
        vol = v[j:i+1]
        vwap[i] = (tp * vol).sum() / vol.sum() if vol.sum() > 0 else np.nan
        std[i] = np.sqrt(np.average((tp - vwap[i])**2, weights=vol)) if vol.sum() > 0 else np.nan
    return vwap, std


@dataclass
class Trade:
    side: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    exit: float
    pnl: float
    reason: str


def simulate(df, signal_func, sl_pct=None, tp_pct=None, max_hold_min=240, cooldown_min=60):
    """Run a generic simulation where signal_func returns side or None each minute."""
    n = len(df)
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    ts = df.index

    signals = signal_func(df)
    equity = BUDGET_INR
    peak = equity
    trades = []
    pos = None
    cooldown_until = -1
    block_loss_until = -1

    for i in range(1, n - 1):
        t = ts[i]
        sign = 1 if pos and pos["side"] == "long" else -1 if pos else 0

        # Manage position
        if pos is not None:
            exit_px = None
            reason = None
            if sl_pct is not None and tp_pct is not None:
                if sign > 0:
                    sl = pos["entry"] * (1 - sl_pct)
                    tp = pos["entry"] * (1 + tp_pct)
                    if l[i] <= sl:
                        exit_px = max(l[i], sl)
                        reason = "stop_loss"
                    elif h[i] >= tp:
                        exit_px = min(h[i], tp)
                        reason = "target"
                else:
                    sl = pos["entry"] * (1 + sl_pct)
                    tp = pos["entry"] * (1 - tp_pct)
                    if h[i] >= sl:
                        exit_px = min(h[i], sl)
                        reason = "stop_loss"
                    elif l[i] <= tp:
                        exit_px = max(l[i], tp)
                        reason = "target"

            if reason is None and i - pos["entry_idx"] >= max_hold_min:
                exit_px = c[i]
                reason = "max_hold"

            if reason is not None:
                fill = exit_px * (1 - sign * SLIP_BPS / 1e4)
                gross = sign * (fill - pos["entry"]) / pos["entry"]
                net = gross - 2 * FEE_BPS / 1e4
                pnl = BUDGET_INR * LEVERAGE * net
                equity += pnl
                peak = max(peak, equity)
                if pnl < 0:
                    block_loss_until = max(block_loss_until, i + 180)
                trades.append(Trade(pos["side"], ts[pos["entry_idx"]], t, pos["entry"], fill, pnl, reason))
                pos = None
            continue

        if i < max(cooldown_until, block_loss_until):
            continue

        sig = signals[i]
        if sig == "long":
            pos = {"side": "long", "entry": c[i] * 1.0002, "entry_idx": i}
        elif sig == "short":
            pos = {"side": "short", "entry": c[i] * 0.9998, "entry_idx": i}

    max_dd = peak - equity
    wins = sum(1 for t in trades if t.pnl > 0)
    return {
        "trades": len(trades), "wins": wins,
        "equity": equity, "max_dd": max_dd,
        "return_pct": 100 * (equity - BUDGET_INR) / BUDGET_INR,
        "trade_list": trades,
    }


def sr_retest_signal(df):
    """Current live price-action S/R retest on 1m candles."""
    from strategies.price_action_sr import ETHPriceActionSRSignal
    strat = ETHPriceActionSRSignal()
    n = len(df)
    sigs = [None] * n
    for i in range(1440 + 5, n):
        window = df.iloc[:i]
        pred = strat.predict(window)
        if pred:
            sigs[i] = "long" if pred.direction == "LONG" else "short"
    return sigs


def rsi2_signal(df):
    c = df["close"].values
    r = rsi(c, 2)
    ma = pd.Series(c).rolling(200).mean().values
    n = len(c)
    sigs = [None] * n
    for i in range(200, n):
        if r[i] < 10 and c[i] > ma[i]:
            sigs[i] = "long"
        elif r[i] > 90 and c[i] < ma[i]:
            sigs[i] = "short"
    return sigs


def donchian_signal(df):
    c = df["close"].values
    upper = pd.Series(c).rolling(24).max().shift(1).values
    lower = pd.Series(c).rolling(24).min().shift(1).values
    ma = pd.Series(c).rolling(24).mean().values
    n = len(c)
    sigs = [None] * n
    for i in range(25, n):
        if c[i] > upper[i] and c[i] > ma[i]:
            sigs[i] = "long"
        elif c[i] < lower[i] and c[i] < ma[i]:
            sigs[i] = "short"
    return sigs


def vwap_signal(df):
    c, h, l, v = df["close"].values, df["high"].values, df["low"].values, df["volume"].values
    vwap, std = vwap_std(h, l, c, v, 60)
    n = len(c)
    sigs = [None] * n
    for i in range(60, n):
        if np.isnan(vwap[i]) or np.isnan(std[i]) or std[i] == 0:
            continue
        z = (c[i] - vwap[i]) / std[i]
        if z < -2.0:
            sigs[i] = "long"
        elif z > 2.0:
            sigs[i] = "short"
    return sigs


def squeeze_signal(df):
    c, h, l = df["close"].values, df["high"].values, df["low"].values
    atr14 = atr(h, l, c, 14)
    atr_low = pd.Series(atr14).rolling(60).min().shift(1).values
    upper = pd.Series(c).rolling(20).max().shift(1).values
    lower = pd.Series(c).rolling(20).min().shift(1).values
    ma = pd.Series(c).rolling(20).mean().values
    n = len(c)
    sigs = [None] * n
    for i in range(60, n):
        if np.isnan(atr14[i]) or np.isnan(atr_low[i]):
            continue
        squeeze = atr14[i] <= atr_low[i] * 1.05
        if squeeze and c[i] > upper[i] and c[i] > ma[i]:
            sigs[i] = "long"
        elif squeeze and c[i] < lower[i] and c[i] < ma[i]:
            sigs[i] = "short"
    return sigs


def run_all():
    df = load_eth_1m()
    print(f"Loaded {len(df)} 1m ETHUSD bars from {df.index[0]} to {df.index[-1]}")

    configs = [
        ("rsi2_reversion", rsi2_signal, 0.01, 0.02, 24*60, 120),
        ("donchian_trend", donchian_signal, 0.01, 0.03, 48*60, 240),
        ("vwap_reversion", vwap_signal, 0.01, 0.02, 24*60, 120),
        ("atr_squeeze", squeeze_signal, 0.01, 0.03, 48*60, 240),
    ]

    results = []
    for name, sig_fn, sl, tp, hold, cool in configs:
        print(f"\nRunning {name}...")
        res = simulate(df, sig_fn, sl_pct=sl, tp_pct=tp, max_hold_min=hold, cooldown_min=cool)
        results.append((name, res))
        print(f"  Trades: {res['trades']}  Wins: {res['wins']} "
              f"({100*res['wins']/res['trades']:.1f}% if >0)  "
              f"Return: {res['return_pct']:+.1f}%  MaxDD: {100*res['max_dd']/BUDGET_INR:.1f}%")

    print("\n" + "=" * 80)
    print("Summary")
    print("=" * 80)
    print(f"{'Strategy':<20} {'Trades':>8} {'Win%':>8} {'Return%':>10} {'MaxDD%':>10} {'Ret/DD':>8}")
    for name, res in results:
        trades = res["trades"]
        win_pct = 100 * res["wins"] / trades if trades else 0
        ret = res["return_pct"]
        dd = 100 * res["max_dd"] / BUDGET_INR
        ret_dd = ret / dd if dd > 0 else 0
        print(f"{name:<20} {trades:>8} {win_pct:>7.1f}% {ret:>+9.1f}% {dd:>9.1f}% {ret_dd:>8.2f}")


if __name__ == "__main__":
    run_all()
