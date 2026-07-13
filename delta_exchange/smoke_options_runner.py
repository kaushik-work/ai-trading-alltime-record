"""Smoke test for the options runner — paper mode only."""
import os
import sys

# Force paper mode and options enabled BEFORE importing modules that read env.
os.environ["OPTIONS_TRADING_MODE"] = "paper"
os.environ["ENABLE_OPTIONS_RUNNER"] = "1"
os.environ["OPTIONS_FIXED_CAPITAL_INR"] = "50000"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.execution import options_runner
from core.execution.options_runner import (
    _get_broker, _get_strategies, _open_position, _manage_positions, get_options_state,
)


def main():
    print("Smoke-testing ETH short straddle options runner (paper mode)...")
    broker = _get_broker()
    spot = broker.get_perp_mark("ETHUSD")
    print(f"  ETH spot: {spot}")

    strat = _get_strategies()["eth_short_straddle"]
    dec = strat.on_tick()
    if dec is None:
        print("  No signal — this is normal outside the configured entry window.")
        print(f"  State: {get_options_state()}")
        return

    print(f"  Decision: qty={dec.qty} credit=${dec.call_mark+dec.put_mark:.4f} "
          f"margin=${dec.total_margin:.2f}")
    pos = _open_position(dec)
    if pos is None:
        print("  Failed to open position.")
        return

    options_runner._OPEN_POSITIONS.append(pos)
    print(f"  Opened: {pos['qty']} straddles credit=${pos['entry_credit']:.4f}")
    _manage_positions()
    state = get_options_state()
    print(f"  State: open={len(state['open_positions'])} closed={len(state['closed_trades'])}")


if __name__ == "__main__":
    main()
