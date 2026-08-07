"""Per-lens brains: measured track record, weight, and selection pressure.

Each lens owns one brain document. That document is its identity — weight,
lifecycle state, rolling attribution and health — and it is what makes a lens
mortal: a lens that does not earn money loses its vote and is eventually
retired, automatically and visibly.

THE METRIC IS EXPECTANCY CONTRIBUTION, NEVER WIN RATE.

    contribution = E[pnl | lens voted WITH the trade]
                 - E[pnl | lens voted AGAINST the trade]

Win rate would select the wrong lens. Booking half at 1R pins win rate at 50%
for every R:R from 1:2 up while capping the average win at ~35 points against
~87 for a single-stage exit — a 29%-win-rate lens with big winners beats a
50%-win-rate lens with small ones. See RESEARCH_LEARNINGS section 3.1.

THREE GUARDRAILS AGAINST FITTING NOISE, each one paid for:

  1. A weight does not move until LENS_MIN_TRADES_FOR_WEIGHT closed trades.
     With ~23 hypotheses roughly one clears p<0.05 by chance (section 2.1).
  2. Attribution runs over a rolling window of TRADES, not one session.
  3. Reviews run once daily, out of hours, frozen during the session.

Transitions are asymmetric on purpose: LENS_SUSPEND_CONTRIBUTION is smaller in
magnitude and needs fewer trades than LENS_PROMOTE_CONTRIBUTION, so losing
money benches a lens faster than making money promotes one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from statistics import mean
from typing import Iterable, Optional

from nse.config import (
    LENS_ATTRIBUTION_WINDOW,
    LENS_DEFAULT_WEIGHT,
    LENS_MAX_STRIKES,
    LENS_MIN_TRADES_FOR_WEIGHT,
    LENS_PROBATION_WEIGHT_CAP,
    LENS_PROMOTE_CONTRIBUTION,
    LENS_PROMOTE_MIN_TRADES,
    LENS_REVIEWS_BEFORE_RETIRE,
    LENS_SUSPEND_CONTRIBUTION,
    LENS_SUSPEND_MIN_TRADES,
    LENS_WEIGHT_MAX,
    LENS_WEIGHT_MIN,
)

logger = logging.getLogger(__name__)

BRAIN_COLLECTION = "nse_lens_brains"
REVIEW_COLLECTION = "nse_lens_reviews"


class Lifecycle(str, Enum):
    """How much say a lens currently has.

        SHADOW      votes recorded, weight 0, no capital. Where every lens
                    starts and where a non-backtestable lens stays until live
                    attribution earns it something.
        PROBATION   weight capped — proven enough to matter, not enough to lead.
        ACTIVE      full weight.
        SUSPENDED   benched for losing money or for being broken. Can recover.
        RETIRED     terminal.
    """

    SHADOW = "SHADOW"
    PROBATION = "PROBATION"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"

    @property
    def can_vote(self) -> bool:
        return self in (Lifecycle.PROBATION, Lifecycle.ACTIVE)


@dataclass
class Attribution:
    """What a lens's votes were worth, measured over closed trades."""

    n_for: int = 0
    n_against: int = 0
    n_neutral: int = 0
    pnl_for: float = 0.0            # mean rupees on trades it backed
    pnl_against: float = 0.0        # mean rupees on trades it opposed
    baseline: float = 0.0           # mean rupees across the whole window
    contribution: float = 0.0       # the number that decides the lens's fate

    @property
    def n_scored(self) -> int:
        return self.n_for + self.n_against + self.n_neutral


@dataclass
class BrainState:
    """One lens's persistent brain."""

    lens: str
    lifecycle: Lifecycle = Lifecycle.SHADOW
    weight: float = 0.0
    bootstrap_weight: float = 0.0     # from backtest; used until live sample lands
    backtestable: bool = True

    n_closed: int = 0                 # closed trades this lens has voted on
    attribution: Attribution = field(default_factory=Attribution)

    n_verdicts: int = 0
    n_abstained: int = 0
    strikes: int = 0                  # consecutive exceptions
    failed_reviews: int = 0

    last_review: Optional[str] = None
    last_transition: Optional[str] = None
    notes: str = ""

    # ── derived, for the dashboard ───────────────────────────────────────────
    @property
    def abstain_rate(self) -> float:
        return self.n_abstained / self.n_verdicts if self.n_verdicts else 0.0

    @property
    def trades_until_review(self) -> int:
        """How many more closed trades before this lens's weight can move.

        Surfaced on the dashboard so a lens's mortality is visible rather than
        implicit.
        """
        return max(0, LENS_MIN_TRADES_FOR_WEIGHT - self.n_closed)

    @property
    def health(self) -> float:
        """0-1 readable health. Contribution dominates; reliability modulates.

        Deliberately NOT the thing that drives transitions — those key off
        contribution directly. This is a human-facing summary.
        """
        if self.n_closed < LENS_SUSPEND_MIN_TRADES:
            base = 0.5                                   # unproven, not unhealthy
        else:
            c = self.attribution.contribution
            span = max(LENS_PROMOTE_CONTRIBUTION, 1.0)
            base = 0.5 + 0.5 * max(-1.0, min(1.0, c / span))
        reliability = 1.0 - min(1.0, self.abstain_rate)
        broken = 1.0 - min(1.0, self.strikes / max(1, LENS_MAX_STRIKES))
        return round(max(0.0, min(1.0, base * (0.5 + 0.3 * reliability + 0.2 * broken))), 4)

    @property
    def is_dying(self) -> bool:
        """True when the next bad review could bench or retire this lens."""
        if self.lifecycle is Lifecycle.SUSPENDED:
            return True
        return (self.n_closed >= LENS_SUSPEND_MIN_TRADES
                and self.attribution.contribution < 0)

    def effective_weight(self) -> float:
        """The weight the aggregator should actually use right now."""
        if not self.lifecycle.can_vote:
            return 0.0
        if self.lifecycle is Lifecycle.PROBATION:
            return min(self.weight, LENS_PROBATION_WEIGHT_CAP)
        return self.weight

    def to_doc(self) -> dict:
        d = asdict(self)
        d["lifecycle"] = self.lifecycle.value
        d["health"] = self.health
        d["abstain_rate"] = round(self.abstain_rate, 4)
        d["trades_until_review"] = self.trades_until_review
        d["effective_weight"] = round(self.effective_weight(), 4)
        d["is_dying"] = self.is_dying
        return d

    @classmethod
    def from_doc(cls, doc: dict) -> "BrainState":
        attr = doc.get("attribution") or {}
        known = {f for f in Attribution.__dataclass_fields__}
        return cls(
            lens=doc["lens"],
            lifecycle=Lifecycle(doc.get("lifecycle", "SHADOW")),
            weight=float(doc.get("weight", 0.0)),
            bootstrap_weight=float(doc.get("bootstrap_weight", 0.0)),
            backtestable=bool(doc.get("backtestable", True)),
            n_closed=int(doc.get("n_closed", 0)),
            attribution=Attribution(**{k: v for k, v in attr.items() if k in known}),
            n_verdicts=int(doc.get("n_verdicts", 0)),
            n_abstained=int(doc.get("n_abstained", 0)),
            strikes=int(doc.get("strikes", 0)),
            failed_reviews=int(doc.get("failed_reviews", 0)),
            last_review=doc.get("last_review"),
            last_transition=doc.get("last_transition"),
            notes=doc.get("notes", ""),
        )


# ── attribution ──────────────────────────────────────────────────────────────

def compute_attribution(lens: str, closed_trades: Iterable[dict],
                        window: int = LENS_ATTRIBUTION_WINDOW) -> Attribution:
    """Score one lens over the most recent `window` closed trades.

    `closed_trades` are decision documents that resulted in a filled, closed
    position. Each carries the realized `pnl` and the `verdicts` of every lens
    at the moment of the decision — including the lenses that were voted down,
    which is precisely what makes this computable for trades a lens opposed.

    Degenerate case worth naming: a heavily-weighted lens may be on the winning
    side of the vote every single time, leaving `n_against` at zero. Comparing
    it against an empty set would divide by nothing, so it is scored against
    the window BASELINE instead — did the trades it backed beat the average
    trade? That is the honest question when a lens never dissents.
    """
    rows = list(closed_trades)[-window:] if window else list(closed_trades)
    if not rows:
        return Attribution()

    pnl_for: list[float] = []
    pnl_against: list[float] = []
    n_neutral = 0
    all_pnl: list[float] = []

    for row in rows:
        pnl = row.get("pnl")
        if pnl is None:
            continue
        pnl = float(pnl)
        all_pnl.append(pnl)

        verdict = _verdict_for(row, lens)
        if verdict is None or verdict.get("abstained"):
            continue                                  # never scored on what it didn't see

        lens_dir = int(verdict.get("direction", 0))
        trade_dir = int(row.get("direction", 0))
        if lens_dir == 0 or trade_dir == 0:
            n_neutral += 1
        elif lens_dir == trade_dir:
            pnl_for.append(pnl)
        else:
            pnl_against.append(pnl)

    if not all_pnl:
        return Attribution()

    baseline = mean(all_pnl)
    m_for = mean(pnl_for) if pnl_for else 0.0
    m_against = mean(pnl_against) if pnl_against else 0.0

    if pnl_for and pnl_against:
        contribution = m_for - m_against
    elif pnl_for:
        contribution = m_for - baseline           # never dissented: beat the average?
    elif pnl_against:
        contribution = baseline - m_against       # only ever dissented, and was wrong
    else:
        contribution = 0.0

    return Attribution(
        n_for=len(pnl_for),
        n_against=len(pnl_against),
        n_neutral=n_neutral,
        pnl_for=round(m_for, 2),
        pnl_against=round(m_against, 2),
        baseline=round(baseline, 2),
        contribution=round(contribution, 2),
    )


def _verdict_for(decision_doc: dict, lens: str) -> Optional[dict]:
    for v in decision_doc.get("verdicts", []) or []:
        if v.get("lens") == lens:
            return v
    return None


# ── lifecycle ────────────────────────────────────────────────────────────────

def review(state: BrainState, attribution: Attribution,
           n_closed: int) -> tuple[BrainState, Optional[str]]:
    """Apply one daily review. Returns the updated state and a transition note.

    Pure and side-effect free so it is directly unit-testable: hand it a state
    and an attribution, assert the transition. Persistence is the caller's job.
    """
    state.attribution = attribution
    state.n_closed = n_closed
    state.last_review = datetime.now(timezone.utc).isoformat()

    c = attribution.contribution
    before = state.lifecycle
    note: Optional[str] = None

    # Broken beats unprofitable: a lens that cannot read the snapshot is absent,
    # not merely wrong, and its contribution number is meaningless.
    if state.strikes >= LENS_MAX_STRIKES and state.lifecycle is not Lifecycle.RETIRED:
        state.lifecycle = Lifecycle.SUSPENDED
        state.weight = 0.0
        note = f"SUSPENDED: {state.strikes} consecutive errors"
        state.last_transition = note
        return state, note

    if state.lifecycle is Lifecycle.RETIRED:
        state.weight = 0.0
        return state, None

    losing = (n_closed >= LENS_SUSPEND_MIN_TRADES and c < LENS_SUSPEND_CONTRIBUTION)
    earning = (n_closed >= LENS_PROMOTE_MIN_TRADES and c > LENS_PROMOTE_CONTRIBUTION)

    if state.lifecycle is Lifecycle.SUSPENDED:
        if earning:
            state.lifecycle = Lifecycle.PROBATION      # must re-earn, never straight to ACTIVE
            state.failed_reviews = 0
            note = f"REINSTATED to PROBATION: contribution {c:+.0f}/trade"
        else:
            state.failed_reviews += 1
            if state.failed_reviews >= LENS_REVIEWS_BEFORE_RETIRE:
                state.lifecycle = Lifecycle.RETIRED
                note = (f"RETIRED after {state.failed_reviews} failed reviews, "
                        f"contribution {c:+.0f}/trade")

    elif losing:
        state.lifecycle = Lifecycle.SUSPENDED
        state.failed_reviews = 1
        note = (f"SUSPENDED: contribution {c:+.0f}/trade over {n_closed} trades "
                f"(threshold {LENS_SUSPEND_CONTRIBUTION:+.0f})")

    elif state.lifecycle is Lifecycle.SHADOW:
        if earning:
            state.lifecycle = Lifecycle.PROBATION
            note = f"PROMOTED to PROBATION: contribution {c:+.0f}/trade"

    elif state.lifecycle is Lifecycle.PROBATION:
        # Twice the sample to go fully ACTIVE — promotion is the slow direction.
        if earning and n_closed >= LENS_PROMOTE_MIN_TRADES * 2:
            state.lifecycle = Lifecycle.ACTIVE
            note = f"PROMOTED to ACTIVE: contribution {c:+.0f}/trade over {n_closed}"

    state.weight = _weight_for(state)
    if note:
        state.last_transition = note
        logger.info("lens %s: %s -> %s | %s", state.lens, before.value,
                    state.lifecycle.value, note)
    return state, note


def _weight_for(state: BrainState) -> float:
    """Weight from measured contribution, bounded.

    Below the minimum sample the lens keeps its BOOTSTRAP weight — the value
    its backtest earned it — rather than drifting on a handful of live trades.
    """
    if not state.lifecycle.can_vote:
        return 0.0
    if state.n_closed < LENS_MIN_TRADES_FOR_WEIGHT:
        return max(LENS_WEIGHT_MIN, min(LENS_WEIGHT_MAX, state.bootstrap_weight))
    span = max(LENS_PROMOTE_CONTRIBUTION, 1.0)
    scaled = LENS_DEFAULT_WEIGHT * (1.0 + state.attribution.contribution / span)
    return round(max(LENS_WEIGHT_MIN, min(LENS_WEIGHT_MAX, scaled)), 4)


def record_verdict(state: BrainState, abstained: bool, errored: bool) -> BrainState:
    """Fold one tick's verdict into the running counters.

    Strikes count CONSECUTIVE errors and reset on any clean read, so an
    intermittently flaky lens is not benched for one bad snapshot while a
    genuinely broken one trips the limit quickly.
    """
    state.n_verdicts += 1
    if abstained:
        state.n_abstained += 1
    state.strikes = state.strikes + 1 if errored else 0
    return state


# ── persistence ──────────────────────────────────────────────────────────────

def load(lens: str, *, backtestable: bool = True,
         bootstrap_weight: float = 0.0) -> BrainState:
    """Load a lens's brain, creating a SHADOW brain on first sight.

    A brand-new lens always starts in SHADOW with weight 0. It cannot trade
    your capital on the strength of an assertion — only on a measured record.
    """
    try:
        from core.mongo import get_db
        db = get_db()
        if db is not None:
            doc = db[BRAIN_COLLECTION].find_one({"lens": lens}, {"_id": 0})
            if doc:
                return BrainState.from_doc(doc)
    except Exception as e:
        logger.warning("brain load failed for %s (using fresh SHADOW): %s", lens, e)

    return BrainState(
        lens=lens,
        lifecycle=Lifecycle.SHADOW,
        weight=0.0,
        bootstrap_weight=bootstrap_weight if backtestable else 0.0,
        backtestable=backtestable,
    )


def save(state: BrainState) -> bool:
    """Persist a brain. Returns False when Mongo is unavailable — never raises."""
    try:
        from core.mongo import get_db
        db = get_db()
        if db is None:
            return False
        db[BRAIN_COLLECTION].update_one(
            {"lens": state.lens},
            {"$set": {**state.to_doc(),
                      "updated_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )
        return True
    except Exception as e:
        logger.warning("brain save failed for %s: %s", state.lens, e)
        return False


def log_transition(state: BrainState, note: str) -> None:
    """Append to the lifecycle audit trail. Why a lens died stays on record."""
    try:
        from core.mongo import get_db
        db = get_db()
        if db is None:
            return
        db[REVIEW_COLLECTION].insert_one({
            "ts": datetime.now(timezone.utc).isoformat(),
            "lens": state.lens,
            "lifecycle": state.lifecycle.value,
            "weight": state.weight,
            "n_closed": state.n_closed,
            "contribution": state.attribution.contribution,
            "note": note,
        })
    except Exception as e:
        logger.warning("brain transition log failed for %s: %s", state.lens, e)
