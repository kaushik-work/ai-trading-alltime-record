"""Volume/OI lens — where positioning sits, and where business actually got done.

Three reads on the same snapshot, deliberately kept separable so attribution can
later say which one (if any) carried the signal rather than crediting the blend:

  1. OI WALLS.  The strike with the most call OI acts as resistance, the strike
     with the most put OI as support — not because open interest is magic, but
     because the dealers short those strikes hedge against them, and that
     hedging flow leans against price approaching. Spot's position between the
     two walls is the read.

  2. VOLUME PROFILE.  Where the index actually traded, by price. Inside the
     value area is acceptance; outside it is a probe that either gets accepted
     or rejected. Built from bars STRICTLY up to the decision timestamp, since
     a session's POC is only knowable once the session is over.

  3. OI BUILD DIRECTION.  Change in call OI versus put OI since the session's
     first print. Put writing into strength is bullish positioning; call
     writing into weakness is bearish.

WHAT THIS LENS DOES NOT DO

It does not read OI as a level in isolation. Total OI rises through the week and
collapses at expiry for reasons that have nothing to do with direction, so a raw
level is a calendar artefact. Everything here is relative — spot against a wall,
spot against the value area, calls against puts.

CALIBRATION

Every threshold is measured on TRAIN before use, not guessed. The Greeks lens
was a reminder of why: its structural constant looked reasonable at 0.0 and
would have produced a permanent bearish tilt, because the underlying quantity is
negative 98.4% of the time. Measure the distribution, then set the gate — see
RESEARCH_LEARNINGS 1.3 and 3.5.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from nse.lenses.base import BaseLens, Direction, LensVerdict, abstain
from nse.quant import volume_profile
from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

# Minimum strikes per side before the OI picture is worth reading.
MIN_STRIKES_PER_SIDE = 5

# Minimum bars before a volume profile means anything. A POC built from six
# minutes of trade is a coin flip wearing a level's clothes.
MIN_BARS_FOR_PROFILE = 20

# MEASURED ON TRAIN (2021-2023) only, 120 sessions / 1,440 verdicts at a
# 30-minute grid. Each scale is the p90 of |component|, so roughly the top
# decile of readings saturates and the rest scale linearly beneath it.
#
#   component              mean     sd     |x| p90   coverage
#   wall_position         +0.044   0.472    0.769      89%
#   value_area_position   +0.061   0.810    1.289      92%
#   oi_build              +0.008   0.412    0.685     100%
#
# All three already centre near zero, so unlike the Greeks lens there is no
# structural tilt to subtract. What DOES need correcting is scale: the raw
# spreads differ two-fold, so an equal-weight blend of the raw numbers would be
# quietly dominated by the value-area term regardless of what it was saying.
# Dividing each by its own p90 puts them on one footing, which is what makes
# W_WALL / W_VALUE_AREA / W_BUILD mean what they claim to.
WALL_FULL_CONVICTION = 0.769   # |wall position| that saturates conviction
VA_FULL_CONVICTION = 1.289     # |value-area position| that saturates
BUILD_FULL_CONVICTION = 0.685  # |OI build imbalance| that saturates

# Blend weights across the three reads. Equal until live attribution earns a
# lens component more say — the brain weights LENSES, and this is the same
# principle applied one level down.
W_WALL, W_VALUE_AREA, W_BUILD = 1.0, 1.0, 1.0


class VolumeOILens(BaseLens):
    name = "volume_oi"
    backtestable = True

    def _evaluate(self, snap: MarketSnapshot) -> LensVerdict:
        chain = snap.chain
        if chain is None or chain.empty:
            return abstain(self.name, "empty chain")

        col = "option_type" if "option_type" in chain.columns else "side"
        if col not in chain.columns or "strike" not in chain.columns:
            return abstain(self.name, "chain lacks strike/option_type")
        if "oi" not in chain.columns:
            return abstain(self.name, "chain carries no open interest")

        df = chain.copy()
        df["_t"] = df[col].astype(str).str.upper().str[0]
        df["oi"] = pd.to_numeric(df["oi"], errors="coerce").fillna(0)
        calls = df[df["_t"] == "C"]
        puts = df[df["_t"] == "P"]
        if len(calls) < MIN_STRIKES_PER_SIDE or len(puts) < MIN_STRIKES_PER_SIDE:
            return abstain(self.name,
                           f"only {len(calls)}C/{len(puts)}P strikes — too thin")
        if calls["oi"].sum() <= 0 or puts["oi"].sum() <= 0:
            return abstain(self.name, "no open interest on one side")

        wall_pos, walls = _wall_position(calls, puts, snap.spot)
        va_pos, prof = _value_area_position(snap)
        build, build_detail = _oi_build(calls, puts)

        components: list[tuple[float, float]] = []      # (signal, weight)
        if wall_pos is not None:
            components.append((_clamp(wall_pos / WALL_FULL_CONVICTION), W_WALL))
        if va_pos is not None:
            components.append((_clamp(va_pos / VA_FULL_CONVICTION), W_VALUE_AREA))
        if build is not None:
            components.append((_clamp(build / BUILD_FULL_CONVICTION), W_BUILD))

        if not components:
            return abstain(self.name, "no component produced a reading")

        wsum = sum(w for _, w in components)
        score = sum(s * w for s, w in components) / wsum

        if score > 0:
            direction = Direction.LONG
        elif score < 0:
            direction = Direction.SHORT
        else:
            direction = Direction.NEUTRAL

        pcr = float(puts["oi"].sum() / calls["oi"].sum())
        return LensVerdict(
            lens=self.name,
            direction=direction,
            confidence=min(1.0, abs(score)),
            rationale=_rationale(score, wall_pos, walls, va_pos, prof, build),
            features={
                "score": round(score, 5),
                "wall_position": None if wall_pos is None else round(wall_pos, 5),
                "call_wall": walls[0], "put_wall": walls[1],
                "value_area_position": None if va_pos is None else round(va_pos, 5),
                "poc": None if prof is None else round(prof.poc, 2),
                "vah": None if prof is None else round(prof.vah, 2),
                "val": None if prof is None else round(prof.val, 2),
                "oi_build": None if build is None else round(build, 5),
                "call_oi_change": build_detail[0],
                "put_oi_change": build_detail[1],
                "pcr_oi": round(pcr, 4),
                "n_components": len(components),
                "dte": round(snap.dte, 3),
                "spot": round(snap.spot, 2),
            },
        )

    def _deliberate(self, snap: MarketSnapshot, own: LensVerdict,
                    peers: dict, journal) -> LensVerdict:
        """Listen, but only to things that should make this lens LESS sure.

        This is the only lens with a measured edge, so it leads. That makes the
        asymmetry here deliberate: a peer can lower its confidence and can never
        raise it.

        The reason is measured, not stylistic. Using the rejected lenses as
        CONFIRMING filters looked outstanding on TRAIN — ict_smc's agreement
        marked bars worth +4.82 bps against a +1.66 baseline, bootstrap
        p=0.0010 — and collapsed to +0.55 bps (p=0.75) on VALIDATE
        (RESEARCH_LEARNINGS section 3.15). Confirmation from a lens with no
        measured edge is not evidence; it only looked like evidence.

        Objection is treated differently from confirmation because the two fail
        differently. Being wrongly talked out of a trade costs one trade's
        expectancy. Being wrongly talked into one costs a position.
        """
        cut, notes = 1.0, []

        # A structural peer sitting on the opposite side is a reason for pause.
        ict = peers.get("ict_smc")
        if (ict is not None and ict.speaks and ict.direction != Direction.NEUTRAL
                and ict.direction != own.direction and ict.confidence >= 0.30):
            cut *= 0.75
            notes.append(f"ict_smc reads {ict.direction.label} against me "
                         f"at {ict.confidence:.2f}")

        # Yesterday. The journal RECORDS; this lens decides what it means — and
        # what it means is caution, never extra size.
        if journal is not None and journal.struggled(self.name):
            day = journal.lens(self.name)
            cut *= 0.75
            notes.append(f"I lost {day.mean_outcome_bps:+.2f}bps on "
                         f"{journal.session} ({journal.atr_regime}-ATR "
                         f"{journal.trend})")

        if cut >= 1.0:
            return own
        return own.revise(own.direction, own.confidence * cut,
                          "; ".join(notes) + " — holding direction, cutting size")


# ── components ───────────────────────────────────────────────────────────────
def _wall_position(calls: pd.DataFrame, puts: pd.DataFrame,
                   spot: float) -> tuple[Optional[float], tuple]:
    """Where spot sits between the put wall (support) and call wall (resistance).

        -1.0  pinned at the call wall  (resistance overhead -> bearish)
        +1.0  pinned at the put wall   (support beneath   -> bullish)

    Sign convention: NEGATIVE near the call wall, because price pressing into
    heavy call OI is meeting resistance. Returns None when the walls do not
    straddle spot, since "distance to a wall that is on the same side as the
    other one" has no clean interpretation.
    """
    call_wall = int(calls.loc[calls["oi"].idxmax(), "strike"])
    put_wall = int(puts.loc[puts["oi"].idxmax(), "strike"])
    if call_wall <= put_wall:
        return None, (call_wall, put_wall)
    if not (put_wall <= spot <= call_wall):
        return None, (call_wall, put_wall)
    span = call_wall - put_wall
    if span <= 0:
        return None, (call_wall, put_wall)
    # 0 at the put wall, 1 at the call wall -> map to +1 .. -1.
    frac = (spot - put_wall) / span
    return float(1.0 - 2.0 * frac), (call_wall, put_wall)


def _value_area_position(snap: MarketSnapshot):
    """Signed position of spot relative to the value area.

    Positive above VAH (acceptance higher), negative below VAL. Inside the band
    scales linearly between -1 and +1, so acceptance in the middle reads as the
    neutral thing it is.
    """
    bars = snap.bars
    if bars is None or len(bars) < MIN_BARS_FOR_PROFILE:
        return None, None
    step = snap.step
    prof = volume_profile.from_bars(bars, bin_width=step)
    if prof is None or prof.value_area_width <= 0:
        return None, prof
    # position_of returns 0 at VAL and 1 at VAH; recentre to -1..+1.
    return float(2.0 * prof.position_of(snap.spot) - 1.0), prof


def _oi_build(calls: pd.DataFrame, puts: pd.DataFrame
              ) -> tuple[Optional[float], tuple]:
    """Net OI build on puts versus calls, as a signed imbalance in [-1, 1].

    Put writing (put OI rising) is bullish positioning; call writing is bearish.
    Normalised by total build so a quiet day and a frantic one are comparable.
    """
    col = "oi_change" if "oi_change" in calls.columns else None
    if col is None:
        return None, (None, None)
    c = float(pd.to_numeric(calls[col], errors="coerce").fillna(0).sum())
    p = float(pd.to_numeric(puts[col], errors="coerce").fillna(0).sum())
    denom = abs(c) + abs(p)
    if denom <= 0:
        return None, (c, p)
    return float((p - c) / denom), (round(c, 1), round(p, 1))


def _clamp(x: float) -> float:
    return float(max(-1.0, min(1.0, x)))


def _rationale(score, wall_pos, walls, va_pos, prof, build) -> str:
    bits = []
    if wall_pos is not None:
        bits.append(f"spot between put wall {walls[1]} and call wall {walls[0]} "
                    f"({wall_pos:+.2f})")
    if va_pos is not None and prof is not None:
        where = ("above value" if va_pos > 1 else
                 "below value" if va_pos < -1 else "inside value")
        bits.append(f"{where} (POC {prof.poc:.0f}, {va_pos:+.2f})")
    if build is not None:
        bits.append(f"OI build {'puts' if build > 0 else 'calls'} "
                    f"({build:+.2f})")
    return f"score {score:+.3f} — " + "; ".join(bits) if bits else "no reading"
