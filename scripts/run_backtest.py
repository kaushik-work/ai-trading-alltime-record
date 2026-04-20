"""
Backtest permutation runner — ATR Intraday (27 combos)
All timeframes x All periods (30d, 60d, 90d)

Usage:
    python scripts/run_backtest.py                     # default: 60d, 50K capital
    python scripts/run_backtest.py --period 30d        # 30-day window
    python scripts/run_backtest.py --period all        # 30d + 60d + 90d
    python scripts/run_backtest.py --capital 70000
    python scripts/run_backtest.py --no-cache          # force re-fetch from Angel One
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
parser.add_argument("--strategy", type=str,   default="atr",
                    help="atr  (default atr)")
parser.add_argument("--no-cache", action="store_true",
                    help="Force re-fetch data even if cache exists")
parser.add_argument("--monthly",  action="store_true",
                    help="Run month-on-month breakdown (Dec 2025 – current) at optimal config")
args = parser.parse_args()

INITIAL_CAPITAL = args.capital
PERIODS         = ["30d", "60d", "90d"] if args.period == "all" else [args.period]
FORCE_REFETCH   = args.no_cache
RUN_STRATEGY    = args.strategy.lower()
RUN_MONTHLY     = args.monthly

# ── Permutation grids ─────────────────────────────────────────────────────────

# 5m is the proven timeframe (backtest 2026-03-29).
# Optimal config: score=6, R:R=2.0, risk=2% → 51.5% net, 45.2% WR, 13.1% DD (60d)
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

# ── Monthly breakdown ─────────────────────────────────────────────────────────

# Optimal config (backtest-validated 2026-03-29)
MONTHLY_CONFIG = {"min_score": 6, "rr_ratio": 2.0, "risk_pct": 2.0}

def run_monthly(engine):
    """Fetch ~150d of 5m data and break results down by calendar month."""
    import pandas as pd

    print("\nFetching 150d of 5m NIFTY data for monthly breakdown...")
    cache_file = os.path.join(CACHE_DIR, "NIFTY_5m_150d.csv")
    os.makedirs(CACHE_DIR, exist_ok=True)

    if os.path.exists(cache_file) and not FORCE_REFETCH:
        df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
        if "_date" not in df.columns:
            df["_date"] = df.index.date
        print(f"  (cache) NIFTY 5m 150d: {len(df)} bars")
    else:
        df = engine.fetch_data("NIFTY", period="150d", interval="5m")
        df.to_csv(cache_file)
        print(f"  (fetch) NIFTY 5m 150d: {len(df)} bars — cached")

    # Group by year-month
    df.index = pd.to_datetime(df.index)
    df["_ym"] = df.index.to_period("M")
    months = sorted(df["_ym"].unique())

    print(f"\nRunning ATR Intraday (score={MONTHLY_CONFIG['min_score']}, "
          f"R:R={MONTHLY_CONFIG['rr_ratio']}, risk={MONTHLY_CONFIG['risk_pct']}%) "
          f"across {len(months)} months...\n")

    rows = []
    equity = INITIAL_CAPITAL
    for ym in months:
        month_df = df[df["_ym"] == ym].drop(columns=["_ym"])
        if "_date" not in month_df.columns:
            month_df = month_df.copy()
            month_df["_date"] = month_df.index.date
        if len(month_df) < 20:
            continue  # skip months with too few bars (partial data)
        try:
            result = engine.run(
                symbol="NIFTY", interval="5m", period="",
                min_score=MONTHLY_CONFIG["min_score"],
                rr_ratio=MONTHLY_CONFIG["rr_ratio"],
                risk_pct=MONTHLY_CONFIG["risk_pct"],
                _df=month_df,
            )
            trades    = result["trades"]
            final_eq  = result["final_equity"]
            net_pct   = round((final_eq - equity) / equity * 100, 1)
            rows.append({
                "month":    str(ym),
                "trades":   len(trades),
                "win_rate": _win_rate(trades),
                "pf":       _pf(trades),
                "max_dd":   _max_dd(result["equity_curve"]),
                "net_pct":  net_pct,
                "start_eq": round(equity, 0),
                "end_eq":   round(final_eq, 0),
            })
            equity = final_eq  # compound month to month
            print(".", end="", flush=True)
        except Exception as e:
            print(f"\n  [SKIP] {ym}: {e}")

    print(f"\n\n{'='*80}")
    print("  ATR INTRADAY — Month-on-Month Results (compounding, 5m)")
    print(f"{'='*80}")
    hdr = f"{'Month':>8}  {'#Tr':>4}  {'Win%':>6}  {'PF':>5}  {'DD%':>5}  {'Net%':>6}  {'StartEq':>9}  {'EndEq':>9}"
    print(hdr)
    print('-' * 80)
    for r in rows:
        pf_str = f"{r['pf']:.2f}" if r["pf"] != float("inf") else ">99"
        flag   = " ***" if r["net_pct"] > 0 and r["win_rate"] >= 35 and r["max_dd"] <= 20 else ""
        print(
            f"{r['month']:>8}  {r['trades']:>4}  {r['win_rate']:>6.1f}  {pf_str:>5}  "
            f"{r['max_dd']:>5.1f}  {r['net_pct']:>6.1f}  "
            f"{r['start_eq']:>9,.0f}  {r['end_eq']:>9,.0f}{flag}"
        )

    total_net = round((equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 1)
    print(f"\n  Starting capital : Rs{INITIAL_CAPITAL:>10,.0f}")
    print(f"  Final equity     : Rs{equity:>10,.0f}  ({total_net:+.1f}% total)")
    print(f"\n*** = net positive + win rate >=35% + max drawdown <=20%")
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    engine = BacktestEngine(initial_capital=INITIAL_CAPITAL)

    if RUN_MONTHLY:
        run_monthly(engine)
        sys.exit(0)

    # Collect all needed intervals
    needed = set()
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

    if RUN_STRATEGY in ("atr", "all"):
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
