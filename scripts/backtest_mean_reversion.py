"""
Kotegawa Mean Reversion Backtest — NSE Nifty50 Stocks

Strategy: Buy when price crashes far below 25-day SMA (irrational panic).
          Exit when price reverts back toward SMA25 or hits TP/SL.

Key rules (Kotegawa):
  - Buy the blood: price must be X% below SMA25 (default 10/15/20%)
  - Exit at SMA25 (full reversion), or TP=+12%, or SL=-7%
  - Hold max N days (Kotegawa averaged 1-7 days per trade)
  - Only one position per stock at a time

Usage:
    python scripts/backtest_mean_reversion.py
    python scripts/backtest_mean_reversion.py --threshold 15 --period 365
    python scripts/backtest_mean_reversion.py --no-cache
"""

import sys, os, argparse, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging
logging.basicConfig(level=logging.WARNING)

import pandas as pd
import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument("--threshold", type=float, default=10.0,
                    help="pct below SMA25 to trigger entry (default 10)")
parser.add_argument("--tp",        type=float, default=12.0,
                    help="Take-profit pct from entry (default 12)")
parser.add_argument("--sl",        type=float, default=7.0,
                    help="Stop-loss pct from entry (default 7)")
parser.add_argument("--max-hold",  type=int,   default=15,
                    help="Max holding days (default 15)")
parser.add_argument("--period",    type=int,   default=365,
                    help="Days of history to test (default 365)")
parser.add_argument("--capital",   type=float, default=100_000.0,
                    help="Portfolio capital INR (default 100000)")
parser.add_argument("--risk-pct",  type=float, default=5.0,
                    help="Max risk per trade as pct of portfolio (default 5)")
parser.add_argument("--no-cache",  action="store_true",
                    help="Force re-fetch all data")
args = parser.parse_args()

THRESHOLD   = args.threshold / 100   # e.g. 0.10
TP_PCT      = args.tp     / 100
SL_PCT      = args.sl     / 100
MAX_HOLD    = args.max_hold
PERIOD_DAYS = args.period
CAPITAL     = args.capital
RISK_PCT    = args.risk_pct / 100
FORCE_RFCH  = args.no_cache

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "backtest_cache", "mean_reversion")

# ── Nifty50 universe ──────────────────────────────────────────────────────────
# Zerodha NSE symbols (no exchange prefix needed for historical API)
NIFTY50 = [
    # Financials
    "HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "SBIN",
    "BAJFINANCE", "BAJAJFINSV", "HDFCLIFE", "SBILIFE",
    # IT
    "INFY", "TCS", "WIPRO", "HCLTECH", "TECHM",
    # Energy / Infra
    "RELIANCE", "NTPC", "POWERGRID", "ONGC", "COALINDIA", "BPCL",
    # Consumer
    "HINDUNILVR", "ITC", "ASIANPAINT", "NESTLEIND", "TITAN",
    # Auto
    "MARUTI", "TATAMOTORS", "M&M", "EICHERMOT", "HEROMOTOCO",
    # Pharma
    "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB",
    # Metals / Materials
    "TATASTEEL", "JSWSTEEL", "HINDALCO", "ULTRACEMCO", "GRASIM",
    # Others
    "LT", "ADANIPORTS", "TATACONSUM", "BHARTIARTL", "INDUSINDBK",
    "APOLLOHOSP", "WIPRO",
]
NIFTY50 = list(dict.fromkeys(NIFTY50))   # deduplicate


# ── Data fetch ────────────────────────────────────────────────────────────────

def _fetch_daily(symbol: str):
    """Fetch daily OHLCV from Zerodha. Returns None on failure."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"{symbol}_daily_{PERIOD_DAYS}d.csv")

    if os.path.exists(cache) and not FORCE_RFCH:
        df = pd.read_csv(cache, index_col=0, parse_dates=True)
        return df if len(df) >= 30 else None

    try:
        from data.zerodha_fetcher import ZerodhaFetcher
        zf = ZerodhaFetcher.get()
        df = zf.fetch_equity_daily(symbol, days=PERIOD_DAYS)
        if df is None or len(df) < 30:
            return None
        df.to_csv(cache)
        return df
    except Exception as e:
        logging.debug("Fetch failed %s: %s", symbol, e)
        return None


# ── Single-stock backtest ─────────────────────────────────────────────────────

def _run_stock(symbol: str, df: pd.DataFrame) -> list:
    """
    Bar-by-bar mean reversion backtest for one stock.
    Returns list of trade dicts.
    """
    df = df.copy()
    df["sma25"]   = df["Close"].rolling(25).mean()
    df["below_pct"] = (df["sma25"] - df["Close"]) / df["sma25"]

    trades   = []
    position = None   # {entry_price, entry_date, bars_held}

    for i in range(25, len(df)):
        row      = df.iloc[i]
        price    = float(row["Close"])
        sma25    = float(row["sma25"])
        date     = df.index[i]

        if pd.isna(sma25) or sma25 <= 0:
            continue

        # ── Manage open position ───────────────────────────────────────────────
        if position:
            entry   = position["entry"]
            bars    = position["bars_held"]
            pnl_pct = (price - entry) / entry

            # Exit conditions (priority order)
            exit_reason = None
            exit_price  = price

            if pnl_pct >= TP_PCT:
                exit_reason = "TP"
                exit_price  = round(entry * (1 + TP_PCT), 2)
            elif pnl_pct <= -SL_PCT:
                exit_reason = "SL"
                exit_price  = round(entry * (1 - SL_PCT), 2)
            elif price >= sma25:
                exit_reason = "REVERT"   # Kotegawa's primary exit: back to SMA25
            elif bars >= MAX_HOLD:
                exit_reason = "TIMEOUT"

            if exit_reason:
                pnl      = (exit_price - entry) / entry
                trades.append({
                    "symbol":       symbol,
                    "entry_date":   str(position["entry_date"].date()),
                    "exit_date":    str(date.date()),
                    "entry":        round(entry, 2),
                    "exit":         round(exit_price, 2),
                    "bars_held":    bars,
                    "pnl_pct":      round(pnl * 100, 2),
                    "exit_reason":  exit_reason,
                    "entry_gap":    round(position["entry_gap"] * 100, 2),  # % below SMA at entry
                })
                position = None
            else:
                position["bars_held"] += 1
            continue

        # ── Entry: price far below SMA25 ───────────────────────────────────────
        below = float(row["below_pct"])
        if below >= THRESHOLD:
            position = {
                "entry":      price,
                "entry_date": date,
                "entry_gap":  below,
                "bars_held":  1,
            }

    return trades


# ── Portfolio stats ───────────────────────────────────────────────────────────

def _stats(trades: list) -> dict:
    if not trades:
        return {"n": 0, "win_rate": 0, "avg_pnl": 0, "pf": 0,
                "avg_hold": 0, "best": 0, "worst": 0}

    wins   = [t["pnl_pct"] for t in trades if t["pnl_pct"] > 0]
    losses = [t["pnl_pct"] for t in trades if t["pnl_pct"] <= 0]
    pf     = (sum(wins) / abs(sum(losses))) if losses else float("inf")

    return {
        "n":        len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "avg_pnl":  round(sum(t["pnl_pct"] for t in trades) / len(trades), 2),
        "pf":       round(pf, 2) if pf != float("inf") else 99.0,
        "avg_hold": round(sum(t["bars_held"] for t in trades) / len(trades), 1),
        "best":     round(max(t["pnl_pct"] for t in trades), 2),
        "worst":    round(min(t["pnl_pct"] for t in trades), 2),
    }


def _exit_breakdown(trades: list) -> dict:
    counts = {}
    for t in trades:
        counts[t["exit_reason"]] = counts.get(t["exit_reason"], 0) + 1
    return counts


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\nKotegawa Mean Reversion Backtest — NSE Nifty50")
    print(f"  Entry   : price < SMA25 × {1 - THRESHOLD:.0%}  (≥{args.threshold:.0f}% below 25-day MA)")
    print(f"  Exit    : SMA25 reversion | TP {args.tp:.0f}% | SL {args.sl:.0f}% | Max {MAX_HOLD}d hold")
    print(f"  Period  : {PERIOD_DAYS} days  |  Stocks: {len(NIFTY50)}\n")

    all_trades = []
    stock_rows = []

    for sym in NIFTY50:
        df = _fetch_daily(sym)
        if df is None:
            print(f"  [SKIP] {sym} — no data")
            continue

        trades = _run_stock(sym, df)
        st     = _stats(trades)
        print(f"  {sym:<15} {st['n']:>3} trades  WR {st['win_rate']:>5.1f}%  "
              f"avg {st['avg_pnl']:>+6.2f}%  PF {st['pf']:>4.2f}  "
              f"hold {st['avg_hold']:.1f}d", end="")
        if st["n"] > 0:
            flag = " ***" if st["win_rate"] >= 50 and st["avg_pnl"] > 0 and st["pf"] >= 1.3 else ""
            print(flag)
        else:
            print()

        all_trades.extend(trades)
        if st["n"] > 0:
            stock_rows.append({**st, "symbol": sym,
                               "exits": _exit_breakdown(trades)})

    # ── Portfolio-level summary ────────────────────────────────────────────────
    overall = _stats(all_trades)
    exits   = _exit_breakdown(all_trades)

    print(f"\n{'='*72}")
    print(f"  OVERALL — {len(NIFTY50)} stocks | {args.threshold:.0f}% threshold | {PERIOD_DAYS}d period")
    print(f"{'='*72}")
    print(f"  Total trades    : {overall['n']}")
    print(f"  Win rate        : {overall['win_rate']}%")
    print(f"  Avg trade PnL   : {overall['avg_pnl']:+.2f}%")
    print(f"  Profit factor   : {overall['pf']:.2f}")
    print(f"  Avg hold        : {overall['avg_hold']} days")
    print(f"  Best trade      : +{overall['best']:.2f}%")
    print(f"  Worst trade     : {overall['worst']:.2f}%")
    print(f"  Exit breakdown  : {exits}")

    # ── Best stocks by win rate + PF ──────────────────────────────────────────
    good = [r for r in stock_rows if r["n"] >= 3 and r["win_rate"] >= 50 and r["pf"] >= 1.2]
    good.sort(key=lambda r: r["win_rate"] * r["pf"], reverse=True)

    if good:
        print(f"\n  Top mean-reversion candidates (WR≥50%, PF≥1.2, ≥3 trades):")
        print(f"  {'Symbol':<15} {'#Tr':>4}  {'WR%':>6}  {'AvgPnL':>7}  {'PF':>5}  {'Hold':>5}  Exits")
        print(f"  {'-'*70}")
        for r in good[:15]:
            ex = r["exits"]
            ex_str = f"TP:{ex.get('TP',0)} REV:{ex.get('REVERT',0)} SL:{ex.get('SL',0)} TO:{ex.get('TIMEOUT',0)}"
            print(f"  {r['symbol']:<15} {r['n']:>4}  {r['win_rate']:>6.1f}  "
                  f"{r['avg_pnl']:>+7.2f}  {r['pf']:>5.2f}  {r['avg_hold']:>5.1f}  {ex_str}")

    # ── Save results ──────────────────────────────────────────────────────────
    out = {
        "config": {
            "threshold_pct": args.threshold,
            "tp_pct": args.tp, "sl_pct": args.sl,
            "max_hold_days": MAX_HOLD, "period_days": PERIOD_DAYS,
        },
        "overall": overall,
        "exit_breakdown": exits,
        "top_stocks": good[:10],
        "all_trades": all_trades,
    }
    out_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        f"backtest_results_mean_reversion_{int(args.threshold)}pct.json"
    )
    clean = json.dumps(out, default=str)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(clean)
    print(f"\n  Full results → backtest_results_mean_reversion_{int(args.threshold)}pct.json")

    # ── Quick grid: run 3 thresholds if this was the default run ──────────────
    if args.threshold == 10.0 and PERIOD_DAYS == 365:
        print(f"\n  Tip: test other thresholds:")
        print(f"    python scripts/backtest_mean_reversion.py --threshold 15")
        print(f"    python scripts/backtest_mean_reversion.py --threshold 20")
        print(f"    python scripts/backtest_mean_reversion.py --threshold 10 --tp 8 --sl 5")
