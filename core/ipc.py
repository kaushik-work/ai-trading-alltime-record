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
FLAG_PAUSE = "pause"
FLAG_RESUME = "resume"


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
    """Clear all flags — call on bot startup to avoid stale signals."""
    for f in FLAGS_DIR.iterdir():
        f.unlink(missing_ok=True)


# ── Force trade IPC ───────────────────────────────────────────────────────────

FLAG_FORCE_TRADE = "force_trade.json"


def write_force_trade(symbol: str, side: str, quantity: int, reason: str = "Manual override") -> None:
    """Dashboard writes this to queue a manual trade for the bot to execute."""
    import json
    (FLAGS_DIR / FLAG_FORCE_TRADE).write_text(
        json.dumps({"symbol": symbol, "side": side, "quantity": quantity, "reason": reason})
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
