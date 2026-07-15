"""Dataclasses for NSE synthetic-forward strategy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class OptionQuote:
    symbol: str                # NIFTY / BANKNIFTY / FINNIFTY / SENSEX
    expiry: datetime           # expiry datetime (UTC)
    strike: int
    option_type: str           # "CE" or "PE"
    ltp: float                 # last traded price
    bid: float = 0.0
    ask: float = 0.0
    volume: int = 0
    oi: int = 0
    spot: float = 0.0
    timestamp: Optional[datetime] = None


@dataclass
class SyntheticForwardSignal:
    symbol: str
    expiry: datetime
    pred: float                # median deviation (synthetic forward vs spot)
    n_strikes: int
    spot: float
    synth_forward: float       # absolute implied forward price
    side: str                  # "long" or "short"
    timestamp: datetime
    strikes_used: list[int] = field(default_factory=list)


@dataclass
class ComboLeg:
    side: str                  # "BUY" or "SELL"
    option_type: str           # "CE" or "PE"
    strike: int
    expiry: datetime
    tradingsymbol: str
    token: str
    lots: int
    entry_px: float = 0.0
    filled_px: Optional[float] = None
    order_id: Optional[str] = None


@dataclass
class Position:
    position_id: str
    symbol: str
    signal_side: str           # "long" or "short" synthetic forward
    entry_time: datetime
    legs: list[ComboLeg]
    spot_at_entry: float
    pred_pct: float
    stop_loss_pct: float
    target_pct: float
    max_hold_until: datetime
    status: str = "OPEN"       # OPEN, CLOSED
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    pnl: float = 0.0
    peak_pnl_pct: float = 0.0
    tp_taken: bool = False
    margin_used: float = 0.0


@dataclass
class TradeEvent:
    event_id: str
    position_id: str
    symbol: str
    event_type: str            # ENTRY, PARTIAL_TP, STOP, TARGET, TRAIL, MAX_HOLD, EXPIRY
    timestamp: datetime
    spot: float
    pnl: float = 0.0
    pnl_pct: float = 0.0
    equity_after: float = 0.0
    metadata: dict = field(default_factory=dict)
