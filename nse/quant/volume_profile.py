"""Volume profile — where trading actually happened, by price rather than by time.

A candle chart asks "what was the price at time T". A volume profile asks the
more useful question for locating support and resistance: "how much business got
done at price P". The answer is three levels:

    POC   point of control — the single price level with the most volume.
          The market's own idea of fair value for the session.
    VAH   value area high
    VAL   value area low
          The band around POC holding VALUE_AREA_PCT of all volume. Price
          inside the value area is accepted; price outside it is being probed.

The value-area expansion follows the standard Market Profile rule: start at the
POC and repeatedly step to whichever adjacent level carries more volume, until
the enclosed volume reaches the target. That asymmetric walk is what makes the
band hug the real distribution instead of being a symmetric window around POC.

Nothing here knows about options. It takes prices and volumes and returns
levels, so it works on index bars, on a single contract, or on anything else
with a price and a size.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

# Fraction of total volume enclosed by the value area. 70% is the Market
# Profile convention — roughly one standard deviation if the distribution were
# normal, which it is not, which is rather the point of measuring it.
VALUE_AREA_PCT = 0.70


@dataclass(frozen=True)
class Profile:
    poc: float
    vah: float
    val: float
    total_volume: float
    n_levels: int
    bin_width: float

    @property
    def value_area_width(self) -> float:
        return self.vah - self.val

    def position_of(self, price: float) -> float:
        """Where a price sits in the value area, normalised.

            0.0   at VAL
            1.0   at VAH
            <0    below the value area
            >1    above it

        Returns the signed position rather than a bare in/out flag because how
        FAR outside the band price is matters as much as the fact of it.
        """
        w = self.value_area_width
        if w <= 0:
            return 0.0
        return (price - self.val) / w

    def is_inside(self, price: float) -> bool:
        return self.val <= price <= self.vah

    def as_dict(self) -> dict:
        return {"poc": self.poc, "vah": self.vah, "val": self.val,
                "total_volume": self.total_volume, "n_levels": self.n_levels}


def build(prices: Sequence[float], volumes: Sequence[float],
          bin_width: Optional[float] = None,
          value_area_pct: float = VALUE_AREA_PCT) -> Optional[Profile]:
    """Build a profile from paired prices and volumes.

    `bin_width` defaults to the instrument's own granularity via a
    Freedman-Diaconis-style choice bounded to something sane. Passing the
    strike step explicitly is better when you have it: the levels then line up
    with strikes traders actually watch.

    Returns None rather than a degenerate profile when there is no volume to
    profile — an all-zero-volume session is a data gap, not a flat market, and
    a POC invented from it would be pure noise dressed as a level.
    """
    p = np.asarray(prices, dtype=float)
    v = np.asarray(volumes, dtype=float)
    if p.size == 0 or p.size != v.size:
        return None

    ok = np.isfinite(p) & np.isfinite(v) & (v > 0)
    p, v = p[ok], v[ok]
    if p.size == 0 or v.sum() <= 0:
        return None

    lo, hi = float(p.min()), float(p.max())
    if hi <= lo:
        # Everything traded at one price. That IS the POC, and the value area
        # collapses onto it — degenerate but true, so report it rather than
        # failing.
        return Profile(poc=lo, vah=lo, val=lo, total_volume=float(v.sum()),
                       n_levels=1, bin_width=bin_width or 0.0)

    if bin_width is None or bin_width <= 0:
        bin_width = max((hi - lo) / 50.0, 1e-9)

    n_bins = max(1, int(np.ceil((hi - lo) / bin_width)))
    edges = lo + bin_width * np.arange(n_bins + 1)
    idx = np.clip(((p - lo) / bin_width).astype(int), 0, n_bins - 1)

    vol = np.zeros(n_bins, dtype=float)
    np.add.at(vol, idx, v)
    centres = edges[:-1] + bin_width / 2.0

    poc_i = int(np.argmax(vol))
    total = float(vol.sum())
    target = total * value_area_pct

    # Expand outward from the POC, always taking the heavier neighbour.
    lo_i = hi_i = poc_i
    enclosed = vol[poc_i]
    while enclosed < target and (lo_i > 0 or hi_i < n_bins - 1):
        below = vol[lo_i - 1] if lo_i > 0 else -1.0
        above = vol[hi_i + 1] if hi_i < n_bins - 1 else -1.0
        if above >= below:
            hi_i += 1
            enclosed += vol[hi_i]
        else:
            lo_i -= 1
            enclosed += vol[lo_i]

    return Profile(
        poc=float(centres[poc_i]),
        vah=float(edges[hi_i + 1]),
        val=float(edges[lo_i]),
        total_volume=total,
        n_levels=n_bins,
        bin_width=float(bin_width),
    )


def from_bars(bars, price_col: str = "close", volume_col: str = "volume",
              bin_width: Optional[float] = None) -> Optional[Profile]:
    """Profile a bar DataFrame.

    Uses the bar's CLOSE, not its high/low range. On this dataset that is not a
    simplification but a requirement: `spot` is a close-only series, so
    resampled highs and lows are extremes of closes rather than true intrabar
    extremes. Spreading a bar's volume across a fabricated range would invent
    structure the data cannot support. See RESEARCH_LEARNINGS section 4.
    """
    if bars is None or len(bars) == 0:
        return None
    if price_col not in bars.columns or volume_col not in bars.columns:
        return None
    return build(bars[price_col].to_numpy(), bars[volume_col].to_numpy(),
                 bin_width=bin_width)
