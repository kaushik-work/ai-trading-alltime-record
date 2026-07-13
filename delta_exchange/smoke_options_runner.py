"""Read-only smoke test for the options runner — does NOT place orders."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from core.execution.options_runner import (
    _get_broker, _get_strategies, get_options_state,
)


def main():
    print("Smoke-testing ETH short straddle options runner (read-only)...")
    broker = _get_broker()
    spot = broker.get_perp_mark("ETHUSD")
    print(f"  Trading mode: {broker.mode}")
    print(f"  ETH spot: {spot}")

    chain = broker.get_option_chain("ETH")
    print(f"  Option chain size: {len(chain)}")
    if chain:
        o = chain[0]
        print(f"    sample: {o.get('symbol')} mark={o.get('mark')} "
              f"strike={o.get('strike_price')} size={o.get('contract_size')} "
              f"expiry={o.get('expiry')}")

    strat = _get_strategies()["eth_short_straddle"]
    dec = strat.on_tick()
    if dec is None:
        print("  No signal produced (normal outside entry window).")
    else:
        print(f"  Decision: qty={dec.qty} credit=${dec.call_mark+dec.put_mark:.4f} "
              f"margin=${dec.total_margin:.2f} call={dec.call_symbol} put={dec.put_symbol}")

    print(f"  State: {get_options_state()}")


if __name__ == "__main__":
    main()
