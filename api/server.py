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
    for origin in os.getenv(
        "DASHBOARD_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,https://ai-trading-alltime-record.vercel.app",
    ).split(",")
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
# Shadow strategies — read at runtime from feature_signals.ALL_SIGNALS for
# any per-strategy aggregation the snapshot needs.
SHADOW_STRATEGY_NAMES = ("q5_straddle_level", "q5_straddle_mom3",
                          "q5_pcr_mom3", "q5_iv_cheap_090")

# ── Price cache — shared across all WebSocket connections ─────────────────────
# Without this, 5 open browser tabs × every-5s broadcast = 60 Angel One calls/min
import time as _time
_price_cache: dict = {}
_price_cache_ts: float = 0.0
_PRICE_TTL = 30  # seconds — refresh live price at most once every 30s

def _get_prices() -> dict:
    global _price_cache, _price_cache_ts
    from zoneinfo import ZoneInfo as _ZI
    from datetime import datetime as _dt, time as _dtime
    _now = _dt.now(_ZI("Asia/Kolkata"))
    _t = _now.time()
    from core.ipc import is_market_holiday as _is_hol_fn
    _is_hol, _ = _is_hol_fn(_now.strftime("%Y-%m-%d"))
    _market_open = _dtime(9, 15) <= _t <= _dtime(15, 30) and _now.weekday() < 5 and not _is_hol
    if not _market_open:
        return _price_cache  # return last known prices, don't hit Angel One API
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
    from core.angel_error_log import get_all
    for item in get_all():
        if item.get("source") in {"live_order_preflight", "live_order_rejected"}:
            return item
    return None


_account_cache: dict = {"data": None, "checked_at": 0.0}
_ACCOUNT_CACHE_TTL = 60   # rmsLimit hits Angel One — cache 60s


def _get_account_status(today_round_trips: list) -> dict:
    """Return a compact account-status block for the dashboard top strip.

    Surfaces three things prominently:
      - available_cash    cash that can fund a new trade today (₹)
      - is_unfunded       True if balance < a meaningful threshold
      - rejections_today  count of virtual_rejected entries today
      - cooldown_hit      True if the bot has auto-paused due to too many rejections

    Cached 60s so we don't hammer rmsLimit on every WebSocket tick.
    """
    import time as _t
    now = _t.monotonic()
    if _account_cache["data"] is not None and now - _account_cache["checked_at"] < _ACCOUNT_CACHE_TTL:
        cached = dict(_account_cache["data"])
    else:
        cached = {"available_cash": None, "net_value": None, "locked_cash": None,
                  "is_unfunded": False, "error": None}
        try:
            from core.broker import get_broker
            broker = get_broker()
            summary = broker.get_portfolio_summary() or {}
            # balance == available_cash (truly free, per the post-fix logic in
            # core/broker.py). net is total account value, may include pledged
            # margin / unsettled funds / position MTM that can't fund new BUYs.
            avail = float(summary.get("available_cash") or summary.get("balance") or 0)
            net   = float(summary.get("net") or 0)
            cached["available_cash"] = avail
            cached["net_value"]      = net
            cached["locked_cash"]    = max(0.0, net - avail)  # what's NOT free
            # < ₹5000 is effectively unfunded for 1 lot NIFTY option buying
            cached["is_unfunded"]    = avail < 5000
        except Exception as e:
            cached["error"] = str(e)
        _account_cache["data"]       = cached
        _account_cache["checked_at"] = now

    # Rejection counters come from today's trades — not cached because they
    # mutate fast and the data is cheap to compute.
    rejections = sum(
        1 for t in today_round_trips
        if (t.get("mode") == "virtual_rejected")
    )
    try:
        from core.bot_runner import get_runner
        is_paused = get_runner().paused
    except Exception:
        is_paused = False

    return {
        **cached,
        "rejections_today": rejections,
        "cooldown_hit":     False,   # ATR rejection cooldown removed with ATR
        "strategy_paused":  is_paused,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_snapshot() -> dict:
    """Compact dashboard snapshot built around the shadow-trading ledger.

    All real-trade history has been removed (ATR strategy retired). The bot
    only forward-tests shadow signals; this snapshot reflects that reality.
    """
    from zoneinfo import ZoneInfo
    from datetime import time as _dtime
    IST = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(IST)
    market_open = (
        now_ist.weekday() < 5
        and _dtime(9, 15) <= now_ist.time() <= _dtime(15, 30)
    )

    # Live prices — cached
    prices = _get_prices()

    # Bot status
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

    # Shadow trades summary from Mongo
    shadow_summary = {"open": 0, "today_pnl": 0.0, "total_pnl": 0.0,
                      "strategies": {}, "trades_today": 0}
    all_records = {}
    try:
        from core import mongo as _mongo
        from core.records import RecordTracker
        db = _mongo.get_db()
        if db is not None:
            today_str = now_ist.date().isoformat()
            for s_name in SHADOW_STRATEGY_NAMES:
                rows = list(db.shadow_trades.find(
                    {"strategy": s_name},
                    projection={"_id": 0, "pnl": 1, "status": 1, "date": 1},
                    sort=[("entry_dt", -1)],
                    limit=500,
                ))
                today_rows = [r for r in rows if r.get("date") == today_str]
                closed = [r for r in rows if r.get("status") == "CLOSED"]
                today_closed = [r for r in today_rows if r.get("status") == "CLOSED"]
                wins = sum(1 for r in closed if (r.get("pnl") or 0) > 0)
                shadow_summary["strategies"][s_name] = {
                    "trades_today":     len(today_rows),
                    "today_pnl":        round(sum(r.get("pnl") or 0 for r in today_closed), 2),
                    "total_pnl":        round(sum(r.get("pnl") or 0 for r in closed), 2),
                    "closed":           len(closed),
                    "wins":             wins,
                    "win_rate":         round(wins / len(closed) * 100, 1) if closed else 0,
                    "open":             any(r.get("status") == "OPEN" for r in rows),
                }
            shadow_summary["open"]         = sum(1 for s in shadow_summary["strategies"].values()
                                                  if s["open"])
            shadow_summary["today_pnl"]    = round(sum(s["today_pnl"]
                                                        for s in shadow_summary["strategies"].values()), 2)
            shadow_summary["total_pnl"]    = round(sum(s["total_pnl"]
                                                        for s in shadow_summary["strategies"].values()), 2)
            shadow_summary["trades_today"] = sum(s["trades_today"]
                                                  for s in shadow_summary["strategies"].values())
        all_records = records.get_all_records()
    except Exception as e:
        logger.debug("snapshot: shadow summary failed: %s", e)

    return {
        "timestamp":           now_ist.isoformat(),
        "bot_status":          bot_status,
        "scheduler_running":   scheduler_ok,
        "market_open":         market_open,
        "last_heartbeat":      runner.last_heartbeat,
        "token_set_at":        _get_token_status(),
        "option_chain":        runner.last_option_chain,
        "shadow":              shadow_summary,
        "mode":                "shadow",   # this bot does forward-testing only
        "prices":              prices,
        "angel_error_count":   len(__import__("core.angel_error_log",
                                              fromlist=["get_all"]).get_all()),
        "latest_order_issue":  _latest_order_issue(),
        "settings":            ipc.read_settings(),
        "account":             _get_account_status([]),
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
    """Return shadow trade history grouped by day.

    The legacy ATR P&L view is gone — this now reports the shadow ledger.
    """
    from core import mongo as _mongo
    from collections import defaultdict
    db = _mongo.get_db()
    if db is None:
        return {"total_pnl": 0, "total_trades": 0, "completed_trades": 0,
                "rejected_trades": 0, "win_rate": 0, "daily": [], "trades": []}

    q: dict = {}
    if start: q["date"] = {"$gte": start}
    if end:   q.setdefault("date", {}).update({"$lte": end})
    rows = list(db.shadow_trades.find(q, projection={"_id": 0}, limit=2000,
                                       sort=[("entry_dt", -1)]))

    daily: dict = defaultdict(lambda: {"trades": [], "total_pnl": 0,
                                        "wins": 0, "losses": 0, "rejected": 0})
    for t in rows:
        d = t.get("date", "unknown")
        daily[d]["trades"].append(t)
        pnl = t.get("pnl") or 0
        daily[d]["total_pnl"] = round(daily[d]["total_pnl"] + pnl, 2)
        if t.get("status") == "CLOSED":
            if pnl > 0: daily[d]["wins"]   += 1
            else:        daily[d]["losses"] += 1

    daily_summary = [{"date": d, **v} for d, v in sorted(daily.items(), reverse=True)]
    closed_rows = [r for r in rows if r.get("status") == "CLOSED"]
    total_pnl   = round(sum((r.get("pnl") or 0) for r in closed_rows), 2)
    wins        = sum(1 for r in closed_rows if (r.get("pnl") or 0) > 0)
    win_rate    = round(wins / len(closed_rows) * 100, 1) if closed_rows else 0

    return {
        "total_pnl":         total_pnl,
        "total_trades":      len(rows),
        "completed_trades":  len(closed_rows),
        "rejected_trades":   0,
        "win_rate":          win_rate,
        "daily":             daily_summary,
        "trades":            rows,
    }

@app.get("/api/health")
def health():
    from zoneinfo import ZoneInfo
    return {"status": "ok", "time": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()}


@app.get("/api/risk-budget")
def risk_budget_endpoint(user: str = Depends(get_current_user)):
    """Today's risk-budget status: aggregate + per-strategy caps & lot multipliers."""
    from core import risk_budget
    return risk_budget.status_snapshot()


@app.get("/api/websocket-status")
def websocket_status(user: str = Depends(get_current_user)):
    """Live tick stack diagnostics: WebSocket connection, market state,
    subscription manager. Surfaces whether signals are running on live ticks
    or falling back to Mongo reads."""
    out = {"ws": None, "market_state": None, "sub_manager": None}
    try:
        from data.angel_websocket import get_client
        out["ws"] = get_client().diagnostics()
    except Exception as e:
        out["ws"] = {"error": str(e)}
    try:
        from core.market_state import get_state
        out["market_state"] = get_state().diagnostics()
    except Exception as e:
        out["market_state"] = {"error": str(e)}
    try:
        from core.subscription_manager import get_manager
        # get_manager requires args on first call — wrap in try; if not yet
        # initialised, just report not-yet-started
        out["sub_manager"] = get_manager().diagnostics()
    except Exception as e:
        out["sub_manager"] = {"error": str(e)}
    return out


@app.get("/api/shadow-trades")
def shadow_trades_endpoint(days: int = 30, user: str = Depends(get_current_user)):
    """Multi-strategy shadow trade ledger.

    Forward-test data only — these are simulated trades, no real orders.
    Returns one block per strategy (q5_straddle_level, q5_straddle_mom3,
    q5_pcr_mom3) plus an aggregate.
    """
    from core import mongo as _mongo
    from datetime import datetime, timedelta
    db = _mongo.get_db()
    if db is None:
        return {"enabled": False, "strategies": {}, "aggregate": None}

    today = datetime.now().date().isoformat()
    since_date = (datetime.now().date() - timedelta(days=days)).isoformat()

    rows = list(db.shadow_trades.find(
        {"date": {"$gte": since_date}},
        projection={"_id": 0},
        sort=[("entry_dt", -1)],
        limit=500,
    ))

    # Group by strategy. Default name covers any pre-multi-strategy rows.
    strategies: dict = {}
    for r in rows:
        s = r.get("strategy") or "q5_straddle_level"
        strategies.setdefault(s, []).append(r)

    # Per-strategy summary
    out: dict = {}
    for strat_name in ("q5_straddle_level", "q5_straddle_mom3", "q5_pcr_mom3"):
        s_rows = strategies.get(strat_name, [])
        open_pos  = next((r for r in s_rows if r.get("status") == "OPEN"), None)
        today_pnl = sum(r.get("pnl", 0) or 0 for r in s_rows
                        if r.get("date") == today and r.get("status") == "CLOSED")
        total_pnl = sum(r.get("pnl", 0) or 0 for r in s_rows
                        if r.get("status") == "CLOSED")
        closed_cnt = sum(1 for r in s_rows if r.get("status") == "CLOSED")
        wins_cnt   = sum(1 for r in s_rows
                          if r.get("status") == "CLOSED" and (r.get("pnl") or 0) > 0)
        out[strat_name] = {
            "open_position": open_pos,
            "today_pnl":     round(today_pnl, 2),
            "total_pnl":     round(total_pnl, 2),
            "closed_count":  closed_cnt,
            "wins_count":    wins_cnt,
            "win_rate":      round(wins_cnt / closed_cnt * 100, 1) if closed_cnt else 0,
            "trades":        s_rows[:50],   # cap per-strategy list
        }

    # Aggregate row (sum across all strategies)
    agg_today = sum(s["today_pnl"]    for s in out.values())
    agg_total = sum(s["total_pnl"]    for s in out.values())
    agg_open  = sum(1 for s in out.values() if s["open_position"])

    return {
        "enabled":      True,
        "strategies":   out,
        "aggregate":    {
            "today_pnl":      round(agg_today, 2),
            "total_pnl":      round(agg_total, 2),
            "open_count":     agg_open,
            "strategy_count": len(out),
        },
    }


_mongo_status_cache: dict = {"result": None, "checked_at": 0.0}
_MONGO_STATUS_TTL = 30   # re-check Mongo connectivity at most every 30s


@app.get("/api/mongo/status")
def mongo_status(user: str = Depends(get_current_user)):
    """Mongo mirror health + per-collection counts.

    Cached 30s so dashboard polling doesn't spam pymongo with count_documents().
    Returns enabled=False whenever Mongo is unreachable / unconfigured — the
    bot keeps working fine in that case (SQLite is the primary store).
    """
    import time as _t
    now = _t.monotonic()
    if (
        _mongo_status_cache["result"] is not None
        and now - _mongo_status_cache["checked_at"] < _MONGO_STATUS_TTL
    ):
        return _mongo_status_cache["result"]

    from core import mongo as _mongo
    db = _mongo.get_db()
    if db is None:
        result = {
            "enabled":  False,
            "db_name":  os.environ.get("MONGODB_DB_NAME") or None,
            "url_host": _mask_mongo_host(os.environ.get("MONGODB_URL")),
            "error":    "MONGODB_URL/MONGODB_DB_NAME missing or first connect failed",
            "checked_at": datetime.now().isoformat(),
        }
    else:
        try:
            counts = {}
            for col in ("trades", "signal_log", "daily_journals",
                        "option_snapshots", "records", "weekly_reviews",
                        "shadow_trades"):
                try:
                    counts[col] = db[col].estimated_document_count()
                except Exception as _ce:
                    counts[col] = f"err: {_ce}"
            # Most-recent timestamps for the spotcheck columns
            latest = {}
            try:
                t = db.trades.find_one(sort=[("timestamp", -1)], projection={"timestamp": 1, "_id": 0})
                latest["latest_trade_ts"] = (t or {}).get("timestamp")
            except Exception:
                latest["latest_trade_ts"] = None
            try:
                s = db.option_snapshots.find_one(sort=[("timestamp", -1)],
                                                 projection={"timestamp": 1, "_id": 0})
                latest["latest_snapshot_ts"] = (s or {}).get("timestamp")
            except Exception:
                latest["latest_snapshot_ts"] = None

            result = {
                "enabled":  True,
                "db_name":  db.name,
                "url_host": _mask_mongo_host(os.environ.get("MONGODB_URL")),
                "counts":   counts,
                **latest,
                "checked_at": datetime.now().isoformat(),
            }
        except Exception as e:
            result = {
                "enabled":  False,
                "db_name":  None,
                "error":    f"ping/count failed: {e}",
                "checked_at": datetime.now().isoformat(),
            }

    _mongo_status_cache["result"]    = result
    _mongo_status_cache["checked_at"] = now
    return result


def _mask_mongo_host(url: str | None) -> str | None:
    """Show only the cluster hostname — never expose user/password."""
    if not url:
        return None
    try:
        # mongodb+srv://user:pass@cluster.xxxx.mongodb.net/?... → cluster.xxxx.mongodb.net
        from urllib.parse import urlparse
        h = urlparse(url).hostname
        return h
    except Exception:
        return None

_token_cache: dict = {"result": None, "checked_at": 0.0}
_TOKEN_CACHE_TTL = 300  # re-check token liveness every 5 minutes

def _get_token_status() -> dict:
    """Check Angel One session liveness (cached for 5 minutes)."""
    import time
    now = time.monotonic()
    if now - _token_cache["checked_at"] < _TOKEN_CACHE_TTL and _token_cache["result"] is not None:
        return _token_cache["result"]
    try:
        from data.angel_fetcher import AngelFetcher
        live = AngelFetcher.get().is_token_live()
        if live:
            from dotenv import dotenv_values
            env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
            set_at = dotenv_values(env_path).get("ANGEL_TOKEN_SET_AT") or None
            result = {"live": True, "set_at": set_at}
        else:
            result = {"live": False, "set_at": None}
    except Exception as e:
        logger.error("Token liveness check failed: %s", e, exc_info=True)
        result = {"live": False, "set_at": None}
    _token_cache["result"] = result
    _token_cache["checked_at"] = now if result["live"] else now - _TOKEN_CACHE_TTL + 30
    return result


@app.post("/api/token/refresh")
def token_refresh(user: str = Depends(get_current_user)):
    """Force-clear token cache and Angel One session, then re-check liveness."""
    import traceback
    _token_cache["result"] = None
    _token_cache["checked_at"] = 0.0
    error = None
    live = False
    try:
        from data.angel_fetcher import AngelFetcher
        inst = AngelFetcher.get()
        with inst._lock:
            inst._api = None
            inst._login_date = None
            inst._failed_at = None
        try:
            live = inst.is_token_live()
        except Exception:
            live = False
            error = traceback.format_exc()
    except Exception:
        live = False
        error = traceback.format_exc()
    status = _get_token_status()
    return {"live": live, "token_status": status, "error": error}


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

@app.get("/api/settings")
def get_settings(user: str = Depends(get_current_user)):
    return ipc.read_settings()

@app.post("/api/settings")
def update_settings(body: dict, user: str = Depends(get_current_user)):
    min_lots = body.get("min_lots")
    if min_lots is not None:
        min_lots = int(min_lots)
        # Cap upper bound at config.MAX_LOTS so a stray dashboard click can't
        # blow past the user-configured live limit. Bump MAX_LOTS in config.py
        # to raise this ceiling.
        upper = max(1, int(getattr(config, "MAX_LOTS", 10)))
        if min_lots < 1 or min_lots > upper:
            raise HTTPException(
                status_code=400,
                detail=f"min_lots must be between 1 and {upper} (config.MAX_LOTS)",
            )
    patch = {}
    if min_lots is not None:
        patch["min_lots"] = min_lots
    return ipc.write_settings(patch)


@app.get("/api/angel-errors")
def get_angel_errors(user: str = Depends(get_current_user)):
    from core.angel_error_log import get_all
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


# ── Market Holidays API ───────────────────────────────────────────────────────

@app.get("/api/market-holidays")
def get_market_holidays(_user: str = Depends(get_current_user)):
    """List all NSE market holidays (config + runtime-added)."""
    import config
    from datetime import date
    today_str = date.today().isoformat()
    runtime   = ipc.read_runtime_holidays()
    all_dates = sorted(set(config.NSE_MARKET_HOLIDAYS) | set(runtime))
    holidays  = []
    for d in all_dates:
        label  = config.NSE_MARKET_HOLIDAYS.get(d) or runtime.get(d, "")
        source = "config" if d in config.NSE_MARKET_HOLIDAYS else "runtime"
        holidays.append({
            "date":     d,
            "label":    label,
            "source":   source,
            "is_today": d == today_str,
        })
    is_hol, hol_label = ipc.is_market_holiday(today_str)
    return {
        "holidays":      holidays,
        "today_holiday": is_hol,
        "today_label":   hol_label if is_hol else None,
    }


@app.post("/api/market-holidays")
def add_holiday(body: dict, _user: str = Depends(get_current_user)):
    """Add a runtime market holiday. Body: {date: 'YYYY-MM-DD', label: 'reason'}"""
    from datetime import date as _date
    date_str = body.get("date", "").strip()
    label    = body.get("label", "NSE Holiday").strip()
    try:
        _date.fromisoformat(date_str)
    except ValueError:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")
    ipc.add_market_holiday(date_str, label)
    return {"status": "added", "date": date_str, "label": label}


@app.delete("/api/market-holidays/{date_str}")
def remove_holiday(date_str: str, user: str = Depends(get_current_user)):
    """Remove a runtime market holiday. Config-hardcoded holidays cannot be removed here."""
    import config
    if date_str in config.NSE_MARKET_HOLIDAYS:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Cannot remove config-hardcoded holiday. Edit config.py.")
    ipc.remove_market_holiday(date_str)
    return {"status": "removed", "date": date_str}


@app.post("/api/angel/session")
def angel_create_session(user: str = Depends(get_current_user)):
    """Force a fresh Angel One TOTP login — auto-generates TOTP, no manual step needed."""
    from data.angel_fetcher import AngelFetcher
    _token_cache["result"] = None
    _token_cache["checked_at"] = 0.0
    inst = AngelFetcher.get()
    # Only clear the cooldown — don't nuke _api (avoids hanging when rate-limited)
    inst._failed_at = None
    # If session genuinely invalid, force re-login
    if not inst.is_token_live():
        with inst._lock:
            inst._api = None
            inst._login_date = None
    try:
        ok = inst._ensure_logged_in()
        if not ok:
            raise HTTPException(status_code=502, detail="Angel One login failed — check ANGEL_* vars in .env")
        from dotenv import dotenv_values
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        set_at = dotenv_values(env_path).get("ANGEL_TOKEN_SET_AT") or "unknown"
        return {"status": "ok", "set_at": set_at}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/angel-errors")
def clear_angel_errors(user: str = Depends(get_current_user)):
    from core.angel_error_log import clear
    clear()
    return {"status": "cleared"}


_chart_cache: dict = {"data": None, "ts": 0.0}
_CHART_CACHE_TTL = 300   # 5 minutes


def _compute_poc(df, bucket_size: float = 25.0):
    """
    Price-based Point of Control (POC) using candle body overlap — Time At Price.
    Returns (poc, vah, val) or (None, None, None) if insufficient data.
      poc = price with most candle body overlap (highest concentration)
      vah = Value Area High (top of 70% activity band)
      val = Value Area Low  (bottom of 70% activity band)
    """
    try:
        import numpy as np
        opens  = df["Open"].astype(float).values
        closes = df["Close"].astype(float).values
        if len(opens) < 10:
            return None, None, None

        p_min = float(min(min(opens), min(closes))) - bucket_size
        p_max = float(max(max(opens), max(closes))) + bucket_size

        # Build price buckets
        buckets: dict = {}
        p = p_min
        while p <= p_max:
            buckets[round(p)] = 0
            p += bucket_size
        bkeys = sorted(buckets.keys())

        # Count candle body overlaps per bucket
        for i in range(len(opens)):
            b_top = max(opens[i], closes[i])
            b_bot = min(opens[i], closes[i])
            if b_top == b_bot:   # doji — skip
                continue
            for bk in bkeys:
                if bk < b_top and bk + bucket_size > b_bot:
                    buckets[bk] += 1

        if not any(buckets.values()):
            return None, None, None

        poc_key = max(buckets, key=buckets.get)
        poc = round(poc_key + bucket_size / 2, 2)

        # Value Area: buckets representing 70% of total bar activity
        total  = sum(buckets.values())
        target = total * 0.70
        accum  = 0
        included = set()
        for bk in sorted(buckets, key=buckets.get, reverse=True):
            included.add(bk)
            accum += buckets[bk]
            if accum >= target:
                break

        vah = round(max(included) + bucket_size, 2)
        val = round(min(included), 2)
        return poc, vah, val
    except Exception:
        return None, None, None


@app.get("/api/chart-data")
def chart_data(user: str = Depends(get_current_user)):
    """Returns NIFTY 5m OHLCV candles + S/R levels + POC for the live chart."""
    import time as _time
    now = _time.time()
    if _chart_cache["data"] and now - _chart_cache["ts"] < _CHART_CACHE_TTL:
        return Response(content=_safe_json(_chart_cache["data"]), media_type="application/json")
    try:
        from data.angel_fetcher import AngelFetcher
        from core.sr_levels import compute_sr_levels
        import pandas as pd

        # 10 days for POC (concentration line), display only last 3 days as candles
        df_full = AngelFetcher.get().fetch_historical_df("NIFTY", "5m", days=10)
        if df_full is None or len(df_full) < 10:
            return {"candles": [], "levels": [], "supply_zones": [], "demand_zones": [],
                    "structure": "ranging", "position": "open_air", "error": "no data"}

        # Normalise columns
        df_full = df_full.copy()
        for col in ["Open", "High", "Low", "Close"]:
            if col not in df_full.columns and col.lower() in df_full.columns:
                df_full[col] = df_full[col.lower()]
        df_full.index = pd.to_datetime(df_full.index, utc=True)

        # Last 3 days for display (~225 bars)
        df = df_full.tail(3 * 75)

        # Build candles for TradingView Lightweight Charts (Unix seconds)
        candles = []
        for ts, row in df.iterrows():
            candles.append({
                "time":  int(ts.timestamp()),
                "open":  round(float(row["Open"]),  2),
                "high":  round(float(row["High"]),  2),
                "low":   round(float(row["Low"]),   2),
                "close": round(float(row["Close"]), 2),
            })

        # S/R levels from last 200 bars
        sr = compute_sr_levels(df.tail(200))

        # POC/VAH/VAL from full 10-day window
        poc, vah, val = _compute_poc(df_full)

        # EMA 20, 50, 200 on close prices
        closes = df["Close"].astype(float)
        times  = [int(ts.timestamp()) for ts in df.index]
        def _ema_series(period: int) -> list:
            vals = closes.ewm(span=period, adjust=False).mean()
            return [{"time": t, "value": round(float(v), 2)}
                    for t, v in zip(times, vals)
                    if not (v != v)]  # skip NaN

        result = {
            "candles":          candles,
            "levels":           sr["levels"],
            "supply":           sr["support"],
            "resistance":       sr["resistance"],
            "supply_zones":     sr["supply_zones"],
            "demand_zones":     sr["demand_zones"],
            "structure":        sr["structure"],
            "position":         sr["position"],
            "current_price":    sr["current_price"],
            "nearest_support":  sr["nearest_support"],
            "nearest_resistance": sr["nearest_resistance"],
            "ema20":  _ema_series(20),
            "ema50":  _ema_series(50),
            "ema200": _ema_series(200),
            "poc":    poc,
            "vah":    vah,
            "val":    val,
        }
        _chart_cache["data"] = result
        _chart_cache["ts"]   = now
        return Response(content=_safe_json(result), media_type="application/json")
    except Exception as e:
        return {"candles": [], "levels": [], "supply_zones": [], "demand_zones": [],
                "structure": "ranging", "position": "open_air", "error": str(e)}


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
