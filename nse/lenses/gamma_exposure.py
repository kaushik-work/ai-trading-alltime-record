"""Gamma-exposure lens — the mechanism that makes OI walls matter.

WHERE THIS CAME FROM

The operator observed "OI spikes when delta or vega starts trending" and asked
for it to be learned from YouTube and Reddit. The observation is worth testing;
the sourcing was the problem. This lens is the measurable version, built on the
published dealer-hedging mechanism rather than on folklore:

    Dealers who are net SHORT gamma must buy as price rises and sell as it
    falls. Their hedging AMPLIFIES moves.

    Dealers who are net LONG gamma sell into strength and buy weakness. Their
    hedging DAMPENS moves, and price tends to pin near large open interest.

That is mechanical, not sentiment, and it has empirical support outside equities
— a measured FX study found negative gamma exposure of about -1000bn USD raised
EURUSD volatility by 0.7% (ScienceDirect S0261560622000304).

It also explains something this repo already measured. `volume_oi` reads OI
walls and is the one lens with an edge (+1.66/+1.49 bps). Gamma exposure is a
candidate MECHANISM for why a wall would matter at all: a wall is only a wall if
somebody is hedging against it. If GEX carries information that OI walls alone
do not, these two should be measurably different rather than the same idea
twice — which is exactly what the pairwise correlation check is for
(RESEARCH_LEARNINGS section 3.12, where vwap turned out to be volume_oi wearing
a hat).

CONVENTION, DECLARED BEFORE MEASUREMENT

Dealer positioning is ASSUMED, because the exchange does not publish it. The
standard retail-flow assumption is that customers buy calls and sell puts, so
dealers are short calls and long puts:

    GEX = sum over strikes of ( gamma * OI * spot^2 * 0.01 ) with
          CALLS counted POSITIVE and PUTS counted NEGATIVE.

Then:

    GEX strongly POSITIVE  -> dealers dampen -> price pins near the gamma flip
                              -> fade moves away from it        (mean reversion)
    GEX strongly NEGATIVE  -> dealers amplify -> moves extend
                              -> follow the move                (continuation)

The direction therefore comes from where spot sits relative to the ZERO-GAMMA
FLIP POINT, and the sign of GEX decides whether to fade or follow.

THE ASSUMPTION IS THE WEAK LINK AND IS STATED, NOT HIDDEN. If Indian retail
flow is not "long calls, short puts" — and on expiry day in NIFTY it is very
plausibly the opposite, with heavy retail option SELLING — the sign convention
is backwards and the lens measures the negative of what it claims. That is a
one-bit error, discoverable by measurement, and it must NOT be fixed by
flipping the sign after seeing the result (section 3.13).

Greeks are not trusted under 2 DTE (OPTIONS_GREEKS_LEARNINGS section 3), and
gamma is the worst-behaved of them near expiry — it is precisely the Greek that
explodes as T goes to zero. This lens abstains there rather than reading a
number that is confidently wrong.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from nse.lenses.base import BaseLens, Direction, LensVerdict, abstain
from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

MIN_STRIKES_PER_SIDE = 4

#: Distance from the gamma flip point, in ATR, at which conviction saturates.
#: CALIBRATED ON TRAIN.
FLIP_FULL_CONVICTION_ATR = 2.0

#: |GEX| below this fraction of its own recent scale is "no regime" — the
#: dealer book is too balanced for its hedging to dominate anything.
GEX_NEUTRAL_BAND = 0.15


class GammaExposureLens(BaseLens):
    name = "gamma_exposure"
    backtestable = True

    def _evaluate(self, snap: MarketSnapshot) -> LensVerdict:
        if not snap.greeks_trustworthy:
            return abstain(self.name,
                           f"{snap.dte:.2f} DTE — gamma is the Greek that "
                           f"explodes near expiry; not reading it here")

        chain = snap.chain
        if chain is None or chain.empty:
            return abstain(self.name, "no chain")
        need = {"strike", "oi"}
        if not need <= set(chain.columns):
            return abstain(self.name, "chain lacks strike/oi")

        df = chain.copy()
        col = "option_type" if "option_type" in df.columns else "side"
        if col not in df.columns:
            return abstain(self.name, "chain lacks option_type")
        df["_t"] = df[col].astype(str).str.upper().str[0]

        if "gamma" not in df.columns:
            try:
                from nse.data.greeks_vectorized import add_greeks_to_dataframe
                if "timestamp" not in df.columns:
                    df["timestamp"] = pd.Timestamp(snap.ts)
                if "spot" not in df.columns:
                    df["spot"] = snap.spot
                if "expiry" not in df.columns:
                    df["expiry"] = pd.Timestamp(snap.expiry)
                add_greeks_to_dataframe(df)
            except Exception as e:
                return abstain(self.name, f"could not compute gamma: {e}")
        if "gamma" not in df.columns:
            return abstain(self.name, "gamma unavailable")

        df["gamma"] = pd.to_numeric(df["gamma"], errors="coerce").fillna(0.0)
        df["oi"] = pd.to_numeric(df["oi"], errors="coerce").fillna(0.0)
        calls = df[df["_t"] == "C"]
        puts = df[df["_t"] == "P"]
        if len(calls) < MIN_STRIKES_PER_SIDE or len(puts) < MIN_STRIKES_PER_SIDE:
            return abstain(self.name,
                           f"only {len(calls)}C/{len(puts)}P strikes")

        # Dollar gamma per 1% move. The spot^2 * 0.01 factor is the standard
        # conversion; the absolute scale is arbitrary here because everything
        # below is a ratio, but keeping the conventional form makes the number
        # comparable to published GEX figures.
        k = float(snap.spot) ** 2 * 0.01
        gex_calls = float((calls["gamma"] * calls["oi"]).sum()) * k
        gex_puts = float((puts["gamma"] * puts["oi"]).sum()) * k
        gex = gex_calls - gex_puts
        gross = abs(gex_calls) + abs(gex_puts)
        if gross <= 0:
            return abstain(self.name, "zero gamma exposure across the chain")
        gex_norm = gex / gross                       # in [-1, 1]

        flip = _gamma_flip(df, float(snap.spot), k)

        if abs(gex_norm) < GEX_NEUTRAL_BAND:
            return LensVerdict(
                lens=self.name, direction=Direction.NEUTRAL, confidence=0.0,
                rationale=(f"dealer book near balanced (GEX {gex_norm:+.3f}) — "
                           f"no hedging regime to trade"),
                features=_features(gex, gex_norm, gex_calls, gex_puts, flip, snap))

        if flip is None:
            return LensVerdict(
                lens=self.name, direction=Direction.NEUTRAL, confidence=0.0,
                rationale=f"GEX {gex_norm:+.3f} but no gamma flip point in the chain",
                features=_features(gex, gex_norm, gex_calls, gex_puts, flip, snap))

        atr = _atr(snap)
        if atr is None or atr <= 0:
            return abstain(self.name, "no ATR — cannot scale distance to the flip")

        dist_atr = (float(snap.spot) - flip) / atr

        if gex_norm > 0:
            # Long gamma: dealers dampen. Price is pulled BACK toward the flip.
            direction_i = -1 if dist_atr > 0 else 1
            why = "long-gamma: dealers dampen, expect pinning toward the flip"
        else:
            # Short gamma: dealers amplify. Moves away from the flip extend.
            direction_i = 1 if dist_atr > 0 else -1
            why = "short-gamma: dealers amplify, expect the move to extend"

        conviction = float(np.clip(abs(dist_atr) / FLIP_FULL_CONVICTION_ATR, 0.0, 1.0))
        conviction *= min(1.0, abs(gex_norm) / 0.5)
        if conviction <= 0.0:
            return LensVerdict(
                lens=self.name, direction=Direction.NEUTRAL, confidence=0.0,
                rationale=f"spot sits on the gamma flip {flip:.0f}",
                features=_features(gex, gex_norm, gex_calls, gex_puts, flip, snap))

        return LensVerdict(
            lens=self.name,
            direction=Direction.LONG if direction_i > 0 else Direction.SHORT,
            confidence=conviction,
            rationale=(f"GEX {gex_norm:+.3f}, flip {flip:.0f}, spot "
                       f"{dist_atr:+.2f} ATR from it — {why}"),
            features=_features(gex, gex_norm, gex_calls, gex_puts, flip, snap,
                               dist_atr))


def _gamma_flip(df: pd.DataFrame, spot: float, k: float) -> Optional[float]:
    """Strike where cumulative dealer gamma crosses zero.

    Walked across strikes rather than solved, because the chain is a coarse
    ladder and a solved root between two strikes implies a precision the data
    does not have.
    """
    rows = []
    for strike, grp in df.groupby("strike"):
        c = grp[grp["_t"] == "C"]
        p = grp[grp["_t"] == "P"]
        g = (float((c["gamma"] * c["oi"]).sum())
             - float((p["gamma"] * p["oi"]).sum())) * k
        rows.append((float(strike), g))
    if len(rows) < 3:
        return None
    rows.sort()
    cum = 0.0
    prev_strike, prev_cum = rows[0][0], 0.0
    for strike, g in rows:
        cum += g
        if prev_cum != 0.0 and (cum > 0) != (prev_cum > 0):
            return (prev_strike + strike) / 2.0
        prev_strike, prev_cum = strike, cum
    return None


def _atr(snap: MarketSnapshot) -> Optional[float]:
    bars = snap.continuous_bars(30) if hasattr(snap, "continuous_bars") else snap.bars
    if bars is None or bars.empty or "close" not in bars.columns:
        return None
    try:
        from core.chart.structure import atr_series
        a = atr_series(bars)
        if a is not None and len(a):
            return float(a[-1])
    except Exception:
        pass
    c = pd.to_numeric(bars["close"], errors="coerce")
    v = float(c.diff().abs().tail(14).mean())
    return v if np.isfinite(v) and v > 0 else None


def _features(gex, gex_norm, gc, gp, flip, snap, dist_atr=None) -> dict:
    return {
        "gex": round(float(gex), 2),
        "gex_norm": round(float(gex_norm), 5),
        "gex_calls": round(float(gc), 2),
        "gex_puts": round(float(gp), 2),
        "gamma_flip": None if flip is None else round(float(flip), 2),
        "dist_to_flip_atr": None if dist_atr is None else round(float(dist_atr), 4),
        "regime": ("long_gamma" if gex_norm > 0 else "short_gamma"),
        "dte": round(snap.dte, 3),
        "spot": round(float(snap.spot), 2),
    }
