"""
Backtest: Black-Scholes IV mispricing — "buy options when they're cheap".

The core Wall Street idea: an option's Black-Scholes-implied volatility (IV)
is what the *market* expects the underlying to move. Realised volatility (RV)
is what the underlying *actually* did. When IV << RV, options are
systematically underpriced — buying them has positive expectancy because the
underlying's real move outpaces the option's priced-in move.

Strategy under test:
  1. Every 5-min bar, compute ATM CE/PE implied vol via Black-Scholes inversion.
  2. Compute realised vol from the last N bars of spot returns, annualised.
  3. Compute IV/RV ratio.
  4. When IV/RV < THRESHOLD (default 0.85), buy ITM-50 CE.
  5. Standard SL/TP exit (₹10 SL, RR 2.25 → TP at +₹22.50) or EOD 15:20.
  6. Same risk caps as the shadow executor: 4/day, ₹2,000/day per-strategy
     cap, ₹3,500/day aggregate cap.

Run:
  python scripts/backtest_iv_mispricing.py
  python scripts/backtest_iv_mispricing.py --threshold 0.80
  python scripts/backtest_iv_mispricing.py --no-loss-caps
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: F401
import numpy as np
import pandas as pd
from scipy.stats import norm
from scipy.optimize import brentq

from core import mongo  # noqa: E402

LOT_SIZE = 65
EOD_LIMIT = dtime(15, 20)
RISK_FREE_RATE = 0.07     # India 1-year G-Sec ~7%
TRADING_DAYS_PER_YEAR = 252
BARS_PER_DAY = 75         # 5-min bars between 09:15 and 15:30
SL_DIST = 10.0
RR = 2.25
PER_STRAT_LOSS_CAP = 2_000
DAILY_AGG_LOSS_CAP = 3_500
MAX_TRADES_PER_DAY = 5
RV_WINDOW_BARS = 12        # 12 × 5min = 60 min trailing realised-vol window


# ── Black-Scholes ────────────────────────────────────────────────────────────

def _bs_call_price(S, K, T, r, sigma):
    """European call price under Black-Scholes. T in years, sigma annualised."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)


def _bs_put_price(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def _implied_vol(market_price, S, K, T, r, option_type):
    """Invert Black-Scholes to recover IV. Returns None on failure."""
    if T <= 0 or market_price <= 0:
        return None
    intrinsic = max(0.0, S - K) if option_type == "CE" else max(0.0, K - S)
    if market_price <= intrinsic:
        # Trading at intrinsic — IV ~0 or arb. Skip.
        return None
    f = _bs_call_price if option_type == "CE" else _bs_put_price

    def diff(sigma):
        return f(S, K, T, r, sigma) - market_price

    try:
        # Bracket: sigma in (0.001, 5.0) — covers everything from quiet to crash
        return brentq(diff, 1e-3, 5.0, maxiter=100, xtol=1e-4)
    except (ValueError, RuntimeError):
        return None


# ── Loaders ─────────────────────────────────────────────────────────────────

def _load_snapshots(db, symbol: str) -> pd.DataFrame:
    print(f"Loading option_snapshots for {symbol} ...", flush=True)
    cur = db.option_snapshots.find(
        {"symbol": symbol},
        projection={"_id": 0, "date": 1, "timestamp": 1, "strike": 1,
                    "option_type": 1, "ltp": 1, "spot": 1, "expiry": 1},
    )
    df = pd.DataFrame(list(cur))
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["ts", "ltp", "spot"])
    df["strike"] = df["strike"].astype(int)
    df["ltp"]    = df["ltp"].astype(float)
    df["spot"]   = df["spot"].astype(float)
    print(f"  loaded {len(df):,} rows across {df['date'].nunique()} days", flush=True)
    return df


def _build_bar_table(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (date, ts): spot, atm_strike, atm_ce_ltp, atm_pe_ltp, expiry."""
    rows = []
    for (date_str, ts), g in df.groupby(["date", "ts"], sort=True):
        spot = float(g["spot"].iloc[0])
        atm = int(round(spot / 50)) * 50
        ce = g[(g["strike"] == atm) & (g["option_type"] == "CE")]["ltp"].mean()
        pe = g[(g["strike"] == atm) & (g["option_type"] == "PE")]["ltp"].mean()
        if pd.isna(ce) or pd.isna(pe):
            continue
        expiry_str = g["expiry"].iloc[0]
        try:
            expiry_dt = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        rows.append({"date": date_str, "ts": ts, "spot": spot,
                     "atm_strike": atm, "atm_ce_ltp": float(ce),
                     "atm_pe_ltp": float(pe), "expiry": expiry_dt})
    return pd.DataFrame(rows).sort_values(["date", "ts"]).reset_index(drop=True)


def _compute_features(bars: pd.DataFrame) -> pd.DataFrame:
    """Add iv_ce, iv_pe, iv_atm, rv_60m, iv_rv_ratio columns."""
    bars = bars.copy()
    iv_ce_list, iv_pe_list = [], []

    for r in bars.itertuples(index=False):
        bar_date = datetime.strptime(r.date, "%Y-%m-%d").date()
        days_to_expiry = max(1, (r.expiry - bar_date).days)
        T = days_to_expiry / TRADING_DAYS_PER_YEAR
        iv_ce = _implied_vol(r.atm_ce_ltp, r.spot, r.atm_strike, T,
                              RISK_FREE_RATE, "CE")
        iv_pe = _implied_vol(r.atm_pe_ltp, r.spot, r.atm_strike, T,
                              RISK_FREE_RATE, "PE")
        iv_ce_list.append(iv_ce)
        iv_pe_list.append(iv_pe)

    bars["iv_ce"] = iv_ce_list
    bars["iv_pe"] = iv_pe_list
    # ATM IV = average of CE and PE IV (smoother estimate)
    bars["iv_atm"] = bars[["iv_ce", "iv_pe"]].mean(axis=1, skipna=True)

    # Realised vol: rolling stddev of log returns × √annualisation
    log_returns = np.log(bars["spot"] / bars["spot"].shift(1))
    bars["log_ret"] = log_returns
    bars_per_year = TRADING_DAYS_PER_YEAR * BARS_PER_DAY
    bars["rv_60m"] = (log_returns.groupby(bars["date"])
                       .rolling(RV_WINDOW_BARS, min_periods=6).std()
                       .reset_index(level=0, drop=True)
                       * math.sqrt(bars_per_year))

    bars["iv_rv_ratio"] = bars["iv_atm"] / bars["rv_60m"]

    # IV momentum — change in ATM IV over last 3 bars (15 min), within-day
    bars["iv_mom3"] = bars.groupby("date")["iv_atm"].diff(3)

    # IV percentile rank vs last 5 trading days' distribution
    dates_sorted = sorted(bars["date"].unique())
    iv_rank = []
    for i, r in enumerate(bars.itertuples(index=False)):
        # Trailing 5-day window of IV values (strictly prior days)
        idx = dates_sorted.index(r.date)
        if idx < 5:
            iv_rank.append(np.nan)
            continue
        prior_dates = set(dates_sorted[idx-5:idx])
        sample = bars.loc[bars["date"].isin(prior_dates), "iv_atm"].dropna().values
        if len(sample) < 30 or pd.isna(r.iv_atm):
            iv_rank.append(np.nan)
        else:
            iv_rank.append(float((sample < r.iv_atm).mean()))
    bars["iv_rank"] = iv_rank

    return bars


# ── Backtest ────────────────────────────────────────────────────────────────

def _walk_forward(series, entry_dt, entry_premium, sl_dist, rr):
    sl_price = round(entry_premium - sl_dist, 1)
    tp_price = round(entry_premium + sl_dist * rr, 1)
    for dt, ltp in series:
        if dt <= entry_dt:
            continue
        if dt.time() >= EOD_LIMIT:
            return dt, ltp, "EOD"
        if ltp <= sl_price:
            return dt, sl_price, "SL"
        if ltp >= tp_price:
            return dt, tp_price, "TP"
    if series and series[-1][0] > entry_dt:
        return series[-1][0], series[-1][1], "EOD-last"
    return entry_dt, entry_premium, "no-data"


def _index_premium_series(df: pd.DataFrame) -> dict:
    by_key = defaultdict(list)
    for r in df.itertuples(index=False):
        by_key[(r.date, int(r.strike), r.option_type)].append(
            (r.ts.to_pydatetime(), float(r.ltp))
        )
    for k in by_key:
        by_key[k].sort()
    return by_key


def _pick_strike_for_target_premium(df: pd.DataFrame, bar_date: str,
                                     bar_ts, atm: int,
                                     target_premium: float,
                                     side: str = "CE") -> int:
    """Walk strikes from ATM downward (for CE) until the option's LTP at this
    bar is closest to `target_premium`. Returns the chosen strike.

    Searches the full chain (ATM-400 to ATM+400 in 50-pt steps).
    """
    candidates = df[(df["date"] == bar_date) &
                    (df["ts"] == bar_ts) &
                    (df["option_type"] == side)]
    if candidates.empty:
        return atm - 50   # fallback to ITM-50
    best_strike = atm
    best_diff   = float("inf")
    for r in candidates.itertuples(index=False):
        diff = abs(float(r.ltp) - target_premium)
        if diff < best_diff:
            best_diff   = diff
            best_strike = int(r.strike)
    return best_strike


def _evaluate_mode(r, mode: str, threshold: float) -> bool:
    """Return True if this bar should fire under the chosen mode."""
    if mode == "iv_rv_low":
        v = r.iv_rv_ratio
        return not pd.isna(v) and v < threshold
    if mode == "iv_rv_high":
        v = r.iv_rv_ratio
        return not pd.isna(v) and v > threshold
    if mode == "iv_mom3":
        v = r.iv_mom3
        return not pd.isna(v) and v > threshold
    if mode == "iv_rank_low":
        v = r.iv_rank
        return not pd.isna(v) and v < threshold
    if mode == "iv_rank_high":
        v = r.iv_rank
        return not pd.isna(v) and v > threshold
    raise ValueError(f"unknown mode: {mode}")


def _run_backtest(bars: pd.DataFrame, premium_series: dict,
                  threshold: float, sl_dist: float, rr: float,
                  apply_loss_caps: bool, mode: str,
                  df_raw: pd.DataFrame | None = None,
                  target_premium: float | None = None):
    trades, refused = [], defaultdict(int)
    open_until: dict = {}
    state = {"strat_pnl_today": defaultdict(float),
             "count_today":     defaultdict(int)}

    for r in bars.itertuples(index=False):
        if not _evaluate_mode(r, mode, threshold):
            continue

        # Reuse-the-bar guard: if a trade is still open today, skip
        if r.date in open_until and r.ts.to_pydatetime() < open_until[r.date]:
            continue

        # Caps
        if state["count_today"][r.date] >= MAX_TRADES_PER_DAY:
            refused["4/day cap"] += 1; continue
        if apply_loss_caps and state["strat_pnl_today"][r.date] <= -PER_STRAT_LOSS_CAP:
            refused["loss cap"] += 1; continue

        # Strike: closest to target premium, or fallback ITM-50
        if target_premium is not None and df_raw is not None:
            chosen_strike = _pick_strike_for_target_premium(
                df_raw, r.date, r.ts, int(r.atm_strike), target_premium, "CE"
            )
        else:
            chosen_strike = int(r.atm_strike) - 50
        key = (r.date, chosen_strike, "CE")
        series = premium_series.get(key)
        if not series:
            refused["no premium data"] += 1; continue
        entry_dt = entry_premium = None
        for dt, ltp in series:
            if dt >= r.ts.to_pydatetime():
                entry_dt = dt; entry_premium = ltp; break
        if entry_premium is None:
            continue

        exit_dt, exit_premium, reason = _walk_forward(series, entry_dt,
                                                       entry_premium,
                                                       sl_dist, rr)
        pnl = round((exit_premium - entry_premium) * LOT_SIZE, 2)
        trades.append({
            "date":          r.date,
            "entry_dt":      entry_dt,
            "exit_dt":       exit_dt,
            "strike":        chosen_strike,
            "iv_atm":        round(r.iv_atm, 4) if not pd.isna(r.iv_atm) else None,
            "rv_60m":        round(r.rv_60m, 4) if not pd.isna(r.rv_60m) else None,
            "iv_rv_ratio":   round(r.iv_rv_ratio, 3) if not pd.isna(r.iv_rv_ratio) else None,
            "iv_mom3":       round(r.iv_mom3, 4) if not pd.isna(r.iv_mom3) else None,
            "iv_rank":       round(r.iv_rank, 3) if not pd.isna(r.iv_rank) else None,
            "entry_premium": round(entry_premium, 2),
            "exit_premium":  round(exit_premium, 2),
            "reason":        reason,
            "pnl":           pnl,
        })
        open_until[r.date]               = exit_dt
        state["count_today"][r.date]    += 1
        state["strat_pnl_today"][r.date] += pnl

    return trades, refused


def _summarise(trades, refused, label):
    print(f"\n{'='*72}\n=== {label} ===\n{'='*72}")
    if not trades:
        print("  no trades")
        return
    pnls   = [t["pnl"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gw     = sum(wins); gl = abs(sum(losses))
    pf = (gw / gl) if gl > 0 else float("inf")
    print(f"  Trades: {len(trades)}   WR: {len(wins)/len(pnls)*100:.1f}%   "
          f"PF: {pf:.2f}   Net: Rs {sum(pnls):+,.0f}   "
          f"Exp: Rs {sum(pnls)/len(pnls):+,.0f}/trade")

    by_day = defaultdict(float)
    for t in trades: by_day[t["date"]] += t["pnl"]
    print(f"\n  Per-day P&L:")
    cum = 0.0
    for d, p in sorted(by_day.items()):
        cum += p
        print(f"    {d}   Rs {p:>+8,.0f}   (cum Rs {cum:>+8,.0f})")

    reasons = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for t in trades:
        reasons[t["reason"]]["n"]   += 1
        reasons[t["reason"]]["pnl"] += t["pnl"]
    print(f"\n  Exit reasons:")
    for r, v in sorted(reasons.items(), key=lambda x: -x[1]["pnl"]):
        print(f"    {r:10}  {v['n']:>3}  pnl Rs {v['pnl']:+,.0f}")

    if refused:
        print(f"\n  Refused entries:")
        for r, c in sorted(refused.items(), key=lambda x: -x[1]):
            print(f"    {r:>20}  {c}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="iv_rv_low",
                    choices=["iv_rv_low", "iv_rv_high", "iv_mom3",
                             "iv_rank_low", "iv_rank_high"],
                    help="Signal mode (default iv_rv_low)")
    ap.add_argument("--threshold", type=float, default=0.85,
                    help="Mode-dependent threshold; see code (default 0.85 for iv_rv_low)")
    ap.add_argument("--sl",        type=float, default=10.0)
    ap.add_argument("--rr",        type=float, default=2.25)
    ap.add_argument("--no-loss-caps", action="store_true")
    ap.add_argument("--symbol",         default="NIFTY")
    ap.add_argument("--target-premium", type=float, default=170.0,
                    help="Target option premium for strike selection (default 170)")
    ap.add_argument("--csv",       default=None)
    args = ap.parse_args()

    db = mongo.get_db()
    if db is None:
        print("Mongo unreachable"); sys.exit(1)
    df = _load_snapshots(db, args.symbol)
    if df.empty:
        print("No data"); return
    bars = _build_bar_table(df)
    print(f"  built {len(bars):,} bars across {bars['date'].nunique()} days")
    bars = _compute_features(bars)
    print(f"  IV computable on {bars['iv_atm'].notna().sum():,} bars "
          f"({bars['iv_atm'].notna().mean()*100:.1f}%)")
    print(f"  IV summary:    median {bars['iv_atm'].median():.3f}, "
          f"P5 {bars['iv_atm'].quantile(0.05):.3f}, "
          f"P95 {bars['iv_atm'].quantile(0.95):.3f}")
    print(f"  RV summary:    median {bars['rv_60m'].median():.3f}, "
          f"P5 {bars['rv_60m'].quantile(0.05):.3f}, "
          f"P95 {bars['rv_60m'].quantile(0.95):.3f}")
    print(f"  IV/RV summary: median {bars['iv_rv_ratio'].median():.3f}, "
          f"P5 {bars['iv_rv_ratio'].quantile(0.05):.3f}, "
          f"P95 {bars['iv_rv_ratio'].quantile(0.95):.3f}")

    premium_series = _index_premium_series(df)

    trades, refused = _run_backtest(bars, premium_series,
                                     threshold=args.threshold,
                                     sl_dist=args.sl, rr=args.rr,
                                     apply_loss_caps=not args.no_loss_caps,
                                     mode=args.mode,
                                     df_raw=df,
                                     target_premium=args.target_premium)
    _summarise(trades, refused,
               f"{args.mode} threshold={args.threshold} target_premium=Rs{args.target_premium} (BUY CE)")

    if args.csv and trades:
        out = pd.DataFrame(trades)
        out["entry_dt"] = out["entry_dt"].astype(str)
        out["exit_dt"]  = out["exit_dt"].astype(str)
        out.to_csv(args.csv, index=False)
        print(f"\nLedger written to {args.csv}")


if __name__ == "__main__":
    main()
