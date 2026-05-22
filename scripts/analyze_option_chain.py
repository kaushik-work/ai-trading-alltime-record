"""
Option-chain feature discovery.

Loads every 5-min NIFTY option snapshot from Mongo (option_snapshots), builds
per-bar aggregate features, then correlates each feature with the forward
NIFTY move at +5m / +15m / +30m horizons.

Goal: find any single feature (or combo) that shows |corr| > 0.15 with future
returns on the data we already have. If something pops, it becomes the next
backtested entry signal. If nothing pops, we need more data before building
strategies on top of this stream.

Features computed per (date, timestamp) bar:
  pcr_oi          total PE OI / total CE OI                          (level)
  pcr_vol         total PE volume / total CE volume                  (level)
  d_pcr_oi        change in pcr_oi vs prior bar                      (flow)
  d_ce_oi         change in total CE OI vs prior bar                 (flow)
  d_pe_oi         change in total PE OI vs prior bar                 (flow)
  atm_straddle    ATM CE premium + ATM PE premium                    (IV proxy)
  d_straddle      change in atm_straddle vs prior bar                (IV flow)
  call_wall_dist  (call_wall_strike - spot) / spot                   (resistance)
  put_wall_dist   (spot - put_wall_strike)  / spot                   (support)
  max_pain_dist   (max_pain_strike - spot) / spot                    (magnet)
  oi_skew         (sum_oi_above_spot - sum_oi_below_spot) / total    (lean)

Usage:
  python scripts/analyze_option_chain.py
  python scripts/analyze_option_chain.py --symbol NIFTY --csv out.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: F401  -- triggers load_dotenv()
import numpy as np
import pandas as pd
from core import mongo  # noqa: E402


def _load_snapshots(db, symbol: str) -> pd.DataFrame:
    print(f"Loading option_snapshots for {symbol} ...", flush=True)
    cur = db.option_snapshots.find(
        {"symbol": symbol},
        projection={"_id": 0, "date": 1, "timestamp": 1, "strike": 1,
                    "option_type": 1, "ltp": 1, "oi": 1, "volume": 1, "spot": 1},
    )
    df = pd.DataFrame(list(cur))
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["ts", "ltp", "spot"])
    df["strike"] = df["strike"].astype(int)
    df["oi"]     = df["oi"].fillna(0).astype(int)
    df["volume"] = df["volume"].fillna(0).astype(int)
    df["ltp"]    = df["ltp"].astype(float)
    df["spot"]   = df["spot"].astype(float)
    print(f"  loaded {len(df):,} rows across {df['date'].nunique()} days", flush=True)
    return df


def _max_pain(bar: pd.DataFrame) -> int:
    """Strike where total option writer P&L (sum of intrinsic × OI) is minimised."""
    strikes = sorted(bar["strike"].unique())
    ce = bar[bar["option_type"] == "CE"].set_index("strike")["oi"]
    pe = bar[bar["option_type"] == "PE"].set_index("strike")["oi"]
    losses = {}
    for k in strikes:
        ce_loss = sum(max(0, k - s) * ce.get(s, 0) for s in strikes)
        pe_loss = sum(max(0, s - k) * pe.get(s, 0) for s in strikes)
        losses[k] = ce_loss + pe_loss
    return min(losses, key=losses.get)


def _bar_features(bar: pd.DataFrame) -> dict:
    """Aggregate features for one (date, timestamp) bar."""
    spot = bar["spot"].iloc[0]
    ce = bar[bar["option_type"] == "CE"]
    pe = bar[bar["option_type"] == "PE"]
    if ce.empty or pe.empty:
        return {}

    total_ce_oi  = ce["oi"].sum()
    total_pe_oi  = pe["oi"].sum()
    total_ce_vol = ce["volume"].sum()
    total_pe_vol = pe["volume"].sum()

    # ATM strike = strike with smallest |strike - spot|
    atm_strike = int(bar.iloc[(bar["strike"] - spot).abs().argsort()[:1]]["strike"].iloc[0])
    atm_ce = ce[ce["strike"] == atm_strike]["ltp"].mean()
    atm_pe = pe[pe["strike"] == atm_strike]["ltp"].mean()
    straddle = (atm_ce or 0) + (atm_pe or 0)

    call_wall = int(ce.loc[ce["oi"].idxmax(), "strike"]) if total_ce_oi > 0 else atm_strike
    put_wall  = int(pe.loc[pe["oi"].idxmax(), "strike"]) if total_pe_oi > 0 else atm_strike

    mp = _max_pain(bar)

    oi_above = bar[bar["strike"] > spot]["oi"].sum()
    oi_below = bar[bar["strike"] < spot]["oi"].sum()
    total_oi = oi_above + oi_below

    return {
        "spot":            spot,
        "atm_strike":      atm_strike,
        "pcr_oi":          (total_pe_oi / total_ce_oi) if total_ce_oi else np.nan,
        "pcr_vol":         (total_pe_vol / total_ce_vol) if total_ce_vol else np.nan,
        "total_ce_oi":     total_ce_oi,
        "total_pe_oi":     total_pe_oi,
        "atm_straddle":    straddle,
        "call_wall_dist":  (call_wall - spot) / spot,
        "put_wall_dist":   (spot - put_wall) / spot,
        "max_pain_dist":   (mp - spot) / spot,
        "oi_skew":         ((oi_above - oi_below) / total_oi) if total_oi else np.nan,
    }


def _build_bar_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (date, ts), g in df.groupby(["date", "ts"], sort=True):
        feats = _bar_features(g)
        if not feats:
            continue
        feats["date"] = date
        feats["ts"]   = ts
        rows.append(feats)
    out = pd.DataFrame(rows).sort_values(["date", "ts"]).reset_index(drop=True)

    # Flow features (deltas within the same day)
    for col in ("pcr_oi", "total_ce_oi", "total_pe_oi", "atm_straddle"):
        out[f"d_{col}"] = out.groupby("date")[col].diff()

    # Rename for shorter labels in the correlation table
    out = out.rename(columns={
        "d_pcr_oi":       "d_pcr_oi",
        "d_total_ce_oi":  "d_ce_oi",
        "d_total_pe_oi":  "d_pe_oi",
        "d_atm_straddle": "d_straddle",
    })
    return out


def _add_forward_returns(bars: pd.DataFrame, horizons_bars=(1, 3, 6)) -> pd.DataFrame:
    """Forward spot return at +N bars (within same day)."""
    for n in horizons_bars:
        fwd = bars.groupby("date")["spot"].shift(-n)
        bars[f"fwd_{n*5}m"] = (fwd - bars["spot"]) / bars["spot"]
    return bars


def _corr_table(bars: pd.DataFrame, features: list, horizons: list) -> pd.DataFrame:
    rows = []
    for f in features:
        row = {"feature": f}
        for h in horizons:
            sub = bars[[f, h]].dropna()
            if len(sub) < 30:
                row[h] = np.nan
                continue
            row[h] = sub[f].corr(sub[h])
        row["n"] = len(bars[[f] + horizons].dropna())
        rows.append(row)
    return pd.DataFrame(rows)


def _quintile_breakdown(bars: pd.DataFrame, feature: str, target: str) -> pd.DataFrame:
    sub = bars[[feature, target]].dropna()
    if len(sub) < 50:
        return pd.DataFrame()
    sub = sub.copy()
    sub["bucket"] = pd.qcut(sub[feature], 5, labels=["Q1(low)", "Q2", "Q3", "Q4", "Q5(high)"],
                            duplicates="drop")
    g = sub.groupby("bucket", observed=True)[target].agg(["count", "mean", "std"])
    g["mean_bps"] = g["mean"] * 10_000
    return g[["count", "mean_bps", "std"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--csv", default=None, help="Write per-bar feature table to this path")
    args = ap.parse_args()

    db = mongo.get_db()
    if db is None:
        print("Mongo not configured / unreachable. Aborting.")
        sys.exit(1)

    df = _load_snapshots(db, args.symbol)
    if df.empty:
        print("No snapshots found.")
        return

    print("Building per-bar feature table ...", flush=True)
    bars = _build_bar_table(df)
    bars = _add_forward_returns(bars, horizons_bars=(1, 3, 6))
    print(f"  built {len(bars):,} bars across {bars['date'].nunique()} days")

    features = [
        "pcr_oi", "pcr_vol", "d_pcr_oi", "d_ce_oi", "d_pe_oi",
        "atm_straddle", "d_straddle",
        "call_wall_dist", "put_wall_dist", "max_pain_dist", "oi_skew",
    ]
    horizons = ["fwd_5m", "fwd_15m", "fwd_30m"]

    print("\n=== Pearson correlation: feature vs forward spot return ===")
    corr = _corr_table(bars, features, horizons)
    # Pretty print
    fmt = corr.copy()
    for h in horizons:
        fmt[h] = fmt[h].map(lambda v: f"{v:+.3f}" if pd.notna(v) else "  nan ")
    print(fmt.to_string(index=False))

    # Find strongest feature × horizon (by abs corr, ignoring nans)
    melted = corr.melt(id_vars=["feature", "n"], value_vars=horizons,
                       var_name="horizon", value_name="corr").dropna()
    melted["abs"] = melted["corr"].abs()
    top = melted.sort_values("abs", ascending=False).head(5)

    print("\n=== Top 5 strongest signals ===")
    for _, r in top.iterrows():
        print(f"  {r['feature']:>15}  ->  {r['horizon']:<7}  corr={r['corr']:+.3f}  (n={int(r['n'])})")

    # ── Direction vs volatility: also correlate features with |fwd_return|.
    # If a feature's |corr| jumps a lot when we switch from signed to abs returns,
    # it's a volatility predictor, not a directional one.
    print("\n=== Abs-return correlation (volatility signal check) ===")
    bars_abs = bars.copy()
    for h in horizons:
        bars_abs[f"abs_{h}"] = bars_abs[h].abs()
    abs_horizons = [f"abs_{h}" for h in horizons]
    corr_abs = _corr_table(bars_abs, features, abs_horizons)
    fmt_abs = corr_abs.copy()
    for h in abs_horizons:
        fmt_abs[h] = fmt_abs[h].map(lambda v: f"{v:+.3f}" if pd.notna(v) else "  nan ")
    print(fmt_abs.to_string(index=False))

    # Quintile breakdown for the top feature
    if not top.empty:
        best = top.iloc[0]
        print(f"\n=== Quintile breakdown: {best['feature']} -> {best['horizon']} ===")
        q = _quintile_breakdown(bars, best["feature"], best["horizon"])
        if q.empty:
            print("  not enough data")
        else:
            q["mean_bps"] = q["mean_bps"].map(lambda v: f"{v:+.2f}")
            q["std"]      = q["std"].map(lambda v: f"{v:.4f}")
            print(q.to_string())
            # spread Q5 - Q1 in bps
            try:
                hi = float(q.iloc[-1]["mean_bps"])
                lo = float(q.iloc[0]["mean_bps"])
                print(f"\n  Q5 − Q1 spread: {hi - lo:+.2f} bps")
            except Exception:
                pass

    # Sample size sanity
    days = bars["date"].nunique()
    print(f"\nNOTE: only {days} trading days in sample. "
          f"|corr| < 0.1 is noise here; need {days*5}+ bars / ~{30} days to trust < 0.1.")

    if args.csv:
        out = Path(args.csv)
        bars.to_csv(out, index=False)
        print(f"\nPer-bar feature table written to {out}")


if __name__ == "__main__":
    main()
