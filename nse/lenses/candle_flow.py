"""Candle-structure lens — what the shape of a bar says about who won it.

Reads the three things a candle can actually tell you, without pretending to
read the one it cannot.

    REJECTION WICK   a long wick means price went there and was refused. A long
                     LOWER wick with a close near the high is buyers taking back
                     the level; a long UPPER wick with a close near the low is
                     sellers doing the same.

    CLOSE LOCATION   where in the bar's own range the close landed. A bar that
                     travels a long way and closes at one end was directional;
                     one that closes mid-range was a fight nobody won.

    EFFORT vs RESULT high volume with a small range is ABSORPTION — someone is
                     filling size without moving price, which is the classic
                     footprint of a large participant working an order. Low
                     volume with a large range is the opposite: an unopposed
                     move through thin book, which tends not to hold.

WHAT THIS LENS DOES NOT CLAIM TO SEE

Real order flow needs the AGGRESSOR SIDE of each trade — who crossed the spread
— and neither data source has it. The NIFTY archive carries bar volume with no
buy/sell split; the live SNAP_QUOTE feed carries depth and total quantities but
not executed aggression. Delta bars are the same.

So "order flow in candles" here means effort-versus-result inferred from volume
and range, which is a PROXY and a weaker one than a real footprint chart. Saying
so up front matters because the phrase invites the assumption that this is
tape-reading, and it is not.

CONVENTION, DECLARED BEFORE MEASUREMENT

    long lower wick + close near high            -> LONG   (demand rejected lower)
    long upper wick + close near low             -> SHORT  (supply rejected higher)
    absorption (high volume, small range),
        direction taken from close location      -> follow the close
    unopposed move (low volume, large range)     -> FADE it

Components combine by weighted mean and the lens abstains when none fires. If
measurement inverts this, that is ONE BIT of information — that continuation
was the right reading — and NOT permission to flip the signs and re-run
(RESEARCH_LEARNINGS section 3.13, where exactly that produced a fake winner).

NSE BACKTEST AND NSE LIVE DO NOT SEE THE SAME VOLUME, AND THIS LENS WILL
OVERSTATE ITSELF ON NIFTY IF THAT IS FORGOTTEN.

`nse/backtest/replay.py` builds each index bar's volume by SUMMING OPTION
VOLUME across the chain for that minute. That is a real number and a reasonable
proxy for participation, so the effort/result component runs in backtest. The
LIVE index feed carries no volume at all — a cash index prints a level, not a
trade — so live the same component silently does not contribute.

A NIFTY measurement therefore describes a three-component lens while production
runs a one-component one. `has_volume` is journaled on every verdict so the two
cases can be separated after the fact rather than averaged together, and any
NIFTY result must be read with it. On crypto perps the volume is genuinely
traded and both paths agree.

VOLUME IS OPTIONAL AND ITS ABSENCE IS HANDLED HONESTLY. A cash index has no
traded volume, so on NIFTY the effort/result component simply does not
contribute and the lens runs on wick geometry alone — at lower conviction,
because it is reading less. On crypto perps, where volume is real, all three
components run. The lens reports which ones fired so the two cases are never
confused in the record.
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

#: Bars used to normalise volume and range. Short enough to track a changing
#: session, long enough that one outlier does not define "normal".
NORM_WINDOW = 20

#: A wick must be at least this fraction of the bar's range to count as a
#: rejection rather than noise.
MIN_WICK_FRAC = 0.45

#: Close must be within this fraction of an extreme to count as "near" it.
CLOSE_NEAR_FRAC = 0.30

#: Volume z-score above / range z-score below which a bar counts as absorption.
ABSORB_VOL_Z = 1.0
ABSORB_RANGE_Z = -0.25

#: Mirror image: an unopposed move on thin participation.
THIN_VOL_Z = -0.75
THIN_RANGE_Z = 0.75

W_WICK, W_ABSORB, W_THIN = 1.0, 1.0, 0.75


class CandleFlowLens(BaseLens):
    name = "candle_flow"
    backtestable = True

    def _evaluate(self, snap: MarketSnapshot) -> LensVerdict:
        bars = snap.continuous_bars(MIN_BARS)
        if bars is None or len(bars) < MIN_BARS:
            n = 0 if bars is None else len(bars)
            return abstain(self.name, f"{n} bars — need {MIN_BARS}")
        need = {"open", "high", "low", "close"}
        if not need <= set(bars.columns):
            return abstain(self.name, "bars lack OHLC — this lens reads shape")

        o = _num(bars["open"]); h = _num(bars["high"])
        l = _num(bars["low"]); c = _num(bars["close"])
        rng = h - l
        if not np.isfinite(rng[-1]) or rng[-1] <= 0:
            return abstain(self.name, "last bar has no range")

        # ── wick geometry ────────────────────────────────────────────────────
        body_hi = max(o[-1], c[-1])
        body_lo = min(o[-1], c[-1])
        upper = (h[-1] - body_hi) / rng[-1]
        lower = (body_lo - l[-1]) / rng[-1]
        close_loc = (c[-1] - l[-1]) / rng[-1]        # 0 at the low, 1 at the high

        parts: list[tuple[float, float]] = []
        fired: list[str] = []

        if lower >= MIN_WICK_FRAC and close_loc >= 1 - CLOSE_NEAR_FRAC:
            parts.append((min(1.0, lower / 0.7), W_WICK))
            fired.append(f"lower wick {lower:.0%} rejected, close at {close_loc:.0%}")
        elif upper >= MIN_WICK_FRAC and close_loc <= CLOSE_NEAR_FRAC:
            parts.append((-min(1.0, upper / 0.7), W_WICK))
            fired.append(f"upper wick {upper:.0%} rejected, close at {close_loc:.0%}")

        # ── effort vs result, only when volume is real ───────────────────────
        vol_z = range_z = None
        if "volume" in bars.columns:
            v = _num(bars["volume"])
            if np.nansum(v[-NORM_WINDOW:]) > 0:
                vol_z = _z(v, NORM_WINDOW)
                range_z = _z(rng, NORM_WINDOW)

        if vol_z is not None and range_z is not None:
            lean = (close_loc - 0.5) * 2.0            # -1 at the low, +1 at the high
            if vol_z >= ABSORB_VOL_Z and range_z <= ABSORB_RANGE_Z:
                parts.append((lean, W_ABSORB))
                fired.append(f"absorption: volume {vol_z:+.1f}sd on range "
                             f"{range_z:+.1f}sd")
            elif vol_z <= THIN_VOL_Z and range_z >= THIN_RANGE_Z:
                # Unopposed move through a thin book — faded, so the sign is
                # inverted relative to where it closed.
                parts.append((-lean, W_THIN))
                fired.append(f"thin move: volume {vol_z:+.1f}sd on range "
                             f"{range_z:+.1f}sd")

        if not parts:
            return LensVerdict(
                lens=self.name, direction=Direction.NEUTRAL, confidence=0.0,
                rationale=(f"no shape: wicks {lower:.0%}/{upper:.0%}, close at "
                           f"{close_loc:.0%} of range"),
                features=_feat(upper, lower, close_loc, vol_z, range_z, [], snap))

        wsum = sum(w for _, w in parts)
        score = sum(s * w for s, w in parts) / wsum
        direction = (Direction.LONG if score > 0 else
                     Direction.SHORT if score < 0 else Direction.NEUTRAL)

        return LensVerdict(
            lens=self.name, direction=direction,
            confidence=min(1.0, abs(score)),
            rationale=f"score {score:+.3f} — " + "; ".join(fired),
            features=_feat(upper, lower, close_loc, vol_z, range_z, fired, snap,
                           score))


def _num(col) -> np.ndarray:
    return pd.to_numeric(col, errors="coerce").to_numpy(float)


def _z(arr: np.ndarray, window: int) -> Optional[float]:
    """Z-score of the last value against the PRECEDING window.

    The current bar is excluded from its own baseline. Including it drags the
    mean toward the value being tested and systematically shrinks every
    z-score — a small bias that would make extreme bars look ordinary, which is
    precisely the signal this lens is trying to find.
    """
    hist = arr[-(window + 1):-1]
    hist = hist[np.isfinite(hist)]
    if len(hist) < window // 2:
        return None
    sd = float(np.std(hist))
    if sd <= 0:
        return None
    return float((arr[-1] - float(np.mean(hist))) / sd)


def _feat(upper, lower, close_loc, vol_z, range_z, fired, snap, score=0.0) -> dict:
    return {
        "upper_wick_frac": round(float(upper), 4),
        "lower_wick_frac": round(float(lower), 4),
        "close_location": round(float(close_loc), 4),
        "volume_z": None if vol_z is None else round(float(vol_z), 3),
        "range_z": None if range_z is None else round(float(range_z), 3),
        "has_volume": vol_z is not None,
        "components": list(fired),
        "score": round(float(score), 5),
        "spot": round(float(snap.spot), 2),
    }
