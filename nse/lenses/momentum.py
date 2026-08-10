"""Momentum lens — range breakout and continuation.

READ THIS BEFORE TRUSTING ANY NUMBER THIS LENS PRODUCES

`vwap.py` measured SIGNIFICANTLY NEGATIVE under a mean-reversion convention:
TRAIN -2.31 bps at p=0.0014. The mechanical inverse of that finding is a
trend-following convention scoring +2.31 on the same bars. So a momentum lens is
not an innocent new hypothesis — it is adjacent to a sign flip on a result
already seen, and TRAIN and VALIDATE are both partially spent against it.

This lens is built anyway, for one reason: it keys on a DIFFERENT construct.
VWAP measures displacement from a volume-weighted session anchor. This measures
whether price has broken the extreme of its own trailing range and followed
through. Those correlate in a trending tape and diverge in a rotating one, which
is exactly why the pairwise correlation against vwap must be measured before
either is weighted (section 3.12).

The honest disposition, fixed in advance: a positive result here is a CLAIM
NEEDING TEST, not a discovery, and it does not get weight off TRAIN/VALIDATE
alone. If it survives, it is the candidate for the one unspent TEST run.

CONVENTION, DECLARED BEFORE MEASUREMENT

    close breaks above the trailing high  -> LONG
    close breaks below the trailing low   -> SHORT
    inside the range                      -> NEUTRAL

Thresholds are in ATR, never in points. 20 points is 0.08% of NIFTY and 1.07% of
ETH, and an absolute constant is how the ATR gate in section 3.15 stopped
transferring between splits.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from nse.lenses.base import BaseLens, Direction, LensVerdict, abstain
from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

MIN_BARS = 30

#: Trailing window whose extremes define the range, in bars.
LOOKBACK = 20

#: A break must clear the extreme by this much ATR to count. Below it the
#: "breakout" is the tick noise that makes every range look broken.
MIN_BREAK_ATR = 0.25

#: Break size, in ATR, at which conviction saturates.
FULL_BREAK_ATR = 1.5

#: Bars since the break beyond which it is stale — a level broken an hour ago
#: is not news.
MAX_BREAK_AGE = 6


class MomentumLens(BaseLens):
    name = "momentum"
    backtestable = True

    def _evaluate(self, snap: MarketSnapshot) -> LensVerdict:
        bars = snap.bars
        if bars is None or len(bars) < MIN_BARS:
            return abstain(self.name,
                           f"{0 if bars is None else len(bars)} bars — too few for a range")
        need = {"high", "low", "close"}
        if not need <= set(bars.columns):
            return abstain(self.name, "bars lack OHLC")

        from core.chart.structure import atr_series

        atr = atr_series(bars)
        if atr is None or len(atr) == 0:
            return abstain(self.name, "could not compute ATR")
        a = float(atr[-1])
        if not np.isfinite(a) or a <= 0:
            return abstain(self.name, "ATR is zero or non-finite")

        h = pd.to_numeric(bars["high"], errors="coerce").to_numpy(float)
        l = pd.to_numeric(bars["low"], errors="coerce").to_numpy(float)
        c = pd.to_numeric(bars["close"], errors="coerce").to_numpy(float)

        # The range EXCLUDES the current bar — comparing a bar's close to a high
        # that the same bar helped set is circular and would fire constantly.
        window_hi = float(np.max(h[-(LOOKBACK + 1):-1]))
        window_lo = float(np.min(l[-(LOOKBACK + 1):-1]))
        px = float(c[-1])

        up_break = (px - window_hi) / a
        dn_break = (window_lo - px) / a

        if up_break >= MIN_BREAK_ATR:
            direction, size, edge = Direction.LONG, up_break, window_hi
        elif dn_break >= MIN_BREAK_ATR:
            direction, size, edge = Direction.SHORT, dn_break, window_lo
        else:
            return LensVerdict(
                lens=self.name, direction=Direction.NEUTRAL, confidence=0.0,
                rationale=(f"inside the {LOOKBACK}-bar range "
                           f"{window_lo:.0f}-{window_hi:.0f} — no break"),
                features={"window_high": round(window_hi, 2),
                          "window_low": round(window_lo, 2),
                          "up_break_atr": round(up_break, 4),
                          "down_break_atr": round(dn_break, 4),
                          "atr": round(a, 3), "dte": round(snap.dte, 3),
                          "spot": round(snap.spot, 2)})

        age = _bars_since_break(c, window_hi, window_lo, direction)
        if age is not None and age > MAX_BREAK_AGE:
            return LensVerdict(
                lens=self.name, direction=Direction.NEUTRAL, confidence=0.0,
                rationale=f"break is {age} bars old — stale, not news",
                features={"window_high": round(window_hi, 2),
                          "window_low": round(window_lo, 2),
                          "break_age": age, "atr": round(a, 3),
                          "dte": round(snap.dte, 3), "spot": round(snap.spot, 2)})

        conviction = float(np.clip(
            (size - MIN_BREAK_ATR) / (FULL_BREAK_ATR - MIN_BREAK_ATR), 0.0, 1.0))

        return LensVerdict(
            lens=self.name,
            direction=direction,
            confidence=conviction,
            rationale=(f"{direction.label}: close {px:.0f} broke the "
                       f"{LOOKBACK}-bar {'high' if direction > 0 else 'low'} "
                       f"{edge:.0f} by {size:.2f} ATR"),
            features={
                "window_high": round(window_hi, 2),
                "window_low": round(window_lo, 2),
                "break_atr": round(float(size), 4),
                "break_age": age,
                "atr": round(a, 3),
                "atr_pct": round(a / snap.spot * 100, 4) if snap.spot else 0.0,
                "dte": round(snap.dte, 3),
                "spot": round(snap.spot, 2),
            },
        )

    def _deliberate(self, snap: MarketSnapshot, own: LensVerdict,
                    peers: dict, journal) -> LensVerdict:
        """A breakout INTO an opposing order block is the textbook false one.

        ict_smc knows where the resting orders are. Breaking a trailing range
        straight into a zone on the other side is the setup that traps
        breakout buyers, so a structural objection cuts hard here rather than
        gently.

        A breakout on no participation is also suspect: a range cleared by
        nobody is a range that has not really been cleared.
        """
        cut, notes = 1.0, []

        ict = peers.get("ict_smc")
        if (ict is not None and ict.speaks and ict.direction != Direction.NEUTRAL
                and ict.direction != own.direction and ict.confidence >= 0.30):
            cut *= 0.5
            notes.append("ict_smc has an opposing zone right where I am "
                         "breaking — classic false break")

        lq = peers.get("liquidity")
        if lq is not None and lq.speaks and lq.confidence < 0.4:
            cut *= 0.6
            notes.append(f"thin tape ({lq.confidence:.2f}) — a range cleared by "
                         f"nobody is not cleared")

        if cut >= 1.0:
            return own
        return own.revise(own.direction, own.confidence * cut,
                          "; ".join(notes))


def _bars_since_break(closes: np.ndarray, hi: float, lo: float,
                      direction: Direction) -> Optional[int]:
    """How many bars ago price first cleared the level it is now beyond."""
    level = hi if direction is Direction.LONG else lo
    inside = ((closes <= level) if direction is Direction.LONG
              else (closes >= level))
    idx = np.where(inside)[0]
    if idx.size == 0:
        return None
    return int(len(closes) - 1 - idx[-1])
