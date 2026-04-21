"""
AngelFetcher — Angel One SmartAPI integration for NSE market data.

Auth: TOTP-based (auto-generated from ANGEL_TOTP_TOKEN in .env).
No manual OAuth flow. Bot auto-logins at startup each morning.
Historical API returns rows as [timestamp, open, high, low, close, volume].
Option tokens looked up from Angel One master file (cached daily).

Instrument tokens (NSE index spot — stable, don't change):
  NIFTY 50  : 99926000
  BANK NIFTY: 99926009
"""

import logging
import os
import threading
from datetime import datetime, date, timedelta
from typing import Optional

import numpy as np
import config

logger = logging.getLogger(__name__)

_SPOT_TOKENS = {
    "NIFTY":      {"token": "99926000", "tradingsymbol": "Nifty 50",   "exchange": "NSE"},
    "BANKNIFTY":  {"token": "99926009", "tradingsymbol": "Nifty Bank",  "exchange": "NSE"},
}

_INTERVAL_MAP = {
    "1m":  "ONE_MINUTE",
    "3m":  "THREE_MINUTE",
    "5m":  "FIVE_MINUTE",
    "10m": "TEN_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "1h":  "SIXTY_MINUTE",
    "1d":  "ONE_DAY",
}

_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"


class AngelFetcher:
    """
    Singleton. Logs in once via TOTP (auto-generated), reuses session all day.
    Thread-safe singleton.
    """

    _instance: Optional["AngelFetcher"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._api = None
        self._login_date: Optional[date] = None
        self._failed_at: Optional[datetime] = None   # datetime of last failure; retry after 10 min
        self._instruments: Optional[list] = None
        self._instruments_date: Optional[date] = None

    @classmethod
    def get(cls) -> "AngelFetcher":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def _read_env() -> dict:
        from dotenv import dotenv_values
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        v = dotenv_values(env_path)
        return {
            "api_key":       v.get("ANGEL_API_KEY", "")       or getattr(config, "ANGEL_API_KEY", ""),
            "client_id":     v.get("ANGEL_CLIENT_ID", "")     or getattr(config, "ANGEL_CLIENT_ID", ""),
            "password":      v.get("ANGEL_PASSWORD", "")      or getattr(config, "ANGEL_PASSWORD", ""),
            "totp_token":    v.get("ANGEL_TOTP_TOKEN", "")    or getattr(config, "ANGEL_TOTP_TOKEN", ""),
            "jwt_token":     v.get("ANGEL_JWT_TOKEN", ""),
            "refresh_token": v.get("ANGEL_REFRESH_TOKEN", ""),
            "feed_token":    v.get("ANGEL_FEED_TOKEN", ""),
        }

    _LOGIN_RETRY_SECS = 600  # retry failed login after 10 minutes

    def _ensure_logged_in(self) -> bool:
        """
        Auto-login using TOTP (no manual step needed).
        Tries stored JWT first; falls back to full generateSession if expired.
        Failed logins are retried after 10 min (not blocked all day).
        """
        from core.utils import now_ist
        now = now_ist()
        today = now.date()

        if self._api is not None and self._login_date == today:
            return True
        if (self._failed_at is not None and
                (now - self._failed_at).total_seconds() < self._LOGIN_RETRY_SECS):
            return False  # within cooldown window, skip retry

        with self._lock:
            if self._api is not None and self._login_date == today:
                return True
            if (self._failed_at is not None and
                    (now - self._failed_at).total_seconds() < self._LOGIN_RETRY_SECS):
                return False

            creds = self._read_env()
            api_key    = creds["api_key"]
            client_id  = creds["client_id"]
            password   = creds["password"]
            totp_token = creds["totp_token"]

            if not all([api_key, client_id, password, totp_token]):
                logger.error(
                    "AngelFetcher: ANGEL_API_KEY / ANGEL_CLIENT_ID / ANGEL_PASSWORD / "
                    "ANGEL_TOTP_TOKEN must all be set in .env"
                )
                self._failed_at = now   # missing creds → retry after 10 min
                return False

            try:
                import pyotp
                from SmartApi import SmartConnect

                api = SmartConnect(api_key=api_key)

                # Try stored JWT first — avoids TOTP call on container restart
                stored_jwt = creds.get("jwt_token", "")
                stored_refresh = creds.get("refresh_token", "")
                if stored_jwt:
                    try:
                        api.setAccessToken(stored_jwt)
                        profile_resp = api.getProfile(stored_refresh)
                        if profile_resp and (profile_resp.get("status") or profile_resp.get("success")):
                            self._api = api
                            self._login_date = today
                            self._failed_at = None
                            logger.info("AngelFetcher: reused stored JWT for %s", client_id)
                            return True
                        else:
                            logger.info("AngelFetcher: stored JWT invalid (api response failed), generating new session via TOTP")
                    except Exception:
                        logger.info("AngelFetcher: stored JWT invalid (exception), generating new session via TOTP")

                # Auto-generate TOTP and create fresh session
                totp_code = pyotp.TOTP(totp_token).now()
                data = api.generateSession(client_id, password, totp_code)

                if not data or not (data.get("status") or data.get("success")):
                    msg = (data or {}).get("message", "Unknown login error")
                    logger.error("AngelFetcher: generateSession failed: %s", msg)
                    self._failed_at = now   # retry after 10 min
                    return False

                session = data["data"]
                self._api = api
                self._login_date = today
                self._failed_at = None
                self._save_tokens(
                    session["jwtToken"],
                    session["refreshToken"],
                    session["feedToken"],
                )
                logger.info("AngelFetcher: new session created for %s", client_id)
                return True

            except Exception as e:
                logger.error("AngelFetcher login failed: %s", e)
                self._api = None
                self._failed_at = now   # retry after 10 min
                return False

    def _save_tokens(self, jwt: str, refresh: str, feed: str):
        """Persist session tokens to .env so container restarts don't need a new TOTP."""
        from datetime import timezone, timedelta as td
        set_at = datetime.now(timezone(td(hours=5, minutes=30))).isoformat()
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        try:
            lines = open(env_path).readlines() if os.path.exists(env_path) else []
            updates = {
                "ANGEL_JWT_TOKEN":     jwt,
                "ANGEL_REFRESH_TOKEN": refresh,
                "ANGEL_FEED_TOKEN":    feed,
                "ANGEL_TOKEN_SET_AT":  set_at,
            }
            new_lines, found = [], set()
            for line in lines:
                key = line.split("=")[0].strip()
                if key in updates:
                    new_lines.append(f"{key}={updates[key]}\n")
                    found.add(key)
                else:
                    new_lines.append(line)
            for k, v in updates.items():
                if k not in found:
                    new_lines.append(f"{k}={v}\n")
            open(env_path, "w").writelines(new_lines)
        except Exception as e:
            logger.warning("AngelFetcher: could not persist tokens to .env: %s", e)

    def is_token_live(self) -> bool:
        """Check session liveness using internal state only.
        Never calls getProfile() — that hits the rate limit and causes TOKEN EXPIRED false positives."""
        from core.utils import now_ist
        today = now_ist().date()
        return self._api is not None and self._login_date == today

    # ── Candle helpers ────────────────────────────────────────────────────────

    def _invalidate_token(self):
        """Force re-login on the next _ensure_logged_in() call."""
        with self._lock:
            self._api = None
            self._login_date = None
            self._failed_at = None

    def _candle_data(self, token: str, exchange: str, angel_interval: str,
                     from_dt: str, to_dt: str) -> Optional[list]:
        """Call getCandleData; return raw rows [[ts, o, h, l, c, v], ...] or None."""
        try:
            resp = self._api.getCandleData({
                "exchange":    exchange,
                "symboltoken": token,
                "interval":    angel_interval,
                "fromdate":    from_dt,
                "todate":      to_dt,
            })
            if resp and resp.get("status") and resp.get("data"):
                return resp["data"]
            if resp and resp.get("errorCode") == "AG8001":
                logger.warning("AngelFetcher._candle_data: token expired (AG8001), forcing re-login")
                self._invalidate_token()
            logger.warning("AngelFetcher._candle_data empty response for %s %s", token, angel_interval)
            return None
        except Exception as e:
            logger.warning("AngelFetcher._candle_data: %s", e)
            self._api = None
            return None

    @staticmethod
    def _rows_to_arrays(rows: list):
        """Convert raw rows to numpy arrays (opens, highs, lows, closes, volumes)."""
        opens = np.array([float(r[1]) for r in rows])
        highs = np.array([float(r[2]) for r in rows])
        lows  = np.array([float(r[3]) for r in rows])
        closes= np.array([float(r[4]) for r in rows])
        vols  = np.array([float(r[5]) for r in rows])
        return opens, highs, lows, closes, vols

    @staticmethod
    def _rows_to_df(rows: list):
        """Convert raw rows to pandas DataFrame with datetime index (IST, tz-naive)."""
        import pandas as pd
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        records = []
        for r in rows:
            try:
                dt = pd.to_datetime(r[0])
                if dt.tzinfo is not None:
                    dt = dt.tz_convert(IST).tz_localize(None)
            except Exception:
                dt = pd.Timestamp(r[0])
            records.append({
                "Date":   dt,
                "Open":   float(r[1]), "High": float(r[2]),
                "Low":    float(r[3]), "Close": float(r[4]),
                "Volume": float(r[5]),
            })
        if not records:
            return None
        df = pd.DataFrame(records).set_index("Date")
        df["_date"] = df.index.date
        return df

    # ── Public fetch API ──────────────────────────────────────────────────────

    def fetch_intraday(self, symbol: str, interval: str):
        """
        Fetch today's intraday bars + 60-day daily closes.
        Returns (opens, highs, lows, closes, volumes, all_closes, bar_time) or None.
        """
        if not self._ensure_logged_in():
            return None
        spot = _SPOT_TOKENS.get(symbol)
        angel_interval = _INTERVAL_MAP.get(interval)
        if spot is None or angel_interval is None:
            logger.warning("AngelFetcher.fetch_intraday: unknown symbol/interval %s %s", symbol, interval)
            return None
        try:
            from zoneinfo import ZoneInfo
            import pandas as pd
            IST = ZoneInfo("Asia/Kolkata")
            now = datetime.now(IST)
            today = now.strftime("%Y-%m-%d")

            rows = self._candle_data(spot["token"], spot["exchange"], angel_interval,
                                     f"{today} 09:15", f"{today} 15:30")
            if not rows or len(rows) < 3:
                logger.warning("AngelFetcher: insufficient intraday bars for %s (%d)",
                               symbol, len(rows) if rows else 0)
                return None

            opens, highs, lows, closes, volumes = self._rows_to_arrays(rows)
            try:
                last_dt = pd.to_datetime(rows[-1][0])
                if last_dt.tzinfo is not None:
                    last_dt = last_dt.tz_convert(IST)
                bar_time = last_dt.time()
            except Exception:
                bar_time = now.time()

            sixty_ago = (now - timedelta(days=65)).strftime("%Y-%m-%d")
            daily_rows = self._candle_data(spot["token"], spot["exchange"], "ONE_DAY",
                                           f"{sixty_ago} 09:15", f"{today} 15:30")
            all_closes = np.array([float(r[4]) for r in daily_rows]) if daily_rows and len(daily_rows) >= 5 else closes

            logger.info("AngelFetcher: %s %s → %d intraday, %d daily bars, last %s",
                        symbol, interval, len(closes), len(all_closes), bar_time)
            return opens, highs, lows, closes, volumes, all_closes, bar_time

        except Exception as e:
            logger.error("AngelFetcher.fetch_intraday %s %s: %s", symbol, interval, e)
            self._api = None
            return None

    def fetch_intraday_df(self, symbol: str, interval: str):
        """Fetch today's intraday bars as a pandas DataFrame. Returns None on failure."""
        if not self._ensure_logged_in():
            return None
        spot = _SPOT_TOKENS.get(symbol)
        angel_interval = _INTERVAL_MAP.get(interval)
        if spot is None or angel_interval is None:
            return None
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("Asia/Kolkata"))
            today = now.strftime("%Y-%m-%d")
            rows = self._candle_data(spot["token"], spot["exchange"], angel_interval,
                                     f"{today} 09:15", f"{today} 15:30")
            if not rows or len(rows) < 3:
                return None
            return self._rows_to_df(rows)
        except Exception as e:
            logger.error("AngelFetcher.fetch_intraday_df %s %s: %s", symbol, interval, e)
            self._api = None
            return None

    def fetch_historical_df(self, symbol: str, interval: str, days: int = 60):
        """
        Fetch multiple days of intraday bars — backtest engine + multi-day scoring.
        Returns pandas DataFrame with Open/High/Low/Close/Volume columns, or None.
        """
        if not self._ensure_logged_in():
            return None
        spot = _SPOT_TOKENS.get(symbol)
        angel_interval = _INTERVAL_MAP.get(interval)
        if spot is None or angel_interval is None:
            return None
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("Asia/Kolkata"))
            from_d = (now - timedelta(days=days + 2)).strftime("%Y-%m-%d")
            to_d   = now.strftime("%Y-%m-%d")
            rows = self._candle_data(spot["token"], spot["exchange"], angel_interval,
                                     f"{from_d} 09:15", f"{to_d} 15:30")
            if not rows or len(rows) < 10:
                logger.warning("AngelFetcher.fetch_historical_df: only %d bars for %s %s",
                               len(rows) if rows else 0, symbol, interval)
                return None
            df = self._rows_to_df(rows)
            logger.info("AngelFetcher.fetch_historical_df: %s %s → %d bars over %d days",
                        symbol, interval, len(df), len(df["_date"].unique()))
            return df
        except Exception as e:
            logger.error("AngelFetcher.fetch_historical_df %s %s: %s", symbol, interval, e)
            self._api = None
            return None

    def fetch_daily_df(self, symbol: str, days: int = 90):
        """Fetch daily OHLCV bars as a pandas DataFrame. Returns None on failure."""
        if not self._ensure_logged_in():
            return None
        spot = _SPOT_TOKENS.get(symbol)
        if spot is None:
            return None
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("Asia/Kolkata"))
            from_d = (now - timedelta(days=days + 5)).strftime("%Y-%m-%d")
            to_d   = now.strftime("%Y-%m-%d")
            rows = self._candle_data(spot["token"], spot["exchange"], "ONE_DAY",
                                     f"{from_d} 09:15", f"{to_d} 15:30")
            if not rows or len(rows) < 5:
                logger.error("AngelFetcher.fetch_daily_df %s: only %d rows", symbol, len(rows) if rows else 0)
                return None
            df = self._rows_to_df(rows)
            df.drop(columns=["_date"], errors="ignore", inplace=True)
            logger.info("AngelFetcher: %s daily → %d bars", symbol, len(df))
            return df
        except Exception as e:
            logger.error("AngelFetcher.fetch_daily_df %s: %s", symbol, e)
            self._api = None
            return None

    # ── VIX ──────────────────────────────────────────────────────────────────

    def fetch_vix(self) -> Optional[float]:
        """Fetch India VIX live price. Returns None if unavailable."""
        if not self._ensure_logged_in():
            return None
        try:
            resp = self._api.ltpData(exchange="NSE", tradingsymbol="India VIX",
                                     symboltoken="99919000")
            if resp and resp.get("status") and resp.get("data"):
                vix = float(resp["data"].get("ltp", 0))
                if vix > 0:
                    logger.info("India VIX: %.2f", vix)
                    return vix
            if resp and resp.get("errorCode") == "AG8001":
                self._invalidate_token()
        except Exception as e:
            logger.warning("AngelFetcher.fetch_vix: %s", e)
        return None

    def fetch_vix_historical_df(self, days: int = 100):
        """Fetch historical daily VIX. Returns DataFrame with 'vix' column or None."""
        if not self._ensure_logged_in():
            return None
        try:
            import pandas as pd
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("Asia/Kolkata"))
            from_d = (now - timedelta(days=days + 5)).strftime("%Y-%m-%d")
            to_d   = now.strftime("%Y-%m-%d")
            rows = self._candle_data("99919000", "NSE", "ONE_DAY",
                                     f"{from_d} 09:15", f"{to_d} 15:30")
            if not rows:
                return None
            df = pd.DataFrame([[r[0], float(r[4])] for r in rows], columns=["date", "vix"])
            df["date"] = pd.to_datetime(df["date"]).dt.date
            return df.set_index("date")
        except Exception as e:
            logger.warning("AngelFetcher.fetch_vix_historical_df: %s", e)
            return None

    # ── Index LTP (for MockBroker paper trading) ──────────────────────────────

    def get_index_ltp(self, symbol: str) -> Optional[float]:
        """Return live spot price for NIFTY/BANKNIFTY."""
        if not self._ensure_logged_in():
            return None
        spot = _SPOT_TOKENS.get(symbol)
        if not spot:
            return None
        try:
            resp = self._api.ltpData(
                exchange=spot["exchange"],
                tradingsymbol=spot["tradingsymbol"],
                symboltoken=spot["token"],
            )
            if resp and resp.get("status") and resp.get("data"):
                ltp = float(resp["data"].get("ltp", 0))
                return ltp if ltp > 0 else None
            if resp and resp.get("errorCode") == "AG8001":
                self._invalidate_token()
        except Exception as e:
            logger.warning("AngelFetcher.get_index_ltp %s: %s", symbol, e)
        return None

    # ── Options helpers ───────────────────────────────────────────────────────

    def _nfo_instruments(self) -> list:
        """Return cached NIFTY/BANKNIFTY OPTIDX instruments from Angel One master file."""
        from core.utils import now_ist
        today = now_ist().date()
        if self._instruments_date == today and self._instruments is not None:
            return self._instruments
        try:
            import requests
            logger.info("AngelFetcher: downloading instrument master from Angel One CDN…")
            resp = requests.get(_MASTER_URL, timeout=30)
            resp.raise_for_status()
            all_inst = resp.json()
            self._instruments = [
                i for i in all_inst
                if i.get("name") in ("NIFTY", "BANKNIFTY")
                and i.get("instrumenttype") == "OPTIDX"
                and i.get("exch_seg") == "NFO"
            ]
            self._instruments_date = today
            logger.info("AngelFetcher: cached %d NIFTY/BANKNIFTY instruments", len(self._instruments))
        except Exception as e:
            logger.error("AngelFetcher: instrument master download failed: %s", e)
            if self._instruments is None:
                self._instruments = []
        return self._instruments

    @classmethod
    def nearest_weekly_expiry(cls) -> date:
        """Return nearest tradable NIFTY expiry from Angel One instrument master."""
        from core.utils import now_ist
        now = now_ist()
        today = now.date()
        inst = cls.get()
        try:
            if inst._ensure_logged_in():
                expiries = sorted({
                    _parse_expiry(i["expiry"])
                    for i in inst._nfo_instruments()
                    if i.get("name") == "NIFTY" and i.get("expiry")
                    and _parse_expiry(i["expiry"]) is not None
                    and _parse_expiry(i["expiry"]) >= today
                })
                if expiries:
                    if now.hour > 15 or (now.hour == 15 and now.minute >= 30):
                        future = [e for e in expiries if e > today]
                        if future:
                            return future[0]
                    return expiries[0]
        except Exception as e:
            logger.warning("nearest_weekly_expiry: %s", e)
        days_to_thu = (3 - today.weekday()) % 7
        if days_to_thu == 0 and (now.hour > 15 or (now.hour == 15 and now.minute >= 30)):
            days_to_thu = 7
        return today + timedelta(days=days_to_thu)

    def get_option_ltp(self, symbol: str, strike: int, option_type: str, expiry: date):
        """
        Return (tradingsymbol, last_traded_price) for a NIFTY/BN option.
        Returns (None, None) on failure.
        """
        if not self._ensure_logged_in():
            return None, None
        try:
            instruments = self._nfo_instruments()
            target_expiry = _format_expiry(expiry)

            match = next((
                i for i in instruments
                if i.get("name") == symbol
                and int(float(i.get("strike", 0))) == strike
                and i.get("instrumenttype") == "OPTIDX"
                and i.get("symbol", "").endswith(option_type)
                and i.get("expiry", "") == target_expiry
            ), None)

            if match is None:
                today = date.today()
                candidates = sorted([
                    i for i in instruments
                    if i.get("name") == symbol
                    and int(float(i.get("strike", 0))) == strike
                    and i.get("symbol", "").endswith(option_type)
                    and _parse_expiry(i.get("expiry", "")) is not None
                    and _parse_expiry(i.get("expiry", "")) >= today
                ], key=lambda i: _parse_expiry(i["expiry"]))
                if candidates:
                    match = candidates[0]
                    logger.warning("get_option_ltp: using fallback expiry for %s %d%s", symbol, strike, option_type)

            if match is None:
                logger.warning("get_option_ltp: no instrument for %s %s %d %s",
                               symbol, expiry, strike, option_type)
                return None, None

            tradingsymbol = match["symbol"]
            token = match["token"]
            resp = self._api.ltpData(exchange="NFO", tradingsymbol=tradingsymbol, symboltoken=token)
            if not resp or not resp.get("status") or not resp.get("data"):
                logger.warning("get_option_ltp: LTP API failed for %s", tradingsymbol)
                return tradingsymbol, None
            ltp = float(resp["data"].get("ltp", 0))
            logger.info("get_option_ltp: %s = ₹%.2f", tradingsymbol, ltp)
            return tradingsymbol, (ltp if ltp > 0 else None)

        except Exception as e:
            logger.error("AngelFetcher.get_option_ltp %s %d%s: %s", symbol, strike, option_type, e)
            self._api = None
            return None, None

    def get_option_token(self, tradingsymbol: str) -> Optional[str]:
        """Look up Angel One symboltoken for a given NFO tradingsymbol."""
        match = next((i for i in self._nfo_instruments() if i.get("symbol") == tradingsymbol), None)
        return match["token"] if match else None

    # ── NSE equity support (kept for API compat) ──────────────────────────────

    def fetch_equity_daily(self, symbol: str, days: int = 365):
        """Fetch daily OHLCV for an NSE equity. Returns DataFrame or None."""
        if not self._ensure_logged_in():
            return None
        try:
            import requests
            logger.info("AngelFetcher.fetch_equity_daily: searching scrip %s", symbol)
            resp_s = self._api.searchScrip(exchange="NSE", searchscrip=symbol)
            if not resp_s or not resp_s.get("status") or not resp_s.get("data"):
                return None
            token = resp_s["data"][0]["symboltoken"]
            tradingsymbol = resp_s["data"][0]["tradingsymbol"]
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("Asia/Kolkata"))
            from_d = (now - timedelta(days=days + 10)).strftime("%Y-%m-%d")
            to_d   = now.strftime("%Y-%m-%d")
            rows = self._candle_data(token, "NSE", "ONE_DAY", f"{from_d} 09:15", f"{to_d} 15:30")
            if not rows or len(rows) < 10:
                return None
            df = self._rows_to_df(rows)
            df.drop(columns=["_date"], errors="ignore", inplace=True)
            return df
        except Exception as e:
            logger.error("AngelFetcher.fetch_equity_daily %s: %s", symbol, e)
            self._api = None
            return None


# ── Expiry date helpers ───────────────────────────────────────────────────────

def _parse_expiry(s: str) -> Optional[date]:
    """Parse Angel One expiry string '24APR2024' → date. Returns None on error."""
    if not s or len(s) < 9:
        return None
    for fmt in ("%d%b%Y", "%d%b%y"):
        try:
            return datetime.strptime(s.upper(), fmt).date()
        except ValueError:
            continue
    return None


def _format_expiry(d: date) -> str:
    """Format date → Angel One expiry string '24APR2024'."""
    return d.strftime("%d%b%Y").upper()
