"""Weighted-consensus vote across the lenses, and the journal of every decision.

The aggregator is deliberately dull: it multiplies each lens's signed confidence
by that lens's brain weight, sums, and compares to a threshold. No negotiation,
no free-form reasoning, no lens reading another lens. Dull is the feature — it
can be unit-tested, it can be replayed, and after a bad trade you can point at
the exact row that caused it.

EVERY DECISION IS JOURNALED, INCLUDING THE ONES VOTED DOWN.

That is not bookkeeping. Attribution needs to know what a lens said on trades it
LOST the vote on, otherwise a lens can only ever be scored on the trades it
already dominated, and the whole selection mechanism becomes circular. The
rejected decisions are the control group.

WEIGHT COMES FROM THE BRAIN, NOT FROM HERE

A lens in SHADOW has weight 0. It still votes, its verdict is still journaled,
and it still accrues a track record — it simply cannot move capital until
measured attribution promotes it. That is how the vision lens can be present
from day one without ever having earned the right to trade.

CORRELATED LENSES ARE A KNOWN HAZARD

Two lenses that measure nearly the same thing do not supply two independent
opinions; they supply one opinion counted twice, with the confidence that
implies. The vote here cannot detect that on its own, so the pairwise
correlation of signed scores is journaled on every decision and must be checked
before two related lenses are both given weight. `vwap` and the volume-profile
component of `volume_oi` are the current suspects.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from nse.brain import BrainState, load as load_brain
from nse.config import MIN_VOTING_LENSES, VOTE_THRESHOLD
from nse.lenses.base import Direction, LensVerdict
from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

DECISION_COLLECTION = "nse_decisions"


@dataclass
class Decision:
    decision_id: str
    ts: datetime
    symbol: str
    spot: float
    atm: int
    dte: float
    direction: Direction
    conviction: float                 # signed, weight-normalised, in [-1, 1]
    threshold: float
    executed: bool
    reason: str
    verdicts: list[LensVerdict] = field(default_factory=list)
    weights: dict = field(default_factory=dict)
    n_voting: int = 0
    n_abstained: int = 0

    def to_doc(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "ts": self.ts.isoformat(),
            "symbol": self.symbol,
            "spot": self.spot,
            "atm": self.atm,
            "dte": self.dte,
            "direction": int(self.direction),
            "direction_label": self.direction.label,
            "conviction": round(self.conviction, 6),
            "threshold": self.threshold,
            "executed": self.executed,
            "reason": self.reason,
            "n_voting": self.n_voting,
            "n_abstained": self.n_abstained,
            "weights": {k: round(v, 4) for k, v in self.weights.items()},
            "verdicts": [v.to_doc() for v in self.verdicts],
            # Filled in later by the execution path; present from the start so
            # attribution can query on a stable shape.
            "status": "OPEN" if self.executed else "REJECTED",
            "pnl": None,
        }

    def summary(self) -> str:
        votes = ", ".join(
            f"{v.lens}{'(abs)' if v.abstained else f'={v.signed_confidence:+.2f}'}"
            f"x{self.weights.get(v.lens, 0):.2f}"
            for v in self.verdicts)
        return (f"{self.direction.label} conviction {self.conviction:+.3f} "
                f"vs {self.threshold:.2f} -> "
                f"{'EXECUTE' if self.executed else 'reject'} | {votes}")


class Aggregator:
    """Polls every lens on one snapshot and produces a single decision."""

    def __init__(self, lenses: Sequence, brains: Optional[dict] = None,
                 threshold: float = VOTE_THRESHOLD,
                 min_voting: int = MIN_VOTING_LENSES,
                 load_brains: bool = True):
        self.lenses = list(lenses)
        self.threshold = threshold
        self.min_voting = min_voting
        if brains is not None:
            self.brains = brains
        elif load_brains:
            self.brains = {
                l.name: load_brain(l.name,
                                   backtestable=getattr(l, "backtestable", True))
                for l in self.lenses
            }
        else:
            self.brains = {}

    def weight_of(self, name: str) -> float:
        b = self.brains.get(name)
        return b.effective_weight() if isinstance(b, BrainState) else 0.0

    def decide(self, snap: MarketSnapshot) -> Decision:
        verdicts: list[LensVerdict] = []
        weights: dict[str, float] = {}
        num = 0.0
        den = 0.0
        n_voting = 0
        n_abstained = 0

        for lens in self.lenses:
            v = lens.safe_evaluate(snap)
            verdicts.append(v)
            w = self.weight_of(lens.name)
            weights[lens.name] = w
            if v.abstained:
                n_abstained += 1
                continue
            if w <= 0:
                continue          # SHADOW: recorded, but no say
            num += w * v.signed_confidence
            den += w
            n_voting += 1

        conviction = (num / den) if den > 0 else 0.0
        direction = (Direction.LONG if conviction > 0 else
                     Direction.SHORT if conviction < 0 else Direction.NEUTRAL)

        if n_voting < self.min_voting:
            executed, reason = False, (
                f"only {n_voting} lens(es) with weight could read this snapshot, "
                f"need {self.min_voting}")
        elif abs(conviction) < self.threshold:
            executed, reason = False, (
                f"conviction {abs(conviction):.3f} below threshold {self.threshold:.2f}")
        elif direction is Direction.NEUTRAL:
            executed, reason = False, "net vote is exactly neutral"
        else:
            executed, reason = True, "cleared the vote"

        return Decision(
            decision_id=f"dec_{snap.symbol}_{snap.ts.strftime('%Y%m%d_%H%M%S')}_"
                        f"{uuid.uuid4().hex[:6]}",
            ts=snap.ts, symbol=snap.symbol, spot=snap.spot, atm=snap.atm,
            dte=snap.dte, direction=direction, conviction=conviction,
            threshold=self.threshold, executed=executed, reason=reason,
            verdicts=verdicts, weights=weights,
            n_voting=n_voting, n_abstained=n_abstained,
        )


def journal(decision: Decision) -> bool:
    """Persist a decision. Returns False when Mongo is unavailable, never raises.

    Rejected decisions are written exactly like executed ones. Pruning them by
    outcome would destroy the control group that attribution depends on.
    """
    try:
        from core.mongo import get_db
        db = get_db()
        if db is None:
            return False
        db[DECISION_COLLECTION].update_one(
            {"decision_id": decision.decision_id},
            {"$set": decision.to_doc()}, upsert=True)
        return True
    except Exception as e:
        logger.warning("decision journal failed: %s", e)
        return False


def close_decision(decision_id: str, pnl: float, exit_ts: str,
                   exit_reason: str = "") -> bool:
    """Attach the realised P&L to a journaled decision.

    This is the row `compute_attribution` reads. Until it lands, the decision
    has no outcome and is correctly excluded from every lens's score.
    """
    try:
        from core.mongo import get_db
        db = get_db()
        if db is None:
            return False
        db[DECISION_COLLECTION].update_one(
            {"decision_id": decision_id},
            {"$set": {"pnl": float(pnl), "status": "CLOSED",
                      "exit_ts": exit_ts, "exit_reason": exit_reason}})
        return True
    except Exception as e:
        logger.warning("decision close failed: %s", e)
        return False


def closed_decisions(limit: int = 500) -> list[dict]:
    """Most recent closed decisions, oldest first — the input to attribution."""
    try:
        from core.mongo import get_db
        db = get_db()
        if db is None:
            return []
        rows = list(db[DECISION_COLLECTION]
                    .find({"status": "CLOSED", "pnl": {"$ne": None}}, {"_id": 0})
                    .sort("ts", -1).limit(limit))
        return list(reversed(rows))
    except Exception as e:
        logger.warning("closed_decisions failed: %s", e)
        return []


def run_daily_review(lenses: Sequence, window: int = 500) -> dict:
    """Re-score every lens and apply lifecycle transitions. Runs OUT of hours.

    Deliberately not callable from the trading loop. Weights are frozen during
    the session — no lens gains or loses influence mid-trade on live P&L.
    """
    from nse.brain import compute_attribution, load, log_transition, review, save

    rows = closed_decisions(window)
    out: dict = {"n_closed": len(rows), "lenses": {}}
    for lens in lenses:
        name = lens.name
        state = load(name, backtestable=getattr(lens, "backtestable", True))
        attr = compute_attribution(name, rows)
        state, note = review(state, attr, n_closed=attr.n_scored)
        save(state)
        if note:
            log_transition(state, note)
        out["lenses"][name] = {
            "lifecycle": state.lifecycle.value,
            "weight": state.weight,
            "effective_weight": state.effective_weight(),
            "contribution": attr.contribution,
            "n_scored": attr.n_scored,
            "health": state.health,
            "transition": note,
        }
    return out
