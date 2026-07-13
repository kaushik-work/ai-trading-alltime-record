"""
ETH short straddle — realistic fixed-contract sizing.

Sells exactly 1 ATM call + 1 ATM put per expiry. This removes the position-sizing
illusion from backtest_eth_short_straddle_sweep.py and shows the raw per-contract
edge. Capital allocation is inferred from margin requirement (approx. 15% of spot
notional per short option leg on a typical exchange).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import pandas as pd

DATA = Path(__file__).parent / "data" / "eth" / "options"
PERP_FILE = Path(__file__).parent / "data" / "eth" / "perp" / "ETHUSD_mark_1m.csv"

OPT_FEE_BPS = 25.0
SLIP_BPS = float(os.environ.get("SLIP_BPS", "5.0"))
ENTRY_DTE = 5
PROFIT_PCT = 0.50
STOP_PCT = 2.00
CONTRACTS = 1
CONTRACT_SIZE = 0.01   # ETH per option contract on Delta India
MARGIN_PCT = 0.15      # approx margin per short option leg


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


def main():
    pairs, perp = load_data()
    print("=" * 80)
    print("ETH short straddle — 1 contract per expiry (fixed sizing)")
    print(f"Enter at {ENTRY_DTE} DTE | close {PROFIT_PCT*100:.0f}% | stop {STOP_PCT*100:.0f}% of credit")
    print(f"Fees {OPT_FEE_BPS}bps/leg/side | slippage {SLIP_BPS}bps")
    print("=" * 80)

    trades = []
    total_margin_used = 0
    for pair in pairs:
        exp = pair["expiry"]
        ts = pair["call"].index.intersection(pair["put"].index)
        if len(ts) == 0:
            continue
        call = pair["call"].reindex(ts)
        put = pair["put"].reindex(ts)
        tte = (exp - ts).total_seconds() / 86400
        entry_candidates = ts[tte <= ENTRY_DTE]
        if len(entry_candidates) == 0:
            continue
        entry_t = entry_candidates[0]
        entry_call = call.loc[entry_t] * (1 - SLIP_BPS / 1e4)
        entry_put = put.loc[entry_t] * (1 - SLIP_BPS / 1e4)
        credit = (entry_call + entry_put) * CONTRACT_SIZE
        if credit <= 0:
            continue

        spot_entry = perp.reindex([entry_t], method="nearest").iloc[0]
        spot_exp = perp.reindex([exp], method="nearest").iloc[0]
        margin = 2 * spot_entry * CONTRACT_SIZE * MARGIN_PCT * CONTRACTS
        total_margin_used = max(total_margin_used, margin)

        fee_cost = credit * CONTRACTS * 4 * OPT_FEE_BPS / 1e4

        current_value = credit * CONTRACTS
        exit_reason = "expiry"
        exit_t = exp
        for t in ts[ts > entry_t]:
            cv = (call.loc[t] + put.loc[t]) * CONTRACT_SIZE * CONTRACTS
            if cv <= credit * CONTRACTS * (1 - PROFIT_PCT):
                current_value = cv
                exit_t = t
                exit_reason = "profit_target"
                break
            if cv >= credit * CONTRACTS * (1 + STOP_PCT):
                current_value = cv
                exit_t = t
                exit_reason = "stop_loss"
                break
            current_value = cv

        if exit_reason == "expiry":
            call_iv = max(0, spot_exp - spot_entry)
            put_iv = max(0, spot_entry - spot_exp)
            current_value = (call_iv + put_iv) * CONTRACT_SIZE * CONTRACTS

        gross = credit * CONTRACTS - current_value
        net = gross - fee_cost
        trades.append({
            "entry_t": entry_t, "exit_t": exit_t, "expiry": exp,
            "credit": credit, "net": net, "reason": exit_reason,
            "margin": margin, "spot_entry": spot_entry,
        })

    if not trades:
        print("No trades.")
        return

    df = pd.DataFrame(trades).sort_values("entry_t")
    df["cum"] = df["net"].cumsum()
    max_dd = (df["cum"].cummax() - df["cum"]).max()
    wins = (df["net"] > 0).sum()
    total = df["net"].sum()

    print(f"\n  Trades: {len(df)}")
    print(f"  Wins: {wins} ({100*wins/len(df):.1f}%)")
    print(f"  Total P&L per contract: ${total:+.2f}")
    print(f"  Avg per trade: ${df['net'].mean():+.2f}")
    print(f"  MaxDD: ${max_dd:,.2f}")
    print(f"  Avg margin/trade: ${df['margin'].mean():,.2f}")
    print(f"  Return on avg margin: {100*total/df['margin'].mean():.1f}%")

    reasons = df["reason"].value_counts().to_dict()
    print(f"  Exit reasons: {reasons}")

    monthly = df.groupby(df["entry_t"].dt.to_period("M"))["net"].agg(["sum", "count"])
    print("\n  Monthly:")
    for m, row in monthly.iterrows():
        print(f"    {m}: {int(row['count']):2d} trades, ${row['sum']:>+8,.2f}")


if __name__ == "__main__":
    main()
