"""
Verify Angel One SmartAPI session.

Angel One uses TOTP auto-login — no manual token step is needed each day.
The bot generates a fresh session automatically at startup using pyotp.

Run this script to confirm your credentials are working:
    python scripts/get_token.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

required = ["ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD", "ANGEL_TOTP_TOKEN"]
missing = [k for k in required if not os.getenv(k)]
if missing:
    print(f"ERROR: Missing .env keys: {', '.join(missing)}")
    sys.exit(1)

print("Checking Angel One SmartAPI connection...")
try:
    from data.angel_fetcher import AngelFetcher
    fetcher = AngelFetcher.get()
    ok = fetcher._ensure_logged_in()
    if not ok:
        print("ERROR: Login failed. Check credentials in .env")
        sys.exit(1)
    live = fetcher.is_token_live()
    print(f"Session active: {live}")
    ltp = fetcher.get_index_ltp("NIFTY")
    print(f"NIFTY LTP: {ltp}")
    print("\nAngel One session OK — bot is ready.")
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)
