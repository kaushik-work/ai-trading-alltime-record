"""
Put-Call Parity Diagnostic — Delta Exchange BTC Options
========================================================
Direct application of the technique from the Indian-markets stream
"abib" video — for European options on a forward:

    C(K, T) - P(K, T)  ==  F(T) - K          (parity)
    ⇒ synthetic_F      :=  C - P + K

If the synthetic forward diverges from the actual perp / index price by
significantly more than expected funding-rate effects, either the option
marks are stale/illiquid OR there's a tradeable spread. Either way, large
deviations means our IV inversions in v2/v3 may have been built on noisy
inputs — and that would explain a lot of the backtest noise.

Output:
  - distribution of parity deviation across all (expiry, ATM-strike, hour) triples
  - flag worst offenders (biggest |spot - synthetic_F| / spot)
  - daily summary

Run:
  .venv/Scripts/python diag_parity.py
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"


# ── Data plumbing (re-uses the same shapes as backtest_straddle_v2) ──────────
def load_perp() -> pd.Series:
    df = pd.read_csv(DATA / "perp" / "BTCUSD_mark_1m.csv")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


def parse_symbol(sym: str):
    parts = sym.split("-")
    side = parts[0]
    strike = int(parts[2])
    dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
    expiry = pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")
    return side, strike, expiry


def option_catalogue() -> pd.DataFrame:
    rows = []
    for p in sorted((DATA / "options").glob("*_mark_1h.csv")):
        sym = p.name.replace("_mark_1h.csv", "")
        try:
            side, strike, expiry = parse_symbol(sym)
        except Exception:
            continue
        rows.append({"symbol": sym, "side": side, "strike": strike,
                     "expiry": expiry, "path": p})
    return pd.DataFrame(rows)


def load_mark(path: Path) -> pd.Series:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


# ── Diagnostic ────────────────────────────────────────────────────────────────
def run() -> None:
    print("Loading perp + options...")
    perp = load_perp()
    cat  = option_catalogue()
    expiries = sorted(cat["expiry"].unique())
    print(f"  perp 1m bars : {len(perp):,}  ({perp.index[0]} → {perp.index[-1]})")
    print(f"  options      : {len(cat):,} contracts × {len(expiries)} expiries")
    print()

    rows = []
    for exp in expiries:
        sub = cat[cat["expiry"] == exp]
        calls = sub[sub["side"] == "C"].set_index("strike")
        puts  = sub[sub["side"] == "P"].set_index("strike")
        common = sorted(set(calls.index) & set(puts.index))
        if not common:
            continue
        # for each common strike, load both legs and join on hour
        for K in common:
            c_path = calls.loc[K, "path"]
            p_path = puts.loc[K, "path"]
            try:
                c = load_mark(Path(c_path)).rename("C")
                p = load_mark(Path(p_path)).rename("P")
            except Exception:
                continue
            joined = pd.concat([c, p], axis=1, join="inner")
            if joined.empty:
                continue
            # only sample within 3 days of expiry (most liquid window)
            cutoff = exp - pd.Timedelta(days=3)
            joined = joined[(joined.index >= cutoff) & (joined.index <= exp)]
            if joined.empty:
                continue
            # synthetic forward
            joined["syn_F"] = joined["C"] - joined["P"] + K
            # join with perp
            spot = perp.reindex(joined.index, method="nearest")
            joined["spot"] = spot.values
            joined["dev_bps"] = (joined["syn_F"] - joined["spot"]) / joined["spot"] * 1e4
            joined["abs_dev_bps"] = joined["dev_bps"].abs()
            joined["expiry"]    = exp
            joined["strike"]    = K
            joined["tt_hours"]  = (exp - joined.index).total_seconds() / 3600
            rows.append(joined.reset_index().rename(columns={"timestamp": "t"}))

    if not rows:
        print("No parity samples produced.")
        return

    df = pd.concat(rows, ignore_index=True)
    print("Parity check — synthetic forward (C − P + K) vs perp spot")
    print("=" * 78)
    print(f"  samples            : {len(df):,}")
    print(f"  median |dev|       : {df['abs_dev_bps'].median():.0f} bps")
    print(f"  p90  |dev|         : {df['abs_dev_bps'].quantile(0.90):.0f} bps")
    print(f"  p99  |dev|         : {df['abs_dev_bps'].quantile(0.99):.0f} bps")
    print(f"  worst |dev|        : {df['abs_dev_bps'].max():.0f} bps")
    print(f"  mean signed dev    : {df['dev_bps'].mean():+.0f} bps "
          f"(positive = options imply HIGHER forward than perp)")
    print()

    # by hours-to-expiry bucket
    df["tt_bucket"] = pd.cut(df["tt_hours"], bins=[0, 6, 24, 48, 72],
                              labels=["0-6h", "6-24h", "24-48h", "48-72h"])
    print("  By hours-to-expiry:")
    g = df.groupby("tt_bucket", observed=True)["abs_dev_bps"].agg(
        ["count", "median", lambda x: x.quantile(0.90), lambda x: x.quantile(0.99)]
    )
    g.columns = ["n", "median_bps", "p90_bps", "p99_bps"]
    print(g.to_string())
    print()

    # daily summary
    df["date"] = df["t"].dt.date
    daily = df.groupby("date")["abs_dev_bps"].agg(["count", "median",
                                                    lambda x: x.quantile(0.95)])
    daily.columns = ["n", "median_bps", "p95_bps"]
    print(f"  By day (last 14):")
    print(daily.tail(14).to_string())
    print()

    # worst offenders
    worst = df.nlargest(10, "abs_dev_bps")[
        ["t", "expiry", "strike", "spot", "syn_F", "dev_bps", "tt_hours"]
    ]
    print("  Top 10 worst parity violations:")
    print(worst.to_string(index=False))
    print()

    out = DATA / "diag_parity.csv"
    df.to_csv(out, index=False)
    print(f"  full series → {out.relative_to(DATA.parent)}")


if __name__ == "__main__":
    run()
