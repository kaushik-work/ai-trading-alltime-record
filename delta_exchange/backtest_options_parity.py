"""
ETH options / synthetic parity backtest.

For each expiry, computes synthetic forward from the ATM call and put:
    synthetic_F = C - P + K

If synthetic_F diverges from the perp spot by more than ENTRY_PCT, the strategy
fades the divergence by trading the perp (not the options, because we can only
trade perp in the live bot). The bet is that the deviation mean-reverts.

Costs: perp taker fee + slippage on entry and exit.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import re
import numpy as np
import pandas as pd
from backtest_price_action_sweep import load_perp

BUDGET_INR = 50_000.0
LEVERAGE = 15.0
PERP_FEE_BPS = 5.0
SLIPPAGE_BPS = 2.0
SYMBOL = "ETHUSD"

DATA = Path(__file__).parent / "data" / "eth"
OPT_DIR = DATA / "options"

ENTRY_PCT = 0.006      # enter when deviation > 60 bps
EXIT_PCT = 0.001       # exit when deviation < 10 bps
SL_PCT = 0.01          # 1% stop on perp
MAX_HOLD_H = 48


def parse_symbol(sym: str):
    parts = sym.replace("_mark_1h.csv", "").split("-")
    side, asset, strike, ddmmyy = parts[0], parts[1], int(parts[2]), parts[3]
    expiry = pd.Timestamp(f"20{ddmmyy[4:6]}-{ddmmyy[2:4]}-{ddmmyy[0:2]} 12:00:00", tz="UTC")
    return side, strike, expiry


def load_perp_1h():
    dfs = [load_perp(s, SYMBOL) for s in ["eth", "july_eth"]]
    df = pd.concat(dfs).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[df.index >= pd.Timestamp("2026-04-01", tz="UTC")]
    return df["close"].resample("1h").last().dropna()


def load_option_pairs():
    if not OPT_DIR.exists():
        return []
    files = sorted(OPT_DIR.glob("*_mark_1h.csv"))
    by_expiry = {}
    for f in files:
        side, strike, expiry = parse_symbol(f.name)
        by_expiry.setdefault(expiry, {"call": None, "put": None, "strike": strike})
        s = pd.read_csv(f)
        s["timestamp"] = pd.to_datetime(s["time"], unit="s", utc=True)
        s = s.set_index("timestamp")["close"].sort_index()
        key = "call" if side == "C" else "put"
        by_expiry[expiry][key] = s

    pairs = []
    for exp, d in by_expiry.items():
        if d["call"] is not None and d["put"] is not None:
            pairs.append({
                "expiry": exp,
                "strike": d["strike"],
                "call": d["call"],
                "put": d["put"],
            })
    return pairs


def run():
    perp = load_perp_1h()
    pairs = load_option_pairs()

    print("=" * 80)
    print("ETH options / synthetic parity backtest")
    print(f"Budget ₹{BUDGET_INR:,.0f} | {LEVERAGE:.0f}x | {PERP_FEE_BPS}bps fee + {SLIPPAGE_BPS}bps slip")
    print(f"Entry deviation > {ENTRY_PCT*10000:.0f} bps | exit < {EXIT_PCT*10000:.0f} bps")
    print("=" * 80)

    if not pairs:
        print("No option pairs found. Run fetch_eth_options_for_parity.py first.")
        return

    print(f"Loaded {len(pairs)} ATM option pairs")

    # Build a deviation series: at each perp timestamp, median deviation across pairs
    deviations = []
    for p in pairs:
        syn = (p["call"] - p["put"] + p["strike"]).rename("syn")
        joined = pd.concat([perp.rename("spot"), syn], axis=1, join="inner").dropna()
        if joined.empty:
            continue
        joined["dev"] = (joined["syn"] - joined["spot"]) / joined["spot"]
        joined["expiry"] = p["expiry"]
        deviations.append(joined.reset_index())

    if not deviations:
        print("No overlapping perp+option data.")
        return

    all_dev = pd.concat(deviations)
    # For each timestamp, pick the pair with max |deviation|
    idx = all_dev.groupby("timestamp")["dev"].apply(lambda x: x.abs().idxmax())
    signal_df = all_dev.loc[idx].set_index("timestamp").sort_index()

    print(f"Signal timestamps: {len(signal_df)}")
    print(f"Median |deviation|: {signal_df['dev'].abs().median()*10000:.1f} bps")
    print(f"p99 |deviation|: {signal_df['dev'].abs().quantile(0.99)*10000:.1f} bps")

    # Backtest: fade the deviation via perp
    equity = BUDGET_INR
    peak = equity
    trades = []
    pos = None

    ts = signal_df.index
    dev = signal_df["dev"].values
    spot = signal_df["spot"].values
    n = len(ts)

    # Need perp high/low for SL; resample perp OHLC to 1h
    perp_ohlc = load_perp("eth", SYMBOL).resample("1h").agg({"high": "max", "low": "min"})
    perp_ohlc = perp_ohlc.reindex(ts, method="nearest")
    h = perp_ohlc["high"].values
    l = perp_ohlc["low"].values

    for i in range(n - 1):
        t = ts[i]

        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            reason = None
            exit_px = None

            if sign > 0:
                if l[i] <= pos["sl"]:
                    exit_px = max(l[i], pos["sl"])
                    reason = "sl"
                elif abs(dev[i]) <= EXIT_PCT:
                    exit_px = spot[i] * (1 - sign * SLIPPAGE_BPS / 1e4)
                    reason = "mean_revert"
            else:
                if h[i] >= pos["sl"]:
                    exit_px = min(h[i], pos["sl"])
                    reason = "sl"
                elif abs(dev[i]) <= EXIT_PCT:
                    exit_px = spot[i] * (1 - sign * SLIPPAGE_BPS / 1e4)
                    reason = "mean_revert"

            if reason is None and (t - pos["entry_time"]).total_seconds() / 3600 >= MAX_HOLD_H:
                exit_px = spot[i] * (1 - sign * SLIPPAGE_BPS / 1e4)
                reason = "max_hold"

            if reason is not None:
                fill = exit_px * (1 - sign * SLIPPAGE_BPS / 1e4)
                gross = sign * (fill - pos["entry"]) / pos["entry"]
                net = gross - 2 * PERP_FEE_BPS / 1e4
                pnl = BUDGET_INR * LEVERAGE * net
                equity += pnl
                peak = max(peak, equity)
                trades.append({**pos, "exit": fill, "reason": reason, "pnl": pnl,
                               "exit_time": t, "exit_dev": dev[i]})
                pos = None
            continue

        # Enter when deviation is extreme; fade it
        if dev[i] >= ENTRY_PCT:
            # synthetic > spot → short perp (bet syn falls or spot rises to meet)
            entry = spot[i] * 0.9998
            pos = {
                "side": "short", "entry": entry,
                "sl": entry * (1 + SL_PCT),
                "entry_time": t, "entry_dev": dev[i],
            }
        elif dev[i] <= -ENTRY_PCT:
            entry = spot[i] * 1.0002
            pos = {
                "side": "long", "entry": entry,
                "sl": entry * (1 - SL_PCT),
                "entry_time": t, "entry_dev": dev[i],
            }

    max_dd = peak - equity
    wins = sum(1 for t in trades if t["pnl"] > 0)

    print(f"\n  Trades: {len(trades)}")
    if trades:
        print(f"  Wins: {wins} ({100*wins/len(trades):.1f}%)")
    print(f"  Gross P&L: ₹{equity - BUDGET_INR:+.0f}")
    print(f"  Return: {100*(equity-BUDGET_INR)/BUDGET_INR:+.1f}%")
    print(f"  MaxDD: ₹{max_dd:,.0f} ({100*max_dd/BUDGET_INR:.1f}%)")

    if trades:
        reasons = {}
        for t in trades:
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
        print(f"  Exit reasons: {reasons}")
        print("\n  First/last 5 trades:")
        for t in trades[:5] + trades[-5:]:
            print(f"    {t['side']:5} entry {t['entry_time']} dev={t['entry_dev']*10000:+.0f}bps "
                  f"→ exit {t['exit_time']} dev={t['exit_dev']*10000:+.0f}bps "
                  f"reason={t['reason']:<12} pnl=₹{t['pnl']:>+8,.0f}")


if __name__ == "__main__":
    run()
