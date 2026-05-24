"""
WebSocket subscription manager — keeps the strike list in sync with spot.

NIFTY spot moves throughout the day. Our signals need ATM ± 4 strikes
(9 strikes × CE/PE = 18 NFO tokens) plus the NIFTY index itself. When spot
drifts more than ~100 pts from the last subscription center, we need to
rotate: subscribe new strikes, unsubscribe old ones, and update the market
state registry to match.

Lifecycle:
  mgr = SubscriptionManager(ws_client, market_state)
  mgr.start()                 # registers callbacks, kicks off first sub
  ...
  mgr.refresh(now_dt)         # periodic call from scheduler — checks drift
  ...
  mgr.stop()

Design:
  • One singleton per process.
  • Token lookup uses AngelFetcher._nfo_instruments() (already cached).
  • Subscribe NEW tokens BEFORE unsubscribing old ones so we never go blind.
  • Re-evaluates after each WebSocket reconnect (idempotent).
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

STRIKES_AROUND_ATM = 4      # subscribe ATM ± 4 = 9 strikes
DRIFT_THRESHOLD    = 100    # pts of spot drift before rotating
STRIKE_STEP        = 50     # NIFTY step

# NIFTY-50 index token on NSE_CM (well-known constant)
NIFTY_INDEX_TOKEN = "99926000"


class SubscriptionManager:
    def __init__(self, ws_client, market_state):
        self._ws = ws_client
        self._state = market_state
        self._lock = threading.Lock()
        self._current_center: Optional[int] = None   # ATM strike last subscribed around
        self._current_expiry: Optional[date] = None
        self._spot_registered = False
        self._started = False

    def start(self) -> None:
        """Register the spot token and wire reconnect callback."""
        if self._started:
            return
        self._started = True
        # Tell market state which token is spot
        self._state.register_spot("NSE", NIFTY_INDEX_TOKEN)
        # On every reconnect, force a re-sync of subscriptions
        self._ws.on_connect(self._on_ws_connect)
        # Initial subscription kicked off by the first refresh() call

    def stop(self) -> None:
        self._started = False

    # ── Reconnect handler ──────────────────────────────────────────────────

    def _on_ws_connect(self) -> None:
        """WS just (re)connected — re-subscribe spot + current option strikes."""
        try:
            # Spot
            self._ws.subscribe([("NSE", NIFTY_INDEX_TOKEN)])
            self._spot_registered = True
            # Re-issue current strike subs if we had any
            with self._lock:
                if self._current_center is not None and self._current_expiry is not None:
                    tokens = self._resolve_strike_tokens(self._current_center,
                                                           self._current_expiry)
                    if tokens:
                        self._ws.subscribe(tokens)
                        logger.info("SubscriptionManager: re-subscribed %d strike tokens "
                                    "after reconnect (center=%d)",
                                    len(tokens), self._current_center)
        except Exception as e:
            logger.warning("SubscriptionManager: reconnect resync failed: %s", e)

    # ── Periodic refresh — called from bot_runner tick ──────────────────────

    def refresh(self, now_dt: Optional[datetime] = None) -> None:
        """Check if ATM has drifted; rotate strikes if so. Idempotent."""
        if not self._started:
            return
        try:
            spot = self._state.get_spot()
            if spot is None:
                logger.debug("SubscriptionManager: spot unknown — skipping refresh")
                return

            new_center = int(round(spot / STRIKE_STEP)) * STRIKE_STEP

            # Find current weekly expiry
            from data.angel_fetcher import AngelFetcher
            af = AngelFetcher.get()
            expiry = af.nearest_weekly_expiry()

            with self._lock:
                # Already at this center for this expiry? Nothing to do.
                if (self._current_center is not None
                        and self._current_expiry == expiry
                        and abs(new_center - self._current_center) < DRIFT_THRESHOLD):
                    return

                old_center = self._current_center
                old_expiry = self._current_expiry

                # Resolve new tokens FIRST (so we don't go blind if lookup fails)
                new_tokens = self._resolve_strike_tokens(new_center, expiry)
                if not new_tokens:
                    logger.warning("SubscriptionManager: no tokens resolved for "
                                    "center=%d expiry=%s — keeping current subs",
                                    new_center, expiry)
                    return

                # Subscribe NEW first, then unsubscribe OLD (no blind window)
                self._ws.subscribe(new_tokens)
                for (exch, token, strike, side) in self._tokens_in_state_around(new_center, expiry):
                    self._state.register_option(exch, token, strike, side)

                if old_center is not None and old_expiry is not None and (
                    old_center != new_center or old_expiry != expiry
                ):
                    old_tokens = self._resolve_strike_tokens(old_center, old_expiry,
                                                               exclude_center=new_center)
                    if old_tokens:
                        self._ws.unsubscribe(old_tokens)
                        for (exch, token, _s, _t) in [(t[0], t[1], None, None) for t in old_tokens]:
                            # Only unregister strikes that aren't in the new set
                            pass   # we keep state for any strike still subscribed

                self._current_center = new_center
                self._current_expiry = expiry
                logger.info("SubscriptionManager: rotated to center=%d expiry=%s "
                            "(spot=%.2f, %d tokens subscribed)",
                            new_center, expiry, spot, len(new_tokens))
        except Exception as e:
            logger.warning("SubscriptionManager.refresh failed: %s", e)

    # ── Internals ──────────────────────────────────────────────────────────

    def _strikes_around(self, center: int) -> list:
        return [center + k * STRIKE_STEP
                for k in range(-STRIKES_AROUND_ATM, STRIKES_AROUND_ATM + 1)]

    def _tokens_in_state_around(self, center: int, expiry: date) -> list:
        """Resolve (exch, token, strike, side) tuples for registering market state.
        Different return shape from _resolve_strike_tokens which returns just
        (exch, token) for the WebSocket subscribe call."""
        from data.angel_fetcher import AngelFetcher
        af = AngelFetcher.get()
        results = []
        try:
            instruments = af._nfo_instruments()
            for strike in self._strikes_around(center):
                for side in ("CE", "PE"):
                    m = self._find_instrument(instruments, strike, side, expiry)
                    if m:
                        results.append(("NFO", str(m["token"]), strike, side))
        except Exception as e:
            logger.warning("SubscriptionManager: token resolution failed: %s", e)
        return results

    def _resolve_strike_tokens(self, center: int, expiry: date,
                                exclude_center: Optional[int] = None) -> list:
        """Resolve (exchange, token) tuples for subscribing to WS."""
        from data.angel_fetcher import AngelFetcher
        af = AngelFetcher.get()
        results = []
        try:
            instruments = af._nfo_instruments()
            exclude_strikes = set()
            if exclude_center is not None:
                exclude_strikes = set(self._strikes_around(exclude_center))
            for strike in self._strikes_around(center):
                if strike in exclude_strikes:
                    continue   # already subscribed under the new center
                for side in ("CE", "PE"):
                    m = self._find_instrument(instruments, strike, side, expiry)
                    if m:
                        results.append(("NFO", str(m["token"])))
        except Exception as e:
            logger.warning("SubscriptionManager: token resolution failed: %s", e)
        return results

    def _find_instrument(self, instruments: list, strike: int,
                          side: str, expiry: date):
        """Match an instrument row by name/strike/side/expiry."""
        from data.angel_fetcher import _parse_expiry
        # Angel master stores strike × 100
        for i in instruments:
            try:
                if i.get("name") != "NIFTY":
                    continue
                if i.get("instrumenttype") != "OPTIDX":
                    continue
                if int(float(i.get("strike", 0))) // 100 != strike:
                    continue
                if not i.get("symbol", "").endswith(side):
                    continue
                exp_parsed = _parse_expiry(i.get("expiry", ""))
                if exp_parsed != expiry:
                    continue
                return i
            except Exception:
                continue
        return None

    def diagnostics(self) -> dict:
        with self._lock:
            return {
                "started":         self._started,
                "current_center":  self._current_center,
                "current_expiry":  self._current_expiry.isoformat() if self._current_expiry else None,
                "spot_registered": self._spot_registered,
            }


# ── Singleton ─────────────────────────────────────────────────────────────

_instance: Optional[SubscriptionManager] = None
_lock = threading.Lock()


def get_manager(ws_client=None, market_state=None) -> SubscriptionManager:
    global _instance
    with _lock:
        if _instance is None:
            if ws_client is None or market_state is None:
                raise RuntimeError("First call to get_manager must supply ws_client + market_state")
            _instance = SubscriptionManager(ws_client, market_state)
    return _instance
