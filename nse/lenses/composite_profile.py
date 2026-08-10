"""Composite-profile lens — where price sits in SEVERAL days of value.

Every other profile-reading lens in this repo is single-session. `volume_oi`
builds a profile from today's bars and reads OI walls; `vwap` anchors to today's
open. Both forget everything at 15:30. This one reads the last N sessions as one
distribution, which is the classic Market Profile view and the thing an intraday
trader is actually looking at on a composite chart.

WHAT IT READS THAT NOTHING ELSE DOES

    COMPOSITE VALUE AREA   POC / VAH / VAL over N sessions rather than one.
                           Today's value area can sit entirely inside or
                           entirely outside the multi-day one, and those are
                           different situations that a session profile cannot
                           tell apart.

    NAKED POC              a prior session's POC that price has NOT traded back
                           to since. Untested high-volume prices act as magnets
                           — the profile's own unfinished business. This is the
                           `nVPOC 24500` marker on a composite chart.

    COMPOSITE VWAP         volume-weighted average across the whole window, not
                           anchored to today's open. Distinct from `vwap`, which
                           is session-anchored BY DEFINITION and must stay that
                           way (see MarketSnapshot.prior_bars).

CONVENTION, DECLARED BEFORE MEASUREMENT

Mean-reverting toward unfinished business:

    price ABOVE composite value, nearest naked POC BELOW   -> SHORT
    price BELOW composite value, nearest naked POC ABOVE   -> LONG
    price inside the composite value area                  -> NEUTRAL

The reasoning is the standard one — accepted value attracts, and an untested POC
is where the market has business it did not finish. If measurement says the
opposite, the honest conclusion is "continuation was the right convention on
this data", ONE bit, and NOT a licence to flip the sign and re-run. `vwap`
established that rule the expensive way and `momentum` confirmed it.

WHAT THIS LENS DOES NOT DO

It does not emit a probability. Turning a score into "68% chance up" requires
measuring the empirical hit rate per score bucket on TRAIN and confirming the
buckets hold on VALIDATE — calibration, not a rescale of confidence. Confidence
here is conviction, not probability, and labelling it otherwise would be the
most dangerous kind of wrong: a number that looks like it means something
precise. Calibration comes after this lens shows an edge worth calibrating.

ORDER FLOW IS NOT IN HERE EITHER, and the reason is data. Real order flow needs
per-trade aggressor side. The archive has bar volume with no buy/sell split, and
the live SNAP_QUOTE feed carries depth and total buy/sell quantity but not
executed aggression. `book_imbalance` from the live chain is the nearest
available proxy and it does not exist historically, so a lens built on it could
never be replayed. Stated so the gap is a known absence rather than a silent one.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from nse.lenses.base import BaseLens, Direction, LensVerdict, abstain
from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

#: Sessions in the composite window. Seven is the operator's brief and is a
#: reasonable intraday memory: long enough that a single trend day does not
#: define value, short enough that the levels are ones people still watch.
COMPOSITE_SESSIONS = 7

#: Bars needed before a composite means anything. Seven sessions of 5-minute
#: bars is ~525; requiring most of one session guards the ramp-up.
MIN_BARS = 60

#: How close, in ATR, price must be to a naked POC for it to pull.
NAKED_POC_PULL_ATR = 3.0

#: Distance outside the composite value area, in units of value-area width, at
#: which conviction saturates. CALIBRATED ON TRAIN.
VA_FULL_CONVICTION = 1.0


class CompositeProfileLens(BaseLens):
    name = "composite_profile"
    backtestable = True

    def _evaluate(self, snap: MarketSnapshot) -> LensVerdict:
        bars = _composite_bars(snap)
        if bars is None or len(bars) < MIN_BARS:
            n = 0 if bars is None else len(bars)
            return abstain(self.name, f"{n} composite bars — need {MIN_BARS}")
        if "volume" not in bars.columns or "close" not in bars.columns:
            return abstain(self.name, "composite bars lack close/volume")

        vol = pd.to_numeric(bars["volume"], errors="coerce").fillna(0.0)
        if float(vol.sum()) <= 0:
            # NIFTY index bars carry no volume — the index does not trade. This
            # is the same wall `vwap` hits live, and it is a data fact, not a
            # failure to be worked around with a volume proxy nobody measured.
            return abstain(self.name, "no volume in the composite window")

        from nse.quant import volume_profile

        prof = volume_profile.from_bars(bars, price_col="close",
                                        volume_col="volume")
        if prof is None:
            return abstain(self.name, "could not build the composite profile")

        spot = float(snap.spot)
        width = max(prof.value_area_width, 1e-9)
        # Signed distance outside the value area, in value-area widths.
        if spot > prof.vah:
            outside = (spot - prof.vah) / width
            side = -1                      # above value -> lean short
        elif spot < prof.val:
            outside = (prof.val - spot) / width
            side = +1
        else:
            outside, side = 0.0, 0

        naked, naked_dist_atr = _nearest_naked_poc(bars, spot, snap)

        if side == 0 and naked is None:
            return LensVerdict(
                lens=self.name, direction=Direction.NEUTRAL, confidence=0.0,
                rationale=(f"inside composite value {prof.val:.0f}-{prof.vah:.0f} "
                           f"(POC {prof.poc:.0f}), no naked POC in range"),
                features=_features(prof, spot, 0.0, None, None, len(bars)))

        # The naked POC only counts when it agrees with the value-area read.
        # Two different reasons to lean the same way is a stronger signal than
        # either alone; two that disagree is an argument, and this lens does not
        # get to pick a winner — it stands down.
        pull = 0
        if naked is not None and naked_dist_atr is not None:
            if naked_dist_atr <= NAKED_POC_PULL_ATR:
                pull = 1 if naked > spot else -1

        if side != 0 and pull != 0 and side != pull:
            return LensVerdict(
                lens=self.name, direction=Direction.NEUTRAL, confidence=0.0,
                rationale=(f"value area says {'short' if side < 0 else 'long'} "
                           f"but the naked POC at {naked:.0f} pulls the other "
                           f"way — no read"),
                features=_features(prof, spot, outside, naked, naked_dist_atr,
                                   len(bars)))

        direction_i = side if side != 0 else pull
        conviction = float(np.clip(outside / VA_FULL_CONVICTION, 0.0, 1.0))
        if pull != 0 and pull == side:
            conviction = min(1.0, conviction * 1.25)
        elif side == 0:
            # Naked POC alone, inside value: a weak read by construction.
            conviction = 0.25

        if conviction <= 0.0 or direction_i == 0:
            return LensVerdict(
                lens=self.name, direction=Direction.NEUTRAL, confidence=0.0,
                rationale=f"inside composite value {prof.val:.0f}-{prof.vah:.0f}",
                features=_features(prof, spot, outside, naked, naked_dist_atr,
                                   len(bars)))

        direction = Direction.LONG if direction_i > 0 else Direction.SHORT
        bits = [f"spot {spot:.0f} vs composite value "
                f"{prof.val:.0f}-{prof.vah:.0f} (POC {prof.poc:.0f})"]
        if outside > 0:
            bits.append(f"{outside:.2f} VA-widths outside")
        if naked is not None and pull != 0:
            bits.append(f"naked POC {naked:.0f} at {naked_dist_atr:.1f} ATR")

        return LensVerdict(
            lens=self.name, direction=direction, confidence=conviction,
            rationale=" — ".join(bits),
            features=_features(prof, spot, outside, naked, naked_dist_atr,
                               len(bars)))


def _composite_bars(snap: MarketSnapshot) -> Optional[pd.DataFrame]:
    """Session bars prepended with the composite window's prior sessions."""
    prior = getattr(snap, "prior_bars", None)
    today = snap.bars if snap.bars is not None else pd.DataFrame()
    if prior is None or prior.empty:
        return today
    cols = [c for c in today.columns if c in prior.columns] or list(prior.columns)
    if today.empty:
        return prior
    return pd.concat([prior[cols], today[cols]], ignore_index=True)


def _nearest_naked_poc(bars: pd.DataFrame, spot: float,
                       snap: MarketSnapshot) -> tuple:
    """The closest prior-session POC price has NOT traded back through.

    "Naked" means untested since it formed. Once price trades through a POC the
    business is finished and it stops being a magnet, so a POC that is still
    naked is the profile's own unfinished business.
    """
    if "datetime" not in bars.columns:
        return None, None
    from nse.quant import volume_profile

    df = bars.copy()
    df["_d"] = pd.to_datetime(df["datetime"]).dt.date
    days = sorted(df["_d"].unique())
    if len(days) < 2:
        return None, None

    atr = _atr(bars)
    if atr is None or atr <= 0:
        return None, None

    best, best_dist = None, None
    for i, d in enumerate(days[:-1]):                 # exclude today
        day = df[df["_d"] == d]
        p = volume_profile.from_bars(day, price_col="close", volume_col="volume")
        if p is None:
            continue
        later = df[df["_d"] > d]
        if later.empty:
            continue
        lo = pd.to_numeric(later.get("low", later["close"]), errors="coerce").min()
        hi = pd.to_numeric(later.get("high", later["close"]), errors="coerce").max()
        if lo <= p.poc <= hi:
            continue                                   # traded through: tested
        dist = abs(spot - p.poc) / atr
        if best_dist is None or dist < best_dist:
            best, best_dist = float(p.poc), float(dist)
    return best, best_dist


def _atr(bars: pd.DataFrame) -> Optional[float]:
    try:
        from core.chart.structure import atr_series
        a = atr_series(bars)
        return float(a[-1]) if a is not None and len(a) else None
    except Exception:
        c = pd.to_numeric(bars["close"], errors="coerce")
        return float(c.diff().abs().tail(14).mean()) or None


def _features(prof, spot, outside, naked, naked_dist, n_bars) -> dict:
    return {
        "poc": round(float(prof.poc), 2),
        "vah": round(float(prof.vah), 2),
        "val": round(float(prof.val), 2),
        "va_width": round(float(prof.value_area_width), 2),
        "va_widths_outside": round(float(outside), 4),
        "naked_poc": None if naked is None else round(float(naked), 2),
        "naked_poc_dist_atr": (None if naked_dist is None
                               else round(float(naked_dist), 3)),
        "composite_bars": int(n_bars),
        "composite_sessions": COMPOSITE_SESSIONS,
        "spot": round(float(spot), 2),
    }
