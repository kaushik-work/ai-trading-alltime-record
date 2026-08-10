"""Extension lens — how much of the move has already happened.

THE PROBLEM THIS ADDRESSES, IN THE OPERATOR'S WORDS

    "today's entry was a late entry and it hit stoploss, it was like entering
     at momentum end"

Nothing else in the council measures how far a move has already travelled.
`volume_oi` reads where OI walls sit, `ict_smc` reads structure, `momentum`
reads whether a range broke — none of them asks whether the move is young or
finished. A signal that is right about direction can still be a bad entry if
the distance is already spent, and that failure looks identical to a wrong
signal in the P&L.

There is measured support for the concern. `momentum` — which buys breakouts,
i.e. enters after a move has started — measured NEGATIVE on both venues
(NIFTY −1.06/−0.73, ETHUSD −3.14/−0.05). Entering late losing money is not
folklore here; it is the one thing this repo has measured twice.

WHAT IT MEASURES

    travel        distance from the session/window anchor, in ATR
    exhaustion    how much of the recent range is already behind price
    persistence   consecutive bars in the same direction

CONVENTION, DECLARED BEFORE MEASUREMENT

This is a CONTEXT lens. It emits NEUTRAL with a confidence meaning "how much
room is left", so the directional harness scores it at exactly zero and that
zero means nothing — the same arrangement as `liquidity`, and for the same
reason.

    confidence near 1.0   the move is young; there is room
    confidence near 0.0   the move is spent; a fresh entry is late

It is measured as a GATE on the lead lens — does `volume_oi` do better on the
bars this lens calls "early"? — not as a direction. And the gate is measured
against a random subset of the same size, because any filter that admits fewer,
more-selective bars will show a higher mean regardless of whether it selected
anything (RESEARCH_LEARNINGS section 3.15, where `ict_smc` looked like the best
filter in the set on TRAIN at bootstrap p=0.0010 and was worth nothing on
VALIDATE).

If it does not clear that control it stays at weight 0 like everything else.
The operator's observation being plausible is not evidence.
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

#: Window whose extremes define "the recent range".
LOOKBACK = 20

#: Travel from the anchor, in ATR, at which a move counts as fully extended.
#: CALIBRATED ON TRAIN — not a guess: it is the point past which there is, by
#: this lens's definition, no room left.
FULL_TRAVEL_ATR = 3.0

#: Consecutive same-direction bars at which persistence saturates.
FULL_PERSISTENCE = 6


class ExtensionLens(BaseLens):
    name = "extension"
    backtestable = True

    def _evaluate(self, snap: MarketSnapshot) -> LensVerdict:
        bars = snap.continuous_bars(MIN_BARS)
        if bars is None or len(bars) < MIN_BARS:
            n = 0 if bars is None else len(bars)
            return abstain(self.name, f"{n} bars — need {MIN_BARS}")
        if "close" not in bars.columns:
            return abstain(self.name, "bars lack close")

        c = pd.to_numeric(bars["close"], errors="coerce").to_numpy(float)
        hi = pd.to_numeric(bars.get("high", bars["close"]),
                           errors="coerce").to_numpy(float)
        lo = pd.to_numeric(bars.get("low", bars["close"]),
                           errors="coerce").to_numpy(float)

        atr = _atr(bars)
        if atr is None or atr <= 0:
            return abstain(self.name, "no ATR — cannot scale travel")

        px = float(c[-1])

        # ── travel from the anchor ───────────────────────────────────────────
        # The anchor is the start of the visible window, which for NSE is the
        # session and for crypto is the rolling window. Distance from it, in
        # ATR, is how far this leg has come.
        anchor = float(c[0])
        travel_atr = abs(px - anchor) / atr
        room_travel = float(np.clip(1.0 - travel_atr / FULL_TRAVEL_ATR, 0.0, 1.0))

        # ── position within the recent range ─────────────────────────────────
        w_hi = float(np.max(hi[-LOOKBACK:]))
        w_lo = float(np.min(lo[-LOOKBACK:]))
        span = max(w_hi - w_lo, 1e-9)
        loc = (px - w_lo) / span                      # 0 at the low, 1 at the high
        # Room is measured toward whichever extreme price is NOT at. Sitting on
        # either extreme means the range is spent in that direction.
        room_range = float(1.0 - abs(loc - 0.5) * 2.0)

        # ── persistence ──────────────────────────────────────────────────────
        d = np.sign(np.diff(c[-(FULL_PERSISTENCE + 2):]))
        run = 0
        for x in d[::-1]:
            if x == 0 or (run and x != np.sign(d[-1])):
                break
            run += 1
        room_persist = float(np.clip(1.0 - run / FULL_PERSISTENCE, 0.0, 1.0))

        # Geometric mean: a move that is extended on ANY axis has little room,
        # and an average would let two comfortable readings hide one extreme.
        room = float((room_travel * room_range * room_persist) ** (1 / 3))

        state = ("early" if room >= 0.6 else
                 "mid" if room >= 0.3 else "extended")

        return LensVerdict(
            lens=self.name,
            direction=Direction.NEUTRAL,       # context only — see the docstring
            confidence=room,
            rationale=(f"{state}: travelled {travel_atr:.1f} ATR from the anchor, "
                       f"at {loc:.0%} of the {LOOKBACK}-bar range, "
                       f"{run} bars in one direction"),
            features={
                "room": round(room, 4),
                "room_travel": round(room_travel, 4),
                "room_range": round(room_range, 4),
                "room_persistence": round(room_persist, 4),
                "travel_atr": round(travel_atr, 4),
                "range_location": round(loc, 4),
                "run_bars": int(run),
                "atr": round(atr, 4),
                "state": state,
                "spot": round(px, 2),
            },
        )


def _atr(bars: pd.DataFrame) -> Optional[float]:
    try:
        from core.chart.structure import atr_series
        a = atr_series(bars)
        if a is not None and len(a):
            v = float(a[-1])
            if np.isfinite(v) and v > 0:
                return v
    except Exception:
        pass
    c = pd.to_numeric(bars["close"], errors="coerce")
    v = float(c.diff().abs().tail(14).mean())
    return v if np.isfinite(v) and v > 0 else None
