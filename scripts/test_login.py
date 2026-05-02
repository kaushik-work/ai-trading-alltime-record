"""
Test Angel One login. Run this manually whenever you suspect token issues.

  python scripts/test_login.py

Green output = bot is ready to trade. Red = fix credentials before market open.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.angel_fetcher import AngelFetcher

def main():
    print("\nTesting Angel One login...")

    af = AngelFetcher.get()
    af._api        = None   # force fresh login, never reuse cached session
    af._login_date = None

    ok = af._ensure_logged_in()
    if not ok:
        print("  FAIL: Login returned False. Check .env credentials.")
        sys.exit(1)

    nifty = af.get_index_ltp("NIFTY")
    bnk   = af.get_index_ltp("BANKNIFTY")

    if not nifty:
        print("  FAIL: Logged in but NIFTY LTP returned None (token may be partial).")
        sys.exit(1)

    print(f"  OK  NIFTY     = {nifty:,.2f}")
    print(f"  OK  BANKNIFTY = {bnk:,.2f}" if bnk else "  WARN BANKNIFTY LTP = None")

    # Quick instrument master check
    instruments = af._nfo_instruments()
    print(f"  OK  NFO instruments loaded: {len(instruments)}")

    print("\n  Angel One session is live. Bot is ready.\n")

if __name__ == "__main__":
    main()
