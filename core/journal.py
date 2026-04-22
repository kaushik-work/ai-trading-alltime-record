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
from typing import Optional
from core.utils import now_ist, today_ist

import config
from core import ipc
from core.memory import TradeMemory

logger = logging.getLogger(__name__)

STRATEGIES = ["ATR Intraday", "C-ICT"]


def _ensure_dir():
    os.makedirs(config.JOURNALS_DIR, exist_ok=True)


def _fetch_nifty_day_ohlc() -> dict:
    try:
        from data.angel_fetcher import AngelFetcher
        fetcher = AngelFetcher.get()
        if not fetcher._ensure_logged_in():
            return {}
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        now = _dt.now(ZoneInfo("Asia/Kolkata"))
        today = now.strftime("%Y-%m-%d")
        rows = fetcher._candle_data("99926000", "NSE", "ONE_DAY",
                                    f"{today} 09:15", f"{today} 15:30")
        if not rows:
            return {}
        r = rows[-1]
        return {
            "open":   float(r[1]),
            "high":   float(r[2]),
            "low":    float(r[3]),
            "close":  float(r[4]),
            "volume": int(r[5]) if len(r) > 5 else 0,
        }
    except Exception as e:
        logger.warning("_fetch_nifty_day_ohlc: %s", e)
        return {}


def generate_ai_review(journal: dict, recent_journals: list) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    date_str  = journal.get("date", "?")
    summary   = journal.get("summary", {})
    trades    = journal.get("trades", [])
    vix_ctx   = journal.get("vix_context", {})
    breakdown = journal.get("strategy_breakdown", {})
    nifty     = journal.get("nifty_day", {})

    from datetime import date as _date, datetime as _dt
    try:
        d        = _date.fromisoformat(date_str)
        weekday  = d.strftime("%A")
        is_expiry = d.weekday() == 3  # Thursday = NIFTY expiry
    except Exception:
        weekday   = "?"
        is_expiry = False

    if nifty:
        change   = nifty.get("close", 0) - nifty.get("open", 0)
        pct      = (change / nifty["open"]) * 100 if nifty.get("open") else 0
        direction = "UP" if change >= 0 else "DOWN"
        nifty_line = (
            f"Open {nifty['open']:.0f} | High {nifty['high']:.0f} | "
            f"Low {nifty['low']:.0f} | Close {nifty['close']:.0f} | "
            f"Range {nifty['high'] - nifty['low']:.0f}pts | {direction} {pct:+.2f}%"
        )
    else:
        nifty_line = "NIFTY data unavailable"

    vix        = vix_ctx.get("india_vix")
    vix_str    = f"{vix:.1f}" if vix else "N/A"
    vix_status = "ELEVATED — blocked trading" if vix_ctx.get("blocked_by_vix") else "normal"

    trade_lines = []
    for t in trades:
        et = t.get("entry_time", "")
        try:
            et = _dt.fromisoformat(et).strftime("%H:%M")
        except Exception:
            pass
        pnl   = t.get("pnl", 0)
        pnl_s = f"+₹{pnl:,.0f}" if pnl >= 0 else f"-₹{abs(pnl):,.0f}"
        line  = (
            f"  [{et}] {t.get('strategy','?')} | {t.get('option_type','?')} "
            f"{t.get('strike','?')} @ ₹{t.get('entry_price',0):.0f}→₹{t.get('exit_price',0):.0f} "
            f"{pnl_s} [{t.get('close_reason','?')}] score={t.get('score','?')}"
        )
        if t.get("entry_remark"):
            line += f"\n    Signal: {t['entry_remark']}"
        if t.get("exit_remark"):
            line += f"\n    Note: {t['exit_remark']}"
        trade_lines.append(line)
    trades_block = "\n".join(trade_lines) if trade_lines else "  No trades today."

    bd_lines = [
        f"  {s}: ₹{d.get('pnl',0):+.0f} ({d.get('wins',0)}W/{d.get('losses',0)}L)"
        for s, d in breakdown.items() if d.get("trades", 0) > 0
    ]
    bd_block = "\n".join(bd_lines) if bd_lines else "  No activity."

    recent_lines = [
        f"  {r.get('date','?')}: ₹{r.get('summary',{}).get('total_pnl',0):+.0f} "
        f"({r.get('summary',{}).get('wins',0)}W/{r.get('summary',{}).get('losses',0)}L "
        f"| {r.get('summary',{}).get('win_rate',0):.0f}% WR)"
        for r in recent_journals[-5:]
    ]
    recent_block = "\n".join(recent_lines) if recent_lines else "  No prior data."

    prompt = f"""You are a professional NIFTY options algorithmic trading coach. Review today's bot performance with brutal honesty and data-driven specificity.

DATE: {date_str} ({weekday}){" — NIFTY EXPIRY DAY" if is_expiry else ""}
NIFTY: {nifty_line}
India VIX: {vix_str} ({vix_status})

TODAY'S TRADES ({summary.get('completed_trades', 0)} trades | {summary.get('wins', 0)}W/{summary.get('losses', 0)}L | ₹{summary.get('total_pnl', 0):+.0f}):
{trades_block}

STRATEGY P&L:
{bd_block}

RECENT 5 DAYS:
{recent_block}

---
Write a 350-400 word review. Use this exact structure:

**What Worked**
[Reference specific trades by time and price. Explain WHY — market condition, signal, timing]

**What Failed**
[Root cause — market direction mismatch, signal noise, bad timing window, VIX? Be specific, not generic]

**Pattern of the Day**
[One non-obvious pattern from today's numbers — something not visible without reading the data]

**Tomorrow's Adjustment**
[One concrete change directly supported by today's data: a time filter, VIX gate, strategy priority. Not "be more careful"]

**Verdict**
[One sentence. Honest.]

No filler. No "it's important to note that...". The reader is a serious algorithmic trader who wants truth."""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.error("generate_ai_review failed: %s", e)
        return f"AI review unavailable: {e}"


def _journal_path(date_str: str) -> str:
    return os.path.join(config.JOURNALS_DIR, f"{date_str}.json")


def _collect_vix_context() -> dict:
    """Collect today's VIX level and per-strategy override decisions."""
    try:
        from data.angel_fetcher import AngelFetcher
        vix = AngelFetcher.get().fetch_vix()
    except Exception:
        vix = None

    vix_override_global = ipc.flag_exists(ipc.FLAG_VIX_OVERRIDE)
    vix_override_atr    = ipc.flag_exists(ipc.FLAG_VIX_OVERRIDE_ATR)
    vix_override_ict    = ipc.flag_exists(ipc.FLAG_VIX_OVERRIDE_ICT)
    threshold           = config.VIX_THRESHOLD

    blocked = (vix is not None) and (vix > threshold) and not vix_override_global

    return {
        "india_vix":         vix,
        "threshold":         threshold,
        "blocked_by_vix":    blocked,
        "override_global":   vix_override_global,
        "override_atr":      vix_override_atr,
        "override_ict":      vix_override_ict,
        "learning": _analyse_vix_decision(vix, threshold, vix_override_global, vix_override_atr, vix_override_ict),
    }


def _analyse_vix_decision(vix, threshold, override_global, override_atr, override_ict) -> str:
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
            lines.append(f"ATR Intraday: {atr_status}. C-ICT: {ict_status}.")
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

    # NIFTY day OHLC for AI context
    nifty_day = _fetch_nifty_day_ohlc()

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
        "nifty_day":    nifty_day,
        "trades":       trades_list,
        "learning_notes": "",
        "ai_review":    None,
    }

    path = _journal_path(date_str)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(journal, f, indent=2, ensure_ascii=False)
    logger.info("Daily journal saved → %s (%d trades, PnL=₹%.2f)", path, len(trades_list), total_pnl)

    # Generate AI review (may take a few seconds — runs after file is already saved)
    try:
        recent = [j for d in list_journals()[1:6] if (j := load_journal(d)) is not None]
        journal["ai_review"] = generate_ai_review(journal, recent)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(journal, f, indent=2, ensure_ascii=False)
        logger.info("AI review added to journal → %s", path)
    except Exception as e:
        logger.error("AI review generation failed: %s", e)

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


def save_weekly_review() -> str:
    """Generate a Claude-powered week-in-review. Run Saturday morning."""
    import anthropic
    from datetime import date as _date, timedelta
    _ensure_dir()

    today  = _date.today()
    monday = today - timedelta(days=today.weekday())

    week_journals = []
    for i in range(5):
        d = monday + timedelta(days=i)
        if d <= today:
            j = load_journal(d.isoformat())
            if j:
                week_journals.append(j)

    if not week_journals:
        logger.info("Weekly review: no journals found for this week")
        return ""

    total_pnl    = sum(j.get("summary", {}).get("total_pnl", 0) for j in week_journals)
    total_trades = sum(j.get("summary", {}).get("completed_trades", 0) for j in week_journals)
    total_wins   = sum(j.get("summary", {}).get("wins", 0) for j in week_journals)
    total_losses = sum(j.get("summary", {}).get("losses", 0) for j in week_journals)
    week_wr      = round(total_wins / total_trades * 100, 1) if total_trades else 0.0

    day_lines = []
    for j in week_journals:
        s     = j.get("summary", {})
        nifty = j.get("nifty_day", {})
        nifty_move = ""
        if nifty and nifty.get("open"):
            pct = (nifty["close"] - nifty["open"]) / nifty["open"] * 100
            nifty_move = f" | NIFTY {pct:+.1f}%"
        day_lines.append(
            f"  {j.get('date','?')}: ₹{s.get('total_pnl',0):+.0f} "
            f"({s.get('wins',0)}W/{s.get('losses',0)}L){nifty_move}"
        )

    strat_totals: dict = {}
    for j in week_journals:
        for strat, d in j.get("strategy_breakdown", {}).items():
            t = strat_totals.setdefault(strat, {"pnl": 0, "wins": 0, "losses": 0, "trades": 0})
            t["pnl"]    += d.get("pnl", 0)
            t["wins"]   += d.get("wins", 0)
            t["losses"] += d.get("losses", 0)
            t["trades"] += d.get("trades", 0)

    strat_lines = []
    for strat, d in strat_totals.items():
        if d["trades"] > 0:
            wr = round(d["wins"] / d["trades"] * 100)
            strat_lines.append(f"  {strat}: ₹{d['pnl']:+.0f} ({d['wins']}W/{d['losses']}L, {wr}% WR)")

    week_label = monday.strftime("W%V %Y")
    friday     = monday + timedelta(days=4)

    prompt = f"""You are a professional NIFTY options algorithmic trading coach reviewing a full week of bot trading.

WEEK: {week_label} ({monday.strftime('%d %b')} – {friday.strftime('%d %b %Y')})
TOTALS: ₹{total_pnl:+.0f} | {total_trades} trades | {total_wins}W/{total_losses}L | {week_wr}% WR

DAILY BREAKDOWN:
{chr(10).join(day_lines)}

STRATEGY PERFORMANCE (WEEK):
{chr(10).join(strat_lines) if strat_lines else "  No activity."}

---
Write a 450-500 word weekly review:

**Best Day**
[Which day, why it worked — market + signal combination that aligned]

**Worst Day**
[Which day, root cause — what pattern led to losses]

**Strategy Rankings**
[Rank strategies by this week's PnL. One line explaining each ranking]

**Repeating Pattern**
[One pattern that appeared across multiple days — the single most actionable insight of the week]

**One Rule for Next Week**
[Based only on this week's data: one specific rule to add, tighten, or remove. Not generic. Must be directly supported by the numbers]

**Week Verdict**
[One sentence: was this a good week for the bot, and what was the deciding factor]

No filler. No "it's important to note that...". Direct, data-driven, serious."""

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    review_text = msg.content[0].text.strip()

    week_num = monday.strftime("W%V")
    year     = monday.strftime("%Y")
    path     = os.path.join(config.JOURNALS_DIR, f"week-{year}-{week_num}.json")

    data = {
        "type":    "weekly_review",
        "week":    week_label,
        "monday":  monday.isoformat(),
        "friday":  friday.isoformat(),
        "saved_at": now_ist().isoformat(),
        "summary": {
            "total_pnl":   round(total_pnl, 2),
            "total_trades": total_trades,
            "wins":         total_wins,
            "losses":       total_losses,
            "win_rate":     week_wr,
        },
        "strategy_totals": strat_totals,
        "ai_review": review_text,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Weekly review saved → %s", path)
    return path


def list_weekly_reviews() -> list:
    """Return weekly review file names sorted newest first."""
    _ensure_dir()
    files = [
        f[:-5] for f in os.listdir(config.JOURNALS_DIR)
        if f.startswith("week-") and f.endswith(".json")
    ]
    return sorted(files, reverse=True)


def load_weekly_review(key: str) -> Optional[dict]:
    path = os.path.join(config.JOURNALS_DIR, f"{key}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
