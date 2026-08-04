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
SLICE_CACHE = CACHE_DIR / "vol_harvest_slice.csv"

# Checkpoints the strategies actually touch. Reading all 1,255 raw files once
# per variant means ~7,500 CSV loads and hours of wall clock; the variants only
# ever look at ~25 minutes of each session, so extract those once and reuse.
REBALANCE_EVERY = 15


def _needed_minutes() -> list[str]:
    mins = {ENTRY_OVERNIGHT, EXIT_OVERNIGHT, ENTRY_INTRADAY, EXIT_INTRADAY}
    h, m = 9, 30
    while (h, m) <= (15, 20):
        mins.add(f"{h:02d}:{m:02d}")
        m += REBALANCE_EVERY
        h, m = h + m // 60, m % 60
    return sorted(mins)


def build_slice(force: bool = False) -> pd.DataFrame:
    """Extract only the (minute, strike, side) points any variant can use."""
    if SLICE_CACHE.exists() and not force:
        return pd.read_csv(SLICE_CACHE, parse_dates=["date"])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    want = set(_needed_minutes())
    files = sorted(DEFAULT_ROOT.glob("*/NIFTY_*_1m.csv"))
    out = []
    for i, f in enumerate(files, 1):
        try:
            d = clean_day(pd.read_csv(f, usecols=[
                "datetime", "option_type", "close", "iv", "strike_price",
                "spot", "volume"]))
        except Exception:
            continue
        if d.empty:
            continue
        d["dt"] = pd.to_datetime(d["datetime"], errors="coerce")
        d = d.dropna(subset=["dt"])
        d["hhmm"] = d["dt"].dt.strftime("%H:%M")
        d = d[d["hhmm"].isin(want)]
        if d.empty:
            continue
        d["isC"] = d["option_type"].astype(str).str.upper().str.startswith("C")
        d["tradable"] = d["volume"] > 0
        d["date"] = pd.Timestamp(f.name[6:16])
        out.append(d[["date", "hhmm", "strike_price", "isC", "close", "iv",
                      "spot", "tradable"]])
        if i % 250 == 0:
            print(f"    ... sliced {i}/{len(files)}", flush=True)
    df = pd.concat(out, ignore_index=True)
    df.to_csv(SLICE_CACHE, index=False)
    print(f"  cached {len(df):,} rows -> {SLICE_CACHE}")
    return df


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


class Session:
    """One session's usable points, indexed for O(1) probing.

    A bar with zero volume did not trade; its price is stale and not fillable
    (RESEARCH_LEARNINGS 4), so `tradable` is carried through to entry checks.
    """

    __slots__ = ("date", "lookup", "spot_at", "minutes")

    def __init__(self, date, g: pd.DataFrame):
        self.date = date
        self.lookup = {
            (h, k, c): (px, iv, sp, tr) for h, k, c, px, iv, sp, tr in zip(
                g["hhmm"], g["strike_price"], g["isC"], g["close"], g["iv"],
                g["spot"], g["tradable"])
        }
        self.spot_at = g.groupby("hhmm")["spot"].median().to_dict()
        self.minutes = sorted(self.spot_at)

    def leg(self, hhmm: str, strike: float, is_call: bool) -> dict | None:
        got = self.lookup.get((hhmm, float(strike), bool(is_call)))
        if got is None:
            return None
        px, iv, sp, tr = got
        return {"px": float(px), "iv": float(iv), "spot": float(sp),
                "tradable": bool(tr)}

    def strikes(self, hhmm: str) -> list[float]:
        return sorted({k for (h, k, _) in self.lookup if h == hhmm})

    def atm(self, hhmm: str) -> tuple[float, float] | None:
        spot = self.spot_at.get(hhmm)
        if spot is None or not np.isfinite(spot):
            return None
        ks = self.strikes(hhmm)
        if not ks:
            return None
        return min(ks, key=lambda k: abs(k - spot)), float(spot)


def load_sessions(force: bool = False) -> dict:
    df = build_slice(force)
    df["strike_price"] = df["strike_price"].astype(float)
    df["isC"] = df["isC"].astype(bool)
    return {d: Session(d, g) for d, g in df.groupby("date", sort=True)}


def _delta(spot, K, T_years, iv_pct, is_call) -> float:
    if not np.isfinite(iv_pct) or iv_pct <= 0.5 or T_years <= 0:
        # No usable vol: fall back to the expiry payoff slope.
        return (1.0 if spot > K else 0.0) if is_call else (
            -1.0 if spot < K else 0.0)
    return greeks(spot, K, T_years, R, iv_pct / 100.0,
                  "C" if is_call else "P").delta


STRIKE_STEP = 50.0


def _spec(K: float, wings: int,
          avail: list[float] | None = None) -> list[tuple[float, bool, float]]:
    """Short ATM straddle, plus long wings when defined-risk is wanted.

    CLAMPING, AND WHY IT IS NOT OPTIONAL: our ladder only stores ATM+/-10
    RELATIVE TO THE FILE'S OWN REFERENCE ATM. When the index moves, the 09:30
    ATM drifts off that reference and the nominal wing falls outside the stored
    strikes. Dropping those sessions looks harmless and is not: the wing goes
    missing precisely on the days the index moved, which are the losing days
    for a short straddle. Measured, the dropped sessions averaged -140 index
    points against +7 for the kept ones at +/-200, and at +/-500 only 169 of
    981 sessions survived with a mean absolute move of 0.085%.

    That is conditioning on the outcome and it manufactured an edge of +19.67
    points out of nothing. So instead of dropping, clamp the wing to the
    outermost strike we DO hold. The market had the nominal strike; only our
    data does not, and a slightly narrower wing is a real tradeable structure.

    `avail` must be the INTERSECTION of the strikes present at entry and at
    exit. Clamping on entry strikes alone fixed nothing: the ladder re-centres
    intraday, so on a big move the wing is quoted at 09:30 and gone by 15:20.
    That accounted for every one of the 279 dropped legs at +/-400.
    """
    s = [(K, True, -1.0), (K, False, -1.0)]
    if wings > 0:
        hi, lo = K + wings * STRIKE_STEP, K - wings * STRIKE_STEP
        if avail:
            hi = min(max(avail), hi)
            lo = max(min(avail), lo)
        s += [(hi, True, +1.0), (lo, False, +1.0)]
    return s


def run_intraday(sessions: dict, exp_days: dict, wings: int,
                 hedge_band: float) -> pd.DataFrame:
    """Sell the ATM straddle at 09:30, buy it back at 15:20 the same session."""
    rows = []
    for date, sess in sessions.items():
        dte_cal = exp_days.get(date)
        if dte_cal is None:
            continue
        T0 = max(dte_cal, 0.0) / 365.0
        got = sess.atm(ENTRY_INTRADAY)
        if got is None:
            continue
        K, spot0 = got

        both = sorted(set(sess.strikes(ENTRY_INTRADAY))
                      & set(sess.strikes(EXIT_INTRADAY)))
        spec = _spec(K, wings, both)
        ent = [(sess.leg(ENTRY_INTRADAY, k, c), k, c, q) for k, c, q in spec]
        if any(e is None or not e["tradable"] or e["px"] <= 0
               for e, *_ in ent):
            continue
        ext = [(sess.leg(EXIT_INTRADAY, k, c), q) for k, c, q in spec]
        if any(e is None or e["px"] < 0 for e, _ in ext):
            continue

        credit = -sum(q * e["px"] for e, _, _, q in ent)
        debit = -sum(q * e["px"] for e, q in ext)

        # ── delta hedge across the session ───────────────────────────────
        hedge_pnl, hedge_units, n_rebal = 0.0, 0.0, 0
        if hedge_band > 0:
            marks = [t for t in sess.minutes
                     if ENTRY_INTRADAY <= t <= EXIT_INTRADAY]
            prev_spot = spot0
            for j, t in enumerate(marks):
                spot_t = sess.spot_at.get(t)
                if spot_t is None or not np.isfinite(spot_t):
                    continue
                hedge_pnl += hedge_units * (spot_t - prev_spot)
                prev_spot = spot_t
                if t == EXIT_INTRADAY:
                    break
                # Time decays across the session; a session is 6.25/24 of a day.
                elapsed = (j / max(len(marks) - 1, 1)) * (6.25 / 24) / 365.0
                T_t = max(T0 - elapsed, 1e-6)
                net = 0.0
                for e, k, c, q in ent:
                    cur = sess.leg(t, k, c)
                    net += q * _delta(spot_t, k, T_t,
                                      cur["iv"] if cur else e["iv"], c)
                if abs(net + hedge_units) > hedge_band:
                    hedge_units = -net
                    n_rebal += 1

        spot_exit = float(ext[0][0]["spot"])
        rows.append({"date": date, "K": K, "dte_cal": dte_cal,
                     "credit": credit, "opt_pnl": credit - debit,
                     "hedge_pnl": hedge_pnl, "n_rebal": n_rebal,
                     "pnl": credit - debit + hedge_pnl,
                     "move_pct": (spot_exit - spot0) / spot0 * 100})
    return pd.DataFrame(rows)


def run_overnight(sessions: dict, exp_days: dict, wings: int) -> pd.DataFrame:
    """Sell at 15:20, buy back at 09:20 the NEXT session. Unhedgeable while the
    market is shut — which is exactly what the comparison is testing."""
    rows = []
    dates = sorted(sessions)
    for i, date in enumerate(dates[:-1]):
        dte_cal = exp_days.get(date)
        if dte_cal is None or dte_cal < 1:
            continue                      # expires before the buy-back
        sess, nsess = sessions[date], sessions[dates[i + 1]]
        got = sess.atm(ENTRY_OVERNIGHT)
        if got is None:
            continue
        K, spot0 = got

        both = sorted(set(sess.strikes(ENTRY_OVERNIGHT))
                      & set(nsess.strikes(EXIT_OVERNIGHT)))
        spec = _spec(K, wings, both)
        ent = [(sess.leg(ENTRY_OVERNIGHT, k, c), q) for k, c, q in spec]
        ext = [(nsess.leg(EXIT_OVERNIGHT, k, c), q) for k, c, q in spec]
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
    ap.add_argument("--rebuild", action="store_true", help="rebuild the slice cache")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    sessions = load_sessions(force=args.rebuild)
    if args.limit:
        keep = sorted(sessions)[-args.limit:]
        sessions = {d: sessions[d] for d in keep}
    exp_days = _expiry_map()

    print("=" * 118)
    print("VARIANCE PREMIUM AS A TRADE — short ATM straddle on recorded prices")
    print("=" * 118)
    print(f"  {len(sessions):,} sessions. P&L in INDEX POINTS per 1 unit; "
          f"x{LOT} for rupees per lot. Gross of costs.")
    print(f"  {'variant':32}{'n':>6}{'win':>6}{'mean':>9}{'sd':>9}"
          f"{'worst':>9}{'kurt':>8}{'TRAIN':>8}{'VALID':>8}{'TEST':>8}{'BE/leg':>8}")

    print("\n  INTRADAY  (sell 09:30, buy back 15:20 — never held overnight)")
    for wings, band, lab in ((0, 0.0, "naked straddle, unhedged"),
                             (0, 0.15, "naked straddle, delta-hedged"),
                             (4, 0.0, "iron fly +/-200, unhedged"),
                             (4, 0.15, "iron fly +/-200, delta-hedged")):
        t = run_intraday(sessions, exp_days, wings, band)
        summarise(t, lab, 4 if wings else 2)
        if wings == 0 and band == 0.0:
            t.to_csv(RESULT_CACHE, index=False)

    print("\n  OVERNIGHT (sell 15:20, buy back 09:20 next session — unhedgeable)")
    for wings, lab in ((0, "naked straddle"), (4, "iron fly +/-200")):
        t = run_overnight(sessions, exp_days, wings)
        summarise(t, lab, 4 if wings else 2)

    print("\n  'BE/leg' is the half-spread per leg that would erase the mean edge.")
    print("  A variant is only interesting if BE/leg comfortably exceeds a")
    print("  realistic NIFTY weekly spread, and TRAIN/VALID/TEST are all positive.")
    print("  'kurt' is the tail. A high mean with kurtosis in double digits is")
    print("  a strategy that works until the day it removes the account.")


if __name__ == "__main__":
    main()
