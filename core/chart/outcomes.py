"""Label what actually happened after every sweep. The dataset risk sizing needs.

Risk should scale with how likely a setup is to work. That is right, and it is
also why this module has to exist before any such sizing does: you cannot size
on a probability you have not measured, and a probability invented to fill the
gap is worse than a flat risk fraction because it carries false precision.

This repo has three recorded instances of exactly that failure — a 0.60% entry
gate above the 0.404% maximum the quantity ever reached, a 1:7 target hit once
in 515 trades, and a skew constant of 0.0 for a quantity negative 98.4% of the
time. Every one looked reasonable when written.

So: replay every sweep, walk forward bar by bar, record whether it reached the
target before the stop, and store the features that were visible AT THE MOMENT
OF ENTRY next to that outcome. What comes out is a labelled table. Only then is
there something to fit a probability to.

RESOLUTION RULES

  target first    high (long) / low (short) reaches target before the stop
  stop first      the reverse
  ambiguous       BOTH touched inside the same bar. Counted as a STOP, not a
                  coin flip, because intrabar order is unknowable from OHLC and
                  the pessimistic reading is the only honest one. The fraction
                  of these is reported — if it is large, the bar interval is
                  too coarse for the stop distance being used.
  timeout         neither within max_bars. Recorded separately and marked to
                  the last close, never silently dropped.

FEATURES ARE ENTRY-TIME ONLY. Anything computed from bars after the entry bar
would leak the answer into the predictor.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from core.chart.structure import ChartStructure, read_structure
from core.chart.sweep import Sweep, find_sweeps

logger = logging.getLogger(__name__)

# Give a setup this many bars to resolve before calling it a timeout. At a 5m
# grid, 48 bars is four hours — long enough for an intraday move to play out,
# short enough that it is still the same market regime.
MAX_BARS_TO_RESOLVE = 48

# Rolling window of bars used to read levels at each entry. Long enough to hold
# real structure, short enough that levels are still ones anyone is watching.
STRUCTURE_WINDOW_BARS = 300


@dataclass
class Outcome:
    """One sweep, its entry-time features, and what happened next."""

    # identity
    venue: str
    symbol: str
    idx: int
    ts: object
    direction: int

    # the trade as it would have been taken
    entry: float
    stop: float
    target: float
    rr: float
    risk: float

    # entry-time features — candidates for the probability model
    pierce_atr: float
    rejection_frac: float
    level_age_bars: int
    stop_atr: float
    target_touches: int
    trend: str
    atr_pct: float             # ATR as % of price: the volatility regime
    bars_into_session: int

    # optional NSE option-chain features, absent on crypto
    oi_wall_distance_atr: Optional[float] = None
    pcr_oi: Optional[float] = None
    max_pain_distance_atr: Optional[float] = None

    # outcome
    resolved: str = ""          # "target" | "stop" | "timeout"
    bars_to_resolve: int = 0
    r_multiple: float = 0.0     # realised, in units of initial risk
    ambiguous: bool = False     # target and stop both touched in one bar
    mfe_r: float = 0.0          # max favourable excursion, in R
    mae_r: float = 0.0          # max adverse excursion, in R

    @property
    def won(self) -> bool:
        return self.resolved == "target"

    def as_row(self) -> dict:
        return asdict(self)


def label_sweeps(df: pd.DataFrame, *, venue: str, symbol: str,
                 structure_df: Optional[pd.DataFrame] = None,
                 max_bars: int = MAX_BARS_TO_RESOLVE,
                 structure_window: int = STRUCTURE_WINDOW_BARS,
                 min_rr: float = 1.2,
                 max_stop_atr: float = 1.6,
                 chain_features=None) -> list[Outcome]:
    """Every sweep in `df`, with its entry-time features and its outcome.

    `structure_df` supplies the higher-timeframe levels used for targets. It
    must be aligned to `df` and contain no bars beyond it. When omitted, `df`
    is used for both, which is a valid single-timeframe variant.

    `chain_features(idx) -> dict` optionally injects option-chain features for
    NSE. It is passed the ENTRY bar index and must not look past it.
    """
    need = {"open", "high", "low", "close"}
    if df is None or len(df) < 60 or not need <= set(df.columns):
        return []

    sweeps = find_sweeps(df)
    if not sweeps:
        return []

    h = pd.to_numeric(df["high"], errors="coerce").to_numpy(float)
    l = pd.to_numeric(df["low"], errors="coerce").to_numpy(float)
    c = pd.to_numeric(df["close"], errors="coerce").to_numpy(float)
    n = len(df)

    out: list[Outcome] = []
    for sw in sweeps:
        i = sw.idx
        if i >= n - 2:
            continue

        # Structure from a ROLLING WINDOW ending at the entry bar. Two reasons,
        # both load-bearing: a level from ten thousand bars ago is not a level
        # anyone is watching, and reading the whole prefix for every sweep is
        # O(n^2), which on 115k bars is the difference between minutes and
        # hours. Never extends past i — that would be lookahead.
        src = structure_df if structure_df is not None else df
        lo = max(0, i + 1 - structure_window)
        st = read_structure(src.iloc[lo:i + 1])
        if st is None:
            continue

        entry, stop = sw.close, sw.stop
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        stop_atr = risk / st.atr if st.atr > 0 else 999.0
        if stop_atr > max_stop_atr:
            continue

        lvl = (st.nearest_above(entry) if sw.direction > 0
               else st.nearest_below(entry))
        if lvl is None:
            continue
        target = lvl.price
        rr = abs(target - entry) / risk
        if rr < min_rr:
            continue

        res, bars_used, r_mult, ambiguous, mfe, mae = _walk_forward(
            h, l, c, i, sw.direction, entry, stop, target, risk, max_bars, n)

        o = Outcome(
            venue=venue, symbol=symbol, idx=i,
            ts=(df["datetime"].iloc[i] if "datetime" in df.columns else i),
            direction=sw.direction, entry=entry, stop=stop, target=target,
            rr=rr, risk=risk,
            pierce_atr=sw.pierce_atr, rejection_frac=sw.rejection_frac,
            level_age_bars=sw.level_age_bars, stop_atr=stop_atr,
            target_touches=lvl.touches, trend=st.trend,
            atr_pct=(st.atr / entry * 100) if entry else 0.0,
            bars_into_session=i,
            resolved=res, bars_to_resolve=bars_used, r_multiple=r_mult,
            ambiguous=ambiguous, mfe_r=mfe, mae_r=mae,
        )
        if chain_features is not None:
            try:
                for k, v in (chain_features(i) or {}).items():
                    if hasattr(o, k):
                        setattr(o, k, v)
            except Exception as e:
                logger.debug("chain_features failed at %d: %s", i, e)
        out.append(o)
    return out


def _walk_forward(h, l, c, i, direction, entry, stop, target, risk,
                  max_bars, n):
    """Bar-by-bar from the entry bar's close to resolution."""
    mfe = mae = 0.0
    for j in range(i + 1, min(i + 1 + max_bars, n)):
        if direction > 0:
            fav, adv = (h[j] - entry) / risk, (entry - l[j]) / risk
            hit_t, hit_s = h[j] >= target, l[j] <= stop
        else:
            fav, adv = (entry - l[j]) / risk, (h[j] - entry) / risk
            hit_t, hit_s = l[j] <= target, h[j] >= stop
        mfe, mae = max(mfe, fav), max(mae, adv)

        if hit_t and hit_s:
            # Both inside one bar. OHLC cannot say which came first, so take
            # the loss. Optimism here is how a backtest invents an edge.
            return "stop", j - i, -1.0, True, mfe, mae
        if hit_t:
            return "target", j - i, abs(target - entry) / risk, False, mfe, mae
        if hit_s:
            return "stop", j - i, -1.0, False, mfe, mae

    last = c[min(i + max_bars, n - 1)]
    r = ((last - entry) if direction > 0 else (entry - last)) / risk
    return "timeout", min(max_bars, n - 1 - i), r, False, mfe, mae


def summarise(outcomes: Iterable[Outcome]) -> dict:
    """Base rate and the sanity numbers that decide whether it is trustworthy."""
    rows = list(outcomes)
    if not rows:
        return {"n": 0}

    n = len(rows)
    wins = [o for o in rows if o.resolved == "target"]
    stops = [o for o in rows if o.resolved == "stop"]
    timeouts = [o for o in rows if o.resolved == "timeout"]
    amb = [o for o in rows if o.ambiguous]
    resolved = len(wins) + len(stops)
    rs = [o.r_multiple for o in rows]

    return {
        "n": n,
        "hit_rate": len(wins) / resolved if resolved else 0.0,
        "n_target": len(wins), "n_stop": len(stops), "n_timeout": len(timeouts),
        "ambiguous_frac": len(amb) / n,
        "mean_r": float(np.mean(rs)),
        "median_rr_offered": float(np.median([o.rr for o in rows])),
        "expectancy_r": float(np.mean(rs)),
        "mean_bars_to_resolve": float(np.mean([o.bars_to_resolve for o in rows])),
        "mean_mfe_r": float(np.mean([o.mfe_r for o in rows])),
        "mean_mae_r": float(np.mean([o.mae_r for o in rows])),
    }


def to_frame(outcomes: Iterable[Outcome]) -> pd.DataFrame:
    rows = [o.as_row() for o in outcomes]
    return pd.DataFrame(rows) if rows else pd.DataFrame()
