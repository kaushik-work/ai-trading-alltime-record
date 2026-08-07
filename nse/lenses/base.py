"""The lens contract: what a specialist reads, and what it is allowed to say.

Every lens implements one method:

    evaluate(snapshot) -> LensVerdict

That signature is the whole point. A lens receives the shared observation and
returns its own reading — it cannot see another lens's verdict, cannot fetch
its own data, and cannot place an order. Independence is structural, not a
convention someone has to remember.

ABSTAIN is a first-class answer and is NOT the same as NEUTRAL:

    NEUTRAL   "I looked and I see no edge here."     counts in attribution
    ABSTAIN   "I cannot read this snapshot."          excluded from attribution

Conflating them would quietly poison the brain: a lens that crashes on every
expiry-day snapshot would otherwise accumulate a track record of NEUTRAL votes
on exactly the sessions it never actually saw.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Optional, Protocol, runtime_checkable

from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)


class Direction(IntEnum):
    """Which way a lens thinks the underlying resolves."""

    SHORT = -1
    NEUTRAL = 0
    LONG = 1

    @property
    def label(self) -> str:
        return {-1: "SHORT", 0: "NEUTRAL", 1: "LONG"}[int(self)]


@dataclass(frozen=True)
class LensVerdict:
    """One lens's reading of one snapshot.

    `features` carries the numbers behind the call and is journaled verbatim.
    That is what makes a bad trade auditable six weeks later, and it is the
    training input for the daily attribution pass — so put the inputs that
    drove the decision in it, not a prose summary.

    `rationale` is the human-readable line that reaches the dashboard's left
    panel. Write it for the person reading the panel at 09:20, not for a log
    grep.
    """

    lens: str
    direction: Direction
    confidence: float                 # 0.0 - 1.0, magnitude only
    rationale: str = ""
    features: dict = field(default_factory=dict)
    abstained: bool = False
    error: Optional[str] = None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        # Clamp rather than raise. A lens returning confidence 5.0 is a bug,
        # but killing the tick mid-session is worse than voting at 1.0 and
        # shouting about it — the aggregator would otherwise silently hand that
        # lens five votes.
        c = self.confidence
        if c is None or c != c:                      # None or NaN
            object.__setattr__(self, "confidence", 0.0)
            object.__setattr__(self, "abstained", True)
            logger.error("lens %s returned non-numeric confidence %r -> ABSTAIN",
                         self.lens, c)
        elif not (0.0 <= c <= 1.0):
            logger.error("lens %s returned confidence %.4f outside [0,1] — clamped",
                         self.lens, c)
            object.__setattr__(self, "confidence", max(0.0, min(1.0, float(c))))

        if not isinstance(self.direction, Direction):
            try:
                object.__setattr__(self, "direction", Direction(int(self.direction)))
            except (ValueError, TypeError):
                logger.error("lens %s returned invalid direction %r -> ABSTAIN",
                             self.lens, self.direction)
                object.__setattr__(self, "direction", Direction.NEUTRAL)
                object.__setattr__(self, "abstained", True)

    @property
    def signed_confidence(self) -> float:
        """Direction * confidence — the quantity the aggregator actually sums.

        Always 0.0 for an abstention, so an unreadable snapshot cannot tug the
        vote in either direction.
        """
        if self.abstained:
            return 0.0
        return float(self.direction) * self.confidence

    @property
    def counts_for_attribution(self) -> bool:
        """Abstentions are excluded from the lens's measured track record."""
        return not self.abstained

    def to_doc(self) -> dict:
        """Journal form. Every verdict is stored, including voted-down ones."""
        return {
            "lens": self.lens,
            "direction": int(self.direction),
            "direction_label": self.direction.label,
            "confidence": round(self.confidence, 6),
            "signed_confidence": round(self.signed_confidence, 6),
            "rationale": self.rationale,
            "features": self.features,
            "abstained": self.abstained,
            "error": self.error,
            "ts": self.ts.isoformat(),
        }


def abstain(lens: str, why: str, error: Optional[str] = None) -> LensVerdict:
    """Build an abstention. Prefer this over returning a zero-confidence NEUTRAL."""
    return LensVerdict(
        lens=lens,
        direction=Direction.NEUTRAL,
        confidence=0.0,
        rationale=why,
        abstained=True,
        error=error,
    )


@runtime_checkable
class Lens(Protocol):
    """Structural type for anything the aggregator will poll."""

    name: str

    def evaluate(self, snap: MarketSnapshot) -> LensVerdict: ...


class BaseLens(ABC):
    """Common scaffolding. Subclasses implement `_evaluate` only.

    `safe_evaluate` is what the runner calls. It guarantees the runner always
    receives a LensVerdict — never an exception — so one broken lens cannot
    stop a tick or prevent the other lenses from voting. A lens that raises
    returns ABSTAIN carrying the error, and the brain records a health strike
    against it.
    """

    #: Stable identifier. Used as the Mongo key for this lens's brain document,
    #: so renaming it orphans that lens's entire measured history.
    name: str = "base"

    #: Set False on a lens that cannot be replayed against Mongo history. Such
    #: a lens can never earn weight the way the numeric ones do — there is no
    #: historical record to score it on — so it is pinned to SHADOW.
    backtestable: bool = True

    @abstractmethod
    def _evaluate(self, snap: MarketSnapshot) -> LensVerdict:
        """Read the snapshot. Raise freely — safe_evaluate contains it."""

    def safe_evaluate(self, snap: MarketSnapshot) -> LensVerdict:
        try:
            verdict = self._evaluate(snap)
        except Exception as e:
            logger.exception("lens %s raised on %s: %s", self.name, snap.describe(), e)
            return abstain(self.name, f"{type(e).__name__} while reading snapshot",
                           error=f"{type(e).__name__}: {e}")

        if verdict is None:
            logger.error("lens %s returned None", self.name)
            return abstain(self.name, "returned no verdict",
                           error="TypeError: _evaluate returned None")

        if verdict.lens != self.name:
            # A lens mislabelling itself would file its verdict under another
            # lens's brain document and corrupt that lens's track record.
            logger.error("lens %s returned verdict labelled %r — relabelling",
                         self.name, verdict.lens)
            object.__setattr__(verdict, "lens", self.name)

        return verdict

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
