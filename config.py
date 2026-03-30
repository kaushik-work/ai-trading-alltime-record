import os
from dotenv import load_dotenv

load_dotenv()

# Trading Mode
TRADING_MODE = os.getenv("TRADING_MODE", "paper")  # "paper" or "live"
IS_PAPER = TRADING_MODE == "paper"

# Zerodha Kite Connect API (https://kite.trade/docs/connect/v3/)
ZERODHA_USER_ID   = os.getenv("ZERODHA_USER_ID", "")      # for logging only
ZERODHA_API_KEY   = os.getenv("ZERODHA_API_KEY", "")
ZERODHA_API_SECRET = os.getenv("ZERODHA_API_SECRET", "")
ZERODHA_ACCESS_TOKEN = os.getenv("ZERODHA_ACCESS_TOKEN", "")  # generated daily via scripts/get_token.py
ZERODHA_TOKEN_SET_AT = os.getenv("ZERODHA_TOKEN_SET_AT", "")  # ISO timestamp written by get_token.py

# Claude API
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── Trading Roadmap ────────────────────────────────────────────────────────────
# Phase A : Intraday NIFTY + BANKNIFTY  | Budget ₹20,000  | Goal: compound to ₹1.5L
# Phase B : Intraday + short-term swing | Budget ₹1.5L    | Goal: compound to ₹15L
# Phase C : Options selling (Iron Condor / Straddle selling) | Budget ₹15L+
#
# Current phase is set by TRADING_PHASE env var.
TRADING_PHASE = os.getenv("TRADING_PHASE", "A")  # "A", "B", or "C"

# ── Budget & Position Sizing ───────────────────────────────────────────────────
# Phase A: start ₹20,000, risk ≤5% per trade (₹1,000), max 2 positions
# Phase B: ₹1.5L budget, risk ≤3% per trade, max 3 positions
# Phase C: ₹15L+, options selling — separate strategy module (future)

STARTING_BUDGET      = int(os.getenv("STARTING_BUDGET", 20000))
TARGET_PHASE_A       = 150000    # ₹1.5L — unlock Phase B
TARGET_PHASE_B       = 1500000   # ₹15L  — unlock options selling (Phase C)
RISK_PER_TRADE_PCT   = float(os.getenv("RISK_PER_TRADE_PCT", 2.0))   # % of budget at risk per trade (backtest optimal: 2% → 51.5% net / 13.1% DD)
MAX_DAILY_LOSS_PCT   = float(os.getenv("MAX_DAILY_LOSS_PCT", 10.0))  # % of budget — auto-pause if hit

# Derived risk amounts (budget-relative, update these when budget grows)
MAX_TRADE_AMOUNT = int(os.getenv("MAX_TRADE_AMOUNT", 10000))   # max capital per single trade
MAX_DAILY_LOSS   = int(os.getenv("MAX_DAILY_LOSS",   2000))    # ₹2000 daily loss limit on ₹20k

# ── Lot Sizes (NSE) ────────────────────────────────────────────────────────────
LOT_SIZES = {
    "NIFTY":     65,   # 1 lot = 65 units (revised Feb 2026 — source: Zerodha support)
    "BANKNIFTY": 30,   # 1 lot = 30 units (revised Feb 2026)
    # NOTE: lot sizes change on monthly expiry cycles when SEBI revises them.
    # Contracts in the current monthly series keep the OLD lot size until expiry.
    # New monthly (and weekly) contracts starting after the revision date use the NEW lot size.
    # Always verify at: https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/lot-size-for-index-derivatives
}
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", 2))   # max 2 concurrent for Phase A

# ── Intraday Settings ──────────────────────────────────────────────────────────
# AishDoc approach: wait for market to settle, trade in the confirmed trend window
TRADING_TYPE      = "intraday"           # "intraday" | "swing" | "positional"
INTRADAY_START    = "09:45"              # don't trade the first 30 min (volatile/fake moves)
INTRADAY_EXIT_BY  = "15:10"             # square off all positions before this time
ORB_WINDOW_MINS   = 15                  # Opening Range = first 15 min (3 × 5-min candles)

# ── Stop Loss / Take Profit ────────────────────────────────────────────────────
STOP_LOSS_PCT    = float(os.getenv("STOP_LOSS_PCT",    1.5))   # intraday: tighter 1.5% SL
TAKE_PROFIT_PCT  = float(os.getenv("TAKE_PROFIT_PCT",  3.0))   # target 1:2 R:R → 3% TP

# ── Strategy Parameters (AishDoc) ─────────────────────────────────────────────
MIN_SIGNAL_SCORE = int(os.getenv("MIN_SIGNAL_SCORE", 6))  # trade only when score ≥ 6/10 (backtest optimal: 45.2% WR at 6 vs 44.7% at 5)

# ── Watchlist — NIFTY & BANKNIFTY only ────────────────────────────────────────
WATCHLIST = {
    "indices": ["NIFTY", "BANKNIFTY"],
}

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH      = os.getenv("DB_PATH", os.path.join(BASE_DIR, "db", "trading.db"))
LOGS_DIR     = os.path.join(BASE_DIR, "logs")
JOURNALS_DIR = os.getenv("JOURNALS_DIR", os.path.join(BASE_DIR, "journals"))

# ── Market Hours (IST) ─────────────────────────────────────────────────────────
MARKET_OPEN  = "09:15"
MARKET_CLOSE = "15:30"

# ── Claude model ───────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-6"

# ── India VIX Gate ─────────────────────────────────────────────────────────────
# Skip all new entries if India VIX is above this level at market open.
# Normal: 12–16. Budget/RBI: 18–22. War/panic: 22–30+.
# Set higher (e.g. 25) to only block extreme events, lower (18) to block RBI days too.
VIX_THRESHOLD = float(os.getenv("VIX_THRESHOLD", 22.0))

# ── Event Calendar — skip trading on these dates (IV crush guaranteed) ─────────
# Budget / RBI MPC / election result days: premiums halve post-announcement.
# Update this dict each quarter. Key = "YYYY-MM-DD", value = event label.
EVENT_BLOCK_DATES: dict[str, str] = {
    "2026-02-01": "Union Budget",
    "2026-02-07": "RBI MPC Policy",
    "2026-04-09": "RBI MPC Policy",
    "2026-06-06": "RBI MPC Policy",
    "2026-08-06": "RBI MPC Policy",
    "2026-10-08": "RBI MPC Policy",
    "2026-12-05": "RBI MPC Policy",
}
