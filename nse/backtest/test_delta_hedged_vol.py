"""Harvest the variance risk premium — short straddle, optionally delta-hedged.

WHAT THIS TESTS
    OPTIONS_GREEKS_LEARNINGS 6 measured that NIFTY implied vol exceeds realised
    vol in every year (ratio 0.69-0.99). That is an observation about prices,
    not a strategy. This is the strategy: sell the ATM straddle and find out
    whether the premium survives contact with the actual recorded exit prices.

    Delta-hedging is not a separate idea layered on top. Rebalancing a short
    gamma book mechanically buys high and sells low, and the cost of doing so
    is precisely what the premium pays for. So a delta-hedged short straddle IS
    the variance premium, expressed as a trade.

THE QUESTION THAT DECIDES THE STRUCTURE
    We measured that the overnight session is 42.3% of total variance and
    carries ALL of the tail (close-to-close kurtosis 13.75 vs 0.98 intraday).
    But theta is paid in calendar time, and 15:20 -> 09:15 is ~18 hours of it.

    So overnight is where most of the premium is earned AND where all of the
    risk lives. Whether that trade is worth taking cannot be reasoned out; it
    has to be measured. Hence the two variants below.

RULES THAT KEEP IT HONEST
    - Every fill is a RECORDED price from the option chain, never a model
      price (RESEARCH_LEARNINGS 1.7).
    - Fills are at the CLOSE of the bar acted on, never the bar label
      (RESEARCH_LEARNINGS 1.2).
    - Time to expiry comes from the measured expiry calendar, not an assumed
      weekday (OPTIONS_GREEKS_LEARNINGS 1).
    - Gross of costs, but the break-even cost is reported, so the result is
      not hostage to a spread assumption (RESEARCH_LEARNINGS 2.3).
    - TRAIN 2021-23 / VALID 2024 / TEST 2025-26.

Usage:
    python -m nse.backtest.test_delta_hedged_vol
    python -m nse.backtest.test_delta_hedged_vol --wings 4 --hedge-band 0.15
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from nse.backtest.nifty_loader import DEFAULT_ROOT, CACHE_DIR, clean_day
from nse.quant.black_scholes import greeks
from nse.quant.expiry_calendar import load_expiries

LOT = 65
R = 0.07
ENTRY_INTRADAY, EXIT_INTRADAY = "09:30", "15:20"
ENTRY_OVERNIGHT, EXIT_OVERNIGHT = "15:20", "09:20"
RESULT_CACHE = CACHE_DIR / "vol_harvest_legs.csv"


def _expiry_map() -> dict:
    """date -> calendar days to the next expiry (0 on expiry day itself)."""
    ex = load_expiries()
    dates = ex["date"].to_numpy()
    exp_dates = ex.loc[ex["is_expiry"], "date"].to_numpy()
    out, j = {}, 0
    for d in dates:
        while j < len(exp_dates) and exp_dates[j] < d:
            j += 1
        if j < len(exp_dates):
            out[pd.Timestamp(d)] = max((exp_dates[j] - d) / np.timedelta64(1, "D"), 0.0)
    return out


def _session_frame(path: Path) -> pd.DataFrame | None:
    """Wide frame: one row per (minute, strike) with CE/PE close and IV."""
    try:
        d = clean_day(pd.read_csv(path, usecols=[
            "datetime", "option_type", "close", "iv", "strike_price",
            "spot", "volume"]))
    except Exception:
        return None
    if d.empty:
        return None
    d["dt"] = pd.to_datetime(d["datetime"], errors="coerce")
    d = d.dropna(subset=["dt"])
    d["hhmm"] = d["dt"].dt.strftime("%H:%M")
    d["isC"] = d["option_type"].astype(str).str.upper().str.startswith("C")

    # A bar with zero volume did not trade; its "price" is stale and not
    # fillable (RESEARCH_LEARNINGS 4). Keep it for marking, flag for entry.
    d["tradable"] = d["volume"] > 0

    # Delta-hedging probes ~100 (minute, strike, side) points per session.
    # A boolean scan of 15k rows each time dominates the runtime, so index once.
    d.attrs["lookup"] = {
        (h, k, c): (px, iv, sp, tr) for h, k, c, px, iv, sp, tr in zip(
            d["hhmm"], d["strike_price"], d["isC"], d["close"], d["iv"],
            d["spot"], d["tradable"])
    }
    return d


def _leg(sess: pd.DataFrame, hhmm: str, strike: float, is_call: bool) -> dict | None:
    got = sess.attrs["lookup"].get((hhmm, strike, is_call))
    if got is None:
        return None
    px, iv, sp, tr = got
    return {"px": float(px), "iv": float(iv), "spot": float(sp),
            "tradable": bool(tr)}


def _delta(spot, K, T_years, iv_pct, is_call) -> float:
    if not np.isfinite(iv_pct) or iv_pct <= 0.5 or T_years <= 0:
        # No usable vol: fall back to the expiry payoff slope.
        return (1.0 if spot > K else 0.0) if is_call else (
            -1.0 if spot < K else 0.0)
    return greeks(spot, K, T_years, R, iv_pct / 100.0,
                  "C" if is_call else "P").delta


def run_intraday(files, exp_days, wings: int, hedge_band: float,
                 rebalance_min: int) -> pd.DataFrame:
    """Sell the ATM straddle at 09:30, buy it back at 15:20 the same session."""
    rows = []
    strike_step = 50.0
    for n, f in enumerate(files, 1):
        if n % 250 == 0:
            print(f"    ... {n}/{len(files)}", flush=True)
        sess = _session_frame(f)
        if sess is None:
            continue
        date = pd.Timestamp(f.name[6:16])
        dte_cal = exp_days.get(date)
        if dte_cal is None:
            continue
        T0 = max(dte_cal, 0.0) / 365.0

        at_entry = sess[sess["hhmm"] == ENTRY_INTRADAY]
        if at_entry.empty:
            continue
        spot0 = float(at_entry["spot"].median())
        K = float(at_entry.iloc[(at_entry["strike_price"] - spot0)
                                .abs().argsort()].iloc[0]["strike_price"])

        legs = {}
        for tag, k, isc, qty in (("ce", K, True, -1.0), ("pe", K, False, -1.0)):
            legs[tag] = (_leg(sess, ENTRY_INTRADAY, k, isc), k, isc, qty)
        if wings > 0:
            legs["ce_w"] = (_leg(sess, ENTRY_INTRADAY, K + wings * strike_step,
                                 True), K + wings * strike_step, True, +1.0)
            legs["pe_w"] = (_leg(sess, ENTRY_INTRADAY, K - wings * strike_step,
                                 False), K - wings * strike_step, False, +1.0)
        if any(v[0] is None or not v[0]["tradable"] or v[0]["px"] <= 0
               for v in legs.values()):
            continue

        credit = -sum(v[3] * v[0]["px"] for v in legs.values())

        # ── delta hedge across the session ───────────────────────────────
        hedge_pnl, hedge_units, n_rebal = 0.0, 0.0, 0
        if hedge_band > 0:
            marks = [t for t in sorted(sess["hhmm"].unique())
                     if ENTRY_INTRADAY <= t <= EXIT_INTRADAY]
            marks = marks[::rebalance_min]
            prev_spot = spot0
            for t in marks:
                sub = sess[sess["hhmm"] == t]
                if sub.empty:
                    continue
                spot_t = float(sub["spot"].median())
                hedge_pnl += hedge_units * (spot_t - prev_spot)
                prev_spot = spot_t
                frac = 1 - (marks.index(t) / max(len(marks) - 1, 1))
                T_t = max(T0 - (1 - frac) * (6.25 / 24) / 365.0, 1e-6)
                net = 0.0
                for lg, k, isc, qty in legs.values():
                    iv_t = _leg(sess, t, k, isc)
                    iv = iv_t["iv"] if iv_t else lg["iv"]
                    net += qty * _delta(spot_t, k, T_t, iv, isc)
                if abs(net + hedge_units) > hedge_band:
                    hedge_units = -net
                    n_rebal += 1
            sub = sess[sess["hhmm"] == EXIT_INTRADAY]
            if not sub.empty:
                hedge_pnl += hedge_units * (float(sub["spot"].median()) - prev_spot)

        exits = {tag: _leg(sess, EXIT_INTRADAY, k, isc)
                 for tag, (lg, k, isc, qty) in legs.items()}
        if any(v is None for v in exits.values()):
            continue
        debit = -sum(legs[t][3] * exits[t]["px"] for t in legs)
        opt_pnl = credit - debit
        spot_exit = float(exits["ce"]["spot"])

        rows.append({"date": date, "K": K, "dte_cal": dte_cal,
                     "credit": credit, "opt_pnl": opt_pnl,
                     "hedge_pnl": hedge_pnl, "n_rebal": n_rebal,
                     "pnl": opt_pnl + hedge_pnl,
                     "move_pct": (spot_exit - spot0) / spot0 * 100})
    return pd.DataFrame(rows)


def run_overnight(files, exp_days, wings: int) -> pd.DataFrame:
    """Sell at 15:20, buy back at 09:20 the NEXT session. No hedging possible
    while the market is shut — that is the whole point of the comparison."""
    rows = []
    strike_step = 50.0
    frames = {}
    for n, f in enumerate(files, 1):
        if n % 250 == 0:
            print(f"    ... {n}/{len(files)}", flush=True)
        date = pd.Timestamp(f.name[6:16])
        frames[date] = f
    dates = sorted(frames)

    prev = None
    for i, date in enumerate(dates):
        if i + 1 >= len(dates):
            break
        nxt = dates[i + 1]
        dte_cal = exp_days.get(date)
        if dte_cal is None or dte_cal < 1:
            continue                      # expires before the buy-back
        sess = _session_frame(frames[date])
        nsess = _session_frame(frames[nxt])
        if sess is None or nsess is None:
            continue
        at_entry = sess[sess["hhmm"] == ENTRY_OVERNIGHT]
        if at_entry.empty:
            continue
        spot0 = float(at_entry["spot"].median())
        K = float(at_entry.iloc[(at_entry["strike_price"] - spot0)
                                .abs().argsort()].iloc[0]["strike_price"])

        spec = [(K, True, -1.0), (K, False, -1.0)]
        if wings > 0:
            spec += [(K + wings * strike_step, True, +1.0),
                     (K - wings * strike_step, False, +1.0)]
        ent = [(_leg(sess, ENTRY_OVERNIGHT, k, c), q) for k, c, q in spec]
        ext = [(_leg(nsess, EXIT_OVERNIGHT, k, c), q) for k, c, q in spec]
        if any(e is None or not e["tradable"] or e["px"] <= 0 for e, _ in ent):
            continue
        if any(e is None or e["px"] < 0 for e, _ in ext):
            continue

        credit = -sum(q * e["px"] for e, q in ent)
        debit = -sum(q * e["px"] for e, q in ext)
        spot1 = float(ext[0][0]["spot"])
        rows.append({"date": date, "K": K, "dte_cal": dte_cal,
                     "credit": credit, "opt_pnl": credit - debit,
                     "hedge_pnl": 0.0, "n_rebal": 0, "pnl": credit - debit,
                     "move_pct": (spot1 - spot0) / spot0 * 100})
    return pd.DataFrame(rows)


def split_of(d):
    y = pd.Timestamp(d).year
    return "TRAIN" if y <= 2023 else ("VALID" if y == 2024 else "TEST")


def summarise(t: pd.DataFrame, label: str, n_legs: int) -> None:
    if t.empty or len(t) < 30:
        print(f"  {label:32} too few trades ({len(t)})")
        return
    t = t.copy()
    t["sp"] = t["date"].map(split_of)
    cells = [t[t["sp"] == k]["pnl"].mean() for k in ("TRAIN", "VALID", "TEST")]
    allpos = all(np.isfinite(c) and c > 0 for c in cells)
    # Break-even half-spread: cost per leg that would erase the mean edge.
    be = t["pnl"].mean() / n_legs
    print(f"  {label:32}{len(t):>6}{(t['pnl'] > 0).mean() * 100:>6.0f}%"
          f"{t['pnl'].mean():>9.2f}{t['pnl'].std():>9.2f}"
          f"{t['pnl'].min():>9.1f}{t['pnl'].kurtosis():>8.1f}"
          + "".join(f"{c:>8.2f}" for c in cells)
          + f"{be:>8.2f}" + ("  ALL +" if allpos else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wings", type=int, default=0,
                    help="strikes out for long wings; 0 = naked")
    ap.add_argument("--hedge-band", type=float, default=0.0,
                    help="rebalance when |net delta| exceeds this; 0 = unhedged")
    ap.add_argument("--rebalance-min", type=int, default=15)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files = sorted(DEFAULT_ROOT.glob("*/NIFTY_*_1m.csv"))
    if args.limit:
        files = files[-args.limit:]
    exp_days = _expiry_map()

    print("=" * 118)
    print("VARIANCE PREMIUM AS A TRADE — short ATM straddle on recorded prices")
    print("=" * 118)
    print(f"  {len(files):,} sessions. P&L in INDEX POINTS per 1 unit; "
          f"x{LOT} for rupees per lot. Gross of costs.")
    print(f"  {'variant':32}{'n':>6}{'win':>6}{'mean':>9}{'sd':>9}"
          f"{'worst':>9}{'kurt':>8}{'TRAIN':>8}{'VALID':>8}{'TEST':>8}{'BE/leg':>8}")

    print("\n  INTRADAY  (sell 09:30, buy back 15:20 — never held overnight)")
    for wings, band, lab in ((0, 0.0, "naked straddle, unhedged"),
                             (0, 0.15, "naked straddle, delta-hedged"),
                             (4, 0.0, "iron fly +/-200, unhedged"),
                             (4, 0.15, "iron fly +/-200, delta-hedged")):
        t = run_intraday(files, exp_days, wings, band, args.rebalance_min)
        summarise(t, lab, 4 if wings else 2)
        if wings == 0 and band == 0.0:
            t.to_csv(RESULT_CACHE, index=False)

    print("\n  OVERNIGHT (sell 15:20, buy back 09:20 next session — unhedgeable)")
    for wings, lab in ((0, "naked straddle"), (4, "iron fly +/-200")):
        t = run_overnight(files, exp_days, wings)
        summarise(t, lab, 4 if wings else 2)

    print("\n  'BE/leg' is the half-spread per leg that would erase the mean edge.")
    print("  A variant is only interesting if BE/leg comfortably exceeds a")
    print("  realistic NIFTY weekly spread, and TRAIN/VALID/TEST are all positive.")
    print("  'kurt' is the tail. A high mean with kurtosis in double digits is")
    print("  a strategy that works until the day it removes the account.")


if __name__ == "__main__":
    main()
