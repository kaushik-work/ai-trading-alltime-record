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

            CREATE TABLE IF NOT EXISTS signal_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT NOT NULL,
                date            TEXT NOT NULL,
                strategy        TEXT NOT NULL,
                symbol          TEXT NOT NULL DEFAULT 'NIFTY',
                score           REAL NOT NULL,
                threshold       REAL NOT NULL,
                direction       TEXT NOT NULL,
                will_trade      INTEGER NOT NULL DEFAULT 0,
                did_trade       INTEGER NOT NULL DEFAULT 0,
                reason_skipped  TEXT,
                nifty_spot      REAL,
                option_type     TEXT,
                strike          INTEGER,
                option_premium  REAL,
                signals_fired   TEXT
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


def _compute_pnl(sell_trade: dict, buy_trade: dict | None) -> float:
    """
    Compute round-trip P&L.
    Prefer the stored pnl on the SELL row (set by _execute after our fix).
    Fall back to computing from prices if stored value is 0 or missing —
    handles trades logged before the fix was deployed.
    """
    stored = sell_trade.get("pnl")
    if stored and float(stored) != 0.0:
        return round(float(stored), 2)
    # Fallback: compute from prices
    exit_px  = float(sell_trade.get("price") or 0)
    entry_px = float((buy_trade or {}).get("price") or 0)
    qty      = int(sell_trade.get("quantity") or (buy_trade or {}).get("quantity") or 0)
    if entry_px > 0 and exit_px > 0 and qty > 0:
        return round((exit_px - entry_px) * qty, 2)
    return 0.0


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
                # Honor explicit "mode" on the order (e.g. virtual_rejected) so
                # rejected entries and their virtual exits aren't misclassified
                # as live/paper trades on the PPnL dashboard.
                order.get("mode") or ("paper" if config.IS_PAPER else "live"),
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
            row_id = cursor.lastrowid
        # Mirror to Mongo (fire-and-forget; failures swallowed)
        try:
            from core import mongo
            mongo.mirror_trade(order, decision)
        except Exception:
            pass
        return row_id

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
        closed_at = now_ist().isoformat()
        with get_connection() as conn:
            conn.execute("""
                UPDATE trades SET pnl = ?, closed_at = ? WHERE order_id = ?
            """, (pnl, closed_at, order_id))
        try:
            from core import mongo
            mongo.mirror_trade_close(order_id, pnl, closed_at)
        except Exception:
            pass

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

    def has_open_for_strategy(self, strategy: str, underlying: str) -> bool:
        """Return True if THIS strategy has an unclosed BUY for the underlying today.

        Use this when enforcing "one live trade per strategy at a time" — if the
        bot ever runs more than one strategy concurrently again, each strategy's
        open position must not block the others. Includes virtual_rejected rows
        so a rejected trade still blocks fresh entries until it's resolved.
        """
        today = today_ist()
        with get_connection() as conn:
            row = conn.execute("""
                SELECT 1 FROM trades
                WHERE strategy = ? AND underlying = ?
                  AND side = 'BUY' AND closed_at IS NULL
                  AND timestamp >= ?
                LIMIT 1
            """, (strategy, underlying, f"{today}T00:00:00")).fetchone()
        return row is not None

    def close_virtual_rejected_today(self, underlying: str, strategy: str) -> int:
        """Close any open virtual_rejected rows for this strategy + underlying today.

        Used by the EOD square-off path: real positions get closed via the
        broker; virtual_rejected entries (Angel One rejected the order, no
        actual position exists) need to be closed directly in the DB so they
        don't block tomorrow's duplicate guard. Returns rows updated.

        Mirrors the closure to Mongo so the dashboard reflects EOD state.
        """
        today = today_ist()
        closed_at = now_ist().isoformat()
        with get_connection() as conn:
            # Fetch the rows we're about to close so we can mirror each to Mongo
            rows_before = conn.execute("""
                SELECT order_id FROM trades
                WHERE strategy = ? AND underlying = ?
                  AND mode = 'virtual_rejected'
                  AND side = 'BUY' AND closed_at IS NULL
                  AND timestamp >= ?
            """, (strategy, underlying, f"{today}T00:00:00")).fetchall()
            cur = conn.execute("""
                UPDATE trades
                SET closed_at = ?, exit_remark = 'virtual EOD close (no real position)'
                WHERE strategy = ? AND underlying = ?
                  AND mode = 'virtual_rejected'
                  AND side = 'BUY' AND closed_at IS NULL
                  AND timestamp >= ?
            """, (closed_at, strategy, underlying, f"{today}T00:00:00"))
            rows = cur.rowcount
        # Mongo mirror
        if rows and rows_before:
            try:
                from core import mongo
                for r in rows_before:
                    if r["order_id"]:
                        mongo.mirror_trade_close(r["order_id"], 0.0, closed_at)
            except Exception:
                pass
        return rows

    def close_open_underlying_today(self, underlying: str, close_reason: str = "manual") -> int:
        """Mark all unclosed BUY rows for this underlying today as manually closed. Returns rows updated."""
        today = today_ist()
        with get_connection() as conn:
            cur = conn.execute("""
                UPDATE trades SET closed_at = ?, exit_remark = ?
                WHERE underlying = ? AND side = 'BUY' AND closed_at IS NULL
                  AND timestamp >= ?
            """, (now_ist().isoformat(), close_reason, underlying, f"{today}T00:00:00"))
            return cur.rowcount

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
                "pnl": _compute_pnl(trade, buy),
                "close_reason": trade.get("close_reason") or (buy or {}).get("close_reason"),
                "score": (buy or {}).get("score", trade.get("score")),
                "entry_time": (buy or {}).get("timestamp"),
                "exit_time": trade.get("timestamp"),
                "closed_at": trade.get("closed_at") or (buy or {}).get("closed_at"),
                "status": trade.get("status"),
                "mode": trade.get("mode") or (buy or {}).get("mode"),
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

    def get_open_trade_for_symbol(self, symbol: str) -> Optional[dict]:
        """Return the unclosed BUY row for this symbol (any strategy), or None."""
        with get_connection() as conn:
            row = conn.execute("""
                SELECT * FROM trades
                WHERE symbol = ? AND side = 'BUY' AND closed_at IS NULL
                ORDER BY timestamp DESC LIMIT 1
            """, (symbol,)).fetchone()
        return dict(row) if row else None

    def get_open_trade_for_underlying(self, underlying: str) -> Optional[dict]:
        """Return the unclosed BUY row for this underlying (any strategy), or None."""
        with get_connection() as conn:
            row = conn.execute("""
                SELECT * FROM trades
                WHERE underlying = ? AND side = 'BUY' AND closed_at IS NULL
                ORDER BY timestamp DESC LIMIT 1
            """, (underlying,)).fetchone()
        return dict(row) if row else None

    def save_market_snapshot(self, symbol: str, data: dict):
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO market_snapshots (symbol, data, timestamp) VALUES (?, ?, ?)
            """, (symbol, json.dumps(data), now_ist().isoformat()))


def log_signal(
    strategy: str,
    score: float,
    threshold: float,
    direction: str,
    will_trade: bool,
    did_trade: bool = False,
    reason_skipped: str = "",
    nifty_spot: float = 0,
    option_type: str = "",
    strike: int = 0,
    option_premium: float = 0,
    signals_fired: str = "",
    symbol: str = "NIFTY",
):
    """Persist every 5-min signal evaluation — trade or no-trade — to signal_log."""
    ts = now_ist().isoformat()
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO signal_log
            (timestamp, date, strategy, symbol, score, threshold, direction,
             will_trade, did_trade, reason_skipped, nifty_spot, option_type,
             strike, option_premium, signals_fired)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ts, ts[:10], strategy, symbol,
            round(score, 2), round(threshold, 2), direction,
            int(will_trade), int(did_trade), reason_skipped,
            round(nifty_spot, 2) if nifty_spot else None,
            option_type or None,
            strike or None,
            round(option_premium, 2) if option_premium else None,
            signals_fired or None,
        ))
    try:
        from core import mongo
        mongo.mirror_signal({
            "timestamp":      ts,
            "date":           ts[:10],
            "strategy":       strategy,
            "symbol":         symbol,
            "score":          round(score, 2),
            "threshold":      round(threshold, 2),
            "direction":      direction,
            "will_trade":     bool(will_trade),
            "did_trade":      bool(did_trade),
            "reason_skipped": reason_skipped,
            "nifty_spot":     round(nifty_spot, 2) if nifty_spot else None,
            "option_type":    option_type or None,
            "strike":         strike or None,
            "option_premium": round(option_premium, 2) if option_premium else None,
            "signals_fired":  signals_fired or None,
        })
    except Exception:
        pass


def get_signal_log(date: str = None, limit: int = 500) -> list:
    """Return signal evaluations, newest first. Optionally filter by date (YYYY-MM-DD)."""
    with get_connection() as conn:
        if date:
            rows = conn.execute("""
                SELECT * FROM signal_log WHERE date = ?
                ORDER BY timestamp DESC LIMIT ?
            """, (date, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM signal_log ORDER BY timestamp DESC LIMIT ?
            """, (limit,)).fetchall()
    return [dict(r) for r in rows]
