"""Smoke test: print BTC option chain metadata and a BTC short straddle decision."""

from __future__ import annotations

import os
import sys
import logging

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from core.brokers.delta_crypto import DeltaCryptoBroker
from strategies.eth_short_straddle import BTCShortStraddleSignal
from core.risk_management import OPTIONS_TRADING_MODE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    broker = DeltaCryptoBroker(mode=OPTIONS_TRADING_MODE)
    chain = broker.get_option_chain("BTC")
    logger.info("BTC option chain size: %d", len(chain))

    perps = broker.get_perp_mark("BTCUSD")
    logger.info("BTCUSD perp mark: %s", perps)

    # Print first few call/put sizes
    calls = [c for c in chain if c.get("contract_type") == "call_options"][:3]
    puts = [c for c in chain if c.get("contract_type") == "put_options"][:3]
    for c in calls:
        logger.info("CALL sample: symbol=%s strike=%s size=%s expiry=%s mark=%s",
                    c["symbol"], c["strike_price"], c["contract_size"], c["expiry"], c["mark"])
    for p in puts:
        logger.info("PUT sample: symbol=%s strike=%s size=%s expiry=%s mark=%s",
                    p["symbol"], p["strike_price"], p["contract_size"], p["expiry"], p["mark"])

    signal = BTCShortStraddleSignal(broker=broker)
    dec = signal.on_tick()
    if dec is None:
        logger.warning("No BTC short straddle decision returned")
        return

    logger.info(
        "BTC decision: expiry=%s K=%.2f qty=%d credit=%.4f margin=%.2f "
        "call=%s put=%s contract_size=%s",
        dec.expiry, dec.call_strike, dec.qty, dec.qty * dec.contract_size * (dec.call_mark + dec.put_mark),
        dec.total_margin, dec.call_symbol, dec.put_symbol, dec.contract_size,
    )


if __name__ == "__main__":
    main()
