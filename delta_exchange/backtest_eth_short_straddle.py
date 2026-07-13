"""
ETH short straddle backtest — sell ATM call + put.

A classic high-probability options strategy: collect premium and profit if ETH
stays near the strike through expiration. The danger is a large move, which can
produce losses many times the credit received (negative skew).

This script uses the 1h mark history of ATM options fetched by
fetch_eth_options_for_parity.py. It enters at a fixed DTE, manages at 50% profit
and 200% stop, and includes option fees + slippage.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data" / "eth" / "options"
PERP_FILE = Path(__file__).parent / "data" / "eth" / "perp" / "ETHUSD_mark_1m.csv"

BUDGET_USD = 1_000.0      # risk budget per straddle
OPT_FEE_BPS = 25.0        # per side per leg
SLIP_BPS = 5.0            # option slippage
ENTRY_DTE = 5             # enter when this many days to expiry
PROFIT_PCT = 0.50         # close at 50% of max profit
STOP_PCT = 2.00           # stop at 200% of credit received


def parse_symbol(sym: str):
    parts = sym.replace("_mark_1h.csv", "").split("-")
    side, asset, strike, ddmmyy = parts[0], parts[1], int(parts[2]), parts[3]
    expiry = pd.Timestamp(f"20{ddmmyy[4:6]}-{ddmmyy[2:4]}-{ddmmyy[0:2]} 12:00:00", tz="UTC")
    return side, strike, expiry


def load_option_pairs():
    files = sorted(DATA.glob("*_mark_1h.csv"))
    by_expiry = {}
    for f in files:
        side, strike, expiry = parse_symbol(f.name)
        df = pd.read_csv(f)
        if df.empty:
            continue
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        s = df.set_index("timestamp")["close"].sort_index()
        by_expiry.setdefault(expiry, {})[side] = s
    pairs = []
    for exp, d in by_expiry.items():
        if "C" in d and "P" in d:
            pairs.append({"expiry": exp, "call": d["C"], "put": d["P"]})
    return pairs


def load_perp():
    df = pd.read_csv(PERP_FILE)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


def run():
    pairs = load_option_pairs()
    perp = load_perp()

    print("=" * 80)
    print("ETH short straddle backtest")
    print(f"Enter at {ENTRY_DTE} DTE | close at {PROFIT_PCT*100:.0f}% profit | stop at {STOP_PCT*100:.0f}% of credit")
    print(f"Fees: {OPT_FEE_BPS} bps/leg/side | slippage: {SLIP_BPS} bps")
    print("=" * 80)
    print(f"Loaded {len(pairs)} ATM pairs")

    trades = []
    for pair in pairs:
        exp = pair["expiry"]
        call = pair["call"]
        put = pair["put"]

        # Align call/put timestamps
        ts = call.index.intersection(put.index)
        if len(ts) == 0:
            continue
        call = call.reindex(ts)
        put = put.reindex(ts)
        tte = (exp - ts).total_seconds() / 86400

        # Find entry: first timestamp where DTE <= ENTRY_DTE
        entry_candidates = ts[tte <= ENTRY_DTE]
        if len(entry_candidates) == 0:
            continue
        entry_t = entry_candidates[0]
        entry_call = call.loc[entry_t] * (1 - SLIP_BPS / 1e4)
        entry_put = put.loc[entry_t] * (1 - SLIP_BPS / 1e4)
        credit = entry_call + entry_put
        if credit <= 0:
            continue

        # Determine contract multiplier / sizing so max risk ≈ BUDGET_USD
        # Max loss is unbounded for a short straddle; use 2× credit as proxy risk
        n_contracts = max(1, int(BUDGET_USD / (credit * STOP_PCT)))
        max_risk = credit * STOP_PCT * n_contracts

        # Per-leg fees: entry + exit = 4 fills total
        fee_cost = credit * n_contracts * 4 * OPT_FEE_BPS / 1e4

        # Spot at entry and expiry
        spot_entry = perp.reindex([entry_t], method="nearest").iloc[0]
        spot_exp = perp.reindex([exp], method="nearest").iloc[0]

        # Intrinsic value at expiry
        call_iv = max(0, spot_exp - spot_entry)  # strike ≈ spot_entry (ATM)
        put_iv = max(0, spot_entry - spot_exp)
        expiry_cost = (call_iv + put_iv) * n_contracts

        # Management simulation using intraday marks: close at 50% credit or 200% loss
        current_value = credit * n_contracts
        exit_reason = "expiry"
        exit_t = exp

        for t in ts[ts > entry_t]:
            cv = (call.loc[t] + put.loc[t]) * n_contracts
            if cv <= credit * n_contracts * (1 - PROFIT_PCT):
                current_value = cv
                exit_t = t
                exit_reason = "profit_target"
                break
            if cv >= credit * n_contracts * (1 + STOP_PCT):
                current_value = cv
                exit_t = t
                exit_reason = "stop_loss"
                break
            current_value = cv

        if exit_reason == "expiry":
            current_value = expiry_cost

        gross = credit * n_contracts - current_value
        net = gross - fee_cost

        trades.append({
            "expiry": exp, "entry_t": entry_t, "exit_t": exit_t,
            "credit": credit, "contracts": n_contracts,
            "spot_entry": spot_entry, "spot_exit": spot_exp,
            "gross": gross, "net": net, "reason": exit_reason,
            "max_risk": max_risk,
        })

    if not trades:
        print("No trades produced.")
        return

    df = pd.DataFrame(trades).sort_values("entry_t")
    df["cum_net"] = df["net"].cumsum()
    max_dd = (df["cum_net"].cummax() - df["cum_net"]).max()
    wins = (df["net"] > 0).sum()

    print(f"\n  Trades: {len(df)}")
    print(f"  Wins: {wins} ({100*wins/len(df):.1f}%)")
    print(f"  Gross P&L: ${df['net'].sum():+.2f}")
    print(f"  MaxDD: ${max_dd:,.2f}")
    print(f"  Avg trade: ${df['net'].mean():+.2f}")

    reasons = df["reason"].value_counts().to_dict()
    print(f"  Exit reasons: {reasons}")

    print("\n  First/last 5 trades:")
    for _, t in df.head(5).iterrows():
        print(f"    {t['entry_t'].date()} → {t['exit_t'].date()} "
              f"credit=${t['credit']:.1f} n={t['contracts']} "
              f"reason={t['reason']:<12} net=${t['net']:>+7.2f}")
    for _, t in df.tail(5).iterrows():
        print(f"    {t['entry_t'].date()} → {t['exit_t'].date()} "
              f"credit=${t['credit']:.1f} n={t['contracts']} "
              f"reason={t['reason']:<12} net=${t['net']:>+7.2f}")


if __name__ == "__main__":
    run()
