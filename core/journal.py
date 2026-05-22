"""
Daily journal — shadow-trading summary.

The legacy ATR-trade journal (with Claude AI review, weekly reviews, bias
analysis, OHLC fetch) was retired alongside the ATR strategy. This module
now writes a simple daily summary of the shadow ledger to:

  • Mongo  collection daily_journals (one upsert per date)
  • Disk   journals/<YYYY-MM-DD>.json   (audit copy)

Called from bot_runner._save_journal at 15:25 IST.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

JOURNAL_DIR = Path(config.BASE_DIR) / "journals"


def _journal_path(date_str: str) -> str:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    return str(JOURNAL_DIR / f"{date_str}.json")


def _gather_shadow_summary(date_str: str) -> dict:
    """Pull all shadow trades for the date from Mongo and aggregate."""
    out: dict = {"date": date_str, "strategies": {}, "totals": {}}
    try:
        from core import mongo
        db = mongo.get_db()
        if db is None:
            return out
        rows = list(db.shadow_trades.find(
            {"date": date_str},
            projection={"_id": 0},
            sort=[("entry_dt", 1)],
        ))
        out["trades"] = rows

        all_strats = sorted({r.get("strategy", "unknown") for r in rows})
        for s in all_strats:
            sub = [r for r in rows if r.get("strategy") == s]
            closed = [r for r in sub if r.get("status") == "CLOSED"]
            wins   = [r for r in closed if (r.get("pnl") or 0) > 0]
            losses = [r for r in closed if (r.get("pnl") or 0) < 0]
            gw     = sum(r["pnl"] for r in wins)
            gl     = abs(sum(r["pnl"] for r in losses))
            out["strategies"][s] = {
                "entries":     len(sub),
                "closed":      len(closed),
                "wins":        len(wins),
                "losses":      len(losses),
                "pnl":         round(sum((r.get("pnl") or 0) for r in closed), 2),
                "win_rate":    round(len(wins) / len(closed) * 100, 1) if closed else 0,
                "profit_factor": round(gw / gl, 2) if gl > 0 else None,
            }

        closed_all = [r for r in rows if r.get("status") == "CLOSED"]
        wins_all   = [r for r in closed_all if (r.get("pnl") or 0) > 0]
        losses_all = [r for r in closed_all if (r.get("pnl") or 0) < 0]
        gw = sum(r["pnl"] for r in wins_all)
        gl = abs(sum(r["pnl"] for r in losses_all))
        out["totals"] = {
            "entries":       len(rows),
            "closed":        len(closed_all),
            "wins":          len(wins_all),
            "losses":        len(losses_all),
            "pnl":           round(sum((r.get("pnl") or 0) for r in closed_all), 2),
            "win_rate":      round(len(wins_all) / len(closed_all) * 100, 1)
                              if closed_all else 0,
            "profit_factor": round(gw / gl, 2) if gl > 0 else None,
        }
    except Exception as e:
        logger.warning("journal: shadow summary failed: %s", e)
    return out


def save_daily_journal(date_str: Optional[str] = None) -> str:
    """Write today's shadow summary to Mongo + disk. Returns the disk path."""
    if date_str is None:
        date_str = datetime.now(IST).date().isoformat()

    journal = _gather_shadow_summary(date_str)
    journal["saved_at"] = datetime.now(IST).isoformat()
    journal["mode"]     = "shadow"

    # Mongo upsert
    try:
        from core import mongo
        mongo.mirror_journal(date_str, journal)
    except Exception as e:
        logger.warning("journal: mongo mirror failed: %s", e)

    # Disk
    path = _journal_path(date_str)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(journal, f, indent=2, default=str)
    logger.info("Journal written: %s (pnl=Rs %+.2f, %d trades)",
                path, journal["totals"].get("pnl", 0),
                journal["totals"].get("entries", 0))
    return path


def load_journal(date_str: str) -> Optional[dict]:
    path = _journal_path(date_str)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_journals() -> list:
    """All journal dates, newest first."""
    if not JOURNAL_DIR.exists():
        return []
    return sorted(
        (p.stem for p in JOURNAL_DIR.glob("*.json")),
        reverse=True,
    )


def update_learning_notes(date_str: str, notes: str) -> bool:
    """Persist a free-text learning note onto today's journal."""
    journal = load_journal(date_str)
    if journal is None:
        return False
    journal["learning_notes"] = notes
    journal["notes_updated_at"] = datetime.now(IST).isoformat()
    with open(_journal_path(date_str), "w", encoding="utf-8") as f:
        json.dump(journal, f, indent=2, default=str)
    try:
        from core import mongo
        mongo.mirror_journal(date_str, journal)
    except Exception:
        pass
    return True
