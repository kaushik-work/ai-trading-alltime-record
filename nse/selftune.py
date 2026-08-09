"""Lenses that re-tune themselves — carefully, and on a short leash.

A lens whose thresholds were fitted once in 2026 is fitted to 2026. Markets move;
a conviction cut that admitted the top third of signals in a calm year admits
almost nothing in a violent one — that is exactly how the ATR gate in
section 3.15 kept 11% of TRAIN and 6% of VALIDATE and stopped meaning the same
thing. Self-tuning is the fix for that.

It is also the single most dangerous mechanism in this repo, because it is an
overfitting engine wearing the costume of adaptiveness. A parameter free to
chase last week's returns will find last week's noise every time, and it will do
it invisibly, one small nudge per day, until the lens is trading a curve fit that
no human ever approved.

So it operates under five hard constraints, each of which exists because of a
specific way this could go wrong:

    1. PERCENTILE TARGETS, NOT VALUE TARGETS. A lens does not learn "trade above
       0.414". It learns "trade the top third", and the VALUE that corresponds to
       is recomputed from its own recent distribution. This is the fix that made
       the regime signal transfer across splits (median 0.287 TRAIN vs 0.280
       VALIDATE) where the absolute cut did not.

    2. CAUSAL WINDOW ONLY. Recalibration at session N sees sessions < N. Never
       the current session, never later ones. Enforced by the caller passing
       strictly-past observations, and asserted here.

    3. MINIMUM SAMPLE. Below MIN_SAMPLE the parameter does not move at all.

    4. SHRINKAGE TOWARD THE BOOTSTRAP VALUE. The new estimate is blended with
       the originally measured one, so a single strange window nudges rather
       than replaces. Shrinkage weight rises with sample size.

    5. A HARD BAND. A parameter can never leave [0.5x, 2.0x] of its measured
       bootstrap value, whatever the data says. If the data genuinely demands
       more than that, the lens is broken and a human should look at it — that
       is a research finding, not something to silently absorb.

WHAT SELF-TUNING IS NOT ALLOWED TO TOUCH

Direction conventions. A lens may adjust HOW SURE it needs to be; it may never
flip which way it reads a signal. Sign-flipping after seeing results is the
error that made `train_signed` look like it beat the champion (section 3.13),
and automating it would industrialise that mistake.

Every adjustment is journaled with its before, after, sample size and window, so
a drifting parameter is visible in the record rather than inferred from
behaviour six weeks later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)

TUNING_COLLECTION = "nse_lens_tuning"

#: Below this many observations in the window, nothing moves.
MIN_SAMPLE: int = 200

#: Sample at which the new estimate is trusted at full weight against the prior.
FULL_TRUST_SAMPLE: int = 800

#: A parameter may never leave this multiple of its bootstrap value.
BAND_LO, BAND_HI = 0.5, 2.0

#: Sessions of history a recalibration may look at.
WINDOW_SESSIONS: int = 60


@dataclass
class Tunable:
    """One self-adjusting parameter.

    `target_percentile` is the invariant the lens actually wants to hold — "keep
    the top third" — and `value` is merely what that currently works out to.
    Storing the intent rather than the number is what makes this transfer across
    regimes instead of decaying into a stale constant.
    """

    name: str
    bootstrap: float
    value: float
    target_percentile: float
    n_last_fit: int = 0
    last_fit: Optional[str] = None
    history: list = field(default_factory=list)

    @property
    def band(self) -> tuple:
        lo, hi = self.bootstrap * BAND_LO, self.bootstrap * BAND_HI
        return (min(lo, hi), max(lo, hi))

    @property
    def at_band_edge(self) -> bool:
        lo, hi = self.band
        return self.value <= lo + 1e-9 or self.value >= hi - 1e-9

    def to_doc(self) -> dict:
        return {"name": self.name, "bootstrap": self.bootstrap,
                "value": self.value, "target_percentile": self.target_percentile,
                "n_last_fit": self.n_last_fit, "last_fit": self.last_fit,
                "history": self.history[-50:]}

    @classmethod
    def from_doc(cls, d: dict) -> "Tunable":
        return cls(name=d["name"], bootstrap=d["bootstrap"], value=d["value"],
                   target_percentile=d["target_percentile"],
                   n_last_fit=d.get("n_last_fit", 0),
                   last_fit=d.get("last_fit"), history=d.get("history", []))


def shrink_weight(n: int) -> float:
    """How much to trust a fresh estimate over the bootstrap prior.

    Zero below MIN_SAMPLE, rising to 1.0 at FULL_TRUST_SAMPLE. Linear because a
    cleverer curve would be a third unmeasured parameter, and the honest default
    for something nobody has measured is the simplest shape that respects the
    two endpoints.
    """
    if n < MIN_SAMPLE:
        return 0.0
    if n >= FULL_TRUST_SAMPLE:
        return 1.0
    return (n - MIN_SAMPLE) / (FULL_TRUST_SAMPLE - MIN_SAMPLE)


def recalibrate(t: Tunable, observations: Sequence[float],
                as_of: Optional[date] = None) -> tuple[Tunable, str]:
    """Move a tunable toward its target percentile of recent observations.

    `observations` must come from sessions STRICTLY BEFORE `as_of`. This
    function cannot verify that itself — it sees only numbers — so the caller
    owns it and `recalibrate_lens` below is the path that enforces it.
    """
    n = len(observations)
    if n < MIN_SAMPLE:
        return t, (f"{t.name}: {n} observations < {MIN_SAMPLE} — held at "
                   f"{t.value:.4f}")

    raw = float(np.percentile(np.asarray(observations, dtype=float),
                              t.target_percentile * 100.0))
    w = shrink_weight(n)
    blended = (1.0 - w) * t.bootstrap + w * raw

    lo, hi = t.band
    clamped = float(min(max(blended, lo), hi))

    before = t.value
    t.value = clamped
    t.n_last_fit = n
    t.last_fit = (as_of or datetime.now(timezone.utc).date()).isoformat()
    t.history.append({"at": t.last_fit, "before": round(before, 6),
                      "after": round(clamped, 6), "raw": round(raw, 6),
                      "n": n, "shrink": round(w, 4)})

    note = (f"{t.name}: {before:.4f} -> {clamped:.4f} "
            f"(p{t.target_percentile:.0%} of {n} obs = {raw:.4f}, "
            f"shrink {w:.2f} toward bootstrap {t.bootstrap:.4f})")
    if clamped != blended:
        # Hitting the band is a finding, not a routine clip. Say so loudly.
        note += f" [CLAMPED to band {lo:.4f}-{hi:.4f} — the data wants more than allowed]"
        logger.warning("selftune %s", note)
    return t, note


def recalibrate_lens(lens_name: str, tunable: Tunable,
                     observations_by_session: dict,
                     as_of: date,
                     window_sessions: int = WINDOW_SESSIONS) -> tuple[Tunable, str]:
    """Recalibrate from sessions strictly before `as_of`.

    This is the guarded entry point. It does the date filtering itself rather
    than trusting a caller to have done it, because "the caller will pass the
    right window" is precisely the assumption that produced the section 1.2
    lookahead bug twice.
    """
    past = sorted(d for d in observations_by_session if d < as_of)
    if not past:
        return tunable, f"{lens_name}: no prior sessions before {as_of}"
    window = past[-window_sessions:]
    values: list = []
    for d in window:
        values.extend(observations_by_session[d])
    values = [v for v in values if v is not None and np.isfinite(v)]

    assert all(d < as_of for d in window), "selftune window leaked the present"
    t, note = recalibrate(tunable, values, as_of=as_of)
    return t, f"[{lens_name}] {note} over {len(window)} sessions"


# ── persistence ──────────────────────────────────────────────────────────────
def save(lens: str, t: Tunable) -> bool:
    try:
        from core.mongo import get_db
        db = get_db()
        if db is None:
            return False
        db[TUNING_COLLECTION].update_one(
            {"lens": lens, "name": t.name},
            {"$set": {**t.to_doc(), "lens": lens,
                      "updated_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True)
        return True
    except Exception as e:
        logger.warning("selftune save failed for %s.%s: %s", lens, t.name, e)
        return False


def load(lens: str, name: str, bootstrap: float,
         target_percentile: float) -> Tunable:
    """Load a tunable, or mint one pinned to its bootstrap value."""
    try:
        from core.mongo import get_db
        db = get_db()
        if db is not None:
            doc = db[TUNING_COLLECTION].find_one({"lens": lens, "name": name},
                                                 {"_id": 0})
            if doc:
                t = Tunable.from_doc(doc)
                # The bootstrap is the code's, not the database's: if the
                # measured prior changed in a commit, the band moves with it.
                t.bootstrap = bootstrap
                lo, hi = t.band
                t.value = float(min(max(t.value, lo), hi))
                return t
    except Exception as e:
        logger.warning("selftune load failed for %s.%s: %s", lens, name, e)
    return Tunable(name=name, bootstrap=bootstrap, value=bootstrap,
                   target_percentile=target_percentile)
