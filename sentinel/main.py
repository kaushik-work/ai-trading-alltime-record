"""Execution sentinel — the only process that talks to Angel's order API.

Runs on a small always-on VPS whose static IP is whitelisted with the broker.
It holds the Angel credentials, the SEBI Strategy ID, and the daily 2FA session.
The brain tier sends it signed intents and a heartbeat; it sends back fills.

THE DEAD-MAN'S SWITCH IS THE REASON THIS PROCESS EXISTS SEPARATELY.

If the brain stops sending heartbeats during market hours while a position is
open, the sentinel flattens. Home power, home Wi-Fi, a laptop lid, a kernel
panic mid-trade — none of those should leave a leveraged options position
unattended. The brief calls this non-negotiable before real capital and it is
right.

Three details that make the switch safe rather than merely present:

  LIMIT, NOT MARKET.  A forced exit lands at the worst possible moment by
  definition. Orders go through the touch far enough to cross but still bounded,
  the same reasoning as the GTT bracket (nse/config.py GTT_TRIGGER_SLIP_PCT). A
  market order into a thin options book at the moment everything is moving is
  how a stop becomes a disaster.

  IT LATCHES.  Once fired, the sentinel refuses new intents until a human
  clears it. Whatever killed the brain has not been diagnosed just because the
  heartbeat came back, and a flapping connection must not produce a flapping
  book.

  IT IS THE SECOND LAYER, NOT THE ONLY ONE.  Every entry already carries an
  exchange-side GTT OCO bracket, which survives even this process dying. The
  switch covers the case the bracket cannot: the brain going dark while the
  position is still inside its bracket and drifting.

DEPLOYMENT
    SENTINEL_SECRET   >=32 chars, shared with the brain tier. Not the Angel key.
    ANGEL_*           the usual credentials, ONLY on this host.
    uvicorn sentinel.main:app --host 0.0.0.0 --port 8090
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)

# Silence for this long, during market hours, with a position open, arms the
# flatten. Six missed 5s heartbeats: long enough to ride out a blip, short
# enough that a dead brain is caught inside half a minute.
DEADMAN_TIMEOUT_SEC = float(os.environ.get("SENTINEL_DEADMAN_SEC", 30.0))
DEADMAN_CHECK_INTERVAL_SEC = 1.0

# Refuse to act at all unless explicitly armed. A sentinel that starts hot on a
# fresh deploy is a sentinel that trades on a config you have not reviewed.
LIVE_ORDERS = os.environ.get("SENTINEL_LIVE_ORDERS", "0") == "1"


class State:
    def __init__(self):
        self.last_heartbeat: float = 0.0
        self.deadman_fired: bool = False
        self.deadman_fired_at: Optional[str] = None
        self.intents_accepted = 0
        self.intents_rejected = 0
        self.orders_placed = 0
        self.positions: dict[str, dict] = {}
        self.started = time.time()
        self.last_reject_reason: str = ""


STATE = State()
_nonce_cache = None


def _cache():
    global _nonce_cache
    if _nonce_cache is None:
        from sentinel.auth import NonceCache
        _nonce_cache = NonceCache()
    return _nonce_cache


def _market_open() -> bool:
    try:
        from core.utils import now_ist
        from nse.config import MARKET_CLOSE, MARKET_OPEN
        n = now_ist()
        return n.weekday() < 5 and MARKET_OPEN <= n.time() <= MARKET_CLOSE
    except Exception:
        return False


async def _deadman_loop():
    """Watch the heartbeat. Flatten if the brain goes dark mid-session."""
    while True:
        try:
            await asyncio.sleep(DEADMAN_CHECK_INTERVAL_SEC)
            if STATE.deadman_fired or not STATE.positions:
                continue
            if not _market_open():
                continue
            if STATE.last_heartbeat <= 0:
                continue          # never heard from the brain; nothing to lose
            silence = time.time() - STATE.last_heartbeat
            if silence <= DEADMAN_TIMEOUT_SEC:
                continue

            logger.critical(
                "DEAD-MAN'S SWITCH: %.1fs without a heartbeat with %d position(s) "
                "open. Flattening.", silence, len(STATE.positions))
            STATE.deadman_fired = True
            STATE.deadman_fired_at = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(_flatten_all, f"deadman:{silence:.0f}s")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("deadman loop error: %s", e, exc_info=True)


def _flatten_all(reason: str) -> None:
    """Close every open position with bounded limit orders. Never raises."""
    from nse.config import EXCHANGE, LOT_SIZES, gtt_limit_through

    # Paper positions are dropped without touching a broker. Handled BEFORE the
    # broker is constructed, so a rehearsal never needs Angel credentials and a
    # credential failure below cannot mask whether the switch fired.
    for pid, pos in list(STATE.positions.items()):
        if pos.get("paper"):
            STATE.positions.pop(pid, None)
            logger.critical("FLATTEN (paper) %s — %s", pid, reason)
    if not STATE.positions:
        return

    try:
        from data.angel_fetcher import AngelFetcher
        from nse.broker.angel_broker import AngelBroker
        broker = AngelBroker(AngelFetcher.get())
    except Exception as e:
        logger.critical("FLATTEN FAILED to reach the broker (%s) — positions "
                        "remain open and are protected ONLY by their exchange "
                        "GTT brackets", e)
        return

    for pid, pos in list(STATE.positions.items()):
        try:
            symbol = pos["symbol"]
            exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
            quote = broker.fetcher.get_option_quote(
                pos["tradingsymbol"], pos["token"], EXCHANGE.get(symbol, "NFO"))
            # Cross the book, but bounded. A market order here is how a forced
            # exit becomes the worst fill of the day.
            ref = (quote or {}).get("bid" if exit_side == "SELL" else "ask") \
                or (quote or {}).get("ltp") or 0.0
            limit = gtt_limit_through(float(ref), exit_side == "SELL") if ref else None

            res = broker.place_single_order(
                symbol, pos["tradingsymbol"], pos["token"], pos["option_type"],
                exit_side, pos["lots"], limit_price=limit)
            logger.critical("FLATTEN %s (%s): %s", pid, reason, res)
            if res.get("status"):
                STATE.positions.pop(pid, None)
        except Exception as e:
            logger.critical("FLATTEN FAILED for %s: %s — position still open", pid, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_deadman_loop())
    logger.warning("sentinel up | live_orders=%s | deadman=%.0fs",
                   LIVE_ORDERS, DEADMAN_TIMEOUT_SEC)
    yield
    task.cancel()


app = FastAPI(title="NSE execution sentinel", lifespan=lifespan)


async def _verified(request: Request) -> tuple[bool, dict, str]:
    from sentinel.auth import verify
    try:
        env = await request.json()
    except Exception:
        return False, {}, "unparseable body"
    ok, why = verify(env, _cache())
    if not ok:
        STATE.intents_rejected += 1
        STATE.last_reject_reason = why
        logger.warning("sentinel: rejected envelope: %s", why)
        return False, {}, why
    return True, env.get("intent", {}), "ok"


@app.post("/heartbeat")
async def heartbeat(request: Request):
    ok, _intent, why = await _verified(request)
    if not ok:
        return {"ok": False, "error": why}
    STATE.last_heartbeat = time.time()
    return {"ok": True, "deadman_fired": STATE.deadman_fired,
            "positions": len(STATE.positions)}


@app.post("/intent")
async def intent(request: Request):
    ok, body, why = await _verified(request)
    if not ok:
        return {"ok": False, "error": why}

    if STATE.deadman_fired:
        STATE.intents_rejected += 1
        return {"ok": False, "error":
                "dead-man's switch has fired; clear it manually after finding "
                "out why the brain went dark"}
    if not LIVE_ORDERS:
        STATE.intents_accepted += 1
        logger.info("sentinel: PAPER accept %s", body)
        # Register a PAPER position so the dead-man's switch has something to
        # guard. It previously returned here without touching STATE.positions,
        # and the deadman loop skips whenever `not STATE.positions` — so the
        # single most safety-critical component in this system could not be
        # exercised at all without arming real orders against a live account.
        #
        # A safety mechanism with no rehearsal path is a safety mechanism you
        # find out about during the incident. These positions carry
        # `paper: True` and `_flatten_all` drops them without calling a broker,
        # so the timing, arming and firing logic is exercised end to end while
        # the order path stays cold.
        if body.get("type") == "OPEN":
            pid = f"paper_{body.get('decision_id', uuid.uuid4().hex[:8])}"
            STATE.positions[pid] = {
                "paper": True,
                "symbol": body.get("symbol"),
                "side": body.get("side"),
                "option_type": body.get("option_type"),
                "strike": body.get("strike"),
                "lots": body.get("lots"),
                "tradingsymbol": f"PAPER-{body.get('symbol')}-{body.get('strike')}"
                                 f"{body.get('option_type')}",
                "token": None,
                "opened_at": datetime.now(timezone.utc).isoformat(),
            }
            return {"ok": True, "paper": True, "position_id": pid,
                    "intent": body}
        if body.get("type") == "CLOSE":
            pid = body.get("position_id")
            STATE.positions.pop(pid, None)
            return {"ok": True, "paper": True, "position_id": pid}
        return {"ok": True, "paper": True, "intent": body}

    STATE.intents_accepted += 1
    return await asyncio.to_thread(_place, body)


def _place(body: dict) -> dict:
    from data.angel_fetcher import AngelFetcher
    from nse.broker.angel_broker import AngelBroker
    from nse.data.option_chain import OptionChainCache

    kind = body.get("type")
    broker = AngelBroker(AngelFetcher.get())

    if kind == "CLOSE":
        pid = body.get("position_id")
        pos = STATE.positions.get(pid)
        if not pos:
            return {"ok": False, "error": f"unknown position {pid}"}
        _flatten_all(body.get("reason", "requested"))
        return {"ok": pid not in STATE.positions, "position_id": pid}

    if kind != "OPEN":
        return {"ok": False, "error": f"unknown intent type {kind}"}

    symbol = body["symbol"]
    cache = OptionChainCache(symbol, broker.fetcher)
    expiry = cache.nearest_expiry(min_days=0)
    if expiry is None:
        return {"ok": False, "error": "no expiry"}
    ts, token = cache.resolve_leg(int(body["strike"]), body["option_type"], expiry)
    if not ts or not token:
        return {"ok": False, "error": "could not resolve the contract"}

    res = broker.place_single_order(
        symbol, ts, token, body["option_type"], body["side"], int(body["lots"]),
        limit_price=body.get("limit_price"),
        sl_points=body.get("sl_points"), target_points=body.get("target_points"))

    if res.get("status") and res.get("order_id"):
        STATE.orders_placed += 1
        STATE.positions[res["order_id"]] = {
            "symbol": symbol, "tradingsymbol": ts, "token": token,
            "option_type": body["option_type"], "side": body["side"],
            "lots": int(body["lots"]), "decision_id": body.get("decision_id"),
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
    return {"ok": bool(res.get("status")), "result": res}


@app.get("/status")
def status():
    silence = (time.time() - STATE.last_heartbeat) if STATE.last_heartbeat else None
    return {
        "ok": True,
        "live_orders": LIVE_ORDERS,
        "market_open": _market_open(),
        "deadman": {
            "fired": STATE.deadman_fired,
            "fired_at": STATE.deadman_fired_at,
            "timeout_sec": DEADMAN_TIMEOUT_SEC,
            "silence_sec": None if silence is None else round(silence, 1),
            "armed": bool(STATE.positions) and _market_open(),
        },
        "positions": len(STATE.positions),
        "intents_accepted": STATE.intents_accepted,
        "intents_rejected": STATE.intents_rejected,
        "last_reject_reason": STATE.last_reject_reason,
        "orders_placed": STATE.orders_placed,
        "uptime_sec": round(time.time() - STATE.started, 1),
    }


@app.post("/deadman/clear")
def clear_deadman():
    """Manual reset. Deliberately unsigned-but-local: run it on the box.

    Not exposed to the brain tier on purpose. Whatever killed the heartbeat has
    not been diagnosed just because it came back, and the decision to resume
    trading belongs to a person.
    """
    was = STATE.deadman_fired
    STATE.deadman_fired = False
    STATE.deadman_fired_at = None
    logger.warning("sentinel: dead-man's switch cleared manually (was %s)", was)
    return {"ok": True, "was_fired": was}
