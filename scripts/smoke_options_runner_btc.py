"""Smoke test: initialize the options runner with BTC enabled (no orders placed)."""

from __future__ import annotations

import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

# Temporarily enable BTC for this smoke test
import core.risk_management as risk
risk.ENABLE_BTC_SHORT_STRADDLE = True
risk.OPTIONS_FIXED_CAPITAL_INR_BY_ASSET["BTC"] = 25000.0

from core.execution import options_runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    # Reset cached strategy instances so the new flag is respected
    options_runner._STRATEGY_INSTANCES.clear()
    strategies = options_runner._get_strategies()
    logger.info("Enabled strategies: %s", list(strategies.keys()))
    for name, strat in strategies.items():
        logger.info("  %s -> underlying=%s symbol=%s", name, strat.underlying, strat.symbol)


if __name__ == "__main__":
    main()
