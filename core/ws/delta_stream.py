"""
DeltaStream — persistent WebSocket connection to Delta India.

Replaces REST polling for perp marks. Runs in a background daemon thread,
maintains in-memory dict of latest marks per symbol. Auto-reconnects on drop
with exponential backoff. Option subscriptions were removed when the short
straddle strategy was deleted.

Thread-safe reads:
    get_perp_mark(symbol)           Optional[float], None if stale/missing
    diagnostics()                   connection + cache stats

Lifecycle:
    start_stream()                  begin background WS connection
    stop_stream()                   close connection, stop thread

Symbol discovery returns the hardcoded perp symbols.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

import websocket

logger = logging.getLogger(__name__)

DELTA_WS_URL = os.environ.get("DELTA_WS_URL", "wss://socket.india.delta.exchange")
DELTA_REST   = os.environ.get("DELTA_BASE_URL", "https://api.india.delta.exchange")

# ETH-only config: only subscribe to ETH perp. Options subscriptions were
# removed when the ETH short straddle strategy was deleted in July 2026.
# Re-add "BTC" here and in PERP_SYMBOLS if BTC trading is re-enabled.
UNDERLYINGS  = ("ETH",)
PERP_SYMBOLS = ("ETHUSD",)

REDISCOVER_SECONDS    = 3600       # refresh symbol list every 1h
STALE_SECONDS         = 60         # mark considered stale after 60s no update
RECONNECT_BACKOFF_MAX = 60         # cap reconnect delay at 60s
SUBSCRIBE_CHUNK_SIZE  = 200        # symbols per subscribe message


class DeltaStream:
    _instance: Optional["DeltaStream"] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        # marks[symbol] = (price, ts)
        self._marks: dict[str, tuple[float, float]] = {}
        self._marks_lock = threading.RLock()
        self._symbols_last_refresh: float = 0.0
        self._subscribed: set[str] = set()
        # connection
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected: bool = False
        self._last_msg_ts: float = 0.0

    @classmethod
    def get(cls) -> "DeltaStream":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── symbol discovery ─────────────────────────────────────────────────────
    def _discover_symbols(self) -> list[str]:
        """Return perp symbols to subscribe. Options subscriptions were removed
        when the short straddle strategy was deleted."""
        self._symbols_last_refresh = time.time()
        return list(PERP_SYMBOLS)

    # ── ws lifecycle ─────────────────────────────────────────────────────────
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            logger.info("delta-stream: already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="delta-stream", daemon=True,
        )
        self._thread.start()
        logger.info("delta-stream: thread started")

    def stop(self):
        self._stop_event.set()
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("delta-stream: stopped")

    def _run_loop(self):
        delay = 1.0
        while not self._stop_event.is_set():
            try:
                # Trigger discovery if it's been REDISCOVER_SECONDS or we have
                # no subscription yet.
                stale = time.time() - self._symbols_last_refresh > REDISCOVER_SECONDS
                if stale or not self._subscribed:
                    discovered = self._discover_symbols()
                    self._subscribed = set(discovered)

                self._ws = websocket.WebSocketApp(
                    DELTA_WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
                self._connected = False
                if self._stop_event.is_set():
                    break
                logger.warning("delta-stream: disconnected, reconnect in %.1fs",
                               delay)
                self._stop_event.wait(delay)
                delay = min(delay * 2, RECONNECT_BACKOFF_MAX)
            except Exception as e:
                logger.error("delta-stream: run_loop error: %s", e, exc_info=True)
                self._stop_event.wait(delay)
                delay = min(delay * 2, RECONNECT_BACKOFF_MAX)
            else:
                # successful connection — reset backoff on next iteration
                if self._connected:
                    delay = 1.0

    # ── ws callbacks ─────────────────────────────────────────────────────────
    def _on_open(self, ws):
        self._connected = True
        logger.info("delta-stream: connected, subscribing %d symbols",
                    len(self._subscribed))
        # mark_price channel uses MARK:<symbol> format on Delta India
        mark_syms = [f"MARK:{s}" for s in self._subscribed]
        for chunk in _chunks(mark_syms, SUBSCRIBE_CHUNK_SIZE):
            msg = {
                "type": "subscribe",
                "payload": {
                    "channels": [
                        {"name": "mark_price", "symbols": chunk},
                    ],
                },
            }
            try:
                ws.send(json.dumps(msg))
            except Exception as e:
                logger.error("delta-stream: subscribe send failed: %s", e)
                return

    def _on_message(self, ws, raw):
        self._last_msg_ts = time.time()
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("type") != "mark_price":
            return
        sym = msg.get("symbol", "")
        if sym.startswith("MARK:"):
            sym = sym[5:]
        try:
            price = float(msg.get("price") or 0)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return
        with self._marks_lock:
            self._marks[sym] = (price, time.time())

    def _on_error(self, ws, error):
        logger.error("delta-stream: ws error: %s", error)

    def _on_close(self, ws, code, reason):
        self._connected = False
        logger.info("delta-stream: ws closed code=%s reason=%s", code, reason)

    # ── reads ────────────────────────────────────────────────────────────────
    def get_perp_mark(self, symbol: str,
                      max_age: float = STALE_SECONDS) -> Optional[float]:
        with self._marks_lock:
            entry = self._marks.get(symbol)
        if entry is None:
            return None
        mark, ts = entry
        if time.time() - ts > max_age:
            return None
        return mark


    def diagnostics(self) -> dict:
        with self._marks_lock:
            n_fresh = sum(
                1 for _, (_, ts) in self._marks.items()
                if time.time() - ts < STALE_SECONDS
            )
            n_total = len(self._marks)
        return {
            "connected": self._connected,
            "subscribed": len(self._subscribed),
            "marks_total": n_total,
            "marks_fresh": n_fresh,
            "last_msg_age_s": (time.time() - self._last_msg_ts
                               if self._last_msg_ts else None),
            "symbols_last_refresh_age_s": (time.time() - self._symbols_last_refresh
                                           if self._symbols_last_refresh else None),
        }


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# Module-level helpers ────────────────────────────────────────────────────────
def get_stream() -> DeltaStream:
    return DeltaStream.get()


def start_stream() -> None:
    DeltaStream.get().start()


def stop_stream() -> None:
    if DeltaStream._instance is not None:
        DeltaStream._instance.stop()
