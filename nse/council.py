"""The council: lenses read, then talk, then one call comes out.

This replaces the weighted vote in `nse/aggregator.py`, which measured as a
dead end — three negative lenses outvote one positive one, and averaging can
only ever add an opinion to the pile, never use one to disqualify another
(RESEARCH_LEARNINGS section 3.13).

    ROUND 0   every lens reads the snapshot alone.        -> attribution
    ROUND 1   every lens hears the others and may move.   -> deliberation
    ROUND 2   the council resolves one call.              -> decision

WHAT RESOLUTION IS, AND WHY IT IS NOT A MEAN

A mean says "the average opinion is mildly long". A council says "the lens that
has earned the right to lead is long with conviction, nobody credible objects,
and yesterday says this regime suits it — so we act". Those differ most exactly
where it matters: when opinions conflict. A mean splits the difference and
trades a blurred version of both. A council can stand aside, and standing aside
is the move that a weighted sum structurally cannot make.

Four things can stop a trade here, and only the last is a threshold:

    no lens with weight speaks             nobody credible read this
    the lead lens lacks conviction         MEASURED, see below
    a credible peer objects                disagreement, unresolved by talking
    conviction below the floor             the ordinary gate

THE CONVICTION GATE IS THE ONE PART OF THIS THAT IS MEASURED

`COUNCIL_MIN_LEAD_CONFIDENCE` is volume_oi's TRAIN 67th percentile. Restricting
to its top confidence tercile scored TRAIN +2.42 bps (p=0.0052) and VALIDATE
+3.87 bps (p=0.0107), against an ungated baseline of +1.66 / +1.49 — positive
in both splits with agreeing signs, on 888 and 513 observations
(RESEARCH_LEARNINGS section 3.15).

It does NOT clear a Bonferroni threshold across the 18 gates tested (p<0.00278),
so it is applied as a PROBATION-grade rule and re-measured on live attribution,
not treated as established. It survives here because it is the simplest rule in
the set — one lens's own confidence, no foreign lens, no fitted interaction —
and because everything richer failed.

WHAT WAS TRIED AND FAILED, SO IT IS NOT TRIED AGAIN

Using the REJECTED lenses as filters — the obvious way to give a negative lens a
job — looked excellent on TRAIN and died on VALIDATE:

    ict_smc flipped-confirms   TRAIN +4.82 (boot p=0.0010) -> VALIDATE +0.55 (0.75)
    vwap flipped-confirms      TRAIN +2.93 (boot p=0.0168) -> VALIDATE +1.01 (0.71)

That was a real hypothesis, properly tested, and it was wrong. A negative lens
is not secretly a good filter; it is just negative. The council therefore lets
peers OBJECT but never lets them CONFIRM — an objection can only ever make it
trade less, which is the direction in which being wrong is survivable.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from nse.brain import BrainState
from nse.brain import load as load_brain
from nse.config import VOTE_THRESHOLD
from nse.lenses.base import Direction, LensVerdict
from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

COUNCIL_COLLECTION = "nse_council_decisions"

#: volume_oi's TRAIN 67th percentile. See the module docstring.
COUNCIL_MIN_LEAD_CONFIDENCE: float = 0.414

#: An objector needs at least this much conviction to be worth listening to.
#: Below it a dissenting lens is noise, and letting noise veto trades is just a
#: slower way of not trading.
COUNCIL_MIN_OBJECTION_CONFIDENCE: float = 0.30

#: Is deliberation allowed to CHANGE the traded decision, or only to annotate it?
#:
#: False, because it has not earned it. Measured on 389 sessions, adding
#: deliberation to the conviction-gated council moved VALIDATE +3.70 -> +4.14 bps
#: and adding the journal moved it +4.14 -> +5.01, which reads as both
#: mechanisms working. Neither survived the control: each arm also TRADES FEWER
#: BARS, and against a random subset of its own parent of the same size,
#: deliberation scored p=0.2125 and the journal p=0.1970. Only the conviction
#: gate cleared that test (p=0.0203). See RESEARCH_LEARNINGS section 3.16.
#:
#: So round 1 still runs, is still journaled, and still produces the transcript
#: on the dashboard — the operator sees the lenses argue in real time. It simply
#: cannot move capital yet. This is the SHADOW rule that governs lenses, applied
#: to a mechanism: present and auditable from day one, load-bearing only once
#: live attribution earns it.
#:
#: Flip to True when deliberation beats its parent on live closed trades.
COUNCIL_DELIBERATION_BINDING: bool = False

#: How much a live objection cuts conviction. Deliberately not a veto: a lens
#: with zero measured edge should be able to give the council pause, not the
#: power to overrule the one lens that has been shown to work.
COUNCIL_OBJECTION_HAIRCUT: float = 0.5


@dataclass
class CouncilDecision:
    """One resolved call, with the whole conversation attached."""

    decision_id: str
    ts: datetime
    symbol: str
    spot: float
    atm: int
    dte: float

    direction: Direction
    conviction: float
    executed: bool
    reason: str

    lead: Optional[str] = None                 # which lens led the call
    round0: list = field(default_factory=list)  # independent — ATTRIBUTION USES THIS
    round1: list = field(default_factory=list)  # post-deliberation
    objections: list = field(default_factory=list)
    weights: dict = field(default_factory=dict)
    journal_session: Optional[str] = None      # which journal informed this

    n_spoke: int = 0
    n_deferred: int = 0
    n_revised: int = 0
    n_abstained: int = 0

    def to_doc(self) -> dict:
        return {
            "decision_id": self.decision_id, "ts": self.ts.isoformat(),
            "symbol": self.symbol, "spot": self.spot, "atm": self.atm,
            "dte": round(self.dte, 4),
            "direction": int(self.direction),
            "direction_label": self.direction.label,
            "conviction": round(self.conviction, 6),
            "executed": self.executed, "reason": self.reason, "lead": self.lead,
            # Both rounds are stored. Round 0 is what the brain scores; round 1
            # is what actually traded. Keeping only one of them would make
            # either attribution or post-mortems impossible.
            "round0": [v.to_doc() for v in self.round0],
            "round1": [v.to_doc() for v in self.round1],
            "objections": self.objections,
            "weights": self.weights,
            "journal_session": self.journal_session,
            "n_spoke": self.n_spoke, "n_deferred": self.n_deferred,
            "n_revised": self.n_revised, "n_abstained": self.n_abstained,
        }

    def transcript(self) -> str:
        """The conversation, for the dashboard's left panel."""
        head = (f"[{self.ts:%H:%M}] {self.direction.label} "
                f"conviction {self.conviction:+.3f} — "
                f"{'EXECUTE' if self.executed else 'stand aside'}: {self.reason}")
        lines = [head]
        for v in self.round1:
            if v.abstained:
                lines.append(f"    {v.lens:<11} abstains — {v.rationale}")
            elif v.deferred:
                lines.append(f"    {v.lens:<11} defers — {v.revision_note}")
            elif v.revised:
                od, oc = v.revised_from
                lines.append(f"    {v.lens:<11} {Direction(od).label} {oc:.2f} -> "
                             f"{v.direction.label} {v.confidence:.2f} — {v.revision_note}")
            else:
                lines.append(f"    {v.lens:<11} {v.direction.label} "
                             f"{v.confidence:.2f} — {v.rationale}")
        return "\n".join(lines)


class Council:
    """Runs the three rounds and resolves them."""

    def __init__(self, lenses: Sequence, brains: Optional[dict] = None,
                 threshold: float = VOTE_THRESHOLD,
                 min_lead_confidence: float = COUNCIL_MIN_LEAD_CONFIDENCE,
                 deliberation_binding: bool = COUNCIL_DELIBERATION_BINDING):
        self.lenses = list(lenses)
        self.threshold = threshold
        self.min_lead_confidence = min_lead_confidence
        self.deliberation_binding = deliberation_binding
        self.brains: dict = brains if brains is not None else {
            l.name: load_brain(l.name, backtestable=getattr(l, "backtestable", True))
            for l in self.lenses}

    def weight_of(self, name: str) -> float:
        b = self.brains.get(name)
        return float(b.effective_weight()) if isinstance(b, BrainState) else 0.0

    # ── the three rounds ─────────────────────────────────────────────────────
    def deliberate(self, snap: MarketSnapshot, journal=None) -> CouncilDecision:
        round0 = [l.safe_evaluate(snap) for l in self.lenses]
        by_name = {v.lens: v for v in round0}

        # Round 1. Every lens sees the same round-0 map minus itself, so the
        # order lenses are polled in cannot change the outcome — a council
        # whose answer depended on seating order would not be reproducible.
        round1 = []
        for lens in self.lenses:
            own = by_name[lens.name]
            peers = {k: v for k, v in by_name.items() if k != lens.name}
            round1.append(lens.safe_deliberate(snap, own, peers, journal))

        # Both rounds are always computed and always journaled. Which one the
        # DECISION comes from is the measured question — see
        # COUNCIL_DELIBERATION_BINDING.
        deciding = round1 if self.deliberation_binding else round0
        d = self._resolve(snap, round0, round1, journal, deciding)
        if not self.deliberation_binding:
            d.reason += " [deliberation shadow: annotates, does not bind]"
        return d

    def _resolve(self, snap: MarketSnapshot, round0: list, round1: list,
                 journal, deciding: Optional[list] = None) -> CouncilDecision:
        deciding = round1 if deciding is None else deciding
        weights = {v.lens: self.weight_of(v.lens) for v in deciding}
        speaking = [v for v in deciding if v.speaks and weights.get(v.lens, 0.0) > 0
                    and v.direction != Direction.NEUTRAL]

        def build(direction, conviction, executed, reason, lead=None, objections=()):
            return CouncilDecision(
                decision_id=f"cnl_{snap.symbol}_{snap.ts:%Y%m%d_%H%M%S}_"
                            f"{uuid.uuid4().hex[:6]}",
                ts=snap.ts, symbol=snap.symbol, spot=snap.spot, atm=snap.atm,
                dte=snap.dte, direction=direction, conviction=conviction,
                executed=executed, reason=reason, lead=lead,
                round0=round0, round1=round1, objections=list(objections),
                weights=weights,
                journal_session=(journal.session.isoformat()
                                 if journal is not None else None),
                n_spoke=len(speaking),
                n_deferred=sum(1 for v in round1 if v.deferred),
                n_revised=sum(1 for v in round1 if v.revised),
                n_abstained=sum(1 for v in round1 if v.abstained),
            )

        if not speaking:
            return build(Direction.NEUTRAL, 0.0, False,
                         "no lens with weight had an opinion on this snapshot")

        # The LEAD is the weighted-most-confident lens. Not a mean: one lens
        # owns the call and is accountable for it in attribution.
        lead = max(speaking, key=lambda v: weights[v.lens] * v.confidence)
        direction = lead.direction

        if lead.confidence < self.min_lead_confidence:
            return build(direction, 0.0, False,
                         f"{lead.lens} leads at confidence {lead.confidence:.3f}, "
                         f"below the measured floor {self.min_lead_confidence:.3f}",
                         lead=lead.lens)

        # Peers may OBJECT — never confirm. See the module docstring: letting a
        # measured-negative lens add conviction is how the TRAIN-only filter
        # result would have crept into production.
        objections = [
            {"lens": v.lens, "direction": v.direction.label,
             "confidence": round(v.confidence, 4), "why": v.rationale[:160]}
            for v in deciding
            if v.speaks and v.direction != Direction.NEUTRAL
            and v.direction != direction
            and v.confidence >= COUNCIL_MIN_OBJECTION_CONFIDENCE
            and v.lens != lead.lens]

        conviction = lead.confidence * (COUNCIL_OBJECTION_HAIRCUT ** len(objections))
        signed = conviction * float(direction)

        if conviction < self.threshold:
            why = (f"conviction {conviction:.3f} below threshold "
                   f"{self.threshold:.2f}")
            if objections:
                why += (f" after {len(objections)} objection(s): "
                        + ", ".join(o["lens"] for o in objections))
            return build(direction, signed, False, why, lead=lead.lens,
                         objections=objections)

        reason = f"{lead.lens} leads {direction.label} at {lead.confidence:.3f}"
        if objections:
            reason += f", {len(objections)} objection(s) noted and discounted"
        return build(direction, signed, True, reason, lead=lead.lens,
                     objections=objections)


# ── persistence ──────────────────────────────────────────────────────────────
def journal_decision(d: CouncilDecision) -> bool:
    """Store a decision, executed or not. Never raises.

    Rejected decisions are stored identically to executed ones — they are the
    control group that makes per-lens attribution computable at all.
    """
    try:
        from core.mongo import get_db
        db = get_db()
        if db is None:
            return False
        db[COUNCIL_COLLECTION].update_one({"decision_id": d.decision_id},
                                          {"$set": d.to_doc()}, upsert=True)
        return True
    except Exception as e:
        logger.warning("council journal failed for %s: %s", d.decision_id, e)
        return False
