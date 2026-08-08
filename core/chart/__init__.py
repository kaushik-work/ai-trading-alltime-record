"""Venue-neutral chart reading: levels, liquidity sweeps, setups, sizing.

Everything in here takes an OHLC DataFrame and returns structure. It knows
nothing about NIFTY, Delta, options or perpetuals — which is the point. The same
setup logic runs on a NIFTY 5-minute chart and a BTCUSD 5-minute chart, and
agreement between two unrelated markets is far stronger evidence that a pattern
is real than a good result on either one alone.

That portability has one hard requirement: NO ABSOLUTE PRICE CONSTANTS. A
20-point tolerance is 0.08% of NIFTY, 0.02% of BTC and 1.07% of ETH — the same
number meaning three different things. Every threshold here is expressed in ATR
or in percent, never in points. See RESEARCH_LEARNINGS open item 3.
"""

from core.chart.structure import ChartStructure, Level, read_structure
from core.chart.sweep import Sweep, find_sweeps
from core.chart.setup import Setup, find_setup
from core.chart.sizing import Position, size_position

__all__ = [
    "ChartStructure", "Level", "read_structure",
    "Sweep", "find_sweeps",
    "Setup", "find_setup",
    "Position", "size_position",
]
