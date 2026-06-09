"""
Crypto API routes — registered in api/server.py.

Reads signals + portfolio state from the stream-backed broker (no REST hits
on the hot path). Routes:
  GET  /api/crypto/signals          — signal radar (per asset, per expiry)
  GET  /api/crypto/snapshot         — full dashboard payload (signals + portfolio + marks)
  GET  /api/crypto/portfolio        — equity, day P&L, open positions
  GET  /api/crypto/state            — runner state (kill switch, open positions detail)
  GET  /api/crypto/candles          — historical OHLCV for chart (Delta REST + 60s cache)
  GET  /api/crypto/signal-history   — recent pred_pct samples for chart overlay
  POST /api/crypto/kill             — emergency stop
"""

from __future__ import annotations
import os
import time
from datetime import datetime, timezone
import requests
from fastapi import APIRouter, HTTPException, Query
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


def _signals_from_broker() -> tuple[list, dict]:
    """Stream-backed signal compute. Returns (signals, perp_marks).

    Replaces REST fetch in /signals: reads marks from the broker (which prefers
    the WS stream), so the dashboard and the runner see the exact same data.
    Same shape as legacy _fetch_chain → _compute_per_expiry pipeline.
    """
    from core.brokers.delta_crypto import get_broker
    broker = get_broker()
    now = datetime.now(timezone.utc)
    signals: list = []
    perp_marks: dict = {}
    for underlying in ("BTC", "ETH"):
        sym = f"{underlying}USD"
        spot = broker.get_perp_mark(sym)
        if spot is None: continue
        perp_marks[sym] = spot
        # Build chain rows compatible with _compute_per_expiry: it expects
        # {side, strike, expiry, mark, symbol}. Broker (stream or REST) returns
        # {symbol, mark} (and possibly extras); we parse the symbol locally.
        chain: list = []
        for c in broker.get_option_chain(underlying):
            parsed = _parse_option_symbol(c.get("symbol", ""))
            if not parsed: continue
            side, asset, strike, expiry = parsed
            if asset != underlying: continue
            try: mark = float(c.get("mark") or 0)
            except (TypeError, ValueError): mark = 0
            if mark <= 0: continue
            chain.append({"side": side, "strike": strike, "expiry": expiry,
                          "mark": mark, "symbol": c["symbol"]})
        for s in _compute_per_expiry(spot, chain, now):
            signals.append({
                "underlying": underlying, "spot": spot,
                "expiry": s["expiry"], "pred_pct": s["pred_pct"],
                "n_strikes": s["n_strikes"], "atm_strike": s["atm_strike"],
                "tte_hours": s["tte_hours"],
            })
    return signals, perp_marks


def _build_crypto_snapshot() -> dict:
    """Single source of truth for both /api/crypto/signals and /ws/crypto.

    Bundles everything the crypto dashboard needs in one round-trip: live
    signals, portfolio state, perp marks, and stream diagnostics.
    """
    signals, perp_marks = _signals_from_broker()
    portfolio = _portfolio_snapshot()
    try:
        from core.ws.delta_stream import get_stream
        stream = get_stream().diagnostics()
    except Exception:
        stream = {"connected": False}
    return {
        "ts":         datetime.now(timezone.utc).isoformat(),
        "perp_marks": perp_marks,
        "signals":    signals,
        "portfolio":  portfolio,
        "stream":     stream,
    }


def _portfolio_snapshot() -> dict:
    """Live portfolio: real Delta wallet balance, day P&L, open positions."""
    try:
        from core.crypto_runner import get_state
        from core.brokers.delta_crypto import get_broker
        state = get_state()
        broker = get_broker()
        # In live mode pull the real Delta wallet balance (cached 15s in the
        # broker); paper mode has no wallet so report None — the dashboard
        # treats that as "n/a".
        wallet = None
        if broker.mode == "live":
            try: wallet = broker.get_balance()
            except Exception: wallet = None
        return {
            "wallet_usd":     float(wallet) if wallet is not None else None,
            "day_pnl":        float(state.get("day_pnl_usd", 0) or 0),
            "open_positions": len(state.get("open_positions", {})),
            "killed":         bool(state.get("killed", False)),
            "mode":           state.get("mode", "unknown"),
        }
    except Exception:
        return {"wallet_usd": None, "day_pnl": 0.0, "open_positions": 0,
                "killed": False, "mode": "unknown"}


@router.get("/signals")
def crypto_signals():
    """Current synth-forward signals across BTC, ETH."""
    signals, _ = _signals_from_broker()
    return signals


@router.get("/snapshot")
def crypto_snapshot():
    """Full crypto dashboard payload: signals + portfolio + perp marks + stream."""
    return _build_crypto_snapshot()


@router.get("/portfolio")
def crypto_portfolio():
    """Live portfolio state (day P&L + open positions from crypto_runner)."""
    return _portfolio_snapshot()


@router.get("/state")
def crypto_state():
    """Live runner state — open positions, day P&L, kill switch status."""
    try:
        from core.crypto_runner import get_state
        return get_state()
    except Exception as e:
        return {"enabled": False, "error": str(e),
                "mode": "unknown", "strategies": [], "open_positions": {}}


# ── chart data ───────────────────────────────────────────────────────────────
_RESOLUTION_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400,
}
_candles_cache: dict[tuple[str, str], dict] = {}
_CANDLES_TTL = 60


@router.get("/candles")
def crypto_candles(
    asset: str = Query("BTC", pattern="^(BTC|ETH)$"),
    resolution: str = Query("5m", pattern="^(1m|5m|15m|1h|4h|1d)$"),
    hours: int = Query(24, ge=1, le=720),
):
    """Historical OHLCV candles for chart. Hits Delta /v2/history/candles.

    Default: last 24 hours of 5-min BTC candles. Cached 60s.
    """
    key = (asset, resolution, hours)
    cached = _candles_cache.get(key)
    if cached and time.time() - cached["ts"] < _CANDLES_TTL:
        return cached["data"]
    symbol = f"{asset}USD"
    end_ts = int(time.time())
    start_ts = end_ts - hours * 3600
    try:
        r = requests.get(f"{DELTA_BASE}/v2/history/candles",
                         params={"resolution": resolution, "symbol": symbol,
                                 "start": start_ts, "end": end_ts},
                         timeout=15)
        r.raise_for_status()
        rows = r.json().get("result", []) or []
        candles = []
        for row in rows:
            try:
                candles.append({
                    "time":   int(row["time"]),
                    "open":   float(row["open"]),
                    "high":   float(row["high"]),
                    "low":    float(row["low"]),
                    "close":  float(row["close"]),
                    "volume": float(row.get("volume") or 0),
                })
            except (KeyError, TypeError, ValueError):
                continue
        candles.sort(key=lambda c: c["time"])
        payload = {"asset": asset, "resolution": resolution, "candles": candles}
        _candles_cache[key] = {"data": payload, "ts": time.time()}
        return payload
    except Exception as e:
        return {"asset": asset, "resolution": resolution, "candles": [],
                "error": str(e)}


@router.get("/signal-history")
def crypto_signal_history(
    asset: str = Query("BTC", pattern="^(BTC|ETH)$"),
    hours: int = Query(24, ge=1, le=168),
):
    """Pred_pct samples for chart overlay. Combines two sources:

      1. Runner's in-memory _sig_history (every signal compute, all-day).
      2. Mongo signal_log (only gate-crossings — historic, may be empty).

    The in-memory source ensures the chart has a visible line even when no
    signals have crossed the gate yet. Returns [{ts, pred_pct}] sorted asc.
    """
    samples: list[dict] = []
    # ── 1. in-memory _pred_trace from runner (every tick, ungated) ──────────
    # Bucket to 5-min boundaries so the chart payload is tractable: at 2s
    # ticks the raw trace is ~43k samples/day; bucketed it's ~288. We take
    # the LAST sample per bucket (rather than mean) so the chart reflects
    # the most recent value in that window.
    try:
        from core.crypto_runner import _get_strategies
        strat_name = "btc_synth_forward" if asset == "BTC" else "eth_synth_forward"
        strats = _get_strategies()
        strat = strats.get(strat_name)
        if strat and getattr(strat, "_pred_trace", None):
            cutoff_ts = datetime.now(timezone.utc).timestamp() - hours * 3600
            # Snapshot the list to avoid race with runner thread mutating it
            trace = list(strat._pred_trace)
            buckets: dict[int, tuple[float, float]] = {}
            for t, p in trace:
                if t < cutoff_ts: continue
                b = int(t) - (int(t) % 300)
                buckets[b] = (t, p)
            for b, (t, p) in sorted(buckets.items()):
                samples.append({"ts": b, "pred_pct": float(p)})
    except Exception:
        pass
    # ── 2. Mongo signal_log (gate-crossings only) ───────────────────────────
    try:
        from core import mongo
        from datetime import timedelta
        db = mongo.get_db()
        if db is not None:
            symbol = f"{asset}USD"
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            rows = list(db["signal_log"].find(
                {"venue": "delta_india", "symbol": symbol, "ts": {"$gte": cutoff}},
                projection={"_id": 0, "ts": 1, "pred_pct": 1},
            ).sort("ts", 1))
            for r in rows:
                ts = r.get("ts")
                if not hasattr(ts, "timestamp"): continue
                samples.append({"ts": int(ts.timestamp()),
                                "pred_pct": float(r.get("pred_pct") or 0)})
    except Exception:
        pass
    samples.sort(key=lambda s: s["ts"])
    return {"asset": asset, "samples": samples}


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
