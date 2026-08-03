"""
Risk Management — Crypto Strategy Production Dials
====================================================
Single source of truth for live trading risk parameters. These are part of
the strategy contract — changes should go through PR review, not silent
.env edits. Keep .env for SECRETS only (API keys, DB credentials, JWT).

Defaults below ARE production values. Env vars can still override (useful
for paper-mode debugging or emergency tweaks) but you should not need to
set them in normal operation.
"""

from __future__ import annotations

import os


def _env_float(key: str, default: float) -> float:
    try: return float(os.environ.get(key, default))
    except (TypeError, ValueError): return default


def _env_int(key: str, default: int) -> int:
    try: return int(os.environ.get(key, default))
    except (TypeError, ValueError): return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None: return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ── Activation ───────────────────────────────────────────────────────────────
ENABLE_CRYPTO_RUNNER: bool = _env_bool("ENABLE_CRYPTO_RUNNER", True)
# "live" hits Delta with real orders; "paper" journals but doesn't trade
CRYPTO_TRADING_MODE: str = os.environ.get("CRYPTO_TRADING_MODE", "live")


# ── Trading cadence ──────────────────────────────────────────────────────────
# Bot ticks every 2s, fed by the WebSocket stream — fast enough for stop
# losses to fire within milliseconds of breach without burning CPU.
TICK_INTERVAL_SECONDS: int = max(1, _env_int("CRYPTO_TICK_SECONDS", 2))


# ── Capital deployment ───────────────────────────────────────────────────────
# Paper-mode equity floor (live mode uses real Delta wallet balance).
BASE_EQUITY_USD: float = _env_float("CRYPTO_EQUITY_USD", 1000.0)

# Delta India auto-converts INR↔USD at trade time, so INR sitting in the
# wallet IS tradeable. We convert at this rate to value the pool in USD.
USD_INR_RATE: float = _env_float("USD_INR_RATE", 86.0)

# Per-cycle capital deployed as a fraction of the live wallet pool.
# Per-asset overrides let BTC and ETH be sized independently if desired.
CAPITAL_USE_PCT: float = _env_float("CRYPTO_CAPITAL_USE_PCT", 0.50)
BTC_CAPITAL_PCT: float = _env_float("CRYPTO_BTC_CAPITAL_PCT", 0.50)
ETH_CAPITAL_PCT: float = _env_float("CRYPTO_ETH_CAPITAL_PCT", 0.50)

# Fixed-capital mode: ignore the live wallet balance and use a fixed INR
# budget for every trade.  Hardcoded production dial — not in .env.
FIXED_CAPITAL_MODE: bool = True
FIXED_CAPITAL_INR: float = 50000.0


# ── Risk limits ──────────────────────────────────────────────────────────────
# Leverage applied per order. This dial is intentionally hardcoded (not in
# .env) because we expect to change it often and want every change tracked in
# git. Current live config: ETH-only, fixed Rs 50k notional per trade, 15×
# leverage (margin = Rs 50k / 15). Note: the fixed-capital backtest used
# Rs 25k notional per trade; live deploys Rs 50k, so P&L / drawdown scale 2×.
LEVERAGE: int = 15

# Halt new entries when day P&L drops below this fraction of base equity.
DAILY_LOSS_KILL_PCT: float = _env_float("CRYPTO_DAILY_LOSS_KILL_PCT", 0.05)

# Hard cap on contracts per single order — protection against a sizing bug
# producing a giant order. With the corrected ETH contract size a Rs 50k
# order is ~31 contracts at $1,870, so 150 leaves headroom for a much lower
# ETH price while still catching an order-of-magnitude error. The old value
# of 300 existed only to accommodate the 10x contract-size bug.
MAX_LIVE_CONTRACTS: int = 50
MAX_LIVE_CONTRACTS_BY_ASSET: dict[str, int] = {
    "BTCUSD": 50,
    "ETHUSD": 150,
    # XAUT is ~$4k with contract_value 0.001, so one contract is only ~$4 —
    # a Rs 50k order needs ~145. The old cap of 50 silently capped XAUT at
    # ~35% of the intended budget.
    "XAUTUSD": 250,
}

# Ceiling on realized order notional as a multiple of the requested budget.
# A contract cap alone did NOT catch the ETH contract-size bug: it clipped
# 311 -> 300 contracts, which was still 10x the intended Rs 50k, because a
# contract count is meaningless without the right contract_value. This guard
# is denominated in the same units as the sizing input, so an order-of-
# magnitude error cannot get through regardless of contract metadata.
MAX_ORDER_NOTIONAL_MULT: float = 1.5


# ── Exit regime ──────────────────────────────────────────────────────────────
# "pure_sltp"      — bracket order: full exit on stop OR target. No trail, no
#                    partial TP. Validated on the 9-day Jun 2-10 backtest:
#                      pure_sltp:    38 trades, 92.1% WR, +₹14,407
#                      trail_partial: 42 trades, 90.5% WR, +₹13,443
#                    Pure SL/TP wins by ₹964 over the sample and is simpler
#                    to reason about (no peak-tracking, no half-position state).
# "trail_partial"  — partial TP at +1% closes half, trail arms at peak ≥0.5%
#                    and exits the rest on 0.25% giveback. Lower drawdown per
#                    trade but caps winners earlier.
EXIT_REGIME: str = os.environ.get("CRYPTO_EXIT_REGIME", "pure_sltp")


# ── Exchange-side protective bracket ─────────────────────────────────────────
# After every entry fills we attach a bracket at Delta (POST /v2/orders/bracket)
# so the stop and target live at the exchange, not only in this process. The 2s
# management tick remains as a backstop and still owns max-hold, which the
# exchange cannot express — but a container OOM, a network partition or a
# stalled WS no longer leaves a leveraged position completely unprotected.
EXCHANGE_BRACKET_ENABLED: bool = True

# Delta's bracket legs are LIMIT-only. A stop-limit priced exactly AT the
# trigger frequently does not fill in the fast move that triggered it, so the
# limit is placed this far THROUGH the trigger to cross the book. This is a
# fill guarantee, not a price improvement — on a 15x position a stop that
# fails to fill is far worse than a few bps of slippage.
BRACKET_LIMIT_SLIPPAGE_BPS: float = 15.0

# Trigger off mark price: it matches what the strategy and the backtest read,
# and it avoids a thin last-traded print wicking the stop out.
BRACKET_STOP_TRIGGER_METHOD: str = "mark_price"


# ── Position management ──────────────────────────────────────────────────────
# Max hold is owned by the strategy (price_action_sr.MAX_HOLD_MINUTES = 240).
# Do not reintroduce a second dial here — the runner imports the strategy's.

# Delta perp contract size, verified against GET /v2/products on 2026-08-04:
#   BTCUSD  id 27      contract_value 0.001
#   ETHUSD  id 3136    contract_value 0.01     <- NOT 0.001
#   XAUTUSD id 131253  contract_value 0.001
# ETH was previously listed here as 0.001, which made every ETH order 10x the
# intended notional and understated realized P&L (and therefore the daily-loss
# kill switch) by the same factor. Re-verify against /v2/products before
# adding a symbol.
CONTRACT_SIZE_BY_ASSET: dict[str, float] = {
    "BTCUSD": 0.001,
    "ETHUSD": 0.01,
    "XAUTUSD": 0.001,
}


def capital_pct_for(strategy_name: str) -> float:
    """Resolve per-strategy capital allocation by asset substring."""
    n = strategy_name.lower()
    if "btc" in n: return BTC_CAPITAL_PCT
    if "eth" in n: return ETH_CAPITAL_PCT
    return CAPITAL_USE_PCT


