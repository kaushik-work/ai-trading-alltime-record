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


# ── Run ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=True)
