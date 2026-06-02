"""
Apply v5 synthetic-forward signal to Nifty options
===================================================
Reads NIFTY option chain snapshots from your existing data stores:
  1. Local CSV (preferred): db/oi_snapshots/YYYY-MM-DD_NIFTY.csv
  2. MongoDB option_snapshots collection (fallback)

Computes synthetic forward C − P + K vs Nifty spot, applies the same gate +
persistence + sizing logic as v5. Logs trades to db/oos_nifty_v5_trades.csv.

Run on prod where data lives:
  python scripts/backtest_nifty_synth_forward.py
  python scripts/backtest_nifty_synth_forward.py --source mongo
  python scripts/backtest_nifty_synth_forward.py --symbol BANKNIFTY
"""

from __future__ import annotations
import argparse
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

# v5 dials
ENTRY_PCT      = 0.006
PERSIST_HOURS  = 2
MIN_STRIKES    = 3
SLIPPAGE_BPS   = 5.0     # Nifty futures spread is wider than crypto perp
FEE_BPS        = 3.0     # NSE futures fees + STT
STOP_LOSS_PCT  = 0.015
PARTIAL_TP_PCT = 0.010
TRAIL_PEAK     = 0.005
TRAIL_GIVEBACK = 0.0025
SIZE_BASE_PCT  = 0.005
SIZE_MAX       = 3.0
SIZE_MIN       = 0.5
TT_MIN_HOURS   = 6
TT_MAX_HOURS   = 72


def load_snapshots_csv(symbol: str, db_dir: Path) -> pd.DataFrame:
    """Load all NIFTY/BANKNIFTY etc snapshots from db/oi_snapshots/."""
    files = sorted(db_dir.glob(f"*_{symbol}.csv"))
    if not files:
        raise FileNotFoundError(f"no snapshot files at {db_dir}/*_{symbol}.csv")
    dfs = []
    for f in files:
        try:
            df = pd.read_csv(f)
            dfs.append(df)
        except Exception as e:
            print(f"  skip {f.name}: {e}")
    if not dfs: return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


def load_snapshots_mongo(symbol: str) -> pd.DataFrame:
    """Fallback: read from MongoDB option_snapshots collection.

    Expected document fields (adjust to your schema):
      timestamp, symbol (NIFTY/BANKNIFTY/...), strike, side (CE/PE),
      expiry, ltp, oi, iv
    """
    try:
        import pymongo
    except ImportError:
        raise RuntimeError("pymongo not installed; pip install pymongo")
    url = os.environ.get("MONGODB_URL")
    db_name = os.environ.get("MONGODB_DB_NAME", "ai_trading")
    if not url:
        raise RuntimeError("MONGODB_URL not set")
    client = pymongo.MongoClient(url, serverSelectionTimeoutMS=5000)
    col = client[db_name]["option_snapshots"]
    rows = list(col.find({"symbol": symbol}))
    return pd.DataFrame(rows)


def normalize_schema(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Map whatever column names exist to v5's expected schema."""
    # CSV from your collector typically has these columns. Adjust if needed.
    rename = {
        "ltp": "mark", "last_price": "mark", "lastTradedPrice": "mark",
        "strikePrice": "strike", "strike_price": "strike",
        "optionType": "side", "option_type": "side",
        "expiryDate": "expiry", "expiry_date": "expiry",
        "ts": "timestamp", "time": "timestamp",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    if "timestamp" not in df.columns:
        raise ValueError("no timestamp column found")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp"])
    df["expiry"] = pd.to_datetime(df["expiry"], utc=True, errors="coerce")
    # NSE option side is CE/PE; v5 expects C/P
    df["side"] = df["side"].astype(str).str.upper().str[0]    # 'C' or 'P'
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["mark"] = pd.to_numeric(df["mark"], errors="coerce")
    df = df.dropna(subset=["strike", "mark", "side", "expiry"])
    return df[["timestamp", "side", "strike", "expiry", "mark"]]


def load_spot_series(symbol: str, db_dir: Path) -> pd.Series:
    """Spot price time series.

    Tries db/spot/<symbol>_1m.csv first; falls back to extracting from
    snapshot files (snapshots usually include underlyingValue).
    """
    candidate = db_dir.parent / "spot" / f"{symbol}_1m.csv"
    if candidate.exists():
        s = pd.read_csv(candidate, parse_dates=["timestamp"])
        return s.set_index("timestamp")["close"].sort_index().tz_localize("UTC")
    # extract from snapshots
    files = sorted(db_dir.glob(f"*_{symbol}.csv"))
    spots = []
    for f in files:
        try:
            df = pd.read_csv(f)
            if "underlyingValue" in df.columns and "timestamp" in df.columns:
                spots.append(df[["timestamp", "underlyingValue"]])
        except Exception:
            pass
    if not spots:
        raise RuntimeError("could not find spot series")
    spot_df = pd.concat(spots).rename(columns={"underlyingValue": "close"})
    spot_df["timestamp"] = pd.to_datetime(spot_df["timestamp"], utc=True)
    return spot_df.dropna().drop_duplicates("timestamp").set_index("timestamp")["close"].sort_index()


def compute_synth_forward(df: pd.DataFrame, spot: pd.Series, t: pd.Timestamp) -> list:
    """v5 signal: median (C − P + K − spot) / spot across near-money strikes per expiry."""
    snapshot = df[df["timestamp"] == t]
    if snapshot.empty: return []
    spot_t = float(spot.reindex([t], method="nearest").iloc[0])
    out = []
    expiries = sorted(snapshot["expiry"].dropna().unique())
    for exp in expiries:
        tte_h = (exp - t).total_seconds() / 3600
        if not (TT_MIN_HOURS <= tte_h <= TT_MAX_HOURS): continue
        same = snapshot[snapshot["expiry"] == exp]
        calls = same[same["side"] == "C"].set_index("strike")
        puts  = same[same["side"] == "P"].set_index("strike")
        common = sorted(set(calls.index) & set(puts.index))
        near = [K for K in common if abs(K - spot_t) / spot_t <= 0.05]
        if len(near) < MIN_STRIKES: continue
        devs = []
        for K in near:
            cp = float(calls.loc[K, "mark"])
            pp = float(puts.loc[K, "mark"])
            if cp <= 0 or pp <= 0: continue
            devs.append(((cp - pp + K) - spot_t) / spot_t)
        if len(devs) < MIN_STRIKES: continue
        pos = sum(1 for d in devs if d > 0); neg = sum(1 for d in devs if d < 0)
        if pos < MIN_STRIKES and neg < MIN_STRIKES: continue
        out.append({"expiry": exp, "pred": float(np.median(devs)),
                    "n_strikes": len(devs), "spot": spot_t})
    return out


def backtest(df: pd.DataFrame, spot: pd.Series, symbol: str):
    """Same execution engine as v5_synth_forward but on Nifty data."""
    timestamps = sorted(df["timestamp"].unique())
    print(f"  {len(timestamps):,} unique snapshot timestamps")
    print(f"  spot range: {spot.min():.0f} to {spot.max():.0f}")

    equity = 100_000.0    # Nifty positions are bigger absolute size
    open_pos = None
    trades = []
    sig_hist = {}

    for i, t in enumerate(timestamps):
        if i % 200 == 0:
            print(f"  ... {i}/{len(timestamps)}  open: {1 if open_pos else 0}  closed: {len(trades)}",
                  flush=True)
        sigs = compute_synth_forward(df, spot, t)
        for s in sigs:
            sig_hist.setdefault(s["expiry"], []).append((t, s["pred"]))
        # trim history
        for e in list(sig_hist.keys()):
            sig_hist[e] = [(ti, pi) for ti, pi in sig_hist[e]
                            if (t - ti).total_seconds() <= 6 * 3600]

        spot_t = float(spot.reindex([t], method="nearest").iloc[0])

        # manage open position
        if open_pos:
            held_h = (t - open_pos["entry_t"]).total_seconds() / 3600
            side = open_pos["side"]
            unreal = side * (spot_t - open_pos["entry_px"]) / open_pos["entry_px"]
            open_pos["peak"] = max(open_pos.get("peak", 0), unreal)
            exit_now, reason = False, ""
            if held_h >= 72: exit_now, reason = True, "max_hold"
            elif unreal < -STOP_LOSS_PCT: exit_now, reason = True, "stop"
            elif open_pos["peak"] >= TRAIL_PEAK and (open_pos["peak"] - unreal) > TRAIL_GIVEBACK:
                exit_now, reason = True, "trail"
            if exit_now:
                fill = spot_t * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fill - open_pos["entry_px"]) / open_pos["entry_px"]
                pnl_pct = ret - 2 * FEE_BPS / 1e4
                pnl = open_pos["notional"] * pnl_pct
                equity += pnl
                trades.append({**open_pos, "exit_t": t, "exit_px": fill,
                               "pnl_pct": pnl_pct, "pnl": pnl, "reason": reason,
                               "equity_after": equity})
                open_pos = None

        if open_pos: continue
        if not sigs: continue
        # pick strongest signal
        candidates = sorted(sigs, key=lambda s: -abs(s["pred"]))
        chosen = None
        for c in candidates:
            if abs(c["pred"]) < ENTRY_PCT: break
            hist = sig_hist.get(c["expiry"], [])
            recent = [pi for ti, pi in hist if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
            if len(recent) < PERSIST_HOURS: continue
            same_dir = sum(1 for pi in recent if np.sign(pi) == np.sign(c["pred"]))
            if same_dir < PERSIST_HOURS: continue
            chosen = c; break
        if not chosen: continue
        side = 1 if chosen["pred"] > 0 else -1
        size = min(SIZE_MAX, max(SIZE_MIN, abs(chosen["pred"]) / SIZE_BASE_PCT))
        notional = equity * size
        fill = spot_t * (1 + side * SLIPPAGE_BPS / 1e4)
        open_pos = {"entry_t": t, "entry_px": fill, "side": side,
                    "expiry": chosen["expiry"], "notional": notional,
                    "pred_pct": chosen["pred"] * 100,
                    "n_strikes": chosen["n_strikes"], "peak": 0}

    if not trades:
        print("\nNo trades produced.")
        return

    df_tr = pd.DataFrame(trades)
    n = len(df_tr); wins = (df_tr["pnl"] > 0).sum()
    print()
    print(f"  NIFTY v5 ({symbol}): {n} trades   win {wins/n*100:.1f}%   "
          f"net Rs{df_tr['pnl'].sum():+,.0f}   equity Rs{equity:,.0f}")
    out = Path("db") / f"nifty_v5_{symbol}_trades.csv"
    out.parent.mkdir(exist_ok=True)
    df_tr.to_csv(out, index=False)
    print(f"  trade log to {out}")


def inspect_mongo(symbol: str):
    """Print the actual fields + sample document from MongoDB before running.
    Run this FIRST to confirm schema matches what normalize_schema expects."""
    try:
        import pymongo
    except ImportError:
        print("pymongo not installed — pip install pymongo"); return
    url = os.environ.get("MONGODB_URL")
    db_name = os.environ.get("MONGODB_DB_NAME", "ai_trading")
    if not url:
        print("ERROR: MONGODB_URL not set in env"); return
    client = pymongo.MongoClient(url, serverSelectionTimeoutMS=5000)
    db = client[db_name]
    col = db["option_snapshots"]
    total = col.count_documents({})
    sym_count = col.count_documents({"symbol": symbol})
    print(f"=== MongoDB inspection: {db_name}.option_snapshots ===")
    print(f"  total documents       : {total:,}")
    print(f"  documents for {symbol:<10}: {sym_count:,}")
    sample = col.find_one({"symbol": symbol}) or col.find_one()
    if sample is None:
        print("  No documents found."); return
    print(f"\n  Sample document fields:")
    for k, v in sample.items():
        if k == "_id": continue
        vtype = type(v).__name__
        vshow = str(v)[:60] + ("..." if len(str(v)) > 60 else "")
        print(f"    {k:<25} ({vtype:<10}) = {vshow}")
    # date range
    print(f"\n  Time range:")
    for ts_field in ("timestamp", "ts", "time", "snapshot_time"):
        if ts_field in sample:
            oldest = col.find({"symbol": symbol}).sort(ts_field, 1).limit(1)
            newest = col.find({"symbol": symbol}).sort(ts_field, -1).limit(1)
            try:
                o = next(oldest)[ts_field]; n = next(newest)[ts_field]
                print(f"    field '{ts_field}': {o} to {n}")
            except StopIteration: pass
            break
    distinct_expiries = col.distinct("expiry", {"symbol": symbol})[:10]
    print(f"\n  Distinct expiries (first 10): {distinct_expiries}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--source", choices=["csv", "mongo"], default="mongo")
    ap.add_argument("--db-dir", default="db/oi_snapshots")
    ap.add_argument("--inspect", action="store_true",
                    help="Print MongoDB schema sample and exit (no backtest)")
    args = ap.parse_args()

    if args.inspect:
        inspect_mongo(args.symbol)
        return

    db_dir = Path(args.db_dir)
    print(f"Loading {args.symbol} from {args.source}...")
    if args.source == "csv":
        if not db_dir.exists():
            print(f"  ERROR: {db_dir} doesn't exist."); sys.exit(1)
        raw = load_snapshots_csv(args.symbol, db_dir)
    else:
        raw = load_snapshots_mongo(args.symbol)
    if raw.empty:
        print("  No snapshot data."); sys.exit(1)
    df = normalize_schema(raw, args.symbol)
    print(f"  {len(df):,} normalized rows")

    spot = load_spot_series(args.symbol, db_dir)
    print(f"  spot series: {len(spot):,} bars")
    print()

    backtest(df, spot, args.symbol)


if __name__ == "__main__":
    main()
