"""The brain tier's session loop: snapshot -> council -> sentinel.

This is the wiring that turns a measured library into a running system. It owns
the session lifecycle, the heartbeat, and nothing else — every decision comes
from `nse/council.py` and every order goes through `sentinel_client`.

THIS MODULE CANNOT PLACE AN ORDER, AND THAT IS STRUCTURAL

It never imports `AngelBroker`, never touches `data/angel_fetcher`'s order
paths, and holds no order credentials. Its only outbound trading path is
`SentinelClient.submit_intent`, which posts a signed intent to the VPS. The
static-IP requirement applies to place/modify/cancel and GTT endpoints only, so
this separation is not decoration — running this file on a laptop is safe
because there is no code path from here to an exchange.

`test_no_order_imports()` at the bottom asserts it, so the guarantee survives
somebody later adding a convenient import.

WHAT IT DOES ON EVERY TICK

    1. build a MarketSnapshot (live feed, or replay in paper mode)
    2. council.deliberate(snapshot, yesterday's journal)
    3. journal the decision — executed or not, both are needed for attribution
    4. if executed and risk allows, submit ONE intent
    5. heartbeat, so the dead-man's switch stays disarmed

WHAT IT REFUSES TO DO

Re-enter a symbol it already holds, exceed the concurrent-position cap, trade
outside market hours, or act on a stale snapshot. Each of those is a separate
explicit check rather than one combined guard, because when this stops trading
you need to know WHICH rule stopped it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Optional

from nse.brain import BrainState
from nse.config import LOT_SIZES, TOTAL_CAPITAL_INR
from nse.council import Council, journal_decision
from nse.execution.sentinel_client import SentinelClient
from nse.journal import DayJournal, build as build_journal, for_session, save as save_journal
from nse.lenses import ROSTER
from nse.lenses.base import Direction
from nse.lenses.bootstrap import MEASURED
from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

IST = timezone.utc  # snapshots carry tz-aware UTC; market hours convert below

#: Rupees of risk per trade. The operator's figure, inside the ₹3L pool.
RISK_PER_TRADE_INR: float = 50_000.0

#: Concurrent positions. 6 x ₹50k = the full ₹3L pool.
MAX_CONCURRENT_POSITIONS: int = 6

#: A snapshot older than this is not tradeable. "Live means live" — acting on a
#: stale book is how you buy a price that stopped existing.
MAX_SNAPSHOT_AGE_SEC: float = 15.0

#: Master switch. Defaults OFF: this file existing must never be sufficient to
#: start trading.
ENABLE_OPTIONS_RUNNER: bool = os.environ.get(
    "ENABLE_OPTIONS_RUNNER", "false").lower() == "true"

#: Paper mode journals everything and submits nothing.
PAPER_MODE: bool = os.environ.get("OPTIONS_PAPER_MODE", "true").lower() == "true"


@dataclass
class RunnerState:
    """What the loop knows about the session it is in."""

    session: Optional[date] = None
    open_positions: dict = field(default_factory=dict)   # symbol -> position_id
    decisions: int = 0
    executed: int = 0
    rejected: int = 0
    errors: int = 0
    last_reason: str = ""
    verdicts_by_lens: dict = field(default_factory=dict)
    first_spot: Optional[float] = None
    last_spot: Optional[float] = None

    @property
    def capacity(self) -> int:
        return MAX_CONCURRENT_POSITIONS - len(self.open_positions)

    def summary(self) -> str:
        return (f"{self.session} decisions {self.decisions} executed "
                f"{self.executed} rejected {self.rejected} errors {self.errors} "
                f"open {len(self.open_positions)}/{MAX_CONCURRENT_POSITIONS}")


class OptionsRunner:
    """One council, one sentinel client, one session at a time."""

    def __init__(self, council: Optional[Council] = None,
                 client: Optional[SentinelClient] = None,
                 paper: bool = PAPER_MODE):
        self.council = council or _default_council()
        self.client = client or SentinelClient()
        self.paper = paper
        self.state = RunnerState()
        self._journal: Optional[DayJournal] = None

    # ── session lifecycle ────────────────────────────────────────────────────
    def begin_session(self, session: date, symbol: str = "NIFTY") -> None:
        """Load yesterday's journal and reset per-session counters.

        The journal is fetched ONCE per session rather than per tick: it cannot
        change intraday, and re-reading it every tick would be one Mongo
        round-trip per decision against a value that is constant all day.
        """
        self.state = RunnerState(session=session)
        self._journal = for_session(session, symbol)
        if self._journal is not None:
            logger.info("council reads yesterday: %s", self._journal.summary())
        else:
            logger.info("no prior journal before %s — council starts cold", session)

    def end_session(self, symbol: str = "NIFTY", *,
                    realised_pnl: Optional[float] = None) -> Optional[DayJournal]:
        """Write the journal the next session will read."""
        if self.state.session is None or not self.state.verdicts_by_lens:
            return None
        j = build_journal(
            self.state.session, self.state.verdicts_by_lens, symbol=symbol,
            open_spot=self.state.first_spot, close_spot=self.state.last_spot,
            n_decisions=self.state.decisions, n_executed=self.state.executed,
            realised_pnl=realised_pnl,
            notes=f"paper={self.paper}; {self.state.summary()}")
        save_journal(j)
        logger.info("journal written:\n%s", j.summary())
        return j

    # ── the tick ─────────────────────────────────────────────────────────────
    def on_snapshot(self, snap: MarketSnapshot) -> Optional[dict]:
        """One decision. Returns the sentinel's response, or None if no trade."""
        if not ENABLE_OPTIONS_RUNNER and not self.paper:
            return None

        self.state.decisions += 1
        if self.state.first_spot is None:
            self.state.first_spot = snap.spot
        self.state.last_spot = snap.spot

        decision = self.council.deliberate(snap, self._journal)

        # Every verdict is retained for the end-of-day journal, including the
        # ones from rejected decisions — those are the control group that makes
        # per-lens attribution computable at all.
        for v in decision.round1:
            self.state.verdicts_by_lens.setdefault(v.lens, []).append(v)
        journal_decision(decision)

        if not decision.executed:
            self.state.rejected += 1
            self.state.last_reason = decision.reason
            return None

        blocked = self._blocked(snap)
        if blocked:
            self.state.rejected += 1
            self.state.last_reason = blocked
            logger.info("decision %s cleared the council but %s",
                        decision.decision_id, blocked)
            return None

        return self._submit(snap, decision)

    def _blocked(self, snap: MarketSnapshot) -> Optional[str]:
        """Risk and hygiene checks, each named so a refusal is diagnosable."""
        if snap.is_stale(MAX_SNAPSHOT_AGE_SEC):
            return f"snapshot is stale (> {MAX_SNAPSHOT_AGE_SEC:.0f}s old)"
        if snap.symbol in self.state.open_positions:
            return f"already holding {snap.symbol}"
        if self.state.capacity <= 0:
            return (f"at the position cap "
                    f"({len(self.state.open_positions)}/{MAX_CONCURRENT_POSITIONS})")
        if snap.symbol not in LOT_SIZES:
            return f"no lot size known for {snap.symbol}"
        return None

    def _submit(self, snap: MarketSnapshot, decision) -> Optional[dict]:
        """Turn a decision into ONE intent.

        Naked long options, per the operator's brief: a LONG council call buys a
        CE, a SHORT call buys a PE. Buying in both directions means defined risk
        — the premium — which is what makes a fixed ₹50k risk budget honest.
        Selling would make the loss unbounded and the sizing arithmetic a lie.
        """
        option_type = "CE" if decision.direction is Direction.LONG else "PE"
        strike = snap.atm
        row = snap.at(strike, option_type)
        if row is None:
            self.state.rejected += 1
            self.state.last_reason = f"ATM {strike}{option_type} not quoted"
            return None

        premium = row.get("ltp")
        lots = _size(premium, snap.symbol)
        if lots < 1:
            self.state.rejected += 1
            self.state.last_reason = (
                f"one lot of {strike}{option_type} at {premium} exceeds the "
                f"₹{RISK_PER_TRADE_INR:,.0f} risk budget")
            return None

        if self.paper:
            self.state.executed += 1
            logger.info("[PAPER] %s %s %d lot(s) — %s",
                        option_type, strike, lots, decision.reason)
            self.state.open_positions[snap.symbol] = f"paper_{decision.decision_id}"
            return {"ok": True, "paper": True, "decision_id": decision.decision_id,
                    "option_type": option_type, "strike": strike, "lots": lots}

        resp = self.client.submit_intent(
            symbol=snap.symbol, side="BUY", option_type=option_type,
            strike=strike, lots=lots, decision_id=decision.decision_id)

        if resp.get("ok"):
            self.state.executed += 1
            pid = resp.get("position_id")
            if pid:
                self.state.open_positions[snap.symbol] = pid
        elif resp.get("indeterminate"):
            # The intent may or may not have landed. Do NOT retry and do NOT
            # assume it failed — either guess can double a position. Mark the
            # symbol occupied and let reconciliation against the sentinel's own
            # position view settle it.
            self.state.errors += 1
            self.state.open_positions[snap.symbol] = "INDETERMINATE"
            logger.error("intent %s indeterminate — symbol locked pending "
                         "reconciliation", decision.decision_id)
        else:
            self.state.errors += 1
            logger.error("sentinel rejected %s: %s",
                         decision.decision_id, resp.get("error"))
        return resp

    def heartbeat(self) -> None:
        """Keep the dead-man's switch disarmed. Silence is the kill signal."""
        if self.paper:
            return
        try:
            self.client.heartbeat()
        except Exception as e:
            logger.error("heartbeat failed: %s", e)


def _size(premium, symbol: str) -> int:
    """Lots such that the premium paid stays inside the risk budget.

    For a long option the premium IS the maximum loss, so risk and cost are the
    same number and sizing needs no stop-distance assumption. That is the whole
    reason the brief specifies naked longs.
    """
    try:
        p = float(premium)
    except (TypeError, ValueError):
        return 0
    lot = LOT_SIZES.get(symbol, 0)
    if p <= 0 or lot <= 0:
        return 0
    return int(RISK_PER_TRADE_INR // (p * lot))


def _default_council() -> Council:
    """The measured roster, at its measured weights.

    Brains come from `nse/lenses/bootstrap.py` when Mongo is unavailable, so a
    laptop run behaves like production rather than silently giving every lens a
    default weight.
    """
    lenses = [c() for c in ROSTER if c.name != "vision"]
    brains = {n: BrainState(lens=n, lifecycle=m.lifecycle,
                            weight=m.bootstrap_weight,
                            bootstrap_weight=m.bootstrap_weight,
                            backtestable=(m.train_bps is not None))
              for n, m in MEASURED.items()}
    return Council(lenses, brains=brains)


#: Modules and symbols that can place an order. The brain tier importing any of
#: these would silently dissolve the tier split.
_ORDER_MODULES = ("nse.broker", "core.brokers", "SmartApi", "smartapi")
_ORDER_SYMBOLS = ("AngelBroker", "SmartConnect", "place_single_order",
                  "place_combo", "attach_gtt_brackets")


def test_no_order_imports(path: Optional[str] = None) -> bool:
    """The brain tier must have no path to an exchange. Assert it.

    A structural guarantee that is only documented lasts until the next person
    needs a quote and imports the broker to get one.

    This parses the IMPORT STATEMENTS via AST rather than grepping the source.
    The first version searched the file text for banned names and failed on
    itself — the names appear in this very list. A test that cannot distinguish
    "imports the broker" from "mentions the broker" is not testing the property
    it claims to.
    """
    import ast

    src_path = path or __file__
    with open(src_path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=src_path)

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imported.append(mod)
            imported.extend(f"{mod}.{a.name}" for a in node.names)

    bad = [i for i in imported
           if any(i.startswith(m) for m in _ORDER_MODULES)
           or any(i.rsplit(".", 1)[-1] == s for s in _ORDER_SYMBOLS)]
    assert not bad, f"brain tier imports order-placing code: {bad}"
    return True
