"""VWAP lens — how far price has travelled from where the volume actually paid.

Session-anchored VWAP is the average price every rupee of the day's volume
transacted at. Price far above it means the marginal buyer is paying well over
what the day's participants collectively paid; far below, the reverse. The read
here is MEAN REVERSION toward that anchor, expressed as a z-score of the
distance in units of the session's own dispersion.

    vwap = sum(price x volume) / sum(volume)     over the session so far
    z    = (spot - vwap) / sd(price - vwap)
    read = -z                                    stretched high -> SHORT

WHY MEAN REVERSION AND NOT TREND

VWAP has two opposite conventional uses: a trend filter (above VWAP is bullish)
and a reversion anchor (2 sigma above VWAP is stretched). Both are defensible,
which means picking one after seeing the result would be fitting a coin flip.
The prior chosen here, and stated before measurement: VWAP is an execution
benchmark, so it behaves as fair value, and distance from fair value is a
stretch rather than a confirmation. If the measurement comes back negative, the
honest reading is "trend was the right convention on this data" — ONE bit of
information — not a licence to flip the sign and re-run.

CORRELATION WITH THE VOLUME/OI LENS — READ THIS BEFORE AGGREGATING

Both this lens and the volume-profile component of volume_oi ask "where did
volume happen". They are not the same question — VWAP is a path-dependent
running mean, the profile is a path-independent distribution, and price can sit
above VWAP while inside the value area — but they are certainly related. Two
correlated lenses given equal weight in a vote do not provide two independent
opinions; they provide one opinion counted twice, with false confidence
attached. Measure the correlation between their signed scores before letting
both vote, and weight accordingly.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from nse.lenses.base import BaseLens, Direction, LensVerdict, abstain

#: Measured correlation with volume_oi's signed confidence, on TRAIN
#: (RESEARCH_LEARNINGS section 3.12). Strongly negative: this lens and
#: volume_oi's volume-profile component are largely the same read with opposite
#: sign conventions, agreeing on only 18.4% of decisions.
CORRELATION_WITH_VOLUME_OI: float = -0.769

#: Above this magnitude, treat a peer as measuring the same thing we are and
#: stand down rather than let the council hear one opinion twice.
DUPLICATE_CORRELATION: float = 0.6
from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

# Bars needed before a session VWAP means anything. Early in the session the
# anchor is a handful of prints and the dispersion estimate is noise.
MIN_BARS = 30

# |z| that saturates conviction. CALIBRATED ON TRAIN — see the measured block
# in the commit; a guessed scale here is what made the Greeks lens read as a
# permanent tilt (RESEARCH_LEARNINGS 3.5).
Z_FULL_CONVICTION = 2.0


class VWAPLens(BaseLens):
    name = "vwap"
    backtestable = True

    def _evaluate(self, snap: MarketSnapshot) -> LensVerdict:
        bars = snap.bars
        if bars is None or len(bars) < MIN_BARS:
            return abstain(self.name,
                           f"{0 if bars is None else len(bars)} bars — session "
                           f"anchor not yet meaningful")
        if "close" not in bars.columns or "volume" not in bars.columns:
            return abstain(self.name, "bars lack close/volume")

        px = pd.to_numeric(bars["close"], errors="coerce").to_numpy(dtype=float)
        vol = pd.to_numeric(bars["volume"], errors="coerce").to_numpy(dtype=float)
        ok = np.isfinite(px) & np.isfinite(vol) & (vol > 0) & (px > 0)
        px, vol = px[ok], vol[ok]
        if px.size < MIN_BARS or vol.sum() <= 0:
            return abstain(self.name, "no usable volume in the session so far")

        vwap = float(np.sum(px * vol) / np.sum(vol))

        # Dispersion of price about the ANCHOR, not about its own mean: the
        # question is how unusual this distance from VWAP is, and centring on
        # the sample mean would quietly re-anchor it.
        resid = px - vwap
        sd = float(np.sqrt(np.mean(resid ** 2)))
        if sd <= 0:
            return abstain(self.name, "zero dispersion about VWAP")

        z = (snap.spot - vwap) / sd
        score = -z / Z_FULL_CONVICTION          # stretched high -> SHORT
        score = float(max(-1.0, min(1.0, score)))

        if score > 0:
            direction, why = Direction.LONG, "stretched below VWAP"
        elif score < 0:
            direction, why = Direction.SHORT, "stretched above VWAP"
        else:
            direction, why = Direction.NEUTRAL, "sitting on VWAP"

        return LensVerdict(
            lens=self.name,
            direction=direction,
            confidence=abs(score),
            rationale=(f"spot {snap.spot:.0f} vs VWAP {vwap:.0f} "
                       f"({z:+.2f} sigma) — {why}"),
            features={
                "vwap": round(vwap, 2),
                "z": round(float(z), 4),
                "score": round(score, 5),
                "sd": round(sd, 3),
                "distance_pts": round(float(snap.spot - vwap), 2),
                "n_bars": int(px.size),
                "dte": round(snap.dte, 3),
                "spot": round(snap.spot, 2),
            },
        )

    def _deliberate(self, snap: MarketSnapshot, own: LensVerdict,
                    peers: dict, journal) -> LensVerdict:
        """Stand down when volume_oi has already read this.

        This lens measured -0.769 correlated with volume_oi and agrees with it
        on 18.4% of decisions: it is largely volume_oi's volume-profile
        component with the opposite sign convention, not a second opinion. When
        both speak, the council would be hearing one read twice — and because
        the correlation is NEGATIVE, hearing it twice mostly means hearing it
        argue with itself.

        Deferring is the honest move, and it belongs here rather than in the
        council: this lens is the one that knows what it measures. Note the
        deferral does NOT cost it its track record — round 0 still stands and
        attribution still scores it, so if this lens later proves the better
        read of the two, the brain will see that and can promote it.
        """
        vo = peers.get("volume_oi")
        if vo is not None and vo.speaks and vo.direction != Direction.NEUTRAL:
            if abs(CORRELATION_WITH_VOLUME_OI) >= DUPLICATE_CORRELATION:
                return own.defer(
                    f"volume_oi already read this ({vo.direction.label} "
                    f"{vo.confidence:.2f}); at {CORRELATION_WITH_VOLUME_OI:+.2f} "
                    f"correlation my read is not independent of it")
        return own
