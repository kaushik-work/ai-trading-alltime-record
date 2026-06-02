"""
Red Dot Pillars Hypothesis Test
================================
From the regime-theory transcript:
  "Red Dot Pillars mark n-points — market states where n different regimes
   are equidistant. These are maximum uncertainty, maximum optionality
   positions ... contrarian entries have highest expected value."

Mapping to v5:
  - "n regimes" = our eligible expiries at a given timestamp
  - "equidistant" = signals across expiries disagree in direction
  - "contrarian entry" = trade AGAINST the strongest signal at disagreement points

v5 currently follows the strongest signal regardless of cross-expiry agreement.
Hypothesis: at DISAGREEMENT points, v5 is over-confident — the contrarian
side has higher EV.

Test method:
  1. At each hour, compute all eligible expiry signals.
  2. Bucket by agreement: AGREE (all same sign), SPLIT (mixed).
  3. For each bucket, measure realized 24h spot return in direction of
     (a) strongest signal (v5 default) vs (b) opposite of strongest.
  4. If SPLIT bucket shows contrarian > follow-strongest, hypothesis confirmed.

Run:
  UNDERLYING=BTC ./.venv/Scripts/python diag_red_dot.py
  UNDERLYING=ETH ./.venv/Scripts/python diag_red_dot.py
"""

import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import numpy as np
import pandas as pd

UNDERLYING = os.environ.get("UNDERLYING", "BTC").upper()
DATA = (Path(__file__).parent / "data") if UNDERLYING == "BTC" \
       else (Path(__file__).parent / "data" / UNDERLYING.lower())
PERP_SYMBOL = f"{UNDERLYING}USD"

MIN_TT_HOURS = 6
MAX_TT_HOURS = 168          # wide window to maximize cross-expiry samples
MIN_STRIKES  = 3
SINGLE_GATE  = 0.002        # |pred| > 0.2% to count as a real signal
FORWARD_HOURS = 24


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


def build_index(marks):
    rows = []
    for sym in marks:
        try: side, strike, expiry = parse_symbol(sym)
        except Exception: continue
        rows.append({"symbol": sym, "side": side, "strike": strike, "expiry": expiry})
    return pd.DataFrame(rows)


def signals_at(t, spot, catalogue, marks):
    tt_min = t + pd.Timedelta(hours=MIN_TT_HOURS)
    tt_max = t + pd.Timedelta(hours=MAX_TT_HOURS)
    eligible = catalogue[(catalogue["expiry"] > tt_min) & (catalogue["expiry"] <= tt_max)]
    out = []
    for exp in sorted(eligible["expiry"].unique()):
        same = eligible[eligible["expiry"] == exp]
        calls = same[same["side"] == "C"].set_index("strike")
        puts  = same[same["side"] == "P"].set_index("strike")
        common = sorted(set(calls.index) & set(puts.index))
        near = [K for K in common if abs(K - spot) / spot <= 0.05]
        if len(near) < MIN_STRIKES: continue
        devs = []
        for K in near:
            c = marks.get(calls.loc[K, "symbol"])
            p = marks.get(puts.loc[K, "symbol"])
            if c is None or p is None: continue
            if t not in c.index or t not in p.index: continue
            cp = float(c.loc[t]); pp = float(p.loc[t])
            if cp <= 0 or pp <= 0: continue
            devs.append(((cp - pp + K) - spot) / spot)
        if len(devs) < MIN_STRIKES: continue
        pos = sum(1 for d in devs if d > 0); neg = sum(1 for d in devs if d < 0)
        if pos < MIN_STRIKES and neg < MIN_STRIKES: continue
        out.append({"expiry": exp, "pred": float(np.median(devs))})
    return out


def main():
    print(f"Loading {UNDERLYING}...")
    perp = load_perp()
    marks = load_option_marks()
    cat = build_index(marks)
    hours = perp.index[(perp.index.minute == 0) & (perp.index.second == 0)]
    print(f"  perp 1m: {len(perp):,}  options: {len(marks):,}  decision pts: {len(hours):,}")
    print()

    rows = []
    last_pct = -1
    for i, t in enumerate(hours):
        pct = i * 100 // len(hours)
        if pct != last_pct and pct % 25 == 0 and pct > 0:
            print(f"  ... {pct}%", flush=True); last_pct = pct
        spot = float(perp.loc[t])
        sigs = signals_at(t, spot, cat, marks)
        if len(sigs) < 2: continue
        # filter to "active" signals
        active = [s for s in sigs if abs(s["pred"]) > SINGLE_GATE]
        if len(active) < 2: continue
        # forward 24h return
        t_fwd = t + pd.Timedelta(hours=FORWARD_HOURS)
        if t_fwd > perp.index[-1]: continue
        spot_fwd = float(perp.iloc[perp.index.get_indexer([t_fwd], method="nearest")[0]])
        fwd_ret = (spot_fwd - spot) / spot * 100

        # identify strongest signal + agreement state
        strongest = max(active, key=lambda s: abs(s["pred"]))
        n_pos = sum(1 for s in active if s["pred"] > 0)
        n_neg = sum(1 for s in active if s["pred"] < 0)
        agreement = "AGREE" if (n_pos == len(active) or n_neg == len(active)) else "SPLIT"
        strongest_sign = np.sign(strongest["pred"])

        # v5 strategy: trade in direction of strongest
        v5_pnl = strongest_sign * fwd_ret
        # Red Dot strategy: trade OPPOSITE of strongest
        red_dot_pnl = -strongest_sign * fwd_ret

        rows.append({
            "t": t, "n_active": len(active),
            "n_pos": n_pos, "n_neg": n_neg,
            "agreement": agreement,
            "max_strength_pct": abs(strongest["pred"]) * 100,
            "fwd_ret_pct": fwd_ret,
            "v5_pnl_pct": v5_pnl,
            "red_dot_pnl_pct": red_dot_pnl,
        })

    if not rows:
        print("No samples produced."); return
    df = pd.DataFrame(rows)
    print(f"\nTotal multi-expiry samples: {len(df):,}")
    print(f"  AGREE samples : {(df['agreement']=='AGREE').sum():,}")
    print(f"  SPLIT samples : {(df['agreement']=='SPLIT').sum():,}")
    print()

    print("=" * 80)
    print("  Hypothesis: at DISAGREEMENT points, contrarian EV > follow-strongest")
    print("=" * 80)
    print(f"  {'bucket':<18} {'n':>5} {'v5 hit%':>9} {'v5 avg%':>9} "
          f"{'redDot hit%':>13} {'redDot avg%':>13}")
    print("  " + "-" * 75)
    for label in ["AGREE", "SPLIT"]:
        sub = df[df["agreement"] == label]
        if sub.empty: continue
        v5_hit  = (sub["v5_pnl_pct"] > 0).mean() * 100
        v5_avg  = sub["v5_pnl_pct"].mean()
        rd_hit  = (sub["red_dot_pnl_pct"] > 0).mean() * 100
        rd_avg  = sub["red_dot_pnl_pct"].mean()
        print(f"  {label:<18} {len(sub):>5} {v5_hit:>8.1f}% {v5_avg:>+8.3f}% "
              f"{rd_hit:>12.1f}% {rd_avg:>+12.3f}%")
    print()

    # gate-strength × agreement matrix
    print("  Breakdown by SPLIT × strongest-signal-magnitude:")
    split = df[df["agreement"] == "SPLIT"]
    if len(split) > 0:
        for lo, hi in [(0.2, 0.4), (0.4, 0.6), (0.6, 1.0), (1.0, 99.0)]:
            sub = split[(split["max_strength_pct"] >= lo) & (split["max_strength_pct"] < hi)]
            if sub.empty: continue
            v5_avg = sub["v5_pnl_pct"].mean()
            rd_avg = sub["red_dot_pnl_pct"].mean()
            print(f"    SPLIT × strength {lo:.1f}-{hi:.1f}%  n={len(sub):>4}  "
                  f"v5={v5_avg:+.3f}%  redDot={rd_avg:+.3f}%")
    print()

    print("=" * 80)
    if not df[df["agreement"] == "SPLIT"].empty:
        split_v5 = df[df["agreement"] == "SPLIT"]["v5_pnl_pct"].mean()
        split_rd = df[df["agreement"] == "SPLIT"]["red_dot_pnl_pct"].mean()
        if split_rd > split_v5:
            print(f"  VERDICT: At disagreement points, contrarian EV ({split_rd:+.3f}%) "
                  f"> v5 EV ({split_v5:+.3f}%)")
            print(f"  → Red Dot hypothesis CONFIRMED. Worth building v8 that flips at SPLIT points.")
        else:
            print(f"  VERDICT: At disagreement points, v5 EV ({split_v5:+.3f}%) "
                  f"≥ contrarian EV ({split_rd:+.3f}%)")
            print(f"  → Red Dot hypothesis NOT CONFIRMED on this data. v5 picks dominant correctly.")
    print()


if __name__ == "__main__":
    main()
