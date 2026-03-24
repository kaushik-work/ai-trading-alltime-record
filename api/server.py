import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm

import config
from api.auth import verify_password, create_token, get_current_user, DASHBOARD_USER
from core.memory import init_db, TradeMemory
from core.records import init_records_db, RecordTracker
from core import ipc
from data.market import RealMarketData
from api.broadcaster import manager

app = FastAPI(title="Trading Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in prod to your Vercel URL
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()
init_records_db()
memory = TradeMemory()
records = RecordTracker()
market = RealMarketData()

WATCHLIST = ["NIFTY", "BANKNIFTY"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_snapshot() -> dict:
    all_trades   = memory.get_all_trades(limit=500)
    today_trades = memory.get_today_trades()
    all_records  = records.get_all_records()

    total_pnl  = sum(t.get("pnl", 0) for t in all_trades)
    today_pnl  = sum(t.get("pnl", 0) for t in today_trades)
    win_trades = sum(1 for t in all_trades if t.get("pnl", 0) > 0)
    win_rate   = round(win_trades / len(all_trades) * 100, 1) if all_trades else 0
    open_pos   = [t for t in all_trades if t.get("side") == "BUY" and not t.get("closed_at")]

    wins_today   = sum(1 for t in today_trades if t.get("pnl", 0) > 0)
    losses_today = sum(1 for t in today_trades if t.get("pnl", 0) < 0)

    # Equity curve
    sell_trades = [t for t in all_trades if t.get("side") == "SELL" and t.get("pnl") is not None]
    cumulative = 0
    equity_curve = []
    for t in sorted(sell_trades, key=lambda x: x.get("timestamp", "")):
        cumulative += t.get("pnl", 0)
        equity_curve.append({"timestamp": t.get("timestamp"), "pnl": round(cumulative, 2)})

    # Live prices
    prices = {}
    for sym in WATCHLIST:
        try:
            q = market.get_quote(sym)
            prices[sym] = {
                "price": q.get("last_price", 0),
                "change_pct": round(q.get("change_pct", 0), 2),
                "source": q.get("source", ""),
            }
        except Exception:
            prices[sym] = {"price": 0, "change_pct": 0, "source": "error"}

    return {
        "timestamp": datetime.now().isoformat(),
        "bot_status": "paused" if ipc.flag_exists(ipc.FLAG_PAUSE) else "running",
        "mode": "paper" if config.IS_PAPER else "live",
        "pnl": {
            "total": round(total_pnl, 2),
            "today": round(today_pnl, 2),
            "win_rate": win_rate,
            "total_trades": len(all_trades),
            "today_trades": len(today_trades),
            "wins_today": wins_today,
            "losses_today": losses_today,
            "open_positions": len(open_pos),
        },
        "prices": prices,
        "recent_trades": all_trades[:20],
        "open_positions": open_pos,
        "equity_curve": equity_curve[-100:],  # last 100 points
        "records": [
            {"description": r["description"], "value": r["value"],
             "symbol": r.get("symbol") or "-", "date": r["achieved_at"][:10]}
            for r in all_records.values()
        ] if all_records else [],
    }


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.post("/api/auth/token")
def login(form: OAuth2PasswordRequestForm = Depends()):
    if form.username != DASHBOARD_USER or not verify_password(form.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"access_token": create_token(form.username), "token_type": "bearer"}

@app.get("/api/snapshot")
def snapshot(user: str = Depends(get_current_user)):
    return _build_snapshot()

@app.get("/api/pnl")
def pnl_report(start: str = None, end: str = None, user: str = Depends(get_current_user)):
    """Return trades filtered by date range for the PPnL page."""
    all_trades = memory.get_all_trades(limit=2000)
    if start:
        all_trades = [t for t in all_trades if t.get("timestamp", "") >= start]
    if end:
        # include full end date
        all_trades = [t for t in all_trades if t.get("timestamp", "") <= end + "T23:59:59"]

    # Group by date for daily summary
    from collections import defaultdict
    daily: dict = defaultdict(lambda: {"trades": [], "total_pnl": 0, "wins": 0, "losses": 0})
    for t in all_trades:
        ts = t.get("timestamp", "")
        date = ts[:10] if ts else "unknown"
        daily[date]["trades"].append(t)
        pnl = t.get("pnl") or 0
        daily[date]["total_pnl"] = round(daily[date]["total_pnl"] + pnl, 2)
        if pnl > 0: daily[date]["wins"] += 1
        elif pnl < 0: daily[date]["losses"] += 1

    daily_summary = [
        {"date": d, **v, "trades": v["trades"]}
        for d, v in sorted(daily.items(), reverse=True)
    ]

    total_pnl  = round(sum(t.get("pnl") or 0 for t in all_trades), 2)
    win_trades = sum(1 for t in all_trades if (t.get("pnl") or 0) > 0)
    win_rate   = round(win_trades / len(all_trades) * 100, 1) if all_trades else 0

    return {
        "total_pnl": total_pnl,
        "total_trades": len(all_trades),
        "win_rate": win_rate,
        "daily": daily_summary,
        "trades": all_trades,
    }

@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat()}

@app.post("/api/bot/pause")
def pause_bot(user: str = Depends(get_current_user)):
    ipc.write_flag(ipc.FLAG_PAUSE)
    ipc.clear_flag(ipc.FLAG_RESUME)
    return {"status": "paused"}

@app.post("/api/bot/resume")
def resume_bot(user: str = Depends(get_current_user)):
    ipc.write_flag(ipc.FLAG_RESUME)
    ipc.clear_flag(ipc.FLAG_PAUSE)
    return {"status": "running"}

@app.post("/api/trade/force")
async def force_trade(body: dict, user: str = Depends(get_current_user)):
    symbol   = body.get("symbol")
    side     = body.get("side")
    quantity = int(body.get("quantity", 1))
    reason   = body.get("reason", "Manual override from dashboard")
    if ipc.flag_exists(ipc.FLAG_FORCE_TRADE):
        return {"error": "A trade is already queued"}
    ipc.write_force_trade(symbol, side, quantity, reason)
    return {"status": "queued", "symbol": symbol, "side": side, "quantity": quantity}


@app.post("/api/backtest")
def run_backtest(body: dict, user: str = Depends(get_current_user)):
    from backtesting.engine import BacktestEngine
    from backtesting.metrics import compute_metrics
    symbol          = body.get("symbol", "NIFTY")
    period          = body.get("period", "60d")
    interval        = body.get("interval", "15m")
    initial_capital = float(body.get("capital", 20000))
    min_score       = int(body.get("min_score", 7))
    risk_pct        = float(body.get("risk_pct", 2.0))
    daily_loss      = float(body.get("daily_loss_limit_pct", 3.0))
    rr_ratio        = float(body.get("rr_ratio", 2.0))
    strategy        = body.get("strategy", "ATR Intraday")
    try:
        engine  = BacktestEngine(initial_capital=initial_capital)
        result  = engine.run(symbol, period=period, interval=interval,
                             min_score=min_score, risk_pct=risk_pct,
                             daily_loss_limit_pct=daily_loss, rr_ratio=rr_ratio,
                             strategy=strategy)
        metrics = compute_metrics(result["trades"], result["equity_curve"],
                                  result["initial_capital"])
        return {"result": result, "metrics": metrics}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        # Send initial snapshot immediately on connect
        await ws.send_text(json.dumps(_build_snapshot()))
        while True:
            await asyncio.sleep(5)
            await ws.send_text(json.dumps(_build_snapshot()))
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=True)
