"""Swing levels, in ATR units so the same code reads any instrument.

`core/sr_levels.py` already detects swings, clusters them by touch count and
finds Rally-Base-Drop / Drop-Base-Rally zones. That work is reused rather than
rewritten. What this module adds is the thing that stops it being NIFTY-only:
every tolerance is expressed as a multiple of ATR instead of as a number of
points.

The distinction matters more than it sounds. sr_levels clusters swings within
20 POINTS. On NIFTY at 24,500 that is 0.08% and reasonable. On ETH at 1,870 it
is 1.07%, which would merge every level on the chart into one blob. On BTC at
98,000 it is 0.02%, which would cluster nothing and leave a hundred separate
levels. One constant, three completely different behaviours.

ATR also solves the same problem across TIME, not just across instruments: a
fixed 25-point stop is twice as tight in 2026 as it was in 2021 because NIFTY
itself doubled (RESEARCH_LEARNINGS open item 3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Swings cluster into one level when within this multiple of ATR.
CLUSTER_ATR_MULT = 0.5

# Bars either side of a candidate for it to count as a swing extreme.
SWING_LOOKBACK = 5

# A level needs this many touches before it is worth trading against. One touch
# is a coincidence; the level only means anything if price has respected it.
MIN_TOUCHES = 2

# Price is "at" a level within this multiple of ATR.
PROXIMITY_ATR_MULT = 0.35


@dataclass
class Level:
    price: float
    kind: str                 # "support" | "resistance"
    touches: int
    last_touch_idx: int
    atr_distance: float = 0.0   # distance from current price, in ATR

    def as_dict(self) -> dict:
        return {"price": round(self.price, 4), "kind": self.kind,
                "touches": self.touches, "atr_distance": round(self.atr_distance, 3)}


@dataclass
class ChartStructure:
    atr: float
    price: float
    levels: list[Level] = field(default_factory=list)
    swing_highs: list[tuple] = field(default_factory=list)   # (idx, price)
    swing_lows: list[tuple] = field(default_factory=list)
    trend: str = "ranging"                                    # up | down | ranging

    @property
    def supports(self) -> list[Level]:
        return sorted([l for l in self.levels if l.kind == "support"],
                      key=lambda l: -l.price)

    @property
    def resistances(self) -> list[Level]:
        return sorted([l for l in self.levels if l.kind == "resistance"],
                      key=lambda l: l.price)

    def nearest_above(self, price: float) -> Optional[Level]:
        above = [l for l in self.levels if l.price > price]
        return min(above, key=lambda l: l.price - price) if above else None

    def nearest_below(self, price: float) -> Optional[Level]:
        below = [l for l in self.levels if l.price < price]
        return min(below, key=lambda l: price - l.price) if below else None

    def as_dict(self) -> dict:
        return {"atr": round(self.atr, 4), "price": round(self.price, 4),
                "trend": self.trend, "n_levels": len(self.levels),
                "levels": [l.as_dict() for l in self.levels[:8]]}


def atr_series(df: pd.DataFrame, span: int = 14) -> np.ndarray:
    """Per-bar ATR — the value as it stood AT each bar.

    Anything scanning a whole series must index this rather than call atr(),
    which returns only the final value. Using one series-wide ATR to judge a
    bar 300 positions back measures that bar with volatility from its own
    future: the stop distance, the pierce depth and therefore the R:R all come
    out wrong, and wrong in a way that shifts when more data is appended.
    """
    h = pd.to_numeric(df["high"], errors="coerce").to_numpy(float)
    l = pd.to_numeric(df["low"], errors="coerce").to_numpy(float)
    c = pd.to_numeric(df["close"], errors="coerce").to_numpy(float)
    if len(h) < 2:
        return np.zeros(len(h))
    prev = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - prev), np.abs(l - prev)])
    out = pd.Series(tr).ewm(span=span, adjust=False).mean().to_numpy()
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)


def atr(df: pd.DataFrame, span: int = 14) -> float:
    """ATR at the LAST bar. Safe only when df already ends at the decision bar."""
    s = atr_series(df, span)
    return float(s[-1]) if len(s) else 0.0


def find_swings(df: pd.DataFrame, lookback: int = SWING_LOOKBACK
                ) -> tuple[list[tuple], list[tuple]]:
    """Swing highs and lows as (index, price).

    A swing high is the highest HIGH in a window centred on it — the actual
    wick, not the close. That distinction is the whole basis of sweep detection:
    resting stop orders sit above the wick, and only true OHLC shows where the
    wick reached. On a close-only series these levels are systematically too
    shallow (RESEARCH_LEARNINGS section 4).
    """
    h = pd.to_numeric(df["high"], errors="coerce").to_numpy(float)
    l = pd.to_numeric(df["low"], errors="coerce").to_numpy(float)
    n = len(h)
    highs, lows = [], []
    for i in range(lookback, n - lookback):
        w_h = h[i - lookback:i + lookback + 1]
        w_l = l[i - lookback:i + lookback + 1]
        if h[i] == w_h.max():
            highs.append((i, float(h[i])))
        if l[i] == w_l.min():
            lows.append((i, float(l[i])))
    return highs, lows


def _cluster(swings: list[tuple], width: float, kind: str) -> list[Level]:
    """Group swings within `width` of each other into one level.

    Touch count is the cluster size, and it is the level's strength: a price
    the market has turned at four times is a different proposition from one it
    grazed once.
    """
    if not swings or width <= 0:
        return []
    out: list[Level] = []
    for _, group in _groups(sorted(swings, key=lambda s: s[1]), width):
        prices = [p for _, p in group]
        touches = len(group)
        if touches < MIN_TOUCHES:
            continue
        out.append(Level(price=float(np.mean(prices)), kind=kind,
                         touches=touches,
                         last_touch_idx=max(i for i, _ in group)))
    return out


def _groups(sorted_swings, width):
    cur = [sorted_swings[0]]
    for s in sorted_swings[1:]:
        if s[1] - cur[-1][1] <= width:
            cur.append(s)
        else:
            yield None, cur
            cur = [s]
    yield None, cur


def _trend(df: pd.DataFrame, highs: list[tuple], lows: list[tuple]) -> str:
    """Higher highs AND higher lows is an uptrend. Anything else is not.

    Deliberately stricter than a moving-average cross: this gates which side of
    a level is worth trading, and a loose trend read turns a countertrend trade
    into a with-trend one on paper.
    """
    if len(highs) < 2 or len(lows) < 2:
        return "ranging"
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    lh = highs[-1][1] < highs[-2][1]
    ll = lows[-1][1] < lows[-2][1]
    if hh and hl:
        return "up"
    if lh and ll:
        return "down"
    return "ranging"


def read_structure(df: pd.DataFrame, lookback: int = SWING_LOOKBACK,
                   cluster_atr_mult: float = CLUSTER_ATR_MULT
                   ) -> Optional[ChartStructure]:
    """Read levels and trend off an OHLC frame. None when there is too little.

    `df` needs open/high/low/close columns, oldest first. It must contain ONLY
    bars that had already closed at the moment being analysed — passing the
    whole history and reading the last row is fine, passing future bars is
    lookahead.
    """
    need = {"open", "high", "low", "close"}
    if df is None or len(df) < lookback * 2 + 15 or not need <= set(df.columns):
        return None

    a = atr(df)
    if a <= 0:
        return None

    price = float(pd.to_numeric(df["close"], errors="coerce").iloc[-1])
    highs, lows = find_swings(df, lookback)
    width = a * cluster_atr_mult

    levels = (_cluster(highs, width, "resistance")
              + _cluster(lows, width, "support"))
    # Reclassify against the CURRENT price: a level formed as resistance
    # becomes support once price closes above and holds.
    for lv in levels:
        lv.kind = "resistance" if lv.price > price else "support"
        lv.atr_distance = abs(lv.price - price) / a

    return ChartStructure(atr=a, price=price, levels=levels,
                          swing_highs=highs, swing_lows=lows,
                          trend=_trend(df, highs, lows))
