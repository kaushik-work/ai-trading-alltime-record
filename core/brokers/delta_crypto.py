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
_FUTS_CACHE_TTL  = 30


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
        if mode:
            self.mode = mode
        else:
            from core.risk_management import CRYPTO_TRADING_MODE
            self.mode = CRYPTO_TRADING_MODE
        self._chain_cache: dict[str, dict] = {}
        self._perp_cache: dict[str, dict] = {}
        self._pos_cache: dict[str, dict] = {"data": None, "ts": 0.0}
        self._bal_cache: dict[str, float] = {"value": -1.0, "ts": 0.0}
        self._futs_cache: dict = {"data": None, "ts": 0.0}
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
                # 4xx auth/scope errors don't recover with retry -- raise now
                # so the caller can show a meaningful message and stop hammering.
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    r.raise_for_status()
                r.raise_for_status()
                return r.json()
            except requests.HTTPError as e:
                if e.response is not None and 400 <= e.response.status_code < 500:
                    # Don't spam — caller will log the meaningful error.
                    raise
                if attempt == 2:
                    logger.error("delta %s %s failed: %s", method, path, e)
                    raise
                time.sleep(2 ** attempt)
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

    def _fetch_option_product_meta(self, underlying: str) -> dict[str, dict]:
        """Fetch static product metadata from /v2/products and cache briefly.

        The tickers endpoint gives mark + strike but not settlement_time or
        contract_size. We merge these from /v2/products to let the options
        strategy pick an expiry and size positions correctly.
        """
        cache_key = f"{underlying}_meta"
        cached = self._chain_cache.get(cache_key)
        if cached and time.time() - cached["ts"] < 300:
            return cached["meta"]
        out: dict[str, dict] = {}
        try:
            after = None
            while True:
                params = {
                    "contract_types": "call_options,put_options",
                    "underlying_asset_symbols": underlying,
                }
                if after:
                    params["after"] = after
                data = self._request("GET", "/v2/products", params=params)
                rows = data.get("result", [])
                for p in rows:
                    sym = p.get("symbol")
                    if not sym:
                        continue
                    out[sym] = {
                        "expiry": p.get("settlement_time") or p.get("expiry"),
                        "contract_size": p.get("contract_size") or p.get("lot_size")
                                         or p.get("trading_precision"),
                    }
                after = data.get("meta", {}).get("after")
                if not after or not rows:
                    break
            self._chain_cache[cache_key] = {"meta": out, "ts": time.time()}
        except Exception as e:
            logger.warning("option product meta fetch failed for %s: %s", underlying, e)
        return out

    def get_option_chain(self, underlying: str) -> list[dict]:
        """All call+put options for an underlying. Returns normalized list.

        Returns REST-derived chain with full metadata (strike, expiry,
        contract_type, contract_size). The live WS stream chain is used only
        as a fallback because it currently carries symbol+mark only, and the
        options strategy needs expiry/strike/contract-size to pick an ATM pair.
        """
        cached = self._chain_cache.get(underlying)
        if cached and time.time() - cached["ts"] < _CHAIN_CACHE_TTL:
            return cached["chain"]
        try:
            meta = self._fetch_option_product_meta(underlying)
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
                if mark <= 0:
                    continue
                m = meta.get(sym, {})
                # Delta India options quote a mark_price per contract_size ETH.
                # contract_value from tickers / contract_size from products is
                # the ETH notional per contract (e.g. 0.01 ETH).
                contract_size = m.get("contract_size")
                if contract_size is None:
                    try:
                        contract_size = float(o.get("contract_value") or 0)
                    except (TypeError, ValueError):
                        contract_size = 0.0
                chain.append({
                    "symbol": sym,
                    "mark":   mark,
                    "oi":     o.get("oi"),
                    "mark_iv": o.get("mark_vol") or o.get("mark_iv"),
                    "greeks": o.get("greeks", {}),
                    "strike_price": o.get("strike_price"),
                    "contract_type": o.get("contract_type"),
                    "contract_size": contract_size if contract_size > 0 else None,
                    "expiry": m.get("expiry"),
                })
            self._chain_cache[underlying] = {"chain": chain, "ts": time.time()}
            return chain
        except Exception as e:
            logger.error("get_option_chain(%s): %s", underlying, e)
        # Degraded fallback: fresh WS marks only
        try:
            from core.ws.delta_stream import get_stream
            stream_chain = get_stream().get_option_chain(underlying)
            if len(stream_chain) >= 6:
                logger.warning("get_option_chain(%s): using stream fallback; "
                               "contract_size/expiry may be missing", underlying)
                return stream_chain
        except Exception as e:
            logger.debug("stream chain lookup failed (%s)", e)
        return []

    def get_option_mark(self, symbol: str) -> Optional[float]:
        """Current mark for an option symbol. Prefers WS stream, then REST."""
        try:
            from core.ws.delta_stream import get_stream
            stream_mark = get_stream().get_mark(symbol)
            if stream_mark is not None:
                return stream_mark
        except Exception as e:
            logger.debug("stream option mark lookup failed (%s); falling back to REST", e)
        # REST fallback: refresh chain for the underlying and scan
        for underlying in ("ETH", "BTC"):
            chain = self.get_option_chain(underlying)
            for o in chain:
                if o.get("symbol") == symbol:
                    return float(o.get("mark") or 0)
        return None

    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Current annualized funding rate for perp."""
        stats = self.get_futures_stats().get(symbol)
        return stats.get("funding_rate") if stats else None

    def get_futures_stats(self) -> dict[str, dict]:
        """Fan-out futures market stats for all perps in one REST call,
        cached 30s. Returns {symbol: {funding_rate, open_interest,
        open_interest_usd, mark_price, volume_24h_usd, oi_change_24h}}.

        Funding rate sign convention (Delta India):
            positive = longs paying shorts  → market is heavy-long, mean-revert risk
            negative = shorts paying longs  → market is heavy-short, squeeze risk
        """
        cached = self._futs_cache
        if cached["data"] is not None and time.time() - cached["ts"] < _FUTS_CACHE_TTL:
            return cached["data"]
        out: dict[str, dict] = {}
        try:
            data = self._request("GET", "/v2/tickers",
                                  params={"contract_types": "perpetual_futures"})
            for row in data.get("result", []):
                sym = row.get("symbol")
                if not sym: continue
                def _f(k):
                    v = row.get(k)
                    try: return float(v) if v is not None and v != "" else None
                    except (TypeError, ValueError): return None
                mark = _f("mark_price")
                oi   = _f("oi")
                out[sym] = {
                    "funding_rate":     _f("funding_rate"),
                    "open_interest":    oi,
                    "open_interest_usd": (oi * mark) if (oi is not None and mark is not None) else None,
                    "mark_price":       mark,
                    "volume_24h_usd":   _f("volume") or _f("turnover_usd"),
                    "mark_change_24h":  _f("mark_change_24h"),
                }
            self._futs_cache = {"data": out, "ts": time.time()}
            return out
        except Exception as e:
            logger.error("get_futures_stats: %s", e)
        return out

    def get_candles(self, symbol: str, resolution: str = "1m",
                    lookback_hours: int = 24) -> list[dict]:
        """Fetch historical candles from Delta public history endpoint.
        Returns list of {open, high, low, close, volume, time} dicts.
        """
        try:
            end = int(time.time())
            start = end - int(lookback_hours * 3600)
            data = self._request(
                "GET", "/v2/history/candles",
                params={"symbol": symbol, "resolution": resolution,
                        "start": start, "end": end},
            )
            if data.get("success"):
                rows = data.get("result", [])
                rows.sort(key=lambda x: x.get("time", 0))
                return rows
        except Exception as e:
            logger.error("get_candles %s: %s", symbol, e)
        return []

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
        """Total tradeable balance in USD-equivalents, cached 15s.

        Delta India auto-converts INR to USD at trade time for USDT-margined
        perps -- so INR sitting in the wallet IS tradeable capital for our
        purposes. We sum USD-stablecoins + (INR / USD_INR_RATE) so the
        runner's sizing has the full pool to work with, and the user only
        has to decide what percent of the pool to deploy.

        Returns None in paper mode so the runner uses env equity unchanged.
        """
        wallet = self.get_wallet_breakdown()
        if not wallet:
            return None
        usd = float(wallet.get("usd_total", 0))
        inr = float(wallet.get("inr_balance", 0))
        rate = float(os.environ.get("USD_INR_RATE", "86"))
        return usd + (inr / rate if rate > 0 else 0.0)

    def get_wallet_breakdown(self) -> dict:
        """Full wallet breakdown: USD-stablecoin total + INR balance.

        Returned dict shape:
            {"usd_total": float,    # tradeable margin (USD+USDT+USDC summed)
             "inr_balance": float,  # INR sitting in wallet (NOT tradeable for
                                    #   USDT-margined perps until converted)
             "by_asset": {symbol: balance},
             "raw_rows": list}      # ALL rows from Delta — for debugging when
                                    # an asset isn't being detected
        Cached 15s on success, 60s on failure (stops 401/timeout spam).
        """
        if self.mode == "paper":
            return {}
        cached = self._bal_cache
        # Successful cache still valid?
        if cached.get("value", -1) >= 0 and time.time() - cached["ts"] < _BAL_CACHE_TTL:
            return cached.get("breakdown", {})
        # Failed cache still in cool-down? Avoid hammering Delta on auth errors.
        if cached.get("error_ts") and time.time() - cached["error_ts"] < 60:
            return {}
        try:
            data = self._request("GET", "/v2/wallet/balances", authed=True)
            usd_total = 0.0
            inr_balance = 0.0
            by_asset: dict[str, float] = {}
            raw_rows: list = []
            # Asset detection — accept everything that looks like a USD
            # stablecoin (USD, USDT, USDC, USDt, BUSD, TUSD…). Match by
            # uppercase containment so future stablecoins on Delta still work.
            USD_LIKE = ("USD", "USDT", "USDC", "USDP", "BUSD", "TUSD", "DAI")
            for row in data.get("result", []):
                asset_raw = row.get("asset_symbol") or row.get("asset") or ""
                asset = str(asset_raw).upper().strip()
                bal_raw = row.get("balance") or 0
                avail_raw = row.get("available_balance")
                pick = avail_raw if (avail_raw not in (None, "")) else bal_raw
                try: val = float(pick or 0)
                except (TypeError, ValueError): val = 0.0
                if val > 0:
                    raw_rows.append({"asset": asset, "balance": bal_raw,
                                     "available": avail_raw})
                if val <= 0:
                    continue
                if asset == "INR":
                    inr_balance += val
                    by_asset["INR"] = by_asset.get("INR", 0) + val
                elif asset in USD_LIKE or "USD" in asset:
                    usd_total += val
                    by_asset[asset] = by_asset.get(asset, 0) + val
                else:
                    by_asset[asset] = by_asset.get(asset, 0) + val
            # Log once per cache miss so the user can see exactly what assets
            # the wallet endpoint returned (helps when something else is in
            # there — BTC collateral, sub-account, etc.).
            if raw_rows:
                logger.info("delta wallet: %s (usd_total=$%.2f, inr=%.2f)",
                            raw_rows, usd_total, inr_balance)
            else:
                logger.info("delta wallet: no non-zero rows in response")
            if usd_total > 0 or inr_balance > 0:
                breakdown = {"usd_total": usd_total,
                             "inr_balance": inr_balance,
                             "by_asset": by_asset}
                self._bal_cache = {"value": usd_total, "ts": time.time(),
                                   "breakdown": breakdown}
                return breakdown
        except Exception as e:
            # Cache the failure for 60s so we stop hammering Delta on a 401
            # (which only resolves when the user updates the API key scopes).
            err_str = str(e)
            self._bal_cache = {"value": -1.0, "ts": 0.0,
                               "error_ts": time.time(), "error": err_str}
            if "401" in err_str:
                logger.warning("delta wallet: 401 — API key needs 'Wallet Read' "
                               "permission on delta.exchange (next retry in 60s)")
            else:
                logger.error("get_balance: %s (next retry in 60s)", e)
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
            mark = self._paper_fill_price(symbol)
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

    def _paper_fill_price(self, symbol: str) -> Optional[float]:
        """Best-effort paper fill price for any Delta symbol."""
        if symbol.endswith("USD"):
            return self.get_perp_mark(symbol)
        if symbol.startswith(("C-", "P-")):
            return self.get_option_mark(symbol)
        return None

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
