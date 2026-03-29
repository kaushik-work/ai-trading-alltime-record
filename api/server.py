import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,
)
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm

import config
from api.auth import verify_password, create_token, get_current_user, DASHBOARD_USER
from core.memory import init_db, TradeMemory
from core.records import init_records_db, RecordTracker
from core import ipc
from core.bot_runner import get_runner
from data.market import RealMarketData
from api.broadcaster import manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──────────────────────────────────────────────────────────────
    init_db()
    init_records_db()
    runner = get_runner()
    runner.start()
    yield
    # ── shutdown ─────────────────────────────────────────────────────────────
    runner.stop()


app = FastAPI(title="Trading Bot API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in prod to your Vercel URL
    allow_methods=["*"],
    allow_headers=["*"],
)

memory = TradeMemory()
records = RecordTracker()
market = RealMarketData()

WATCHLIST = ["NIFTY"]


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

    # Per-strategy daily summary
    STRATEGIES = ["Musashi", "Raijin", "ATR Intraday"]
    strategy_summary = {}
    for strat in STRATEGIES:
        strat_trades = [t for t in today_trades if t.get("strategy") == strat and t.get("status") == "COMPLETE"]
        strat_pnl    = round(sum(t.get("pnl", 0) for t in strat_trades), 2)
        strat_wins   = sum(1 for t in strat_trades if t.get("pnl", 0) > 0)
        strategy_summary[strat] = {
            "trades": len(strat_trades),
            "pnl":    strat_pnl,
            "wins":   strat_wins,
            "losses": len(strat_trades) - strat_wins,
        }

    # Today's completed journal (closed trades with full detail)
    today_journal = [
        {
            "strategy":    t.get("strategy", "—"),
            "symbol":      t.get("symbol"),
            "option_type": t.get("option_type", "—"),
            "strike":      t.get("strike"),
            "side":        t.get("side"),
            "lot_size":    t.get("lot_size", 65),
            "entry_price": t.get("price"),
            "pnl":         round(t.get("pnl", 0), 2),
            "close_reason": t.get("close_reason", "—"),
            "score":        t.get("score"),
            "entry_time":   t.get("timestamp"),
            "exit_time":    t.get("closed_at"),
            "status":       t.get("status"),
            "entry_remark": t.get("entry_remark"),
            "exit_remark":  t.get("exit_remark"),
        }
        for t in today_trades
        if t.get("status") == "COMPLETE" and t.get("strategy")
    ]

    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(IST)
    market_open = (
        now_ist.weekday() < 5
        and __import__("datetime").time(9, 15) <= now_ist.time() <= __import__("datetime").time(15, 30)
    )

    runner = get_runner()
    scheduler_ok = runner.scheduler.running
    if ipc.flag_exists(ipc.FLAG_PAUSE):
        bot_status = "paused"
    elif not scheduler_ok:
        bot_status = "stopped"
    elif not market_open:
        bot_status = "market_closed"
    else:
        bot_status = "running"

    return {
        "timestamp": now_ist.isoformat(),
        "bot_status": bot_status,
        "scheduler_running": scheduler_ok,
        "market_open": market_open,
        "last_heartbeat": runner.last_heartbeat,
        "last_scores": runner.last_scores,
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
        "strategy_summary": strategy_summary,
        "today_journal":    today_journal,
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
    from zoneinfo import ZoneInfo
    return {"status": "ok", "time": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()}

@app.get("/api/bot/debug")
def bot_debug(user: str = Depends(get_current_user)):
    """Live signal scores for all 3 strategies — no trades placed."""
    from zoneinfo import ZoneInfo
    from core.bot_runner import _fetch_intraday, _is_market_hours, IST
    from strategies.nifty_intraday import score_signal as musashi_score, SCORE_THRESHOLD as M_THRESH
    from strategies.nifty_scalp import score_signal as raijin_score, SCORE_THRESHOLD as R_THRESH
    runner = get_runner()
    now_ist = datetime.now(IST)
    result = {"time_ist": now_ist.isoformat(), "market_open": _is_market_hours(),
              "last_heartbeat": runner.last_heartbeat, "last_scores": runner.last_scores,
              "strategies": {}}
    for name, fetch_interval, scorer, thresh in [
        ("Musashi",      "15m", musashi_score, M_THRESH),
        ("Raijin",       "5m",  raijin_score,  R_THRESH),
    ]:
        try:
            data = _fetch_intraday("NIFTY", fetch_interval)
            if data is None:
                result["strategies"][name] = {"error": "yfinance returned None"}
                continue
            opens, highs, lows, closes, volumes, all_closes, bar_time = data
            sig = scorer(opens, highs, lows, closes, volumes, all_closes)
            result["strategies"][name] = {
                "buy_score": sig.get("buy_score"), "sell_score": sig.get("sell_score"),
                "action": sig.get("action"), "threshold": thresh,
                "will_trade": sig.get("action") != "HOLD",
                "bar_time_ist": str(bar_time), "bars": len(closes),
                "details": sig.get("details", {}),
            }
        except Exception as e:
            result["strategies"][name] = {"error": str(e)}

    # ATR Intraday uses TrendStrategy (signal_scorer) — get its last known state
    try:
        from strategies.signal_scorer import SignalScorer
        from data.market import RealMarketData
        mkt = RealMarketData()
        indicators = mkt.get_indicators("NIFTY")
        scorer_atr = SignalScorer()
        score_val, direction = scorer_atr.score(indicators)
        result["strategies"]["ATR Intraday"] = {
            "score": score_val, "direction": direction,
            "threshold": 7, "will_trade": abs(score_val) >= 7,
            "note": "score range -10 to +10, threshold ±7",
        }
    except Exception as e:
        result["strategies"]["ATR Intraday"] = {"error": str(e)}

    return result

@app.post("/api/bot/pause")
def pause_bot(user: str = Depends(get_current_user)):
    ipc.write_flag(ipc.FLAG_PAUSE)
    ipc.clear_flag(ipc.FLAG_RESUME)
    return {"status": "paused"}

@app.post("/api/bot/resume")
def resume_bot(user: str = Depends(get_current_user)):
    ipc.clear_flag(ipc.FLAG_PAUSE)
    ipc.write_flag(ipc.FLAG_RESUME)
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


@app.get("/api/journals")
def list_journals(user: str = Depends(get_current_user)):
    """List all saved daily journal dates (newest first)."""
    from core.journal import list_journals as _list
    return {"dates": _list()}

@app.get("/api/journals/{date}")
def get_journal(date: str, user: str = Depends(get_current_user)):
    """Return a specific day's journal JSON (date format: YYYY-MM-DD)."""
    from core.journal import load_journal
    journal = load_journal(date)
    if journal is None:
        raise HTTPException(status_code=404, detail=f"No journal found for {date}")
    return journal

@app.patch("/api/journals/{date}/notes")
def update_notes(date: str, body: dict, user: str = Depends(get_current_user)):
    """Save/update learning notes for a day's journal."""
    from core.journal import update_learning_notes
    notes = body.get("notes", "")
    ok = update_learning_notes(date, notes)
    if not ok:
        raise HTTPException(status_code=404, detail=f"No journal found for {date}")
    return {"status": "saved", "date": date}

@app.post("/api/journals/save-now")
def save_journal_now(user: str = Depends(get_current_user)):
    """Manually trigger today's journal save."""
    from core.journal import save_daily_journal
    path = save_daily_journal()
    return {"status": "saved", "path": path}


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
