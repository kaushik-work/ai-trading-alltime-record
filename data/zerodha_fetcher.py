"""
ZerodhaFetcher — uses Zerodha Kite Connect API to pull real NSE intraday bars.

Replaces yfinance in bot_runner._fetch_intraday so strategies get
correct OHLCV data (proper volume, no 15-min delay).

Instrument tokens (NSE index, does NOT change):
  NIFTY 50   : 256265
  BANK NIFTY : 260105

Requires a valid ZERODHA_ACCESS_TOKEN in .env (run scripts/get_token.py daily).
"""

import logging
import threading
from datetime import datetime, date, timedelta
from typing import Optional
import numpy as np

import config

logger = logging.getLogger(__name__)

_TOKENS = {
    "NIFTY":     256265,
    "BANKNIFTY": 260105,
}

_INTERVAL_MAP = {
    "5m":  "5minute",
    "15m": "15minute",
    "1d":  "day",
}


class ZerodhaFetcher:
    """
    Singleton — login once at startup, reuse session all day.
    Thread-safe: a lock prevents concurrent logins.
    """

    _instance: Optional["ZerodhaFetcher"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._broker = None   # KiteConnect session
        self._login_date: Optional[date] = None
        self._failed_date: Optional[date] = None   # don't retry after failure same day

    # ── singleton access ──────────────────────────────────────────────────────

    @classmethod
    def get(cls) -> "ZerodhaFetcher":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── login ─────────────────────────────────────────────────────────────────

    def _ensure_logged_in(self) -> bool:
        """Initialise Kite Connect session using the access token from config.

        The access token is generated once per trading day via scripts/get_token.py
        and stored in .env as ZERODHA_ACCESS_TOKEN.
        """
        today = date.today()
        if self._broker is not None and self._login_date == today:
            return True

        if self._failed_date == today:
            return False

        with self._lock:
            if self._broker is not None and self._login_date == today:
                return True
            if self._failed_date == today:
                return False
            try:
                from kiteconnect import KiteConnect
                if not config.ZERODHA_API_KEY or not config.ZERODHA_ACCESS_TOKEN:
                    logger.error(
                        "ZerodhaFetcher: ZERODHA_API_KEY or ZERODHA_ACCESS_TOKEN not set. "
                        "Run scripts/get_token.py to generate today's token."
                    )
                    self._failed_date = today
                    return False

                kite = KiteConnect(api_key=config.ZERODHA_API_KEY)
                kite.set_access_token(config.ZERODHA_ACCESS_TOKEN)
                self._broker = kite
                self._login_date = today
                self._failed_date = None
                logger.info("ZerodhaFetcher: Kite Connect session ready for %s", config.ZERODHA_USER_ID or "user")
                return True
            except Exception as e:
                logger.error("ZerodhaFetcher setup failed: %s", e)
                self._broker = None
                self._failed_date = today
                return False

    # ── data fetch ────────────────────────────────────────────────────────────

    def fetch_intraday(self, symbol: str, interval: str):
        """
        Fetch today's intraday bars + 60-day daily closes.

        Returns (opens, highs, lows, closes, volumes, all_closes, last_bar_time)
        or None on failure.

        Mirrors the return shape of bot_runner._fetch_intraday so it's a
        drop-in replacement.
        """
        if not self._ensure_logged_in():
            return None

        token = _TOKENS.get(symbol)
        if token is None:
            logger.warning("ZerodhaFetcher: no token for %s", symbol)
            return None

        zd_interval = _INTERVAL_MAP.get(interval)
        if zd_interval is None:
            logger.warning("ZerodhaFetcher: unsupported interval %s", interval)
            return None

        try:
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
            now = datetime.now(IST)
            today_str = now.strftime("%Y-%m-%d")
            # Intraday bars: from market open today
            intraday = self._broker.historical_data(
                instrument_token=token,
                from_date=f"{today_str} 09:15:00",
                to_date=f"{today_str} 15:30:00",
                interval=zd_interval,
            )
            if not intraday or len(intraday) < 3:
                logger.warning("ZerodhaFetcher: insufficient intraday bars for %s (%d)", symbol, len(intraday) if intraday else 0)
                return None

            opens   = np.array([b["open"]   for b in intraday], dtype=float)
            highs   = np.array([b["high"]   for b in intraday], dtype=float)
            lows    = np.array([b["low"]    for b in intraday], dtype=float)
            closes  = np.array([b["close"]  for b in intraday], dtype=float)
            volumes = np.array([b["volume"] for b in intraday], dtype=float)

            # Last bar timestamp → extract time component
            last_dt = intraday[-1]["date"]
            if hasattr(last_dt, "astimezone"):
                last_dt = last_dt.astimezone(IST)
            bar_time = last_dt.time() if hasattr(last_dt, "time") else now.time()

            # 60-day daily closes for EMA/RSI stability
            sixty_ago = (now - timedelta(days=65)).strftime("%Y-%m-%d")
            daily = self._broker.historical_data(
                instrument_token=token,
                from_date=f"{sixty_ago} 09:15:00",
                to_date=f"{today_str} 15:30:00",
                interval="day",
            )
            if daily and len(daily) >= 5:
                all_closes = np.array([b["close"] for b in daily], dtype=float)
            else:
                all_closes = closes  # fallback to intraday closes

            logger.info(
                "ZerodhaFetcher: %s %s → %d intraday bars, %d daily bars, last bar %s",
                symbol, interval, len(closes), len(all_closes), bar_time,
            )
            return opens, highs, lows, closes, volumes, all_closes, bar_time

        except Exception as e:
            logger.error("ZerodhaFetcher.fetch_intraday %s %s: %s", symbol, interval, e)
            # Session may have expired — force re-login next call
            self._broker = None
            return None

    def fetch_daily_df(self, symbol: str, days: int = 90):
        """
        Fetch `days` of daily OHLCV bars as a pandas DataFrame.
        Columns: Open, High, Low, Close, Volume  (index = date).
        Returns None on failure.
        """
        if not self._ensure_logged_in():
            return None
        token = _TOKENS.get(symbol)
        if token is None:
            return None
        try:
            import pandas as pd
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
            now = datetime.now(IST)
            from_d = (now - timedelta(days=days + 5)).strftime("%Y-%m-%d")
            to_d   = now.strftime("%Y-%m-%d")
            records = self._broker.historical_data(
                instrument_token=token,
                from_date=f"{from_d} 09:15:00",
                to_date=f"{to_d} 15:30:00",
                interval="day",
            )
            if not records or len(records) < 5:
                return None
            df = pd.DataFrame(records)
            df.rename(columns={
                "date": "Date", "open": "Open", "high": "High",
                "low": "Low", "close": "Close", "volume": "Volume",
            }, inplace=True)
            df.set_index("Date", inplace=True)
            logger.info("ZerodhaFetcher: %s daily → %d bars", symbol, len(df))
            return df
        except Exception as e:
            logger.error("ZerodhaFetcher.fetch_daily_df %s: %s", symbol, e)
            self._broker = None
            return None

    def fetch_historical_df(self, symbol: str, interval: str, days: int = 60):
        """
        Fetch multiple days of intraday bars — used by the backtest engine.
        Returns a pandas DataFrame with Open/High/Low/Close/Volume columns
        and a datetime index in IST, or None on failure.

        Zerodha allows:
          5-min  bars : up to 60 days
          15-min bars : up to 60 days
        """
        if not self._ensure_logged_in():
            return None
        token = _TOKENS.get(symbol)
        zd_interval = _INTERVAL_MAP.get(interval)
        if token is None or zd_interval is None:
            return None
        try:
            import pandas as pd
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
            now = datetime.now(IST)
            from_d = (now - timedelta(days=days + 2)).strftime("%Y-%m-%d")
            to_d   = now.strftime("%Y-%m-%d")
            records = self._broker.historical_data(
                instrument_token=token,
                from_date=f"{from_d} 09:15:00",
                to_date=f"{to_d} 15:30:00",
                interval=zd_interval,
            )
            if not records or len(records) < 10:
                logger.warning("ZerodhaFetcher.fetch_historical_df: only %d bars for %s %s",
                               len(records) if records else 0, symbol, interval)
                return None
            df = pd.DataFrame(records)
            df.rename(columns={
                "date": "Date", "open": "Open", "high": "High",
                "low": "Low", "close": "Close", "volume": "Volume",
            }, inplace=True)
            df["Date"] = pd.to_datetime(df["Date"])
            if df["Date"].dt.tz is not None:
                df["Date"] = df["Date"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
            df.set_index("Date", inplace=True)
            df["_date"] = df.index.date
            logger.info("ZerodhaFetcher.fetch_historical_df: %s %s → %d bars over %d days",
                        symbol, interval, len(df), len(df["_date"].unique()))
            return df
        except Exception as e:
            logger.error("ZerodhaFetcher.fetch_historical_df %s %s: %s", symbol, interval, e)
            self._broker = None
            return None

    def fetch_intraday_df(self, symbol: str, interval: str):
        """
        Fetch today's intraday bars as a pandas DataFrame.
        Columns: Open, High, Low, Close, Volume  (index = datetime IST).
        Returns None on failure.
        """
        if not self._ensure_logged_in():
            return None
        token = _TOKENS.get(symbol)
        zd_interval = _INTERVAL_MAP.get(interval)
        if token is None or zd_interval is None:
            return None
        try:
            import pandas as pd
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
            now = datetime.now(IST)
            today_str = now.strftime("%Y-%m-%d")
            records = self._broker.historical_data(
                instrument_token=token,
                from_date=f"{today_str} 09:15:00",
                to_date=f"{today_str} 15:30:00",
                interval=zd_interval,
            )
            if not records or len(records) < 3:
                return None
            df = pd.DataFrame(records)
            df.rename(columns={
                "date": "Date", "open": "Open", "high": "High",
                "low": "Low", "close": "Close", "volume": "Volume",
            }, inplace=True)
            df.set_index("Date", inplace=True)
            return df
        except Exception as e:
            logger.error("ZerodhaFetcher.fetch_intraday_df %s %s: %s", symbol, interval, e)
            self._broker = None
            return None
