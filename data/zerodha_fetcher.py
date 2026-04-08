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
import os
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
    "3m":  "3minute",
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
        self._token_used: str = ""                 # track which token we logged in with
        self._instruments: Optional[list] = None       # cached NFO instrument list
        self._instruments_date: Optional[date] = None  # date when cache was built

    # ── singleton access ──────────────────────────────────────────────────────

    @classmethod
    def get(cls) -> "ZerodhaFetcher":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── login ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _read_env_token() -> tuple[str, str]:
        """Read api_key and access_token directly from .env (always fresh)."""
        from dotenv import dotenv_values
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        vals = dotenv_values(env_path)
        return vals.get("ZERODHA_API_KEY", ""), vals.get("ZERODHA_ACCESS_TOKEN", "")

    def _ensure_logged_in(self) -> bool:
        """Initialise Kite Connect session using the access token from .env.

        Re-reads .env on every call so a fresh token written by get_token.py
        is picked up without a server restart.
        """
        from core.utils import now_ist
        today = now_ist().date()
        api_key, access_token = self._read_env_token()

        # If the token in .env changed, force a re-login regardless of cached state
        token_changed = access_token and access_token != self._token_used

        if self._broker is not None and self._login_date == today and not token_changed:
            return True

        if self._failed_date == today and not token_changed:
            return False

        with self._lock:
            # Re-read inside lock in case another thread already updated
            api_key, access_token = self._read_env_token()
            token_changed = access_token and access_token != self._token_used

            if self._broker is not None and self._login_date == today and not token_changed:
                return True
            if self._failed_date == today and not token_changed:
                return False
            try:
                from kiteconnect import KiteConnect
                if not api_key or not access_token:
                    logger.error(
                        "ZerodhaFetcher: ZERODHA_API_KEY or ZERODHA_ACCESS_TOKEN not set. "
                        "Run scripts/get_token.py to generate today's token."
                    )
                    self._failed_date = today
                    return False

                kite = KiteConnect(api_key=api_key)
                kite.set_access_token(access_token)
                self._broker = kite
                self._login_date = today
                self._failed_date = None
                self._token_used = access_token
                logger.info("ZerodhaFetcher: Kite Connect session ready for %s", config.ZERODHA_USER_ID or "user")
                return True
            except Exception as e:
                logger.error("ZerodhaFetcher setup failed: %s", e)
                self._broker = None
                self._failed_date = today
                return False

    def is_token_live(self) -> bool:
        """Verify the token is actually valid by calling kite.profile(). Fast — ~100ms."""
        if not self._ensure_logged_in():
            return False
        try:
            self._broker.profile()
            return True
        except Exception:
            # Reset broker so next call re-reads .env and attempts fresh login
            with self._lock:
                self._broker = None
                self._login_date = None
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

    # ── options helpers ───────────────────────────────────────────────────────

    @classmethod
    def nearest_weekly_expiry(cls) -> date:
        """Return the nearest tradable NIFTY expiry from the live NFO instruments list.

        This is safer than assuming "next Thursday" because NSE weekly expiries
        can shift earlier when Thursday is a trading holiday.
        """
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        today = now.date()
        inst = cls.get()

        try:
            if inst._ensure_logged_in():
                expiries = sorted({
                    i["expiry"]
                    for i in inst._nfo_instruments()
                    if i["name"] == "NIFTY" and i["instrument_type"] in ("CE", "PE")
                    and i["expiry"] >= today
                })
                if expiries:
                    if now.hour > 15 or (now.hour == 15 and now.minute >= 30):
                        future = [exp for exp in expiries if exp > today]
                        if future:
                            return future[0]
                    return expiries[0]
        except Exception as e:
            logger.warning("nearest_weekly_expiry: live expiry discovery failed: %s", e)

        days_to_thu = (3 - today.weekday()) % 7
        if days_to_thu == 0 and (now.hour > 15 or (now.hour == 15 and now.minute >= 30)):
            days_to_thu = 7
        return today + timedelta(days=days_to_thu)

    def _nfo_instruments(self) -> list:
        """Return cached NFO instrument list, refreshed once per calendar day."""
        from core.utils import now_ist
        today = now_ist().date()
        if self._instruments_date == today and self._instruments is not None:
            return self._instruments
        instruments = self._broker.instruments("NFO")
        self._instruments = [
            i for i in instruments
            if i["name"] in ("NIFTY", "BANKNIFTY") and i["instrument_type"] in ("CE", "PE")
        ]
        self._instruments_date = today
        logger.info("ZerodhaFetcher: cached %d NIFTY/BANKNIFTY option instruments", len(self._instruments))
        return self._instruments

    def get_option_ltp(self, symbol: str, strike: int, option_type: str, expiry: date):
        """Return (tradingsymbol, last_traded_price) for the given option.

        symbol      : "NIFTY" or "BANKNIFTY"
        strike      : integer ATM strike (e.g. 23000)
        option_type : "CE" or "PE"
        expiry      : date of weekly/monthly expiry

        Returns (None, None) on any failure — caller must handle gracefully.
        """
        if not self._ensure_logged_in():
            return None, None
        try:
            instruments = self._nfo_instruments()
            match = next(
                (i for i in instruments
                 if i["name"] == symbol
                 and int(i["strike"]) == strike
                 and i["instrument_type"] == option_type
                 and i["expiry"] == expiry),
                None,
            )
            if match is None:
                from zoneinfo import ZoneInfo
                today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
                fallback = sorted(
                    (
                        i for i in instruments
                        if i["name"] == symbol
                        and int(i["strike"]) == strike
                        and i["instrument_type"] == option_type
                        and i["expiry"] >= today
                    ),
                    key=lambda i: i["expiry"],
                )
                if fallback:
                    match = fallback[0]
                    logger.warning(
                        "get_option_ltp: using fallback expiry %s for %s strike=%d %s (requested %s)",
                        match["expiry"], symbol, strike, option_type, expiry,
                    )
            if match is None:
                logger.warning(
                    "get_option_ltp: no instrument for %s %s strike=%d %s",
                    symbol, expiry, strike, option_type,
                )
                return None, None
            ts = match["tradingsymbol"]
            data = self._broker.ltp([f"NFO:{ts}"])
            ltp = float(data.get(f"NFO:{ts}", {}).get("last_price", 0.0))
            logger.info("get_option_ltp: %s = ₹%.2f", ts, ltp)
            return ts, ltp
        except Exception as e:
            logger.error("get_option_ltp %s strike=%d %s %s: %s", symbol, strike, option_type, expiry, e)
            self._broker = None  # force re-login next call
            return None, None

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
                logger.error(
                    "ZerodhaFetcher.fetch_daily_df %s: API returned %d records (need ≥5) — "
                    "access token may be expired, run scripts/get_token.py",
                    symbol, len(records) if records else 0,
                )
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

    def fetch_vix_historical_df(self, days: int = 100):
        """
        Fetch historical daily India VIX data from Kite Connect.
        Returns a DataFrame indexed by date with a 'vix' column (daily close).
        India VIX instrument token = 264969.
        """
        if not self._ensure_logged_in():
            return None
        try:
            import pandas as pd
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
            now    = datetime.now(IST)
            from_d = (now - timedelta(days=days + 5)).strftime("%Y-%m-%d")
            to_d   = now.strftime("%Y-%m-%d")
            records = self._broker.historical_data(
                instrument_token=264969,
                from_date=from_d,
                to_date=to_d,
                interval="day",
            )
            if not records:
                return None
            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df = df.rename(columns={"close": "vix"})[["date", "vix"]].set_index("date")
            logger.info("VIX historical: %d days fetched", len(df))
            return df
        except Exception as e:
            logger.warning("fetch_vix_historical_df failed: %s", e)
            return None

    def fetch_vix(self) -> float:
        """
        Fetch the live India VIX value from Kite Connect.
        India VIX instrument token = 264969 (NSE).
        Returns the last traded price as a float, or None on failure.
        """
        if not self._ensure_logged_in():
            return None
        try:
            data = self._broker.ltp(["NSE:INDIA VIX"])
            vix = float(data.get("NSE:INDIA VIX", {}).get("last_price", 0.0))
            logger.info("India VIX: %.2f", vix)
            return vix if vix > 0 else None
        except Exception as e:
            logger.warning("fetch_vix failed: %s", e)
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

    # ── NSE equity support ────────────────────────────────────────────────────

    _nse_token_cache: dict = {}       # symbol → token (class-level, persists all day)
    _nse_cache_date: Optional[date] = None

    def _nse_token(self, symbol: str) -> Optional[int]:
        """Look up NSE equity instrument token. Cached for the trading day."""
        from core.utils import now_ist
        today = now_ist().date()
        if self._nse_cache_date != today:
            ZerodhaFetcher._nse_token_cache = {}
            ZerodhaFetcher._nse_cache_date  = today

        if symbol in self._nse_token_cache:
            return self._nse_token_cache[symbol]

        try:
            instruments = self._broker.instruments("NSE")
            for i in instruments:
                if i["tradingsymbol"] == symbol and i["instrument_type"] == "EQ":
                    ZerodhaFetcher._nse_token_cache[symbol] = i["instrument_token"]
                    return i["instrument_token"]
        except Exception as e:
            logger.warning("_nse_token lookup failed for %s: %s", symbol, e)
        return None

    def fetch_equity_daily(self, symbol: str, days: int = 365):
        """
        Fetch `days` of daily OHLCV for any NSE equity stock.
        Returns pandas DataFrame (Open/High/Low/Close/Volume, date index) or None.
        """
        if not self._ensure_logged_in():
            return None
        token = self._nse_token(symbol)
        if token is None:
            logger.warning("fetch_equity_daily: no NSE EQ token for %s", symbol)
            return None
        try:
            import pandas as pd
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
            now    = datetime.now(IST)
            from_d = (now - timedelta(days=days + 10)).strftime("%Y-%m-%d")
            to_d   = now.strftime("%Y-%m-%d")
            records = self._broker.historical_data(
                instrument_token=token,
                from_date=f"{from_d} 09:15:00",
                to_date=f"{to_d} 15:30:00",
                interval="day",
            )
            if not records or len(records) < 10:
                return None
            df = pd.DataFrame(records)
            df.rename(columns={"date": "Date", "open": "Open", "high": "High",
                                "low": "Low", "close": "Close", "volume": "Volume"}, inplace=True)
            df["Date"] = pd.to_datetime(df["Date"])
            if df["Date"].dt.tz is not None:
                df["Date"] = df["Date"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
            df.set_index("Date", inplace=True)
            return df
        except Exception as e:
            logger.error("fetch_equity_daily %s: %s", symbol, e)
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
