"""
Fetch XAUTUSD 1-minute candles from Delta India API and save to CSV.
XAUTUSD launched 2026-04-17, so history is limited.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone
from pathlib import Path
import requests
import pandas as pd

API_URL = "https://api.india.delta.exchange/v2/history/candles"
SYMBOL = "XAUTUSD"
RESOLUTION = "1m"
OUTPUT_DIR = Path(__file__).parent / "data" / "xaut" / "perp"


def fetch_day(dt: datetime):
    """Fetch 1m candles for a single UTC day."""
    start = int(datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc).timestamp())
    end = start + 24 * 60 * 60
    params = {
        "symbol": SYMBOL,
        "resolution": RESOLUTION,
        "start": start,
        "end": end,
    }
    resp = requests.get(API_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"API error: {data}")
    rows = data.get("result", [])
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.sort_values("time")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("timestamp")
    df = df[["open", "high", "low", "close", "volume"]]
    return df


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Fetch from 2026-06-01 to 2026-07-07
    start_date = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end_date = datetime(2026, 7, 7, tzinfo=timezone.utc)
    frames = []
    day = start_date
    while day <= end_date:
        print(f"Fetching {day.date()}...")
        try:
            df = fetch_day(day)
            if df is not None:
                frames.append(df)
                print(f"  got {len(df)} candles")
            else:
                print(f"  no data")
        except Exception as e:
            print(f"  error: {e}")
        day = pd.Timestamp(day) + pd.Timedelta(days=1)
        time.sleep(0.5)

    if not frames:
        print("No data fetched")
        return

    full = pd.concat(frames).sort_index()
    full = full[~full.index.duplicated(keep="first")]
    out_path = OUTPUT_DIR / f"{SYMBOL}_mark_1m.csv"
    # save in the same format as existing perp CSVs: epoch 'time' column
    full_out = full.reset_index()
    full_out["time"] = full_out["timestamp"].apply(lambda x: int(x.timestamp()))
    full_out[["time", "open", "high", "low", "close", "volume"]].to_csv(out_path, index=False)
    print(f"Saved {len(full)} candles to {out_path}")
    print(full.head())
    print(full.tail())


if __name__ == "__main__":
    main()
