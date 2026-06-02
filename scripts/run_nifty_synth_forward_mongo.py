"""
NIFTY/BankNIFTY/FinNIFTY/SENSEX v5 backtest — runs directly against MongoDB
(stock-data.option_snapshots).

Designed to be invoked from the dev box with .env loaded. Reads the actual
production schema (spot embedded in each document, CE/PE option_type, IST
timestamps, weekly Thursday expiries).
"""

from __future__ import annotations
import argparse
import math
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd

# load .env
_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, _, v = line.partition("=")
        v = v.split("#", 1)[0].strip().strip("'\"")
        os.environ.setdefault(k.strip(), v)

import pymongo
sys.stdout.reconfigure(encoding="utf-8")

# v5 dials
ENTRY_PCT      = 0.006
PERSIST_HOURS  = 2
MIN_STRIKES    = 3
SLIPPAGE_BPS   = 5.0     # NSE perp/fut spread wider than Delta crypto
FEE_BPS        = 3.0
STOP_LOSS_PCT  = 0.015
PARTIAL_TP_PCT = 0.010
TRAIL_PEAK     = 0.005
TRAIL_GIVEBACK = 0.0025
SIZE_BASE_PCT  = 0.005
SIZE_MAX       = 3.0
SIZE_MIN       = 0.5
TT_MIN_HOURS   = 6
TT_MAX_HOURS   = 72

IST = timezone(timedelta(hours=5, minutes=30))


def fetch_data(symbol: str) -> pd.DataFrame:
    client = pymongo.MongoClient(os.environ["MONGODB_URL"],
                                  serverSelectionTimeoutMS=10000)
    db = client[os.environ["MONGODB_DB_NAME"]]
    col = db["option_snapshots"]
    cursor = col.find(
        {"symbol": symbol},
        {"_id": 0, "timestamp": 1, "expiry": 1, "strike": 1,
         "option_type": 1, "ltp": 1, "spot": 1, "oi": 1},
    )
    df = pd.DataFrame(list(cursor))
    if df.empty: return df
    # parse timestamps (IST local) → UTC
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce") \
                       .dt.tz_localize(IST).dt.tz_convert("UTC")
    # expiry strings → UTC at 15:30 IST (NSE close)
    df["expiry"] = (
        pd.to_datetime(df["expiry"], errors="coerce")
        .dt.tz_localize(IST) + pd.Timedelta(hours=15, minutes=30)
    ).dt.tz_convert("UTC")
    # normalize side
    df["side"] = df["option_type"].astype(str).str[0]  # CE→C, PE→P
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["mark"] = pd.to_numeric(df["ltp"], errors="coerce")
    df["spot"] = pd.to_numeric(df["spot"], errors="coerce")
    return df.dropna(subset=["timestamp", "expiry", "strike", "mark", "spot",
                              "side"])


def bucket_to_hour(df: pd.DataFrame) -> pd.DataFrame:
    """Snap to nearest hour for v5's hourly decision cadence."""
    df = df.copy()
    df["t_hour"] = df["timestamp"].dt.floor("1h")
    # for each (t_hour, symbol, expiry, strike, side) keep latest mark
    return df.sort_values("timestamp").drop_duplicates(
        subset=["t_hour", "expiry", "strike", "side"], keep="last")


def compute_signal(snap: pd.DataFrame, t: pd.Timestamp) -> list:
    """Synthetic forward signals at hourly snapshot t."""
    if snap.empty: return []
    spot = float(snap["spot"].median())   # spot embedded in each doc; take median
    out = []
    for exp, grp in snap.groupby("expiry"):
        tte_h = (exp - t).total_seconds() / 3600
        if not (TT_MIN_HOURS <= tte_h <= TT_MAX_HOURS): continue
        calls = grp[grp["side"] == "C"].set_index("strike")
        puts  = grp[grp["side"] == "P"].set_index("strike")
        common = sorted(set(calls.index) & set(puts.index))
        near = [K for K in common if abs(K - spot) / spot <= 0.05]
        if len(near) < MIN_STRIKES: continue
        devs = []
        for K in near:
            cp = float(calls.loc[K, "mark"])
            pp = float(puts.loc[K, "mark"])
            if cp <= 0 or pp <= 0: continue
            devs.append(((cp - pp + K) - spot) / spot)
        if len(devs) < MIN_STRIKES: continue
        pos = sum(1 for d in devs if d > 0); neg = sum(1 for d in devs if d < 0)
        if pos < MIN_STRIKES and neg < MIN_STRIKES: continue
        out.append({"expiry": exp, "pred": float(np.median(devs)),
                    "n_strikes": len(devs), "spot": spot})
    return out


def backtest(df: pd.DataFrame, symbol: str):
    df = bucket_to_hour(df)
    hours = sorted(df["t_hour"].unique())
    print(f"  {len(hours):,} hourly decision points "
          f"({hours[0]} to {hours[-1]})")

    # build spot 1m series from the embedded spot in docs
    spot_series = (df[["timestamp", "spot"]]
                   .dropna().drop_duplicates("timestamp")
                   .set_index("timestamp")["spot"].sort_index())
    print(f"  spot range: {spot_series.min():,.0f} to {spot_series.max():,.0f}")
    print()

    equity = 100_000.0     # ₹1L starting capital
    open_pos = None
    trades = []
    sig_hist = {}
    rejections = {"none": 0, "below_gate": 0, "no_persist": 0, "max_conc": 0}

    for i, t in enumerate(hours):
        snap = df[df["t_hour"] == t]
        sigs = compute_signal(snap, t)
        for s in sigs:
            sig_hist.setdefault(s["expiry"], []).append((t, s["pred"]))
        for e in list(sig_hist.keys()):
            sig_hist[e] = [(ti, pi) for ti, pi in sig_hist[e]
                            if (t - ti).total_seconds() <= 6 * 3600]

        # current spot for PnL
        if t in spot_series.index:
            spot_t = float(spot_series.loc[t])
        else:
            ix = spot_series.index.get_indexer([t], method="nearest")[0]
            spot_t = float(spot_series.iloc[ix])

        # manage open position
        if open_pos:
            held_h = (t - open_pos["entry_t"]).total_seconds() / 3600
            side = open_pos["side"]
            unreal = side * (spot_t - open_pos["entry_px"]) / open_pos["entry_px"]
            open_pos["peak"] = max(open_pos.get("peak", 0), unreal)

            if (not open_pos.get("tp_taken")) and unreal >= PARTIAL_TP_PCT:
                half_notional = open_pos["notional"] * 0.5
                fill = spot_t * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fill - open_pos["entry_px"]) / open_pos["entry_px"]
                pnl_pct = ret - 2 * FEE_BPS / 1e4
                pnl = half_notional * pnl_pct
                equity += pnl
                open_pos["notional"] -= half_notional
                open_pos["tp_taken"] = True
                trades.append({**open_pos, "exit_t": t, "exit_px": fill,
                               "notional": half_notional,
                               "pnl_pct": pnl_pct, "pnl": pnl,
                               "reason": "partial_tp", "equity_after": equity})

            exit_now, reason = False, ""
            if t >= open_pos["expiry"]: exit_now, reason = True, "expiry"
            elif held_h >= 72: exit_now, reason = True, "max_hold"
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
                               "pnl_pct": pnl_pct, "pnl": pnl,
                               "reason": reason, "equity_after": equity})
                open_pos = None

        if open_pos: continue
        if not sigs:
            rejections["none"] += 1; continue
        candidates = sorted(sigs, key=lambda s: -abs(s["pred"]))
        chosen = None
        for c in candidates:
            if abs(c["pred"]) < ENTRY_PCT:
                rejections["below_gate"] += 1; break
            hist = sig_hist.get(c["expiry"], [])
            recent = [pi for ti, pi in hist if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
            if len(recent) < PERSIST_HOURS:
                rejections["no_persist"] += 1; continue
            same = sum(1 for pi in recent if np.sign(pi) == np.sign(c["pred"]))
            if same < PERSIST_HOURS:
                rejections["no_persist"] += 1; continue
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
        print(f"  no trades for {symbol}.  rejections: {rejections}")
        return

    tdf = pd.DataFrame(trades)
    n = len(tdf); wins = (tdf["pnl"] > 0).sum()
    avg_win = tdf.loc[tdf["pnl"] > 0, "pnl"].mean() if wins else 0
    avg_loss = tdf.loc[tdf["pnl"] <= 0, "pnl"].mean() if (n - wins) else 0
    rr = abs(avg_win / avg_loss) if avg_loss else float("nan")
    print()
    print("=" * 80)
    print(f"  NIFTY v5 backtest — {symbol}")
    print(f"  gate {ENTRY_PCT*100:.2f}%  persist≥{PERSIST_HOURS}h  costs {FEE_BPS}bps+{SLIPPAGE_BPS}bps slip")
    print("=" * 80)
    print(f"  trades   : {n}     wins {wins}   win rate {wins/n*100:.1f}%   R:R {rr:.2f}")
    print(f"  avg win  : ₹{avg_win:+,.0f}   avg loss ₹{avg_loss:+,.0f}")
    print(f"  total PnL: ₹{tdf['pnl'].sum():+,.0f}   equity ₹{equity:,.0f}   "
          f"({(equity-100_000)/100_000*100:+.1f}% on ₹1L)")
    print()
    print("  Exits by reason:")
    print(tdf.groupby("reason")["pnl"].agg(["count", "sum", "mean"]).to_string())
    print()
    print("  Last 10 trades:")
    cols = ["entry_t", "side", "pred_pct", "n_strikes", "entry_px", "exit_px",
            "pnl", "reason"]
    print(tdf[cols].tail(10).to_string(index=False))
    print(f"  rejections: {rejections}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    args = ap.parse_args()
    print(f"Loading {args.symbol} from MongoDB...")
    df = fetch_data(args.symbol)
    print(f"  {len(df):,} normalized rows")
    if df.empty: return
    backtest(df, args.symbol)


if __name__ == "__main__":
    main()
