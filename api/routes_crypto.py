"""
Crypto API routes — registered in api/server.py.

Reads signals + portfolio state from the stream-backed broker (no REST hits
on the hot path). Routes:
  GET  /api/crypto/signals          — signal radar (per asset)
  GET  /api/crypto/snapshot         — full dashboard payload (signals + portfolio + marks)
  GET  /api/crypto/portfolio        — equity, day P&L, open positions
  GET  /api/crypto/state            — runner state (kill switch, open positions detail)
  GET  /api/crypto/candles          — historical OHLCV for chart (Delta REST + 60s cache)
  GET  /api/crypto/signal-history   — recent width_pct samples for chart overlay
  POST /api/crypto/kill             — emergency stop
"""

from __future__ import annotations
import logging
import os
import time
from datetime import datetime, timezone
import requests
from dotenv import get_key, set_key
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPBearer
from api.auth import get_current_user

router = APIRouter(prefix="/api/crypto", tags=["crypto"])
_auth = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)

DELTA_BASE = os.environ.get("DELTA_BASE_URL", "https://api.india.delta.exchange")


def _signals_from_broker() -> tuple[list, dict]:
    """Stream-backed signal compute. Returns (signals, perp_marks).

    Reads the live price-action S/R strategy state from the runner's strategy
    instances. The dashboard and the runner see the same perp marks and the
    same 4h S/R range / trend context.
    """
    from core.brokers.delta_crypto import get_broker
    from core.execution.crypto_runner import _get_strategies
    broker = get_broker()
    signals: list = []
    perp_marks: dict = {}
    try:
        strategies = _get_strategies()
    except Exception:
        strategies = {}
    for name, strat in strategies.items():
        sym = getattr(strat, "symbol", None)
        if not sym: continue
        spot = broker.get_perp_mark(sym)
        if spot is None: continue
        perp_marks[sym] = spot
        state = strat.latest_state() if hasattr(strat, "latest_state") else {}
        decision = state.get("last_decision") or {}
        signals.append({
            "underlying": state.get("underlying") or sym.replace("USD", ""),
            "spot": spot,
            "width_pct": float(state.get("width_pct", 0) or 0),
            "r_high": float(state.get("r_high", 0) or 0),
            "r_low": float(state.get("r_low", 0) or 0),
            "trend": state.get("trend", "unknown"),
            "near_support": bool(state.get("near_support", False)),
            "near_resistance": bool(state.get("near_resistance", False)),
            "wick_touch_support": bool(state.get("wick_touch_support", False)),
            "wick_touch_resistance": bool(state.get("wick_touch_resistance", False)),
            "strong_green": bool(state.get("strong_green", False)),
            "strong_red": bool(state.get("strong_red", False)),
            "in_cooldown": bool(state.get("in_cooldown", False)),
            "sl_pct": float(state.get("sl_pct", 0) or 0),
            "tp_pct": float(state.get("tp_pct", 0) or 0),
            "vol_24h": float(state.get("vol_24h", 0) or 0),
            "vol_filter_ok": bool(state.get("vol_filter_ok", True)),
            "side": decision.get("side") if decision else None,
            "ready": bool(state.get("ready", False)),
        })
    return signals, perp_marks


def _build_crypto_snapshot() -> dict:
    """Single source of truth for both /api/crypto/signals and /ws/crypto.

    Bundles everything the crypto dashboard needs in one round-trip: live
    signals, portfolio state, perp marks, futures market stats (funding
    rate + OI), stream diagnostics, and recent shadow trades (would-have-
    fired entries blocked by an empty wallet).
    """
    signals, perp_marks = _signals_from_broker()
    portfolio = _portfolio_snapshot()
    futures_stats = _futures_stats_for_dashboard()
    shadow_trades: list = []
    shadow_summary: dict = {}
    missed_signals: list = []
    try:
        from core.execution.crypto_runner import get_state
        rs = get_state()
        shadow_trades = list(rs.get("shadow_trades", []))
        shadow_summary = dict(rs.get("shadow_summary", {}))
        missed_signals = list(rs.get("missed_signals", []))
    except Exception:
        pass
    try:
        from core.ws.delta_stream import get_stream
        stream = get_stream().diagnostics()
    except Exception:
        stream = {"connected": False}
    return {
        "ts":            datetime.now(timezone.utc).isoformat(),
        "perp_marks":    perp_marks,
        "signals":       signals,
        "portfolio":     portfolio,
        "futures_stats": futures_stats,
        "shadow_trades":  shadow_trades,
        "shadow_summary": shadow_summary,
        "missed_signals": missed_signals,
        "stream":         stream,
    }


def _futures_stats_for_dashboard() -> dict:
    """Just the BTC + ETH perp stats — funding rate + OI in USD + 24h vol."""
    try:
        from core.brokers.delta_crypto import get_broker
        broker = get_broker()
        all_stats = broker.get_futures_stats()
        return {sym: all_stats.get(sym, {}) for sym in ("BTCUSD", "ETHUSD")}
    except Exception:
        return {}


def _portfolio_snapshot() -> dict:
    """Live portfolio: total tradeable pool (USD + INR-converted), day P&L,
    open positions, capital-use percent. INR auto-converts on Delta at trade
    time so we report the combined pool size, not USD-only.
    """
    try:
        import os as _os
        from core.execution.crypto_runner import get_state, CAPITAL_USE_PCT
        from core.risk_management import FIXED_CAPITAL_MODE, FIXED_CAPITAL_INR
        from core.brokers.delta_crypto import get_broker
        state = get_state()
        broker = get_broker()
        wallet_usd = None
        wallet_inr = None
        wallet_pool = None
        if broker.mode == "live":
            try:
                breakdown = broker.get_wallet_breakdown()
                wallet_usd = float(breakdown.get("usd_total") or 0)
                wallet_inr = float(breakdown.get("inr_balance") or 0)
                rate = float(_os.environ.get("USD_INR_RATE", "86"))
                wallet_pool = wallet_usd + (wallet_inr / rate if rate > 0 else 0)
            except Exception:
                wallet_usd = wallet_inr = wallet_pool = None
        return {
            "wallet_usd":       wallet_usd,
            "wallet_inr":       wallet_inr,
            "wallet_pool_usd":  wallet_pool,         # USD + INR-converted
            "capital_use_pct":  float(CAPITAL_USE_PCT),
            "fixed_capital_mode": bool(FIXED_CAPITAL_MODE),
            "fixed_capital_inr": float(FIXED_CAPITAL_INR) if FIXED_CAPITAL_MODE else None,
            "day_pnl":          float(state.get("day_pnl_usd", 0) or 0),
            "open_positions":   len(state.get("open_positions", {})),
            "killed":           bool(state.get("killed", False)),
            "mode":             state.get("mode", "unknown"),
        }
    except Exception:
        return {"wallet_usd": None, "wallet_inr": None, "wallet_pool_usd": None,
                "capital_use_pct": 0.5, "fixed_capital_mode": False,
                "fixed_capital_inr": None, "day_pnl": 0.0,
                "open_positions": 0, "killed": False, "mode": "unknown"}


@router.get("/signals")
def crypto_signals():
    """Current price-action S/R state — ETH-only in this config."""
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
        from core.execution.crypto_runner import get_state
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


def _compute_poc_inline(df, bucket_size: float):
    """Volume-Profile POC / VAH / VAL via candle-body bucket overlap. Inlined
    here to avoid importing api.server (which pulls JWT deps unnecessarily)."""
    try:
        opens  = df["Open"].astype(float).values
        closes = df["Close"].astype(float).values
        if len(opens) < 10: return None, None, None
        p_min = float(min(min(opens), min(closes))) - bucket_size
        p_max = float(max(max(opens), max(closes))) + bucket_size
        buckets: dict = {}
        p = p_min
        while p <= p_max:
            buckets[round(p, 4)] = 0
            p += bucket_size
        bkeys = sorted(buckets.keys())
        for i in range(len(opens)):
            b_top = max(opens[i], closes[i])
            b_bot = min(opens[i], closes[i])
            if b_top == b_bot: continue
            for bk in bkeys:
                if bk < b_top and bk + bucket_size > b_bot:
                    buckets[bk] += 1
        if not any(buckets.values()): return None, None, None
        poc_key = max(buckets, key=buckets.get)
        poc = round(poc_key + bucket_size / 2, 2)
        total  = sum(buckets.values())
        target = total * 0.70
        accum  = 0
        included = set()
        for bk in sorted(buckets, key=buckets.get, reverse=True):
            included.add(bk); accum += buckets[bk]
            if accum >= target: break
        vah = round(max(included) + bucket_size, 2)
        val = round(min(included), 2)
        return poc, vah, val
    except Exception:
        return None, None, None


def _compute_chart_extras(candles: list, asset: str) -> dict:
    """HPS-style overlays on crypto candles: supply/demand zones, S/R levels,
    EMAs, POC/VAH/VAL. Reuses the generic core.sr_levels algorithm —
    tolerance + bucket_size scale by asset price so BTC's ~$63k and ETH's
    ~$1700 both get usable zone widths.
    """
    if not candles or len(candles) < 30:
        return {}
    try:
        import pandas as pd
        from core.sr_levels import compute_sr_levels

        df = pd.DataFrame(candles)
        df.columns = [c.capitalize() if c in ("open", "high", "low", "close",
                                              "volume") else c for c in df.columns]
        df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("dt")

        mean_price = float(df["Close"].mean())
        # Tolerance for level-clustering: 0.087% of mean price (matches NIFTY
        # 20 / 23000). bucket_size for POC: 0.11% of mean price.
        tolerance   = max(0.5, mean_price * 0.00087)
        bucket_size = max(0.5, mean_price * 0.0011)

        sr = compute_sr_levels(df.tail(200), tolerance=tolerance)
        poc, vah, val = _compute_poc_inline(df, bucket_size=bucket_size)

        # Sum volume traded WITHIN each zone's price band — supply zones with
        # more selling volume are stronger resistance; demand zones with more
        # buying volume are stronger support. Used by the frontend to scale
        # the rendered band opacity / outline weight per zone.
        def _zone_volume(z: dict) -> float:
            top, bot = z["top"], z["bottom"]
            mask = (df["High"] >= bot) & (df["Low"] <= top)
            return float(df.loc[mask, "Volume"].sum()) if mask.any() else 0.0

        for z in sr.get("supply_zones", []): z["volume"] = _zone_volume(z)
        for z in sr.get("demand_zones", []): z["volume"] = _zone_volume(z)

        max_vol = max(
            [z["volume"] for z in sr.get("supply_zones", [])
                              + sr.get("demand_zones", [])] or [1.0]
        )
        for z in sr.get("supply_zones", []): z["volume_norm"] = z["volume"] / max_vol
        for z in sr.get("demand_zones", []): z["volume_norm"] = z["volume"] / max_vol

        closes = df["Close"].astype(float)
        times  = [int(t) for t in (df.index.astype("int64") // 10**9)]
        def _ema(period: int) -> list:
            vals = closes.ewm(span=period, adjust=False).mean()
            return [{"time": t, "value": round(float(v), 2)}
                    for t, v in zip(times, vals) if v == v]

        return {
            "levels":             sr["levels"],
            "supply_zones":       sr["supply_zones"],
            "demand_zones":       sr["demand_zones"],
            "structure":          sr["structure"],
            "position":           sr["position"],
            "current_price":      sr["current_price"],
            "nearest_support":    sr["nearest_support"],
            "nearest_resistance": sr["nearest_resistance"],
            "ema20":              _ema(20),
            "ema50":              _ema(50),
            "ema200":             _ema(200),
            "poc":                poc,
            "vah":                vah,
            "val":                val,
        }
    except Exception as e:
        return {"extras_error": str(e)}


@router.get("/candles")
def crypto_candles(
    asset: str = Query("BTC", pattern="^(BTC|ETH)$"),
    resolution: str = Query("5m", pattern="^(1m|5m|15m|1h|4h|1d)$"),
    hours: int = Query(24, ge=1, le=720),
):
    """Historical OHLCV + HPS overlays (zones, S/R, EMAs, POC). 60s cache."""
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
        payload.update(_compute_chart_extras(candles, asset))
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
    """Width_pct samples for chart overlay. Combines two sources:

      1. Runner's in-memory _sig_history (every signal compute, all-day).
      2. Mongo crypto_signal_log (only gate-crossings — historic, may be empty).

    The in-memory source ensures the chart has a visible line even when no
    signals have crossed the gate yet. Returns [{ts, width_pct}] sorted asc.
    """
    samples: list[dict] = []
    # ── 1. in-memory _pred_trace from runner (every tick, ungated) ──────────
    # Bucket to 5-min boundaries so the chart payload is tractable: at 2s
    # ticks the raw trace is ~43k samples/day; bucketed it's ~288. We take
    # the LAST sample per bucket (rather than mean) so the chart reflects
    # the most recent value in that window.
    try:
        from core.execution.crypto_runner import _get_strategies
        # ETH-only: only eth_price_action_sr is instantiated.
        strat_name = "eth_price_action_sr" if asset == "ETH" else None
        strats = _get_strategies()
        strat = strats.get(strat_name) if strat_name else None
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
                samples.append({"ts": b, "width_pct": float(p)})
    except Exception:
        pass
    # ── 2. Mongo crypto_signal_log (gate-crossings only) ────────────────────
    try:
        from core import mongo
        from datetime import timedelta
        db = mongo.get_db()
        if db is not None:
            symbol = f"{asset}USD"
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            rows = list(db["crypto_signal_log"].find(
                {"venue": "delta_india", "symbol": symbol, "ts": {"$gte": cutoff}},
                projection={"_id": 0, "ts": 1, "pred_pct": 1},
            ).sort("ts", 1))
            for r in rows:
                ts = r.get("ts")
                if not hasattr(ts, "timestamp"): continue
                samples.append({"ts": int(ts.timestamp()),
                                "width_pct": float(r.get("pred_pct") or 0)})
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
        from core.execution.crypto_runner import manual_kill, get_state
        before = get_state()
        positions_before = list(before.get("open_positions", {}).keys())
        manual_kill()
        after = get_state()
        return {
            "ok": True,
            "killed_strategies": positions_before,
            "open_after": list(after.get("open_positions", {}).keys()),
            "kill_switch_armed": after.get("killed", True),
            "message": f"Killed {len(positions_before)} perp position(s). "
                       f"Bot will not enter new positions until restart.",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/test_buy_btc")
def crypto_test_buy_btc(user: str = Depends(get_current_user)):
    """Place a test market BUY order for 1 BTCUSD contract at 200x leverage.

    WARNING: This places a real order in live mode. Use only for API testing.
    The order is not tracked by the crypto runner; close it manually if it fills.
    """
    from core.brokers.delta_crypto import get_broker
    symbol = "BTCUSD"
    size = 1
    leverage = 200
    try:
        broker = get_broker()
        mark = broker.get_perp_mark(symbol)
        if mark is None or mark <= 0:
            raise HTTPException(status_code=503, detail="BTCUSD mark price unavailable")

        if broker.mode == "live":
            if not broker.set_leverage(symbol, leverage):
                raise HTTPException(
                    status_code=400,
                    detail=f"Could not set {symbol} leverage to {leverage}x",
                )

        result = broker.place_order(
            symbol=symbol,
            side="buy",
            size=size,
            order_type="market_order",
            leverage=None,
            tag="dashboard_test",
        )
        logger.warning(
            "Crypto test buy BTC by %s | mark=%s size=%s leverage=%s | result=%s",
            user, mark, size, leverage, result,
        )
        return {
            "symbol": symbol,
            "side": "buy",
            "size": size,
            "leverage": leverage,
            "mark_price": mark,
            "mode": broker.mode,
            "order_response": result,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Crypto test buy BTC failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


# ── strategy / instrument toggles ────────────────────────────────────────────

# ── dashboard-managed env overrides ───────────────────────────────────────────
# These knobs are normally set in .env. The dashboard exposes them as switches
# and inputs so operators don't have to SSH in for routine changes. Because the
# container environment is loaded at startup, any change here requires a
# container restart to take effect.

_ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))

_MANAGED_SETTINGS = {
    "ENABLE_CRYPTO_RUNNER": {"type": "bool", "default": "true"},
    "CRYPTO_TRADING_MODE":  {"type": "choice", "choices": {"live", "paper"}, "default": "live"},
    "DELTA_API_KEY":        {"type": "secret"},
    "DELTA_API_SECRET":     {"type": "secret"},
    "CRYPTO_EQUITY_USD":    {"type": "float", "default": "1000"},
}

_SECRET_PLACEHOLDER = "********"


def _read_managed_setting(key: str, meta: dict):
    raw = get_key(_ENV_PATH, key)
    if raw is None:
        raw = meta.get("default", "")
    raw = raw.strip()
    if meta["type"] == "bool":
        return raw.lower() in ("1", "true", "yes", "on")
    if meta["type"] == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return float(meta.get("default", "0"))
    if meta["type"] == "secret":
        return bool(raw and raw != _SECRET_PLACEHOLDER)
    # choice / str
    return raw if raw else meta.get("default", "")


@router.get("/settings")
def crypto_settings(user: str = Depends(get_current_user)):
    """Current dashboard-managed env values. Secrets are returned as present/absent."""
    try:
        values = {k: _read_managed_setting(k, m) for k, m in _MANAGED_SETTINGS.items()}
        return {
            "values": values,
            "requires_restart": list(_MANAGED_SETTINGS.keys()),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/settings")
def update_crypto_settings(body: dict, user: str = Depends(get_current_user)):
    """Update dashboard-managed env values in .env. Restart required to apply."""
    updated: list[str] = []
    errors: list[str] = []
    for key, meta in _MANAGED_SETTINGS.items():
        if key not in body:
            continue
        value = body[key]
        if meta["type"] == "bool":
            value = "true" if value else "false"
        elif meta["type"] == "float":
            try:
                value = str(float(value))
            except (TypeError, ValueError):
                errors.append(f"{key} must be a number")
                continue
        elif meta["type"] == "choice":
            if value not in meta["choices"]:
                errors.append(f"{key} must be one of {sorted(meta['choices'])}")
                continue
            value = str(value)
        elif meta["type"] == "secret":
            if not value or value == _SECRET_PLACEHOLDER:
                continue
            value = str(value)
        else:
            value = str(value)
        try:
            set_key(_ENV_PATH, key, value, quote_mode="never")
            updated.append(key)
        except Exception as e:
            errors.append(f"{key}: {e}")
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    return {
        "ok": True,
        "updated": updated,
        "requires_restart": list(_MANAGED_SETTINGS.keys()),
        "values": {k: _read_managed_setting(k, m) for k, m in _MANAGED_SETTINGS.items()},
    }


@router.get("/strategies")
def crypto_strategies(user: str = Depends(get_current_user)):
    """List all strategies and their instruments with enable/disable state."""
    try:
        from core.strategy_toggles import list_strategies
        return {"strategies": list_strategies()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/strategies/{name}/enable")
def enable_strategy(name: str, user: str = Depends(get_current_user)):
    """Enable a strategy (allows new entries for its instruments)."""
    try:
        from core.strategy_toggles import set_strategy_enabled
        cfg = set_strategy_enabled(name, True)
        return {"ok": True, "strategy": name, "enabled": True, "config": cfg}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/strategies/{name}/disable")
def disable_strategy(name: str, user: str = Depends(get_current_user)):
    """Disable a strategy (no new entries; existing positions still managed)."""
    try:
        from core.strategy_toggles import set_strategy_enabled
        cfg = set_strategy_enabled(name, False)
        return {"ok": True, "strategy": name, "enabled": False, "config": cfg}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/strategies/{name}/instruments/{instrument}/enable")
def enable_instrument(name: str, instrument: str, user: str = Depends(get_current_user)):
    """Enable a specific instrument within a strategy."""
    try:
        from core.strategy_toggles import set_instrument_enabled
        cfg = set_instrument_enabled(name, instrument, True)
        return {"ok": True, "strategy": name, "instrument": instrument, "enabled": True, "config": cfg}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/strategies/{name}/instruments/{instrument}/disable")
def disable_instrument(name: str, instrument: str, user: str = Depends(get_current_user)):
    """Disable a specific instrument within a strategy."""
    try:
        from core.strategy_toggles import set_instrument_enabled
        cfg = set_instrument_enabled(name, instrument, False)
        return {"ok": True, "strategy": name, "instrument": instrument, "enabled": False, "config": cfg}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
