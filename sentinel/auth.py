"""Signed intent protocol between the brain tier and the execution sentinel.

The sentinel is the only process holding Angel order credentials, and it sits on
a public IP. Anything that can POST to it can move real money, so the protocol
has to assume the network is hostile.

    HMAC-SHA256 over a canonical serialisation of the intent, keyed by a shared
    secret that never crosses the wire, plus a timestamp and a nonce.

Three attacks this is built against:

  TAMPERING     the signature covers the canonical JSON of the whole intent, so
                changing a strike, a side or a quantity invalidates it.
  REPLAY        a nonce is accepted exactly once, and intents older than
                MAX_INTENT_AGE_SEC are refused outright. Without the age bound
                the nonce cache would have to be infinite to be safe.
  TIMING        signatures are compared with hmac.compare_digest, not ==.

What this is NOT: transport security. Run the sentinel behind TLS. A signature
proves the intent was authored by someone holding the secret and has not been
altered; it does nothing to keep the contents private.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Optional

# Intents older than this are refused, which is what makes a bounded nonce
# cache sufficient. Generous enough to survive clock skew between two hosts,
# tight enough that a captured intent is stale before it is useful.
MAX_INTENT_AGE_SEC = 30.0

# Nonces are remembered for twice the age bound, so a replay can never slip
# through the gap between "still fresh" and "already forgotten".
NONCE_TTL_SEC = MAX_INTENT_AGE_SEC * 2


def _secret() -> str:
    s = os.environ.get("SENTINEL_SECRET", "")
    if not s or len(s) < 32:
        raise RuntimeError(
            "SENTINEL_SECRET must be set and at least 32 chars. This key is the "
            "only thing standing between the open internet and your order API.")
    return s


def canonical(payload: dict) -> str:
    """Deterministic serialisation. Both sides MUST produce identical bytes.

    sort_keys and a fixed separator remove every source of ambiguity — dict
    ordering, whitespace, float formatting — that would otherwise make a valid
    intent fail verification on a different Python version or platform.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str)


def sign(payload: dict, secret: Optional[str] = None) -> str:
    key = (secret or _secret()).encode()
    return hmac.new(key, canonical(payload).encode(), hashlib.sha256).hexdigest()


def build_envelope(intent: dict, secret: Optional[str] = None) -> dict:
    """Wrap an intent with the freshness fields, then sign the whole thing.

    The timestamp and nonce are signed too — signing only the intent would let
    an attacker re-stamp a captured message and defeat the replay window.
    """
    body = {
        "intent": intent,
        "ts": time.time(),
        "nonce": uuid.uuid4().hex,
    }
    body["signature"] = sign({k: v for k, v in body.items()}, secret)
    return body


class NonceCache:
    """Remembers recently seen nonces. Bounded by TTL, swept on read."""

    def __init__(self, ttl: float = NONCE_TTL_SEC):
        self.ttl = ttl
        self._seen: dict[str, float] = {}

    def check_and_add(self, nonce: str, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        cutoff = now - self.ttl
        if self._seen:
            self._seen = {n: t for n, t in self._seen.items() if t > cutoff}
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True

    def __len__(self) -> int:
        return len(self._seen)


def verify(envelope: dict, cache: NonceCache,
           secret: Optional[str] = None,
           now: Optional[float] = None) -> tuple[bool, str]:
    """Validate an envelope. Returns (ok, reason).

    Order matters: cheap structural checks first, signature last, and the nonce
    is only consumed AFTER the signature verifies — otherwise an attacker could
    burn valid nonces by flooding garbage.
    """
    now = now if now is not None else time.time()

    for field in ("intent", "ts", "nonce", "signature"):
        if field not in envelope:
            return False, f"missing {field}"

    try:
        age = now - float(envelope["ts"])
    except (TypeError, ValueError):
        return False, "unparseable timestamp"
    if age > MAX_INTENT_AGE_SEC:
        return False, f"stale intent ({age:.1f}s old)"
    if age < -MAX_INTENT_AGE_SEC:
        return False, f"intent from the future ({-age:.1f}s ahead) — check clocks"

    body = {k: v for k, v in envelope.items() if k != "signature"}
    try:
        expected = sign(body, secret)
    except RuntimeError as e:
        return False, str(e)
    if not hmac.compare_digest(expected, str(envelope["signature"])):
        return False, "bad signature"

    if not cache.check_and_add(str(envelope["nonce"]), now):
        return False, "replayed nonce"

    return True, "ok"
