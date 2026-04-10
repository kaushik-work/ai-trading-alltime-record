"""
DailyJournal — saves each trading day as a JSON file in journals/YYYY-MM-DD.json

Structure:
  {
    "date":               "2026-03-25",
    "saved_at":           "2026-03-25T15:20:01",
    "summary": {
      "total_pnl":        1250.00,
      "total_trades":     5,
      "completed_trades": 4,
      "wins":             3,
      "losses":           1,
      "win_rate":         75.0
    },
    "strategy_breakdown": {
      "Musashi":     {"trades": 2, "pnl": 800.0, "wins": 1, "losses": 1},
      ...
    },
    "trades": [
      {
        "strategy":     "Musashi",
        "symbol":       "NIFTY",
        "option_type":  "CE",
        "strike":       22500,
        "side":         "BUY",
        "entry_price":  220.50,
        "exit_price":   265.30,   # from close SELL row, or null if still open
        "lot_size":     65,
        "pnl":          3360.00,
        "close_reason": "TP",
        "score":        7.5,
        "entry_time":   "...",
        "exit_time":    "...",
        "entry_remark": "...",
        "exit_remark":  "..."
      }
    ],
    "learning_notes":   ""        # blank — user fills in manually later
  }
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional
from core.utils import now_ist, today_ist

import config
from core import ipc
from core.memory import TradeMemory

logger = logging.getLogger(__name__)

STRATEGIES = ["ATR Intraday", "C-ICT", "Fib-OF"]


def _ensure_dir():
    os.makedirs(config.JOURNALS_DIR, exist_ok=True)


def _journal_path(date_str: str) -> str:
    return os.path.join(config.JOURNALS_DIR, f"{date_str}.json")


def _collect_vix_context() -> dict:
    """Collect today's VIX level and per-strategy override decisions."""
    try:
        from data.zerodha_fetcher import ZerodhaFetcher
        vix = ZerodhaFetcher.get().fetch_vix()
    except Exception:
        vix = None

    vix_override_global = ipc.flag_exists(ipc.FLAG_VIX_OVERRIDE)
    vix_override_atr    = ipc.flag_exists(ipc.FLAG_VIX_OVERRIDE_ATR)
    vix_override_ict    = ipc.flag_exists(ipc.FLAG_VIX_OVERRIDE_ICT)
    vix_override_fib    = ipc.flag_exists(ipc.FLAG_VIX_OVERRIDE_FIB)
    threshold           = config.VIX_THRESHOLD

    blocked = (vix is not None) and (vix > threshold) and not vix_override_global

    return {
        "india_vix":         vix,
        "threshold":         threshold,
        "blocked_by_vix":    blocked,
        "override_global":   vix_override_global,
        "override_atr":      vix_override_atr,
        "override_ict":      vix_override_ict,
        "override_fib":      vix_override_fib,
        "learning": _analyse_vix_decision(vix, threshold, vix_override_global, vix_override_atr, vix_override_ict, vix_override_fib),
    }


def _analyse_vix_decision(vix, threshold, override_global, override_atr, override_ict, override_fib) -> str:
    """Generate a human-readable analysis of today's VIX-related decisions."""
    if vix is None:
        return "VIX data unavailable today — no VIX gate decision recorded."
    lines = [f"India VIX today: {vix:.1f} (threshold: {threshold})"]
    if vix <= threshold:
        lines.append("VIX was within normal range — gate was open, no override needed.")
    else:
        lines.append(f"VIX exceeded threshold ({vix:.1f} > {threshold}).")
        if override_global:
            lines.append("GLOBAL VIX override was ON — both strategies traded through high VIX.")
        else:
            atr_status = "bypassed (override ON)" if override_atr else "blocked"
            ict_status = "bypassed (override ON)" if override_ict else "blocked"
            fib_status = "bypassed (override ON)" if override_fib else "blocked"
            lines.append(f"ATR Intraday: {atr_status}. C-ICT: {ict_status}. Fib-OF: {fib_status}.")
    return " ".join(lines)


def _analyse_day_bias(day_bias: dict, trades_list: list) -> dict:
    """Evaluate if the day bias was correct and helpful based on trade outcomes."""
    bias = day_bias.get("bias", "NEUTRAL")
    note = day_bias.get("note", "")
    set_at = day_bias.get("set_at")

    if bias == "NEUTRAL" or not set_at:
        return {
            "bias_set": bias,
            "note": note,
            "was_helpful": None,
            "analysis": "No directional bias was set for today.",
        }

    # Check if trades aligned with bias
    bias_direction = "BUY" if bias == "BULLISH" else ("SELL" if bias == "BEARISH" else None)
    aligned_trades = [t for t in trades_list if bias_direction and t.get("side") == bias_direction]
    aligned_pnl    = sum(t["pnl"] for t in aligned_trades)
    total_pnl      = sum(t["pnl"] for t in trades_list) if trades_list else 0

    was_helpful = None
    analysis_parts = [f"Day bias was set to {bias} ('{note}' at {set_at})."]

    if not trades_list:
        analysis_parts.append("No trades were taken today — bias could not be validated.")
    else:
        if aligned_trades:
            analysis_parts.append(
                f"{len(aligned_trades)} trade(s) aligned with {bias} bias — PnL: ₹{aligned_pnl:.2f}."
            )
            was_helpful = aligned_pnl > 0
            if was_helpful:
                analysis_parts.append("Bias was HELPFUL — aligned trades were profitable.")
            else:
                analysis_parts.append("Bias was UNHELPFUL — aligned trades were losing. Consider reviewing conviction before next bias call.")
        else:
            analysis_parts.append(f"No trades matched the {bias} bias direction. Bias was not tested today.")

        if total_pnl != 0:
            analysis_parts.append(f"Total day PnL: ₹{total_pnl:.2f}.")

    return {
        "bias_set":    bias,
        "note":        note,
        "set_at":      set_at,
        "was_helpful": was_helpful,
        "analysis":    " ".join(analysis_parts),
    }


def save_daily_journal(date_str: Optional[str] = None) -> str:
    """
    Build and save today's trading journal as JSON.
    Returns the file path saved.
    """
    _ensure_dir()
    memory = TradeMemory()

    if date_str is None:
        date_str = today_ist()

    # Pull today's trades from DB
    today_trades = memory.get_today_trades()

    round_trips = memory.build_round_trips(today_trades)

    trades_list = []
    for trip in round_trips:
        trades_list.append({
            "strategy":     trip.get("strategy", "—"),
            "symbol":       trip.get("symbol"),
            "underlying":   trip.get("underlying"),
            "option_type":  trip.get("option_type", "—"),
            "strike":       trip.get("strike"),
            "expiry":       trip.get("expiry"),
            "side":         trip.get("side", "BUY"),
            "entry_price":  trip.get("entry_price"),
            "exit_price":   trip.get("exit_price"),
            "lot_size":     trip.get("lot_size", config.LOT_SIZES.get("NIFTY", 65)),
            "pnl":          round(trip.get("pnl", 0), 2),
            "close_reason": trip.get("close_reason", "—"),
            "score":        trip.get("score"),
            "entry_time":   trip.get("entry_time"),
            "exit_time":    trip.get("exit_time"),
            "entry_remark": trip.get("entry_remark", ""),
            "exit_remark":  trip.get("exit_remark", ""),
        })

    # Summary stats
    total_pnl  = round(sum(t["pnl"] for t in trades_list), 2)
    wins       = sum(1 for t in trades_list if t["pnl"] > 0)
    losses     = sum(1 for t in trades_list if t["pnl"] < 0)
    win_rate   = round(wins / len(trades_list) * 100, 1) if trades_list else 0.0

    # Strategy breakdown
    strategy_breakdown = {}
    for strat in STRATEGIES:
        strat_trades = [t for t in trades_list if t["strategy"] == strat]
        strat_pnl    = round(sum(t["pnl"] for t in strat_trades), 2)
        strat_wins   = sum(1 for t in strat_trades if t["pnl"] > 0)
        strategy_breakdown[strat] = {
            "trades": len(strat_trades),
            "pnl":    strat_pnl,
            "wins":   strat_wins,
            "losses": len(strat_trades) - strat_wins,
        }

    # VIX context + per-strategy override analysis
    vix_context = _collect_vix_context()

    # Day bias analysis — was trader's directional call correct?
    day_bias    = ipc.read_day_bias()
    bias_review = _analyse_day_bias(day_bias, trades_list)

    journal = {
        "date":      date_str,
        "saved_at":  now_ist().isoformat(),
        "summary": {
            "total_pnl":        total_pnl,
            "total_trades":     len(round_trips),
            "completed_trades": len(trades_list),
            "wins":             wins,
            "losses":           losses,
            "win_rate":         win_rate,
        },
        "strategy_breakdown": strategy_breakdown,
        "vix_context":  vix_context,
        "bias_review":  bias_review,
        "trades":       trades_list,
        "learning_notes": (
            "Auto-generated insights:\n"
            f"• VIX: {vix_context['learning']}\n"
            f"• Day bias: {bias_review['analysis']}\n"
            "(Add your manual notes here after reviewing the day.)"
        ),
    }

    path = _journal_path(date_str)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(journal, f, indent=2, ensure_ascii=False)

    logger.info("Daily journal saved → %s (%d trades, PnL=₹%.2f)", path, len(trades_list), total_pnl)
    return path


def load_journal(date_str: str) -> Optional[dict]:
    """Load a saved journal by date string (YYYY-MM-DD). Returns None if not found."""
    path = _journal_path(date_str)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def update_learning_notes(date_str: str, notes: str) -> bool:
    """Append/replace learning notes in an existing journal file."""
    journal = load_journal(date_str)
    if journal is None:
        return False
    journal["learning_notes"] = notes
    journal["notes_updated_at"] = now_ist().isoformat()
    path = _journal_path(date_str)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(journal, f, indent=2, ensure_ascii=False)
    return True


def list_journals() -> list:
    """Return a list of all saved journal dates (sorted newest first)."""
    _ensure_dir()
    files = [
        f[:-5] for f in os.listdir(config.JOURNALS_DIR)
        if f.endswith(".json") and len(f) == 15  # YYYY-MM-DD.json
    ]
    return sorted(files, reverse=True)
