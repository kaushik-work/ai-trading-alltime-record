"""REST routes for NSE synthetic-forward runner."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.auth import decode_token, oauth2_scheme
from nse.execution.nse_runner import get_nse_runner_state
from nse.risk import is_killed, set_killed
from nse.broker.angel_broker import AngelBroker
from nse.data.option_chain import OptionChainCache
from nse.config import STEP_SIZES, LOT_SIZES

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


class BacktestRequest(BaseModel):
    symbol: str = "NIFTY"
    source: str = "csv"
    capital: float = 50_000.0
    interval: int = 5


@router.post("/test_buy_ce")
def nse_test_buy_ce(user: dict = Depends(_get_current_user)):
    """Place a test buy CE market order at current spot NIFTY ATM strike.

    WARNING: This places a real order in live mode. Use only for API testing.
    The order is placed regardless of available funds; Angel One will reject if
    the account is not funded, and the rejection reason is returned.
    """
    try:
        from data.angel_fetcher import AngelFetcher
        fetcher = AngelFetcher.get()
        broker = AngelBroker(fetcher)
        cache = OptionChainCache("NIFTY", fetcher)

        # Auth + RMS check for visibility only.
        if not fetcher._ensure_logged_in():
            raise HTTPException(status_code=503, detail="Angel One not logged in")
        rms = fetcher.get_rms() or {}

        spot = cache.get_underlying_ltp()
        if spot is None:
            raise HTTPException(status_code=503, detail="NIFTY spot not available")

        expiry = cache.nearest_expiry(min_days=0)
        if expiry is None:
            raise HTTPException(status_code=503, detail="NIFTY expiry not available")

        step = STEP_SIZES["NIFTY"]
        atm = int(round(spot / step)) * step
        ts, token = cache.resolve_leg(atm, "CE", expiry)
        if not ts or not token:
            raise HTTPException(status_code=503, detail=f"Could not resolve NIFTY {atm} CE")

        quantity = LOT_SIZES["NIFTY"]
        resp = broker.place_single_order("NIFTY", ts, token, "CE", "BUY", 1)
        logger.warning("NSE test buy CE by %s | spot=%s strike=%s qty=%s | rms=%s | resp=%s",
                       user, spot, atm, quantity, rms, resp)
        return {
            "spot": spot,
            "strike": atm,
            "expiry": expiry.isoformat(),
            "tradingsymbol": ts,
            "token": token,
            "lots": 1,
            "quantity": quantity,
            "available_cash": rms.get("availablecash"),
            "available_limit": rms.get("availablelimitmargin"),
            "net": rms.get("net"),
            "order_response": resp,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("NSE test buy CE failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/backtest/synthetic_forward")
def nse_backtest(req: BacktestRequest, user: dict = Depends(_get_current_user)):
    if req.symbol not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    if req.source not in ("csv", "mongo"):
        raise HTTPException(status_code=400, detail="source must be csv or mongo")

    from nse.backtest.synthetic_forward import run_backtest
    from nse.data.option_chain import load_snapshots_csv, load_snapshots_mongo

    try:
        df = load_snapshots_mongo(req.symbol) if req.source == "mongo" else load_snapshots_csv(req.symbol)
    except Exception as e:
        logger.warning("backtest data load failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Data load failed: {e}") from e

    if df.empty:
        return {"error": "no data"}

    metrics = run_backtest(req.symbol, df, capital=req.capital, interval_minutes=req.interval)
    return {
        "symbol": req.symbol,
        "mode": "live",
        "trades": metrics["trades"],
        "win_rate": round(metrics["win_rate"], 2),
        "total_pnl": round(metrics["total_pnl"], 2),
        "total_return_pct": round(metrics["total_return_pct"], 2),
        "profit_factor": round(metrics["profit_factor"], 2),
        "avg_win": round(metrics["avg_win"], 2),
        "avg_loss": round(metrics["avg_loss"], 2),
        "max_drawdown_pct": round(metrics["max_drawdown_pct"], 2),
        "equity": round(metrics["equity"], 2),
    }
