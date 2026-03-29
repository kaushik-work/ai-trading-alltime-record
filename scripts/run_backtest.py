"""
Backtest permutation runner — Musashi + Raijin + ATR Intraday
All timeframes (3m, 5m, 15m) x All periods (30d, 60d, 90d)

Usage:
    python scripts/run_backtest.py                     # default: 60d, 50K capital
    python scripts/run_backtest.py --period 30d        # 30-day window
    python scripts/run_backtest.py --period all        # 30d + 60d + 90d
    python scripts/run_backtest.py --capital 70000
    python scripts/run_backtest.py --no-cache          # force re-fetch from Zerodha
    python scripts/run_backtest.py --strategy musashi  # single strategy
"""

import sys, os, json, itertools, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging
logging.basicConfig(level=logging.WARNING)

from backtesting.engine import BacktestEngine

parser = argparse.ArgumentParser()
parser.add_argument("--capital",  type=float, default=50_000.0,
                    help="Starting capital INR (default 50000)")
parser.add_argument("--period",   type=str,   default="60d",
                    help="30d | 60d | 90d | all  (default 60d)")
parser.add_argument("--strategy", type=str,   default="all",
                    help="musashi | raijin | atr | all  (default all)")
parser.add_argument("--no-cache", action="store_true",
                    help="Force re-fetch data even if cache exists")
args = parser.parse_args()

INITIAL_CAPITAL = args.capital
PERIODS         = ["30d", "60d", "90d"] if args.period == "all" else [args.period]
FORCE_REFETCH   = args.no_cache
RUN_STRATEGY    = args.strategy.lower()

# ── Permutation grids ─────────────────────────────────────────────────────────

# 5m is the proven timeframe across all 3 strategies (backtest 2026-03-29).
# 15m underperforms over 60d/90d. 3m data not reliably available from Kite (< 60d retention).
MUSASHI_INTERVALS = ["5m"]
MUSASHI_GRID = {
    "min_score": [7.5, 8.0, 8.5],
    "rr_ratio":  [2.0, 2.5, 3.0],
    "risk_pct":  [3.0, 4.0, 5.0],
}

RAIJIN_INTERVALS = ["5m"]
RAIJIN_GRID = {
    "min_score": [6.0, 6.5, 7.0],
    "rr_ratio":  [1.5, 2.0, 2.5],
    "risk_pct":  [3.0, 4.0, 5.0],
}

ATR_INTERVALS = ["5m"]
ATR_GRID = {
    "min_score": [5, 6, 7],
    "rr_ratio":  [2.0, 2.5, 3.0],
    "risk_pct":  [2.0, 3.0, 4.0],
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _pf(trades):
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss   = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    return round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

def _max_dd(equity_curve):
    peak, max_dd = 0, 0
    for pt in equity_curve:
        eq = pt["equity"]
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd
    return round(max_dd, 1)

def _win_rate(trades):
    if not trades: return 0.0
    return round(sum(1 for t in trades if t["pnl"] > 0) / len(trades) * 100, 1)

def _summarise(result, strategy, params, interval, period):
    trades  = result["trades"]
    equity  = result["final_equity"]
    ret_pct = round((equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 1)
    return {
        "strategy":   strategy,
        "interval":   interval,
        "period":     period,
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
    print(f"\n{'='*90}")
    print(f"  {title}")
    print(f"{'='*90}")
    hdr = f"{'TF':>3}  {'Period':>4}  {'Score':>5}  {'R:R':>4}  {'Risk%':>5}  {'#Tr':>5}  {'Win%':>6}  {'PF':>5}  {'DD%':>5}  {'Net%':>6}  {'FinalEq':>9}"
    print(hdr)
    print('-' * 90)
    for r in rows:
        pf_str = f"{r['pf']:.2f}" if r['pf'] != float("inf") else ">99"
        flag   = " ***" if r["net_return"] > 0 and r["win_rate"] >= 35 and r["max_dd_pct"] <= 20 else ""
        print(
            f"{r['interval']:>3}  {r['period']:>4}  "
            f"{r['min_score']:>5.1f}  {r['rr_ratio']:>4.1f}  {r['risk_pct']:>5.1f}  "
            f"{r['trades']:>5}  {r['win_rate']:>6.1f}  {pf_str:>5}  "
            f"{r['max_dd_pct']:>5.1f}  {r['net_return']:>6.1f}  {r['final_eq']:>9,.0f}{flag}"
        )
    print()
    print("*** = net positive + win rate >=35% + max drawdown <=20%")

# ── Cache ─────────────────────────────────────────────────────────────────────

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtest_cache")

def _load_or_fetch(engine, symbol, period, interval):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, f"{symbol}_{interval}_{period}.csv")
    if os.path.exists(cache_file) and not FORCE_REFETCH:
        import pandas as pd
        df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
        if "_date" not in df.columns:
            df["_date"] = df.index.date
        print(f"  (cache) {symbol} {interval} {period}: {len(df)} bars")
        return df
    df = engine.fetch_data(symbol, period=period, interval=interval)
    df.to_csv(cache_file)
    print(f"  (fetch) {symbol} {interval} {period}: {len(df)} bars — cached")
    return df

# ── Strategy runners ──────────────────────────────────────────────────────────

def run_musashi(engine, dfs):
    n = len(MUSASHI_INTERVALS) * len(PERIODS) * len(list(itertools.product(*MUSASHI_GRID.values())))
    print(f"\nRunning Musashi permutations ({n} combos)...")
    rows = []
    keys = list(MUSASHI_GRID.keys())
    for interval in MUSASHI_INTERVALS:
        for period in PERIODS:
            df = dfs.get((interval, period))
            if df is None: continue
            for vals in itertools.product(*MUSASHI_GRID.values()):
                params = dict(zip(keys, vals))
                try:
                    result = engine.run_trend_rider(
                        symbol="NIFTY", period=period, interval=interval,
                        risk_pct=params["risk_pct"], rr_ratio=params["rr_ratio"],
                        min_score=params["min_score"], _df=df,
                    )
                    rows.append(_summarise(result, "Musashi", params, interval, period))
                    print(".", end="", flush=True)
                except Exception as e:
                    print(f"\n  [SKIP] {interval} {period} {params}: {e}")
    print()
    return rows

def run_raijin(engine, dfs):
    n = len(RAIJIN_INTERVALS) * len(PERIODS) * len(list(itertools.product(*RAIJIN_GRID.values())))
    print(f"\nRunning Raijin permutations ({n} combos)...")
    rows = []
    keys = list(RAIJIN_GRID.keys())
    for interval in RAIJIN_INTERVALS:
        for period in PERIODS:
            df = dfs.get((interval, period))
            if df is None: continue
            for vals in itertools.product(*RAIJIN_GRID.values()):
                params = dict(zip(keys, vals))
                try:
                    result = engine.run_vwap_snap(
                        symbol="NIFTY", period=period, interval=interval,
                        risk_pct=params["risk_pct"], rr_ratio=params["rr_ratio"],
                        min_score=params["min_score"], _df=df,
                    )
                    rows.append(_summarise(result, "Raijin", params, interval, period))
                    print(".", end="", flush=True)
                except Exception as e:
                    print(f"\n  [SKIP] {interval} {period} {params}: {e}")
    print()
    return rows

def run_atr(engine, dfs):
    n = len(ATR_INTERVALS) * len(PERIODS) * len(list(itertools.product(*ATR_GRID.values())))
    print(f"\nRunning ATR Intraday permutations ({n} combos)...")
    rows = []
    keys = list(ATR_GRID.keys())
    for interval in ATR_INTERVALS:
        for period in PERIODS:
            df = dfs.get((interval, period))
            if df is None: continue
            for vals in itertools.product(*ATR_GRID.values()):
                params = dict(zip(keys, vals))
                try:
                    result = engine.run(
                        symbol="NIFTY", period=period, interval=interval,
                        risk_pct=params["risk_pct"], rr_ratio=params["rr_ratio"],
                        min_score=int(params["min_score"]),
                    )
                    rows.append(_summarise(result, "ATR", params, interval, period))
                    print(".", end="", flush=True)
                except Exception as e:
                    print(f"\n  [SKIP] {interval} {period} {params}: {e}")
    print()
    return rows

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    engine = BacktestEngine(initial_capital=INITIAL_CAPITAL)

    # Collect all needed intervals across strategies
    needed = set()
    if RUN_STRATEGY in ("all", "musashi"):
        for iv in MUSASHI_INTERVALS:
            for p in PERIODS: needed.add((iv, p))
    if RUN_STRATEGY in ("all", "raijin"):
        for iv in RAIJIN_INTERVALS:
            for p in PERIODS: needed.add((iv, p))
    if RUN_STRATEGY in ("all", "atr"):
        for iv in ATR_INTERVALS:
            for p in PERIODS: needed.add((iv, p))

    print(f"\nFetching {len(needed)} datasets (capital=Rs{INITIAL_CAPITAL:,.0f}, periods={PERIODS})...")
    dfs = {}
    for (interval, period) in sorted(needed):
        try:
            dfs[(interval, period)] = _load_or_fetch(engine, "NIFTY", period, interval)
        except Exception as e:
            print(f"  ERROR {interval} {period}: {e}")

    all_results = {}

    if RUN_STRATEGY in ("all", "musashi"):
        musashi_rows = run_musashi(engine, dfs)
        _print_table(musashi_rows, "MUSASHI — sorted by net return %")
        all_results["musashi"] = musashi_rows

    if RUN_STRATEGY in ("all", "raijin"):
        raijin_rows = run_raijin(engine, dfs)
        _print_table(raijin_rows, "RAIJIN — sorted by net return %")
        all_results["raijin"] = raijin_rows

    if RUN_STRATEGY in ("all", "atr"):
        atr_rows = run_atr(engine, dfs)
        _print_table(atr_rows, "ATR INTRADAY — sorted by net return %")
        all_results["atr"] = atr_rows

    def _clean(rows):
        return [{k: (None if isinstance(v, float) and v == float("inf") else v)
                 for k, v in r.items()} for r in rows]

    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtest_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({k: _clean(v) for k, v in all_results.items()}, f, indent=2)
    print(f"\nFull results saved to {out_path}")
