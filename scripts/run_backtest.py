"""
Backtest permutation runner — Musashi (15m) + Raijin (5m)

Fetches NIFTY data ONCE per strategy, then replays all parameter
combinations against the cached dataframe.

Usage:
    python scripts/run_backtest.py

Output:
    Ranked tables for Musashi and Raijin printed to stdout.
    Full results saved to backtest_results.json in project root.
"""

import sys, os, json, itertools
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force UTF-8 output on Windows (avoids cp1252 UnicodeEncodeError)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging
logging.basicConfig(level=logging.WARNING)   # suppress info noise during sweep

from backtesting.engine import BacktestEngine
from backtesting.metrics import compute_metrics

INITIAL_CAPITAL = 50_000.0   # mid-range of ₹20K-₹70K budget
PERIOD          = "60d"

# ── Permutation grids ────────────────────────────────────────────────────────
# min_score: post-filter on the strategy's internal score output.
#   7.5 = default (uses whatever the strategy's threshold already allows)
#   8.0 / 8.5 = progressively tighter — fewer but higher-quality trades

MUSASHI_GRID = {
    "min_score": [7.5, 8.0, 8.5],
    "rr_ratio":  [2.0, 2.5, 3.0],
    "risk_pct":  [3.0, 4.0, 5.0],
}

RAIJIN_GRID = {
    "min_score": [6.0, 6.5, 7.0],   # threshold now 6.0 — 3 conditions sufficient
    "rr_ratio":  [1.5, 2.0, 2.5],
    "risk_pct":  [3.0, 4.0, 5.0],
}


def _pf(trades):
    """Profit factor = gross_profit / gross_loss (∞ if no losses)."""
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss   = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    return round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")


def _max_dd(equity_curve):
    """Max drawdown % from equity curve."""
    peak, max_dd = 0, 0
    for pt in equity_curve:
        eq = pt["equity"]
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 1)


def _win_rate(trades):
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return round(wins / len(trades) * 100, 1)


def _summarise(result, strategy, params):
    trades = result["trades"]
    equity = result["final_equity"]
    ret_pct = round((equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 1)
    return {
        "strategy":   strategy,
        "min_score":  params["min_score"],
        "rr_ratio":   params["rr_ratio"],
        "risk_pct":   params["risk_pct"],
        "trades":     len(trades),
        "win_rate":   _win_rate(trades),
        "pf":         _pf(trades),
        "max_dd_pct": _max_dd(result["equity_curve"]),
        "net_return": ret_pct,
        "final_eq":   round(equity, 0),
    }


def _print_table(rows, title):
    rows = sorted(rows, key=lambda r: r["net_return"], reverse=True)
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")
    hdr = f"{'Score':>5}  {'R:R':>4}  {'Risk%':>5}  {'#Trades':>7}  {'Win%':>6}  {'PF':>5}  {'MaxDD%':>7}  {'Net%':>7}  {'FinalEq':>9}"
    print(hdr)
    print('-' * 80)
    for r in rows:
        pf_str = f"{r['pf']:.2f}" if r['pf'] != float("inf") else ">99"
        flag = " ***" if r["net_return"] > 0 and r["win_rate"] >= 35 and r["max_dd_pct"] <= 20 else ""
        print(
            f"{r['min_score']:>5.1f}  {r['rr_ratio']:>4.1f}  {r['risk_pct']:>5.1f}  "
            f"{r['trades']:>7}  {r['win_rate']:>6.1f}  {pf_str:>5}  "
            f"{r['max_dd_pct']:>7.1f}  {r['net_return']:>7.1f}  {r['final_eq']:>9,.0f}{flag}"
        )
    print()
    print("*** = net positive + win rate >=35% + max drawdown <=20%")


def run_musashi(engine, df_15m):
    print(f"\nRunning Musashi permutations ({len(list(itertools.product(*MUSASHI_GRID.values())))} combos)…")
    rows = []
    keys = list(MUSASHI_GRID.keys())
    for vals in itertools.product(*MUSASHI_GRID.values()):
        params = dict(zip(keys, vals))
        try:
            result = engine.run_trend_rider(
                symbol="NIFTY", period=PERIOD, interval="15m",
                risk_pct=params["risk_pct"],
                rr_ratio=params["rr_ratio"],
                min_score=params["min_score"],
                _df=df_15m,
            )
            rows.append(_summarise(result, "Musashi", params))
            print(".", end="", flush=True)
        except Exception as e:
            print(f"\n  [SKIP] min_score={params['min_score']} rr={params['rr_ratio']} risk={params['risk_pct']}: {e}")
    print()
    return rows


def run_raijin(engine, df_5m):
    print(f"\nRunning Raijin permutations ({len(list(itertools.product(*RAIJIN_GRID.values())))} combos)…")
    rows = []
    keys = list(RAIJIN_GRID.keys())
    for vals in itertools.product(*RAIJIN_GRID.values()):
        params = dict(zip(keys, vals))
        try:
            result = engine.run_vwap_snap(
                symbol="NIFTY", period=PERIOD, interval="5m",
                risk_pct=params["risk_pct"],
                rr_ratio=params["rr_ratio"],
                min_score=params["min_score"],
                _df=df_5m,
            )
            rows.append(_summarise(result, "Raijin", params))
            print(".", end="", flush=True)
        except Exception as e:
            print(f"\n  [SKIP] min_score={params['min_score']} rr={params['rr_ratio']} risk={params['risk_pct']}: {e}")
    print()
    return rows


CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtest_cache")


def _load_or_fetch(engine, symbol, period, interval):
    """Load from local CSV cache if available; otherwise fetch from Zerodha and cache."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, f"{symbol}_{interval}_{period}.csv")
    if os.path.exists(cache_file):
        import pandas as pd
        df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
        if "_date" not in df.columns:
            df["_date"] = df.index.date
        print(f"  (from cache: {cache_file})")
        return df
    df = engine.fetch_data(symbol, period=period, interval=interval)
    df.to_csv(cache_file)
    print(f"  (cached to: {cache_file})")
    return df


if __name__ == "__main__":
    engine = BacktestEngine(initial_capital=INITIAL_CAPITAL)

    print("Fetching NIFTY 15m data (Musashi)…")
    try:
        df_15m = _load_or_fetch(engine, "NIFTY", PERIOD, "15m")
        print(f"  {len(df_15m)} bars across {df_15m['_date'].nunique()} trading days")
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    print("Fetching NIFTY 5m data (Raijin)…")
    try:
        df_5m = _load_or_fetch(engine, "NIFTY", PERIOD, "5m")
        print(f"  {len(df_5m)} bars across {df_5m['_date'].nunique()} trading days")
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    musashi_rows = run_musashi(engine, df_15m)
    raijin_rows  = run_raijin(engine, df_5m)

    _print_table(musashi_rows, "MUSASHI (15m) — sorted by net return %")
    _print_table(raijin_rows,  "RAIJIN (5m)   — sorted by net return %")

    # Save full results
    out_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "backtest_results.json")
    # Replace float inf with null so JSON is valid
    def _clean(rows):
        return [{k: (None if isinstance(v, float) and v == float("inf") else v)
                 for k, v in r.items()} for r in rows]

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"musashi": _clean(musashi_rows), "raijin": _clean(raijin_rows)}, f, indent=2)
    print(f"\nFull results saved → {out_path}")
