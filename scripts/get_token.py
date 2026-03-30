"""
Generate Zerodha Kite Connect access token.

Run this script once each trading day before starting the bot:
    python scripts/get_token.py

It will:
  1. Print the Kite login URL
  2. Wait for you to paste the request_token from the redirect
  3. Exchange it for an access token and save it to .env
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv, set_key

load_dotenv()

api_key = os.getenv("ZERODHA_API_KEY", "")
api_secret = os.getenv("ZERODHA_API_SECRET", "")

if not api_key or not api_secret:
    print("ERROR: ZERODHA_API_KEY and ZERODHA_API_SECRET must be set in .env")
    print("Get them from: https://developers.kite.trade/")
    sys.exit(1)

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("ERROR: kiteconnect not installed. Run: pip install kiteconnect")
    sys.exit(1)

kite = KiteConnect(api_key=api_key)
login_url = kite.login_url()

print("\nStep 1 — Open this URL in your browser and log in with your Zerodha account:")
print(f"\n  {login_url}\n")
print("Step 2 — After logging in you will be redirected to your app's redirect URL.")
print("         The URL will contain:  ?request_token=xxxxxxxxxxxxxxxx&status=success")
print("         Copy ONLY the request_token value.\n")

request_token = input("Paste request_token here: ").strip()
if not request_token:
    print("ERROR: No request_token provided.")
    sys.exit(1)

try:
    data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = data["access_token"]

    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    token_set_at = datetime.now(ist).isoformat()

    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    set_key(env_path, "ZERODHA_ACCESS_TOKEN", access_token, quote_mode="never")
    set_key(env_path, "ZERODHA_TOKEN_SET_AT", token_set_at, quote_mode="never")

    print(f"\nSuccess! Access token saved to .env")
    print(f"Token: {access_token[:8]}...{access_token[-4:]}")
    print("\nThe token is valid until midnight IST. Run this script again tomorrow.")
except Exception as e:
    print(f"\nFailed to generate session: {e}")
    sys.exit(1)
