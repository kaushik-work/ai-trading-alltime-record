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
from nse.config import (COUNCIL_TRADE_FROM, LOT_SIZES, MIN_OPTION_PREMIUM,
                        TOTAL_CAPITAL_INR, market_close_for)
from nse.council import Council, journal_decision
from nse.execution.sentinel_client import SentinelClient
from nse.journal import DayJournal, build as build_journal, for_session, save as save_journal
from nse.lenses import ROSTER
from nse.lenses.base import Direction
from nse.lenses.bootstrap import MEASURED
from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

from datetime import timedelta as _td
_IST = timezone(_td(hours=5, minutes=30))

#: Rupees of risk per trade. The operator's figure, inside the ₹3L pool.
RISK_PER_TRADE_INR: float = 50_000.0

#: Concurrent positions. 6 x ₹50k = the full ₹3L pool.
MAX_CONCURRENT_POSITIONS: int = 6

#: A snapshot older than this is not tradeable. "Live means live" — acting on a
#: stale book is how you buy a price that stopped existing.
MAX_SNAPSHOT_AGE_SEC: float = 15.0

#: MINUTES TO HOLD BEFORE EXITING. This is not a preference — it is the horizon
#: the entry edge was MEASURED at (`hold_minutes=60` in
#: nse/backtest/options_harness.py, and the 60-minute forward return in
#: lens_harness). The measured +1.66/+1.49 bps describes what happens if you
#: enter and exit sixty minutes later. Hold longer and you are trading a
#: strategy nobody measured.
#:
#: THIS EXISTED NOWHERE BEFORE. The runner opened positions and had no exit path
#: of any kind: no horizon, no stop, no target, no GTT bracket. A live position
#: would have stayed open until expiry — at 1 DTE, very likely to zero — while
#: also blocking re-entry on that symbol forever via the "already holding"
#: guard. The dead-man's switch does not cover this: it fires when the BRAIN
#: goes dark, not when a trade goes against you.
HOLD_MINUTES: int = 60

#: Flatten everything this many minutes before the close. The measurement's
#: other exit is "session ends", and carrying a 1-DTE long option overnight is
#: a different trade from the one that was measured.
FLATTEN_BEFORE_CLOSE_MIN: int = 10

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
    #: symbol -> {"position_id", "opened_at", "strike", "option_type"}
    open_positions: dict = field(default_factory=dict)
    decisions: int = 0
    executed: int = 0
    rejected: int = 0
    errors: int = 0
    last_reason: str = ""
    #: Spot of the last snapshot we actually decided on, per symbol. Used to
    #: detect a frozen feed — see OptionsRunner._blocked.
    last_spot_decided: dict = field(default_factory=dict)
    #: False when the sentinel could not be asked what it holds.
    reconciled: bool = True
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
        #: Set from the snapshots this runner actually sees.
        self._source: str = "live"

    # ── session lifecycle ────────────────────────────────────────────────────
    def begin_session(self, session: date, symbol: str = "NIFTY") -> None:
        """Load yesterday's journal and reset per-session counters.

        The journal is fetched ONCE per session rather than per tick: it cannot
        change intraday, and re-reading it every tick would be one Mongo
        round-trip per decision against a value that is constant all day.
        """
        self.state = RunnerState(session=session)

        # Say so LOUDLY when there is no database. Every persistence path in
        # this system degrades quietly by design — the mirror must never break
        # live trading — but the sum of those quiet degradations is a council
        # that journals to nothing, never learns, and looks healthy doing it.
        # That already happened once: brain seeding wrote to a disabled mirror
        # for an entire session because the entry point never loaded .env.
        try:
            from core.mongo import get_db
            if get_db() is None:
                logger.warning(
                    "NO DATABASE: decisions, journals and brain state will not "
                    "persist. The council cannot read yesterday and attribution "
                    "cannot score anything. Set MONGODB_URL / MONGODB_DB_NAME.")
        except Exception as e:
            logger.warning("database check failed: %s", e)

        # ADOPT WHATEVER THE SENTINEL ALREADY HOLDS before deciding anything.
        #
        # open_positions lived only in memory, so every container restart wiped
        # the "already holding this symbol" guard and the council happily
        # re-entered a setup it was already in -- the same structure, ordered
        # again, once per restart. The sentinel is the only process that places
        # orders and therefore the only one that knows what actually filled, so
        # its view wins over the brain's recollection.
        self.reconcile()

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
            # Tag replay-driven runs so they can never be read back as a live
            # "yesterday". Paper mode over LIVE snapshots is still live data.
            source=self._source,
            notes=f"paper={self.paper}; {self.state.summary()}")
        save_journal(j)
        logger.info("journal written:\n%s", j.summary())
        return j

    # ── the tick ─────────────────────────────────────────────────────────────
    def on_snapshot(self, snap: MarketSnapshot) -> Optional[dict]:
        """One decision. Returns the sentinel's response, or None if no trade."""
        if not ENABLE_OPTIONS_RUNNER and not self.paper:
            return None

        # Exits first. A stale position must not block the next entry, and a
        # position past its horizon is already outside the measured strategy.
        self.manage_exits(snap)

        self.state.decisions += 1
        self._source = getattr(snap, "source", "live")
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

    def reconcile(self) -> int:
        """Replace the in-memory position book with the sentinel's."""
        if self.paper:
            return len(self.state.open_positions)
        resp = self.client.positions()
        if resp.get("indeterminate") or resp.get("error"):
            # Could not ask. Keep what we have and REFUSE to widen the book --
            # trading blind on an unknown position count is how you end up
            # doubled up.
            logger.error("reconcile failed (%s) - keeping the in-memory book "
                         "and blocking new entries", resp.get("error"))
            self.state.reconciled = False
            return len(self.state.open_positions)

        book: dict = {}
        for p in resp.get("positions", []):
            sym = p.get("symbol")
            if not sym:
                continue
            opened = p.get("opened_at")
            opened_dt = None
            if isinstance(opened, str):
                try:
                    opened_dt = datetime.fromisoformat(opened)
                except ValueError:
                    opened_dt = None
            book[sym] = {
                "position_id": p.get("position_id"),
                # An adopted position with no parseable timestamp is treated as
                # opened NOW, so it gets a full horizon rather than being
                # closed instantly by a parse failure.
                "opened_at": opened_dt or datetime.now(timezone.utc),
                "strike": p.get("strike"),
                "option_type": p.get("option_type"),
                "symbol": sym,
                "lots": p.get("lots"),
                "entry_premium": p.get("entry_premium"),
                "decision_id": p.get("decision_id"),
            }
        if set(book) != set(self.state.open_positions):
            logger.warning("reconciled: sentinel holds %d position(s) %s",
                           len(book), list(book))
        self.state.open_positions = book
        self.state.reconciled = True
        return len(book)

    def record_outcome(self, pos: dict, exit_premium, reason: str) -> None:
        """Write what the trade was WORTH back onto its decision.

        Without this the brains never learn. `compute_attribution` scores a
        lens over "decision documents that resulted in a filled, closed
        position, each carrying the realised pnl" -- and nothing wrote that
        field, so every brain sat at n_closed=0 forever. Promotion needs 30
        closed trades; at zero, no lens could ever be promoted OR suspended and
        the whole lifecycle was decorative.

        Recorded on the DECISION rather than only the position, because
        attribution needs what every lens said at entry -- including the ones
        that were overruled. Those are the control group.
        """
        did = pos.get("decision_id")
        entry = pos.get("entry_premium")
        if not did or entry is None or exit_premium is None:
            logger.warning("no outcome recorded for %s: decision=%s entry=%s "
                           "exit=%s", pos.get("position_id"), did, entry,
                           exit_premium)
            return
        lot = LOT_SIZES.get(pos.get("symbol") or "", 0)
        qty = int(pos.get("lots") or 0) * lot
        pnl = (float(exit_premium) - float(entry)) * qty
        try:
            from core.mongo import get_db
            from nse.council import COUNCIL_COLLECTION
            db = get_db()
            if db is None:
                return
            db[COUNCIL_COLLECTION].update_one(
                {"decision_id": did},
                {"$set": {"status": "CLOSED",
                          "entry_premium": float(entry),
                          "exit_premium": float(exit_premium),
                          "qty": qty, "pnl": round(pnl, 2),
                          "exit_reason": reason,
                          "closed_at": datetime.now(timezone.utc).isoformat()}})
            logger.info("outcome %s: entry %.2f exit %.2f qty %d pnl %+.0f (%s)",
                        did, float(entry), float(exit_premium), qty, pnl, reason)
        except Exception as e:
            logger.error("could not record outcome for %s: %s", did, e)

    def manage_exits(self, snap: MarketSnapshot) -> list:
        """Close anything past its measured horizon, or near the close.

        Runs BEFORE the entry check on every tick, for two reasons: a position
        that should already be gone must not keep blocking re-entry, and an exit
        is always more urgent than an entry.

        The horizon is the one the edge was measured at. Exiting late is not a
        smaller version of the strategy, it is a different one — the measured
        number says nothing about minute 61.
        """
        closed = []
        if not self.state.open_positions:
            return closed

        now = snap.ts
        # A PERPETUAL HAS NO SESSION CLOSE, so there is nothing to flatten
        # before. Without this, market_close_for() falls back to the NSE close
        # and every crypto position would be squared off at 15:30 IST daily for
        # no reason — the horizon exit is the only exit a perp needs.
        if getattr(snap, "is_perp", False):
            mins_to_close = 10 ** 6
        else:
            close_t = market_close_for(snap.symbol, now.astimezone(_IST).date())
            mins_to_close = ((close_t.hour * 60 + close_t.minute)
                             - (now.astimezone(_IST).hour * 60
                                + now.astimezone(_IST).minute))

        for symbol, pos in list(self.state.open_positions.items()):
            opened = pos.get("opened_at")
            age_min = ((now - opened).total_seconds() / 60.0) if opened else 0.0

            reason = None
            if mins_to_close <= FLATTEN_BEFORE_CLOSE_MIN:
                reason = f"session close in {mins_to_close}m"
            elif opened and age_min >= HOLD_MINUTES:
                reason = f"{HOLD_MINUTES}m measured horizon reached ({age_min:.0f}m)"

            if not reason:
                continue

            pid = pos.get("position_id")
            exit_px = None
            row = snap.at(pos.get("strike"), pos.get("option_type") or "")
            if row is not None:
                px = row.get("ltp")
                try:
                    exit_px = float(px) if px is not None and float(px) > 0 else None
                except (TypeError, ValueError):
                    exit_px = None

            if self.paper:
                logger.info("[PAPER] CLOSE %s %s - %s", symbol, pid, reason)
                self.record_outcome(pos, exit_px, reason)
                self.state.open_positions.pop(symbol, None)
                closed.append({"symbol": symbol, "reason": reason, "paper": True})
                continue

            resp = self.client.close_position(pid, reason=reason)
            if resp.get("ok"):
                logger.info("CLOSED %s %s - %s", symbol, pid, reason)
                self.record_outcome(pos, exit_px, reason)
                self.state.open_positions.pop(symbol, None)
            elif resp.get("indeterminate"):
                # Do NOT drop it from state on an indeterminate close. Forgetting
                # a position we may still hold is how a "flat" book turns out to
                # be short one leg.
                self.state.errors += 1
                logger.error("close of %s indeterminate — keeping it in state "
                             "pending reconciliation", pid)
            else:
                self.state.errors += 1
                logger.error("close of %s REJECTED: %s — position still open",
                             pid, resp.get("error"))
            closed.append({"symbol": symbol, "reason": reason,
                           "ok": bool(resp.get("ok"))})
        return closed

    def _blocked(self, snap: MarketSnapshot) -> Optional[str]:
        """Risk and hygiene checks, each named so a refusal is diagnosable."""
        if snap.is_stale(MAX_SNAPSHOT_AGE_SEC):
            return f"snapshot is stale (> {MAX_SNAPSHOT_AGE_SEC:.0f}s old)"
        # Price discovery is not price. See COUNCIL_TRADE_FROM.
        # The 09:30 gate exists because OPENING SPREADS are outside the measured
        # regime. A perp has no open, so there is no such window to avoid.
        ist_now = snap.ts.astimezone(_IST).time()
        if not getattr(snap, "is_perp", False) and ist_now < COUNCIL_TRADE_FROM:
            return (f"{ist_now:%H:%M} IST — holding until "
                    f"{COUNCIL_TRADE_FROM:%H:%M} while opening spreads settle")

        # A FROZEN FEED IS NOT A SIGNAL, IT IS THE ABSENCE OF ONE.
        #
        # On 2026-08-10 the cash index stopped ticking at 15:30 while NFO stayed
        # open until 15:40. build_live kept returning snapshots stamped
        # `datetime.now()` — so they always looked fresh — carrying a spot
        # frozen at 24583.8. The council re-derived the SAME verdict from the
        # SAME numbers once a minute and logged ten identical
        # "EXECUTE SHORT -0.879 spot 24583.8" decisions in ten minutes. Armed,
        # that is ten orders on one setup.
        #
        # is_stale() could not catch it: it compares the chain's exch_feed_time
        # against the snapshot's own ts, and both keep advancing when the
        # OPTIONS feed is alive but the UNDERLYING has stopped moving.
        #
        # Identical spot means no new information since the last decision.
        # Whatever the lenses conclude, they concluded it already.
        prev = self.state.last_spot_decided.get(snap.symbol)
        if prev is not None and abs(float(snap.spot) - float(prev)) < 1e-9:
            return (f"spot has not moved since the last decision "
                    f"({snap.spot:.2f}) — the feed is frozen, not signalling")

        if not self.state.reconciled:
            return ("position book is unreconciled - refusing to open anything "
                    "until the sentinel can be reached")
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
        self.state.last_spot_decided[snap.symbol] = float(snap.spot)
        option_type = "CE" if decision.direction is Direction.LONG else "PE"
        strike = snap.atm
        row = snap.at(strike, option_type)
        if row is None:
            self.state.rejected += 1
            self.state.last_reason = f"ATM {strike}{option_type} not quoted"
            return None

        premium = row.get("ltp")

        # Reject cheap contracts. Percentage spread explodes as premium falls,
        # and a Rs 5 option quoted 4.75/5.25 carries a 5% half-spread against a
        # 0.70% break-even. See MIN_OPTION_PREMIUM.
        floor = MIN_OPTION_PREMIUM.get(snap.symbol, 0.0)
        try:
            prem_f = float(premium)
        except (TypeError, ValueError):
            prem_f = 0.0
        if prem_f < floor:
            self.state.rejected += 1
            self.state.last_reason = (
                f"ATM {strike}{option_type} at {prem_f:.2f} is below the "
                f"{snap.symbol} floor of {floor:.0f} — spread would eat it")
            logger.info("%s", self.state.last_reason)
            return None

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
            self.state.open_positions[snap.symbol] = {
                "position_id": f"paper_{decision.decision_id}",
                "opened_at": snap.ts, "strike": strike,
                "option_type": option_type, "symbol": snap.symbol,
                "lots": lots, "entry_premium": premium,
                "decision_id": decision.decision_id}
            return {"ok": True, "paper": True, "decision_id": decision.decision_id,
                    "option_type": option_type, "strike": strike, "lots": lots}

        resp = self.client.submit_intent(
            symbol=snap.symbol, side="BUY", option_type=option_type,
            strike=strike, lots=lots, decision_id=decision.decision_id)

        if resp.get("ok"):
            self.state.executed += 1
            pid = resp.get("position_id")
            if pid:
                self.state.open_positions[snap.symbol] = {
                    "position_id": pid, "opened_at": snap.ts,
                    "strike": strike, "option_type": option_type,
                    "symbol": snap.symbol, "lots": lots,
                    "entry_premium": premium,
                    "decision_id": decision.decision_id}
        elif resp.get("indeterminate"):
            # The intent may or may not have landed. Do NOT retry and do NOT
            # assume it failed — either guess can double a position. Mark the
            # symbol occupied and let reconciliation against the sentinel's own
            # position view settle it.
            self.state.errors += 1
            self.state.open_positions[snap.symbol] = {
                "position_id": "INDETERMINATE", "opened_at": snap.ts,
                "strike": strike, "option_type": option_type}
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
