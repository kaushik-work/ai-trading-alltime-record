"""The end-of-day journal: what happened, and what the council should remember.

Written once after the close, read the next morning by every lens during
deliberation. It is the council's memory — the thing that lets a lens say "I
misread this regime yesterday" instead of starting each session amnesiac.

THE ONE RULE THAT MATTERS: A JOURNAL IS FOR STRICTLY EARLIER SESSIONS

`for_session(d)` returns the journal of the last session BEFORE `d`, never `d`
itself. Handing a session its own end-of-day summary is lookahead of the purest
kind — the council would deliberate using the outcome of the very trades it is
deciding whether to take, and the backtest would be spectacular and worthless.
The load path enforces this rather than trusting callers, because this is a
mistake that reads as correct in every code review.

WHAT GOES IN, AND WHAT DELIBERATELY DOES NOT

In: per-lens hit rate and mean outcome for the session, the regime the session
traded in, which lenses agreed and what happened when they did, and the running
notes a lens chose to leave itself.

Not in: any instruction to trade differently tomorrow. The journal RECORDS; the
lenses decide what to do about it. Keeping inference out of the record means a
journal entry stays true even after the strategy that read it is retired — and
it stops the journal quietly becoming a second, unmeasured strategy.

The regime fields are coarse on purpose (terciles, not values). A lens matching
on "yesterday was high-ATR" generalises; a lens matching on "yesterday's ATR was
0.4713%" is fitting one day of noise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

JOURNAL_COLLECTION = "nse_day_journal"

#: Sessions of history a lens may consider. Beyond this the market has moved on.
JOURNAL_LOOKBACK_SESSIONS = 10


@dataclass
class LensDay:
    """One lens's session, as the journal records it."""

    lens: str
    n_verdicts: int = 0
    n_abstained: int = 0
    n_deferred: int = 0
    n_revised: int = 0
    long_frac: float = 0.0
    mean_outcome_bps: Optional[float] = None   # None until outcomes are known
    hit_rate: Optional[float] = None
    note: str = ""

    @property
    def was_read(self) -> bool:
        """Did this lens actually see the session, or just fail all day?"""
        return self.n_verdicts > self.n_abstained

    def to_doc(self) -> dict:
        return {
            "lens": self.lens, "n_verdicts": self.n_verdicts,
            "n_abstained": self.n_abstained, "n_deferred": self.n_deferred,
            "n_revised": self.n_revised, "long_frac": round(self.long_frac, 4),
            "mean_outcome_bps": self.mean_outcome_bps,
            "hit_rate": self.hit_rate, "note": self.note,
        }

    @classmethod
    def from_doc(cls, d: dict) -> "LensDay":
        return cls(**{k: d.get(k, getattr(cls, k, None))
                      for k in ("lens", "n_verdicts", "n_abstained", "n_deferred",
                                "n_revised", "long_frac", "mean_outcome_bps",
                                "hit_rate", "note")})


@dataclass
class DayJournal:
    """One session, closed and written up."""

    session: date
    symbol: str = "NIFTY"

    # Regime, coarse on purpose — see the module docstring.
    atr_regime: str = "unknown"        # low | mid | high
    iv_regime: str = "unknown"         # low | mid | high
    trend: str = "unknown"             # up | down | range
    open_spot: Optional[float] = None
    close_spot: Optional[float] = None

    lenses: dict = field(default_factory=dict)     # name -> LensDay

    n_decisions: int = 0
    n_executed: int = 0
    realised_pnl: Optional[float] = None
    notes: str = ""

    @property
    def day_return_bps(self) -> Optional[float]:
        if not self.open_spot or not self.close_spot:
            return None
        return (self.close_spot - self.open_spot) / self.open_spot * 10_000

    def lens(self, name: str) -> Optional[LensDay]:
        return self.lenses.get(name)

    def struggled(self, name: str, threshold_bps: float = 0.0) -> bool:
        """Did this lens lose money yesterday? Unknown outcome -> False.

        A lens with no measured outcome must not be treated as having failed —
        that would let one un-scored session bench a working lens.
        """
        d = self.lenses.get(name)
        if d is None or d.mean_outcome_bps is None or not d.was_read:
            return False
        return d.mean_outcome_bps < threshold_bps

    def to_doc(self) -> dict:
        return {
            "session": self.session.isoformat(), "symbol": self.symbol,
            "atr_regime": self.atr_regime, "iv_regime": self.iv_regime,
            "trend": self.trend, "open_spot": self.open_spot,
            "close_spot": self.close_spot,
            "day_return_bps": self.day_return_bps,
            "lenses": {k: v.to_doc() for k, v in self.lenses.items()},
            "n_decisions": self.n_decisions, "n_executed": self.n_executed,
            "realised_pnl": self.realised_pnl, "notes": self.notes,
            "written_at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def from_doc(cls, d: dict) -> "DayJournal":
        return cls(
            session=date.fromisoformat(d["session"]),
            symbol=d.get("symbol", "NIFTY"),
            atr_regime=d.get("atr_regime", "unknown"),
            iv_regime=d.get("iv_regime", "unknown"),
            trend=d.get("trend", "unknown"),
            open_spot=d.get("open_spot"), close_spot=d.get("close_spot"),
            lenses={k: LensDay.from_doc(v) for k, v in (d.get("lenses") or {}).items()},
            n_decisions=d.get("n_decisions", 0),
            n_executed=d.get("n_executed", 0),
            realised_pnl=d.get("realised_pnl"),
            notes=d.get("notes", ""),
        )

    def summary(self) -> str:
        head = (f"{self.session} {self.symbol}  {self.atr_regime}-ATR "
                f"{self.iv_regime}-IV {self.trend}")
        if self.day_return_bps is not None:
            head += f"  index {self.day_return_bps:+.0f}bps"
        head += f"  decisions {self.n_decisions} executed {self.n_executed}"
        rows = [head]
        for name, d in sorted(self.lenses.items()):
            out = "  n/a" if d.mean_outcome_bps is None else f"{d.mean_outcome_bps:+.2f}bps"
            rows.append(f"    {name:<11} read {d.n_verdicts - d.n_abstained:>3}"
                        f"/{d.n_verdicts:<3} deferred {d.n_deferred:>3} "
                        f"revised {d.n_revised:>3}  {out}"
                        + (f"  {d.note}" if d.note else ""))
        return "\n".join(rows)


# ── building one ─────────────────────────────────────────────────────────────
def _tercile_label(value: Optional[float], lo: float, hi: float) -> str:
    if value is None:
        return "unknown"
    return "low" if value <= lo else "high" if value >= hi else "mid"


def build(session: date, verdicts_by_lens: dict, *, symbol: str = "NIFTY",
          atr_pct: Optional[float] = None, atm_iv: Optional[float] = None,
          atr_terciles: tuple = (0.05, 0.15), iv_terciles: tuple = (10.0, 16.0),
          open_spot: Optional[float] = None, close_spot: Optional[float] = None,
          outcomes_by_lens: Optional[dict] = None,
          n_decisions: int = 0, n_executed: int = 0,
          realised_pnl: Optional[float] = None, notes: str = "") -> DayJournal:
    """Assemble a journal from a session's verdicts.

    `verdicts_by_lens` maps lens name -> the session's list of LensVerdict.
    `outcomes_by_lens` maps lens name -> list of signed forward returns in bps,
    aligned to the verdicts that had one; omit it when outcomes are not yet
    known and the journal records structure only.
    """
    trend = "range"
    if open_spot and close_spot:
        move = (close_spot - open_spot) / open_spot * 10_000
        trend = "up" if move > 25 else "down" if move < -25 else "range"

    j = DayJournal(
        session=session, symbol=symbol,
        atr_regime=_tercile_label(atr_pct, *atr_terciles),
        iv_regime=_tercile_label(atm_iv, *iv_terciles),
        trend=trend, open_spot=open_spot, close_spot=close_spot,
        n_decisions=n_decisions, n_executed=n_executed,
        realised_pnl=realised_pnl, notes=notes,
    )

    for name, vs in verdicts_by_lens.items():
        vs = list(vs)
        spoke = [v for v in vs if not v.abstained]
        longs = [v for v in spoke if int(v.direction) > 0]
        day = LensDay(
            lens=name,
            n_verdicts=len(vs),
            n_abstained=sum(1 for v in vs if v.abstained),
            n_deferred=sum(1 for v in vs if getattr(v, "deferred", False)),
            n_revised=sum(1 for v in vs if getattr(v, "revised", False)),
            long_frac=(len(longs) / len(spoke)) if spoke else 0.0,
        )
        outs = (outcomes_by_lens or {}).get(name)
        if outs:
            outs = [o for o in outs if o is not None]
            if outs:
                day.mean_outcome_bps = round(sum(outs) / len(outs), 4)
                day.hit_rate = round(sum(1 for o in outs if o > 0) / len(outs), 4)
        j.lenses[name] = day
    return j


# ── persistence ──────────────────────────────────────────────────────────────
def save(j: DayJournal) -> bool:
    """Persist one session's journal. Never raises."""
    try:
        from core.mongo import get_db
        db = get_db()
        if db is None:
            return False
        db[JOURNAL_COLLECTION].update_one(
            {"session": j.session.isoformat(), "symbol": j.symbol},
            {"$set": j.to_doc()}, upsert=True)
        return True
    except Exception as e:
        logger.warning("journal save failed for %s: %s", j.session, e)
        return False


def for_session(session: date, symbol: str = "NIFTY") -> Optional[DayJournal]:
    """The journal of the last session STRICTLY BEFORE `session`.

    The `$lt` is the whole point — see the module docstring. Returns None when
    there is no earlier journal, which is the correct state on the first
    session and must be handled by every caller.
    """
    try:
        from core.mongo import get_db
        db = get_db()
        if db is None:
            return None
        doc = db[JOURNAL_COLLECTION].find_one(
            {"symbol": symbol, "session": {"$lt": session.isoformat()}},
            {"_id": 0}, sort=[("session", -1)])
        return DayJournal.from_doc(doc) if doc else None
    except Exception as e:
        logger.warning("journal load failed for %s: %s", session, e)
        return None


def recent(before: date, symbol: str = "NIFTY",
           limit: int = JOURNAL_LOOKBACK_SESSIONS) -> list[DayJournal]:
    """Up to `limit` journals strictly before `before`, newest first."""
    try:
        from core.mongo import get_db
        db = get_db()
        if db is None:
            return []
        cur = db[JOURNAL_COLLECTION].find(
            {"symbol": symbol, "session": {"$lt": before.isoformat()}},
            {"_id": 0}, sort=[("session", -1)]).limit(limit)
        return [DayJournal.from_doc(d) for d in cur]
    except Exception as e:
        logger.warning("journal history load failed before %s: %s", before, e)
        return []


class ReplayJournals:
    """In-memory journal store for backtests, with the same `$lt` guarantee.

    A replay has no Mongo, but it must not therefore skip the journal — the
    council behaves differently with one, so a backtest without journals is not
    measuring the system that would trade. Sessions are added as they complete
    and only earlier ones are ever returned.
    """

    def __init__(self) -> None:
        self._by_session: dict[date, DayJournal] = {}

    def add(self, j: DayJournal) -> None:
        self._by_session[j.session] = j

    def for_session(self, session: date) -> Optional[DayJournal]:
        earlier = [d for d in self._by_session if d < session]
        return self._by_session[max(earlier)] if earlier else None

    def recent(self, before: date,
               limit: int = JOURNAL_LOOKBACK_SESSIONS) -> list[DayJournal]:
        earlier = sorted((d for d in self._by_session if d < before), reverse=True)
        return [self._by_session[d] for d in earlier[:limit]]

    def __len__(self) -> int:
        return len(self._by_session)
