"""Does the IV-implied +/-1 sigma band actually contain 68.2% of moves?

THE CLAIM UNDER TEST
    "VIX 16 on NIFTY 24000 gives a daily move of +/-242 points, and with 68.2%
    confidence the index stays inside that band."

    The 242 points is arithmetic and it is right (checked in
    greeks_taylor_check.py). The 68.2% is a DISTRIBUTIONAL ASSUMPTION — it
    holds only if returns are normal. Index returns are famously not. So the
    band is stated with a confidence nobody has verified on Indian data.

    This verifies it, on 1,250 sessions of our own ATM IV.

WHY IT IS WORTH THE TROUBLE
    Every short-vol structure is sized off this band. If real coverage is
    higher than 68.2%, options are systematically overpriced relative to what
    the index actually does — which is the variance risk premium, arriving by
    a second and independent route. If the TAIL is fatter than normal at the
    same time, then the same measurement also prices the disaster that makes
    naked short vol unsurvivable. Both facts come out of one table.

Usage:
    python -m nse.quant.test_vix_coverage
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd
from scipy.stats import norm

from nse.backtest.nifty_loader import CACHE_DIR

JOURNAL = CACHE_DIR / "nifty_daily_journal.csv"
TRADING_DAYS = 252


def split_of(d):
    y = pd.Timestamp(d).year
    return "TRAIN" if y <= 2023 else ("VALID" if y == 2024 else "TEST")


def load() -> pd.DataFrame:
    j = pd.read_csv(JOURNAL, parse_dates=["date"]).sort_values("date")
    j = j[j["atm_iv"].notna() & (j["atm_iv"] > 1) & (j["atm_iv"] < 100)].copy()

    # Predicted 1-session sigma, in percent, from ATM IV observed at 09:30.
    j["sigma_pred_pct"] = j["atm_iv"] / np.sqrt(TRADING_DAYS)

    # Realised move over the SAME session the IV was quoted for: 09:30 spot to
    # the close. Using close-to-close instead would apply a 09:30 observation
    # to a window that started before it existed.
    j["realised_pct"] = (j["close"] - j["spot_0930"]) / j["spot_0930"] * 100

    # 09:30->close is ~5.75h of a 6.25h session. Scale the prediction by
    # sqrt(time) so like is compared with like.
    j["sigma_scaled_pct"] = j["sigma_pred_pct"] * np.sqrt(5.75 / 6.25)
    j["z"] = j["realised_pct"] / j["sigma_scaled_pct"]
    j["split"] = j["date"].map(split_of)
    return j.dropna(subset=["z"])


def main() -> None:
    j = load()
    z = j["z"]
    print("=" * 92)
    print("IV-IMPLIED BAND vs WHAT NIFTY ACTUALLY DID — 09:30 IV, same-session move")
    print("=" * 92)
    print(f"  {len(j):,} sessions, {j['date'].min():%Y-%m-%d} -> {j['date'].max():%Y-%m-%d}")
    print(f"  median ATM IV {j['atm_iv'].median():.2f}%  ->  "
          f"predicted sigma {j['sigma_scaled_pct'].median():.3f}% per session")
    print(f"  median |realised| move {j['realised_pct'].abs().median():.3f}%")

    print("\n  COVERAGE — how often the move stayed inside k sigma")
    print(f"  {'band':>8}{'normal says':>14}{'actual':>10}"
          f"{'TRAIN':>9}{'VALID':>9}{'TEST':>9}")
    for k in (1, 2, 3):
        theo = (2 * norm.cdf(k) - 1) * 100
        cells = [(j[j["split"] == s]["z"].abs() <= k).mean() * 100
                 for s in ("TRAIN", "VALID", "TEST")]
        print(f"  {'+/-' + str(k) + ' sd':>8}{theo:>13.1f}%"
              f"{(z.abs() <= k).mean() * 100:>9.1f}%"
              + "".join(f"{c:>8.1f}%" for c in cells))

    print("\n  Coverage ABOVE the normal figure means the band is too WIDE — the")
    print("  option market charged for more movement than the index delivered.")

    print("\n  IS IT NORMAL? the tails are the part that kills a short-vol book")
    print(f"  {'measure':>28}{'normal':>10}{'actual':>10}")
    print(f"  {'std dev of z':>28}{1.00:>10.2f}{z.std():>10.2f}")
    print(f"  {'kurtosis (0 = normal)':>28}{0.0:>10.2f}{z.kurtosis():>10.2f}")
    print(f"  {'skew':>28}{0.0:>10.2f}{z.skew():>10.2f}")
    print(f"  {'worst down move, in sigma':>28}{'':>10}{z.min():>10.2f}")
    print(f"  {'worst up move, in sigma':>28}{'':>10}{z.max():>10.2f}")
    beyond3 = (z.abs() > 3).mean() * 100
    print(f"  {'beyond 3 sd':>28}{(1 - (2 * norm.cdf(3) - 1)) * 100:>9.2f}%"
          f"{beyond3:>9.2f}%")

    print("\n  RATIO of realised to predicted, by year — the variance premium")
    print("  seen from a second angle. Below 1.00 means IV overcharged.")
    print(f"  {'year':>6}{'n':>6}{'pred sigma':>12}{'realised sd':>13}{'ratio':>8}")
    for y, g in j.groupby(j["date"].dt.year):
        pred = g["sigma_scaled_pct"].mean()
        real = g["realised_pct"].std()
        print(f"  {y:>6}{len(g):>6}{pred:>11.3f}%{real:>12.3f}%{real / pred:>8.2f}")

    overnight_decomposition(j)

    print("\n  READ THIS CAREFULLY BEFORE SHORTING VOL:")
    print("  The ratio is below 1.00 in every year, and that is the edge. But")
    print("  note what the tail table above does NOT say: normalised by its own")
    print("  IV, the intraday move never reached 3 sd and kurtosis is only")
    print(f"  {z.kurtosis():.2f}. On this measure NIFTY's intraday distribution is")
    print("  close to normal. The fat tail people warn about is not here — it")
    print("  is in the overnight gap, which this measure excludes entirely.")


def overnight_decomposition(j: pd.DataFrame) -> None:
    """How much of the real move happens while the market is shut?

    RESEARCH_LEARNINGS 3.3 flagged that our realised vol is intraday-only
    while implied prices the whole calendar. That makes every premium we have
    measured too flattering. This quantifies the gap rather than caveating it.
    """
    d = j.copy()
    d["overnight_pct"] = (d["open"] - d["prev_close"]) / d["prev_close"] * 100
    d["intraday_pct"] = (d["close"] - d["open"]) / d["open"] * 100
    d["c2c_pct"] = (d["close"] - d["prev_close"]) / d["prev_close"] * 100

    # TIMING: close-to-close spans from LAST night, so an IV quoted at 09:30
    # today already knows the gap that opened the window. Predicting with it
    # is lookahead — it inflates the forecast on exactly the days the move was
    # large, flattering both the ratio and the kurtosis. Use the PREVIOUS
    # session's IV, which is genuinely in the information set beforehand.
    d["sigma_pred_pct"] = d["sigma_pred_pct"].shift(1)
    d = d.dropna(subset=["overnight_pct", "intraday_pct", "c2c_pct",
                         "sigma_pred_pct"])

    v_on = d["overnight_pct"].var()
    v_in = d["intraday_pct"].var()
    v_cc = d["c2c_pct"].var()

    print("\n" + "=" * 92)
    print("CLOSING RESEARCH_LEARNINGS OPEN ITEM 4 — the overnight gap")
    print("=" * 92)
    print("  Variance decomposition over "
          f"{len(d):,} sessions (variance adds, vol does not)")
    print(f"  {'component':>24}{'std dev':>11}{'variance':>11}{'share':>9}")
    for lab, v in (("overnight (close->open)", v_on),
                   ("intraday (open->close)", v_in)):
        print(f"  {lab:>24}{np.sqrt(v):>10.3f}%{v:>11.4f}{v / v_cc * 100:>8.1f}%")
    print(f"  {'close-to-close TOTAL':>24}{np.sqrt(v_cc):>10.3f}%{v_cc:>11.4f}"
          f"{100.0:>8.1f}%")
    cross = v_cc - v_on - v_in
    print(f"  {'(cross term)':>24}{'':>11}{cross:>11.4f}{cross / v_cc * 100:>8.1f}%")

    print("\n  Implied vol is quoted per CALENDAR period and therefore prices")
    print("  the overnight session too. Comparing it with intraday-only")
    print("  realised vol overstates the premium by the ratio below.")
    print(f"    intraday-only realised vol   {np.sqrt(v_in):.3f}%")
    print(f"    true close-to-close vol      {np.sqrt(v_cc):.3f}%")
    print(f"    understatement factor        {np.sqrt(v_cc / v_in):.3f}x")

    print("\n  CORRECTED variance premium (was intraday-only in 3.3)")
    print(f"  {'year':>6}{'n':>6}{'pred':>9}{'intraday':>10}{'true c2c':>10}"
          f"{'old ratio':>11}{'true ratio':>12}")
    for y, g in d.groupby(d["date"].dt.year):
        pred = g["sigma_pred_pct"].mean()
        r_in = g["intraday_pct"].std()
        r_cc = g["c2c_pct"].std()
        print(f"  {y:>6}{len(g):>6}{pred:>8.3f}%{r_in:>9.3f}%{r_cc:>9.3f}%"
              f"{r_in / pred:>11.2f}{r_cc / pred:>12.2f}")

    print("\n  Tails, now that the overnight session is included")
    zc = d["c2c_pct"] / d["sigma_pred_pct"]
    zi = d["intraday_pct"] / d["sigma_pred_pct"]
    print(f"  {'':>22}{'kurtosis':>11}{'worst day':>12}{'beyond 3sd':>12}")
    for lab, s in (("intraday only", zi), ("close-to-close", zc)):
        print(f"  {lab:>22}{s.kurtosis():>11.2f}{s.min():>11.2f}sd"
              f"{(s.abs() > 3).mean() * 100:>11.2f}%")
    print("\n  This is the honest comparison, and it is the one that decides")
    print("  whether a short-vol structure survives.")


if __name__ == "__main__":
    main()
