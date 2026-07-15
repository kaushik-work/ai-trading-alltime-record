"""NSE-specific risk management.

All dials are hardcoded. No .env overrides. The NSE runner is always enabled
when the API starts; there is no paper mode.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from nse.config import TOTAL_CAPITAL_INR

logger = logging.getLogger(__name__)

# Activation — always enabled.
ENABLE_NSE_RUNNER: bool = True

# Capital
NSE_BASE_CAPITAL_INR: float = TOTAL_CAPITAL_INR
NSE_FIXED_CAPITAL_INR: float = TOTAL_CAPITAL_INR

# Limits
NSE_DAILY_LOSS_KILL_PCT: float = 0.03
NSE_MAX_OPEN_POSITIONS: int = 2
NSE_MAX_POSITIONS_PER_SYMBOL: int = 1

# Runtime state
_DAY_PNL_INR: float = 0.0
_DAY_PNL_RESET_DATE: Optional[str] = None
_KILLED: bool = False


def _reset_day_pnl_if_needed() -> None:
    global _DAY_PNL_INR, _DAY_PNL_RESET_DATE
    today = datetime.now(timezone.utc).date().isoformat()
    if _DAY_PNL_RESET_DATE != today:
        _DAY_PNL_INR = 0.0
        _DAY_PNL_RESET_DATE = today


def add_day_pnl(pnl: float) -> None:
    global _DAY_PNL_INR
    _reset_day_pnl_if_needed()
    _DAY_PNL_INR += pnl


def get_day_pnl() -> float:
    _reset_day_pnl_if_needed()
    return _DAY_PNL_INR


def check_kill_switch() -> bool:
    """Return True if new entries should be halted."""
    global _KILLED
    _reset_day_pnl_if_needed()
    if _KILLED:
        return True
    if _DAY_PNL_INR < -NSE_BASE_CAPITAL_INR * NSE_DAILY_LOSS_KILL_PCT:
        _KILLED = True
        logger.error(
            "NSE KILL SWITCH: day PnL %.0f < -%.1f%% of base — halting entries",
            _DAY_PNL_INR,
            NSE_DAILY_LOSS_KILL_PCT * 100,
        )
        return True
    return False


def set_killed(killed: bool) -> None:
    global _KILLED
    _KILLED = killed


def is_killed() -> bool:
    return _KILLED
