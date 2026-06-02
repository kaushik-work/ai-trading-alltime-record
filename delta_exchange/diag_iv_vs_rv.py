"""
IV vs RV Diagnostic -- Delta Exchange BTC Options
==================================================
Premise check: does the BTC options market show a structural gap between
implied volatility (from option mark prices) and realized volatility (from
perp returns) that's wide enough to harvest after costs?

For each hour in the data window:
  1. Pick the front-week expiry (settlement_dt within 1–10 days ahead).
  2. Find the ATM call+put pair (strike closest to spot).
  3. Back out IV from each via Newton on Black-Scholes (r=0).
  4. Average call/put IV → ATM IV.
  5. Compute 7-day annualized realized vol from BTCUSD perp 1m log-returns.
  6. Vol premium = IV - RV.

Output:
  - stats per day: avg IV, avg RV, avg spread, hit rate of RV > IV
  - identifies regimes where long-straddle (RV > IV) makes money in principle
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
import warnings
from pathlib import Path
import numpy as np
import pandas as pd

# silence the expected nanmean-of-all-NaN noise — semantically meaningful (skips that timestamp)
warnings.filterwarnings("ignore", category=RuntimeWarning, message="Mean of empty slice")

DATA = Path(__file__).parent / "data"

# ── Black-Scholes (r=0, no div) ───────────────────────────────────────────────
def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S, K, T, sigma):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _phi(d1) - K * _phi(d2)


def bs_put(S, K, T, sigma):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * _phi(-d2) - S * _phi(-d1)


def implied_vol(price, S, K, T, is_call, lo=0.01, hi=5.0, tol=1e-4, max_iter=60):
    """Bisection IV solver — robust where Newton occasionally diverges on far-OTM."""
    if T <= 0 or price <= 0:
        return float("nan")
    intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
    if price < intrinsic - 1e-3:
        return float("nan")
    f = bs_call if is_call else bs_put
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        if f(S, K, T, mid) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            return 0.5 * (lo + hi)
    return 0.5 * (lo + hi)


# ── Data loading ──────────────────────────────────────────────────────────────
def load_perp_mark() -> pd.DataFrame:
    df = pd.read_csv(DATA / "perp" / "BTCUSD_mark_1m.csv")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df[["timestamp", "close"]].set_index("timestamp").sort_index()


def parse_symbol(sym: str):
    """C-BTC-71400-310526 → ('C', 71400, datetime(2026, 5, 31))"""
    parts = sym.split("-")
    side = parts[0]
    strike = int(parts[2])
    dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
    expiry = pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")
    return side, strike, expiry


def load_options_index() -> pd.DataFrame:
    """All option contracts we have MARK files for, parsed."""
    rows = []
    for p in sorted((DATA / "options").glob("*_mark_1h.csv")):
        sym = p.name.replace("_mark_1h.csv", "")
        try:
            side, strike, expiry = parse_symbol(sym)
        except Exception:
            continue
        rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": expiry, "path": p})
    return pd.DataFrame(rows)


def load_option_mark(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df[["timestamp", "close"]].set_index("timestamp").sort_index()


# ── Diagnostic ────────────────────────────────────────────────────────────────
def realized_vol(returns: pd.Series, lookback_min: int = 7 * 24 * 60) -> pd.Series:
    """Annualized realized vol (365×24×60 minutes/yr) over rolling window."""
    rv = returns.rolling(lookback_min).std() * math.sqrt(365 * 24 * 60)
    return rv


def run() -> None:
    print("Loading perp mark data...")
    perp = load_perp_mark()
    perp["ret"] = np.log(perp["close"]).diff()
    perp["rv7d"] = realized_vol(perp["ret"], 7 * 24 * 60)
    print(f"  perp: {len(perp):,} 1m bars, {perp.index[0]} → {perp.index[-1]}")

    print("Indexing option contracts...")
    opts = load_options_index()
    print(f"  options: {len(opts):,} contracts; expiries: "
          f"{sorted(set(opts['expiry'].dt.date))}")

    if opts.empty:
        print("No option contracts found.")
        return

    # Sample at one timestamp per hour to keep this fast
    sample_idx = perp.index[(perp.index.minute == 0) & (perp.index.second == 0)]
    rows = []
    print(f"Computing IV at {len(sample_idx):,} hourly timestamps...")
    for t in sample_idx:
        spot = perp["close"].get(t)
        rv   = perp["rv7d"].get(t)
        if not (isinstance(spot, float) and spot > 0) or not (isinstance(rv, float) and rv > 0):
            continue
        # front-week expiry: smallest expiry > t with at least 24h to go
        eligible = opts[(opts["expiry"] > t + pd.Timedelta(hours=24)) &
                        (opts["expiry"] <= t + pd.Timedelta(days=14))]
        if eligible.empty:
            continue
        front_exp = eligible["expiry"].min()
        front = eligible[eligible["expiry"] == front_exp]
        # find ATM strike
        atm_strike = front.iloc[(front["strike"] - spot).abs().argsort()[:1]]["strike"].iloc[0]
        atm = front[front["strike"] == atm_strike]

        call_iv = put_iv = float("nan")
        for _, r in atm.iterrows():
            mark = load_option_mark(r["path"])
            opt_px = mark["close"].get(t)
            if not (isinstance(opt_px, float) and opt_px > 0):
                continue
            T_yrs = (front_exp - t).total_seconds() / (365 * 86400)
            iv = implied_vol(opt_px, spot, atm_strike, T_yrs, is_call=(r["side"] == "C"))
            if r["side"] == "C":
                call_iv = iv
            else:
                put_iv = iv
        atm_iv = np.nanmean([call_iv, put_iv])
        if not np.isfinite(atm_iv):
            continue
        rows.append({
            "t": t, "spot": spot, "rv7d": rv,
            "strike": atm_strike, "expiry": front_exp,
            "tt_days": (front_exp - t).total_seconds() / 86400,
            "call_iv": call_iv, "put_iv": put_iv, "atm_iv": atm_iv,
            "vol_premium": atm_iv - rv,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        print("No IV samples computed — check option data coverage vs perp window.")
        return
    df["t"] = pd.to_datetime(df["t"], utc=True)
    df = df.set_index("t")

    print()
    print("=" * 70)
    print("  IV vs RV diagnostic — BTC ATM front-week straddle")
    print("=" * 70)
    print(f"  samples              : {len(df):,}")
    print(f"  spot range           : ${df['spot'].min():,.0f} → ${df['spot'].max():,.0f}")
    print(f"  avg time-to-expiry   : {df['tt_days'].mean():.1f} days")
    print(f"  avg ATM IV           : {df['atm_iv'].mean()*100:.1f}%")
    print(f"  avg 7d RV            : {df['rv7d'].mean()*100:.1f}%")
    print(f"  avg vol premium (IV-RV): {df['vol_premium'].mean()*100:+.2f} pp")
    print(f"  median vol premium   : {df['vol_premium'].median()*100:+.2f} pp")
    print(f"  hours RV > IV        : {(df['vol_premium'] < 0).mean()*100:.1f}%   "
          f"(long-gamma opportunity)")
    print(f"  hours IV > RV + 5pp  : {(df['vol_premium'] > 0.05).mean()*100:.1f}%  "
          f"(short-gamma opportunity)")
    print("=" * 70)

    daily = df.resample("1D").agg(
        spot=("spot", "mean"), rv=("rv7d", "mean"), iv=("atm_iv", "mean"),
        prem=("vol_premium", "mean"),
    )
    print("\n  Daily snapshot (last 14 days of data):")
    print(f"  {'Date':<12} {'Spot':>10} {'RV7d':>7} {'ATM IV':>7} {'Premium':>9}")
    for dt, row in daily.tail(14).iterrows():
        print(f"  {dt.strftime('%Y-%m-%d'):<12} ${row['spot']:>9,.0f} "
              f"{row['rv']*100:>6.1f}% {row['iv']*100:>6.1f}% {row['prem']*100:>+7.2f}pp")
    print()

    out_path = DATA / "diag_iv_vs_rv.csv"
    df.to_csv(out_path)
    print(f"  full series saved → {out_path.relative_to(DATA.parent)}")


if __name__ == "__main__":
    run()
