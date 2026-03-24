import os
from dotenv import load_dotenv

load_dotenv()

# Trading Mode
TRADING_MODE = os.getenv("TRADING_MODE", "paper")  # "paper" or "live"
IS_PAPER = TRADING_MODE == "paper"

# Zerodha (jugaad-trader — free, no paid API key needed)
ZERODHA_USER_ID = os.getenv("ZERODHA_USER_ID", "")
ZERODHA_PASSWORD = os.getenv("ZERODHA_PASSWORD", "")
ZERODHA_TOTP_SECRET = os.getenv("ZERODHA_TOTP_SECRET", "")

# Zerodha Kite Connect (optional — upgrade later)
KITE_API_KEY = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "")

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
RISK_PER_TRADE_PCT   = float(os.getenv("RISK_PER_TRADE_PCT", 5.0))   # % of budget at risk per trade
MAX_DAILY_LOSS_PCT   = float(os.getenv("MAX_DAILY_LOSS_PCT", 10.0))  # % of budget — auto-pause if hit

# Derived risk amounts (budget-relative, update these when budget grows)
MAX_TRADE_AMOUNT = int(os.getenv("MAX_TRADE_AMOUNT", 10000))   # max capital per single trade
MAX_DAILY_LOSS   = int(os.getenv("MAX_DAILY_LOSS",   2000))    # ₹2000 daily loss limit on ₹20k

# ── Lot Sizes (NSE) ────────────────────────────────────────────────────────────
LOT_SIZES = {
    "NIFTY":     25,   # 1 lot = 25 units (NSE standard, verify before live trading)
    "BANKNIFTY": 15,   # 1 lot = 15 units
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
MIN_SIGNAL_SCORE = int(os.getenv("MIN_SIGNAL_SCORE", 5))  # trade only when score ≥ 5/10

# ── Watchlist — NIFTY & BANKNIFTY only ────────────────────────────────────────
WATCHLIST = {
    "indices": ["NIFTY", "BANKNIFTY"],
}

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "db", "trading.db")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

# ── Market Hours (IST) ─────────────────────────────────────────────────────────
MARKET_OPEN  = "09:15"
MARKET_CLOSE = "15:30"

# ── Claude model ───────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-6"
