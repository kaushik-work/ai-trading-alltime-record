"""
BotRunner — scheduler host

The legacy NSE Q5 scheduler stack has been retired. BotRunner now only
exists to host the APScheduler that the crypto runner uses for its 2s
tick + 5min wallet heartbeat. The class shape is preserved so the
FastAPI lifespan in api/server.py keeps working without churn.
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)


def _is_cloud_host() -> bool:
    """True on a Linux server (where APScheduler should actually run).
    Returns False on dev laptops so we don't accidentally fire timers
    against production endpoints while debugging locally."""
    return sys.platform.startswith("linux")


class BotRunner:
    """Thin wrapper that owns the APScheduler instance."""

    def __init__(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        self.scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
        self.last_heartbeat = None
        self.last_option_chain = None

    def start(self) -> None:
        if not _is_cloud_host() and os.environ.get("ALLOW_LOCAL_SCHEDULER") != "1":
            logger.warning("BotRunner.start: not a cloud host — scheduler OFF "
                           "(set ALLOW_LOCAL_SCHEDULER=1 to override)")
            return
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("BotRunner: scheduler started (crypto-only mode)")

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("BotRunner: scheduler stopped")


_RUNNER_INSTANCE = None


def get_runner() -> BotRunner:
    global _RUNNER_INSTANCE
    if _RUNNER_INSTANCE is None:
        _RUNNER_INSTANCE = BotRunner()
    return _RUNNER_INSTANCE
