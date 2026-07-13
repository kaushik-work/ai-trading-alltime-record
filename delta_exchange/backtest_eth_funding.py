"""
ETHUSD funding-rate z-score mean reversion.

Logic:
  - Positive funding = longs pay shorts → market is over-leveraged long → short bias.
  - Negative funding = shorts pay longs → market is over-leveraged short → long bias.
  - Compute z-score of funding rate over a rolling window.
  - Enter when |z-score| exceeds threshold, in the direction against the crowd.
  - Hold until funding mean-reverts or max hold expires.

Uses 1h funding data from Delta.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import numpy as np
import pandas as pd

BUDGET_INR = 50_000.0
LEVERAGE = 15.0
PERP_FEE_BPS = 5.0
SLIPPAGE_BPS = 2.0

# Strategy dials
Z_SCORE_WINDOW = 72       # 72 hours lookback
ENTRY_Z = 1.5             # z-score threshold to enter
EXIT_Z = 0.0              # z-score level to exit (mean reversion)
SL_PCT = 0.01             # 1% stop loss
TP_PCT = 0.02             # 2% take profit (1:2 R:R)
MAX_HOLD_HOURS = 24


def load_funding_and_price():
    base = Path(__file__).parent / "data"
    # Try multiple subdirs for ETH perp + funding
    perp_files = []
    fund_files = []
    for subdir in ["eth", "july_eth", "fresh_june_eth", "june_eth"]:
        p = base / subdir / "perp"
        if (p / "ETHUSD_mark_1m.csv").exists():
            perp_files.append(p / "ETHUSD_mark_1m.csv")
        if (p / "ETHUSD_funding_1h.csv").exists():
            fund_files.append(p / "ETHUSD_funding_1h.csv")

    # Load and concat perp
    perp_dfs = []
    for f in perp_files:
        df = pd.read_csv(f)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("timestamp").sort_index()
        perp_dfs.append(df[["close"]])
    perp = pd.concat(perp_dfs).sort_index()
    perp = perp[~perp.index.duplicated(keep="first")]
    perp = perp.resample("1h").last().dropna()

    # Load and concat funding
    fund_dfs = []
    for f in fund_files:
        df = pd.read_csv(f)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("timestamp").sort_index()
        fund_dfs.append(df[["close"]].rename(columns={"close": "funding_rate"}))
    funding = pd.concat(fund_dfs).sort_index()
    funding = funding[~funding.index.duplicated(keep="first")]
    funding = funding.resample("1h").last().dropna()

    # Merge
    df = perp.join(funding, how="inner")
    df = df[df.index >= pd.Timestamp("2026-04-01", tz="UTC")]
    df.columns = ["close", "funding"]
    return df


def run_strategy():
    df = load_funding_and_price()
    c = df["close"].values
    f = df["funding"].values
    ts = df.index
    n = len(c)

    # Compute rolling z-score
    z = np.zeros(n)
    for i in range(Z_SCORE_WINDOW, n):
        window = f[i - Z_SCORE_WINDOW:i]
        mu = window.mean()
        sigma = window.std()
        z[i] = (f[i] - mu) / sigma if sigma > 0 else 0

    equity = BUDGET_INR
    peak = equity
    trades = []
    pos = None

    for i in range(Z_SCORE_WINDOW, n - 1):
        t = ts[i]

        # Exit logic
        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            exit_px = c[i]
            reason = None

            # Exit if funding mean-reverts
            if sign > 0 and z[i] <= EXIT_Z:
                reason = "funding_revert"
            elif sign < 0 and z[i] >= -EXIT_Z:
                reason = "funding_revert"
            elif i - pos["entry_idx"] >= MAX_HOLD_HOURS:
                reason = "max_hold"

            # Stop loss / take profit on unrealised
            unreal_pct = sign * (c[i] - pos["entry"]) / pos["entry"]
            if unreal_pct <= -SL_PCT:
                reason = "stop_loss"
            elif unreal_pct >= TP_PCT:
                reason = "target"

            if reason is not None:
                fill = exit_px * (1 - sign * SLIPPAGE_BPS / 1e4)
                gross = sign * (fill - pos["entry"]) / pos["entry"]
                net = gross - 2 * PERP_FEE_BPS / 1e4
                pnl = BUDGET_INR * LEVERAGE * net
                equity += pnl
                peak = max(peak, equity)
                trades.append({
                    "side": pos["side"], "entry": pos["entry"], "exit": fill,
                    "reason": reason, "pnl": pnl, "net_pct": net,
                    "entry_time": ts[pos["entry_idx"]], "exit_time": t,
                    "entry_z": pos["entry_z"], "exit_z": z[i],
                })
                pos = None
            continue

        # Entry logic
        if z[i] >= ENTRY_Z:
            # Over-leveraged long → short
            entry = c[i] * 0.9998
            pos = {
                "side": "short", "entry": entry,
                "entry_idx": i, "entry_z": z[i],
            }
        elif z[i] <= -ENTRY_Z:
            # Over-leveraged short → long
            entry = c[i] * 1.0002
            pos = {
                "side": "long", "entry": entry,
                "entry_idx": i, "entry_z": z[i],
            }

    max_dd = peak - equity
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "trades": len(trades),
        "wins": wins,
        "equity": equity,
        "max_dd": max_dd,
        "return_pct": 100 * (equity - BUDGET_INR) / BUDGET_INR,
        "trade_list": trades,
    }


def main():
    res = run_strategy()
    print("=" * 80)
    print("ETHUSD funding z-score mean reversion")
    print(f"Budget: ₹{BUDGET_INR:,.0f}, Leverage: {LEVERAGE:.0f}x, Fees: {PERP_FEE_BPS}bps/side")
    print(f"Z-score window: {Z_SCORE_WINDOW}h, Entry |z| > {ENTRY_Z}, Exit z → {EXIT_Z}")
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
            print(f"    {t['side']:5} entry {t['entry_time']} z={t['entry_z']:+.2f} "
                  f"→ exit {t['exit_time']} z={t['exit_z']:+.2f} "
                  f"reason={t['reason']:<15} pnl=₹{t['pnl']:>+8,.0f}")


if __name__ == "__main__":
    main()
