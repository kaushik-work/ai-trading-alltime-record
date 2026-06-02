"""
Crypto API routes — to be registered in api/server.py.

Hits the live Delta India REST API (same logic as delta_exchange/live_signal.py)
and computes the current synthetic-forward signals across BTC, ETH, XAUT.

Routes:
  GET /api/crypto/signals     — current signal radar (per asset, per expiry)
  GET /api/crypto/portfolio   — paper-trade equity, PnL, sharpe, DD (stub for now)

Integration (in server.py):
  from api.routes_crypto import router as crypto_router
  app.include_router(crypto_router)
"""

from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import List, Optional
import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer
import numpy as np

router = APIRouter(prefix="/api/crypto", tags=["crypto"])
_auth = HTTPBearer(auto_error=False)

DELTA_BASE = os.environ.get("DELTA_BASE_URL", "https://api.india.delta.exchange")
GATE_PCT   = float(os.environ.get("CRYPTO_GATE_PCT", "0.006"))
MONEYNESS  = 0.05
MIN_STRIKES = 3
TT_MIN_HOURS = 6
TT_MAX_HOURS = 72


def _parse_option_symbol(sym: str):
    parts = sym.split("-")
    if len(parts) != 4: return None
    side, asset, strike, ddmmyy = parts
    try:
        strike = int(strike)
        dd, mm, yy = ddmmyy[:2], ddmmyy[2:4], ddmmyy[4:6]
        expiry = datetime(2000 + int(yy), int(mm), int(dd), 12, 0, tzinfo=timezone.utc)
    except Exception:
        return None
    return side, asset, strike, expiry


def _fetch_chain(underlying: str) -> tuple[float, list]:
    """Return (spot, list of option contracts) for one underlying."""
    # perp mark
    r = requests.get(f"{DELTA_BASE}/v2/tickers",
                     params={"contract_types": "perpetual_futures",
                             "underlying_asset_symbols": underlying},
                     timeout=10)
    r.raise_for_status()
    perps = r.json().get("result", [])
    spot = None
    target_perp = f"{underlying}USD"
    for p in perps:
        if p.get("symbol") == target_perp:
            try: spot = float(p["mark_price"])
            except (KeyError, TypeError, ValueError): pass
            break
    if spot is None: return None, []

    # options
    r = requests.get(f"{DELTA_BASE}/v2/tickers",
                     params={"contract_types": "call_options,put_options",
                             "underlying_asset_symbols": underlying},
                     timeout=10)
    r.raise_for_status()
    out = []
    for o in r.json().get("result", []):
        sym = o.get("symbol")
        parsed = _parse_option_symbol(sym) if sym else None
        if not parsed: continue
        side, asset, strike, expiry = parsed
        if asset != underlying: continue
        try: mark = float(o.get("mark_price") or 0)
        except (TypeError, ValueError): mark = 0
        if mark <= 0: continue
        out.append({"side": side, "strike": strike, "expiry": expiry, "mark": mark,
                    "symbol": sym})
    return spot, out


def _compute_per_expiry(spot: float, chain: list, now: datetime) -> list:
    out = []
    expiries = sorted({c["expiry"] for c in chain})
    for exp in expiries:
        tte_h = (exp - now).total_seconds() / 3600
        if not (TT_MIN_HOURS <= tte_h <= TT_MAX_HOURS): continue
        same = [c for c in chain if c["expiry"] == exp]
        calls = {c["strike"]: c for c in same if c["side"] == "C"}
        puts  = {c["strike"]: c for c in same if c["side"] == "P"}
        common = sorted(set(calls) & set(puts))
        near = [K for K in common if abs(K - spot) / spot <= MONEYNESS]
        if len(near) < MIN_STRIKES: continue
        devs = []
        for K in near:
            cp = calls[K]["mark"]; pp = puts[K]["mark"]
            if cp <= 0 or pp <= 0: continue
            devs.append(((cp - pp + K) - spot) / spot)
        if len(devs) < MIN_STRIKES: continue
        pos = sum(1 for d in devs if d > 0); neg = sum(1 for d in devs if d < 0)
        if pos < MIN_STRIKES and neg < MIN_STRIKES: continue
        atm_K = min(near, key=lambda K: abs(K - spot))
        out.append({"expiry": exp.strftime("%Y-%m-%d %H:%M"),
                    "pred_pct": float(np.median(devs)) * 100,
                    "n_strikes": len(devs),
                    "atm_strike": atm_K,
                    "tte_hours": tte_h})
    return out


@router.get("/signals")
def crypto_signals():
    """Current synth-forward signals across BTC, ETH, XAUT."""
    out = []
    now = datetime.now(timezone.utc)
    for underlying in ("BTC", "ETH", "XAUT"):
        try:
            spot, chain = _fetch_chain(underlying)
            if spot is None: continue
            sigs = _compute_per_expiry(spot, chain, now)
            for s in sigs:
                out.append({
                    "underlying": underlying, "spot": spot,
                    "expiry": s["expiry"], "pred_pct": s["pred_pct"],
                    "n_strikes": s["n_strikes"], "atm_strike": s["atm_strike"],
                    "tte_hours": s["tte_hours"],
                })
            # XAUT has no options → spot only, no signals
        except Exception:
            continue
    return out


@router.get("/portfolio")
def crypto_portfolio():
    """Paper-trade portfolio state — stub. Wire to real journal in next iteration."""
    return {
        "equity": 10_000.0,
        "day_pnl": 0.0,
        "open_positions": 0,
        "rolling_sharpe": 0.0,
        "max_dd_pct": 0.0,
    }


@router.get("/state")
def crypto_state():
    """Live runner state — open positions, day P&L, kill switch status."""
    try:
        from core.crypto_runner import get_state
        return get_state()
    except Exception as e:
        return {"enabled": False, "error": str(e),
                "mode": "unknown", "strategies": [], "open_positions": {}}


@router.post("/kill")
def crypto_kill():
    """EMERGENCY STOP — closes all open positions and halts new entries.

    Calls crypto_runner.manual_kill() which:
      1. Places reduce_only orders for every open position (market_order)
      2. Sets _KILLED=True so the scheduler halts new entries
      3. Logs the kill event

    Returns the positions that were closed.
    """
    try:
        from core.crypto_runner import manual_kill, get_state
        before = get_state()
        positions_before = list(before.get("open_positions", {}).keys())
        manual_kill()
        after = get_state()
        return {
            "ok": True,
            "killed_strategies": positions_before,
            "open_after": list(after.get("open_positions", {}).keys()),
            "kill_switch_armed": after.get("killed", True),
            "message": f"Killed {len(positions_before)} position(s). "
                       f"Bot will not enter new positions until restart.",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
