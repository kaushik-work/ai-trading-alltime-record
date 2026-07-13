"""
ETHUSD 1h trend breakout (Donchian-style) with trend filter.

Enter long when price breaks above the highest close of the last N hours
while above the 24h MA. Enter short on break below lowest close while below MA.
Use ATR-based SL and R:R target.
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
BREAKOUT_WINDOW = 24
TREND_MA = 24
ATR_LEN = 14
SL_ATR_MULT = 1.0
TP_ATR_MULT = 3.0
MAX_HOLD_HOURS = 48
COOLDOWN_HOURS = 4


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


def atr(high, low, close, length):
    tr1 = high[1:] - low[1:]
    tr2 = np.abs(high[1:] - close[:-1])
    tr3 = np.abs(low[1:] - close[:-1])
    tr = np.maximum(np.maximum(tr1, tr2), tr3)
    atr = pd.Series(tr).rolling(length).mean().values
    return np.concatenate([[np.nan] * (length), atr])


def run_strategy():
    df = load_eth_1h()
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    ts = df.index
    n = len(df)

    ma = pd.Series(c).rolling(TREND_MA).mean().values
    atr_vals = atr(h, l, c, ATR_LEN)
    upper = pd.Series(c).rolling(BREAKOUT_WINDOW).max().shift(1).values
    lower = pd.Series(c).rolling(BREAKOUT_WINDOW).min().shift(1).values

    equity = BUDGET_INR
    peak = equity
    trades = []
    pos = None
    cooldown_until = 0

    for i in range(max(TREND_MA, BREAKOUT_WINDOW, ATR_LEN) + 2, n - 1):
        t = ts[i]
        sign = 1 if pos and pos["side"] == "long" else -1 if pos else 0

        if pos is not None:
            exit_px = None
            reason = None
            sl_pct = pos["sl_pct"]
            tp_pct = pos["tp_pct"]

            if sign > 0:
                sl_px = pos["entry"] * (1 - sl_pct)
                tp_px = pos["entry"] * (1 + tp_pct)
                if l[i] <= sl_px:
                    exit_px = max(l[i], sl_px)
                    reason = "stop_loss"
                elif h[i] >= tp_px:
                    exit_px = min(h[i], tp_px)
                    reason = "target"
            else:
                sl_px = pos["entry"] * (1 + sl_pct)
                tp_px = pos["entry"] * (1 - tp_pct)
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
                })
                pos = None
            continue

        if i < cooldown_until or np.isnan(atr_vals[i]):
            continue

        # Long breakout
        if c[i] > upper[i] and c[i] > ma[i]:
            entry = c[i] * 1.0002
            sl_pct = SL_ATR_MULT * atr_vals[i] / c[i]
            tp_pct = TP_ATR_MULT * atr_vals[i] / c[i]
            pos = {"side": "long", "entry": entry, "entry_idx": i,
                   "sl_pct": sl_pct, "tp_pct": tp_pct}
        # Short breakout
        elif c[i] < lower[i] and c[i] < ma[i]:
            entry = c[i] * 0.9998
            sl_pct = SL_ATR_MULT * atr_vals[i] / c[i]
            tp_pct = TP_ATR_MULT * atr_vals[i] / c[i]
            pos = {"side": "short", "entry": entry, "entry_idx": i,
                   "sl_pct": sl_pct, "tp_pct": tp_pct}

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
    print("ETHUSD 1h trend breakout")
    print(f"Budget: ₹{BUDGET_INR:,.0f}, Leverage: {LEVERAGE:.0f}x, Fees: {PERP_FEE_BPS}bps/side")
    print(f"Breakout window: {BREAKOUT_WINDOW}h, trend MA: {TREND_MA}h, ATR({ATR_LEN})")
    print(f"SL {SL_ATR_MULT}×ATR, TP {TP_ATR_MULT}×ATR, Max hold {MAX_HOLD_HOURS}h")
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
            print(f"    {t['side']:5} entry {t['entry_time']} → exit {t['exit_time']} "
                  f"reason={t['reason']:<12} pnl=₹{t['pnl']:>+8,.0f}")


if __name__ == "__main__":
    main()
