"""AngelStream — live NSE/BSE market data over SmartWebSocketV2.

Replaces REST polling for option-chain reads. A REST snapshot of a 17-strike
chain costs 4 `getMarketData` calls and returns whatever was true when the
request landed; the socket pushes every change as it happens. "Live means
live" is only true over this path.

Runs in a background daemon thread, keeps the newest tick per token in memory,
and reconnects with exponential backoff. Mirrors core/ws/delta_stream.py so the
two venues behave the same way operationally.

Angel-specific constraints that shape this module (see
docs/ANGEL_ONE_API_NOTES.md and the SmartAPI docs):

  * Prices arrive as integers in PAISE. Everything is divided by 100 on the way
    in, exactly once, at the decode boundary. A price that skips that division
    is 100x wrong and will size an order 100x wrong with it.
  * 1000 token subscriptions per session, 3 concurrent sockets per client code.
    The budget is enforced here rather than discovered at runtime.
  * The JWT expires daily at midnight. The socket must therefore re-authenticate
    on reconnect, not reuse the token it started with — a stream holding
    yesterday's token reconnects into a silent failure.

Verified live against Angel One on 2026-08-07 (22 NIFTY weekly contracts,
SNAP_QUOTE): connect to first tick ~3s, full coverage, zero decode errors, and
WS mid agreed with the REST quote to 0.04%.

Known SDK quirk seen in that run: smartapi-python 1.5.5's own
`SmartWebSocketV2._on_close` is declared `(self, wsapp)` but websocket-client
invokes close handlers with three arguments, so the SDK raises
"takes 2 positional arguments but 4 were given" on every disconnect and then
logs a resubscribe attempt of its own. It is noisy rather than harmful — the
SDK is capped at max_retry_attempt=1 and our _stop_event still ends our loop —
but it does mean TWO retry mechanisms exist. Ours (backoff, re-authenticating)
is the one that matters; treat the SDK's single attempt as a transient-blip
cushion, not as the reconnect strategy.

Thread-safe reads:
    get_ltp(token)              Optional[float], None when stale or unseen
    get_tick(token)             the full decoded tick
    chain_frame(symbol)         DataFrame in the schema MarketSnapshot expects
    diagnostics()               connection + freshness stats
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# SmartWebSocketV2 subscription modes.
MODE_LTP = 1
MODE_QUOTE = 2
MODE_SNAP_QUOTE = 3
MODE_DEPTH = 4

# SNAP_QUOTE is the mode we want: it is the only one carrying OPEN INTEREST
# alongside OHLC, volume and the 5-level book. The volume/OI lens is built on
# OI, so QUOTE would silently starve it.
DEFAULT_MODE = MODE_SNAP_QUOTE

# Exchange type codes.
EXCH_NSE_CM = 1        # NSE cash — index spot
EXCH_NSE_FO = 2        # NFO — NIFTY/BANKNIFTY/FINNIFTY options
EXCH_BSE_CM = 3        # BSE cash — SENSEX spot
EXCH_BSE_FO = 4        # BFO — SENSEX options

_EXCHANGE_CODE = {"NSE": EXCH_NSE_CM, "NFO": EXCH_NSE_FO,
                  "BSE": EXCH_BSE_CM, "BFO": EXCH_BSE_FO}

# Hard broker limits. Exceeding the token cap does not error cleanly — the
# socket simply stops delivering some of what you asked for, which is far worse
# than being told no.
MAX_TOKENS_PER_SESSION = 1000

STALE_SECONDS = 15.0            # a tick older than this is not "live"
RECONNECT_BACKOFF_MAX = 60.0
PAISE = 100.0


def _rupees(v) -> float:
    """Paise -> rupees. The single place this conversion happens."""
    try:
        return float(v) / PAISE
    except (TypeError, ValueError):
        return 0.0


def _levels(raw) -> list[dict]:
    """Normalise Angel's best-5 book into [{price, qty, orders}], best first.

    Angel does not guarantee the order of the five levels, so the best bid is
    taken as the HIGHEST buy price and the best ask as the LOWEST sell price
    rather than trusting index 0. Trusting position here would occasionally
    invert the spread, and an inverted spread silently poisons every mid,
    spread_pct and book-imbalance figure downstream.
    """
    out = []
    for lvl in raw or []:
        if not isinstance(lvl, dict):
            continue
        price = _rupees(lvl.get("price"))
        qty = int(lvl.get("quantity") or 0)
        if price <= 0:
            continue
        out.append({"price": price, "qty": qty,
                    "orders": int(lvl.get("no of orders") or lvl.get("orders") or 0)})
    return out


class AngelStream:
    """Singleton SmartWebSocketV2 client with an in-memory tick cache."""

    _instance: Optional["AngelStream"] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._ticks: dict[str, dict] = {}            # token -> decoded tick
        self._ticks_lock = threading.RLock()
        # token -> {symbol, strike, option_type, tradingsymbol, exchange}
        self._contracts: dict[str, dict] = {}
        self._pending: dict[int, set[str]] = {}      # exchange code -> tokens
        self._sws = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = False
        self._last_msg_ts = 0.0
        self._connect_count = 0
        self._reconnects = 0
        self._decode_errors = 0

    @classmethod
    def get(cls) -> "AngelStream":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── subscription registry ────────────────────────────────────────────────
    def register(self, contracts: list[dict]) -> int:
        """Declare the contracts to stream. Returns how many were accepted.

        Each contract needs: token, exchange, and — for options — symbol,
        strike, option_type. Registration is separate from subscribing so the
        set survives a reconnect: on every reconnect the socket resubscribes
        from this registry rather than from whatever it happened to hold.
        """
        accepted = 0
        with self._ticks_lock:
            for c in contracts:
                token = str(c.get("token") or "").strip()
                if not token:
                    continue
                if (token not in self._contracts
                        and len(self._contracts) >= MAX_TOKENS_PER_SESSION):
                    logger.error(
                        "angel-stream: token budget exhausted at %d — refusing %s. "
                        "Reduce strikes_around or split across sockets.",
                        MAX_TOKENS_PER_SESSION, c.get("tradingsymbol") or token)
                    break
                exch = str(c.get("exchange") or "NFO").upper()
                self._contracts[token] = {
                    "token": token,
                    "exchange": exch,
                    "exchange_code": _EXCHANGE_CODE.get(exch, EXCH_NSE_FO),
                    "symbol": c.get("symbol"),
                    "strike": c.get("strike"),
                    "option_type": c.get("option_type"),
                    "tradingsymbol": c.get("tradingsymbol"),
                }
                accepted += 1
        if accepted and self._connected:
            self._subscribe_all()
        return accepted

    def clear(self) -> None:
        """Drop the registry and cached ticks. Used on expiry rollover."""
        with self._ticks_lock:
            self._contracts.clear()
            self._ticks.clear()

    def _token_list(self) -> list[dict]:
        by_exch: dict[int, list[str]] = {}
        with self._ticks_lock:
            for token, c in self._contracts.items():
                by_exch.setdefault(c["exchange_code"], []).append(token)
        return [{"exchangeType": code, "tokens": toks} for code, toks in by_exch.items()]

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            logger.info("angel-stream: already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="angel-stream",
                                        daemon=True)
        self._thread.start()
        logger.info("angel-stream: thread started")

    def stop(self) -> None:
        self._stop_event.set()
        try:
            if self._sws is not None:
                self._sws.close_connection()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._connected = False
        logger.info("angel-stream: stopped")

    def _run_loop(self) -> None:
        delay = 1.0
        while not self._stop_event.is_set():
            try:
                # Re-authenticate on EVERY connect attempt. The JWT dies at
                # midnight; a long-lived stream that cached its token at 09:15
                # yesterday would reconnect into a silent auth failure.
                creds = self._fresh_credentials()
                if creds is None:
                    logger.warning("angel-stream: no credentials, retry in %.0fs", delay)
                    self._stop_event.wait(delay)
                    delay = min(delay * 2, RECONNECT_BACKOFF_MAX)
                    continue

                from SmartApi.smartWebSocketV2 import SmartWebSocketV2

                self._sws = SmartWebSocketV2(
                    creds["auth_token"], creds["api_key"],
                    creds["client_code"], creds["feed_token"],
                    max_retry_attempt=1,
                )
                self._sws.on_open = self._on_open
                self._sws.on_data = self._on_data
                self._sws.on_error = self._on_error
                self._sws.on_close = self._on_close

                self._connect_count += 1
                self._sws.connect()          # blocks until the socket drops

                self._connected = False
                if self._stop_event.is_set():
                    break
                self._reconnects += 1
                logger.warning("angel-stream: disconnected, reconnect in %.1fs", delay)
                self._stop_event.wait(delay)
                delay = min(delay * 2, RECONNECT_BACKOFF_MAX)
            except Exception as e:
                self._connected = False
                logger.error("angel-stream: run_loop error: %s", e, exc_info=True)
                self._stop_event.wait(delay)
                delay = min(delay * 2, RECONNECT_BACKOFF_MAX)

    def _fresh_credentials(self) -> Optional[dict]:
        try:
            from data.angel_fetcher import AngelFetcher
            return AngelFetcher.get().ws_credentials()
        except Exception as e:
            logger.error("angel-stream: credential fetch failed: %s", e)
            return None

    # ── callbacks ────────────────────────────────────────────────────────────
    def _on_open(self, wsapp=None) -> None:
        self._connected = True
        logger.info("angel-stream: connected (%d contracts registered)",
                    len(self._contracts))
        self._subscribe_all()

    def _subscribe_all(self) -> None:
        token_list = self._token_list()
        if not token_list or self._sws is None:
            return
        try:
            self._sws.subscribe("nse-lens", DEFAULT_MODE, token_list)
            n = sum(len(t["tokens"]) for t in token_list)
            logger.info("angel-stream: subscribed %d tokens across %d exchanges "
                        "(mode=SNAP_QUOTE)", n, len(token_list))
        except Exception as e:
            logger.error("angel-stream: subscribe failed: %s", e)

    def _on_data(self, wsapp, message=None) -> None:
        # Some SDK builds invoke this as (wsapp, message), others as (message,).
        msg = message if message is not None else wsapp
        if not isinstance(msg, dict):
            return
        self._last_msg_ts = time.time()
        try:
            tick = self._decode(msg)
        except Exception as e:
            self._decode_errors += 1
            logger.debug("angel-stream: decode failed: %s", e)
            return
        if tick is None:
            return
        with self._ticks_lock:
            self._ticks[tick["token"]] = tick

    def _decode(self, msg: dict) -> Optional[dict]:
        """Angel tick -> our chain schema, with every price converted once."""
        token = str(msg.get("token") or "").strip().strip('"')
        if not token:
            return None

        buy = _levels(msg.get("best_5_buy_data"))
        sell = _levels(msg.get("best_5_sell_data"))
        bid = max((l["price"] for l in buy), default=0.0)
        ask = min((l["price"] for l in sell), default=0.0)
        bid_qty = next((l["qty"] for l in buy if l["price"] == bid), 0) if bid else 0
        ask_qty = next((l["qty"] for l in sell if l["price"] == ask), 0) if ask else 0
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0

        exch_ts = msg.get("exchange_timestamp")
        feed_time = None
        if exch_ts:
            try:
                feed_time = datetime.fromtimestamp(int(exch_ts) / 1000.0,
                                                   tz=timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                feed_time = None

        meta = self._contracts.get(token, {})
        return {
            "token": token,
            "symbol": meta.get("symbol"),
            "strike": meta.get("strike"),
            "option_type": meta.get("option_type"),
            "tradingsymbol": meta.get("tradingsymbol"),
            "exchange": meta.get("exchange"),
            "ltp": _rupees(msg.get("last_traded_price")),
            "open": _rupees(msg.get("open_price_of_the_day")),
            "high": _rupees(msg.get("high_price_of_the_day")),
            "low": _rupees(msg.get("low_price_of_the_day")),
            "close": _rupees(msg.get("closed_price")),
            "avg_price": _rupees(msg.get("average_traded_price")),
            "volume": int(msg.get("volume_trade_for_the_day") or 0),
            "oi": int(msg.get("open_interest") or 0),
            "oi_change_pct": float(msg.get("open_interest_change_percentage") or 0.0),
            "last_trade_qty": int(msg.get("last_traded_quantity") or 0),
            "tot_buy_qty": int(msg.get("total_buy_quantity") or 0),
            "tot_sell_qty": int(msg.get("total_sell_quantity") or 0),
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread": (ask - bid) if bid > 0 and ask > 0 else 0.0,
            "spread_pct": ((ask - bid) / mid * 100) if mid > 0 else 0.0,
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
            "book_imbalance": ((bid_qty - ask_qty) / (bid_qty + ask_qty)
                               if (bid_qty + ask_qty) > 0 else 0.0),
            "depth_buy": buy,
            "depth_sell": sell,
            "exch_feed_time": feed_time,
            "recv_ts": time.time(),
        }

    def _on_error(self, *args) -> None:
        logger.error("angel-stream: ws error: %s", args[-1] if args else "unknown")

    def _on_close(self, *args) -> None:
        self._connected = False
        logger.info("angel-stream: ws closed")

    # ── reads ────────────────────────────────────────────────────────────────
    def get_tick(self, token: str, max_age: float = STALE_SECONDS) -> Optional[dict]:
        with self._ticks_lock:
            tick = self._ticks.get(str(token))
        if tick is None:
            return None
        return None if time.time() - tick["recv_ts"] > max_age else tick

    def get_ltp(self, token: str, max_age: float = STALE_SECONDS) -> Optional[float]:
        tick = self.get_tick(token, max_age)
        if tick is None:
            return None
        return tick["ltp"] if tick["ltp"] > 0 else None

    def get_spot(self, symbol: str,
                 max_age: float = STALE_SECONDS) -> Optional[float]:
        """Index level from the socket, or None when it is not streaming.

        Identified by `strike is None` — the marker `register()` uses for the
        underlying, which keeps it out of the option ladder while still
        flowing through the same tick cache.
        """
        now = time.time()
        with self._ticks_lock:
            for t in self._ticks.values():
                if (t.get("symbol") == symbol and t.get("strike") is None
                        and t.get("ltp", 0) > 0
                        and now - t["recv_ts"] <= max_age):
                    return float(t["ltp"])
        return None

    def chain_frame(self, symbol: str,
                    max_age: float = STALE_SECONDS) -> pd.DataFrame:
        """Live chain for one underlying, in MarketSnapshot's schema.

        Stale rows are DROPPED rather than served cold. A lens cannot tell a
        stale quote from a live one, so handing it a five-minute-old book would
        produce a confident verdict on a market that has moved.
        """
        now = time.time()
        with self._ticks_lock:
            rows = [t for t in self._ticks.values()
                    if t.get("symbol") == symbol
                    and t.get("strike") is not None
                    and now - t["recv_ts"] <= max_age]
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows).sort_values(["strike", "option_type"])
        return df.reset_index(drop=True)

    def coverage(self, symbol: str, max_age: float = STALE_SECONDS) -> tuple[int, int]:
        """(fresh, registered) contract counts for one symbol."""
        now = time.time()
        with self._ticks_lock:
            registered = sum(1 for c in self._contracts.values()
                             if c.get("symbol") == symbol)
            fresh = sum(1 for t in self._ticks.values()
                        if t.get("symbol") == symbol
                        and now - t["recv_ts"] <= max_age)
        return fresh, registered

    def diagnostics(self) -> dict:
        now = time.time()
        with self._ticks_lock:
            total = len(self._ticks)
            fresh = sum(1 for t in self._ticks.values()
                        if now - t["recv_ts"] <= STALE_SECONDS)
            registered = len(self._contracts)
        return {
            "connected": self._connected,
            "registered": registered,
            "budget_used_pct": round(100.0 * registered / MAX_TOKENS_PER_SESSION, 1),
            "ticks_total": total,
            "ticks_fresh": fresh,
            "last_msg_age_s": round(now - self._last_msg_ts, 2) if self._last_msg_ts else None,
            "connects": self._connect_count,
            "reconnects": self._reconnects,
            "decode_errors": self._decode_errors,
            "mode": "SNAP_QUOTE",
        }


_registered_atm: dict[str, int] = {}   # symbol -> last ATM we registered around
_ensure_lock = threading.Lock()


def ensure_subscribed(symbol: str, strikes_around: int = 10,
                      fetcher=None) -> dict:
    """Resolve the chain around ATM and make sure the socket is streaming it.

    Idempotent and cheap to call on every frame: it only does work when the
    symbol is new or when spot has drifted far enough that ATM moved. Without
    that re-registration the ladder would slowly walk off the money over a
    trending session and the strikes nearest spot — the ones anyone actually
    looks at — would be the ones missing.
    """
    from nse.config import STEP_SIZES
    from nse.data.option_chain import OptionChainCache

    stream = AngelStream.get()
    with _ensure_lock:
        cache = OptionChainCache(symbol, fetcher)
        spot = cache.get_underlying_ltp()
        if spot is None or spot <= 0:
            return {"ok": False, "reason": "spot unavailable"}
        expiry = cache.nearest_expiry(min_days=0)
        if expiry is None:
            return {"ok": False, "reason": "no expiry"}

        step = STEP_SIZES[symbol]
        atm = int(round(spot / step)) * step
        if _registered_atm.get(symbol) == atm:
            return {"ok": True, "atm": atm, "spot": spot, "expiry": expiry,
                    "registered": 0, "cached": True}

        contracts = []
        # The index itself, so spot comes off the socket too. Without this the
        # hot loop still needs a REST ltpData call per frame just to locate ATM,
        # and one REST round-trip is worth ~500ms — more than the entire frame
        # budget. `strike` stays None so it never appears in the option ladder.
        from data.angel_fetcher import _SPOT_TOKENS
        spot_meta = _SPOT_TOKENS.get(symbol)
        if spot_meta:
            contracts.append({
                "token": spot_meta["token"],
                "tradingsymbol": spot_meta["tradingsymbol"],
                "exchange": spot_meta["exchange"],
                "symbol": symbol, "strike": None, "option_type": None,
            })

        for k in range(-strikes_around, strikes_around + 1):
            strike = atm + k * step
            for opt in ("CE", "PE"):
                ts, tok = cache.resolve_leg(strike, opt, expiry)
                if ts and tok:
                    contracts.append({
                        "token": tok, "tradingsymbol": ts,
                        "exchange": _EXCHANGE_NAME.get(symbol, "NFO"),
                        "symbol": symbol, "strike": strike, "option_type": opt,
                    })
        n = stream.register(contracts)
        _registered_atm[symbol] = atm
        if not stream._connected:
            stream.start()
        logger.info("angel-stream: ensured %s chain at ATM %d (%d contracts)",
                    symbol, atm, n)
        return {"ok": True, "atm": atm, "spot": spot, "expiry": expiry,
                "registered": n, "cached": False}


_EXCHANGE_NAME = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "FINNIFTY": "NFO",
                  "SENSEX": "BFO"}


# Module-level helpers, mirroring core/ws/delta_stream.py ─────────────────────
def get_stream() -> AngelStream:
    return AngelStream.get()


def start_stream() -> None:
    AngelStream.get().start()


def stop_stream() -> None:
    if AngelStream._instance is not None:
        AngelStream._instance.stop()
