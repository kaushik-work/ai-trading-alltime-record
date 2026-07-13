"""
Cross-exchange perp spread prototype.

Trades the ETH (or BTC) perp price difference between Delta India and a second
venue. When the spread widens beyond fees + slippage + a profit margin, buy the
cheap leg and sell the rich leg; exit when the spread compresses.

If the second-venue data file is missing, the script prints the required layout
and exits.

Required data layout:
  delta_exchange/data/<subdir>/perp/ETHUSD_mark_1m.csv          (Delta India)
  delta_exchange/data/<subdir>/perp/ETHUSD_<venue>_mark_1m.csv  (second venue)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"
SYMBOL = "ETHUSD"
SECOND_VENUE = "BINANCE"  # placeholder; change to actual venue tag

# Dials
ENTRY_BPS = 15.0    # spread needed to enter
EXIT_BPS = 3.0      # spread level to exit
MAX_HOLD_MIN = 240
FEE_BPS = 5.0       # per side per leg
SLIP_BPS = 2.0      # per fill


def load_delta_marks():
    paths = []
    for subdir in ["eth", "july_eth"]:
        p = DATA / subdir / "perp" / f"{SYMBOL}_mark_1m.csv"
        if p.exists():
            paths.append(p)
    if not paths:
        return None
    dfs = [pd.read_csv(p) for p in paths]
    df = pd.concat(dfs).sort_values("time")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


def load_venue_marks(venue_tag):
    paths = []
    for subdir in DATA.iterdir():
        if not subdir.is_dir():
            continue
        p = subdir / "perp" / f"{SYMBOL}_{venue_tag}_mark_1m.csv"
        if p.exists():
            paths.append(p)
    if not paths:
        return None
    dfs = [pd.read_csv(p) for p in paths]
    df = pd.concat(dfs).sort_values("time")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


def run():
    print("=" * 80)
    print("Cross-exchange spread prototype")
    print("=" * 80)

    delta = load_delta_marks()
    if delta is None:
        print(f"No Delta India perp data found for {SYMBOL}.")
        return

    second = load_venue_marks(SECOND_VENUE)
    if second is None:
        print(f"No second-venue data found for {SYMBOL}_{SECOND_VENUE}.")
        print("\nRequired file layout:")
        print(f"  data/<subdir>/perp/{SYMBOL}_mark_1m.csv           (Delta India)")
        print(f"  data/<subdir>/perp/{SYMBOL}_{SECOND_VENUE}_mark_1m.csv  (second venue)")
        print("\nAdd the second-venue 1m mark file and re-run.")
        return

    # Align
    joined = pd.concat([delta.rename("delta"), second.rename("second")], axis=1, join="inner").dropna()
    print(f"Aligned {len(joined)} 1m bars.")
    print("Spread backtest not yet implemented — data now present.")


if __name__ == "__main__":
    run()
