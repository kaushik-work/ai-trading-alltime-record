"""
Polymarket API Client — read-only (no wallet needed for backtesting/scanning)

Polymarket uses two APIs:
  Gamma API : https://gamma-api.polymarket.com  — markets metadata, search
  CLOB API  : https://clob.polymarket.com        — prices, order book, history

No auth required for reading. Live trading needs Polygon wallet + USDC.
"""

import time
import logging
from typing import Optional
import requests

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"

_session = requests.Session()
_session.headers.update({"User-Agent": "polymarket-research-bot/1.0"})


def _get(url: str, params: dict = None, retries: int = 3) -> Optional[dict]:
    for attempt in range(retries):
        try:
            r = _session.get(url, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            if e.response.status_code == 429:
                time.sleep(2 ** attempt)
            else:
                logger.warning("HTTP %s: %s", e.response.status_code, url)
                return None
        except Exception as e:
            logger.warning("Request failed (attempt %d): %s — %s", attempt + 1, url, e)
            time.sleep(1)
    return None


# ── Market listing ────────────────────────────────────────────────────────────

def get_markets(
    active: bool = True,
    limit: int = 100,
    offset: int = 0,
    tag_slug: str = None,
) -> list:
    """
    Fetch open markets from Polymarket.

    Returns list of market dicts:
      id, question, outcomes, outcomePrices, volume, liquidity,
      endDate, closed, resolutionSource, description
    """
    params = {
        "active": str(active).lower(),
        "limit":  limit,
        "offset": offset,
    }
    if tag_slug:
        params["tag_slug"] = tag_slug

    data = _get(f"{GAMMA_BASE}/markets", params)
    if not data:
        return []
    return data if isinstance(data, list) else data.get("markets", [])


def get_market(market_id: str) -> Optional[dict]:
    """Fetch single market by ID."""
    return _get(f"{GAMMA_BASE}/markets/{market_id}")


def get_resolved_markets(limit: int = 200, offset: int = 0) -> list:
    """Fetch recently resolved markets (for backtesting)."""
    params = {"closed": "true", "limit": limit, "offset": offset}
    data = _get(f"{GAMMA_BASE}/markets", params)
    if not data:
        return []
    return data if isinstance(data, list) else data.get("markets", [])


# ── Price history ─────────────────────────────────────────────────────────────

def get_price_history(
    condition_id: str,
    interval: str = "1d",    # "1m" | "5m" | "1h" | "1d" | "all"
    fidelity: int = 100,     # data points (max ~3000 for short intervals)
) -> list:
    """
    Fetch historical YES-token prices for a market condition.

    Returns list of {"t": timestamp_ms, "p": price_0_to_1}
    """
    params = {
        "market":   condition_id,
        "interval": interval,
        "fidelity": fidelity,
    }
    data = _get(f"{CLOB_BASE}/prices-history", params)
    if not data:
        return []
    return data.get("history", [])


# ── Order book (live) ─────────────────────────────────────────────────────────

def get_orderbook(token_id: str) -> Optional[dict]:
    """
    Live order book for a YES or NO token.
    Returns {"asks": [...], "bids": [...], "market": token_id}
    """
    return _get(f"{CLOB_BASE}/book", params={"token_id": token_id})


def get_mid_price(token_id: str) -> Optional[float]:
    """Current mid-price (0-1) for a token."""
    book = get_orderbook(token_id)
    if not book:
        return None
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if bids and asks:
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        return round((best_bid + best_ask) / 2, 4)
    elif bids:
        return float(bids[0]["price"])
    elif asks:
        return float(asks[0]["price"])
    return None


# ── Market search ─────────────────────────────────────────────────────────────

def search_markets(query: str, limit: int = 20) -> list:
    """Search open markets by keyword."""
    params = {"q": query, "limit": limit, "active": "true"}
    data = _get(f"{GAMMA_BASE}/markets", params)
    if not data:
        return []
    return data if isinstance(data, list) else data.get("markets", [])


# ── Tags / categories ─────────────────────────────────────────────────────────

def get_tags() -> list:
    """List available market categories."""
    data = _get(f"{GAMMA_BASE}/tags")
    return data or []


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_market_price(market: dict) -> Optional[float]:
    """
    Extract current YES price (0–1) from a market dict.
    outcomePrices is a JSON-encoded list like '["0.28","0.72"]'
    """
    import json
    try:
        prices = market.get("outcomePrices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        if prices:
            return float(prices[0])   # index 0 = YES price
    except Exception:
        pass
    return None


def parse_liquidity(market: dict) -> float:
    try:
        return float(market.get("liquidity", 0) or 0)
    except Exception:
        return 0.0


def parse_volume(market: dict) -> float:
    try:
        return float(market.get("volume", 0) or 0)
    except Exception:
        return 0.0
