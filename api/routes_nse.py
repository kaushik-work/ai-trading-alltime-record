"""REST routes for NSE synthetic-forward runner."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import decode_token, oauth2_scheme
from nse.execution.nse_runner import get_nse_runner_state
from nse.risk import is_killed, set_killed
from nse.broker.angel_broker import AngelBroker
from nse.data.option_chain import OptionChainCache
from nse.config import STEP_SIZES, LOT_SIZES, EXCHANGE

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/nse")


def _get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        return decode_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc


@router.get("/status")
def nse_status(user: dict = Depends(_get_current_user)):
    return get_nse_runner_state()


@router.post("/kill")
def nse_kill(user: dict = Depends(_get_current_user)):
    if is_killed():
        return {"killed": True, "message": "already killed"}
    set_killed(True)
    logger.warning("NSE kill switch activated via API by %s", user)
    return {"killed": True, "message": "NSE entries halted"}


@router.post("/unkill")
def nse_unkill(user: dict = Depends(_get_current_user)):
    set_killed(False)
    logger.warning("NSE kill switch cleared via API by %s", user)
    return {"killed": False, "message": "NSE entries resumed"}


@router.post("/test_buy_ce")
def nse_test_buy_ce(user: dict = Depends(_get_current_user)):
    """Place a test LIMIT buy CE order at current NIFTY ATM strike with GTT SL/Target.

    Entry is placed at the current ask price (no entry slippage).  After fill,
    two GTT rules are created:
      - SL   : sell if premium drops 10 points from fill price
      - Target: sell if premium rises 50 points from fill price

    WARNING: This places a real order in live mode. Use only for API testing.
    The order is placed regardless of available funds; Angel One will reject if
    the account is not funded, and the rejection reason is returned.
    """
    from data.angel_fetcher import AngelFetcher
    fetcher = AngelFetcher.get()
    broker = AngelBroker(fetcher)
    cache = OptionChainCache("NIFTY", fetcher)

    # Force a fresh login attempt (bypass scheduler cooldown) for user-initiated tests.
    if not fetcher._ensure_logged_in(force=True):
        logger.error("NSE test buy CE by %s: Angel One login failed", user)
        raise HTTPException(status_code=503, detail="Angel One not logged in")

    rms = fetcher.get_rms() or {}

    spot = cache.get_underlying_ltp()
    if spot is None:
        logger.error("NSE test buy CE by %s: NIFTY spot unavailable", user)
        raise HTTPException(status_code=503, detail="NIFTY spot not available")

    expiry = cache.nearest_expiry(min_days=0)
    if expiry is None:
        logger.error("NSE test buy CE by %s: NIFTY expiry unavailable", user)
        raise HTTPException(status_code=503, detail="NIFTY expiry not available")

    step = STEP_SIZES["NIFTY"]
    atm = int(round(spot / step)) * step
    ts, token = cache.resolve_leg(atm, "CE", expiry)
    if not ts or not token:
        logger.error("NSE test buy CE by %s: could not resolve NIFTY %s CE", user, atm)
        raise HTTPException(status_code=503, detail=f"Could not resolve NIFTY {atm} CE")

    quantity = LOT_SIZES["NIFTY"]

    # Fetch ask price so we can enter via LIMIT and avoid slippage.
    # Fall back to LTP when ask is zero (after-hours / illiquid quote).
    quote = fetcher.get_option_quote(ts, token, EXCHANGE.get("NIFTY", "NFO"))
    if not quote:
        logger.error(
            "NSE test buy CE by %s: option quote unavailable for %s",
            user, ts,
        )
        raise HTTPException(status_code=503, detail="Could not fetch NIFTY option quote")
    limit_price = quote.get("ask") or quote.get("ltp") or 0
    if limit_price <= 0:
        logger.error(
            "NSE test buy CE by %s: option quote has no usable price for %s (quote=%s)",
            user, ts, quote,
        )
        raise HTTPException(status_code=503, detail="NIFTY option quote has no usable price")
    limit_price = round(float(limit_price), 2)

    # Connectivity check only — proves the Angel session, instrument lookup,
    # order permissions and GTT OCO attachment all work. The bracket values
    # are arbitrary and intentionally left alone; this is not a trade.
    result = broker.place_single_order(
        "NIFTY", ts, token, "CE", "BUY", 1,
        limit_price=limit_price,
        sl_points=10.0,
        target_points=50.0,
    )
    logger.warning("NSE test buy CE by %s | spot=%s strike=%s qty=%s limit=%s | rms=%s | result=%s",
                   user, spot, atm, quantity, limit_price, rms, result)
    return {
        "spot": spot,
        "strike": atm,
        "expiry": expiry.isoformat(),
        "tradingsymbol": ts,
        "token": token,
        "lots": 1,
        "quantity": quantity,
        "limit_price": limit_price,
        "available_cash": rms.get("availablecash"),
        "available_limit": rms.get("availablelimitmargin"),
        "net": rms.get("net"),
        "order_response": result,
    }
