"""
Zerodha error logger — appends failures to a JSON file so they can be
reviewed on the Errors page and fixed one by one.
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_IST = ZoneInfo("Asia/Kolkata")

ERROR_LOG_PATH = Path(config.LOGS_DIR) / "zerodha_errors.json"
MAX_ERRORS = 500


def log_error(source: str, error: str, symbol: str = "", detail: str = ""):
    """Append one failure entry to the error log JSON."""
    entry = {
        "timestamp": datetime.now(_IST).isoformat(),
        "source":    source,
        "symbol":    symbol,
        "detail":    detail,
        "error":     str(error),
    }
    with _lock:
        try:
            errors = _read()
            errors.append(entry)
            if len(errors) > MAX_ERRORS:
                errors = errors[-MAX_ERRORS:]
            ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            ERROR_LOG_PATH.write_text(json.dumps(errors, indent=2))
        except Exception as e:
            logger.warning("zerodha_error_log: could not write: %s", e)


def get_all() -> list:
    """Return all logged errors, newest first."""
    return list(reversed(_read()))


def clear():
    """Delete all logged errors."""
    with _lock:
        try:
            ERROR_LOG_PATH.write_text("[]")
        except Exception:
            pass


def _read() -> list:
    try:
        if ERROR_LOG_PATH.exists():
            return json.loads(ERROR_LOG_PATH.read_text())
    except Exception:
        pass
    return []
