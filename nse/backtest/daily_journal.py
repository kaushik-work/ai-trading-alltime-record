"""Build a quantitative daily journal from 5 years of data, then test what predicts.

A written journal teaches nothing measurable. A journal whose every field is a
NUMBER, joined to the outcome of that day, is a feature table you can test.
This builds that table for all 1,255 sessions, so we can ask which conditions
actually separate good days from bad ones instead of collecting them one day at
a time for six months.

THE RULE THAT MAKES IT HONEST
    Every feature must be knowable at 09:30, before the 09:30-11:30 entry
    window opens. Anything computed from the rest of the session is lookahead
    and would flatter every result. Features are therefore drawn from:
      - the PREVIOUS session (close, range, return, ATR)
      - today's open and the 09:15-09:30 opening range
      - the option chain AS OF 09:30 (PCR, OI walls, max pain, ATM IV)

WHAT THE JOURNAL TEMPLATE CONTRIBUTES
    The handwritten template lists exactly the right fields - gap, PCR, VIX,
    OI walls, RSI, MACD, EMA position. Its real value is telling us WHICH
    features are worth testing. We then test them against 1,255 days of
    history rather than waiting to accumulate them.

    FII/DII is the one field we cannot reproduce - it needs an external daily
    source. Everything else on that page is computable from what we hold.

Usage:
    python -m nse.backtest.daily_journal --build      # build + cache the table
    python -m nse.backtest.daily_journal              # analyse what predicts
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from nse.backtest.nifty_loader import DEFAULT_ROOT, CACHE_DIR, clean_day, load_spot

JOURNAL_CACHE = CACHE_DIR / "nifty_daily_journal.csv"
CUTOFF = "09:30"          # features frozen here; entries start after


# ── option-chain features, as of 09:30 ───────────────────────────────────────
def chain_features(path: Path) -> dict | None:
    try:
        d = clean_day(pd.read_csv(path, usecols=[
            "datetime", "option_type", "close", "volume", "oi",
            "iv", "strike_price", "spot"]))
    except Exception:
        return None
    if d.empty:
        return None
    d["dt"] = pd.to_datetime(d["datetime"], errors="coerce")
    d = d.dropna(subset=["dt"])
    d = d[d["dt"].dt.strftime("%H:%M") <= CUTOFF]        # nothing after 09:30
    if d.empty:
        return None
    d["isC"] = d["option_type"].astype(str).str.upper().str.startswith("C")
    snap = d.sort_values("dt").drop_duplicates(["isC", "strike_price"], keep="last")
    spot = float(snap["spot"].median())
    ce, pe = snap[snap["isC"]], snap[~snap["isC"]]
    if ce.empty or pe.empty or spot <= 0:
        return None

    coi, poi = ce["oi"].sum(), pe["oi"].sum()
    cvol, pvol = ce["volume"].sum(), pe["volume"].sum()
    atm = snap.iloc[(snap["strike_price"] - spot).abs().argsort()[:4]]
    atm_iv = pd.to_numeric(atm["iv"], errors="coerce")
    atm_iv = atm_iv[(atm_iv > 0) & (atm_iv < 200)]

    # Max pain: strike where total writer payout is smallest.
    strikes = sorted(set(snap["strike_price"]))
    pain = []
    for K in strikes:
        c_loss = float((ce["oi"] * np.maximum(K - ce["strike_price"], 0)).sum())
        p_loss = float((pe["oi"] * np.maximum(pe["strike_price"] - K, 0)).sum())
        pain.append((c_loss + p_loss, K))
    max_pain = min(pain)[1] if pain else np.nan

    call_wall = float(ce.loc[ce["oi"].idxmax(), "strike_price"]) if len(ce) else np.nan
    put_wall = float(pe.loc[pe["oi"].idxmax(), "strike_price"]) if len(pe) else np.nan

    return {
        "spot_0930": spot,
        "pcr_oi": (poi / coi) if coi > 0 else np.nan,
        "pcr_vol": (pvol / cvol) if cvol > 0 else np.nan,
        "atm_iv": float(atm_iv.mean()) if len(atm_iv) else np.nan,
        "max_pain_dist_pct": (max_pain - spot) / spot * 100 if np.isfinite(max_pain) else np.nan,
        "call_wall_dist_pct": (call_wall - spot) / spot * 100 if np.isfinite(call_wall) else np.nan,
        "put_wall_dist_pct": (put_wall - spot) / spot * 100 if np.isfinite(put_wall) else np.nan,
        "total_oi_lakh": (coi + poi) / 1e5,
    }


# ── price features from the cached 1m index series ───────────────────────────
def price_features(spot: pd.DataFrame) -> pd.DataFrame:
    g = spot.groupby("date")["close"]
    day = pd.DataFrame({
        "open": g.first(), "high": g.max(), "low": g.min(), "close": g.last(),
    }).reset_index()
    day["date"] = pd.to_datetime(day["date"])
    day = day.sort_values("date").reset_index(drop=True)

    # PREVIOUS session only — never today's close.
    day["prev_close"] = day["close"].shift(1)
    day["prev_ret_pct"] = day["close"].pct_change().shift(1) * 100
    day["prev_range_pct"] = ((day["high"] - day["low"]) / day["close"]).shift(1) * 100
    tr = np.maximum(day["high"] - day["low"],
                    np.maximum((day["high"] - day["prev_close"]).abs(),
                               (day["low"] - day["prev_close"]).abs()))
    day["atr5_pct"] = (tr.rolling(5).mean() / day["close"]).shift(1) * 100
    day["gap_pct"] = (day["open"] - day["prev_close"]) / day["prev_close"] * 100

    # Daily RSI and EMA distance, both lagged one session.
    delta = day["close"].diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, min_periods=14).mean()
    dn = (-delta).clip(lower=0).ewm(alpha=1 / 14, min_periods=14).mean()
    day["rsi14"] = (100 - 100 / (1 + up / dn.replace(0, np.nan))).shift(1)
    ema20 = day["close"].ewm(span=20, min_periods=10).mean()
    day["ema20_dist_pct"] = ((day["close"] - ema20) / ema20 * 100).shift(1)
    day["dow"] = day["date"].dt.dayofweek

    # Opening range 09:15-09:30 — known exactly at the cutoff.
    s = spot.copy()
    s["hhmm"] = s["datetime"].dt.strftime("%H:%M")
    orng = s[s["hhmm"] <= CUTOFF].groupby("date")["close"].agg(["max", "min", "first"])
    orng["or_range_pct"] = (orng["max"] - orng["min"]) / orng["first"] * 100
    orng.index = pd.to_datetime(orng.index)
    day = day.merge(orng[["or_range_pct"]], left_on="date", right_index=True, how="left")
    day["or_vs_atr"] = day["or_range_pct"] / day["atr5_pct"].replace(0, np.nan)
    return day


def build(force: bool = False) -> pd.DataFrame:
    if JOURNAL_CACHE.exists() and not force:
        return pd.read_csv(JOURNAL_CACHE, parse_dates=["date"])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    day = price_features(load_spot())

    rows = []
    files = sorted(DEFAULT_ROOT.glob("*/NIFTY_*_1m.csv"))
    for i, f in enumerate(files, 1):
        feats = chain_features(f)
        if feats:
            rows.append({"date": pd.Timestamp(f.name[6:16]), **feats})
        if i % 200 == 0:
            print(f"  ... {i}/{len(files)} sessions", flush=True)
    chain = pd.DataFrame(rows)

    j = day.merge(chain, on="date", how="left").sort_values("date")
    j.to_csv(JOURNAL_CACHE, index=False)
    print(f"cached {len(j):,} journal rows -> {JOURNAL_CACHE}")
    return j


FEATURES = ["gap_pct", "prev_ret_pct", "prev_range_pct", "atr5_pct", "rsi14",
            "ema20_dist_pct", "or_range_pct", "or_vs_atr", "pcr_oi", "pcr_vol",
            "atm_iv", "max_pain_dist_pct", "call_wall_dist_pct",
            "put_wall_dist_pct", "total_oi_lakh"]


def split_of(d):
    y = pd.Timestamp(d).year
    return "TRAIN" if y <= 2023 else ("VALID" if y == 2024 else "TEST")


def analyse(j: pd.DataFrame, trades: pd.DataFrame) -> None:
    """Which journal features separate profitable days from losing ones?"""
    daily = trades.groupby("date")["pts"].sum().rename("pnl").reset_index()
    daily["date"] = pd.to_datetime(daily["date"])
    m = j.merge(daily, on="date", how="inner")
    m["split"] = m["date"].map(split_of)
    print(f"\n  {len(m)} traded sessions matched to journal rows\n")

    print("  Feature split at its MEDIAN — mean day P&L in each half")
    print(f"  {'feature':22}{'n':>6}{'low half':>11}{'high half':>11}{'gap':>10}"
          f"{'TRAIN':>9}{'VALID':>9}{'TEST':>9}")
    for f in FEATURES:
        sub = m[m[f].notna()]
        if len(sub) < 60:
            continue
        med = sub[f].median()
        lo, hi = sub[sub[f] <= med], sub[sub[f] > med]
        if len(lo) < 20 or len(hi) < 20:
            continue
        gap = hi["pnl"].mean() - lo["pnl"].mean()
        cells = []
        for k in ("TRAIN", "VALID", "TEST"):
            s = sub[sub["split"] == k]
            if len(s) < 15:
                cells.append(np.nan); continue
            sm = s[f].median()
            cells.append(s[s[f] > sm]["pnl"].mean() - s[s[f] <= sm]["pnl"].mean())
        consistent = all(np.isfinite(c) and np.sign(c) == np.sign(gap) for c in cells)
        print(f"  {f:22}{len(sub):>6}{lo['pnl'].mean():>11.1f}{hi['pnl'].mean():>11.1f}"
              f"{gap:>10.1f}" + "".join(f"{c:>9.1f}" if np.isfinite(c) else f"{'n/a':>9}"
                                        for c in cells)
              + ("   CONSISTENT" if consistent else ""))
    print("\n  'gap' is how much better the high half did. CONSISTENT means the sign")
    print("  held in all three periods — the only ones worth building a filter on.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--sl", type=float, default=25.0)
    ap.add_argument("--rr", type=float, default=5.0)
    args = ap.parse_args()

    j = build(force=args.build)
    print("=" * 108)
    print("QUANTITATIVE DAILY JOURNAL — every field known by 09:30")
    print("=" * 108)
    print(f"  {len(j):,} sessions, {j['date'].min():%Y-%m-%d} -> {j['date'].max():%Y-%m-%d}")
    have = [f for f in FEATURES if j[f].notna().sum() > 100]
    print(f"  features with data: {len(have)}/{len(FEATURES)}")
    print(f"  {'feature':22}{'n':>7}{'median':>10}{'p10':>10}{'p90':>10}")
    for f in have:
        s = j[f].dropna()
        print(f"  {f:22}{len(s):>7}{s.median():>10.2f}{s.quantile(.1):>10.2f}"
              f"{s.quantile(.9):>10.2f}")

    from nse.backtest.test_breakout_3stage import add_chop, run
    from nse.backtest.test_breakout_retest import prepare
    print("\n  Running the strategy to join outcomes ...", flush=True)
    bars = add_chop(prepare(load_spot()))
    t = run(bars, args.sl, args.rr, three_stage=True, chop_min=1.0)
    analyse(j, t)


if __name__ == "__main__":
    main()
