"""The brain tier's ONLY route to an order. Deliberately narrow.

This module can express exactly two things: "here is an intent" and "I am still
alive". It cannot place an order itself, and that is the point — it does not
import AngelBroker, does not hold Angel credentials, and could not reach the
order API even if something upstream told it to.

WHY THE ISOLATION IS STRUCTURAL RATHER THAN A RULE

Angel requires a whitelisted static IP for place/modify/cancel and GTT, and for
nothing else (docs/ANGEL_ONE_API_NOTES.md). Market data, RMS and positions read
fine from anywhere. So the machine doing the thinking physically cannot place an
order even with valid credentials — it is not on the whitelist. Keeping the
credentials off it as well means there is no single mistake, and no single
compromised laptop, that turns the research box into a trading box.

There is a test asserting this package imports no order-placing symbol. If you
find yourself wanting to import AngelBroker here, the answer is no.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

SENTINEL_URL = os.environ.get("SENTINEL_URL", "http://127.0.0.1:8090")
REQUEST_TIMEOUT_SEC = 8.0

# Heartbeat cadence. The sentinel's dead-man's switch fires after
# DEADMAN_TIMEOUT_SEC of silence, so this must be comfortably shorter — six
# beats of headroom absorbs a transient blip without arming the switch, while
# still detecting a genuinely dead brain inside half a minute.
HEARTBEAT_INTERVAL_SEC = 5.0


class SentinelClient:
    def __init__(self, url: Optional[str] = None,
                 secret: Optional[str] = None):
        self.url = (url or SENTINEL_URL).rstrip("/")
        self._secret = secret

    def _post(self, path: str, body: dict) -> dict:
        import requests
        from sentinel.auth import build_envelope
        try:
            env = build_envelope(body, self._secret)
        except RuntimeError as e:
            logger.error("sentinel client: %s", e)
            return {"ok": False, "error": str(e)}
        try:
            r = requests.post(f"{self.url}{path}", json=env,
                              timeout=REQUEST_TIMEOUT_SEC)
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            return r.json()
        except Exception as e:
            # A failed send is NOT a failed order — the intent may or may not
            # have landed. The caller must reconcile against the sentinel's
            # position view rather than assume either outcome.
            logger.error("sentinel client: %s failed: %s", path, e)
            return {"ok": False, "error": str(e), "indeterminate": True}

    def submit_intent(self, *, symbol: str, side: str, option_type: str,
                      strike: int, lots: int, decision_id: str,
                      limit_price: Optional[float] = None,
                      sl_points: Optional[float] = None,
                      target_points: Optional[float] = None) -> dict:
        """Ask the sentinel to open a position. Returns its response verbatim.

        `decision_id` ties the fill back to the journaled decision, which is
        what lets attribution score the lenses that voted for it.
        """
        return self._post("/intent", {
            "type": "OPEN", "symbol": symbol, "side": side.upper(),
            "option_type": option_type.upper(), "strike": int(strike),
            "lots": int(lots), "decision_id": decision_id,
            "limit_price": limit_price, "sl_points": sl_points,
            "target_points": target_points,
        })

    def close_position(self, position_id: str, reason: str = "") -> dict:
        return self._post("/intent", {"type": "CLOSE",
                                      "position_id": position_id,
                                      "reason": reason})

    def heartbeat(self) -> dict:
        """Tell the sentinel the brain is alive. Silence arms the dead-man's switch."""
        return self._post("/heartbeat", {"type": "HEARTBEAT"})

    def status(self) -> dict:
        import requests
        try:
            r = requests.get(f"{self.url}/status", timeout=REQUEST_TIMEOUT_SEC)
            return r.json() if r.status_code == 200 else {
                "ok": False, "error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
