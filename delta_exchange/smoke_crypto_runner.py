"""
Smoke test for the crypto runner live code path.
Runs in paper mode without API keys and exercises:
  - broker initialization
  - strategy instantiation + history backfill (from Delta public API)
  - position-management tick
  - entry-decision tick
  - state snapshot
"""
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["CRYPTO_TRADING_MODE"] = "paper"
os.environ["ENABLE_CRYPTO_RUNNER"] = "1"

from core.execution.crypto_runner import (
    tick_position_management,
    tick_entry_decisions,
    get_state,
    _get_strategies,
)


def main():
    print("Smoke test: crypto runner paper mode")

    # 1. Ensure strategy instantiates and backfills history.
    strategies = _get_strategies()
    assert "eth_price_action_sr" in strategies, "ETH strategy missing"
    strat = strategies["eth_price_action_sr"]
    n = strat.backfill_history(lookback_hours=24)
    print(f"  Backfilled candles: {n}")
    assert n >= 0, "backfill returned negative count"

    # 2. Run a few position-management ticks.
    for i in range(3):
        tick_position_management()
    print("  Position management ticks: OK")

    # 3. Run an entry-decision tick.
    tick_entry_decisions()
    print("  Entry decision tick: OK")

    # 4. Check state snapshot.
    state = get_state()
    assert "open_positions" in state
    assert "shadow_trades" in state
    assert "missed_signals" in state
    print(f"  State keys: {list(state.keys())}")
    print("\n✅ Smoke test passed")


if __name__ == "__main__":
    main()
