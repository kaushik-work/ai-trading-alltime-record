"""
Trade Guard Pipeline — adapted from TraderAlice/OpenAlice
==========================================================
Composable pre-trade checks. Each guard is a callable that takes a
TradeIntent and returns either None (pass) or a string (reject reason).

The pipeline returns the first rejection reason, or None if all guards
pass. This keeps the failure mode obvious and debuggable.

Usage:
  intent = TradeIntent(structure="iron_condor", risk_usd=2000, ...)
  reason = pipeline(intent, state)
  if reason is None: execute(intent)
  else: log_skip(reason)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set
import pandas as pd


@dataclass
class TradeIntent:
    """One proposed position to open."""
    timestamp: pd.Timestamp
    structure: str               # "long_straddle", "iron_condor", "long_move", "short_move"
    underlying: str              # "BTC", "ETH"
    risk_usd: float              # max loss if everything goes wrong
    notional_usd: float          # gross notional involved
    iv_rv_gap_pp: float          # signal strength
    expiry: Optional[pd.Timestamp] = None


@dataclass
class PortfolioState:
    """What the strategy is currently holding / recent history."""
    equity_usd: float
    open_positions: int = 0
    last_n_pnls: List[float] = field(default_factory=list)     # most-recent-last
    cooldown_until: Optional[pd.Timestamp] = None
    instruments_traded_today: Set[str] = field(default_factory=set)


Guard = Callable[[TradeIntent, PortfolioState], Optional[str]]


# ── concrete guards ──────────────────────────────────────────────────────────
def max_risk_pct(pct: float) -> Guard:
    """Reject if the trade's worst-case loss exceeds `pct` of current equity."""
    def g(intent: TradeIntent, state: PortfolioState) -> Optional[str]:
        cap = state.equity_usd * pct
        if intent.risk_usd > cap:
            return f"max_risk_pct: ${intent.risk_usd:,.0f} > {pct:.1%} × equity (${cap:,.0f})"
        return None
    return g


def max_concurrent_positions(n: int) -> Guard:
    def g(intent: TradeIntent, state: PortfolioState) -> Optional[str]:
        if state.open_positions >= n:
            return f"max_concurrent_positions: already {state.open_positions} open"
        return None
    return g


def cooldown_after_consecutive_losses(n_losses: int, cooldown_hours: float) -> Guard:
    """If last `n_losses` trades all lost, refuse to enter for `cooldown_hours`.
    Clears loss history when cooldown expires so the trigger doesn't infinite-loop."""
    def g(intent: TradeIntent, state: PortfolioState) -> Optional[str]:
        # in cooldown — still blocked
        if state.cooldown_until and intent.timestamp < state.cooldown_until:
            return f"cooldown: until {state.cooldown_until} ({n_losses} consec losses)"
        # cooldown JUST expired — wipe slate so we don't re-trigger immediately
        if state.cooldown_until and intent.timestamp >= state.cooldown_until:
            state.cooldown_until = None
            state.last_n_pnls = []
        # do we need to enter cooldown now?
        recent = state.last_n_pnls[-n_losses:]
        if len(recent) == n_losses and all(p < 0 for p in recent):
            until = intent.timestamp + pd.Timedelta(hours=cooldown_hours)
            state.cooldown_until = until
            return f"cooldown: {n_losses} losses in a row → pause until {until}"
        return None
    return g


def min_signal_strength(min_gap_pp: float) -> Guard:
    """Refuse trades with weaker than `min_gap_pp` of IV-RV gap."""
    def g(intent: TradeIntent, state: PortfolioState) -> Optional[str]:
        if abs(intent.iv_rv_gap_pp) < min_gap_pp:
            return f"min_signal_strength: gap {intent.iv_rv_gap_pp:+.2f}pp < {min_gap_pp}pp"
        return None
    return g


def underlying_whitelist(allowed: Set[str]) -> Guard:
    def g(intent: TradeIntent, state: PortfolioState) -> Optional[str]:
        if intent.underlying not in allowed:
            return f"underlying_whitelist: {intent.underlying} not in {allowed}"
        return None
    return g


def max_trades_per_day(n: int) -> Guard:
    """Resets `state.instruments_traded_today` at each new UTC calendar day.
    NOTE: callers must `state.instruments_traded_today.add(symbol)` on entry
    for this guard to count anything."""
    last_date = {"date": None}
    def g(intent: TradeIntent, state: PortfolioState) -> Optional[str]:
        today = intent.timestamp.date()
        if last_date["date"] != today:
            state.instruments_traded_today.clear()
            last_date["date"] = today
        if len(state.instruments_traded_today) >= n:
            return f"max_trades_per_day: {len(state.instruments_traded_today)} ≥ {n}"
        return None
    return g


# ── pipeline ─────────────────────────────────────────────────────────────────
def pipeline(intent: TradeIntent, state: PortfolioState,
             guards: List[Guard]) -> Optional[str]:
    """Run guards in order. Return first failure reason, or None if all pass."""
    for g in guards:
        reason = g(intent, state)
        if reason is not None:
            return reason
    return None


# ── default sane pipeline ────────────────────────────────────────────────────
def default_pipeline(equity_usd: float) -> List[Guard]:
    """A reasonable starting safety net for a single-user retail crypto book."""
    return [
        underlying_whitelist({"BTC", "ETH"}),
        max_risk_pct(0.03),                                  # 3% per trade
        max_concurrent_positions(5),
        max_trades_per_day(10),
        cooldown_after_consecutive_losses(3, cooldown_hours=24),
        min_signal_strength(min_gap_pp=4.0),
    ]
