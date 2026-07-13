"""
ETH short straddle — portfolio-level simulation with overlapping positions.

Tracks a fixed capital pool and only enters a new straddle if enough free margin
is available. Each straddle ties up margin for its life (until profit target,
stop, or expiry). This models the realistic constraint that daily expiries
overlap.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import pandas as pd

DATA = Path(__file__).parent / "data" / "eth" / "options"
PERP_FILE = Path(__file__).parent / "data" / "eth" / "perp" / "ETHUSD_mark_1m.csv"

START_CAPITAL = float(os.environ.get("START_CAPITAL", "5000.0"))
MARGIN_PCT = 0.15
OPT_FEE_BPS = 25.0
SLIP_BPS = 100.0    # 1% option slippage (conservative)
ENTRY_DTE = 5
PROFIT_PCT = 0.50
STOP_PCT = 2.00


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
    print("ETH short straddle — portfolio simulation")
    print(f"Start capital ${START_CAPITAL:,.0f} | margin {MARGIN_PCT*100:.0f}% per leg")
    print(f"Entry {ENTRY_DTE} DTE | close {PROFIT_PCT*100:.0f}% | stop {STOP_PCT*100:.0f}% | slip {SLIP_BPS}bps")
    print("=" * 80)

    # Build chronological list of potential entries
    events = []
    for pair in pairs:
        exp = pair["expiry"]
        ts = pair["call"].index.intersection(pair["put"].index)
        if len(ts) == 0:
            continue
        call = pair["call"].reindex(ts)
        put = pair["put"].reindex(ts)
        tte = (exp - ts).total_seconds() / 86400
        cand = ts[tte <= ENTRY_DTE]
        if len(cand) == 0:
            continue
        entry_t = cand[0]
        entry_call = call.loc[entry_t] * (1 - SLIP_BPS / 1e4)
        entry_put = put.loc[entry_t] * (1 - SLIP_BPS / 1e4)
        credit = entry_call + entry_put
        if credit <= 0:
            continue
        spot_entry = perp.reindex([entry_t], method="nearest").iloc[0]
        margin = 2 * spot_entry * MARGIN_PCT
        events.append({
            "entry_t": entry_t, "expiry": exp, "call": call, "put": put,
            "credit": credit, "margin": margin, "spot_entry": spot_entry,
        })

    events = sorted(events, key=lambda x: x["entry_t"])
    print(f"Potential entries: {len(events)}")

    capital = START_CAPITAL
    peak = capital
    open_positions = []
    trades = []

    for ev in events:
        t = ev["entry_t"]
        # Mark existing positions to market and check exits
        still_open = []
        for pos in open_positions:
            if t in pos["call"].index and t in pos["put"].index:
                cv = pos["call"].loc[t] + pos["put"].loc[t]
            else:
                cv = pos["last_value"]
            pos["last_value"] = cv
            credit = pos["credit"]
            reason = None
            if cv <= credit * (1 - PROFIT_PCT):
                reason = "profit_target"
            elif cv >= credit * (1 + STOP_PCT):
                reason = "stop_loss"
            elif t >= pos["expiry"]:
                reason = "expiry"

            if reason is not None:
                exit_value = cv
                # apply exit slippage
                exit_value *= (1 + SLIP_BPS / 1e4)
                gross = credit - exit_value
                fee = credit * 4 * OPT_FEE_BPS / 1e4
                net = gross - fee
                capital += net
                peak = max(peak, capital)
                trades.append({**pos, "exit_t": t, "exit_value": exit_value,
                               "net": net, "reason": reason})
            else:
                still_open.append(pos)
        open_positions = still_open

        # Enter new position if margin allows
        free_capital = capital - sum(p["margin"] for p in open_positions)
        if free_capital >= ev["margin"]:
            open_positions.append({
                "entry_t": ev["entry_t"], "expiry": ev["expiry"],
                "call": ev["call"], "put": ev["put"],
                "credit": ev["credit"], "margin": ev["margin"],
                "spot_entry": ev["spot_entry"], "last_value": ev["credit"],
            })

    # Close remaining at expiry using perp spot
    for pos in open_positions:
        spot_exp = perp.reindex([pos["expiry"]], method="nearest").iloc[0]
        call_iv = max(0, spot_exp - pos["spot_entry"])
        put_iv = max(0, pos["spot_entry"] - spot_exp)
        cv = call_iv + put_iv
        gross = pos["credit"] - cv
        fee = pos["credit"] * 4 * OPT_FEE_BPS / 1e4
        net = gross - fee
        capital += net
        peak = max(peak, capital)
        trades.append({**pos, "exit_t": pos["expiry"], "exit_value": cv,
                       "net": net, "reason": "expiry"})

    max_dd = peak - capital
    wins = sum(1 for t in trades if t["net"] > 0)

    print(f"\n  Trades taken: {len(trades)}")
    print(f"  Wins: {wins} ({100*wins/len(trades):.1f}%)")
    print(f"  Final capital: ${capital:,.2f}")
    print(f"  Return: {100*(capital-START_CAPITAL)/START_CAPITAL:.1f}%")
    print(f"  MaxDD: ${max_dd:,.2f} ({100*max_dd/START_CAPITAL:.1f}%)")

    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    print(f"  Exit reasons: {reasons}")

    # Monthly P&L by exit date
    if trades:
        monthly = {}
        for t in trades:
            m = t["exit_t"].strftime("%Y-%m")
            monthly.setdefault(m, 0.0)
            monthly[m] += t["net"]
        print("\n  Monthly P&L:")
        for m in sorted(monthly):
            print(f"    {m}: ${monthly[m]:>+10,.2f}")


if __name__ == "__main__":
    main()
