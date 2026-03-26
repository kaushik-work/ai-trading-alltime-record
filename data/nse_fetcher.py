"""
NseFetcher — fetches intraday OHLCV bars directly from NSE India's public API.

Used as a fallback when Zerodha (jugaad-trader) is unavailable.
No login, no API key — NSE India is the official source.

Flow:
  1. GET https://www.nseindia.com  →  capture session cookies
  2. GET chart-databyindex-intraday  →  per-minute [timestamp_ms, price] tuples
  3. Resample into 5-min or 15-min OHLCV bars
  4. Fetch 60-day daily OHLCV from NSE historical index endpoint

NSE rate-limits aggressively — session is cached for the full trading day.
"""

import logging
import threading
import time
from datetime import datetime, date, timedelta
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# NSE API paths
_NSE_BASE   = "https://www.nseindia.com"
_INTRADAY   = "/api/chart-databyindex-intraday?index={index_name}&indices=true"
_HIST_INDEX = "/api/historical/indicesHistory?indexType={index_name}&from={from_d}&to={to_d}"

_INDEX_NAME = {
    "NIFTY":     "NIFTY%2050",
    "BANKNIFTY": "NIFTY%20BANK",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}


class NseFetcher:
    """
    Singleton. Refreshes NSE cookies once per day (they expire overnight).
    """

    _instance: Optional["NseFetcher"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._session = None
        self._cookie_date: Optional[date] = None

    @classmethod
    def get(cls) -> "NseFetcher":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── session ───────────────────────────────────────────────────────────────

    def _ensure_session(self) -> bool:
        import requests
        today = date.today()
        if self._session is not None and self._cookie_date == today:
            return True
        with self._lock:
            if self._session is not None and self._cookie_date == today:
                return True
            try:
                s = requests.Session()
                s.headers.update(_HEADERS)
                # Hit the homepage to capture cookies (required for all API calls)
                r = s.get(_NSE_BASE, timeout=10)
                r.raise_for_status()
                time.sleep(0.5)   # brief pause — NSE rate-limiter
                self._session = s
                self._cookie_date = today
                logger.info("NseFetcher: NSE session established")
                return True
            except Exception as e:
                logger.error("NseFetcher: session setup failed: %s", e)
                self._session = None
                return False

    def _get(self, path: str) -> Optional[dict]:
        if not self._ensure_session():
            return None
        import requests
        url = _NSE_BASE + path
        try:
            r = self._session.get(url, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error("NseFetcher GET %s failed: %s", path, e)
            # Force session refresh on next call
            self._session = None
            return None

    # ── intraday bars ─────────────────────────────────────────────────────────

    def fetch_intraday(self, symbol: str, interval: str):
        """
        Fetch today's intraday bars resampled to `interval` (5m or 15m),
        plus 60-day daily closes for EMA/RSI stability.

        Returns (opens, highs, lows, closes, volumes, all_closes, last_bar_time)
        or None on failure.
        """
        index_name = _INDEX_NAME.get(symbol)
        if index_name is None:
            logger.warning("NseFetcher: unsupported symbol %s", symbol)
            return None

        # ── Step 1: intraday minute data ──────────────────────────────────────
        data = self._get(_INTRADAY.format(index_name=index_name))
        if data is None:
            return None

        # NSE returns {"grapthData": [[ms_timestamp, price], ...], ...}
        raw = data.get("grapthData") or data.get("graphData") or []
        if len(raw) < 5:
            logger.warning("NseFetcher: only %d intraday ticks for %s", len(raw), symbol)
            return None

        # Convert to minute-level closes (no volume from NSE index chart)
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        minute_bars = []
        for item in raw:
            try:
                ts_ms, price = item[0], float(item[1])
                dt = datetime.fromtimestamp(ts_ms / 1000, tz=IST)
                minute_bars.append((dt, price))
            except Exception:
                continue

        if len(minute_bars) < 5:
            return None

        # ── Step 2: resample to target interval ───────────────────────────────
        minutes = int(interval.replace("m", ""))
        buckets: dict = {}
        for dt, price in minute_bars:
            # Bucket start = floor to nearest `minutes`
            floored = dt.replace(
                minute=(dt.minute // minutes) * minutes,
                second=0, microsecond=0
            )
            if floored not in buckets:
                buckets[floored] = {"open": price, "high": price, "low": price, "close": price}
            else:
                b = buckets[floored]
                b["high"]  = max(b["high"], price)
                b["low"]   = min(b["low"],  price)
                b["close"] = price

        if len(buckets) < 3:
            logger.warning("NseFetcher: only %d %s bars for %s", len(buckets), interval, symbol)
            return None

        sorted_keys = sorted(buckets)
        opens   = np.array([buckets[k]["open"]  for k in sorted_keys], dtype=float)
        highs   = np.array([buckets[k]["high"]  for k in sorted_keys], dtype=float)
        lows    = np.array([buckets[k]["low"]   for k in sorted_keys], dtype=float)
        closes  = np.array([buckets[k]["close"] for k in sorted_keys], dtype=float)
        # NSE index chart has no volume — fill with 1s so volume-based checks degrade gracefully
        volumes = np.ones(len(closes), dtype=float)
        bar_time = sorted_keys[-1].time()

        # ── Step 3: 60-day daily closes ───────────────────────────────────────
        all_closes = self._fetch_daily_closes(symbol, closes)

        logger.info(
            "NseFetcher: %s %s → %d bars, last bar %s (volume unavailable — filled 1s)",
            symbol, interval, len(closes), bar_time,
        )
        return opens, highs, lows, closes, volumes, all_closes, bar_time

    def _fetch_daily_closes(self, symbol: str, fallback: np.ndarray) -> np.ndarray:
        df = self.fetch_daily_df(symbol)
        if df is not None and len(df) >= 5:
            return df["Close"].values.astype(float)
        return fallback

    def fetch_daily_df(self, symbol: str, days: int = 90):
        """
        Fetch daily OHLCV from NSE historical index endpoint.
        Returns pandas DataFrame with Open/High/Low/Close/Volume, or None.
        """
        index_name = _INDEX_NAME.get(symbol)
        if index_name is None:
            return None
        today = date.today()
        from_d = (today - timedelta(days=days + 5)).strftime("%d-%m-%Y")
        to_d   = today.strftime("%d-%m-%Y")
        path = _HIST_INDEX.format(index_name=index_name, from_d=from_d, to_d=to_d)
        data = self._get(path)
        if data is None:
            return None
        try:
            import pandas as pd
            records = (
                data.get("data", {}).get("indexCloseOnlineRecords")
                or data.get("indexCloseOnlineRecords")
                or []
            )
            rows = []
            for r in records:
                try:
                    rows.append({
                        "Date":   r.get("EOD_TIMESTAMP", ""),
                        "Open":   float(r.get("EOD_OPEN_INDEX_VAL",  r.get("EOD_CLOSE_INDEX_VAL", 0))),
                        "High":   float(r.get("EOD_HIGH_INDEX_VAL",  r.get("EOD_CLOSE_INDEX_VAL", 0))),
                        "Low":    float(r.get("EOD_LOW_INDEX_VAL",   r.get("EOD_CLOSE_INDEX_VAL", 0))),
                        "Close":  float(r["EOD_CLOSE_INDEX_VAL"]),
                        "Volume": float(r.get("TRADED_VOLUME", 0)),
                    })
                except Exception:
                    continue
            if len(rows) < 5:
                return None
            df = pd.DataFrame(rows)
            df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
            df.dropna(subset=["Date"], inplace=True)
            df.sort_values("Date", inplace=True)
            df.set_index("Date", inplace=True)
            logger.info("NseFetcher: %s daily → %d bars", symbol, len(df))
            return df
        except Exception as e:
            logger.warning("NseFetcher.fetch_daily_df %s: %s", symbol, e)
            return None

    def fetch_intraday_df(self, symbol: str, interval: str):
        """
        Fetch today's intraday bars resampled to `interval` as a pandas DataFrame.
        Returns DataFrame with Open/High/Low/Close/Volume columns, or None.
        Volume is 1.0 for all bars (NSE index charts have no per-bar volume).
        """
        result = self.fetch_intraday(symbol, interval)
        if result is None:
            return None
        import pandas as pd
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        opens, highs, lows, closes, volumes, _, bar_time = result
        # Reconstruct timestamps for the bars (approximate from bar_time of last bar)
        minutes = int(interval.replace("m", ""))
        n = len(closes)
        from datetime import datetime, timedelta
        now = datetime.now(IST)
        last_bar_dt = now.replace(
            hour=bar_time.hour, minute=bar_time.minute, second=0, microsecond=0
        )
        timestamps = [last_bar_dt - timedelta(minutes=minutes * (n - 1 - i)) for i in range(n)]
        df = pd.DataFrame({
            "Open": opens, "High": highs, "Low": lows,
            "Close": closes, "Volume": volumes,
        }, index=timestamps)
        return df
