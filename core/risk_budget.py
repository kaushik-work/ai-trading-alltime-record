"""
Risk-budget enforcer for the shadow + (eventually) live trading executor.

Single source of truth for:
  • Daily loss caps (aggregate + per-strategy)
  • Position sizing / lot multiplier (Kelly-derived, capped)
  • Capital exposure ceiling

Stateless across container restarts — reads counts/P&L from Mongo on every
check so multiple api containers can't drift.

All numbers are tuned for ₹50,000 capital. Don't change without re-running
the multi-strategy historical replay.

Decision policy:
  1. Before opening a new shadow trade, the executor calls allow_entry(strategy).
     Returns (True, "") to open or (False, "<reason>") to refuse.
  2. To size the trade, the executor calls current_lot_multiplier(strategy).
     Returns 1 (default) or 2 (scaled up). Never higher — ₹50K capital cannot
     safely hold 3+ NIFTY option lots concurrent.

Caps were chosen so the worst-case daily loss is ~7% of capital:
  • Per-strategy: ₹2,000 (4% of capital). One SL on a single lot = ~₹650, so
    cap kicks in after ~3 losing trades on a single strategy.
  • Aggregate:    ₹3,500 (7% of capital). Across all 3 strategies combined.
    Once hit, ALL new entries refused for the day. Open positions still tick
    to their exits normally.

The 2× lot scale-up only kicks in once we have meaningful sample (30+ closed
trades per strategy) — i.e. several weeks of forward data. This prevents
in-sample lottery wins from inflating size prematurely.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Tuple

logger = logging.getLogger(__name__)

# ── Capital + caps (₹50K capital baseline) ───────────────────────────────────
CAPITAL                  = 50_000
DAILY_AGG_LOSS_CAP       = 3_500     # ALL strategies combined
PER_STRAT_LOSS_CAP       = 2_000     # any single strategy

# ── Sizing rules ─────────────────────────────────────────────────────────────
MAX_LOT_MULTIPLIER       = 2
SCALE_UP_MIN_CLOSED      = 30        # need ≥30 closed trades to consider scale-up
SCALE_UP_MIN_WR          = 0.50      # rolling 10-trade WR > 50%
SCALE_UP_MIN_PF          = 2.0       # rolling 10-trade PF > 2.0
SCALE_DOWN_SL_STREAK     = 2         # if last N trades on this strategy are SL → back to 1×


def _today_str() -> str:
    return datetime.now().date().isoformat()


def _aggregate_pnl_today(db) -> float:
    """Sum of all CLOSED shadow P&L across all strategies today."""
    try:
        cursor = db.shadow_trades.find(
            {"date": _today_str(), "status": "CLOSED"},
            projection={"_id": 0, "pnl": 1},
        )
        return sum((r.get("pnl") or 0) for r in cursor)
    except Exception:
        return 0.0


def _strategy_pnl_today(db, strategy: str) -> float:
    """Sum of CLOSED shadow P&L for one strategy today."""
    try:
        cursor = db.shadow_trades.find(
            {"date": _today_str(), "status": "CLOSED", "strategy": strategy},
            projection={"_id": 0, "pnl": 1},
        )
        return sum((r.get("pnl") or 0) for r in cursor)
    except Exception:
        return 0.0


def _recent_closed(db, strategy: str, n: int) -> list:
    """Last N closed trades for one strategy, oldest-first."""
    try:
        cursor = db.shadow_trades.find(
            {"strategy": strategy, "status": "CLOSED"},
            projection={"_id": 0, "pnl": 1, "reason": 1, "entry_dt": 1},
            sort=[("entry_dt", -1)],
            limit=n,
        )
        rows = list(cursor)
        rows.reverse()   # oldest-first
        return rows
    except Exception:
        return []


def strike_already_occupied(strike: int, side: str, exclude_strategy: str = "") -> bool:
    """True if ANY shadow strategy already has an OPEN position on this
    strike + side. Prevents the three signals from triple-betting on the same
    bar when they all fire together (the 'same-strike correlation' problem).
    """
    try:
        from core import mongo
        db = mongo.get_db()
        if db is None:
            return False
        q = {
            "strike":   int(strike),
            "side":     side,
            "status":   "OPEN",
            "date":     _today_str(),
        }
        if exclude_strategy:
            q["strategy"] = {"$ne": exclude_strategy}
        return db.shadow_trades.count_documents(q) > 0
    except Exception:
        return False


def allow_entry(strategy: str, strike: int = 0, side: str = "") -> Tuple[bool, str]:
    """
    Returns (allowed, reason). Reason is empty when allowed.

    Refuses if:
      • Aggregate today P&L ≤ -DAILY_AGG_LOSS_CAP
      • This strategy's today P&L ≤ -PER_STRAT_LOSS_CAP
      • Another strategy already has an OPEN trade at the same (strike, side)
    """
    try:
        from core import mongo
        db = mongo.get_db()
        if db is None:
            # Mongo down → fail OPEN (allow trade). The shadow executor itself
            # also no-ops if Mongo is down, so this path is mostly defensive.
            return True, ""

        agg = _aggregate_pnl_today(db)
        if agg <= -DAILY_AGG_LOSS_CAP:
            return False, f"agg daily loss cap hit: Rs {agg:+.0f} <= -{DAILY_AGG_LOSS_CAP}"

        strat = _strategy_pnl_today(db, strategy)
        if strat <= -PER_STRAT_LOSS_CAP:
            return False, f"{strategy} daily loss cap hit: Rs {strat:+.0f} <= -{PER_STRAT_LOSS_CAP}"

        # Same-strike correlation guard
        if strike and side and strike_already_occupied(strike, side,
                                                       exclude_strategy=strategy):
            return False, f"strike {strike}{side} already held by another strategy"

        return True, ""
    except Exception as e:
        logger.warning("risk_budget.allow_entry failed (defaulting to allow): %s", e)
        return True, ""


def current_lot_multiplier(strategy: str) -> int:
    """
    Returns 1 or MAX_LOT_MULTIPLIER (currently 2).

    Default: 1.
    Scale up to 2× if BOTH:
      • Total closed count for this strategy ≥ SCALE_UP_MIN_CLOSED
      • Last 10 trades: WR > SCALE_UP_MIN_WR AND PF > SCALE_UP_MIN_PF
    Scale back to 1× if last SCALE_DOWN_SL_STREAK trades were all SL.
    """
    try:
        from core import mongo
        db = mongo.get_db()
        if db is None:
            return 1

        total_closed = db.shadow_trades.count_documents({
            "strategy": strategy, "status": "CLOSED",
        })
        if total_closed < SCALE_UP_MIN_CLOSED:
            return 1

        recent = _recent_closed(db, strategy, 10)
        if len(recent) < 5:
            return 1

        # De-scale guard: SL streak
        last_n = recent[-SCALE_DOWN_SL_STREAK:]
        if len(last_n) == SCALE_DOWN_SL_STREAK and all(
            (t.get("reason") == "SL") for t in last_n
        ):
            return 1

        wins   = [t["pnl"] for t in recent if (t.get("pnl") or 0) > 0]
        losses = [t["pnl"] for t in recent if (t.get("pnl") or 0) < 0]
        wr = len(wins) / len(recent)
        gw = sum(wins)
        gl = abs(sum(losses))
        pf = (gw / gl) if gl > 0 else float("inf")

        if wr > SCALE_UP_MIN_WR and pf > SCALE_UP_MIN_PF:
            return MAX_LOT_MULTIPLIER
        return 1
    except Exception as e:
        logger.debug("risk_budget.current_lot_multiplier failed: %s", e)
        return 1


def status_snapshot() -> dict:
    """Read-only summary for the API endpoint."""
    try:
        from core import mongo
        db = mongo.get_db()
        if db is None:
            return {"enabled": False}
        agg = _aggregate_pnl_today(db)
        out = {
            "enabled":            True,
            "capital":            CAPITAL,
            "today_agg_pnl":      round(agg, 2),
            "agg_loss_cap":       DAILY_AGG_LOSS_CAP,
            "agg_loss_remaining": round(DAILY_AGG_LOSS_CAP + agg, 2),
            "agg_cap_hit":        agg <= -DAILY_AGG_LOSS_CAP,
            "strategies":         {},
        }
        for strat in ("q5_straddle_level", "q5_straddle_mom3", "q5_pcr_mom3"):
            s_pnl = _strategy_pnl_today(db, strat)
            mult  = current_lot_multiplier(strat)
            out["strategies"][strat] = {
                "today_pnl":      round(s_pnl, 2),
                "loss_cap":       PER_STRAT_LOSS_CAP,
                "loss_remaining": round(PER_STRAT_LOSS_CAP + s_pnl, 2),
                "cap_hit":        s_pnl <= -PER_STRAT_LOSS_CAP,
                "lot_multiplier": mult,
            }
        return out
    except Exception as e:
        return {"enabled": False, "error": str(e)}
