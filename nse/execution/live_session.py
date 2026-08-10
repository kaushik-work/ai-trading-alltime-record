"""Run the council live: eight lenses read the tape, argue, and one call comes out.

    python -m nse.execution.live_session                 # paper, NIFTY, 60s
    python -m nse.execution.live_session --symbol BANKNIFTY --every 30
    python -m nse.execution.live_session --once          # one decision, then exit

Paper by default and refuses to leave it without `--live`, which additionally
requires `ENABLE_OPTIONS_RUNNER=true` in the environment. Two independent
switches, because a single flag is one typo away from real money.

WHAT ACTUALLY HAPPENS EACH CYCLE

    ensure_subscribed()   keep the socket on the strikes around ATM as it moves
    build_live()          one MarketSnapshot, identical in shape to a replayed one
    council.deliberate()  round 0 alone -> round 1 hearing each other -> one call
    journal + transcript  every decision stored, executed or not
    heartbeat             silence arms the sentinel's dead-man's switch

The regime percentile handed to the council is computed from CLOSED sessions
only. It is the same causal rolling percentile the backtest used, so the live
council sees the input the measured one saw — a live-only feature would make the
backtest a description of a different system.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("live_session")

IST = timezone(timedelta(hours=5, minutes=30))

#: Sessions of closed history behind the causal regime percentile.
REGIME_TRAIL_SESSIONS = 20
REGIME_RANK_SESSIONS = 100

#: Which symbols the council may trade, and why.
#:
#: Every lens weight in nse/lenses/bootstrap.py was measured on NIFTY ONLY --
#: the 1m option archive is NIFTY, and nifty_loader is not a generic loader.
#: Running another symbol applies a NIFTY-derived edge to an instrument where it
#: was never tested. That is a real extrapolation, not a config change, and it
#: is worth saying out loud rather than discovering from a P&L curve.
#:
#: Spread is the one cross-symbol thing that HAS been measured
#: (RESEARCH_LEARNINGS section 3.10, Rs 120-190 premium band, median
#: half-spread) against volume_oi's 0.70% break-even:
#:
#: Measured 2026-08-10 from live bid/ask in option_snapshots, near-ATM, each
#: symbol in the premium band matching its own floor:
#:
#:     NIFTY      p50 0.1221%   n=2299   clears
#:     BANKNIFTY  p50 0.1359%   n= 637   clears
#:     SENSEX     p50 0.1438%   n= 413   clears
#:     FINNIFTY   p50 1.7896%   n=  38   FAILS -- 2.5x the break-even
#:
#: BANKNIFTY was previously blocked for a REASON THAT NO LONGER HOLDS: nobody
#: had measured it. Measured, it sits alongside NIFTY, so it is enabled.
#: FINNIFTY is confirmed worse than the earlier 1.4760% reading, on a small
#: sample (n=38) that is itself evidence -- barely anything quotes a two-sided
#: book there in the tradeable premium band.
#:
#: SPREAD CLEARING IS NOT THE SAME AS HAVING AN EDGE. Every lens weight in
#: bootstrap.py was measured on NIFTY, because the 1m option archive is NIFTY.
#: Enabling a symbol means its COSTS do not disqualify it; the edge is still
#: extrapolated from another instrument, and that is a weaker claim than the
#: NIFTY numbers support. Trading BANKNIFTY and SENSEX is a decision to test
#: that extrapolation with real money, and should be sized accordingly.
TRADEABLE: dict[str, str] = {
    "NIFTY":     "p50 half-spread 0.1221% vs 0.70% break-even",
    "BANKNIFTY": "p50 half-spread 0.1359% (edge extrapolated from NIFTY)",
    "SENSEX":    "p50 half-spread 0.1438% (edge extrapolated from NIFTY)",
}
BLOCKED: dict[str, str] = {
    "FINNIFTY": "p50 half-spread 1.7896% -- 2.5x the 0.70% break-even",
}

_stop = False


def _handle_signal(signum, frame):
    global _stop
    _stop = True
    logger.warning("signal %s — finishing this cycle and stopping", signum)


def regime_percentile(symbol: str = "NIFTY") -> Optional[float]:
    """Where today's realised vol sits in its own trailing distribution.

    Built from CLOSED sessions only. Returns None when there is too little
    history, and None means "do not filter" everywhere it is consumed — an
    unknown regime must never be treated as a hard one, or the council would
    stand aside for its first hundred sessions.
    """
    try:
        import numpy as np

        from core.mongo import get_db
        db = get_db()
        if db is None:
            return None
        rows = list(db.nse_day_journal.find(
            {"symbol": symbol, "close_spot": {"$ne": None}},
            {"_id": 0, "session": 1, "close_spot": 1}).sort("session", -1)
            .limit(REGIME_RANK_SESSIONS + REGIME_TRAIL_SESSIONS + 2))
        if len(rows) < REGIME_TRAIL_SESSIONS + 2:
            return None
        rows.reverse()
        closes = [float(r["close_spot"]) for r in rows]
        rets = [(b - a) / a for a, b in zip(closes, closes[1:]) if a]
        if len(rets) < REGIME_TRAIL_SESSIONS + 1:
            return None
        vols = [float(np.std(rets[max(0, i - REGIME_TRAIL_SESSIONS):i]))
                for i in range(REGIME_TRAIL_SESSIONS, len(rets) + 1)]
        today, hist = vols[-1], vols[:-1]
        if len(hist) < 20:
            return None
        return float(np.mean([v < today for v in hist]))
    except Exception as e:
        logger.debug("regime percentile unavailable: %s", e)
        return None


#: Perpetuals trade 24/7. Listed explicitly rather than inferred from a name
#: pattern, because "does this instrument ever close" is a fact about the
#: contract and guessing it from a ticker string is how a 24/7 symbol ends up
#: inheriting an equity calendar.
PERP_SYMBOLS = {"BTCUSD", "ETHUSD", "XAUTUSD"}


def is_perp(symbol: str) -> bool:
    return symbol.upper() in PERP_SYMBOLS


def market_open(now: Optional[datetime] = None, symbol: str = "NIFTY") -> bool:
    """Is the council awake? True from the WARM-UP time, not the bell.

    Deciding starts at 09:00 so the structural lenses have bars and the journal
    is loaded before anything matters. TRADING is gated separately at 09:30 by
    OptionsRunner -- being awake and being allowed to buy are different
    questions, and conflating them is what left three lenses mute through the
    open.
    """
    # A PERP NEVER CLOSES, and this is where forgetting that would bite.
    # market_close_for() falls back to the NSE close for an unknown symbol, so
    # ETHUSD would have inherited 15:40 IST and idled sixteen hours a day —
    # silently, because "outside market hours" is a normal-looking log line.
    if is_perp(symbol):
        return True

    now = (now or datetime.now(timezone.utc)).astimezone(IST)
    if now.weekday() >= 5:
        return False
    from nse.config import COUNCIL_WARMUP_FROM, market_close_for
    close_t = market_close_for(symbol, now.date())
    return COUNCIL_WARMUP_FROM <= now.time() <= close_t


def _start_heartbeat(runner) -> "threading.Thread":
    """Beat on a timer of its own, NOT once per decision.

    The sentinel's dead-man's switch fires after SENTINEL_DEADMAN_SEC (30s by
    default) of silence and flattens every open position. Heartbeating inside
    the decision loop ties liveness to the decision cadence, so the default
    `--every 60` would have gone silent for 60s at a time and tripped the
    switch on every single cycle — flattening positions in production while the
    brain was perfectly healthy.

    Liveness and decision-making are different clocks and must not share one.
    The beat also continues while a slow snapshot build is in flight, which is
    exactly when the loop would otherwise stall past the timeout.
    """
    import threading

    from nse.execution.sentinel_client import HEARTBEAT_INTERVAL_SEC

    def beat():
        while not _stop:
            try:
                runner.heartbeat()
            except Exception as e:
                logger.error("heartbeat failed: %s", e)
            time.sleep(HEARTBEAT_INTERVAL_SEC)

    t = threading.Thread(target=beat, name="sentinel-heartbeat", daemon=True)
    t.start()
    logger.info("heartbeat thread started (every %.0fs)", HEARTBEAT_INTERVAL_SEC)
    return t


def run(symbols=("NIFTY",), every: int = 60, paper: bool = True,
        once: bool = False, ignore_hours: bool = False) -> int:
    from nse.council import assert_deliberation_monotone
    from nse.execution.options_runner import OptionsRunner
    from nse.snapshot import build_live
    from nse.ws.angel_stream import ensure_subscribed

    symbols = [s.upper() for s in symbols]
    for sym in list(symbols):
        if sym in BLOCKED:
            logger.error("refusing %s: %s", sym, BLOCKED[sym])
            symbols.remove(sym)
        elif sym not in TRADEABLE:
            logger.error("refusing %s: no measured spread for it", sym)
            symbols.remove(sym)
    if not symbols:
        logger.error("no tradeable symbols left — nothing to do")
        return 2
    logger.info("trading %s", ", ".join(f"{s} ({TRADEABLE[s]})" for s in symbols))

    # One runner PER SYMBOL. Sharing one would make the position cap and the
    # "already holding" guard collide across instruments, and a NIFTY position
    # would silently block a SENSEX entry.
    runners = {s: OptionsRunner(paper=paper) for s in symbols}
    runner = runners[symbols[0]]

    # Deliberation binds, and it is only safe to bind because every lens's
    # round-1 logic can lower conviction but never raise it. Prove that on the
    # first real snapshot before trading a single decision — this is the
    # invariant the binding rests on, so it is checked at startup rather than
    # trusted from a backtest run weeks ago.
    # Sessions are begun per symbol inside the loop, which also handles the
    # day rolling over without a process restart. Doing it here as well would
    # call begin_session twice on the first day and reconcile twice.
    for sym in symbols:
        logger.info("subscribing to %s option chain", sym)
        try:
            ensure_subscribed(sym, strikes_around=10)
        except Exception as e:
            logger.error("could not subscribe %s: %s", sym, e)

    # Liveness runs on its own clock. See _start_heartbeat.
    if not paper:
        _start_heartbeat(runner)

    checked_monotone = False
    cycles = 0
    idle_logged = False
    while not _stop:
        # WAIT for the next session rather than exiting at the close.
        #
        # Exiting looked tidy and was wrong: compose restarts the container
        # (restart: unless-stopped), which immediately exits again, so the
        # service spends every night in a restart loop -- `docker compose exec`
        # fails against a perpetually-restarting container, and the 09:15 open
        # arrives with the process mid-cycle rather than subscribed and warm.
        #
        # A daily service should sleep through the night, not die every evening
        # and be resurrected by the supervisor.
        if not ignore_hours and not any(market_open(symbol=s) for s in symbols):
            for sym, rn in runners.items():
                if rn.state.decisions and rn.state.session is not None:
                    logger.info("market closed — writing %s journal", sym)
                    rn.end_session(sym)
                    rn.state.session = None
            if not idle_logged:
                logger.info("outside market hours — idling until the next open")
                idle_logged = True
            if once:
                break
            time.sleep(60)
            continue

        # A new trading day: reload yesterday's journal and reset counters.
        today = datetime.now(IST).date()
        for sym, rn in runners.items():
            if rn.state.session != today:
                logger.info("session %s starting for %s", today, sym)
                rn.begin_session(today, sym)
                idle_logged = False

        for sym in symbols:
          runner = runners[sym]
          snap = None
          try:
            ensure_subscribed(sym, strikes_around=10)
            snap = build_live(sym, strikes_around=10)
          except Exception as e:
            logger.error("%s snapshot failed: %s", sym, e)

          if snap is None:
            logger.warning("no %s snapshot this cycle", sym)
          else:
            if not checked_monotone:
                tally = assert_deliberation_monotone(runner.council.lenses, [snap])
                logger.info("deliberation monotone check on live data: %s", tally)
                checked_monotone = True

            decision = runner.council.deliberate(
                snap, runner._journal, regime_pct=regime_percentile(sym))
            from nse.council import journal_decision
            journal_decision(decision)
            print(f"[{sym}] " + decision.transcript(), flush=True)

            for v in decision.round1:
                runner.state.verdicts_by_lens.setdefault(v.lens, []).append(v)
            runner.state.decisions += 1
            if runner.state.first_spot is None:
                runner.state.first_spot = snap.spot
            runner.state.last_spot = snap.spot

            if decision.executed:
                blocked = runner._blocked(snap)
                if blocked:
                    runner.state.rejected += 1
                    logger.info("cleared the council but %s", blocked)
                else:
                    runner._submit(snap, decision)
            else:
                runner.state.rejected += 1

        cycles += 1
        if once:
            break
        time.sleep(max(1, every))

    for sym, rn in runners.items():
        logger.info("session over %s: %s", sym, rn.state.summary())
        rn.end_session(sym)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--symbol", default="NIFTY",
                   help="comma-separated, e.g. NIFTY,SENSEX")
    p.add_argument("--every", type=int, default=60, help="seconds between decisions")
    p.add_argument("--once", action="store_true", help="one decision, then exit")
    p.add_argument("--ignore-hours", action="store_true",
                   help="run outside market hours (data will be stale)")
    p.add_argument("--live", action="store_true",
                   help="submit real intents. Also requires ENABLE_OPTIONS_RUNNER=true")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    paper = not a.live
    if a.live:
        # Two independent switches. A single flag is one typo from real money,
        # and the env var is the one that has to be set deliberately on the box
        # that actually holds the whitelisted IP.
        if os.environ.get("ENABLE_OPTIONS_RUNNER", "false").lower() != "true":
            logger.error("--live requires ENABLE_OPTIONS_RUNNER=true; refusing")
            return 2
        logger.warning("LIVE MODE: intents will be submitted to the sentinel")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    return run(symbols=[x.strip() for x in a.symbol.split(",") if x.strip()],
               every=a.every, paper=paper, once=a.once,
               ignore_hours=a.ignore_hours)


if __name__ == "__main__":
    sys.exit(main())
