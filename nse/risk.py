"""NSE-specific risk management.

All dials are hardcoded. No .env overrides.

RETIRED 2026-08-04. The NSE runner is OFF and no NSE strategy is cleared to
trade. Every candidate was measured and none survived:

    synthetic forward     gate 0.60% vs a maximum observed deviation of
                          0.404%; fired 0 times in 1,869 observations
    breakout-retest       VALID negative at every days-to-expiry bucket
    variance premium      real (Sharpe 1.19) but the edge exists only in a
                          naked short straddle we cannot margin; every
                          defined-risk version is negative before costs

Findings are preserved in docs/RESEARCH_LEARNINGS.md and
docs/OPTIONS_GREEKS_LEARNINGS.md. Do not re-enable without a strategy that
clears TRAIN, VALID and TEST after costs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from nse.config import TOTAL_CAPITAL_INR

logger = logging.getLogger(__name__)

# Activation — RETIRED. See the module docstring for what was measured.
ENABLE_NSE_RUNNER: bool = False

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
