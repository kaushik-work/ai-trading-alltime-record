"""Greeks lens — reads the volatility surface for directional pressure.

MEASURED VERDICT (2026-08-07): NO DIRECTIONAL EDGE. STAYS IN SHADOW.

    split      n     long%   edge      t       p
    TRAIN    1600    56.4%   -0.54bps  -0.76   0.4485
    VALIDATE  784    86.6%   -1.08bps  -1.48   0.1405

390 sessions, 30-minute decision grid, 60-minute forward horizon, signed return
against a mix-matched baseline. Negative in both splits, significant in
neither, and break-even spread is NONE — costs eat it at the tick floor.

Two things that finding is NOT. It is not a reason to sweep horizons and grids
until something clears p<0.05: with enough hypotheses one always does
(RESEARCH_LEARNINGS 2.1). And it is not evidence the surface is uninformative
about anything — only that the 25-delta risk reversal does not predict the next
hour's index direction.

The calibration also does not transport. Long fraction is 56.4% on TRAIN by
construction but 86.6% on VALIDATE, so the 2021-2023 neutral skew level had
drifted by 2024 and the lens read the flatter surface as persistently bullish.
A fixed structural constant is the wrong shape for this quantity; a trailing
reference would be a DIFFERENT lens needing its own measurement, not a tuned
version of this one.

Kept in the repo because the plumbing is correct and reusable, and because the
next lens should not have to rediscover that this one was measured and failed.

WHAT IT LOOKS AT

The 25-delta risk reversal: the implied vol of the 25-delta call minus that of
the 25-delta put, normalised by ATM vol.

    rr_norm = (IV(25d call) - IV(25d put)) / IV(atm)

An equity index carries a structurally NEGATIVE risk reversal — puts are
permanently richer than equidistant calls because index hedging demand is
one-sided. So the raw sign says nothing. What can carry information is the
DEVIATION from that structural level: skew flatter than normal means call
demand has appeared, steeper than normal means the hedging bid has intensified.

    SKEW_NEUTRAL is that structural level, and it is a CALIBRATION, not a fact.
    It defaults to 0.0, which is deliberately the wrong value — it makes the
    lens read raw sign and therefore lean permanently bearish. Calibrate it on
    TRAIN before drawing any conclusion, and never on VALIDATE or TEST.

WHY IT REPRICES EVERYTHING

Every Greek here is solved from the current mark. The archive ships an `iv`
column and Mongo holds stored Greek vectors; both are ignored on purpose.
Stored Greeks are up to 100% wrong inside 2 DTE, and a file in this repo
already documented that rule and then broke it one module later — computing
hedge deltas from Black-Scholes at T->0 and losing 3-5x the credit on two
sessions where the index barely moved. See docs/OPTIONS_GREEKS_LEARNINGS.md
sections 3 and 9.

The same rule is why this lens ABSTAINS below MIN_TRUSTWORTHY_DTE rather than
producing a confident number from an unusable one. On expiry day gamma explodes
and delta flips violently around the strike; there is no honest 25-delta strike
to find.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from nse.lenses.base import BaseLens, Direction, LensVerdict, abstain
from nse.snapshot import RISK_FREE_RATE, MarketSnapshot

logger = logging.getLogger(__name__)

# Target delta for the risk reversal. 25 is the market convention: far enough
# out to carry skew information, near enough to stay liquid.
TARGET_DELTA = 0.25

# Structural skew level for NIFTY, as rr_norm.
#
# MEASURED on TRAIN (2021-2023) only, 150 sessions / 1,092 verdicts, at a
# 30-minute grid:
#
#     median -0.2098   mean -0.2124   sd 0.0995
#     rr_norm is NEGATIVE in 98.4% of observations
#
# That 98.4% is the whole reason this constant exists. Left at 0.0 the lens
# reads raw sign and votes SHORT essentially always — a permanent bearish bias
# dressed up as a signal, which is how a strategy ends up with a gate it can
# never clear. Centring on the measured median makes the vote balanced by
# construction (50.0% long / 50.0% short on TRAIN) so anything that survives is
# timing rather than a structural tilt.
#
# The median is used rather than the mean: it is robust to the crisis sessions
# where skew blows out, which would otherwise drag the neutral level with them.
# See RESEARCH_LEARNINGS section 1.3 — measure the distribution of the quantity
# a threshold gates BEFORE setting the threshold.
SKEW_NEUTRAL = -0.2098

# Deviation from SKEW_NEUTRAL that counts as full conviction. Set to the p90 of
# |rr_norm - median| on the same TRAIN slice, so roughly the top decile of
# skew dislocations saturate the vote and the rest scale linearly below it.
FULL_CONVICTION_DEV = 0.1641

# Minimum strikes per side needed before the surface is worth reading at all.
MIN_STRIKES_PER_SIDE = 5


class GreeksLens(BaseLens):
    name = "greeks"
    backtestable = True

    def _evaluate(self, snap: MarketSnapshot) -> LensVerdict:
        if not snap.greeks_trustworthy:
            return abstain(
                self.name,
                f"{snap.dte:.2f} DTE — inside the zone where analytic Greeks "
                f"stop meaning anything")

        surface = self._surface(snap)
        if surface is None:
            return abstain(self.name, "could not reprice a usable surface")

        calls, puts, atm_iv = surface
        if len(calls) < MIN_STRIKES_PER_SIDE or len(puts) < MIN_STRIKES_PER_SIDE:
            return abstain(
                self.name,
                f"only {len(calls)}C/{len(puts)}P strikes repriced — too thin "
                f"to locate a 25-delta wing")
        if atm_iv is None or atm_iv <= 0:
            return abstain(self.name, "no usable ATM vol")

        iv_c25 = _iv_at_delta(calls, TARGET_DELTA)
        iv_p25 = _iv_at_delta(puts, -TARGET_DELTA)
        if iv_c25 is None or iv_p25 is None:
            return abstain(
                self.name,
                "25-delta wing not bracketed by quoted strikes "
                f"(call={iv_c25}, put={iv_p25})")

        rr = iv_c25 - iv_p25
        rr_norm = rr / atm_iv
        dev = rr_norm - SKEW_NEUTRAL

        conviction = min(1.0, abs(dev) / FULL_CONVICTION_DEV) if FULL_CONVICTION_DEV else 0.0
        if dev > 0:
            direction = Direction.LONG
            why = "calls bid relative to puts"
        elif dev < 0:
            direction = Direction.SHORT
            why = "put hedging bid steepening"
        else:
            direction = Direction.NEUTRAL
            why = "skew sitting on its neutral level"

        # Parity check, journaled but NOT voted on. Under put-call parity a
        # call and a put on the same strike and expiry must imply the same vol;
        # a persistent gap is carry and dividends, a moving one is order flow.
        # Recorded so attribution can later tell whether it carried any of the
        # signal, without giving it a vote it has not earned.
        parity_gap = _atm_parity_gap(calls, puts, snap.atm)

        return LensVerdict(
            lens=self.name,
            direction=direction,
            confidence=conviction,
            rationale=(f"25d risk reversal {rr:+.2f} vol pts "
                       f"({rr_norm:+.3f} of ATM {atm_iv * 100:.1f}%), "
                       f"{abs(dev):.3f} from neutral — {why}"),
            features={
                "rr_vol_points": round(rr, 4),
                "rr_norm": round(rr_norm, 5),
                "deviation": round(dev, 5),
                "iv_call_25d": round(iv_c25, 5),
                "iv_put_25d": round(iv_p25, 5),
                "atm_iv": round(atm_iv, 5),
                "atm_parity_gap": None if parity_gap is None else round(parity_gap, 5),
                "n_calls": len(calls),
                "n_puts": len(puts),
                "dte": round(snap.dte, 3),
                "skew_neutral_used": SKEW_NEUTRAL,
            },
        )

    # ── repricing ────────────────────────────────────────────────────────────
    def _surface(self, snap: MarketSnapshot
                 ) -> Optional[tuple[pd.DataFrame, pd.DataFrame, Optional[float]]]:
        """Solve IV and delta for every quoted contract, from the mark.

        Returns (calls, puts, atm_iv) with each frame carrying strike/iv/delta,
        or None when nothing repriced.
        """
        chain = snap.chain
        if chain is None or chain.empty:
            return None

        col = "option_type" if "option_type" in chain.columns else "side"
        if col not in chain.columns or "strike" not in chain.columns:
            return None

        df = chain.copy()
        df["_type"] = df[col].astype(str).str.upper().str[0]
        df["_mark"] = _mark_series(df)
        df = df[(df["_mark"] > 0) & df["strike"].notna()]

        # A no-trade minute repeats the previous print; repricing it would put
        # a stale premium into the surface as though it were a live quote.
        if "no_trade" in df.columns:
            df = df[~df["no_trade"].astype(bool)]
        if df.empty:
            return None

        from nse.data.greeks_vectorized import option_greeks_array

        opt = np.where(df["_type"].to_numpy() == "C", 1, 0)
        t = np.full(len(df), snap.T)
        try:
            g = option_greeks_array(
                df["spot"].to_numpy(dtype=float) if "spot" in df.columns
                else np.full(len(df), snap.spot),
                df["strike"].to_numpy(dtype=float),
                t, opt, df["_mark"].to_numpy(dtype=float),
                RISK_FREE_RATE, 0.0,
            )
        except Exception as e:
            logger.debug("greeks lens: reprice failed: %s", e)
            return None

        df["iv"] = g.get("iv")
        df["delta"] = g.get("delta")
        df = df[df["iv"].notna() & df["delta"].notna() & (df["iv"] > 0)]
        if df.empty:
            return None

        calls = df[df["_type"] == "C"].sort_values("strike")
        puts = df[df["_type"] == "P"].sort_values("strike")

        atm_iv = _atm_iv(calls, puts, snap.atm)
        return calls, puts, atm_iv


# ── helpers ──────────────────────────────────────────────────────────────────
def _mark_series(df: pd.DataFrame) -> pd.Series:
    """Best available transactable price per row.

    Mid when the book is genuinely two-sided, otherwise LTP. Historical rows
    have no book at all — Mongo bid/ask were zero until 2026-08-04 — so they
    fall through to LTP rather than reporting a mid of zero.
    """
    ltp = pd.to_numeric(df.get("ltp", df.get("mark", 0)), errors="coerce").fillna(0.0)
    if "bid" in df.columns and "ask" in df.columns:
        bid = pd.to_numeric(df["bid"], errors="coerce").fillna(0.0)
        ask = pd.to_numeric(df["ask"], errors="coerce").fillna(0.0)
        mid = (bid + ask) / 2.0
        return mid.where((bid > 0) & (ask > 0), ltp)
    return ltp


def _atm_iv(calls: pd.DataFrame, puts: pd.DataFrame, atm: int) -> Optional[float]:
    """ATM vol as the average of the two ATM legs, or whichever one exists."""
    vals = []
    for side in (calls, puts):
        if side.empty:
            continue
        hit = side[side["strike"] == atm]
        if not hit.empty:
            vals.append(float(hit.iloc[0]["iv"]))
    if vals:
        return sum(vals) / len(vals)
    # No exact ATM strike quoted — fall back to the nearest available.
    both = pd.concat([calls, puts])
    if both.empty:
        return None
    nearest = both.iloc[(both["strike"] - atm).abs().argsort()[:2]]
    return float(nearest["iv"].mean()) if not nearest.empty else None


def _iv_at_delta(side: pd.DataFrame, target: float) -> Optional[float]:
    """IV at a target delta, linearly interpolated between quoted strikes.

    Returns None when the target is NOT bracketed by real quotes. Extrapolating
    past the quoted wing would invent a vol for a strike nobody is showing —
    and the recorded ladder re-centres intraday, so the wing goes missing
    precisely on the days the index moves. Refusing to extrapolate is what
    keeps those sessions from silently becoming a fabricated edge.
    """
    if side.empty or "delta" not in side.columns:
        return None
    d = side[["delta", "iv"]].dropna().copy()
    if len(d) < 2:
        return None
    d = d.sort_values("delta")
    deltas = d["delta"].to_numpy(dtype=float)
    ivs = d["iv"].to_numpy(dtype=float)

    lo, hi = deltas.min(), deltas.max()
    if not (lo <= target <= hi):
        return None
    return float(np.interp(target, deltas, ivs))


def _atm_parity_gap(calls: pd.DataFrame, puts: pd.DataFrame,
                    atm: int) -> Optional[float]:
    """IV(put) - IV(call) at the ATM strike. Zero under strict parity."""
    c = calls[calls["strike"] == atm]
    p = puts[puts["strike"] == atm]
    if c.empty or p.empty:
        return None
    return float(p.iloc[0]["iv"]) - float(c.iloc[0]["iv"])
