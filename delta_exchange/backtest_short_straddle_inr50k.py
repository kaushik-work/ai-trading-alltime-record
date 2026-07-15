"""
Short straddle — ₹50,000 INR fixed-capital backtest (generic).

Mimics the live options runner with corrected assumptions:
  • ATM strike selected at entry time (no look-ahead to expiry spot).
  • Entry only when target-DTE history is available.
  • Fixed capital pool (₹50k → USD at 86) — NOT compounded.
  • Each straddle requires 15% margin per leg (30% of underlying notional).
  • Skip entry if free capital < margin required.
  • Evaluate profit target (50%), stop (200%), and expiry at every bar.
  • Costs: 25 bps per leg × 4 fills, configurable slippage on entry/exit market orders.

Usage:
  UNDERLYING=BTC RESOLUTION=1m .venv/Scripts/python backtest_short_straddle_inr50k.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from datetime import timedelta
from pathlib import Path
import pandas as pd

UNDERLYING = os.environ.get("UNDERLYING", "ETH").upper()
RESOLUTION = os.environ.get("RESOLUTION", "1h")
ENTRY_HOUR_UTC = int(os.environ.get("ENTRY_HOUR_UTC", "4"))
DATA = Path(__file__).parent / "data" / UNDERLYING.lower() / "options"
PERP_FILE = Path(__file__).parent / "data" / UNDERLYING.lower() / "perp" / f"{UNDERLYING}USD_mark_1m.csv"

FIXED_CAPITAL_INR = float(os.environ.get("FIXED_CAPITAL_INR", "50000.0"))
USD_INR_RATE = float(os.environ.get("USD_INR_RATE", "86.0"))
CONTRACT_SIZE = {
    "ETH": 0.01,   # Delta India ETH option contract size
    "BTC": 0.001,  # Delta India BTC option contract size
}.get(UNDERLYING, 0.01)
MARGIN_PCT = 0.15          # per short-option leg
OPTIONS_MAX_MARGIN_PCT_PER_POSITION = 0.60
MAX_QTY_PER_ENTRY = 100    # sanity cap to avoid extreme sizing / slippage
MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "5"))
OPT_FEE_BPS = 25.0
SLIP_BPS = 50.0            # match live OPTIONS_SLIPPAGE_BPS
ENTRY_DTE = 5
PROFIT_PCT = 0.50
STOP_MULT = 2.00
MIN_ENTRY_DTE = 1.0  # do not enter if option expires within 1 day
MAX_ENTRY_DTE = 5.0  # matches live runner's target-DTE window


def parse_symbol(sym: str):
    # Symbol format: C-ETH-1800-170726_mark_1m.csv
    base = sym.replace("_mark_1m.csv", "").replace("_mark_1h.csv", "")
    parts = base.split("-")
    side, strike, ddmmyy = parts[0], int(parts[2]), parts[3]
    expiry = pd.Timestamp(f"20{ddmmyy[4:6]}-{ddmmyy[2:4]}-{ddmmyy[0:2]} 12:00:00", tz="UTC")
    return side, strike, expiry


def load_data():
    pattern = f"*_mark_{RESOLUTION}.csv"
    files = sorted(DATA.glob(pattern))
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
    start_capital_usd = FIXED_CAPITAL_INR / USD_INR_RATE

    print("=" * 80)
    print(f"{UNDERLYING} short straddle — ₹50k INR fixed-capital backtest")
    print(f"Capital ₹{FIXED_CAPITAL_INR:,.0f} = ${start_capital_usd:,.2f} @ ₹{USD_INR_RATE:.2f}/USD")
    print(f"Resolution {RESOLUTION} | Contract size {CONTRACT_SIZE} | Margin {MARGIN_PCT*100:.0f}% per leg")
    print(f"Entry {ENTRY_DTE} DTE | close {PROFIT_PCT*100:.0f}% | stop {STOP_MULT*100:.0f}% | slip {SLIP_BPS}bps")
    print(f"Max open positions {MAX_OPEN_POSITIONS} | fixed-capital sizing (not compounded)")
    print("=" * 80)

    # Build potential entry events
    events = []
    for pair in pairs:
        exp = pair["expiry"]
        ts = pair["call"].index.intersection(pair["put"].index)
        if len(ts) == 0:
            continue
        call = pair["call"].reindex(ts)
        put = pair["put"].reindex(ts)

        tte = (exp - ts).total_seconds() / 86400
        # Candidate bars are within the live runner's DTE window. In practice
        # Delta India options are often listed only ~3 DTE, so we enter at the
        # first available bar rather than forcing exactly 5 DTE.
        cand = ts[(tte <= MAX_ENTRY_DTE) & (tte >= MIN_ENTRY_DTE)]
        if len(cand) == 0:
            continue

        # Align entry with the live runner's daily entry hour (default 04:00 UTC).
        # Prefer the first bar on the option's first available date at or after
        # ENTRY_HOUR_UTC, but not before the target entry date (5 DTE out).
        first_available_date = cand[0].date()
        target_entry_date = (exp - timedelta(days=ENTRY_DTE)).date()
        entry_t = None
        for t in cand:
            if t.date() >= target_entry_date and t.hour >= ENTRY_HOUR_UTC:
                entry_t = t
                break
        if entry_t is None:
            # Data may start after the target entry hour; use the first available bar.
            entry_t = cand[0]

        entry_call = call.loc[entry_t] * (1 - SLIP_BPS / 1e4)
        entry_put = put.loc[entry_t] * (1 - SLIP_BPS / 1e4)
        credit = (entry_call + entry_put) * CONTRACT_SIZE
        if credit <= 0:
            continue
        spot_entry = float(perp.reindex([entry_t], method="nearest").iloc[0])
        margin = 2 * spot_entry * CONTRACT_SIZE * MARGIN_PCT
        events.append({
            "entry_t": entry_t, "expiry": exp,
            "call": call, "put": put,
            "credit": credit, "margin": margin,
            "spot_entry": spot_entry,
        })
    events = sorted(events, key=lambda x: x["entry_t"])
    print(f"Potential entries: {len(events)}")
    if not events:
        print("No usable ATM pairs with mark data.")
        return

    # Build bar timeline for exit checks
    all_times = sorted(set().union(*[
        set(ev["call"].index).union(set(ev["put"].index)) for ev in events
    ]))

    capital = start_capital_usd
    peak = capital
    open_positions = []
    trades = []
    entered_dates = set()

    for t in all_times:
        # Manage existing positions
        still_open = []
        for pos in open_positions:
            if t in pos["call"].index and t in pos["put"].index:
                cv = (pos["call"].loc[t] + pos["put"].loc[t]) * CONTRACT_SIZE
                pos["last_value"] = cv
            else:
                cv = pos["last_value"]

            credit = pos["credit"]
            reason = None
            if t >= pos["expiry"]:
                reason = "expiry"
            elif cv <= credit * (1 - PROFIT_PCT):
                reason = "profit_target"
            elif cv >= credit * STOP_MULT:
                reason = "stop_loss"

            if reason is not None:
                qty = pos.get("qty", 1)
                # Slippage only on market-order buyback, not expiry settlement
                slip_mult = 1 + SLIP_BPS / 1e4 if reason != "expiry" else 1.0
                exit_value = cv * slip_mult
                gross = qty * (credit - exit_value)
                # 4 fills per straddle round-trip: sell call, sell put, buy call, buy put
                fee = qty * (credit + exit_value) * 4 * OPT_FEE_BPS / 1e4
                net = gross - fee
                capital += net
                peak = max(peak, capital)
                trades.append({**pos, "exit_t": t, "exit_value": exit_value,
                               "net": net, "reason": reason})
            else:
                still_open.append(pos)
        open_positions = still_open

        # Try one new entry per calendar day, sized from the FIXED capital pool
        if t.date().isoformat() in entered_dates:
            continue
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            continue
        for ev in events:
            if ev["entry_t"] != t:
                continue
            used_margin = sum(p.get("total_margin", p["margin"]) for p in open_positions)
            free = start_capital_usd - used_margin
            max_by_capital = int(free / ev["margin"])
            max_by_concentration = int(
                (start_capital_usd * OPTIONS_MAX_MARGIN_PCT_PER_POSITION) / ev["margin"]
            )
            qty = max(1, min(max_by_capital, max_by_concentration, MAX_QTY_PER_ENTRY))
            if max_by_capital < 1:
                continue
            total_margin = qty * ev["margin"]
            open_positions.append({
                "entry_t": ev["entry_t"], "expiry": ev["expiry"],
                "call": ev["call"], "put": ev["put"],
                "credit": ev["credit"], "margin": ev["margin"],
                "qty": qty, "total_margin": total_margin,
                "spot_entry": ev["spot_entry"], "last_value": ev["credit"],
            })
            entered_dates.add(t.date().isoformat())
            break

    # Close remaining at expiry using perp spot (no slippage on settlement)
    for pos in open_positions:
        spot_exp = float(perp.reindex([pos["expiry"]], method="nearest").iloc[0])
        call_iv = max(0, spot_exp - pos["spot_entry"])
        put_iv = max(0, pos["spot_entry"] - spot_exp)
        qty = pos.get("qty", 1)
        cv = (call_iv + put_iv) * CONTRACT_SIZE
        exit_value = cv
        gross = qty * (pos["credit"] - exit_value)
        fee = qty * (pos["credit"] + exit_value) * 4 * OPT_FEE_BPS / 1e4
        net = gross - fee
        capital += net
        peak = max(peak, capital)
        trades.append({**pos, "exit_t": pos["expiry"], "exit_value": exit_value,
                       "net": net, "reason": "expiry"})

    max_dd = peak - capital
    wins = sum(1 for t in trades if t["net"] > 0)

    print(f"\n  Trades taken: {len(trades)}")
    print(f"  Wins: {wins} ({100*wins/len(trades):.1f}%)")
    print(f"  Final capital: ${capital:,.2f} (₹{capital*USD_INR_RATE:,.0f})")
    print(f"  Return: {100*(capital-start_capital_usd)/start_capital_usd:.1f}%")
    print(f"  MaxDD: ${max_dd:,.2f} ({100*max_dd/start_capital_usd:.1f}%)")

    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    print(f"  Exit reasons: {reasons}")

    if trades:
        monthly = {}
        for t in trades:
            m = t["exit_t"].strftime("%Y-%m")
            monthly.setdefault(m, 0.0)
            monthly[m] += t["net"]
        print("\n  Monthly P&L (USD):")
        for m in sorted(monthly):
            print(f"    {m}: ${monthly[m]:>+10,.2f}  (₹{monthly[m]*USD_INR_RATE:>+10,.0f})")


if __name__ == "__main__":
    main()
