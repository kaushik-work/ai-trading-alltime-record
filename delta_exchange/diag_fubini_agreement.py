"""
Fubini Cross-Expiry Agreement Diagnostic
=========================================
v5 currently aggregates the joint signal support (time × expiry × strike)
as:   inner = strikes,  outer = expiries,  decision = "pick strongest expiry"

By Fubini's theorem, the same support can be decomposed the other way:
      inner = expiries, outer = strikes,  decision = "cross-expiry voting"

This diagnostic asks: at each hour where multiple expiries are eligible
and we have signals for all of them, does the FRACTION of expiries
agreeing in sign predict win rate / forward return better than a single
expiry alone?

If yes → v5 should add a "≥ k expiries agree" gate. If no → current
single-expiry pick is fine.

Run:
  UNDERLYING=BTC ./.venv/Scripts/python diag_fubini_agreement.py
  UNDERLYING=ETH ./.venv/Scripts/python diag_fubini_agreement.py
"""

import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import math
import numpy as np
import pandas as pd

UNDERLYING = os.environ.get("UNDERLYING", "BTC").upper()
DATA = (Path(__file__).parent / "data") if UNDERLYING == "BTC" \
       else (Path(__file__).parent / "data" / UNDERLYING.lower())
PERP_SYMBOL = f"{UNDERLYING}USD"

MIN_TT_HOURS = 6
MAX_TT_HOURS = 168       # widen to 1 week to catch more overlapping expiries
MIN_STRIKES  = 3
SINGLE_GATE  = 0.001     # very loose — any non-trivial signal counts as a vote

FORWARD_HOURS = 24       # measure realized return over this horizon


def load_perp():
    df = pd.read_csv(DATA / "perp" / f"{PERP_SYMBOL}_mark_1m.csv")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("timestamp")["close"].sort_index()


def parse_symbol(sym):
    parts = sym.split("-")
    side = parts[0]; strike = int(parts[2])
    dd, mm, yy = parts[3][:2], parts[3][2:4], parts[3][4:6]
    return side, strike, pd.Timestamp(f"20{yy}-{mm}-{dd} 12:00:00", tz="UTC")


def load_option_marks():
    out = {}
    for p in sorted((DATA / "options").glob("*_mark_1h.csv")):
        sym = p.name.replace("_mark_1h.csv", "")
        df = pd.read_csv(p)
        if df.empty: continue
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        out[sym] = df.set_index("timestamp")["close"].sort_index()
    return out


def build_index(option_marks):
    rows = []
    for sym in option_marks:
        try: side, strike, expiry = parse_symbol(sym)
        except Exception: continue
        rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": expiry})
    return pd.DataFrame(rows)


def signals_at(t, spot, catalogue, marks):
    """Return list of per-expiry signals at t: [(expiry, pred), ...]"""
    tt_min = t + pd.Timedelta(hours=MIN_TT_HOURS)
    tt_max = t + pd.Timedelta(hours=MAX_TT_HOURS)
    eligible = catalogue[(catalogue["expiry"] > tt_min) & (catalogue["expiry"] <= tt_max)]
    out = []
    for exp in sorted(eligible["expiry"].unique()):
        same = eligible[eligible["expiry"] == exp]
        calls = same[same["side"] == "C"].set_index("strike")
        puts  = same[same["side"] == "P"].set_index("strike")
        common = sorted(set(calls.index) & set(puts.index))
        common_near = [K for K in common if abs(K - spot) / spot <= 0.05]
        if len(common_near) < MIN_STRIKES: continue
        devs = []
        for K in common_near:
            c = marks.get(calls.loc[K, "symbol"])
            p = marks.get(puts.loc[K, "symbol"])
            if c is None or p is None: continue
            if t not in c.index or t not in p.index: continue
            cp = float(c.loc[t]); pp = float(p.loc[t])
            if cp <= 0 or pp <= 0: continue
            devs.append(((cp - pp + K) - spot) / spot)
        if len(devs) < MIN_STRIKES: continue
        out.append({"expiry": exp, "pred": float(np.median(devs)), "n_strikes": len(devs)})
    return out


def main():
    print(f"Loading {UNDERLYING} data...")
    perp = load_perp()
    marks = load_option_marks()
    catalogue = build_index(marks)
    hours = perp.index[(perp.index.minute == 0) & (perp.index.second == 0)]
    print(f"  perp 1m bars: {len(perp):,}  options: {len(marks):,}  "
          f"decision pts: {len(hours):,}")
    print()

    rows = []
    print("Scanning all hourly timestamps...")
    last_pct = -1
    for i, t in enumerate(hours):
        pct = i * 100 // len(hours)
        if pct != last_pct and pct % 25 == 0 and pct > 0:
            print(f"  ... {pct}%", flush=True); last_pct = pct
        spot = float(perp.loc[t])
        sigs = signals_at(t, spot, catalogue, marks)
        if len(sigs) < 2:
            continue
        # active voters = expiries with |pred| above gate
        active = [s for s in sigs if abs(s["pred"]) > SINGLE_GATE]
        if not active:
            continue
        n_pos = sum(1 for s in active if s["pred"] > 0)
        n_neg = sum(1 for s in active if s["pred"] < 0)
        n_active = len(active)
        n_total  = len(sigs)
        # majority direction
        majority_sign = 1 if n_pos > n_neg else (-1 if n_neg > n_pos else 0)
        if majority_sign == 0:
            continue
        # forward perp return over FORWARD_HOURS
        t_fwd = t + pd.Timedelta(hours=FORWARD_HOURS)
        if t_fwd > perp.index[-1]:
            continue
        spot_fwd = float(perp.reindex([t_fwd], method="nearest")["close"].iloc[0]
                          if False else perp.iloc[perp.index.get_indexer([t_fwd], method="nearest")[0]])
        fwd_ret_pct = (spot_fwd - spot) / spot * 100
        rows.append({
            "t": t, "spot": spot, "fwd_ret_pct": fwd_ret_pct,
            "n_active": n_active, "n_total": n_total,
            "n_agree": max(n_pos, n_neg),
            "agreement_pct": max(n_pos, n_neg) / n_active * 100,
            "majority_sign": majority_sign,
            "max_strength_pct": max(abs(s["pred"]) for s in active) * 100,
            "median_strength_pct": float(np.median([abs(s["pred"]) for s in active])) * 100,
            "directional_pnl_pct": majority_sign * fwd_ret_pct,
        })

    if not rows:
        print("No samples — check data."); return

    df = pd.DataFrame(rows)
    print()
    print(f"Total decision points with multi-expiry signal: {len(df):,}")
    print()

    # ── decomposition: by number of expiries agreeing ────────────────────────
    print("=" * 80)
    print("  Performance by CROSS-EXPIRY AGREEMENT (Fubini decomposition)")
    print(f"  Following majority direction; measured forward {FORWARD_HOURS}h spot return")
    print("=" * 80)
    print(f"  {'agreement':<22} {'n':>5} {'hit%':>6} {'mean PnL%':>10} "
          f"{'median':>8} {'sum PnL%':>10}")
    print("  " + "-" * 72)
    # bucket by (n_agree, n_active)
    df["bucket"] = df.apply(
        lambda r: (f"{r['n_agree']}/{r['n_active']}"
                   if r['n_active'] <= 4 else f"≥{r['n_agree']}/≥5"), axis=1)
    # also include "all agree" cleanly
    bucket_order = sorted(df["bucket"].unique(),
                          key=lambda b: (int(b.split("/")[0].replace("≥","")),
                                          int(b.split("/")[1].replace("≥",""))))
    for b in bucket_order:
        sub = df[df["bucket"] == b]
        hit = (sub["directional_pnl_pct"] > 0).mean() * 100
        mean = sub["directional_pnl_pct"].mean()
        med  = sub["directional_pnl_pct"].median()
        total = sub["directional_pnl_pct"].sum()
        print(f"  {b:<22} {len(sub):>5} {hit:>5.1f}% {mean:>+9.3f}% "
              f"{med:>+7.3f}% {total:>+9.1f}%")
    print()

    # ── agreement % buckets ──────────────────────────────────────────────────
    print("  Performance by AGREEMENT PERCENTAGE:")
    pct_buckets = [(50, 70), (70, 90), (90, 100), (100, 101)]
    print(f"  {'agreement %':<18} {'n':>5} {'hit%':>6} {'mean PnL%':>10} {'sum PnL%':>10}")
    print("  " + "-" * 60)
    for lo, hi in pct_buckets:
        sub = df[(df["agreement_pct"] >= lo) & (df["agreement_pct"] < hi)]
        if sub.empty: continue
        hit = (sub["directional_pnl_pct"] > 0).mean() * 100
        mean = sub["directional_pnl_pct"].mean()
        total = sub["directional_pnl_pct"].sum()
        lbl = f"{lo}-{hi}%" if hi != 101 else "100% (unanimous)"
        print(f"  {lbl:<18} {len(sub):>5} {hit:>5.1f}% {mean:>+9.3f}% {total:>+9.1f}%")
    print()

    # ── combined: agreement × strength ──────────────────────────────────────
    print("  Performance by AGREEMENT × MAX SIGNAL STRENGTH:")
    print(f"  {'condition':<32} {'n':>5} {'hit%':>6} {'mean PnL%':>10} {'sum PnL%':>10}")
    print("  " + "-" * 70)
    conds = [
        ("base (any agreement)",          lambda d: d),
        ("agreement ≥ 70%",                lambda d: d[d["agreement_pct"] >= 70]),
        ("unanimous (100%)",                lambda d: d[d["agreement_pct"] == 100]),
        ("unanimous + max strength ≥ 0.6%", lambda d: d[(d["agreement_pct"] == 100)
                                                       & (d["max_strength_pct"] >= 0.6)]),
        ("unanimous + max strength ≥ 1.0%", lambda d: d[(d["agreement_pct"] == 100)
                                                       & (d["max_strength_pct"] >= 1.0)]),
    ]
    for label, fn in conds:
        sub = fn(df)
        if sub.empty:
            print(f"  {label:<32} {len(sub):>5} (no samples)")
            continue
        hit = (sub["directional_pnl_pct"] > 0).mean() * 100
        mean = sub["directional_pnl_pct"].mean()
        total = sub["directional_pnl_pct"].sum()
        print(f"  {label:<32} {len(sub):>5} {hit:>5.1f}% {mean:>+9.3f}% {total:>+9.1f}%")
    print()

    out = DATA / "diag_fubini_agreement.csv"
    df.to_csv(out, index=False)
    print(f"  full series → {out.relative_to(DATA.parent)}")


if __name__ == "__main__":
    main()
