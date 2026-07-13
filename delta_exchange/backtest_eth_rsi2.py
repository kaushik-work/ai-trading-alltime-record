"""
ETHUSD RSI(2) extreme mean reversion.

Classic Larry Connors-style short-term reversal on the 1h timeframe.
Buy when RSI(2) < 10 and price is above its 200-period MA (bullish regime filter).
Sell when RSI(2) > 90 and price is below its 200-period MA (bearish regime filter).

Use fixed capital, realistic fees, SL/TP bracket.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import numpy as np
import pandas as pd

BUDGET_INR = 50_000.0
LEVERAGE = 15.0
PERP_FEE_BPS = 5.0
SLIPPAGE_BPS = 2.0

# Dials
RSI_LEN = 2
TREND_MA = 200
RSI_LONG_ENTRY = 10
RSI_SHORT_ENTRY = 90
SL_PCT = 0.01
TP_PCT = 0.02
MAX_HOLD_HOURS = 24
COOLDOWN_HOURS = 2


def load_eth_1h():
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
    df = df.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    df = df[df.index >= pd.Timestamp("2026-04-01", tz="UTC")]
    return df


def rsi(series, length):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).ewm(alpha=1/length, min_periods=length).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/length, min_periods=length).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def run_strategy():
    df = load_eth_1h()
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    ts = df.index
    n = len(df)

    rsi2 = rsi(pd.Series(c), RSI_LEN).values
    ma200 = pd.Series(c).rolling(TREND_MA).mean().values

    equity = BUDGET_INR
    peak = equity
    trades = []
    pos = None
    cooldown_until = 0

    for i in range(max(TREND_MA, RSI_LEN) + 1, n - 1):
        t = ts[i]
        sign = 1 if pos and pos["side"] == "long" else -1 if pos else 0

        if pos is not None:
            # Check intrabar SL/TP using high/low
            exit_px = None
            reason = None
            if sign > 0:
                sl_px = pos["entry"] * (1 - SL_PCT)
                tp_px = pos["entry"] * (1 + TP_PCT)
                if l[i] <= sl_px:
                    exit_px = max(l[i], sl_px)  # assume worst fill at SL
                    reason = "stop_loss"
                elif h[i] >= tp_px:
                    exit_px = min(h[i], tp_px)
                    reason = "target"
            else:
                sl_px = pos["entry"] * (1 + SL_PCT)
                tp_px = pos["entry"] * (1 - TP_PCT)
                if h[i] >= sl_px:
                    exit_px = min(h[i], sl_px)
                    reason = "stop_loss"
                elif l[i] <= tp_px:
                    exit_px = max(l[i], tp_px)
                    reason = "target"

            if reason is None and i - pos["entry_idx"] >= MAX_HOLD_HOURS:
                exit_px = c[i]
                reason = "max_hold"

            if reason is not None:
                fill = exit_px * (1 - sign * SLIPPAGE_BPS / 1e4)
                gross = sign * (fill - pos["entry"]) / pos["entry"]
                net = gross - 2 * PERP_FEE_BPS / 1e4
                pnl = BUDGET_INR * LEVERAGE * net
                equity += pnl
                peak = max(peak, equity)
                if pnl < 0:
                    cooldown_until = i + COOLDOWN_HOURS
                trades.append({
                    "side": pos["side"], "entry": pos["entry"], "exit": fill,
                    "reason": reason, "pnl": pnl, "net_pct": net,
                    "entry_time": ts[pos["entry_idx"]], "exit_time": t,
                    "entry_rsi": pos["entry_rsi"],
                })
                pos = None
            continue

        if i < cooldown_until:
            continue

        if rsi2[i] < RSI_LONG_ENTRY and c[i] > ma200[i]:
            entry = c[i] * 1.0002
            pos = {"side": "long", "entry": entry, "entry_idx": i, "entry_rsi": rsi2[i]}
        elif rsi2[i] > RSI_SHORT_ENTRY and c[i] < ma200[i]:
            entry = c[i] * 0.9998
            pos = {"side": "short", "entry": entry, "entry_idx": i, "entry_rsi": rsi2[i]}

    max_dd = peak - equity
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "trades": len(trades), "wins": wins, "equity": equity,
        "max_dd": max_dd, "return_pct": 100 * (equity - BUDGET_INR) / BUDGET_INR,
        "trade_list": trades,
    }


def main():
    res = run_strategy()
    print("=" * 80)
    print("ETHUSD RSI(2) extreme mean reversion")
    print(f"Budget: ₹{BUDGET_INR:,.0f}, Leverage: {LEVERAGE:.0f}x, Fees: {PERP_FEE_BPS}bps/side")
    print(f"RSI(2) < {RSI_LONG_ENTRY} long, > {RSI_SHORT_ENTRY} short, trend MA {TREND_MA}")
    print(f"SL {SL_PCT*100:.1f}% / TP {TP_PCT*100:.1f}%, Max hold {MAX_HOLD_HOURS}h")
    print("=" * 80)
    print(f"  Trades: {res['trades']}")
    if res["trades"]:
        print(f"  Wins: {res['wins']} ({100*res['wins']/res['trades']:.1f}%)")
    print(f"  Final equity: ₹{res['equity']:,.0f}")
    print(f"  Return: {res['return_pct']:+.1f}%")
    print(f"  MaxDD: ₹{res['max_dd']:,.0f} ({100*res['max_dd']/BUDGET_INR:.1f}%)")
    if res["trade_list"]:
        print(f"\n  First/last 5 trades:")
        for t in res["trade_list"][:5] + res["trade_list"][-5:]:
            print(f"    {t['side']:5} entry {t['entry_time']} rsi={t['entry_rsi']:5.1f} "
                  f"→ exit {t['exit_time']} reason={t['reason']:<12} pnl=₹{t['pnl']:>+8,.0f}")


if __name__ == "__main__":
    main()
