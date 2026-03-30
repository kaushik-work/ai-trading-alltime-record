import logging
import time
import signal
import sys
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
import config
from core.security import setup_secure_logging
from core.memory import init_db
from core.records import init_records_db
from core import ipc
from strategies.trend_strategy import TrendStrategy

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"{config.LOGS_DIR}/bot_{datetime.now().strftime('%Y%m%d')}.log"),
    ],
)
logger = logging.getLogger(__name__)

strategy: TrendStrategy = None
scheduler = BlockingScheduler(timezone="Asia/Kolkata")


def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    open_h, open_m = map(int, config.MARKET_OPEN.split(":"))
    close_h, close_m = map(int, config.MARKET_CLOSE.split(":"))
    market_open = now.replace(hour=open_h, minute=open_m, second=0)
    market_close = now.replace(hour=close_h, minute=close_m, second=0)
    return market_open <= now <= market_close


def trading_cycle():
    """Called every 5 minutes during market hours."""
    # Poll IPC flags from the dashboard
    if ipc.flag_exists(ipc.FLAG_PAUSE):
        if not strategy.paused:
            strategy.pause()
        ipc.clear_flag(ipc.FLAG_PAUSE)
    elif ipc.flag_exists(ipc.FLAG_RESUME):
        if strategy.paused:
            strategy.resume()
        ipc.clear_flag(ipc.FLAG_RESUME)

    if strategy.paused:
        logger.info("Bot is paused. Skipping cycle.")
        return

    # Execute any manual force trade queued from the dashboard
    force = ipc.read_and_clear_force_trade()
    if force:
        logger.info("Force trade received: %s %d %s — %s",
                    force["side"], force["quantity"], force["symbol"], force["reason"])
        try:
            market_data = strategy.market.get_indicators(force["symbol"])
            decision = {
                "action":      force["side"],
                "symbol":      force["symbol"],
                "quantity":    force["quantity"],
                "confidence":  1.0,
                "reasoning":   force["reason"],
                "risk_level":  "MANUAL",
            }
            if force.get("option_type"): decision["option_type"] = force["option_type"]
            if force.get("strike"):      decision["strike"]      = force["strike"]
            if force.get("sl"):          decision["sl"]          = force["sl"]
            if force.get("tp"):          decision["tp"]          = force["tp"]
            result = strategy._execute(decision, market_data)
            logger.info("Force trade result: %s", result)
        except Exception as e:
            logger.error("Force trade failed: %s", e)

    if not is_market_open():
        logger.info("Market closed. Skipping cycle.")
        return
    logger.info("--- Trading cycle started ---")
    results = strategy.run_watchlist()
    logger.info("Cycle complete. Orders placed: %d", len(results))


def square_off_job():
    """Called at INTRADAY_EXIT_BY — close all open intraday positions."""
    logger.warning("--- INTRADAY SQUARE-OFF triggered (%s) ---", config.INTRADAY_EXIT_BY)
    results = strategy.square_off_all()
    logger.info("Square-off done. Positions closed: %d", len(results))


def end_of_day_job():
    """Called at 15:35 IST — after market close."""
    logger.info("Running end-of-day review...")
    summary = strategy.end_of_day()
    logger.info("EOD done. Trades today: %d | Records broken: %s", summary["trades"], summary["broken_records"])


def handle_signal(sig, frame):
    """Handle Ctrl+C — pause trading and shut down gracefully."""
    logger.warning("Shutdown signal received. Pausing trading and exiting...")
    if strategy:
        strategy.pause()
    scheduler.shutdown(wait=False)
    sys.exit(0)


def manual_pause():
    if strategy:
        strategy.pause()
        print("Trading PAUSED.")


def manual_resume():
    if strategy:
        strategy.resume()
        print("Trading RESUMED.")


def main():
    global strategy

    print(f"""
╔══════════════════════════════════════════════════════════╗
║         AI Trading Bot — All-Time Record                 ║
║  Mode   : {'PAPER (Simulation)      ' if config.IS_PAPER else 'LIVE TRADING            '}                ║
║  Phase  : {config.TRADING_PHASE} | Intraday NIFTY & BANKNIFTY           ║
║  Budget : Rs.{config.STARTING_BUDGET:,} | Target A: Rs.1.5L | Target B: Rs.15L ║
║  Window : {config.INTRADAY_START} - {config.INTRADAY_EXIT_BY} IST | SL:{config.STOP_LOSS_PCT}% TP:{config.TAKE_PROFIT_PCT}%              ║
╚══════════════════════════════════════════════════════════╝
""")

    # Activate credential masking in all logs
    setup_secure_logging()

    # Clear any stale IPC flags from a previous run
    ipc.clear_all_flags()

    # Initialize DB
    init_db()
    init_records_db()

    # Init strategy
    strategy = TrendStrategy()

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Schedule trading cycles
    sq_h, sq_m = map(int, config.INTRADAY_EXIT_BY.split(":"))
    scheduler.add_job(trading_cycle,  "interval", minutes=5, id="trading_cycle",
                      next_run_time=datetime.now())
    scheduler.add_job(square_off_job, "cron", hour=sq_h, minute=sq_m, id="square_off")
    scheduler.add_job(end_of_day_job, "cron", hour=15,   minute=35,   id="eod_review")

    logger.info("Bot started. Trading every 5 minutes during market hours (09:15–15:30 IST).")
    logger.info("Press Ctrl+C to stop.")

    try:
        scheduler.start()
    except Exception as e:
        logger.error("Scheduler error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
