"""
ETH short straddle parameter sweep.

Tests combinations of:
  - entry DTE (1, 3, 5, 7)
  - profit target (25%, 50%, 75%)
  - stop loss (100%, 200%, 300% of credit)

Outputs a ranked table by profit / drawdown.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
from itertools import product
import pandas as pd

DATA = Path(__file__).parent / "data" / "eth" / "options"
PERP_FILE = Path(__file__).parent / "data" / "eth" / "perp" / "ETHUSD_mark_1m.csv"

BUDGET_USD = 1_000.0
CONTRACT_SIZE = 0.01   # ETH per option contract on Delta India
OPT_FEE_BPS = 25.0
SLIP_BPS = 5.0


def parse_symbol(sym: str):
    parts = sym.replace("_mark_1h.csv", "").split("-")
    side, strike, ddmmyy = parts[0], int(parts[2]), parts[3]
    expiry = pd.Timestamp(f"20{ddmmyy[4:6]}-{ddmmyy[2:4]}-{ddmmyy[0:2]} 12:00:00", tz="UTC")
    return side, strike, expiry


def load_data():
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

    perp = pd.read_csv(PERP_FILE)
    perp["timestamp"] = pd.to_datetime(perp["time"], unit="s", utc=True)
    perp = perp.set_index("timestamp")["close"].sort_index()
    return pairs, perp


def run_config(pairs, perp, entry_dte, profit_pct, stop_pct):
    trades = []
    for pair in pairs:
        exp = pair["expiry"]
        call = pair["call"]
        put = pair["put"]
        ts = call.index.intersection(put.index)
        if len(ts) == 0:
            continue
        call = call.reindex(ts)
        put = put.reindex(ts)
        tte = (exp - ts).total_seconds() / 86400

        entry_candidates = ts[tte <= entry_dte]
        if len(entry_candidates) == 0:
            continue
        entry_t = entry_candidates[0]
        entry_call = call.loc[entry_t] * (1 - SLIP_BPS / 1e4)
        entry_put = put.loc[entry_t] * (1 - SLIP_BPS / 1e4)
        credit = (entry_call + entry_put) * CONTRACT_SIZE
        if credit <= 0:
            continue

        n_contracts = max(1, int(BUDGET_USD / (credit * stop_pct)))
        fee_cost = credit * n_contracts * 4 * OPT_FEE_BPS / 1e4

        spot_entry = perp.reindex([entry_t], method="nearest").iloc[0]
        spot_exp = perp.reindex([exp], method="nearest").iloc[0]

        current_value = credit * n_contracts
        exit_reason = "expiry"
        exit_t = exp

        for t in ts[ts > entry_t]:
            cv = (call.loc[t] + put.loc[t]) * CONTRACT_SIZE * n_contracts
            if cv <= credit * n_contracts * (1 - profit_pct):
                current_value = cv
                exit_t = t
                exit_reason = "profit_target"
                break
            if cv >= credit * n_contracts * (1 + stop_pct):
                current_value = cv
                exit_t = t
                exit_reason = "stop_loss"
                break
            current_value = cv

        if exit_reason == "expiry":
            call_iv = max(0, spot_exp - spot_entry)
            put_iv = max(0, spot_entry - spot_exp)
            current_value = (call_iv + put_iv) * CONTRACT_SIZE * n_contracts

        gross = credit * n_contracts - current_value
        net = gross - fee_cost
        trades.append({"net": net, "reason": exit_reason})

    if not trades:
        return None
    total = sum(t["net"] for t in trades)
    cum = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cum += t["net"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    wins = sum(1 for t in trades if t["net"] > 0)
    return {
        "trades": len(trades), "wins": wins,
        "win_pct": 100 * wins / len(trades),
        "total": total, "max_dd": max_dd,
        "pr": total / max(max_dd, 1),
    }


def main():
    pairs, perp = load_data()
    print(f"Loaded {len(pairs)} ATM pairs")

    results = []
    configs = list(product([1, 3, 5, 7], [0.25, 0.50, 0.75], [1.0, 2.0, 3.0]))
    print(f"Running {len(configs)} configurations...")

    for entry_dte, profit_pct, stop_pct in configs:
        res = run_config(pairs, perp, entry_dte, profit_pct, stop_pct)
        if res:
            results.append({
                "DTE": entry_dte, "profit": profit_pct,
                "stop": stop_pct, **res,
            })

    df = pd.DataFrame(results)
    df = df.sort_values("pr", ascending=False)
    print("\n" + "=" * 100)
    print("Top by Profit / MaxDD")
    print("=" * 100)
    print(df.to_string(index=False))

    print("\n" + "=" * 100)
    print("Top by absolute profit")
    print("=" * 100)
    print(df.sort_values("total", ascending=False).to_string(index=False))


if __name__ == "__main__":
    main()
