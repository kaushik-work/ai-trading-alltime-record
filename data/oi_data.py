"""
NSE Option Chain data — PCR (Put-Call Ratio) and OI analysis.
BeSensibull style: PCR > 1.2 = market is hedged/bullish, PCR < 0.8 = bearish.

NSE requires a warmed-up session with browser headers.
All functions are silent-fail and return neutral defaults on error.
"""
import time
import logging
import requests

logger = logging.getLogger(__name__)

_session: requests.Session = None
_session_time: float = 0
_SESSION_TTL = 240  # re-warm every 4 minutes

_BASE = "https://www.nseindia.com"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}

# Cache: symbol -> (fetched_at, result_dict)
_oi_cache: dict = {}
_OI_CACHE_TTL = 300  # 5 min


def _get_session() -> requests.Session:
    global _session, _session_time
    if _session and (time.time() - _session_time) < _SESSION_TTL:
        return _session
    s = requests.Session()
    s.headers.update(_HEADERS)
    try:
        s.get(_BASE, timeout=8)  # warm up — sets cookies
    except Exception as e:
        logger.warning("NSE session warmup failed: %s", e)
    _session = s
    _session_time = time.time()
    return s


def get_pcr(symbol: str = "NIFTY") -> dict:
    """
    Fetch PCR and OI data for NIFTY or BANKNIFTY.
    Returns a dict with pcr, sentiment, total_call_oi, total_put_oi.
    Falls back to neutral on any error.
    """
    now = time.time()
    cached = _oi_cache.get(symbol)
    if cached and (now - cached[0]) < _OI_CACHE_TTL:
        return cached[1]

    result = _fetch_pcr(symbol)
    _oi_cache[symbol] = (now, result)
    return result


def _fetch_pcr(symbol: str) -> dict:
    neutral = {
        "symbol": symbol, "pcr": 1.0,
        "total_call_oi": 0, "total_put_oi": 0,
        "sentiment": "neutral", "source": "fallback",
    }
    try:
        session = _get_session()
        url = f"{_BASE}/api/option-chain-indices?symbol={symbol}"
        resp = session.get(url, timeout=8)
        if not resp.ok:
            logger.warning("NSE OI fetch failed (%d) for %s", resp.status_code, symbol)
            return neutral

        data = resp.json()
        records = data.get("records", {}).get("data", [])

        total_call_oi = sum(r.get("CE", {}).get("openInterest", 0) for r in records if "CE" in r)
        total_put_oi = sum(r.get("PE", {}).get("openInterest", 0) for r in records if "PE" in r)

        if total_call_oi == 0:
            return neutral

        pcr = round(total_put_oi / total_call_oi, 3)

        # BeSensibull thresholds
        if pcr >= 1.3:
            sentiment = "very_bullish"
        elif pcr >= 1.1:
            sentiment = "bullish"
        elif pcr <= 0.7:
            sentiment = "very_bearish"
        elif pcr <= 0.9:
            sentiment = "bearish"
        else:
            sentiment = "neutral"

        result = {
            "symbol": symbol,
            "pcr": pcr,
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "sentiment": sentiment,
            "source": "nse_live",
        }
        logger.debug("PCR %s: %.2f (%s)", symbol, pcr, sentiment)
        return result

    except Exception as e:
        logger.warning("OI fetch error for %s: %s", symbol, e)
        return neutral


def get_market_sentiment() -> dict:
    """
    Get overall market sentiment from NIFTY + BANKNIFTY PCR combined.
    Used as a market-wide filter — avoid longs when overall sentiment is bearish.
    """
    nifty = get_pcr("NIFTY")
    bn = get_pcr("BANKNIFTY")

    avg_pcr = round((nifty["pcr"] + bn["pcr"]) / 2, 3)

    if avg_pcr >= 1.2:
        overall = "bullish"
    elif avg_pcr <= 0.85:
        overall = "bearish"
    else:
        overall = "neutral"

    return {
        "nifty_pcr": nifty["pcr"],
        "banknifty_pcr": bn["pcr"],
        "avg_pcr": avg_pcr,
        "overall_sentiment": overall,
        "nifty_sentiment": nifty["sentiment"],
        "banknifty_sentiment": bn["sentiment"],
    }
