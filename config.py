import os
from dotenv import load_dotenv

load_dotenv()

# ── Secrets (env only — never hardcode) ───────────────────────────────────────
TRADING_MODE         = os.getenv("TRADING_MODE", "live")    # "paper" | "live"
IS_PAPER             = TRADING_MODE == "paper"

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
STARTING_BUDGET    = 125_000   # ₹1.25L — 3 lots × 2 strategies running independently
RISK_PER_TRADE_PCT = 2.0       # % of portfolio risked per trade (backtest optimal)
MAX_OPEN_POSITIONS = 1         # one trade at a time — no concurrent positions
MAX_TRADE_AMOUNT   = 40_000    # max capital per trade — covers 3 NIFTY lots at ₹180 ATM
MAX_DAILY_LOSS              = 6_250   # ₹6,250 combined hard stop (5% of ₹1.25L) — all strategies pause when hit
PER_STRATEGY_DAILY_LOSS_PCT = 3.0    # each strategy pauses independently at 3% loss (₹3,750) — doesn't stop others
MAX_DAILY_TRADES            = 1      # one trade per day — no re-entry after SL/TP

# ── Lot Sizes (NSE) ────────────────────────────────────────────────────────────
# Verify current lot sizes at NSE or Angel One contract specs
LOT_SIZES = {
    "NIFTY": 65,   # revised Feb 2026
}
MIN_LOTS = 1   # fallback default — runtime value is set from dashboard settings

# ── Intraday Timing ────────────────────────────────────────────────────────────
TRADING_TYPE     = "intraday"
INTRADAY_START   = "09:30"    # open at 9:30 — first candle close
INTRADAY_EXIT_BY = "11:20"    # auto square-off all positions at 11:20
ORB_WINDOW_MINS  = 15
LUNCH_SKIP_START = "23:59"    # disabled — we exit at 11:20 before lunch
LUNCH_SKIP_END   = "23:59"

# ── Stop Loss / Take Profit ────────────────────────────────────────────────────
STOP_LOSS_PCT   = 1.5    # % of option premium — SL trigger for all strategies

# Per-strategy R:R ratios (TP = STOP_LOSS_PCT × ratio)
ATR_RR_RATIO    = 3.0    # ATR Intraday  → 1:3  (TP = 4.5%)
TAKE_PROFIT_PCT = STOP_LOSS_PCT * ATR_RR_RATIO  # 4.5% — alias used by backtest + banner

# ── Option Premium Target Range ───────────────────────────────────────────────
MIN_OPTION_PREMIUM = 150   # search strikes until premium ≥ ₹150
MAX_OPTION_PREMIUM = 170   # search strikes until premium ≤ ₹170

# ── Signal Score ───────────────────────────────────────────────────────────────
MIN_SIGNAL_SCORE = 6   # trade only when score ≥ 6 (backtest: 45.2% WR at 6 vs 44.7% at 5)
FIB_OF_SIGNAL_SCORE = 6

# ── Watchlist ──────────────────────────────────────────────────────────────────
WATCHLIST = {
    "indices": ["NIFTY"],
}

# ── Market Hours (IST) ─────────────────────────────────────────────────────────
MARKET_OPEN  = "09:15"
MARKET_CLOSE = "15:30"

# ── Claude Model ───────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-6"

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
