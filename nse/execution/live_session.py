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


def market_open(now: Optional[datetime] = None, symbol: str = "NIFTY") -> bool:
    now = (now or datetime.now(timezone.utc)).astimezone(IST)
    if now.weekday() >= 5:
        return False
    from nse.config import market_close_for
    close_t = market_close_for(symbol, now.date())
    return now.time() >= __import__("datetime").time(9, 15) and now.time() <= close_t


def run(symbol: str = "NIFTY", every: int = 60, paper: bool = True,
        once: bool = False, ignore_hours: bool = False) -> int:
    from nse.council import assert_deliberation_monotone
    from nse.execution.options_runner import OptionsRunner
    from nse.snapshot import build_live
    from nse.ws.angel_stream import ensure_subscribed

    runner = OptionsRunner(paper=paper)

    # Deliberation binds, and it is only safe to bind because every lens's
    # round-1 logic can lower conviction but never raise it. Prove that on the
    # first real snapshot before trading a single decision — this is the
    # invariant the binding rests on, so it is checked at startup rather than
    # trusted from a backtest run weeks ago.
    session = datetime.now(IST).date()
    runner.begin_session(session, symbol)

    logger.info("subscribing to %s option chain", symbol)
    try:
        ensure_subscribed(symbol, strikes_around=10)
    except Exception as e:
        logger.error("could not subscribe: %s", e)
        return 2

    checked_monotone = False
    cycles = 0
    while not _stop:
        if not ignore_hours and not market_open(symbol=symbol):
            logger.info("market closed — stopping")
            break

        snap = None
        try:
            ensure_subscribed(symbol, strikes_around=10)
            snap = build_live(symbol, strikes_around=10)
        except Exception as e:
            logger.error("snapshot failed: %s", e)

        if snap is None:
            logger.warning("no snapshot this cycle")
        else:
            if not checked_monotone:
                tally = assert_deliberation_monotone(runner.council.lenses, [snap])
                logger.info("deliberation monotone check on live data: %s", tally)
                checked_monotone = True

            decision = runner.council.deliberate(
                snap, runner._journal, regime_pct=regime_percentile(symbol))
            from nse.council import journal_decision
            journal_decision(decision)
            print(decision.transcript(), flush=True)

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

        runner.heartbeat()
        cycles += 1
        if once:
            break
        time.sleep(max(1, every))

    logger.info("session over: %s", runner.state.summary())
    runner.end_session(symbol)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--symbol", default="NIFTY")
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
    return run(symbol=a.symbol, every=a.every, paper=paper, once=a.once,
               ignore_hours=a.ignore_hours)


if __name__ == "__main__":
    sys.exit(main())
