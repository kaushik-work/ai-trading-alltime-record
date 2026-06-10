"""
Minimal config kept for the NSE option-chain collectors only.

Crypto bot dials live in core/risk_management.py. Anything NSE-trading
specific (lot sizes, watchlist, market hours, trading phases) has been
retired along with the NSE trading bot.

This file still exists because:
  - data/angel_fetcher.py imports it for Angel One SmartAPI credentials
  - core/ipc.py imports it for the NSE market-holiday calendar (used by
    collectors to skip non-trading days)
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Angel One SmartAPI (used by NSE option-chain collectors only) ────────────
ANGEL_API_KEY       = os.getenv("ANGEL_API_KEY", "")
ANGEL_CLIENT_ID     = os.getenv("ANGEL_CLIENT_ID", "")
ANGEL_PASSWORD      = os.getenv("ANGEL_PASSWORD", "")
ANGEL_TOTP_TOKEN    = os.getenv("ANGEL_TOTP_TOKEN", "")     # base32 secret for pyotp
ANGEL_JWT_TOKEN     = os.getenv("ANGEL_JWT_TOKEN", "")      # runtime
ANGEL_REFRESH_TOKEN = os.getenv("ANGEL_REFRESH_TOKEN", "")  # runtime
ANGEL_FEED_TOKEN    = os.getenv("ANGEL_FEED_TOKEN", "")     # runtime
ANGEL_TOKEN_SET_AT  = os.getenv("ANGEL_TOKEN_SET_AT", "")   # runtime

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")

# ── NSE Market Holidays — collectors skip on these dates ─────────────────────
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
