"""
Carry-Adjusted Synthetic-Forward Signal — Theoretical Robustness Check
=======================================================================
Per Quant Finance 101 (futures pricing under risk-neutral measure):
    F = S × exp((R − Q)(T − t))

For BTCUSD perp on Delta India:
  - R ≈ 0  (USD risk-free, short horizon)
  - Q ≈ funding rate (longs pay shorts; treated as a continuous dividend)

So the EXPECTED options-implied forward, with zero information edge:
    F_expected = Spot × exp(−funding_rate × time_to_expiry)

The synthetic forward we observe is:
    F_synth = C − P + K

Our v5 signal was (F_synth − Spot) / Spot.
The CORRECT alpha-only signal is:
    alpha = (F_synth − F_expected) / Spot
          = (F_synth − Spot × exp(−funding × T)) / Spot

If our directional edge survives this adjustment, v5 is real.
If it shrinks dramatically, v5 was front-running funding rate.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
from pathlib import Path
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"


def load_perp() -> pd.Series:
    df = pd.read_csv(DATA / "perp" / "BTCUSD_mark_1m.csv")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


def load_funding() -> pd.Series:
    """Funding rate candles — each row is the rate for that funding period (8h)."""
    df = pd.read_csv(DATA / "perp" / "BTCUSD_funding_1h.csv")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    # 'close' is the funding rate for the period. Forward-fill across hours.
    s = df.set_index("timestamp")["close"].sort_index()
    return s


def parse_symbol(sym: str):
    parts = sym.split("-")
    side = parts[0]; strike = int(parts[2])
    dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
    return side, strike, pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")


def load_option_marks() -> dict:
    out = {}
    for p in sorted((DATA / "options").glob("*_mark_1h.csv")):
        sym = p.name.replace("_mark_1h.csv", "")
        df = pd.read_csv(p)
        if df.empty: continue
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        out[sym] = df.set_index("timestamp")["close"].sort_index()
    return out


def build_index(option_marks: dict) -> pd.DataFrame:
    rows = []
    for sym in option_marks:
        try:
            side, strike, expiry = parse_symbol(sym)
        except Exception:
            continue
        rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": expiry})
    return pd.DataFrame(rows)


def run():
    print("Loading data...")
    perp = load_perp()
    funding = load_funding()
    print(f"  perp: {len(perp):,} bars  funding: {len(funding):,} rate points")
    print(f"  funding stats: mean {funding.mean()*100:.4f}%/period   "
          f"min {funding.min()*100:.4f}%   max {funding.max()*100:.4f}%")
    print()

    marks = load_option_marks()
    cat = build_index(marks)
    expiries = sorted(cat["expiry"].unique())
    print(f"  options: {len(cat):,} contracts × {len(expiries)} expiries")
    print()

    # Delta's FUNDING:BTCUSD candles return the annualized funding rate directly
    # (e.g. -0.045 = -4.5% annualized). No further scaling needed.

    rows = []
    for exp in expiries:
        sub = cat[cat["expiry"] == exp]
        calls = sub[sub["side"] == "C"].set_index("strike")
        puts  = sub[sub["side"] == "P"].set_index("strike")
        common = sorted(set(calls.index) & set(puts.index))
        if not common:
            continue
        for K in common:
            c = marks.get(calls.loc[K, "symbol"])
            p = marks.get(puts.loc[K, "symbol"])
            if c is None or p is None: continue
            joined = pd.concat([c.rename("C"), p.rename("P")], axis=1, join="inner")
            cutoff = exp - pd.Timedelta(days=3)
            joined = joined[(joined.index >= cutoff) & (joined.index <= exp)]
            if joined.empty: continue
            spot = perp.reindex(joined.index, method="nearest").values
            fund_annual = funding.reindex(joined.index, method="ffill").values

            T_years = np.array([(exp - t).total_seconds() / (365 * 86400)
                                for t in joined.index])
            # F_expected per BS-style forward: S × exp(-funding × T)
            # (positive funding → perp longs pay → forward priced below spot)
            f_expected = spot * np.exp(-fund_annual * T_years)
            f_synth    = joined["C"].values - joined["P"].values + K
            raw_dev    = (f_synth - spot) / spot * 100         # original v5 signal
            alpha_dev  = (f_synth - f_expected) / spot * 100   # carry-adjusted

            joined["spot"]        = spot
            joined["fund_annual"] = fund_annual
            joined["f_synth"]     = f_synth
            joined["f_expected"]  = f_expected
            joined["raw_dev_pct"] = raw_dev
            joined["alpha_pct"]   = alpha_dev
            joined["tt_hours"]    = T_years * 365 * 24
            joined["expiry"]      = exp
            joined["strike"]      = K
            rows.append(joined.reset_index().rename(columns={"timestamp": "t"}))

    if not rows:
        print("no samples")
        return

    df = pd.concat(rows, ignore_index=True)
    df = df.dropna(subset=["raw_dev_pct", "alpha_pct"])

    # collapse to (t, expiry) via median across strikes
    g = df.groupby(["t", "expiry"]).agg(
        raw=("raw_dev_pct",  "median"),
        alpha=("alpha_pct",  "median"),
        spot=("spot",         "first"),
        fund=("fund_annual",  "first"),
        tt=("tt_hours",       "first"),
    ).reset_index()

    # forward returns: what did spot do from t to expiry?
    perp_ser = perp
    g["spot_at_expiry"] = perp_ser.reindex(g["expiry"], method="nearest").values
    g["real_pct"] = (g["spot_at_expiry"] - g["spot"]) / g["spot"] * 100

    print("=" * 80)
    print("  RAW vs CARRY-ADJUSTED synthetic-forward signal")
    print("=" * 80)
    print(f"  samples: {len(g):,}")
    print()
    print(f"  RAW dev (v5 signal):    "
          f"mean {g['raw'].mean():+.3f}%   median {g['raw'].median():+.3f}%   "
          f"std {g['raw'].std():.3f}%")
    print(f"  CARRY-ADJUSTED alpha:   "
          f"mean {g['alpha'].mean():+.3f}%   median {g['alpha'].median():+.3f}%   "
          f"std {g['alpha'].std():.3f}%")
    print(f"  Funding component:      "
          f"mean {(g['raw']-g['alpha']).mean():+.3f}%   "
          f"(this is the predictable carry, NOT alpha)")
    print()

    # signal-strength buckets — same analysis as before, on BOTH signals
    def bucket_stats(sig_col, label):
        sub = g[g[sig_col].abs() > 0.3]
        if sub.empty:
            print(f"  {label}: 0 samples with |signal| > 0.3%"); return
        hit_rate = (np.sign(sub[sig_col]) == np.sign(sub["real_pct"])).mean()
        avg_pos = sub.loc[sub[sig_col] > 0, "real_pct"].mean()
        avg_neg = sub.loc[sub[sig_col] < 0, "real_pct"].mean()
        pnl = (np.sign(sub[sig_col]) * sub["real_pct"]).mean()
        print(f"  {label:<28} n={len(sub):,}  hit={hit_rate*100:.1f}%  "
              f"avg_when_+={avg_pos:+.3f}%  when_-={avg_neg:+.3f}%  "
              f"signal-follower avg PnL={pnl:+.3f}%/trade")

    print("Following the RAW signal vs CARRY-ADJUSTED signal (gate |sig|>0.3%):")
    print("-" * 80)
    bucket_stats("raw",   "RAW (v5 / unadjusted)")
    bucket_stats("alpha", "ALPHA (carry-adjusted)")
    print()

    # also: do they agree on direction?
    same_sign = (np.sign(g["raw"]) == np.sign(g["alpha"])).mean()
    big_gate = g[(g["raw"].abs() > 0.3) | (g["alpha"].abs() > 0.3)]
    flipped = (np.sign(big_gate["raw"]) != np.sign(big_gate["alpha"])).sum()
    print(f"  raw vs alpha same sign across all samples: {same_sign*100:.1f}%")
    print(f"  flipped sign (raw>0.3% but alpha says opposite): {flipped} samples")
    print()

    # high-funding regime check
    print("Signal behavior conditional on funding sign:")
    hi_fund = g[g["fund"] > 0.10]    # > 10% annualized
    lo_fund = g[g["fund"] < -0.10]
    mid     = g[(g["fund"] >= -0.10) & (g["fund"] <= 0.10)]
    for label, sub in [("funding >+10% annual", hi_fund),
                       ("funding flat (±10%)",  mid),
                       ("funding <−10% annual", lo_fund)]:
        if sub.empty: continue
        print(f"  {label:<24} n={len(sub):>5}  "
              f"raw_dev_mean={sub['raw'].mean():+.3f}%  "
              f"alpha_mean={sub['alpha'].mean():+.3f}%")
    print()

    out = DATA / "diag_carry_adjusted.csv"
    g.to_csv(out, index=False)
    print(f"  full series → {out.relative_to(DATA.parent)}")


if __name__ == "__main__":
    run()
