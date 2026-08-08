"""The trade: higher-timeframe level, lower-timeframe sweep, chart-based target.

    15m   where the levels are, and which way the structure leans
     5m   the sweep that triggers, and the wick the stop hides behind

    entry    the close of the sweep bar
    stop     beyond the sweep's own extreme (core/chart/sweep.py explains why)
    target   the next opposing level on the 15m chart

THE TARGET IS READ OFF THE CHART, NOT ASSUMED

This is the specific correction to what killed the previous strategy. The
retired price-action bot paired a ~0.7% stop with a 1:7 target on a four-hour
hold. Measured afterwards, that target was reachable in 0.46% of BTC windows
and 1.15% of ETH windows, and was hit ONCE in 515 trades
(RESEARCH_LEARNINGS 1.4). The R:R on paper was fiction.

Here the target is wherever the next real level sits, so R:R is an OUTPUT that
gets measured, not an input that gets wished for. Setups whose nearest opposing
level is closer than MIN_RR away are rejected rather than stretched to fit —
the market decides whether a trade is worth taking, not the ratio we would
prefer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from core.chart.structure import ChartStructure, read_structure
from core.chart.sweep import Sweep, latest_sweep

logger = logging.getLogger(__name__)

# Reject anything that cannot pay this much for the risk taken. Not a target —
# a filter on setups whose next level is too close to be worth the spread.
MIN_RR = 1.2

# Refuse a stop wider than this in ATR. A very wide stop is usually a sign the
# sweep was actually a breakout, and on a leveraged venue it also walks the
# position toward liquidation (see core/chart/sizing.py).
MAX_STOP_ATR = 1.6

# The 15m trend must not be directly against the trade. A sweep of lows in a
# clean downtrend is often just the downtrend continuing.
RESPECT_TREND = True


@dataclass
class Setup:
    ts: object
    direction: int              # +1 long, -1 short
    entry: float
    stop: float
    target: float
    atr: float
    rr: float
    stop_atr: float
    sweep: Sweep
    trend: str
    target_level_touches: int
    rationale: str = ""

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward(self) -> float:
        return abs(self.target - self.entry)

    def as_dict(self) -> dict:
        return {"direction": self.direction, "entry": round(self.entry, 4),
                "stop": round(self.stop, 4), "target": round(self.target, 4),
                "rr": round(self.rr, 3), "stop_atr": round(self.stop_atr, 3),
                "risk": round(self.risk, 4), "reward": round(self.reward, 4),
                "trend": self.trend,
                "target_touches": self.target_level_touches,
                "sweep": self.sweep.as_dict(), "rationale": self.rationale}


def find_setup(df_entry: pd.DataFrame, df_structure: pd.DataFrame, *,
               min_rr: float = MIN_RR, max_stop_atr: float = MAX_STOP_ATR,
               respect_trend: bool = RESPECT_TREND,
               within_bars: int = 1) -> Optional[Setup]:
    """One setup from the latest bar, or None.

    BOTH frames must end at the SAME moment and contain only closed bars. The
    15m frame is the structure, the 5m frame is the trigger; feeding a 15m
    frame that extends past the 5m frame's last bar leaks the future into the
    level set.
    """
    structure = read_structure(df_structure)
    if structure is None:
        return None

    sweep = latest_sweep(df_entry, within_bars=within_bars)
    if sweep is None:
        return None

    if respect_trend:
        if sweep.direction > 0 and structure.trend == "down":
            return None
        if sweep.direction < 0 and structure.trend == "up":
            return None

    entry = sweep.close
    stop = sweep.stop
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    stop_atr = risk / structure.atr if structure.atr > 0 else 999.0
    if stop_atr > max_stop_atr:
        return None

    # Target: the next opposing level on the higher timeframe. Read, not chosen.
    lvl = (structure.nearest_above(entry) if sweep.direction > 0
           else structure.nearest_below(entry))
    if lvl is None:
        return None

    target = lvl.price
    reward = abs(target - entry)
    rr = reward / risk
    if rr < min_rr:
        return None

    ts = df_entry.index[-1] if isinstance(df_entry.index, pd.DatetimeIndex) else (
        df_entry["datetime"].iloc[-1] if "datetime" in df_entry.columns else None)

    side = "long" if sweep.direction > 0 else "short"
    return Setup(
        ts=ts, direction=sweep.direction, entry=entry, stop=stop, target=target,
        atr=structure.atr, rr=rr, stop_atr=stop_atr, sweep=sweep,
        trend=structure.trend, target_level_touches=lvl.touches,
        rationale=(f"{side}: swept {sweep.level:.2f} to {sweep.extreme:.2f} "
                   f"({sweep.pierce_atr:.2f} ATR) and rejected "
                   f"{sweep.rejection_frac:.0%}; stop {stop:.2f} beyond the wick, "
                   f"target {target:.2f} at a {lvl.touches}-touch level, "
                   f"R:R {rr:.2f}, 15m trend {structure.trend}"),
    )
