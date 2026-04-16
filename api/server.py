import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
import logging
import math

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import OAuth2PasswordRequestForm

import config
from api.auth import verify_password, create_token, get_current_user, decode_token, DASHBOARD_USER
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

_cors_origins = [
    origin.strip()
    for origin in os.getenv("DASHBOARD_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

memory = TradeMemory()
records = RecordTracker()
market = RealMarketData()

WATCHLIST = ["NIFTY"]
STRATEGIES = ["ATR Intraday", "C-ICT", "Fib-OF"]

# ── Price cache — shared across all WebSocket connections ─────────────────────
# Without this, 5 open browser tabs × every-5s broadcast = 60 Zerodha calls/min
import time as _time
_price_cache: dict = {}
_price_cache_ts: float = 0.0
_PRICE_TTL = 30  # seconds — refresh live price at most once every 30s

def _get_prices() -> dict:
    global _price_cache, _price_cache_ts
    if _time.time() - _price_cache_ts < _PRICE_TTL:
        return _price_cache
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
    _price_cache    = prices
    _price_cache_ts = _time.time()
    return prices


def _latest_order_issue() -> dict | None:
    from core.zerodha_error_log import get_all
    for item in get_all():
        if item.get("source") in {"live_order_preflight", "live_order_rejected"}:
            return item
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_snapshot() -> dict:
    all_trades   = memory.get_all_trades(limit=500)
    today_trades = memory.get_today_trades()
    all_round_trips = memory.build_round_trips(all_trades)
    today_round_trips = memory.build_round_trips(today_trades)
    all_records  = records.get_all_records()

    total_pnl  = sum(t.get("pnl", 0) for t in all_trades)
    today_pnl  = sum(t.get("pnl", 0) for t in today_round_trips)
    win_trades = sum(1 for t in all_round_trips if t.get("pnl", 0) > 0)
    win_rate   = round(win_trades / len(all_round_trips) * 100, 1) if all_round_trips else 0
    open_pos   = [t for t in all_trades if t.get("side") == "BUY" and not t.get("closed_at")]

    wins_today   = sum(1 for t in today_round_trips if t.get("pnl", 0) > 0)
    losses_today = sum(1 for t in today_round_trips if t.get("pnl", 0) < 0)

    # Equity curve
    sell_trades = [t for t in all_trades if t.get("side") == "SELL" and t.get("pnl") is not None]
    cumulative = 0
    equity_curve = []
    for t in sorted(sell_trades, key=lambda x: x.get("timestamp", "")):
        cumulative += t.get("pnl", 0)
        equity_curve.append({"timestamp": t.get("timestamp"), "pnl": round(cumulative, 2)})

    # Live prices — cached for 30s, shared across all WebSocket connections
    prices = _get_prices()

    # Per-strategy daily summary
    strategy_summary = {}
    for strat in STRATEGIES:
        strat_trades = [t for t in today_round_trips if t.get("strategy") == strat]
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
            "strategy":     t.get("strategy", "—"),
            "symbol":       t.get("symbol"),
            "underlying":   t.get("underlying"),
            "option_type":  t.get("option_type", "—"),
            "strike":       t.get("strike"),
            "expiry":       t.get("expiry"),
            "side":         t.get("side", "BUY"),
            "lot_size":     t.get("lot_size", 65),
            "entry_price":  t.get("entry_price"),
            "exit_price":   t.get("exit_price"),
            "pnl":          round(t.get("pnl", 0), 2),
            "close_reason": t.get("close_reason", "—"),
            "score":        t.get("score"),
            "entry_time":   t.get("entry_time"),
            "exit_time":    t.get("exit_time"),
            "status":       t.get("status"),
            "entry_remark": t.get("entry_remark"),
            "exit_remark":  t.get("exit_remark"),
        }
        for t in today_round_trips
    ]

    open_trade_feed = [
        {
            "symbol":       t.get("underlying") or t.get("symbol"),
            "contract_symbol": t.get("symbol"),
            "underlying":   t.get("underlying"),
            "option_type":  t.get("option_type"),
            "strike":       t.get("strike"),
            "expiry":       t.get("expiry"),
            "strategy":     t.get("strategy"),
            "buy_price":    t.get("price"),
            "sell_price":   None,
            "qty":          t.get("quantity"),
            "pnl":          None,
            "status":       "OPEN",
            "entry_time":   t.get("timestamp"),
            "exit_time":    None,
            "activity_time": t.get("timestamp"),
        }
        for t in all_trades
        if t.get("side") == "BUY" and not t.get("closed_at") and t.get("strategy")
    ]

    closed_trade_feed = [
        {
            "symbol":         t.get("underlying") or t.get("symbol"),
            "contract_symbol": t.get("symbol"),
            "underlying":     t.get("underlying"),
            "option_type":    t.get("option_type"),
            "strike":         t.get("strike"),
            "expiry":         t.get("expiry"),
            "strategy":       t.get("strategy"),
            "buy_price":      t.get("entry_price"),
            "sell_price":     t.get("exit_price"),
            "qty":            t.get("quantity"),
            "pnl":            t.get("pnl"),
            "status":         t.get("status"),
            "entry_time":     t.get("entry_time"),
            "exit_time":      t.get("exit_time"),
            "activity_time":  t.get("exit_time") or t.get("entry_time"),
        }
        for t in all_round_trips
    ]

    recent_activity = sorted(
        open_trade_feed + closed_trade_feed,
        key=lambda x: x.get("activity_time", "") or "",
        reverse=True,
    )

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
        "token_set_at": _get_token_status(),
        "day_bias": runner.last_day_bias,
        "mode": "paper" if config.IS_PAPER else "live",
        "pnl": {
            "total": round(total_pnl, 2),
            "today": round(today_pnl, 2),
            "win_rate": win_rate,
            "total_trades": len(all_round_trips),
            "today_trades": len(today_round_trips),
            "wins_today": wins_today,
            "losses_today": losses_today,
            "open_positions": len(open_pos),
        },
        "strategy_summary": strategy_summary,
            "today_journal":    today_journal,
            "prices": prices,
            "recent_trades": all_trades[:20],
            "recent_activity": recent_activity[:20],
            "round_trips":   all_round_trips[:20],
        "zerodha_error_count": len(__import__("core.zerodha_error_log", fromlist=["get_all"]).get_all()),
        "latest_order_issue": _latest_order_issue(),
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
    return Response(content=_safe_json(_build_snapshot()), media_type="application/json")

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

_token_cache: dict = {"result": None, "checked_at": 0.0}
_TOKEN_CACHE_TTL = 300  # re-check token liveness every 5 minutes

def _get_token_status() -> dict:
    """Check Zerodha token liveness (cached for 5 minutes to avoid hammering the API)."""
    import time
    now = time.monotonic()
    if now - _token_cache["checked_at"] < _TOKEN_CACHE_TTL and _token_cache["result"] is not None:
        return _token_cache["result"]
    try:
        from data.zerodha_fetcher import ZerodhaFetcher
        live = ZerodhaFetcher.get().is_token_live()
        if live:
            from dotenv import dotenv_values
            env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
            set_at = dotenv_values(env_path).get("ZERODHA_TOKEN_SET_AT") or None
            result = {"live": True, "set_at": set_at}
        else:
            result = {"live": False, "set_at": None}
    except Exception as e:
        logger.error("Token liveness check failed: %s", e, exc_info=True)
        result = {"live": False, "set_at": None}
    _token_cache["result"] = result
    # Only cache live=True for the full TTL; expired token retries after 30s
    _token_cache["checked_at"] = now if result["live"] else now - _TOKEN_CACHE_TTL + 30
    return result


@app.post("/api/token/refresh")
def token_refresh(user: str = Depends(get_current_user)):
    """Force-clear the token cache and ZerodhaFetcher state, then re-check liveness."""
    import traceback
    # Clear server-level cache
    _token_cache["result"] = None
    _token_cache["checked_at"] = 0.0
    # Reset ZerodhaFetcher so it re-reads .env from scratch
    try:
        from data.zerodha_fetcher import ZerodhaFetcher
        inst = ZerodhaFetcher.get()
        with inst._lock:
            inst._broker = None
            inst._login_date = None
            inst._failed_date = None
            inst._token_used = ""
        # Now attempt login + profile check and capture any error
        error = None
        try:
            live = inst.is_token_live()
        except Exception as e:
            live = False
            error = traceback.format_exc()
    except Exception as e:
        live = False
        error = traceback.format_exc()
    status = _get_token_status()
    return {"live": live, "token_status": status, "error": error}


@app.get("/api/bot/debug")
def bot_debug(user: str = Depends(get_current_user)):
    """Live signal scores for ATR Intraday + C-ICT strategies — no trades placed."""
    from core.bot_runner import _is_market_hours, IST
    runner = get_runner()
    now_ist = datetime.now(IST)

    result = {"time_ist": now_ist.isoformat(), "market_open": _is_market_hours(),
              "last_heartbeat": runner.last_heartbeat, "last_scores": runner.last_scores,
              "token_set_at": _get_token_status(),
              "latest_order_issue": _latest_order_issue(),
              "strategies": {}}

    # Use last_scores from bot cycles (populated after each ATR/ICT cycle).
    # Fall back to a live score fetch if the bot hasn't run yet (e.g. first load).
    for strat_name, score_mode in [("ATR Intraday", "atr_only"), ("C-ICT", "ict_only"), ("Fib-OF", "fib_of_only")]:
        if strat_name in runner.last_scores:
            result["strategies"][strat_name] = runner.last_scores[strat_name]
        else:
            try:
                from strategies.signal_scorer import score_symbol
                from strategies.patterns import detect_patterns, get_candles_from_df
                from data.zerodha_fetcher import ZerodhaFetcher
                fetcher = ZerodhaFetcher.get()
                intraday_raw = fetcher.fetch_intraday("NIFTY", "15m")
                if intraday_raw is None:
                    raise ValueError("No intraday data")
                opens, highs, lows, closes, volumes, all_closes, bar_time = intraday_raw
                import pandas as pd
                import numpy as np
                df = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes})
                price = float(closes[-1]) if len(closes) else 0
                indicators = {"price": price, "symbol": "NIFTY"}
                sc = score_symbol(indicators, {}, {}, mode=score_mode, df_5m=None)
                result["strategies"][strat_name] = {
                    "score": sc["score"], "direction": sc["action"], "action": sc["action"],
                    "threshold": sc["threshold"], "will_trade": abs(sc["score"]) >= sc["threshold"],
                    "note": f"live fetch (bot cycle not yet run) | mode={score_mode}",
                }
            except Exception as e:
                result["strategies"][strat_name] = {"error": str(e)}

    return Response(content=_safe_json(result), media_type="application/json")

@app.get("/api/bot/bias")
def get_bias(user: str = Depends(get_current_user)):
    return ipc.read_day_bias()

@app.post("/api/bot/bias")
def set_bias(body: dict, user: str = Depends(get_current_user)):
    from core.note_parser import parse_trade_note
    bias = body.get("bias", "NEUTRAL").upper()
    if bias not in ("BULLISH", "BEARISH", "NEUTRAL"):
        raise HTTPException(status_code=400, detail="bias must be BULLISH, BEARISH, or NEUTRAL")
    note = body.get("note", "")

    parsed = parse_trade_note(note)

    # If natural language bias detected, upgrade the explicit bias selection
    if parsed["type"] == "bias" and bias == "NEUTRAL":
        bias = parsed["bias"]

    # Queue force trade (bypasses signal scorer)
    if parsed["type"] == "force_trade":
        if ipc.flag_exists(ipc.FLAG_FORCE_TRADE):
            parsed["explanation"] = "A trade is already queued — wait for it to execute first."
            parsed["type"] = "unclear"
        else:
            ipc.write_force_trade(
                symbol      = parsed.get("symbol", "NIFTY"),
                side        = parsed["direction"],
                quantity    = 1,
                reason      = f"Note trade: {note}",
                option_type = parsed.get("option_type"),
                strike      = parsed.get("strike"),
                sl          = parsed.get("sl"),
                tp          = parsed.get("tp"),
            )

    ipc.write_day_bias(bias, note, parsed)
    result = ipc.read_day_bias()
    result["parsed"] = parsed
    # Update runner cache so WebSocket broadcast reflects new bias immediately
    get_runner().last_day_bias = {k: v for k, v in result.items() if k != "parsed"}
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


@app.post("/api/live/preflight")
def live_preflight(body: dict, user: str = Depends(get_current_user)):
    from core.broker import KiteBroker
    symbol = str(body.get("symbol", "NIFTY")).upper()
    side = str(body.get("side", "BUY")).upper()
    option_type = str(body.get("option_type") or ("PE" if side == "SELL" else "CE")).upper()
    quantity = int(body.get("quantity") or config.LOT_SIZES.get(symbol, 1))
    strike = body.get("strike")
    strike = int(strike) if strike not in (None, "") else None
    order_type = str(body.get("order_type", "MARKET")).upper()
    product = str(body.get("product", "MIS")).upper()
    exchange = str(body.get("exchange", "NFO")).upper()
    try:
        broker = KiteBroker()
        report = broker.preflight_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            exchange=exchange,
            product=product,
            option_type=option_type,
            strike=strike,
            tag="preflight",
            log_failures=False,
        )
        return report
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


@app.get("/api/zerodha-errors")
def get_zerodha_errors(user: str = Depends(get_current_user)):
    from core.zerodha_error_log import get_all
    return get_all()


@app.get("/api/event-blocks")
def get_event_blocks(user: str = Depends(get_current_user)):
    """Return all blocked dates: hardcoded config + runtime overrides, with unblock status."""
    from datetime import date
    today = date.today().isoformat()
    hardcoded  = config.EVENT_BLOCK_DATES
    runtime    = ipc.read_event_blocks()
    unblocks   = ipc.read_event_unblocks()
    merged = {**hardcoded, **runtime}
    today_blocked_raw = bool(merged.get(today))
    today_unblocked   = today in unblocks
    return {
        "blocks": [
            {
                "date":       d,
                "label":      label,
                "source":     "runtime" if d in runtime else "config",
                "is_today":   d == today,
                "unblocked":  d in unblocks,
            }
            for d, label in sorted(merged.items())
        ],
        "today_blocked": today_blocked_raw and not today_unblocked,
        "today_label":   merged.get(today),
        "today_unblocked": today_unblocked,
    }


@app.post("/api/event-blocks")
def add_event_block(body: dict, user: str = Depends(get_current_user)):
    """Add a runtime event block date. Body: {date: 'YYYY-MM-DD', label: 'reason'}"""
    date_str = body.get("date", "").strip()
    label    = body.get("label", "Manual block").strip()
    if not date_str:
        raise HTTPException(status_code=400, detail="date is required (YYYY-MM-DD)")
    try:
        from datetime import date as _date
        _date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    ipc.add_event_block(date_str, label)
    return {"status": "added", "date": date_str, "label": label}


@app.delete("/api/event-blocks/{date_str}")
def remove_event_block(date_str: str, user: str = Depends(get_current_user)):
    """Remove a runtime event block. Config-hardcoded dates cannot be removed here."""
    if date_str in config.EVENT_BLOCK_DATES:
        raise HTTPException(status_code=400, detail="This date is hardcoded in config.py — use unblock instead")
    ipc.remove_event_block(date_str)
    return {"status": "removed", "date": date_str}


@app.post("/api/event-blocks/{date_str}/unblock")
def unblock_date(date_str: str, user: str = Depends(get_current_user)):
    """Force-allow trading on a blocked date (overrides both config and runtime blocks)."""
    ipc.add_event_unblock(date_str)
    return {"status": "unblocked", "date": date_str}


@app.delete("/api/event-blocks/{date_str}/unblock")
def remove_unblock(date_str: str, user: str = Depends(get_current_user)):
    """Remove the unblock override — date goes back to its original blocked state."""
    ipc.remove_event_unblock(date_str)
    return {"status": "block_restored", "date": date_str}


@app.get("/api/zerodha/login-url")
def zerodha_login_url(user: str = Depends(get_current_user)):
    """Return the Kite Connect login URL so the frontend can open it."""
    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=config.ZERODHA_API_KEY)
        return {"url": kite.login_url()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not build login URL: {e}")


@app.get("/api/zerodha/callback")
def zerodha_callback_redirect(request_token: str = "", status: str = "", action: str = ""):
    """Zerodha redirects here after login (GET). Show token so user can copy-paste into the modal."""
    from fastapi.responses import HTMLResponse
    if status != "success" or not request_token:
        return HTMLResponse("<h2>Login failed or cancelled. Close this tab and try again.</h2>", status_code=400)
    html = f"""<!DOCTYPE html><html><head><title>Zerodha Token</title>
<style>body{{font-family:sans-serif;max-width:480px;margin:60px auto;padding:0 20px;}}
.box{{background:#f0f9ff;border:1px solid #bae6fd;border-radius:12px;padding:24px;}}
.token{{font-family:monospace;font-size:14px;word-break:break-all;background:#fff;
border:1px solid #cbd5e1;border-radius:8px;padding:12px;margin:12px 0;}}
button{{background:#4f46e5;color:#fff;border:none;padding:10px 20px;border-radius:8px;
cursor:pointer;font-size:14px;font-weight:600;}}
button:active{{background:#3730a3;}}
</style></head><body>
<div class="box">
<h3 style="margin-top:0">✓ Zerodha login successful</h3>
<p>Copy the token below, then go back to the dashboard and paste it in the <b>Get Token</b> modal.</p>
<div class="token" id="tok">{request_token}</div>
<button onclick="navigator.clipboard.writeText('{request_token}').then(()=>this.textContent='✓ Copied!')">
Copy Token</button>
<p style="margin-top:16px;font-size:12px;color:#64748b">After pasting in the modal, click Submit — the bot picks it up immediately.</p>
</div></body></html>"""
    return HTMLResponse(html)


@app.post("/api/zerodha/callback")
def zerodha_callback(body: dict, user: str = Depends(get_current_user)):
    """Exchange Zerodha request_token for access_token and persist to .env."""
    request_token = body.get("request_token", "").strip()
    if not request_token:
        raise HTTPException(status_code=400, detail="request_token is required")
    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=config.ZERODHA_API_KEY)
        data = kite.generate_session(request_token, api_secret=config.ZERODHA_API_SECRET)
        access_token = data["access_token"]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Zerodha session error: {e}")

    # Persist to .env
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    set_at = datetime.now(ist).isoformat()
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        lines = open(env_path).readlines() if os.path.exists(env_path) else []
        updated, found_token, found_set_at = [], False, False
        for line in lines:
            if line.startswith("ZERODHA_ACCESS_TOKEN="):
                updated.append(f"ZERODHA_ACCESS_TOKEN={access_token}\n"); found_token = True
            elif line.startswith("ZERODHA_TOKEN_SET_AT="):
                updated.append(f"ZERODHA_TOKEN_SET_AT={set_at}\n"); found_set_at = True
            else:
                updated.append(line)
        if not found_token:  updated.append(f"ZERODHA_ACCESS_TOKEN={access_token}\n")
        if not found_set_at: updated.append(f"ZERODHA_TOKEN_SET_AT={set_at}\n")
        open(env_path, "w").writelines(updated)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write .env: {e}")

    # Reset ZerodhaFetcher so it picks up the new token immediately
    _token_cache["result"] = None
    _token_cache["checked_at"] = 0.0
    try:
        from data.zerodha_fetcher import ZerodhaFetcher
        inst = ZerodhaFetcher.get()
        with inst._lock:
            inst._broker = None
            inst._login_date = None
            inst._failed_date = None
            inst._token_used = ""
    except Exception:
        pass

    return {"status": "ok", "set_at": set_at}


@app.delete("/api/zerodha-errors")
def clear_zerodha_errors(user: str = Depends(get_current_user)):
    from core.zerodha_error_log import clear
    clear()
    return {"status": "cleared"}


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


# ── Safe JSON serializer (handles numpy floats, NaN, Inf) ─────────────────────

def _safe_json(obj) -> str:
    """JSON serializer that handles numpy types and non-finite floats."""
    def default(o):
        try:
            import numpy as np
            if isinstance(o, np.bool_):    return bool(o)
            if isinstance(o, np.integer):  return int(o)
            if isinstance(o, np.floating): return None if (math.isnan(float(o)) or math.isinf(float(o))) else float(o)
            if isinstance(o, np.ndarray):  return o.tolist()
        except ImportError:
            pass
        if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
            return None
        raise TypeError(f"Object of type {type(o)} is not JSON serializable")
    return json.dumps(obj, default=default)


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query(default="")):
    await ws.accept()
    try:
        decode_token(token)
    except Exception:
        await ws.close(code=1008, reason="Invalid or expired token")
        return
    await manager.connect(ws)
    try:
        # Send initial snapshot immediately on connect
        await ws.send_text(_safe_json(_build_snapshot()))
        while True:
            await asyncio.sleep(5)
            await ws.send_text(_safe_json(_build_snapshot()))
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:
        logger.error("WebSocket error (disconnecting): %s", e)
        manager.disconnect(ws)


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=True)
