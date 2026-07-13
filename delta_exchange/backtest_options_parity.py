"""
Options / synthetic parity prototype.

This script is designed to run when local option-chain mark files are available.
If they are missing, it prints the required data layout and exits.

Two tradeable ideas once data exists:
  1. Perp vs synthetic forward: if |C − P + K − spot| > threshold, trade perp
     against the mispricing (direction = fade the deviation).
  2. Straddle timing: when the magnitude of parity deviation is large across
     multiple strikes, buy ATM call + put (long straddle) expecting a move.

Required data layout:
  delta_exchange/data/<subdir>/options/<SYMBOL>-<CP>-<STRIKE>-<DDMMYY>_mark_1h.csv
  delta_exchange/data/<subdir>/perp/<SYMBOL>_mark_1m.csv
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import re
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"
SYMBOL = "ETHUSD"
UNDERLYING = "ETH"

# Trade dials
ENTRY_PCT = 0.006          # 60 bps deviation to enter perp trade
STRADDLE_GATE = 0.008      # 80 bps median deviation to buy straddle
MAX_HOLD_H = 48
OPT_FEE_BPS = 25.0         # per option leg
PERP_FEE_BPS = 5.0
SLIP_BPS = 2.0


def find_options():
    """Return list of option mark CSV paths for the underlying, if any."""
    paths = []
    for subdir in DATA.iterdir():
        opt_dir = subdir / "options"
        if opt_dir.is_dir():
            pattern = re.compile(rf"^[CP]-{UNDERLYING}-\d+-\d{{6}}_mark_1h\.csv$")
            paths.extend([p for p in opt_dir.iterdir() if pattern.match(p.name)])
    return paths


def parse_symbol(sym: str):
    parts = sym.replace("_mark_1h.csv", "").split("-")
    side, asset, strike, ddmmyy = parts[0], parts[1], int(parts[2]), parts[3]
    expiry = pd.Timestamp(f"20{ddmmyy[4:6]}-{ddmmyy[2:4]}-{ddmmyy[0:2]} 12:00:00", tz="UTC")
    return side, asset, strike, expiry


def load_perp_marks():
    perp_paths = []
    for subdir in ["eth", "july_eth"]:
        p = DATA / subdir / "perp" / f"{SYMBOL}_mark_1m.csv"
        if p.exists():
            perp_paths.append(p)
    if not perp_paths:
        raise FileNotFoundError(f"No perp data for {SYMBOL}")
    dfs = [pd.read_csv(p) for p in perp_paths]
    df = pd.concat(dfs).sort_values("time")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


def run():
    option_paths = find_options()
    if not option_paths:
        print("=" * 80)
        print("Options / synthetic parity prototype")
        print("=" * 80)
        print(f"No option mark files found for {UNDERLYING}.")
        print("\nRequired files (one per call/put strike/expiry):")
        print(f"  data/<subdir>/options/C-{UNDERLYING}-<STRIKE>-<DDMMYY>_mark_1h.csv")
        print(f"  data/<subdir>/options/P-{UNDERLYING}-<STRIKE>-<DDMMYY>_mark_1h.csv")
        print("\nPlus perp marks:")
        print(f"  data/<subdir>/perp/{SYMBOL}_mark_1m.csv")
        print("\nInstall the option-chain data, then re-run this script.")
        return

    perp = load_perp_marks()
    print(f"Loaded {len(perp)} perp marks and {len(option_paths)} option series.")
    print("Parity backtest not yet implemented — data now present.")


if __name__ == "__main__":
    run()
