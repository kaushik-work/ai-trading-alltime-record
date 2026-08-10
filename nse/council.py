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

THE SAME ASYMMETRY IS WHY ROUND 1 IS ALLOWED TO BIND AT ALL

Deliberation never cleared its statistical control (p=0.2125 against a random
subset of its own parent). It binds anyway, on an invariant rather than a
p-value: every lens's round-1 logic is MONOTONE DOWNWARD — cut, defer, or hold,
never raise, never flip. So the worst case for an unproven mechanism here is
trades not taken, and `assert_deliberation_monotone()` fails the build if a
future lens breaks that property. The adaptive quorum is equally unproven and
stays in shadow precisely because it does NOT have this property.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from nse.brain import BrainState
from nse.brain import load as load_brain
from nse.config import VOTE_THRESHOLD
from nse.lenses.base import Direction, LensVerdict
from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

COUNCIL_COLLECTION = "nse_council_decisions"

#: volume_oi's TRAIN 67th percentile. See the module docstring.
COUNCIL_MIN_LEAD_CONFIDENCE: float = 0.414

#: An objector needs at least this much conviction to be worth listening to.
#: Below it a dissenting lens is noise, and letting noise veto trades is just a
#: slower way of not trading.
COUNCIL_MIN_OBJECTION_CONFIDENCE: float = 0.30

#: Is deliberation allowed to CHANGE the traded decision, or only annotate it?
#:
#: TRUE, and the justification is structural rather than statistical.
#:
#: The measurement never cleared deliberation: adding it moved VALIDATE
#: +3.70 -> +4.14 bps, but against a random subset of its own parent of the same
#: size it scored p=0.2125 — indistinguishable from simply trading fewer bars.
#: On that evidence alone it would stay in shadow.
#:
#: What changes the calculus is an invariant, not a p-value. EVERY lens's round-1
#: logic is MONOTONE DOWNWARD: it may cut its own confidence, defer, or hold, and
#: it can never raise confidence or flip direction. Verified on 502 real
#: lens-decisions — 324 held, 115 cut, 63 deferred, 0 raised, 0 flipped — and
#: enforced by `assert_deliberation_monotone()` so a future lens cannot quietly
#: break it.
#:
#: A monotone-downward mechanism can only ever make the council trade LESS. Being
#: wrong about it costs missed trades, not losses, which is the one direction in
#: which an unproven mechanism is safe to run live. Contrast the adaptive quorum,
#: which is also unproven but gates in both directions and therefore stays in
#: shadow.
#:
#: If live attribution shows deliberation is costing more in missed trades than
#: it saves, set this False.
COUNCIL_DELIBERATION_BINDING: bool = True

#: The conviction gate is the one thing here that self-tunes. It is expressed as
#: a TARGET PERCENTILE of the lead lens's own recent confidence distribution —
#: "trade the top third" — rather than as the value 0.414, which is merely what
#: the top third worked out to on TRAIN.
#:
#: This is the difference that makes it survive a regime change. An absolute cut
#: means a different thing in every regime: the ATR gate in section 3.15 was
#: fitted on TRAIN, kept 11% of TRAIN, and kept 6% of VALIDATE. A percentile
#: keeps the same fraction of the tape by construction. See nse/selftune.py.
COUNCIL_LEAD_TARGET_PERCENTILE: float = 0.67

#: Is the ADAPTIVE QUORUM allowed to change the traded decision?
#:
#: False. The operator's rule — demand more agreement when the tape is hard,
#: let one good lens through when it is easy — measured POSITIVE IN BOTH SPLITS
#: (+0.67 TRAIN, +1.27 VALIDATE bps against the conviction-gated arm) and
#: reached significance in NEITHER (p=0.145, p=0.153).
#:
#: Consistent-but-not-significant is exactly the state that deserves shadow
#: rather than a coin flip: it is too plausible to discard and too weak to fund.
#: It runs, it is journaled, the dashboard shows what it WOULD have done, and it
#: moves no capital until live attribution separates it from noise.
#:
#: Note the near-miss it protects against: `uncontested only` scored VALIDATE
#: +10.52 bps at bootstrap p=0.0010 and was worth NOTHING on TRAIN (p=0.62).
#: One spectacular split is what an artefact looks like.
COUNCIL_ADAPTIVE_QUORUM_BINDING: bool = False

#: Below this regime percentile the tape is "hard" — chop, where the lead lens
#: measured worst (ATR bottom tercile: TRAIN +0.79, VALIDATE -1.35).
COUNCIL_HARD_REGIME_PCT: float = 0.33

#: In a hard regime, this fraction of the roster must have been able to READ the
#: snapshot before the council acts on it. A snapshot most lenses abstain on is,
#: by direct evidence, hard to read.
COUNCIL_HARD_MIN_READABILITY: float = 0.75

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

    #: What the shadow mechanisms WOULD have done. Journaled on every decision
    #: so their live track record accrues from day one — a mechanism cannot earn
    #: its way out of shadow if nobody recorded what it advised while in it.
    shadow: dict = field(default_factory=dict)

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
            "shadow": self.shadow,
            "n_spoke": self.n_spoke, "n_deferred": self.n_deferred,
            "n_revised": self.n_revised, "n_abstained": self.n_abstained,
        }

    def transcript(self) -> str:
        """The conversation, for the dashboard's left panel.

        Times render in IST. `ts` is tz-aware UTC, and formatting it raw printed
        05:07 for a decision taken at 10:37 IST — a five-hour discrepancy on the
        one surface a human uses to check the bot against their own screen.
        """
        local = self.ts.astimezone(_IST) if self.ts.tzinfo else self.ts
        head = (f"[{local:%H:%M} IST] {self.direction.label} "
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
                 min_lead_confidence: Optional[float] = None,
                 deliberation_binding: bool = COUNCIL_DELIBERATION_BINDING,
                 adaptive_quorum_binding: bool = COUNCIL_ADAPTIVE_QUORUM_BINDING,
                 self_tune: bool = True):
        self.lenses = list(lenses)
        self.threshold = threshold
        self.deliberation_binding = deliberation_binding
        self.adaptive_quorum_binding = adaptive_quorum_binding
        self._regime_pct: Optional[float] = None
        self.brains: dict = brains if brains is not None else {
            l.name: load_brain(l.name, backtestable=getattr(l, "backtestable", True))
            for l in self.lenses}

        # The conviction gate is self-tuning: it tracks a PERCENTILE of the lead
        # lens's own recent confidence, not a frozen number. An explicit
        # `min_lead_confidence` overrides that and pins it — used by the
        # measurement harness, where a moving threshold would make two arms
        # incomparable.
        if min_lead_confidence is not None:
            self.gate = None
            self.min_lead_confidence = float(min_lead_confidence)
        elif self_tune:
            from nse.selftune import load as load_tunable
            self.gate = load_tunable("council", "min_lead_confidence",
                                     bootstrap=COUNCIL_MIN_LEAD_CONFIDENCE,
                                     target_percentile=COUNCIL_LEAD_TARGET_PERCENTILE)
            self.min_lead_confidence = self.gate.value
        else:
            self.gate = None
            self.min_lead_confidence = COUNCIL_MIN_LEAD_CONFIDENCE

    def retune_gate(self, confidences_by_session: dict, as_of,
                    persist: bool = False) -> Optional[str]:
        """Move the conviction gate to its target percentile of recent history.

        Call this ONCE per session, out of market hours. Retuning intraday would
        mean two snapshots an hour apart were judged against different bars,
        which makes the session's decisions incomparable with each other and the
        day's attribution meaningless.

        PERSISTENCE IS OPT-IN, and that default is not timidity.

        This method used to save unconditionally. A verification run that fed it
        `np.random.uniform(0, 1)` — checking only that the shrinkage and band
        logic worked — therefore wrote a gate of 0.5615 into the PRODUCTION
        cluster, fitted to random numbers. The live council then rejected trades
        against a "measured floor" that was noise, and said so in its transcript
        with a plausible-looking number.

        A function that mutates shared production state as a side effect of
        being called is a function that will eventually be called by a test.
        Callers that mean it pass persist=True.
        """
        if self.gate is None:
            return None
        from nse.selftune import recalibrate_lens, save as save_tunable
        self.gate, note = recalibrate_lens("council", self.gate,
                                           confidences_by_session, as_of)
        self.min_lead_confidence = self.gate.value
        if persist:
            save_tunable("council", self.gate)
        logger.info("council gate retuned%s: %s",
                    "" if persist else " (NOT persisted)", note)
        return note

    def weight_of(self, name: str) -> float:
        b = self.brains.get(name)
        return float(b.effective_weight()) if isinstance(b, BrainState) else 0.0

    # ── how hard is this tape? ───────────────────────────────────────────────
    def difficulty(self, round1: list,
                   regime_pct: Optional[float] = None) -> dict:
        """Read the council's own difficulty off the verdicts it just produced.

        `readability` is the signal worth having: a snapshot most lenses ABSTAIN
        on is, by direct evidence, hard to read. That is the council measuring
        its own footing rather than being told about it by an external indicator.

        `regime_pct` is a CAUSAL ROLLING PERCENTILE supplied by the caller (where
        today's realised vol sits in the trailing distribution), or None when
        there is not enough history. Never an absolute volatility level — see
        COUNCIL_HARD_REGIME_PCT.
        """
        total = len(round1) or 1
        spoke = [v for v in round1
                 if v.speaks and v.direction != Direction.NEUTRAL]
        readability = len(spoke) / total
        if len(spoke) >= 2:
            dirs = [int(v.direction) for v in spoke]
            contest = 1.0 - abs(sum(dirs)) / len(dirs)   # 0 unanimous, 1 split
        else:
            contest = 0.0
        hard = ((regime_pct is not None and regime_pct < COUNCIL_HARD_REGIME_PCT)
                or contest > 0.0)
        return {"regime_pct": regime_pct, "readability": round(readability, 4),
                "contest": round(contest, 4), "hard": hard}

    def quorum_ok(self, diff: dict) -> tuple[bool, str]:
        """The operator's adaptive rule: easy tape needs one lens, hard needs more.

        Returns (allowed, why) and is ADVISORY unless
        COUNCIL_ADAPTIVE_QUORUM_BINDING — see that flag for the measurement.
        """
        if not diff["hard"]:
            return True, "tape is easy — the lead alone is enough"
        if diff["readability"] >= COUNCIL_HARD_MIN_READABILITY:
            return True, (f"hard tape but {diff['readability']:.0%} of the roster "
                          f"could read it")
        return False, (f"hard tape and only {diff['readability']:.0%} of the "
                       f"roster could read it "
                       f"(contest {diff['contest']:.2f}, "
                       f"regime {diff['regime_pct']})")

    # ── the three rounds ─────────────────────────────────────────────────────
    def deliberate(self, snap: MarketSnapshot, journal=None,
                   regime_pct: Optional[float] = None) -> CouncilDecision:
        round0 = [l.safe_evaluate(snap) for l in self.lenses]
        by_name = {v.lens: v for v in round0}
        self._regime_pct = regime_pct

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

        # The adaptive quorum. Advisory unless the flag says otherwise, but
        # journaled either way so its live record accrues while it is in shadow.
        diff = self.difficulty(deciding, getattr(self, "_regime_pct", None))
        allowed, why = self.quorum_ok(diff)

        reason = f"{lead.lens} leads {direction.label} at {lead.confidence:.3f}"
        if objections:
            reason += f", {len(objections)} objection(s) noted and discounted"

        if not allowed and self.adaptive_quorum_binding:
            d = build(direction, signed, False, f"adaptive quorum: {why}",
                      lead=lead.lens, objections=objections)
            d.shadow = {"difficulty": diff, "quorum_allowed": False,
                        "quorum_why": why, "quorum_binding": True}
            return d

        d = build(direction, signed, True, reason, lead=lead.lens,
                  objections=objections)
        d.shadow = {"difficulty": diff, "quorum_allowed": allowed,
                    "quorum_why": why,
                    "quorum_binding": self.adaptive_quorum_binding,
                    # What the shadow rule WOULD have changed. Explicit, so the
                    # comparison is a stored fact rather than a later
                    # reconstruction from two columns nobody kept.
                    "quorum_would_have_blocked": (not allowed)}
        return d


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


def assert_deliberation_monotone(lenses: Sequence, snapshots: Sequence,
                                 journal=None) -> dict:
    """Round 1 must only ever LOWER conviction. Verify it, do not assume it.

    `COUNCIL_DELIBERATION_BINDING` is True on the strength of this property, not
    on a p-value: deliberation never cleared its statistical control, but a
    mechanism that can only reduce trading is safe to run unproven, because
    being wrong costs missed trades rather than losses.

    The moment some future lens raises its own confidence after hearing a peer,
    that argument evaporates and deliberation becomes an unproven mechanism that
    can ADD positions. This function is what stops that happening silently.

    Raises AssertionError listing every violation. Returns the tally.
    """
    tally = {"checked": 0, "held": 0, "cut": 0, "deferred": 0,
             "raised": 0, "flipped": 0}
    violations: list[str] = []

    for snap in snapshots:
        round0 = {l.name: l.safe_evaluate(snap) for l in lenses}
        for lens in lenses:
            own = round0[lens.name]
            if own.abstained:
                continue
            peers = {k: v for k, v in round0.items() if k != lens.name}
            new = lens.safe_deliberate(snap, own, peers, journal)
            tally["checked"] += 1

            if new.deferred:
                tally["deferred"] += 1
                continue
            if new.confidence > own.confidence + 1e-12:
                tally["raised"] += 1
                violations.append(
                    f"{lens.name} RAISED {own.confidence:.4f} -> "
                    f"{new.confidence:.4f} at {snap.ts}")
            elif new.confidence < own.confidence - 1e-12:
                tally["cut"] += 1
            else:
                tally["held"] += 1
            if new.direction != own.direction:
                tally["flipped"] += 1
                violations.append(
                    f"{lens.name} FLIPPED {own.direction.label} -> "
                    f"{new.direction.label} at {snap.ts}")

    assert not violations, (
        "deliberation is no longer monotone-downward, so "
        "COUNCIL_DELIBERATION_BINDING is no longer safe:\n  "
        + "\n  ".join(violations[:20]))
    return tally
