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


# ── Risk limits ──────────────────────────────────────────────────────────────
# Leverage applied per order. Price-action S/R backtest (Apr-Jun 2026,
# liquidation-aware on 1m wicks) shows 40× hits the ~400%/mo target with
# zero liquidations in-sample. Effective exposure = LEVERAGE × CAPITAL_USE_PCT
# (40 × 0.50 = 20× pool notional). This is aggressive: a ~2.5% adverse wick
# wipes the allocated margin. Override via CRYPTO_LEVERAGE env for paper tests.
LEVERAGE: int = _env_int("CRYPTO_LEVERAGE", 40)

# Halt new entries when day P&L drops below this fraction of base equity.
DAILY_LOSS_KILL_PCT: float = _env_float("CRYPTO_DAILY_LOSS_KILL_PCT", 0.05)

# Hard cap on contracts per single order — extra protection against a
# sizing bug producing a giant order.
MAX_LIVE_CONTRACTS: int = _env_int("CRYPTO_MAX_LIVE_CONTRACTS", 50)


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


# ── Position management ──────────────────────────────────────────────────────
MAX_HOLD_HOURS: int = 72

# Delta India BTC/ETH perp contract size = 0.001 underlying. Used by the
# sizing formula to convert USD notional → integer contract count.
CONTRACT_SIZE_BY_ASSET: dict[str, float] = {
    "BTCUSD": 0.001,
    "ETHUSD": 0.001,
    "XAUTUSD": 0.001,
}


def capital_pct_for(strategy_name: str) -> float:
    """Resolve per-strategy capital allocation by asset substring."""
    n = strategy_name.lower()
    if "btc" in n: return BTC_CAPITAL_PCT
    if "eth" in n: return ETH_CAPITAL_PCT
    return CAPITAL_USE_PCT
