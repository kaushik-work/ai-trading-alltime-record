"""
DeltaStream — persistent WebSocket connection to Delta India.

Replaces REST polling for marks (perp + options). Runs in a background
daemon thread, maintains in-memory dict of latest marks per symbol.
Auto-reconnects on drop with exponential backoff.

Thread-safe reads:
    get_perp_mark(symbol)           Optional[float], None if stale/missing
    get_option_chain(underlying)    list[{symbol, mark}] for fresh options
    diagnostics()                   connection + cache stats

Lifecycle:
    start_stream()                  begin background WS connection
    stop_stream()                   close connection, stop thread

Symbol discovery is REST-driven (one call at start, refreshed every 4h)
so we pick up new daily expiries automatically.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

from datetime import datetime, timezone
import re

import requests
import websocket

logger = logging.getLogger(__name__)

DELTA_WS_URL = os.environ.get("DELTA_WS_URL", "wss://socket.india.delta.exchange")
DELTA_REST   = os.environ.get("DELTA_BASE_URL", "https://api.india.delta.exchange")

UNDERLYINGS  = ("BTC", "ETH")
PERP_SYMBOLS = ("BTCUSD", "ETHUSD")

REDISCOVER_SECONDS    = 3600       # refresh option symbol list every 1h (was 4h)
STALE_SECONDS         = 60         # mark considered stale after 60s no update
RECONNECT_BACKOFF_MAX = 60         # cap reconnect delay at 60s
SUBSCRIBE_CHUNK_SIZE  = 200        # symbols per subscribe message

# Subscription filter: only subscribe to near-money strikes so the WS
# message rate stays tractable on a 1 vCPU box. Strategy uses ±5% strikes
# for signal compute; we subscribe to ±7 strikes around spot plus the ATM
# strike, both Call and Put sides — that's ~15 strikes per expiry,
# typically wider than ±5% in dollar terms so the strategy always has
# enough corroborating data even after spot drift between rediscoveries.
SUB_STRIKES_BELOW   = 7   # ITM-for-call / OTM-for-put count
SUB_STRIKES_ABOVE   = 7   # OTM-for-call / ITM-for-put count
SUB_MAX_TTE_HOURS   = 96  # only subscribe to expiries within 4 days

_SYMBOL_RE = re.compile(r"^([CP])-([A-Z]+)-(\d+)-(\d{6})$")


class DeltaStream:
    _instance: Optional["DeltaStream"] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        # marks[symbol] = (price, ts)
        self._marks: dict[str, tuple[float, float]] = {}
        self._marks_lock = threading.RLock()
        # underlying -> [option_symbol, ...]
        self._option_symbols: dict[str, list[str]] = {}
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
    def _fetch_spots(self) -> dict[str, float]:
        """One REST call returning current perp spot for both underlyings."""
        out: dict[str, float] = {}
        try:
            r = requests.get(
                f"{DELTA_REST}/v2/tickers",
                params={"contract_types": "perpetual_futures"},
                timeout=10,
            )
            r.raise_for_status()
            for t in r.json().get("result", []):
                sym = t.get("symbol", "")
                for u in UNDERLYINGS:
                    if sym == f"{u}USD":
                        try: out[u] = float(t["mark_price"])
                        except (KeyError, TypeError, ValueError): pass
        except Exception as e:
            logger.warning("delta-stream: fetch_spots failed: %s", e)
        return out

    def _filter_near_money(
        self, all_symbols: list[str], underlying: str, spot: float,
    ) -> list[str]:
        """Pick ~15 strikes per expiry: 7 below spot + 7 above + ATM, both
        Call and Put. Skips expiries beyond SUB_MAX_TTE_HOURS."""
        now = datetime.now(timezone.utc)
        # by_expiry[expiry] = {"C": {strike: sym}, "P": {strike: sym}}
        by_expiry: dict[datetime, dict[str, dict[int, str]]] = {}
        for sym in all_symbols:
            m = _SYMBOL_RE.match(sym)
            if not m: continue
            side, asset, strike_s, ddmmyy = m.group(1), m.group(2), m.group(3), m.group(4)
            if asset != underlying: continue
            try:
                strike = int(strike_s)
                dd, mm, yy = int(ddmmyy[:2]), int(ddmmyy[2:4]), int(ddmmyy[4:6])
                expiry = datetime(2000 + yy, mm, dd, 12, 0, tzinfo=timezone.utc)
            except Exception:
                continue
            tte_h = (expiry - now).total_seconds() / 3600
            if not (0 < tte_h <= SUB_MAX_TTE_HOURS): continue
            by_expiry.setdefault(expiry, {"C": {}, "P": {}})[side][strike] = sym

        selected: list[str] = []
        for expiry, sides in by_expiry.items():
            strikes = sorted(set(sides["C"]) | set(sides["P"]))
            if not strikes: continue
            below = [k for k in strikes if k < spot][-SUB_STRIKES_BELOW:]
            above = [k for k in strikes if k > spot][:SUB_STRIKES_ABOVE]
            atm   = min(strikes, key=lambda k: abs(k - spot))
            keep  = set(below) | set(above) | {atm}
            for k in keep:
                for side in ("C", "P"):
                    if k in sides[side]:
                        selected.append(sides[side][k])
        return selected

    def _discover_symbols(self) -> list[str]:
        """Fetch option symbols per underlying, filter to near-money strikes,
        and return the subscription set. Retries each underlying up to 3x
        with exponential backoff. If any underlying fails ALL retries the
        refresh timestamp stays un-bumped so the run_loop will re-attempt."""
        out: list[str] = list(PERP_SYMBOLS)
        all_ok = True
        spots = self._fetch_spots()
        for underlying in UNDERLYINGS:
            spot = spots.get(underlying)
            ok = False
            for attempt in range(3):
                try:
                    r = requests.get(
                        f"{DELTA_REST}/v2/tickers",
                        params={"contract_types": "call_options,put_options",
                                "underlying_asset_symbols": underlying},
                        timeout=10,
                    )
                    r.raise_for_status()
                    all_syms = [t["symbol"] for t in r.json().get("result", [])
                                if t.get("symbol")]
                    # Filter to near-money if we have a fresh spot; otherwise
                    # subscribe to everything (safer than missing strikes).
                    if spot is not None and spot > 0:
                        syms = self._filter_near_money(all_syms, underlying, spot)
                        logger.info("delta-stream: %s spot=%.0f, selected "
                                    "%d/%d near-money option symbols",
                                    underlying, spot, len(syms), len(all_syms))
                    else:
                        syms = all_syms
                        logger.warning("delta-stream: %s spot unavailable, "
                                       "subscribing to all %d symbols (degraded)",
                                       underlying, len(syms))
                    self._option_symbols[underlying] = syms
                    out.extend(syms)
                    ok = True
                    break
                except Exception as e:
                    if attempt < 2:
                        wait = 2 ** attempt
                        logger.warning("delta-stream: discover %s attempt %d/3 "
                                       "failed (%s), retry in %ds",
                                       underlying, attempt + 1, e, wait)
                        time.sleep(wait)
                    else:
                        logger.error("delta-stream: discover failed for %s "
                                     "after 3 attempts: %s", underlying, e)
            if not ok:
                all_ok = False
        if all_ok:
            self._symbols_last_refresh = time.time()
        return out

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
                # Trigger discovery if: (a) it's been REDISCOVER_SECONDS,
                # (b) we have no subscription yet, or (c) any underlying
                # came back empty — that's a partial failure we want to
                # heal on the next loop iteration, not wait 4h to retry.
                stale   = time.time() - self._symbols_last_refresh > REDISCOVER_SECONDS
                missing = any(not self._option_symbols.get(u) for u in UNDERLYINGS)
                if stale or missing or not self._subscribed:
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

    def get_option_chain(self, underlying: str,
                         max_age: float = STALE_SECONDS) -> list[dict]:
        """Return [{symbol, mark}] for fresh options on this underlying."""
        syms = self._option_symbols.get(underlying, [])
        now = time.time()
        out: list[dict] = []
        with self._marks_lock:
            for s in syms:
                entry = self._marks.get(s)
                if entry is None:
                    continue
                mark, ts = entry
                if now - ts > max_age:
                    continue
                out.append({"symbol": s, "mark": mark})
        return out

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
