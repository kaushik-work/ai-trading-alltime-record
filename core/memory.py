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
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
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
                underlying TEXT,
                option_type TEXT,
                strike REAL,
                expiry TEXT,
                exchange TEXT,
                product TEXT,
                order_type_meta TEXT,
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
        ("underlying",  "TEXT"),
        ("option_type", "TEXT"),
        ("strike",      "REAL"),
        ("expiry",      "TEXT"),
        ("exchange",    "TEXT"),
        ("product",     "TEXT"),
        ("order_type_meta", "TEXT"),
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
                 strategy, underlying, option_type, strike, expiry, exchange, product, order_type_meta,
                 lot_size, sl_price, tp_price, close_reason, score,
                 entry_remark, exit_remark)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                order.get("underlying"),
                order.get("option_type"),
                order.get("strike"),
                order.get("expiry"),
                order.get("exchange"),
                order.get("product"),
                order.get("order_type"),
                order.get("lot_size", config.LOT_SIZES.get("NIFTY", 65)),
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

    def close_latest_open_trade(self, symbol: str, strategy: str, pnl: float):
        """Mark the most recent open BUY row as closed for a completed exit."""
        with get_connection() as conn:
            conn.execute("""
                UPDATE trades
                SET pnl = ?, closed_at = ?
                WHERE id = (
                    SELECT id FROM trades
                    WHERE symbol = ? AND strategy = ? AND side = 'BUY' AND closed_at IS NULL
                    ORDER BY timestamp DESC
                    LIMIT 1
                )
            """, (pnl, now_ist().isoformat(), symbol, strategy))

    def has_open_trade(self, symbol: str, strategy: str) -> bool:
        """Return True when this strategy has an unclosed BUY for symbol."""
        with get_connection() as conn:
            row = conn.execute("""
                SELECT 1 FROM trades
                WHERE symbol = ? AND strategy = ? AND side = 'BUY' AND closed_at IS NULL
                LIMIT 1
            """, (symbol, strategy)).fetchone()
        return row is not None

    def has_open_underlying_today(self, underlying: str) -> bool:
        """Return True if ANY strategy has an unclosed BUY for this underlying today.

        Checks the `underlying` column (e.g. "NIFTY") so it catches any option
        contract on the same underlying. Scoped to today so stale rows from
        previous sessions don't block fresh entries.
        """
        today = today_ist()
        with get_connection() as conn:
            row = conn.execute("""
                SELECT 1 FROM trades
                WHERE underlying = ? AND side = 'BUY' AND closed_at IS NULL
                  AND timestamp >= ?
                LIMIT 1
            """, (underlying, f"{today}T00:00:00")).fetchone()
        return row is not None

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

    def build_round_trips(self, trades: Optional[list] = None) -> list:
        """
        Pair BUY/SELL rows into completed round trips.

        Keyed by option contract identity plus strategy so dashboard, journal,
        and analytics use the same trade counting model.
        """
        trade_rows = trades if trades is not None else self.get_all_trades(limit=2000)
        sorted_trades = sorted(trade_rows, key=lambda x: x.get("timestamp", ""))
        pending_buys: dict = {}
        round_trips: list = []

        for trade in sorted_trades:
            key = (
                trade.get("symbol"),
                trade.get("strategy"),
                trade.get("option_type"),
                trade.get("strike"),
                trade.get("expiry"),
            )
            side = trade.get("side")
            if side == "BUY":
                pending_buys[key] = trade
                continue
            if side != "SELL":
                continue

            buy = pending_buys.pop(key, None)
            round_trips.append({
                "strategy": trade.get("strategy") or (buy or {}).get("strategy"),
                "symbol": trade.get("symbol"),
                "underlying": trade.get("underlying") or (buy or {}).get("underlying"),
                "option_type": trade.get("option_type") or (buy or {}).get("option_type"),
                "strike": trade.get("strike") if trade.get("strike") is not None else (buy or {}).get("strike"),
                "expiry": trade.get("expiry") or (buy or {}).get("expiry"),
                "side": "BUY",
                "quantity": trade.get("quantity") or (buy or {}).get("quantity"),
                "lot_size": trade.get("lot_size") or (buy or {}).get("lot_size", config.LOT_SIZES.get("NIFTY", 65)),
                "entry_price": (buy or {}).get("price"),
                "exit_price": trade.get("price"),
                "avg_buy_price": trade.get("avg_buy_price") or (buy or {}).get("price"),
                "pnl": round(float(trade.get("pnl") or 0), 2),
                "close_reason": trade.get("close_reason") or (buy or {}).get("close_reason"),
                "score": (buy or {}).get("score", trade.get("score")),
                "entry_time": (buy or {}).get("timestamp"),
                "exit_time": trade.get("timestamp"),
                "closed_at": trade.get("closed_at") or (buy or {}).get("closed_at"),
                "status": trade.get("status"),
                "entry_remark": (buy or {}).get("entry_remark"),
                "exit_remark": trade.get("exit_remark"),
                "buy_order_id": (buy or {}).get("order_id"),
                "sell_order_id": trade.get("order_id"),
            })

        round_trips.sort(key=lambda x: x.get("exit_time", ""), reverse=True)
        return round_trips

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
