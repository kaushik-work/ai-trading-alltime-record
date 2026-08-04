"""The contract between a signal source and the execution layer.

All strategies were retired on 2026-08-04 (see core/risk_management.py and
docs/RESEARCH_LEARNINGS.md). The execution layer was kept: order placement,
bracket orders, reconciliation, the kill switch and the dashboard all still
work and were expensive to get right.

This module is what remains of the boundary between the two. A future strategy
only has to emit `CryptoSignalDecision` and register itself in
`crypto_runner._get_strategies()`; nothing else in the execution path needs to
change.

The exit dials below stay here rather than in the deleted strategy file because
they are consumed by SIZING and EXIT logic, which is execution, not signal
generation. They are the values the retired price-action strategy ran with,
preserved so live positions and the dashboard keep behaving identically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Exit geometry. NOTE these describe the RETIRED strategy and are kept only so
# the execution path has defaults. The 1:7 target was reached once in 515
# trades (RESEARCH_LEARNINGS 1.4) — do not treat these as validated.
SL_PCT = 0.005                    # 0.5% base stop
RR_RATIO = 7.0                    # 1:7 target
ASSET_DIALS = {                   # per-asset overrides
    "BTC": {"sl_pct": 0.006, "rr_ratio": 7.0},
    "ETH": {"sl_pct": 0.007, "rr_ratio": 7.0},
}
MAX_HOLD_MINUTES = 240            # 4h max hold


@dataclass
class CryptoSignalDecision:
    """What a crypto strategy emits when it wants to trade."""

    name: str                       # strategy id
    symbol: str                     # perp symbol (e.g. BTCUSD)
    side: str                       # "buy" | "sell"
    pred_pct: float                 # signal strength in percent
    n_strikes: int                  # corroborating strikes
    expiry: Optional[str] = None    # associated option expiry (for context)
    size_mult: float = 1.0          # 0.5x-3.0x equity multiplier
    stop_loss_pct: float = 0.015
    partial_tp_pct: float = 0.010
    trail_peak_pct: float = 0.005
    trail_giveback: float = 0.0025
    metadata: dict = field(default_factory=dict)
