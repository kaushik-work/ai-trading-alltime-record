"""Smoke test: print ETH option chain metadata and a ETH short straddle decision."""

from __future__ import annotations

import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from core.brokers.delta_crypto import DeltaCryptoBroker
from strategies.eth_short_straddle import ETHShortStraddleSignal
from core.risk_management import OPTIONS_TRADING_MODE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    broker = DeltaCryptoBroker(mode=OPTIONS_TRADING_MODE)
    chain = broker.get_option_chain("ETH")
    logger.info("ETH option chain size: %d", len(chain))

    perp = broker.get_perp_mark("ETHUSD")
    logger.info("ETHUSD perp mark: %s", perp)

    calls = [c for c in chain if c.get("contract_type") == "call_options"][:3]
    puts = [c for c in chain if c.get("contract_type") == "put_options"][:3]
    for c in calls:
        logger.info("CALL sample: symbol=%s strike=%s size=%s expiry=%s mark=%s",
                    c["symbol"], c["strike_price"], c["contract_size"], c["expiry"], c["mark"])
    for p in puts:
        logger.info("PUT sample: symbol=%s strike=%s size=%s expiry=%s mark=%s",
                    p["symbol"], p["strike_price"], p["contract_size"], p["expiry"], p["mark"])

    signal = ETHShortStraddleSignal(broker=broker)
    dec = signal.on_tick()
    if dec is None:
        logger.warning("No ETH short straddle decision returned")
        return

    logger.info(
        "ETH decision: expiry=%s K=%.2f qty=%d credit=%.4f margin=%.2f "
        "call=%s put=%s contract_size=%s",
        dec.expiry, dec.call_strike, dec.qty, dec.qty * dec.contract_size * (dec.call_mark + dec.put_mark),
        dec.total_margin, dec.call_symbol, dec.put_symbol, dec.contract_size,
    )


if __name__ == "__main__":
    main()
