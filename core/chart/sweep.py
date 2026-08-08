"""Liquidity sweeps — price taking out the stops, then rejecting.

THE PATTERN

Stop orders pile up just beyond obvious swing extremes, because that is where
everyone puts them. A sweep is price reaching through that shelf, filling those
stops, and then closing back inside — the extension was liquidity collection,
not a real breakout.

    bullish sweep     low  pierces a prior swing LOW,  close comes back ABOVE it
    bearish sweep     high pierces a prior swing HIGH, close comes back BELOW it

WHY THE STOP GOES BEYOND THE WICK, NOT AT THE LEVEL

This is the specific thing that makes the pattern worth trading. If your stop
sits at the obvious level, you are part of the liquidity being swept — you get
taken out at the extreme and the move then goes your way without you. Placing
it beyond the sweep's own extreme means the market has to do something
genuinely new to be wrong, rather than merely repeat what it just did.

TRUE OHLC IS NON-NEGOTIABLE HERE

Every condition above is about a WICK — where price reached intrabar, not where
it closed. On a close-only series the pierce never registers and this detector
finds nothing. The archived NIFTY option dataset carries `spot` as a close-only
series (RESEARCH_LEARNINGS section 4), so it cannot be used for this. Angel
getCandleData and Delta /v2/history/candles both return real OHLC, and those
are the inputs this expects.

CALIBRATION

Pierce depth and rejection strength are in ATR, never in points, so the same
thresholds read NIFTY, BANKNIFTY, BTC and ETH identically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from core.chart.structure import ChartStructure, atr

logger = logging.getLogger(__name__)

# The wick must pierce the level by at least this much ATR. Below it the
# "sweep" is indistinguishable from noise brushing the level.
MIN_PIERCE_ATR = 0.05

# ...and by no more than this. A pierce of two full ATR is not a stop raid, it
# is a breakout, and fading a genuine breakout is how accounts die.
MAX_PIERCE_ATR = 1.20

# Where the close must sit WITHIN THE BAR'S OWN RANGE for the bar to read as a
# rejection: 0.66 means the close is in the top third for a bullish sweep, the
# bottom third for a bearish one. The classic rejection candle.
#
# This was originally defined as "fraction of the pierce given back", which was
# unreachable dead code. A valid sweep already requires the close back above the
# swept level, so that quantity was always > 1.0 and a 0.5 threshold could never
# bind — the same shape of bug as the synthetic forward's 0.60% gate, which sat
# above the 0.404% maximum the quantity ever reached and therefore fired zero
# times in 1,869 observations (RESEARCH_LEARNINGS 1.3).
#
# Measured against the bar's range instead, the value is bounded in [0, 1] and
# the threshold does real work: it separates a decisive rejection wick from a
# bar that merely closed a hair back over the line.
MIN_REJECTION_FRAC = 0.66

# How recently the swept level must have formed, in bars. Old levels have
# already had their liquidity taken.
MAX_LEVEL_AGE_BARS = 120


@dataclass
class Sweep:
    idx: int                  # bar index of the sweep
    direction: int            # +1 bullish (swept lows), -1 bearish (swept highs)
    level: float              # the swing extreme that was swept
    extreme: float            # how far the wick actually reached
    close: float
    pierce_atr: float         # depth beyond the level, in ATR
    rejection_frac: float     # where the close sits in the bar's range, [0, 1]
    level_age_bars: int
    atr: float

    @property
    def stop(self) -> float:
        """Beyond the wick, not at the level.

        Buffered by a fraction of ATR so a re-test of the same extreme does not
        take the position out — being swept out of a sweep trade is the exact
        failure this setup exists to avoid.
        """
        buf = self.atr * 0.15
        return self.extreme - buf if self.direction > 0 else self.extreme + buf

    @property
    def stop_distance(self) -> float:
        return abs(self.close - self.stop)

    def as_dict(self) -> dict:
        return {"idx": self.idx, "direction": self.direction,
                "level": round(self.level, 4), "extreme": round(self.extreme, 4),
                "close": round(self.close, 4),
                "pierce_atr": round(self.pierce_atr, 3),
                "rejection_frac": round(self.rejection_frac, 3),
                "level_age_bars": self.level_age_bars,
                "stop": round(self.stop, 4),
                "stop_distance": round(self.stop_distance, 4)}


def find_sweeps(df: pd.DataFrame, structure: Optional[ChartStructure] = None,
                *, min_pierce_atr: float = MIN_PIERCE_ATR,
                max_pierce_atr: float = MAX_PIERCE_ATR,
                min_rejection: float = MIN_REJECTION_FRAC,
                max_age: int = MAX_LEVEL_AGE_BARS) -> list[Sweep]:
    """Every sweep in the frame, oldest first.

    A sweep is judged against swing extremes that existed BEFORE it. The swing
    list is walked with an index cutoff rather than filtered afterwards,
    because a swing high is only confirmed `lookback` bars after it prints —
    treating it as known at the moment it formed would be lookahead of exactly
    the kind that turned -Rs 6,110 into +Rs 377,749 (RESEARCH_LEARNINGS 1.2).
    """
    from core.chart.structure import SWING_LOOKBACK, find_swings

    need = {"open", "high", "low", "close"}
    if df is None or len(df) < 40 or not need <= set(df.columns):
        return []

    a = structure.atr if structure else atr(df)
    if a <= 0:
        return []

    h = pd.to_numeric(df["high"], errors="coerce").to_numpy(float)
    l = pd.to_numeric(df["low"], errors="coerce").to_numpy(float)
    c = pd.to_numeric(df["close"], errors="coerce").to_numpy(float)
    highs, lows = find_swings(df, SWING_LOOKBACK)

    out: list[Sweep] = []
    for i in range(SWING_LOOKBACK * 2, len(df)):
        # A swing at index j is only CONFIRMED at j + SWING_LOOKBACK, so bar i
        # may only consider swings whose confirmation already happened.
        confirmed_by = i - SWING_LOOKBACK

        for j, lvl in reversed(lows):
            if j > confirmed_by or i - j > max_age:
                continue
            if l[i] >= lvl:
                continue                       # never reached the level
            pierce = (lvl - l[i]) / a
            if not (min_pierce_atr <= pierce <= max_pierce_atr):
                continue
            if c[i] <= lvl:
                continue                       # closed below: a break, not a sweep
            rng = h[i] - l[i]
            rej = (c[i] - l[i]) / rng if rng > 0 else 0.0
            if rej < min_rejection:
                continue                       # close not in the upper third
            out.append(Sweep(idx=i, direction=1, level=lvl, extreme=float(l[i]),
                             close=float(c[i]), pierce_atr=pierce,
                             rejection_frac=float(rej), level_age_bars=i - j, atr=a))
            break                              # nearest recent level only

        for j, lvl in reversed(highs):
            if j > confirmed_by or i - j > max_age:
                continue
            if h[i] <= lvl:
                continue
            pierce = (h[i] - lvl) / a
            if not (min_pierce_atr <= pierce <= max_pierce_atr):
                continue
            if c[i] >= lvl:
                continue
            rng = h[i] - l[i]
            rej = (h[i] - c[i]) / rng if rng > 0 else 0.0
            if rej < min_rejection:
                continue                       # close not in the lower third
            out.append(Sweep(idx=i, direction=-1, level=lvl, extreme=float(h[i]),
                             close=float(c[i]), pierce_atr=pierce,
                             rejection_frac=float(rej), level_age_bars=i - j, atr=a))
            break

    return out


def latest_sweep(df: pd.DataFrame, structure: Optional[ChartStructure] = None,
                 within_bars: int = 2, **kw) -> Optional[Sweep]:
    """The most recent sweep, if it happened within the last `within_bars`.

    The freshness bound is the point: a sweep from twenty bars ago has already
    been traded and the reaction is over. Acting on it now is acting on old news.
    """
    sweeps = find_sweeps(df, structure, **kw)
    if not sweeps:
        return None
    last = sweeps[-1]
    return last if (len(df) - 1 - last.idx) <= within_bars else None
