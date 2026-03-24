import sqlite3
import logging
from datetime import datetime
from typing import Optional
import config
from core.memory import get_connection

logger = logging.getLogger(__name__)


def init_records_db():
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS alltime_records (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL,
                symbol TEXT,
                order_id TEXT,
                achieved_at TEXT NOT NULL,
                description TEXT
            );
        """)


class RecordTracker:
    RECORDS = {
        "best_single_trade_pnl":    ("Best Single Trade P&L (₹)", "highest"),
        "worst_single_trade_pnl":   ("Worst Single Trade P&L (₹)", "lowest"),
        "best_day_pnl":             ("Best Day P&L (₹)", "highest"),
        "worst_day_pnl":            ("Worst Day P&L (₹)", "lowest"),
        "highest_win_streak":       ("Longest Win Streak", "highest"),
        "highest_loss_streak":      ("Longest Loss Streak", "highest"),
        "most_trades_in_a_day":     ("Most Trades in a Day", "highest"),
        "best_win_rate":            ("Best Win Rate (%)", "highest"),
        "largest_single_order":     ("Largest Single Order (₹)", "highest"),
    }

    def get_all_records(self) -> dict:
        with get_connection() as conn:
            rows = conn.execute("SELECT * FROM alltime_records").fetchall()
        return {r["key"]: dict(r) for r in rows}

    def get_record(self, key: str) -> Optional[dict]:
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM alltime_records WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def _update_record(self, key: str, value: float, symbol: str = None, order_id: str = None):
        description = self.RECORDS.get(key, (key, "highest"))[0]
        direction = self.RECORDS.get(key, (key, "highest"))[1]
        existing = self.get_record(key)

        is_new_record = False
        if existing is None:
            is_new_record = True
        elif direction == "highest" and value > existing["value"]:
            is_new_record = True
        elif direction == "lowest" and value < existing["value"]:
            is_new_record = True

        if is_new_record:
            with get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO alltime_records (key, value, symbol, order_id, achieved_at, description)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (key, value, symbol, order_id, datetime.now().isoformat(), description))
            logger.info("NEW ALL-TIME RECORD: %s = %.2f (%s)", description, value, symbol or "")
            return True
        return False

    def check_trade(self, trade: dict) -> list:
        """Check a single trade against all-time records. Returns list of broken records."""
        broken = []
        pnl = trade.get("pnl", 0)
        order_value = trade.get("price", 0) * trade.get("quantity", 0)
        symbol = trade.get("symbol")
        order_id = trade.get("order_id")

        if pnl > 0 and self._update_record("best_single_trade_pnl", pnl, symbol, order_id):
            broken.append("best_single_trade_pnl")
        if pnl < 0 and self._update_record("worst_single_trade_pnl", pnl, symbol, order_id):
            broken.append("worst_single_trade_pnl")
        if self._update_record("largest_single_order", order_value, symbol, order_id):
            broken.append("largest_single_order")

        return broken

    def check_daily(self, trades: list) -> list:
        """Check end-of-day stats against all-time records."""
        broken = []
        if not trades:
            return broken

        pnls = [t.get("pnl", 0) for t in trades]
        total_pnl = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        win_rate = (wins / len(pnls)) * 100 if pnls else 0

        if self._update_record("best_day_pnl", total_pnl):
            broken.append("best_day_pnl")
        if total_pnl < 0 and self._update_record("worst_day_pnl", total_pnl):
            broken.append("worst_day_pnl")
        if self._update_record("most_trades_in_a_day", len(trades)):
            broken.append("most_trades_in_a_day")
        if self._update_record("best_win_rate", win_rate):
            broken.append("best_win_rate")

        # Streak tracking
        win_streak = loss_streak = current_win = current_loss = 0
        for p in pnls:
            if p > 0:
                current_win += 1
                current_loss = 0
            elif p < 0:
                current_loss += 1
                current_win = 0
            win_streak = max(win_streak, current_win)
            loss_streak = max(loss_streak, current_loss)

        if self._update_record("highest_win_streak", win_streak):
            broken.append("highest_win_streak")
        if self._update_record("highest_loss_streak", loss_streak):
            broken.append("highest_loss_streak")

        return broken

    def format_records_display(self) -> str:
        records = self.get_all_records()
        if not records:
            return "No records yet. Start trading!"
        lines = ["=== ALL-TIME RECORDS ==="]
        for key, r in records.items():
            lines.append(f"🏆 {r['description']}: {r['value']:.2f} | {r.get('symbol','') or ''} | {r['achieved_at'][:10]}")
        return "\n".join(lines)
