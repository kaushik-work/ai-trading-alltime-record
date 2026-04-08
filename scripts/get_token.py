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

from dotenv import load_dotenv

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
    import re
    ist = timezone(timedelta(hours=5, minutes=30))
    token_set_at = datetime.now(ist).isoformat()

    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")

    # Write in-place (same inode) so Docker bind mounts pick up the new value.
    # set_key() renames a temp file → new inode → container never sees the update.
    with open(env_path, "r") as f:
        content = f.read()
    content = re.sub(r"^ZERODHA_ACCESS_TOKEN=.*$", f"ZERODHA_ACCESS_TOKEN={access_token}", content, flags=re.MULTILINE)
    content = re.sub(r"^ZERODHA_TOKEN_SET_AT=.*$", f"ZERODHA_TOKEN_SET_AT={token_set_at}", content, flags=re.MULTILINE)
    with open(env_path, "w") as f:
        f.write(content)

    print(f"\nSuccess! Access token saved to .env")
    print(f"Token: {access_token[:8]}...{access_token[-4:]}")
    print("\nThe token is valid until midnight IST. Run this script again tomorrow.")
except Exception as e:
    print(f"\nFailed to generate session: {e}")
    sys.exit(1)
