"""
MongoDB write-through mirror.

Design:
  • SQLite + CSV files are still the PRIMARY store. Live trading never depends
    on Mongo being reachable.
  • Every critical write (trade, signal, journal, option snapshot, record) ALSO
    fires a fire-and-forget mirror call here.
  • Mongo write failures are caught, logged, and silently swallowed. The bot
    keeps trading.
  • Reads from the dashboard/external tools can hit Mongo without touching the
    live SQLite path.

Env config:
  MONGODB_URL      — full connection string (e.g. "mongodb+srv://user:pass@…")
  MONGODB_DB_NAME  — database name (e.g. "ai_trading")

If either env var is missing the module silently no-ops — useful for local dev
without Mongo.

Collections:
  trades              — round-trip trades + virtual_rejected entries
  signal_log          — every 5-min scorer evaluation
  daily_journals      — per-day journal JSON
  option_snapshots    — 5-min option chain snapshots from collector
  records             — all-time record breakers
  weekly_reviews      — Saturday Claude weekly review
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_client_lock = threading.Lock()
_client = None       # lazy-initialised pymongo.MongoClient
_db = None           # lazy-initialised database handle
_disabled = False    # set True if env vars missing or first connect fails


def _get_db():
    """Return the database handle, or None if Mongo isn't configured / reachable.

    Lazy & idempotent. The first failed connect sets _disabled so we don't
    keep retrying every write — Mongo is opt-in.
    """
    global _client, _db, _disabled
    if _disabled:
        return None
    if _db is not None:
        return _db
    with _client_lock:
        if _db is not None:
            return _db
        if _disabled:
            return None
        url     = os.environ.get("MONGODB_URL")
        db_name = os.environ.get("MONGODB_DB_NAME")
        if not url or not db_name:
            logger.info("Mongo mirror disabled: MONGODB_URL / MONGODB_DB_NAME not set")
            _disabled = True
            return None
        try:
            from pymongo import MongoClient, ASCENDING, DESCENDING
            _client = MongoClient(
                url,
                serverSelectionTimeoutMS=3000,
                connectTimeoutMS=3000,
                appname="ai-trading-alltime-record",
            )
            _client.admin.command("ping")  # blocks until reachable or 3s timeout
            _db = _client[db_name]
            _ensure_indexes(_db)
            logger.info("Mongo mirror connected: db=%s", db_name)
            return _db
        except Exception as e:
            logger.warning("Mongo mirror disabled (connect failed): %s", e)
            _disabled = True
            return None


def _ensure_indexes(db) -> None:
    """Create indexes on first connect. Idempotent — pymongo no-ops if exists."""
    try:
        from pymongo import ASCENDING, DESCENDING
        db.trades.create_index([("order_id", ASCENDING)], unique=True, sparse=True)
        db.trades.create_index([("timestamp", DESCENDING)])
        db.trades.create_index([("strategy", ASCENDING), ("underlying", ASCENDING),
                                ("closed_at", ASCENDING)])
        db.signal_log.create_index([("timestamp", DESCENDING)])
        db.signal_log.create_index([("strategy", ASCENDING), ("date", DESCENDING)])
        db.daily_journals.create_index([("date", ASCENDING)], unique=True)
        db.option_snapshots.create_index([("timestamp", DESCENDING)])
        db.option_snapshots.create_index([("date", ASCENDING), ("symbol", ASCENDING)])
        db.records.create_index([("description", ASCENDING)], unique=True)
        db.weekly_reviews.create_index([("week", ASCENDING)], unique=True)
    except Exception as e:
        logger.warning("Mongo index creation failed (non-fatal): %s", e)


def _safe(fn):
    """Decorator: swallow any exception so Mongo issues never break the live bot."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.warning("Mongo mirror %s failed (non-fatal): %s", fn.__name__, e)
            return None
    wrapper.__name__ = fn.__name__
    return wrapper


# ── Mirror APIs ──────────────────────────────────────────────────────────────

@_safe
def mirror_trade(order: dict, decision: Optional[dict] = None) -> None:
    """Upsert a trade row by order_id. Called from TradeMemory.log_trade."""
    db = _get_db()
    if db is None or not order.get("order_id"):
        return
    doc = {
        **{k: v for k, v in order.items() if v is not None},
        "_mirrored_at": datetime.now(timezone.utc),
    }
    if decision:
        doc["decision_reasoning"] = decision.get("reasoning")
        doc["decision_confidence"] = decision.get("confidence")
        doc["decision_risk_level"] = decision.get("risk_level")
    db.trades.update_one({"order_id": order["order_id"]}, {"$set": doc}, upsert=True)


@_safe
def mirror_trade_close(order_id: str, pnl: float, closed_at: str) -> None:
    """Update an existing trade with PnL + closed_at when SELL fires."""
    db = _get_db()
    if db is None or not order_id:
        return
    db.trades.update_one(
        {"order_id": order_id},
        {"$set": {"pnl": pnl, "closed_at": closed_at,
                  "_mirrored_at": datetime.now(timezone.utc)}},
    )


@_safe
def mirror_signal(row: dict) -> None:
    """Append a signal_log row (one per 5-min eval, trade or no-trade)."""
    db = _get_db()
    if db is None:
        return
    doc = {**row, "_mirrored_at": datetime.now(timezone.utc)}
    db.signal_log.insert_one(doc)


@_safe
def mirror_journal(date_str: str, journal: dict) -> None:
    """Upsert a daily journal by date."""
    db = _get_db()
    if db is None:
        return
    doc = {**journal, "_mirrored_at": datetime.now(timezone.utc)}
    db.daily_journals.update_one({"date": date_str}, {"$set": doc}, upsert=True)


@_safe
def mirror_weekly_review(key: str, data: dict) -> None:
    """Upsert a weekly review (Saturday 08:00 IST)."""
    db = _get_db()
    if db is None:
        return
    doc = {**data, "_mirrored_at": datetime.now(timezone.utc)}
    db.weekly_reviews.update_one({"week": key}, {"$set": doc}, upsert=True)


@_safe
def mirror_option_snapshot(rows: list[dict]) -> int:
    """Bulk-insert option chain snapshot rows. Returns inserted count."""
    db = _get_db()
    if db is None or not rows:
        return 0
    docs = [{**r, "_mirrored_at": datetime.now(timezone.utc)} for r in rows]
    res = db.option_snapshots.insert_many(docs, ordered=False)
    return len(res.inserted_ids)


@_safe
def mirror_record(description: str, value: Any, symbol: str = "",
                  achieved_at: Optional[str] = None) -> None:
    """Upsert an all-time record by description."""
    db = _get_db()
    if db is None:
        return
    doc = {
        "description": description,
        "value":       value,
        "symbol":      symbol,
        "achieved_at": achieved_at or datetime.now(timezone.utc).isoformat(),
        "_mirrored_at": datetime.now(timezone.utc),
    }
    db.records.update_one({"description": description}, {"$set": doc}, upsert=True)


# ── Read helpers (dashboard / scripts) ────────────────────────────────────────

def is_enabled() -> bool:
    """True if Mongo is configured AND reachable."""
    return _get_db() is not None


def get_db():
    """Return the database handle for ad-hoc reads. May return None."""
    return _get_db()
