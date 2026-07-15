"""NSE production dials — hardcoded, not .env."""

from __future__ import annotations

from datetime import time

# Supported underlyings and their option chain step sizes / lot sizes.
SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX")

STEP_SIZES: dict[str, int] = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "SENSEX": 100,
}

# Lot sizes effective January 2026 (NSE revision from Dec 2025 expiry).
# Always verify from the broker's instrument master / scrip file before live.
LOT_SIZES: dict[str, int] = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 60,
    "SENSEX": 20,
}

EXCHANGE: dict[str, str] = {
    "NIFTY": "NFO",
    "BANKNIFTY": "NFO",
    "FINNIFTY": "NFO",
    "SENSEX": "BFO",
}

# Market hours in IST (tz-naive).
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# Synthetic-forward signal dials.
ENTRY_PCT = 0.006            # 0.6% gate
PERSIST_HOURS = 1            # same-sign for ≥1 hour
MIN_STRIKES = 3              # ≥3 strikes must agree
MONEYNESS = 0.05             # ATM ±5%
TT_MIN_HOURS = 6             # minimum time-to-expiry
TT_MAX_HOURS = 72            # maximum time-to-expiry

# Execution dials.
TICK_ENTRY_MINUTES = 5
TICK_POSITION_SECONDS = 30
MAX_HOLD_HOURS = 72

# Total trading budget (INR). This is the maximum capital the bot may deploy
# across all NSE symbols at any one time. Live execution queries Angel One's
# margin API per combo before every order, so this number is the only hard
# capital dial. Backtests cannot call the broker, so they use the fallback
# estimates below.
TOTAL_CAPITAL_INR = 300_000.0

# Backtest-only fallback margin per combo lot. These are NOT fixed constants;
# they are conservative midpoints of the realistic ranges for naked/hedged
# short option combos. Override with actual SPAN values when available.
# Live code must use AngelFetcher.get_margin_required() instead.
BACKTEST_MARGIN_FALLBACK_INR: dict[str, float] = {
    "NIFTY": 160_000.0,
    "BANKNIFTY": 160_000.0,
    "FINNIFTY": 120_000.0,
    "SENSEX": 120_000.0,
}

# Product type for live orders.  CARRYFORWARD = NRML (no intraday leverage).
# Change to "INTRADAY" only if you explicitly want MIS margin/leverage.
PRODUCT_TYPE = "CARRYFORWARD"

# Keep legacy name for compatibility until all imports are updated.
FIXED_CAPITAL_INR = TOTAL_CAPITAL_INR

# Exit dials.
STOP_LOSS_PCT = 0.015        # 1.5% SL on synthetic-forward notional
TARGET_PCT = 0.010           # 1.0% TP
TRAIL_PEAK_PCT = 0.005
TRAIL_GIVEBACK_PCT = 0.0025

# Cost assumptions for backtest (live broker records actual fills).
SLIPPAGE_BPS = 5.0
FEE_BPS_PER_LEG = 3.0        # per fill, both entry and exit
