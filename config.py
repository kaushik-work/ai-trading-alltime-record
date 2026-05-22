import os
from dotenv import load_dotenv

load_dotenv()

# ── Mode ──────────────────────────────────────────────────────────────────────
# Live trading only. Paper trading + MockBroker were removed entirely; the
# TRADING_MODE env var is no longer read. IS_PAPER is kept as a constant
# False for any legacy call site that imports it.
IS_PAPER = False

# ── Angel One SmartAPI ────────────────────────────────────────────────────────
ANGEL_API_KEY        = os.getenv("ANGEL_API_KEY", "")
ANGEL_CLIENT_ID      = os.getenv("ANGEL_CLIENT_ID", "")
ANGEL_PASSWORD       = os.getenv("ANGEL_PASSWORD", "")
ANGEL_TOTP_TOKEN     = os.getenv("ANGEL_TOTP_TOKEN", "")    # QR secret for pyotp
ANGEL_JWT_TOKEN      = os.getenv("ANGEL_JWT_TOKEN", "")     # set at runtime
ANGEL_REFRESH_TOKEN  = os.getenv("ANGEL_REFRESH_TOKEN", "") # set at runtime
ANGEL_FEED_TOKEN     = os.getenv("ANGEL_FEED_TOKEN", "")    # set at runtime
ANGEL_TOKEN_SET_AT   = os.getenv("ANGEL_TOKEN_SET_AT", "")  # set at runtime

ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
DB_PATH              = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "trading.db"))

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR     = os.path.join(BASE_DIR, "logs")
JOURNALS_DIR = os.path.join(BASE_DIR, "journals")

# ── Trading Roadmap ────────────────────────────────────────────────────────────
# Phase A : ₹50K budget  | intraday NIFTY only      | goal ₹1.5L
# Phase B : ₹1.5L budget | intraday + swing          | goal ₹15L
# Phase C : ₹15L+        | options selling (future)
TRADING_PHASE    = "A"
TARGET_PHASE_A   = 150_000
TARGET_PHASE_B   = 1_500_000

# ── Budget & Risk ──────────────────────────────────────────────────────────────
# Shadow trading only — risk knobs live in core/risk_budget.py.
STARTING_BUDGET = 50_000   # baseline capital for the shadow risk-budget

# ── Lot Sizes (NSE) ────────────────────────────────────────────────────────────
LOT_SIZES = {
    "NIFTY": 65,   # revised Feb 2026
}
MIN_LOTS = 1
MAX_LOTS = 1

# ── Watchlist ──────────────────────────────────────────────────────────────────
WATCHLIST = {
    "indices": ["NIFTY"],
}

# ── Market Hours (IST) ─────────────────────────────────────────────────────────
MARKET_OPEN  = "09:15"
MARKET_CLOSE = "15:30"

# ── Claude Model ───────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-6"

# ── NSE Market Holidays — exchange closed, no trading possible ────────────────
NSE_MARKET_HOLIDAYS: dict[str, str] = {
    "2026-01-26": "Republic Day",
    "2026-02-26": "Mahashivratri",
    "2026-03-25": "Holi",
    "2026-04-02": "Ram Navami",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-08-15": "Independence Day",
    "2026-08-27": "Ganesh Chaturthi",
    "2026-10-02": "Gandhi Jayanti / Dussehra",
    "2026-10-20": "Diwali (Laxmi Puja)",
    "2026-10-21": "Diwali (Balipratipada)",
    "2026-11-05": "Gurunanak Jayanti",
    "2026-12-25": "Christmas",
}

# ── Event Calendar — skip on these dates (IV crush guaranteed) ────────────────
EVENT_BLOCK_DATES: dict[str, str] = {
    "2026-02-01": "Union Budget",
    "2026-02-07": "RBI MPC Policy",
    "2026-04-09": "RBI MPC Policy",
    "2026-06-06": "RBI MPC Policy",
    "2026-08-06": "RBI MPC Policy",
    "2026-10-08": "RBI MPC Policy",
    "2026-12-05": "RBI MPC Policy",
}
