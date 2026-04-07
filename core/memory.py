import sqlite3
import json
import logging
from datetime import datetime
from typing import Optional
import config
from core.utils import now_ist, today_ist

logger = logging.getLogger(__name__)


def get_connection():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize database tables."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT UNIQUE,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                pnl REAL DEFAULT 0,
                status TEXT NOT NULL,
                action_reason TEXT,
                confidence REAL DEFAULT 0,
                risk_level TEXT,
                timestamp TEXT NOT NULL,
                closed_at TEXT,
                mode TEXT DEFAULT 'paper',
                strategy TEXT,
                option_type TEXT,
                strike REAL,
                lot_size INTEGER DEFAULT 65,
                sl_price REAL,
                tp_price REAL,
                close_reason TEXT,
                score REAL
            );

            CREATE TABLE IF NOT EXISTS daily_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE NOT NULL,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                losing_trades INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                best_trade_pnl REAL DEFAULT 0,
                worst_trade_pnl REAL DEFAULT 0,
                review TEXT
            );

            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                data TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
        """)
    # Migration: add new columns to existing databases
    new_cols = [
        ("strategy",    "TEXT"),
        ("option_type", "TEXT"),
        ("strike",      "REAL"),
        ("lot_size",    "INTEGER DEFAULT 65"),
        ("sl_price",    "REAL"),
        ("tp_price",    "REAL"),
        ("close_reason","TEXT"),
        ("score",        "REAL"),
        ("entry_remark", "TEXT"),
        ("exit_remark",  "TEXT"),
    ]
    with get_connection() as conn:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
        for col, typ in new_cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
    logger.info("Database initialized at %s", config.DB_PATH)


class TradeMemory:
    def log_trade(self, order: dict, decision: dict) -> int:
        """Save a trade to memory."""
        with get_connection() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO trades
                (order_id, symbol, side, quantity, price, pnl, status, action_reason,
                 confidence, risk_level, timestamp, mode,
                 strategy, option_type, strike, lot_size, sl_price, tp_price, close_reason, score,
                 entry_remark, exit_remark)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order.get("order_id"),
                order.get("symbol"),
                order.get("side"),
                order.get("quantity"),
                order.get("price", 0),
                order.get("pnl", 0),
                order.get("status"),
                decision.get("reasoning"),
                decision.get("confidence", 0),
                decision.get("risk_level"),
                order.get("timestamp", now_ist().isoformat()),
                "paper" if config.IS_PAPER else "live",
                order.get("strategy"),
                order.get("option_type"),
                order.get("strike"),
                order.get("lot_size", 75),
                order.get("sl_price"),
                order.get("tp_price"),
                order.get("close_reason"),
                order.get("score"),
                order.get("entry_remark"),
                order.get("exit_remark"),
            ))
            return cursor.lastrowid

    def update_remarks(self, order_id: str, entry_remark: str = None, exit_remark: str = None):
        """Update AI-generated remarks on a trade."""
        with get_connection() as conn:
            if entry_remark:
                conn.execute("UPDATE trades SET entry_remark = ? WHERE order_id = ?",
                             (entry_remark, order_id))
            if exit_remark:
                conn.execute("UPDATE trades SET exit_remark = ? WHERE order_id = ?",
                             (exit_remark, order_id))

    def close_trade(self, order_id: str, pnl: float):
        """Update a trade with P&L when closed."""
        with get_connection() as conn:
            conn.execute("""
                UPDATE trades SET pnl = ?, closed_at = ? WHERE order_id = ?
            """, (pnl, now_ist().isoformat(), order_id))

    def get_trades_for_symbol(self, symbol: str, limit: int = 20) -> list:
        """Get recent trades for a symbol — used as context for Claude."""
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT symbol, side, quantity, price, pnl, status, action_reason, confidence, timestamp
                FROM trades WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?
            """, (symbol, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_today_trades(self) -> list:
        today = today_ist()
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM trades WHERE timestamp LIKE ? ORDER BY timestamp DESC
            """, (f"{today}%",)).fetchall()
        return [dict(r) for r in rows]

    def get_all_trades(self, limit: int = 100) -> list:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def save_daily_summary(self, date: str, trades: list, review: str = ""):
        pnls = [t.get("pnl", 0) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        with get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO daily_summary
                (date, total_trades, winning_trades, losing_trades, total_pnl, best_trade_pnl, worst_trade_pnl, review)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date,
                len(trades),
                len(wins),
                len(losses),
                sum(pnls),
                max(pnls) if pnls else 0,
                min(pnls) if pnls else 0,
                review,
            ))

    def save_market_snapshot(self, symbol: str, data: dict):
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO market_snapshots (symbol, data, timestamp) VALUES (?, ?, ?)
            """, (symbol, json.dumps(data), now_ist().isoformat()))
