"""
Delta India crypto broker — REST + WebSocket adapter
======================================================
Pattern mirrors core/broker.py (AngelOneBroker): one class with the methods
the bot_runner + strategies need, a module-level singleton + factory.

Read-only methods are unauthenticated (Delta REST public endpoints).
Authenticated methods (place_order, get_positions, get_balance) require
DELTA_API_KEY + DELTA_API_SECRET in env. HMAC-SHA256 signing per Delta docs.

Trade-mode dial:
  CRYPTO_TRADING_MODE = "paper" | "live"
    paper  → place_order writes to journal but doesn't hit Delta
    live   → place_order signs + POSTs to Delta /v2/orders

Cache TTLs mirror the AngelOne broker pattern to avoid rate limits.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from typing import Any, Optional
import requests

logger = logging.getLogger(__name__)

_BROKER_INSTANCE = None
_lock = threading.Lock()

# Cache TTLs (seconds)
_CHAIN_CACHE_TTL = 30
_PERP_CACHE_TTL  = 5
_POS_CACHE_TTL   = 15
_BAL_CACHE_TTL   = 15


class DeltaCryptoBroker:
    """Delta India crypto exchange adapter."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "",
        mode: str = "",
    ):
        self.api_key    = api_key or os.environ.get("DELTA_API_KEY", "").strip()
        self.api_secret = api_secret or os.environ.get("DELTA_API_SECRET", "").strip()
        self.base_url   = base_url or os.environ.get(
            "DELTA_BASE_URL", "https://api.india.delta.exchange"
        )
        self.mode = mode or os.environ.get("CRYPTO_TRADING_MODE", "paper")
        self._chain_cache: dict[str, dict] = {}
        self._perp_cache: dict[str, dict] = {}
        self._pos_cache: dict[str, dict] = {"data": None, "ts": 0.0}
        self._bal_cache: dict[str, float] = {"value": -1.0, "ts": 0.0}
        self._has_auth = bool(self.api_key and self.api_secret)
        if self.mode == "live" and not self._has_auth:
            raise RuntimeError(
                "DeltaCryptoBroker mode=live but DELTA_API_KEY/SECRET missing"
            )
        logger.info(
            "DeltaCryptoBroker init: base=%s mode=%s auth=%s",
            self.base_url, self.mode, self._has_auth,
        )

    # ── HTTP plumbing ────────────────────────────────────────────────────────
    def _sign(self, method: str, path: str, query: str, body: str, ts: str) -> dict:
        msg = method + ts + path + query + body
        sig = hmac.new(self.api_secret.encode(), msg.encode(),
                       hashlib.sha256).hexdigest()
        return {
            "api-key": self.api_key,
            "signature": sig,
            "timestamp": ts,
            "User-Agent": "tgc-bot-python/1.0",
        }

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None, authed: bool = False) -> dict:
        url = self.base_url + path
        headers: dict[str, str] = {}
        body_str = ""
        if body is not None:
            import json
            body_str = json.dumps(body, separators=(",", ":"))
            headers["Content-Type"] = "application/json"
        if authed:
            if not self._has_auth:
                raise RuntimeError("authed request needs DELTA_API_KEY/SECRET")
            ts = str(int(time.time()))
            query = "?" + requests.compat.urlencode(params) if params else ""
            headers.update(self._sign(method, path, query, body_str, ts))
        for attempt in range(3):
            try:
                r = requests.request(
                    method, url, params=params,
                    data=body_str if body_str else None,
                    headers=headers, timeout=15,
                )
                if r.status_code == 429:
                    wait = int(r.headers.get("X-RATE-LIMIT-RESET", 3))
                    logger.warning("delta rate-limit, sleep %ds", wait)
                    time.sleep(wait); continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                if attempt == 2:
                    logger.error("delta %s %s failed: %s", method, path, e)
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError("unreachable")

    # ── Read-only / public ──────────────────────────────────────────────────
    def get_perp_mark(self, symbol: str) -> Optional[float]:
        """Current perp mark. Prefers live WS stream; falls back to REST cache."""
        try:
            from core.ws.delta_stream import get_stream
            stream_mark = get_stream().get_perp_mark(symbol)
            if stream_mark is not None:
                return stream_mark
        except Exception as e:
            logger.debug("stream perp lookup failed (%s); falling back to REST", e)
        cached = self._perp_cache.get(symbol)
        if cached and time.time() - cached["ts"] < _PERP_CACHE_TTL:
            return cached["mark"]
        try:
            data = self._request("GET", "/v2/tickers",
                                  params={"contract_types": "perpetual_futures"})
            for row in data.get("result", []):
                if row.get("symbol") == symbol:
                    mark = float(row["mark_price"])
                    self._perp_cache[symbol] = {"mark": mark, "ts": time.time()}
                    return mark
        except Exception as e:
            logger.error("get_perp_mark(%s): %s", symbol, e)
        return None

    def get_option_chain(self, underlying: str) -> list[dict]:
        """All call+put options for an underlying. Returns normalized list.

        Prefers live WS stream when it has a usable chain (>=6 fresh marks);
        falls back to REST cache otherwise. v5 strategy only consumes the
        {symbol, mark} fields, so the stream-built chain is feature-complete.
        """
        try:
            from core.ws.delta_stream import get_stream
            stream_chain = get_stream().get_option_chain(underlying)
            if len(stream_chain) >= 6:
                return stream_chain
        except Exception as e:
            logger.debug("stream chain lookup failed (%s); falling back to REST", e)
        cached = self._chain_cache.get(underlying)
        if cached and time.time() - cached["ts"] < _CHAIN_CACHE_TTL:
            return cached["chain"]
        try:
            data = self._request(
                "GET", "/v2/tickers",
                params={"contract_types": "call_options,put_options",
                        "underlying_asset_symbols": underlying},
            )
            chain = []
            for o in data.get("result", []):
                sym = o.get("symbol", "")
                try:
                    mark = float(o.get("mark_price") or 0)
                except (TypeError, ValueError):
                    mark = 0
                if mark <= 0: continue
                chain.append({
                    "symbol": sym,
                    "mark":   mark,
                    "oi":     o.get("oi"),
                    "mark_iv": o.get("mark_vol") or o.get("mark_iv"),
                    "greeks": o.get("greeks", {}),
                    "strike_price": o.get("strike_price"),
                    "contract_type": o.get("contract_type"),
                })
            self._chain_cache[underlying] = {"chain": chain, "ts": time.time()}
            return chain
        except Exception as e:
            logger.error("get_option_chain(%s): %s", underlying, e)
        return []

    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Current annualized funding rate for perp."""
        try:
            data = self._request("GET", "/v2/tickers",
                                  params={"contract_types": "perpetual_futures"})
            for row in data.get("result", []):
                if row.get("symbol") == symbol:
                    fr = row.get("funding_rate")
                    return float(fr) if fr is not None else None
        except Exception as e:
            logger.error("get_funding_rate(%s): %s", symbol, e)
        return None

    # ── Authenticated / live trading ────────────────────────────────────────
    def get_positions(self) -> list[dict]:
        """Current open positions (account-wide). Empty list in paper mode."""
        if self.mode == "paper":
            return []
        cached = self._pos_cache
        if cached["data"] is not None and time.time() - cached["ts"] < _POS_CACHE_TTL:
            return cached["data"]
        try:
            data = self._request("GET", "/v2/positions/margined", authed=True)
            positions = data.get("result", [])
            self._pos_cache = {"data": positions, "ts": time.time()}
            return positions
        except Exception as e:
            logger.error("get_positions: %s", e)
        return []

    def get_balance(self) -> Optional[float]:
        """Free USD balance in trading account, cached 15s.

        Returns Delta's `available_balance` field (balance minus margin
        already locked in open positions) when present, falling back to
        `balance`. Returns None in paper mode so the runner knows to use
        its env-configured equity unchanged.
        """
        if self.mode == "paper":
            return None
        cached = self._bal_cache
        if cached["value"] >= 0 and time.time() - cached["ts"] < _BAL_CACHE_TTL:
            return cached["value"]
        try:
            data = self._request("GET", "/v2/wallet/balances", authed=True)
            for row in data.get("result", []):
                if row.get("asset_symbol") != "USD":
                    continue
                # Prefer available_balance (already net of locked margin);
                # otherwise fall back to balance (which may overstate what
                # we can actually spend on a new entry).
                raw = row.get("available_balance")
                if raw is None or raw == "":
                    raw = row.get("balance", 0)
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    continue
                self._bal_cache = {"value": val, "ts": time.time()}
                return val
        except Exception as e:
            logger.error("get_balance: %s", e)
        return None

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a product before placing orders. No-op in paper mode."""
        if self.mode == "paper":
            return True
        try:
            resp = self._request("POST", "/v2/products/orders/leverage",
                                 body={"product_symbol": symbol, "leverage": leverage},
                                 authed=True)
            logger.info("leverage set: %s → %d×", symbol, leverage)
            return resp.get("success", False)
        except Exception as e:
            logger.warning("set_leverage(%s, %d) failed: %s", symbol, leverage, e)
            return False

    def place_order(
        self,
        symbol: str,
        side: str,           # "buy" | "sell"
        size: int,           # contracts
        order_type: str = "market_order",
        limit_price: Optional[float] = None,
        post_only: bool = False,
        reduce_only: bool = False,
        tag: str = "",
        leverage: Optional[int] = None,
    ) -> dict:
        """Place an order. Paper mode → journal write, no broker call."""
        if self.mode == "paper":
            mark = self.get_perp_mark(symbol)
            return {
                "ok": True,
                "paper": True,
                "symbol": symbol, "side": side, "size": size,
                "fill_price": mark, "tag": tag,
                "timestamp": int(time.time()),
            }
        if leverage is not None:
            self.set_leverage(symbol, leverage)
        body = {
            "product_symbol": symbol,
            "size": int(size),
            "side": side,
            "order_type": order_type,
            "post_only": post_only,
            "reduce_only": reduce_only,
        }
        if limit_price is not None:
            body["limit_price"] = str(limit_price)
        try:
            resp = self._request("POST", "/v2/orders", body=body, authed=True)
            logger.info("placed %s %d %s → %s", side, size, symbol,
                         resp.get("result", {}).get("id"))
            return {"ok": True, "paper": False, "response": resp}
        except Exception as e:
            logger.error("place_order failed: %s", e)
            return {"ok": False, "error": str(e)}

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        if self.mode == "paper": return True
        try:
            self._request("DELETE", f"/v2/orders/{order_id}",
                          params={"product_symbol": symbol}, authed=True)
            return True
        except Exception as e:
            logger.error("cancel_order(%s) failed: %s", order_id, e)
            return False


def get_broker() -> DeltaCryptoBroker:
    """Lazy module-level singleton — same pattern as core/broker.get_broker()."""
    global _BROKER_INSTANCE
    if _BROKER_INSTANCE is None:
        with _lock:
            if _BROKER_INSTANCE is None:
                _BROKER_INSTANCE = DeltaCryptoBroker()
    return _BROKER_INSTANCE
