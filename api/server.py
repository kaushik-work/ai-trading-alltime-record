"""
FastAPI app — crypto-only

The legacy NSE / NIFTY trading endpoints have been retired. The only
trading surface is the crypto bot (v5.5 synth-forward on Delta India).
The NSE option-chain collectors are still available via the `nse` docker
compose profile but they no longer touch the API.

Surfaces:
    POST /api/auth/token       — JWT login
    GET  /api/health           — liveness probe
    GET  /api/crypto/*         — crypto dashboard data (see routes_crypto)
    POST /api/crypto/kill      — emergency stop the bot
    WS   /ws/crypto            — push live dashboard snapshot every 1s
"""

import asyncio
import json
import logging
import math
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import OAuth2PasswordRequestForm

from api.auth import verify_password, create_token, decode_token, DASHBOARD_USER
from api.routes_crypto import router as crypto_router
from api.routes_nse import router as nse_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──────────────────────────────────────────────────────────────
    # APScheduler lives inside BotRunner — we still use it as the host for the
    # crypto runner's tick jobs. NSE jobs (option-chain refresh + journals)
    # have been gutted; BotRunner just provides the scheduler now.
    from core.bot_runner import get_runner
    from core.execution.crypto_runner import init_crypto_runner
    from core.ws.delta_stream import start_stream, stop_stream
    from core.risk_management import ENABLE_CRYPTO_RUNNER

    runner = get_runner()
    runner.start()
    # Delta WS stream feeds the crypto runner with real-time perp + option
    # marks. Must start BEFORE the runner so the first tick has fresh data.
    if ENABLE_CRYPTO_RUNNER:
        start_stream()
    init_crypto_runner(runner.scheduler)
    # NSE synthetic-forward runner (paper by default).
    try:
        from nse.execution.nse_runner import init_nse_runner
        init_nse_runner(runner.scheduler)
    except Exception as e:
        logger.error("Failed to initialize NSE runner: %s", e)
    yield
    # ── shutdown ─────────────────────────────────────────────────────────────
    runner.stop()
    stop_stream()


app = FastAPI(title="Trading Bot API", lifespan=lifespan)

# Hard-coded origins that must ALWAYS work regardless of env overrides.
# Prevents accidental misconfiguration of DASHBOARD_ORIGINS from breaking prod.
_ALWAYS_ALLOWED_ORIGINS = {
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://ai-trading-alltime-record.vercel.app",
}
_env_origins = {
    origin.strip()
    for origin in os.getenv("DASHBOARD_ORIGINS", "").split(",")
    if origin.strip()
}
_cors_origins = sorted(_ALWAYS_ALLOWED_ORIGINS | _env_origins)

# Also allow Vercel preview deployments — every PR gets a unique URL like
# ai-trading-alltime-record-git-<branch>-<team>.vercel.app
_VERCEL_PREVIEW_REGEX = r"^https://ai-trading-alltime-record(-[a-z0-9\-]+)?\.vercel\.app$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=_VERCEL_PREVIEW_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(crypto_router)
app.include_router(nse_router)


# ── Auth + Health ────────────────────────────────────────────────────────────
@app.post("/api/auth/token")
def login(form: OAuth2PasswordRequestForm = Depends()):
    if form.username != DASHBOARD_USER or not verify_password(form.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"access_token": create_token(form.username), "token_type": "bearer"}


@app.get("/api/health")
def health():
    from zoneinfo import ZoneInfo
    return {"status": "ok", "time": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()}


# ── Safe JSON (handles numpy types, NaN, Inf) ────────────────────────────────
def _safe_json(obj) -> str:
    def default(o):
        try:
            import numpy as np
            if isinstance(o, np.bool_):    return bool(o)
            if isinstance(o, np.integer):  return int(o)
            if isinstance(o, np.floating):
                return None if (math.isnan(float(o)) or math.isinf(float(o))) else float(o)
            if isinstance(o, np.ndarray):  return o.tolist()
        except ImportError:
            pass
        if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
            return None
        raise TypeError(f"Object of type {type(o)} is not JSON serializable")
    return json.dumps(obj, default=default)


# ── WebSocket ────────────────────────────────────────────────────────────────
@app.websocket("/ws/crypto")
async def crypto_websocket_endpoint(ws: WebSocket, token: str = Query(default="")):
    """Live crypto dashboard stream. Pushes the full snapshot every second.

    Snapshot = signals + portfolio + perp_marks + futures stats + shadow
    trades + stream diagnostics. Source data is the WS-backed broker, so
    payloads reflect sub-second mark changes without REST hits.
    """
    await ws.accept()
    try:
        decode_token(token)
    except Exception:
        await ws.close(code=1008, reason="Invalid or expired token")
        return
    from api.routes_crypto import _build_crypto_snapshot
    try:
        await ws.send_text(_safe_json(_build_crypto_snapshot()))
        while True:
            await asyncio.sleep(1)
            await ws.send_text(_safe_json(_build_crypto_snapshot()))
    except WebSocketDisconnect:
        return
    except Exception as e:
        logger.error("crypto WebSocket error (disconnecting): %s", e)


@app.websocket("/ws/nse/chain")
async def nse_chain_websocket(ws: WebSocket, token: str = Query(default=""),
                              symbol: str = Query(default="NIFTY"),
                              strikes: int = Query(default=10)):
    """Tick-driven option chain. Replaces 3-second polling.

    Cadence is bounded by physics, not by choice. Angel's SNAP_QUOTE delivers a
    few updates per second per contract and browser round-trip alone is
    10-50ms, so "every microsecond" is not on offer from any retail feed. What
    IS achievable, and what this does, is remove the polling delay entirely:
    the socket cache is read at CHAIN_PUSH_HZ and anything that changed goes
    out immediately. The screen is never more than ~100ms behind the exchange.

    Payloads are diffed. The first frame is a full snapshot; after that only
    strikes whose numbers actually moved are sent, which keeps a 40-contract
    chain at a few hundred bytes per frame instead of ~15KB.
    """
    await ws.accept()
    try:
        decode_token(token)
    except Exception:
        await ws.close(code=1008, reason="Invalid or expired token")
        return

    from api.routes_nse import build_chain_payload

    CHAIN_PUSH_HZ = 10.0
    interval = 1.0 / CHAIN_PUSH_HZ
    last_rows: dict = {}
    last_header: dict = {}
    first = True

    # Subscribing is best-effort: outside market hours it fails and the builder
    # simply falls back to REST, which is the correct degraded behaviour rather
    # than an error the user has to interpret.
    logger.info("nse chain WS: accepted symbol=%s strikes=%d", symbol, strikes)
    try:
        from nse.ws.angel_stream import ensure_subscribed
        await asyncio.wait_for(
            asyncio.to_thread(ensure_subscribed, symbol, strikes), timeout=30)
        logger.info("nse chain WS: subscribe done")
    except Exception as e:
        # Never let a slow or stuck subscribe block the push loop: the builder
        # falls back to REST on its own, and a chain arriving over the fallback
        # is far better than a socket that silently never sends anything.
        logger.info("nse chain WS: stream subscribe skipped (%s: %s)",
                    type(e).__name__, e)

    try:
        while True:
            try:
                payload = await asyncio.wait_for(
                    asyncio.to_thread(build_chain_payload, symbol, strikes),
                    timeout=15)
            except Exception as e:
                logger.warning("nse chain WS: build failed (%s: %s)",
                               type(e).__name__, e)
                await ws.send_text(_safe_json({"type": "error", "message": str(e)}))
                await asyncio.sleep(2.0)
                continue
            if first:
                logger.info("nse chain WS: first payload built, %d rows, source=%s",
                            len(payload.get("rows", [])), payload.get("source"))

            rows = {r["strike"]: r for r in payload["rows"]}
            header = {k: v for k, v in payload.items() if k != "rows"}

            if first:
                await ws.send_text(_safe_json({"type": "snapshot", **payload}))
                first = False
            else:
                changed = [r for k, r in rows.items() if last_rows.get(k) != r]
                if changed or header != last_header:
                    await ws.send_text(_safe_json({
                        "type": "patch", "rows": changed, **header,
                    }))
            last_rows, last_header = rows, header
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        return
    except Exception as e:
        logger.error("nse chain WebSocket error (disconnecting): %s", e)


# ── Run ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=True)
