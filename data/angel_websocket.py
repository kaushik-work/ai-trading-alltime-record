"""
Angel One SmartAPI WebSocket V2 client — tick-by-tick LTP + OI for our
subscribed option strikes + NIFTY spot.

Lifecycle:
  client = AngelWebSocketClient()
  client.on_tick(callback)         # callback(tick_dict) called per tick
  client.start()                   # starts background WS thread
  client.subscribe([("NFO", "12345"), ("NSE", "26000")])   # tuples of (exch, token)
  ...
  client.stop()

Design notes:
  • Runs the underlying SmartWebSocketV2.connect() in a daemon thread because
    .connect() blocks forever. Tick callbacks fire in that thread — callers
    must use locks/queues if their state isn't thread-safe.
  • Exposes a high-level subscribe(token_list) that batches into Angel's
    required {"exchangeType": int, "tokens": [str]} format.
  • Reconnect-on-disconnect with exponential backoff (1s → 60s cap).
  • Re-applies current subscriptions automatically after reconnect.
  • Token format: instrument tokens are strings ("26000" for NIFTY index,
    NFO option tokens come from AngelFetcher._nfo_instruments()).

Tick dict shape this client emits (normalised):
    {
        "exchange":     "NFO" | "NSE",
        "token":        "12345",
        "ltp":          152.30,            # in rupees (Angel sends paise)
        "oi":           1_234_567,         # integer contract count
        "ts_ms":        1716345678901,     # exchange feed time, epoch ms
        "received_at":  datetime.now(IST), # local clock when we got it
    }
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# Angel One exchange type codes
_EXCH_CODE = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5}
_EXCH_NAME = {v: k for k, v in _EXCH_CODE.items()}

# Subscription mode — SNAP_QUOTE gives LTP + OI + volume in one stream
_MODE_SNAP_QUOTE = 3


class AngelWebSocketClient:
    """Wraps SmartWebSocketV2 with reconnect, queued subscribe, and
    a normalised tick callback."""

    def __init__(self):
        self._ws = None
        self._ws_thread: Optional[threading.Thread] = None
        self._on_tick_cb: Optional[Callable[[dict], None]] = None
        self._on_connect_cb: Optional[Callable[[], None]] = None
        self._stop_requested = False

        # {(exch_code, token): True}  — set of all currently-active subs
        self._active_subs: dict = {}
        self._subs_lock = threading.Lock()

        # Reconnect state
        self._reconnect_attempt = 0
        self._connection_ready = threading.Event()

        # Diagnostics
        self._ticks_received = 0
        self._last_tick_at: Optional[datetime] = None
        self._connect_count = 0
        self._disconnect_count = 0

    # ── Public API ──────────────────────────────────────────────────────────

    def on_tick(self, cb: Callable[[dict], None]) -> None:
        """Register the tick handler. Called once per normalised tick."""
        self._on_tick_cb = cb

    def on_connect(self, cb: Callable[[], None]) -> None:
        """Register a callback fired each time WS finishes connecting (incl. reconnect)."""
        self._on_connect_cb = cb

    def start(self) -> None:
        """Start the WebSocket thread. Returns immediately."""
        if self._ws_thread and self._ws_thread.is_alive():
            logger.info("AngelWebSocket already running")
            return
        self._stop_requested = False
        self._ws_thread = threading.Thread(target=self._run_loop,
                                            name="angel-ws", daemon=True)
        self._ws_thread.start()
        logger.info("AngelWebSocket: background thread started")

    def stop(self) -> None:
        """Signal stop and close the connection."""
        self._stop_requested = True
        try:
            if self._ws is not None:
                self._ws.close_connection()
        except Exception as e:
            logger.debug("AngelWebSocket.stop close error: %s", e)
        self._connection_ready.clear()

    def is_connected(self) -> bool:
        return self._connection_ready.is_set()

    def subscribe(self, tokens: list) -> None:
        """tokens: list of (exchange_name, token_str) tuples, e.g.
           [("NFO", "55001"), ("NSE", "26000")]"""
        if not tokens:
            return
        with self._subs_lock:
            for exch_name, token in tokens:
                exch_code = _EXCH_CODE.get(exch_name)
                if exch_code is None:
                    logger.warning("Unknown exchange %s — skipped", exch_name)
                    continue
                self._active_subs[(exch_code, str(token))] = True

        if self._connection_ready.is_set():
            self._apply_subscriptions(tokens, "subscribe")

    def unsubscribe(self, tokens: list) -> None:
        """tokens: list of (exchange_name, token_str) tuples."""
        if not tokens:
            return
        with self._subs_lock:
            for exch_name, token in tokens:
                exch_code = _EXCH_CODE.get(exch_name)
                if exch_code is not None:
                    self._active_subs.pop((exch_code, str(token)), None)

        if self._connection_ready.is_set():
            self._apply_subscriptions(tokens, "unsubscribe")

    def active_subscriptions(self) -> list:
        with self._subs_lock:
            return [(_EXCH_NAME.get(e, "?"), t) for (e, t) in self._active_subs]

    def diagnostics(self) -> dict:
        return {
            "is_connected":      self.is_connected(),
            "active_subs_count": len(self._active_subs),
            "ticks_received":    self._ticks_received,
            "last_tick_at":      self._last_tick_at.isoformat() if self._last_tick_at else None,
            "connect_count":     self._connect_count,
            "disconnect_count":  self._disconnect_count,
            "reconnect_attempt": self._reconnect_attempt,
        }

    # ── Internal: connection loop ───────────────────────────────────────────

    def _run_loop(self) -> None:
        """Reconnect loop with exponential backoff (1s → 60s cap)."""
        backoff = 1.0
        while not self._stop_requested:
            try:
                self._connect_once()
                # On clean disconnect, reset backoff
                backoff = 1.0
            except Exception as e:
                logger.warning("AngelWebSocket: connect loop error: %s", e)

            if self._stop_requested:
                break

            self._reconnect_attempt += 1
            logger.warning("AngelWebSocket: reconnecting in %.1fs (attempt %d)",
                            backoff, self._reconnect_attempt)
            time.sleep(backoff)
            backoff = min(60.0, backoff * 2.0)

        logger.info("AngelWebSocket: run loop exited")

    def _connect_once(self) -> None:
        """One connection lifecycle. Returns when disconnected."""
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        # Pull credentials from the existing AngelFetcher session
        from data.angel_fetcher import AngelFetcher
        af = AngelFetcher.get()
        if not af._ensure_logged_in() or af._api is None:
            raise RuntimeError("AngelFetcher not logged in — cannot start WS")

        # SmartConnect stores tokens on the api instance after generateSession
        api = af._api
        auth_token = getattr(api, "access_token", None) or getattr(api, "jwt_token", None)
        feed_token = getattr(api, "feed_token", None)
        api_key    = getattr(api, "api_key", None)
        client_code = None
        # Client code comes from getProfile or saved config
        import config as _cfg
        client_code = _cfg.ANGEL_CLIENT_ID

        if not all([auth_token, feed_token, api_key, client_code]):
            raise RuntimeError(
                f"AngelWebSocket: missing creds — "
                f"auth={'OK' if auth_token else 'MISSING'} "
                f"feed={'OK' if feed_token else 'MISSING'} "
                f"key={'OK' if api_key else 'MISSING'} "
                f"client={'OK' if client_code else 'MISSING'}"
            )

        self._ws = SmartWebSocketV2(
            auth_token=auth_token,
            api_key=api_key,
            client_code=client_code,
            feed_token=feed_token,
            max_retry_attempt=3,
        )

        # Wire callbacks
        self._ws.on_open    = lambda wsapp: self._on_open(wsapp)
        self._ws.on_data    = lambda wsapp, data: self._on_data(wsapp, data)
        self._ws.on_close   = lambda wsapp: self._on_close(wsapp)
        self._ws.on_error   = self._on_error
        self._ws.on_message = lambda wsapp, msg: self._on_message(wsapp, msg)

        logger.info("AngelWebSocket: connecting ...")
        # connect() blocks until disconnect
        self._ws.connect()

    # ── Callbacks ───────────────────────────────────────────────────────────

    def _on_open(self, wsapp) -> None:
        self._connect_count += 1
        self._connection_ready.set()
        logger.info("AngelWebSocket: connected (#%d)", self._connect_count)
        # Re-apply current subscriptions
        with self._subs_lock:
            subs_to_restore = [
                (_EXCH_NAME.get(e, "?"), t) for (e, t) in self._active_subs
            ]
        if subs_to_restore:
            logger.info("AngelWebSocket: restoring %d subscriptions",
                        len(subs_to_restore))
            self._apply_subscriptions(subs_to_restore, "subscribe")
        if self._on_connect_cb:
            try:
                self._on_connect_cb()
            except Exception as e:
                logger.warning("AngelWebSocket: on_connect callback error: %s", e)

    def _on_close(self, wsapp) -> None:
        self._disconnect_count += 1
        self._connection_ready.clear()
        logger.warning("AngelWebSocket: connection closed (#%d)",
                        self._disconnect_count)

    def _on_error(self) -> None:
        logger.warning("AngelWebSocket: error reported by underlying client")

    def _on_message(self, wsapp, message) -> None:
        # Control / status messages (rare during steady-state)
        logger.debug("AngelWebSocket: control msg: %s", message)

    def _on_data(self, wsapp, data) -> None:
        """Normalise raw tick → emit to registered callback."""
        try:
            # Angel One sends LTP / volumes in paise — divide by 100
            ltp_raw = data.get("last_traded_price")
            ltp     = (ltp_raw / 100.0) if isinstance(ltp_raw, (int, float)) else None

            tick = {
                "exchange":     _EXCH_NAME.get(data.get("exchange_type"), "?"),
                "token":        str(data.get("token", "")),
                "ltp":          ltp,
                "oi":           int(data.get("open_interest") or 0),
                "ts_ms":        data.get("exchange_feed_time_epoch_millis") or
                                data.get("exchange_timestamp") or
                                int(time.time() * 1000),
                "received_at":  datetime.now(IST),
            }
            self._ticks_received += 1
            self._last_tick_at = tick["received_at"]
            if self._on_tick_cb:
                self._on_tick_cb(tick)
        except Exception as e:
            logger.warning("AngelWebSocket: on_data error: %s", e)

    # ── Subscription helper ─────────────────────────────────────────────────

    def _apply_subscriptions(self, tokens: list, action: str) -> None:
        """Send a subscribe/unsubscribe to the live WS."""
        if self._ws is None:
            return
        # Group by exchange code
        groups: dict = defaultdict(list)
        for exch_name, token in tokens:
            exch_code = _EXCH_CODE.get(exch_name)
            if exch_code is None:
                continue
            groups[exch_code].append(str(token))

        token_list = [{"exchangeType": code, "tokens": toks}
                      for code, toks in groups.items()]
        if not token_list:
            return
        corr_id = f"shadow-bot-{int(time.time())}"
        try:
            if action == "subscribe":
                self._ws.subscribe(corr_id, _MODE_SNAP_QUOTE, token_list)
                logger.info("AngelWebSocket: subscribed %d tokens across %d exchanges",
                            sum(len(t["tokens"]) for t in token_list), len(token_list))
            else:
                self._ws.unsubscribe(corr_id, _MODE_SNAP_QUOTE, token_list)
                logger.info("AngelWebSocket: unsubscribed %d tokens",
                            sum(len(t["tokens"]) for t in token_list))
        except Exception as e:
            logger.warning("AngelWebSocket: %s failed: %s", action, e)


# ── Singleton accessor ─────────────────────────────────────────────────────

_client_instance: Optional[AngelWebSocketClient] = None
_singleton_lock = threading.Lock()


def get_client() -> AngelWebSocketClient:
    """Process-wide singleton."""
    global _client_instance
    with _singleton_lock:
        if _client_instance is None:
            _client_instance = AngelWebSocketClient()
    return _client_instance
