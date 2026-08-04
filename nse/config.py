"""NSE production dials — hardcoded, not .env."""

from __future__ import annotations

from datetime import date, time

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
# NSE extended equity-derivatives close from 15:30 to 15:40 on 2026-08-03 to
# align with the cash-market Closing Auction Session (15:15-15:35). BSE has
# not issued a matching circular, so BFO (SENSEX) still closes at 15:30 and
# its final settlement still uses the 15:00-15:30 VWAP window. Flip the BFO
# entry to time(15, 40) when BSE announces.
MARKET_OPEN = time(9, 15)
MARKET_CLOSE_BY_EXCHANGE: dict[str, time] = {
    "NFO": time(15, 40),
    "BFO": time(15, 30),
}
# Widest close across exchanges — use only for coarse "is the day over" checks.
# Per-symbol decisions must go through market_close_for().
MARKET_CLOSE = max(MARKET_CLOSE_BY_EXCHANGE.values())


# The NFO 15:40 close took effect on this date. Contracts that expired before
# it settled at 15:30, so historical time-to-expiry must use the old bell.
NFO_EXTENDED_CLOSE_FROM = date(2026, 8, 3)


def market_close_for(symbol: str, on: date | None = None) -> time:
    """Exchange-aware close time for a symbol.

    Pass `on` to make it history-aware: NFO expiries dated before
    2026-08-03 closed at 15:30, so backtests over older option snapshots
    get the correct time-to-expiry.
    """
    exch = EXCHANGE.get(symbol, "NFO")
    if exch == "NFO" and on is not None and on < NFO_EXTENDED_CLOSE_FROM:
        return time(15, 30)
    return MARKET_CLOSE_BY_EXCHANGE.get(exch, MARKET_CLOSE)

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

# ── Broker-side protective bracket (GTT OCO) ─────────────────────────────────
# Each leg gets ONE GTT rule of type OCO carrying both a target and a stop, so
# the exchange holds the bracket even if this process dies. One rule per leg —
# never two — because separate target and stop rules have no one-cancels-other
# relationship: the survivor stays armed after the first fires and would later
# open a brand-new position on a combo we no longer hold.
#
# IMPORTANT — why these are wide:
# The strategy's stop/target are on the COMBO's net value (CE - PE). No
# per-leg rule can express that. If a leg's GTT fired at the strategy's own
# stop it would close that leg and leave the other one NAKED, which is
# strictly worse risk than the combo it was protecting. So the GTT sits far
# enough out that the runner's combo-level exit almost always wins the race,
# and the GTT only matters when the runner is not there to act at all.
#
# Levels are in OPTION PREMIUM terms, derived from each leg's actual fill —
# they are NOT spot points.
GTT_ENABLED: bool = True
# How much wider than the strategy's own stop the broker bracket sits.
GTT_BRACKET_MULT: float = 2.5
# ATM option delta. The synthetic forward is delta ~1 overall, so each leg
# absorbs roughly half the combo's move. Used to convert the combo's
# spot-based stop into a per-leg PREMIUM distance.
GTT_LEG_DELTA: float = 0.5
# Exchange tick floor — a GTT price must never be <= 0.
GTT_MIN_PREMIUM: float = 0.05
# GTT legs are limit orders. A limit priced exactly AT its trigger frequently
# does not fill in the move that fired it, which on a stop is the worst
# possible time to miss. Placing the limit this far THROUGH the trigger makes
# the leg behave like a market order while still bounding the fill.
GTT_TRIGGER_SLIP_PCT: float = 2.0


def gtt_limit_through(trigger: float, is_exit_sell: bool) -> float:
    """Limit price set through the trigger so the leg actually crosses.

    An exiting SELL must accept a LOWER price; an exiting BUY a HIGHER one.
    """
    slip = GTT_TRIGGER_SLIP_PCT / 100.0
    px = trigger * (1 - slip) if is_exit_sell else trigger * (1 + slip)
    return round(max(GTT_MIN_PREMIUM, px), 2)
# Rule lifetime is clamped to the contract's remaining life; a weekly option
# must never carry a 365-day rule that outlives the instrument.
GTT_MAX_TIMEPERIOD_DAYS: int = 365


def gtt_leg_distance(spot: float) -> float:
    """Premium distance for the broker bracket, in option points.

    Derived from the STRATEGY's stop, not from a fraction of premium. The
    combo exits when spot moves STOP_LOSS_PCT; each leg carries about
    GTT_LEG_DELTA of that move. Multiplying by GTT_BRACKET_MULT puts the
    broker bracket safely outside the combo exit.

    Sizing this as a fraction of premium instead would be wrong: a 75%-of-
    premium stop on a 45-point leg is 34 points, while the combo's own stop
    needs ~180 points of leg movement — the rule would fire first and leave
    the other leg naked, which is worse risk than the combo it protects.
    """
    return abs(spot) * STOP_LOSS_PCT * GTT_LEG_DELTA * GTT_BRACKET_MULT


def gtt_levels_for_leg(fill_premium: float, is_long: bool,
                       spot: float) -> tuple[float, float]:
    """Protective (stop, target) premium levels for one option leg.

    Single source of truth: the live broker attaches GTT rules at these levels
    and the backtest checks the same ones, so the two cannot drift apart.

    A long option loses as premium falls; a short loses as it rises. A long
    leg's stop usually floors at GTT_MIN_PREMIUM, which is correct — its loss
    is already capped at the premium paid, so it needs no protection.
    """
    dist = gtt_leg_distance(spot)
    if is_long:
        stop = max(GTT_MIN_PREMIUM, fill_premium - dist)
        target = fill_premium + dist
    else:
        stop = fill_premium + dist
        target = max(GTT_MIN_PREMIUM, fill_premium - dist)
    return round(stop, 2), round(target, 2)

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
