"""
Inter-process communication between main.py (bot) and the Streamlit dashboard.
Uses flag files in db/flags/ — presence/absence of a file is the signal.
No sockets, no queues, no shared memory needed.
"""
from pathlib import Path

_BASE_DIR = Path(__file__).parent.parent
FLAGS_DIR = _BASE_DIR / "db" / "flags"
FLAGS_DIR.mkdir(parents=True, exist_ok=True)

# Flag name constants
FLAG_PAUSE        = "pause"
FLAG_RESUME       = "resume"
FLAG_VIX_OVERRIDE = "vix_override"  # when set, VIX gate is bypassed for the day


def write_flag(name: str) -> None:
    """Create a flag file (signals the bot)."""
    (FLAGS_DIR / name).touch()


def clear_flag(name: str) -> None:
    """Remove a flag file (consume the signal)."""
    (FLAGS_DIR / name).unlink(missing_ok=True)


def flag_exists(name: str) -> bool:
    """Check whether a flag is currently set."""
    return (FLAGS_DIR / name).exists()


def clear_all_flags() -> None:
    """Clear transient flags on bot startup. Preserves persistent overrides like vix_override."""
    _persist = {FLAG_VIX_OVERRIDE}
    for f in FLAGS_DIR.iterdir():
        if f.name not in _persist:
            f.unlink(missing_ok=True)


# ── Force trade IPC ───────────────────────────────────────────────────────────

FLAG_FORCE_TRADE = "force_trade.json"


def write_force_trade(symbol: str, side: str, quantity: int, reason: str = "Manual override",
                      option_type: str = None, strike: int = None,
                      sl: float = None, tp: float = None) -> None:
    """Dashboard writes this to queue a manual trade for the bot to execute."""
    import json
    payload = {"symbol": symbol, "side": side, "quantity": quantity, "reason": reason}
    if option_type: payload["option_type"] = option_type
    if strike:      payload["strike"]      = strike
    if sl:          payload["sl"]          = sl
    if tp:          payload["tp"]          = tp
    (FLAGS_DIR / FLAG_FORCE_TRADE).write_text(json.dumps(payload))


def read_day_bias() -> dict:
    """Return current day bias. Default NEUTRAL if not set."""
    import json
    f = FLAGS_DIR / "day_bias.json"
    if not f.exists():
        return {"bias": "NEUTRAL", "note": "", "set_at": None}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {"bias": "NEUTRAL", "note": "", "set_at": None}


def write_day_bias(bias: str, note: str = "", parsed: dict = None) -> None:
    """Dashboard writes trader's directional bias for the day."""
    import json
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    (FLAGS_DIR / "day_bias.json").write_text(
        json.dumps({
            "bias": bias.upper(),
            "note": note,
            "parsed": parsed or {},
            "set_at": datetime.now(ist).isoformat(),
        })
    )


def read_and_clear_force_trade() -> dict | None:
    """Bot reads this once and immediately deletes it. Returns None if not set."""
    import json
    f = FLAGS_DIR / FLAG_FORCE_TRADE
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text())
    except Exception:
        data = None
    f.unlink(missing_ok=True)
    return data
