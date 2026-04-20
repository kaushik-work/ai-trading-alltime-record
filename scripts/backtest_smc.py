"""
SMC Algo Backtest — walks 5m NIFTY bars, runs score_smc() at each candle.

Enters when score >= +6 (BUY) or <= -6 (SELL).
SL = 1× ATR(14) from entry candle.
TP = 2× ATR(14) from entry candle.
P&L in NIFTY spot points × lots × lot_size.

Usage:
  python scripts/backtest_smc.py
  python scripts/backtest_smc.py --months 2026-03,2026-04
  python scripts/backtest_smc.py --date 2026-04-10
  python scripts/backtest_smc.py --no-cache
"""

import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging
logging.basicConfig(level=logging.WARNING)

import pandas as pd
import numpy as np
from datetime import time, date as date_t

# ── Args ──────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--capital",  type=float, default=150_000.0)
parser.add_argument("--lots",     type=int,   default=3)
parser.add_argument("--no-cache", action="store_true")
parser.add_argument("--months",   type=str,   default=None,
                    help="Comma-separated e.g. 2026-04 or 2026-01,2026-02")
parser.add_argument("--date",     type=str,   default=None,
                    help="Single date YYYY-MM-DD")
parser.add_argument("--window",   type=int,   default=60,
                    help="Rolling bar window fed to score_smc (default 60)")
args = parser.parse_args()

LOT_SIZE = 65
LOTS     = args.lots
THRESHOLD = 6
SL_ATR   = 1.0    # stop loss = 1× ATR(14)
TP_ATR   = 2.0    # take profit = 2× ATR(14)
TRADE_START = time(9, 45)
TRADE_EXIT  = time(15, 10)
WINDOW = args.window

TARGET_MONTHS = (
    [m.strip() for m in args.months.split(",") if m.strip()]
    if args.months
    else (
        [args.date[:7]] if args.date
        else ["2026-01", "2026-02", "2026-03"]
    )
)

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtest_cache")


# ── Data loading ──────────────────────────────────────────────────────────────

def _norm(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.capitalize() for c in df.columns]
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    if df.index.tzinfo is not None:
        df.index = df.index.tz_convert("Asia/Kolkata")
    else:
        df.index = df.index.tz_localize("Asia/Kolkata")
    df["_date"] = df.index.date
    df["_time"] = df.index.time
    return df


def _load_5m() -> pd.DataFrame:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, "NIFTY_5m_90d.csv")
    if os.path.exists(cache_path) and not args.no_cache:
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        print(f"  (cache) {len(df)} bars")
        return _norm(df)
    print("  Fetching NIFTY 5m from Angel One (90d)...")
    from data.angel_fetcher import AngelFetcher
    df = AngelFetcher.get().fetch_historical_df("NIFTY", "5m", days=90)
    if df is None or df.empty:
        raise RuntimeError("No data from Angel One")
    df.to_csv(cache_path)
    print(f"  Saved cache: {len(df)} bars")
    return _norm(df)


# ── ATR helper ────────────────────────────────────────────────────────────────

def _atr14(df: pd.DataFrame) -> float:
    h, l, c = df["High"].values, df["Low"].values, df["Close"].values
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    if len(tr) < 1:
        return 50.0
    return float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr))


# ── Backtest engine ───────────────────────────────────────────────────────────

def run_backtest(df: pd.DataFrame) -> list:
    from strategies.smc_scorer import score_smc

    # Filter to target months
    df = df[df["_date"].astype(str).str[:7].isin(TARGET_MONTHS)].copy()
    if args.date:
        df = df[df["_date"].astype(str) == args.date]

    if df.empty:
        print("No data for the selected period.")
        return []

    trading_days = sorted(df["_date"].unique())
    trades = []

    for day in trading_days:
        day_df = df[df["_date"] == day].copy()
        in_trade = None   # {side, entry, sl, tp, entry_idx, atr}

        for i in range(WINDOW, len(day_df)):
            bar = day_df.iloc[i]
            t   = bar["_time"]

            # Only enter during market hours
            if t < TRADE_START or t > TRADE_EXIT:
                in_trade = None
                continue

            # Manage open trade
            if in_trade:
                # Check SL / TP on this bar's OHLC
                if in_trade["side"] == "BUY":
                    if bar["Low"] <= in_trade["sl"]:
                        pnl = (in_trade["sl"] - in_trade["entry"]) * LOTS * LOT_SIZE
                        trades.append({**in_trade, "exit": in_trade["sl"], "pnl": pnl,
                                        "exit_time": t, "exit_day": day, "reason": "SL"})
                        in_trade = None
                        continue
                    if bar["High"] >= in_trade["tp"]:
                        pnl = (in_trade["tp"] - in_trade["entry"]) * LOTS * LOT_SIZE
                        trades.append({**in_trade, "exit": in_trade["tp"], "pnl": pnl,
                                        "exit_time": t, "exit_day": day, "reason": "TP"})
                        in_trade = None
                        continue
                else:  # SELL
                    if bar["High"] >= in_trade["sl"]:
                        pnl = (in_trade["entry"] - in_trade["sl"]) * LOTS * LOT_SIZE
                        trades.append({**in_trade, "exit": in_trade["sl"], "pnl": pnl,
                                        "exit_time": t, "exit_day": day, "reason": "SL"})
                        in_trade = None
                        continue
                    if bar["Low"] <= in_trade["tp"]:
                        pnl = (in_trade["entry"] - in_trade["tp"]) * LOTS * LOT_SIZE
                        trades.append({**in_trade, "exit": in_trade["tp"], "pnl": pnl,
                                        "exit_time": t, "exit_day": day, "reason": "TP"})
                        in_trade = None
                        continue

                # Force EOD close
                if t >= TRADE_EXIT:
                    close_px = bar["Close"]
                    if in_trade["side"] == "BUY":
                        pnl = (close_px - in_trade["entry"]) * LOTS * LOT_SIZE
                    else:
                        pnl = (in_trade["entry"] - close_px) * LOTS * LOT_SIZE
                    trades.append({**in_trade, "exit": close_px, "pnl": pnl,
                                    "exit_time": t, "exit_day": day, "reason": "EOD"})
                    in_trade = None
                continue

            # No trade — evaluate signal
            window = day_df.iloc[i - WINDOW: i + 1]
            sig = score_smc(window)
            score = sig.get("score", 0)

            if abs(score) < THRESHOLD:
                continue

            entry_px = bar["Close"]
            atr = _atr14(window)
            side = "BUY" if score >= THRESHOLD else "SELL"

            if side == "BUY":
                sl = entry_px - SL_ATR * atr
                tp = entry_px + TP_ATR * atr
            else:
                sl = entry_px + SL_ATR * atr
                tp = entry_px - TP_ATR * atr

            in_trade = {
                "side": side, "entry": entry_px, "sl": sl, "tp": tp,
                "score": score, "entry_time": t, "entry_day": day, "atr": atr,
            }

        # EOD force-close if still in trade at session end
        if in_trade:
            last_bar = day_df.iloc[-1]
            close_px = last_bar["Close"]
            if in_trade["side"] == "BUY":
                pnl = (close_px - in_trade["entry"]) * LOTS * LOT_SIZE
            else:
                pnl = (in_trade["entry"] - close_px) * LOTS * LOT_SIZE
            trades.append({**in_trade, "exit": close_px, "pnl": pnl,
                            "exit_time": last_bar["_time"], "exit_day": day, "reason": "EOD"})

    return trades


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(trades: list, capital: float):
    if not trades:
        print("No trades taken.")
        return

    pnls = [t["pnl"] for t in trades]
    wins  = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl  = sum(pnls)
    max_dd     = 0.0
    peak = 0.0
    running = 0.0
    for p in pnls:
        running += p
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    print("\n" + "=" * 60)
    print(f"  SMC Algo Backtest — {', '.join(TARGET_MONTHS)}")
    print(f"  Lots: {LOTS}  |  Lot size: {LOT_SIZE}  |  Window: {WINDOW} bars")
    print(f"  SL: {SL_ATR}× ATR  |  TP: {TP_ATR}× ATR  |  Threshold: {THRESHOLD}")
    print("=" * 60)
    print(f"  Trades:       {len(trades)}")
    print(f"  Win rate:     {len(wins)/len(trades)*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Total P&L:    ₹{total_pnl:,.0f}")
    print(f"  Avg trade:    ₹{total_pnl/len(trades):,.0f}")
    print(f"  Best trade:   ₹{max(pnls):,.0f}")
    print(f"  Worst trade:  ₹{min(pnls):,.0f}")
    print(f"  Max drawdown: ₹{max_dd:,.0f}")
    print(f"  Return:       {total_pnl/capital*100:.1f}%")
    print("=" * 60)

    # Per-day breakdown
    print("\n  Daily P&L:")
    by_day: dict = {}
    for t in trades:
        d = str(t["entry_day"])
        by_day.setdefault(d, []).append(t["pnl"])
    for d, ps in sorted(by_day.items()):
        sym = "+" if sum(ps) >= 0 else "-"
        print(f"    {d}  {sym}  ₹{sum(ps):>8,.0f}  ({len(ps)} trades)")

    # Exit reason breakdown
    print("\n  Exit reasons:")
    reasons: dict = {}
    for t in trades:
        r = t.get("reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    for r, cnt in sorted(reasons.items()):
        print(f"    {r}: {cnt}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\nSMC Algo Backtest — months: {TARGET_MONTHS}")
    df = _load_5m()
    trades = run_backtest(df)
    print_report(trades, args.capital)
