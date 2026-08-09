"""The lens contract: what a specialist reads, and what it is allowed to say.

A lens is polled in TWO rounds, and the split between them is the design:

    ROUND 0   evaluate(snapshot) -> LensVerdict
              Independent. Cannot see any other lens. This is the round that
              ATTRIBUTION SCORES.

    ROUND 1   deliberate(snapshot, peers, journal) -> LensVerdict
              Sees every peer's round-0 reading and yesterday's journal, and
              may revise its own. This is where the lenses actually talk.

WHY BOTH ROUNDS EXIST INSTEAD OF JUST THE SECOND

Deliberation is what the operator asked for and it is the more powerful
mechanism — but a purely deliberative council cannot be measured. Once lens B
has heard lens A, B's opinion is partly A's, and "what is B worth?" stops having
an answer. The brain's whole lifecycle — promote, suspend, retire — runs on
per-lens attribution, so a council with no independent round is a council whose
members can never be fired.

Keeping round 0 independent and journaling it separately preserves attribution
intact AND makes the interesting question measurable: does the post-discussion
answer beat the pre-discussion one? That is a number, checked in
`nse/council.py`, not an assumption.

ABSTAIN is a first-class answer and is NOT the same as NEUTRAL:

    NEUTRAL   "I looked and I see no edge here."     counts in attribution
    ABSTAIN   "I cannot read this snapshot."          excluded from attribution

Conflating them would quietly poison the brain: a lens that crashes on every
expiry-day snapshot would otherwise accumulate a track record of NEUTRAL votes
on exactly the sessions it never actually saw.

DEFER is the third answer, and it only exists in round 1:

    DEFER     "another lens already read this better than I can."

A lens that knows it duplicates a peer says so instead of adding a second vote
for one opinion. `vwap` does exactly this: it measured -0.769 correlated with
`volume_oi` (RESEARCH_LEARNINGS section 3.12), so it stands down rather than
letting the council mistake an echo for a confirmation.
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

    # ── set only in round 1, by deliberation ─────────────────────────────────
    #: True when this lens stood down because a peer covers the same ground.
    #: A deferral is NOT an abstention: the lens read the snapshot fine, so it
    #: still counts for attribution on its round-0 opinion. It simply declines
    #: to have that opinion counted twice by the council.
    deferred: bool = False
    #: What this verdict looked like before the lens heard its peers, as
    #: (direction, confidence). None means the lens held its ground.
    revised_from: Optional[tuple] = None
    #: Why it moved. Reaches the dashboard's left panel verbatim.
    revision_note: str = ""

    @property
    def revised(self) -> bool:
        return self.revised_from is not None

    @property
    def speaks(self) -> bool:
        """Does this verdict carry an opinion the council should hear?"""
        return not self.abstained and not self.deferred

    def revise(self, direction: Direction, confidence: float,
               note: str) -> "LensVerdict":
        """Return a revised copy, remembering what it used to say.

        The original reading is preserved in `revised_from` rather than
        overwritten, because a lens that changed its mind after hearing a peer
        is exactly the event worth auditing after a bad trade.
        """
        from dataclasses import replace
        return replace(
            self,
            direction=direction,
            confidence=max(0.0, min(1.0, float(confidence))),
            revised_from=(int(self.direction), round(self.confidence, 6)),
            revision_note=note,
        )

    def defer(self, note: str) -> "LensVerdict":
        """Stand down in favour of a peer covering the same ground."""
        from dataclasses import replace
        return replace(self, deferred=True, revision_note=note)

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

        Always 0.0 for an abstention or a deferral, so neither an unreadable
        snapshot nor a lens that deliberately stood down can tug the vote.
        """
        if self.abstained or self.deferred:
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
            "deferred": self.deferred,
            "revised_from": list(self.revised_from) if self.revised_from else None,
            "revision_note": self.revision_note,
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
    """Structural type for anything the council will poll."""

    name: str

    def evaluate(self, snap: MarketSnapshot) -> LensVerdict: ...

    def deliberate(self, snap: MarketSnapshot, own: LensVerdict,
                   peers: dict, journal) -> LensVerdict: ...


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

    # ── round 1: the lens hears its peers ────────────────────────────────────
    def _deliberate(self, snap: MarketSnapshot, own: LensVerdict,
                    peers: dict, journal) -> LensVerdict:
        """React to what the other lenses said. Default: hold your position.

        `peers` maps lens name -> that lens's ROUND-0 verdict, excluding this
        one. `journal` is yesterday's `DayJournal` (or None on the first
        session ever, and on any day the previous journal is missing).

        Override this to encode how YOUR specialism should respond to another's
        — that is the per-lens brain the operator asked for, and it belongs in
        the lens that owns the domain knowledge, not in a central rulebook.

        Three moves are available, and holding is a real answer:

            own                      hold — nothing a peer said changes my read
            own.revise(d, c, note)   change direction and/or confidence
            own.defer(note)          stand down; a peer covers this ground

        HARD RULE: read `peers` and revise your own verdict. Do not reach into
        the snapshot for anything you did not already look at in round 0. Round
        1 is for reconciling opinions, not for a second bite at the data — a
        lens that behaves differently across the two rounds for reasons
        unrelated to its peers makes the "did talking help?" measurement
        meaningless.
        """
        return own

    def safe_deliberate(self, snap: MarketSnapshot, own: LensVerdict,
                        peers: dict, journal=None) -> LensVerdict:
        """Contained round-1 call. Falls back to the round-0 verdict.

        A lens that crashes while deliberating keeps the opinion it already
        formed rather than dropping out — its independent read was valid and
        losing it would silently shrink the council.
        """
        if own.abstained:
            return own
        try:
            revised = self._deliberate(snap, own, peers, journal)
        except Exception as e:
            logger.exception("lens %s raised while deliberating: %s", self.name, e)
            return own
        if revised is None or not isinstance(revised, LensVerdict):
            logger.error("lens %s returned %r from _deliberate — holding round 0",
                         self.name, revised)
            return own
        if revised.lens != self.name:
            logger.error("lens %s deliberated into a verdict labelled %r",
                         self.name, revised.lens)
            object.__setattr__(revised, "lens", self.name)
        return revised

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
