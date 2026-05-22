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
FLAG_PAUSE  = "pause"
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


# Flag files that MUST survive bot restart (persistent state).
# Anything not in this set is treated as a transient signal and wiped on startup.
_PERSISTENT_FLAGS = {
    "market_holidays.json",   # frontend-added NSE holidays
    "event_blocks.json",      # custom Budget / RBI MPC dates
    "event_unblocks.json",    # force-allow overrides
    "settings.json",          # min_lots and any future runtime settings
}


def clear_all_flags() -> None:
    """Clear ONLY transient flags (pause / resume / force_trade) on bot startup.

    Persistent state (holidays, settings, SL/TP order tracking, day bias, etc.)
    survives container restarts. Wiping live SL/TP order IDs would orphan
    exchange orders during a mid-position restart — never do that.
    """
    for f in FLAGS_DIR.iterdir():
        if f.name in _PERSISTENT_FLAGS:
            continue
        f.unlink(missing_ok=True)


# Force-trade IPC and day_bias removed alongside ATR retirement — both
# only existed to nudge the ATR scorer's decisions.

# ── Event block overrides ─────────────────────────────────────────────────────

EVENT_BLOCKS_FILE   = FLAGS_DIR / "event_blocks.json"
EVENT_UNBLOCKS_FILE = FLAGS_DIR / "event_unblocks.json"


def read_event_blocks() -> dict:
    """Return runtime event block overrides {date: label}."""
    import json
    if not EVENT_BLOCKS_FILE.exists():
        return {}
    try:
        return json.loads(EVENT_BLOCKS_FILE.read_text())
    except Exception:
        return {}


def write_event_blocks(blocks: dict) -> None:
    """Persist runtime event block overrides."""
    import json
    EVENT_BLOCKS_FILE.write_text(json.dumps(blocks, indent=2))


def read_event_unblocks() -> set:
    """Return set of dates that are explicitly unblocked (override config.py blocks)."""
    import json
    if not EVENT_UNBLOCKS_FILE.exists():
        return set()
    try:
        return set(json.loads(EVENT_UNBLOCKS_FILE.read_text()))
    except Exception:
        return set()


def add_event_unblock(date: str) -> set:
    unblocks = read_event_unblocks()
    unblocks.add(date)
    import json
    EVENT_UNBLOCKS_FILE.write_text(json.dumps(sorted(unblocks)))
    return unblocks


def remove_event_unblock(date: str) -> set:
    unblocks = read_event_unblocks()
    unblocks.discard(date)
    import json
    EVENT_UNBLOCKS_FILE.write_text(json.dumps(sorted(unblocks)))
    return unblocks


def add_event_block(date: str, label: str) -> dict:
    blocks = read_event_blocks()
    blocks[date] = label
    write_event_blocks(blocks)
    return blocks


def remove_event_block(date: str) -> dict:
    blocks = read_event_blocks()
    blocks.pop(date, None)
    write_event_blocks(blocks)
    return blocks


# ── Market holidays (runtime additions) ──────────────────────────────────────

HOLIDAYS_FILE = FLAGS_DIR / "market_holidays.json"


def read_runtime_holidays() -> dict:
    """Return runtime-added holidays {date: label}."""
    import json
    if not HOLIDAYS_FILE.exists():
        return {}
    try:
        return json.loads(HOLIDAYS_FILE.read_text())
    except Exception:
        return {}


def add_market_holiday(date: str, label: str) -> dict:
    holidays = read_runtime_holidays()
    holidays[date] = label
    import json
    HOLIDAYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOLIDAYS_FILE.write_text(json.dumps(holidays, indent=2))
    return holidays


def remove_market_holiday(date: str) -> dict:
    holidays = read_runtime_holidays()
    holidays.pop(date, None)
    import json
    HOLIDAYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOLIDAYS_FILE.write_text(json.dumps(holidays, indent=2))
    return holidays


def is_market_holiday(date_str: str) -> tuple[bool, str]:
    """Return (is_holiday, reason). Checks config + runtime."""
    import config
    if date_str in config.NSE_MARKET_HOLIDAYS:
        return True, config.NSE_MARKET_HOLIDAYS[date_str]
    runtime = read_runtime_holidays()
    if date_str in runtime:
        return True, runtime[date_str]
    return False, ""


# ── Runtime settings (lots, etc.) ────────────────────────────────────────────

SETTINGS_FILE = FLAGS_DIR / "settings.json"

_SETTINGS_DEFAULTS = {
    "min_lots": 1,
}


def read_settings() -> dict:
    """Return runtime settings. Falls back to defaults if file missing."""
    import json
    if not SETTINGS_FILE.exists():
        return dict(_SETTINGS_DEFAULTS)
    try:
        stored = json.loads(SETTINGS_FILE.read_text())
        return {**_SETTINGS_DEFAULTS, **stored}
    except Exception:
        return dict(_SETTINGS_DEFAULTS)


def write_settings(settings: dict) -> dict:
    """Persist runtime settings and return the merged result."""
    import json
    merged = {**read_settings(), **settings}
    SETTINGS_FILE.write_text(json.dumps(merged, indent=2))
    return merged


# SL/TP order tracking, pre-market watch zones, and force_trade IPC all
# removed alongside ATR retirement — these only mattered for live ATR trades.
# The shadow executor stores its open positions in Mongo (`shadow_trades`) and
# requires no on-disk state.


