"""ICT/SMC lens — order blocks, fair-value gaps, liquidity sweeps.

Reads the three structures that Smart Money Concepts is actually about:

  ORDER BLOCK       the last opposing candle before an impulsive move. Price
                    returning to it often reacts, because that is where the
                    move originated. `core/sr_levels.py` already finds these as
                    Rally-Base-Drop / Drop-Base-Rally zones, so that detector is
                    reused rather than rewritten.

  FAIR-VALUE GAP    a three-bar imbalance where bar 1's high is below bar 3's
                    low (or vice versa) — price moved so fast it left no
                    two-sided trade. Gaps tend to get revisited.

  LIQUIDITY SWEEP   a wick through a prior swing extreme that closes back
                    inside. `core/chart/sweep.py` owns this.

THE HONEST CAVEAT, STATED BEFORE MEASUREMENT

Every one of those is defined on WICKS. The archived NIFTY option dataset
carries `spot` as a close-only series, so its resampled highs and lows are
extremes of closes rather than true intrabar extremes (RESEARCH_LEARNINGS §4).
This lens will therefore under-fire on replay relative to live, and will
validate worse than it performs. That is a data limitation, not a verdict — but
it means a weak backtest result here is genuinely ambiguous, where a weak result
from the Greeks lens was not.

Recorded up front so a poor number later is read correctly rather than being
explained away after the fact.

AND THE OTHER ONE: THIS OVERLAPS THE SWEEP WORK THAT ALREADY FAILED

The liquidity-sweep component is the same construct measured on BTC/ETH 5m in
§3.9, where it had no edge in any of four cells. Different market and different
timeframe, so it is worth one honest test here — but the prior is poor, and a
positive result on NIFTY should be treated as a claim needing a hold-out rather
than a discovery.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from nse.lenses.base import BaseLens, Direction, LensVerdict, abstain
from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

# Bars needed before structure means anything.
MIN_BARS = 40

# A fair-value gap must be at least this many ATR wide to count. Below it the
# "imbalance" is a tick of noise between adjacent bars.
MIN_FVG_ATR = 0.15

# Gaps older than this have usually been filled or forgotten.
MAX_FVG_AGE_BARS = 60

# Distance within which price is "at" a zone, in ATR.
ZONE_PROXIMITY_ATR = 0.5

# Scale that saturates each component's contribution. CALIBRATED ON TRAIN —
# left at 1.0 here because the components are already bounded to [-1, 1] by
# construction, unlike the volume_oi components which needed rescaling.
FULL_CONVICTION = 1.0

W_ORDER_BLOCK, W_FVG, W_SWEEP = 1.0, 1.0, 1.0


class ICTSMCLens(BaseLens):
    name = "ict_smc"
    backtestable = True

    def _evaluate(self, snap: MarketSnapshot) -> LensVerdict:
        bars = snap.bars
        if bars is None or len(bars) < MIN_BARS:
            return abstain(self.name,
                           f"{0 if bars is None else len(bars)} bars — too few for structure")
        need = {"open", "high", "low", "close"}
        if not need <= set(bars.columns):
            return abstain(self.name, "bars lack OHLC — this lens needs wicks")

        from core.chart.structure import atr_series, read_structure

        st = read_structure(bars)
        if st is None or st.atr <= 0:
            return abstain(self.name, "could not read structure")

        ob, ob_detail = _order_block_signal(bars, snap.spot, st.atr)
        fvg, fvg_detail = _fvg_signal(bars, snap.spot, st.atr)
        sweep, sweep_detail = _sweep_signal(bars, st)

        parts: list[tuple[float, float]] = []
        if ob is not None:
            parts.append((_clamp(ob / FULL_CONVICTION), W_ORDER_BLOCK))
        if fvg is not None:
            parts.append((_clamp(fvg / FULL_CONVICTION), W_FVG))
        if sweep is not None:
            parts.append((_clamp(sweep / FULL_CONVICTION), W_SWEEP))

        if not parts:
            return abstain(self.name, "no structure in view")

        wsum = sum(w for _, w in parts)
        score = sum(s * w for s, w in parts) / wsum
        direction = (Direction.LONG if score > 0 else
                     Direction.SHORT if score < 0 else Direction.NEUTRAL)

        bits = []
        if ob is not None:
            bits.append(f"order block {ob_detail}")
        if fvg is not None:
            bits.append(f"FVG {fvg_detail}")
        if sweep is not None:
            bits.append(f"sweep {sweep_detail}")

        return LensVerdict(
            lens=self.name,
            direction=direction,
            confidence=min(1.0, abs(score)),
            rationale=f"score {score:+.3f} — " + "; ".join(bits),
            features={
                "score": round(score, 5),
                "order_block": None if ob is None else round(ob, 4),
                "fvg": None if fvg is None else round(fvg, 4),
                "sweep": None if sweep is None else round(sweep, 4),
                "n_components": len(parts),
                "trend": st.trend,
                "atr_pct": round(st.atr / snap.spot * 100, 4) if snap.spot else 0.0,
                "dte": round(snap.dte, 3),
                "spot": round(snap.spot, 2),
            },
        )

    def _deliberate(self, snap: MarketSnapshot, own: LensVerdict,
                    peers: dict, journal) -> LensVerdict:
        """A level that has just been broken is not a level.

        momentum is the direct contradiction to a structural read: if price has
        cleared the trailing range in the opposite direction to the zone this
        lens is leaning on, that zone is being invalidated in real time. A
        supply zone price has already broken up through is not supply.

        Thin participation matters too. Order blocks and fair-value gaps only
        mean anything if there is someone there to defend them.
        """
        cut, notes = 1.0, []

        mo = peers.get("momentum")
        if (mo is not None and mo.speaks and mo.direction != Direction.NEUTRAL
                and mo.direction != own.direction and mo.confidence >= 0.30):
            cut *= 0.5
            notes.append(f"momentum has broken the range {mo.direction.label} "
                         f"through my zone — it is being invalidated")

        lq = peers.get("liquidity")
        if lq is not None and lq.speaks and lq.confidence < 0.4:
            cut *= 0.75
            notes.append(f"thin tape ({lq.confidence:.2f}) — nobody here to "
                         f"defend these levels")

        if cut >= 1.0:
            return own
        return own.revise(own.direction, own.confidence * cut,
                          "; ".join(notes))


# ── components ───────────────────────────────────────────────────────────────
def _order_block_signal(bars: pd.DataFrame, spot: float,
                        atr: float) -> tuple[Optional[float], str]:
    """Where price sits relative to the nearest fresh supply/demand zone.

    Reuses core/sr_levels.py's Rally-Base-Drop / Drop-Base-Rally detector, which
    is exactly order-block logic: the base candles before an impulsive move are
    where the institutional orders sat.

    +1 sitting in a demand zone (buyers below), −1 in a supply zone.
    """
    from core.sr_levels import compute_sr_levels

    df = bars.rename(columns={c: c.capitalize() for c in
                              ("open", "high", "low", "close")})
    try:
        sr = compute_sr_levels(df)
    except Exception as e:
        logger.debug("ict_smc: sr_levels failed: %s", e)
        return None, ""

    demand = sr.get("demand_zones") or []
    supply = sr.get("supply_zones") or []
    if not demand and not supply:
        return None, ""

    prox = atr * ZONE_PROXIMITY_ATR

    def _nearest(zones):
        best, best_d = None, None
        for z in zones:
            top, bot = z.get("top"), z.get("bottom")
            if top is None or bot is None:
                continue
            d = 0.0 if bot <= spot <= top else min(abs(spot - top), abs(spot - bot))
            if best_d is None or d < best_d:
                best, best_d = z, d
        return best, best_d

    d_zone, d_dist = _nearest(demand)
    s_zone, s_dist = _nearest(supply)

    in_demand = d_dist is not None and d_dist <= prox
    in_supply = s_dist is not None and s_dist <= prox
    if in_demand and in_supply:
        # Overlapping zones cancel — the market has orders on both sides here
        # and the structure says nothing directional.
        return 0.0, "price inside overlapping supply and demand"
    if in_demand:
        strength = min(1.0, (d_zone.get("strength") or 1) / 3.0)
        return strength, f"in demand at {d_zone.get('price')}"
    if in_supply:
        strength = min(1.0, (s_zone.get("strength") or 1) / 3.0)
        return -strength, f"in supply at {s_zone.get('price')}"
    return None, ""


def _fvg_signal(bars: pd.DataFrame, spot: float,
                atr: float) -> tuple[Optional[float], str]:
    """Nearest unfilled fair-value gap, and which way it pulls.

    A bullish FVG is a three-bar window where bar 1's HIGH sits below bar 3's
    LOW — price gapped up through a range nobody traded two-sided. Price above
    an unfilled bullish gap tends to get pulled back down into it, so an
    unfilled gap BELOW spot is a downward magnet and vice versa.

    A gap is only counted while unfilled: once price trades back through it the
    imbalance is resolved and it stops being a level.
    """
    h = pd.to_numeric(bars["high"], errors="coerce").to_numpy(float)
    l = pd.to_numeric(bars["low"], errors="coerce").to_numpy(float)
    n = len(h)
    if n < 3 or atr <= 0:
        return None, ""

    best = None      # (distance, direction, lo, hi, age)
    start = max(0, n - MAX_FVG_AGE_BARS)
    for i in range(start, n - 2):
        # bullish gap: high[i] < low[i+2]
        if h[i] < l[i + 2]:
            lo, hi = h[i], l[i + 2]
            if (hi - lo) < atr * MIN_FVG_ATR:
                continue
            if np.any(l[i + 3:] <= lo):      # traded back through -> filled
                continue
            d = 0.0 if lo <= spot <= hi else min(abs(spot - lo), abs(spot - hi))
            if best is None or d < best[0]:
                best = (d, +1, lo, hi, n - 1 - i)
        # bearish gap: low[i] > high[i+2]
        elif l[i] > h[i + 2]:
            lo, hi = h[i + 2], l[i]
            if (hi - lo) < atr * MIN_FVG_ATR:
                continue
            if np.any(h[i + 3:] >= hi):
                continue
            d = 0.0 if lo <= spot <= hi else min(abs(spot - lo), abs(spot - hi))
            if best is None or d < best[0]:
                best = (d, -1, lo, hi, n - 1 - i)

    if best is None:
        return None, ""
    d, direction, lo, hi, age = best
    if d > atr * 2.0:
        return None, ""                       # too far away to matter

    # An unfilled gap acts as a magnet: price is drawn toward it, so the signal
    # points from spot toward the gap rather than in the gap's own direction.
    mid = (lo + hi) / 2.0
    pull = 1.0 if mid > spot else -1.0
    strength = max(0.2, 1.0 - d / (atr * 2.0))
    return pull * strength, (f"{'bull' if direction > 0 else 'bear'} gap "
                             f"{lo:.1f}-{hi:.1f}, {age} bars old")


def _sweep_signal(bars: pd.DataFrame, structure) -> tuple[Optional[float], str]:
    """Most recent liquidity sweep, if it is still fresh.

    Delegates to core/chart/sweep.py — the same detector measured on BTC/ETH in
    §3.9, where it had no edge. Included for completeness of the SMC picture,
    not because the prior is good.
    """
    from core.chart.sweep import latest_sweep

    sw = latest_sweep(bars, structure, within_bars=3)
    if sw is None:
        return None, ""
    strength = min(1.0, sw.rejection_frac)
    return float(sw.direction) * strength, (
        f"{'bull' if sw.direction > 0 else 'bear'} at {sw.level:.1f}, "
        f"{sw.pierce_atr:.2f} ATR")


def _clamp(x: float) -> float:
    return float(max(-1.0, min(1.0, x)))
