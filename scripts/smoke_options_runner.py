"""Smoke test: initialize the options runner and print enabled strategies."""

from __future__ import annotations

import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from core.execution import options_runner
from core.risk_management import ENABLE_BTC_SHORT_STRADDLE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    strategies = options_runner._get_strategies()
    logger.info("Enabled strategies: %s", list(strategies.keys()))
    logger.info("ENABLE_BTC_SHORT_STRADDLE=%s", ENABLE_BTC_SHORT_STRADDLE)
    for name, strat in strategies.items():
        logger.info("  %s -> underlying=%s symbol=%s", name, strat.underlying, strat.symbol)


if __name__ == "__main__":
    main()
