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

# Suppress noisy internal ERROR logs from smartapi-python for non-critical failures
# (e.g. VIX token lookup, scrip master misses). Real auth/order errors still surface.
logging.getLogger("smartConnect").setLevel(logging.CRITICAL)
import os
import threading
from datetime import datetime, date, timedelta
from typing import Optional

import numpy as np
import config

logger = logging.getLogger(__name__)

_SPOT_TOKENS = {
    "NIFTY":      {"token": "99926000", "tradingsymbol": "Nifty 50",          "exchange": "NSE"},
    "BANKNIFTY":  {"token": "99926009", "tradingsymbol": "Nifty Bank",        "exchange": "NSE"},
    "FINNIFTY":   {"token": "99926037", "tradingsymbol": "Nifty Fin Service",  "exchange": "NSE"},
    "SENSEX":     {"token": "1",        "tradingsymbol": "SENSEX",             "exchange": "BSE"},
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
        self._candle_failures = 0  # consecutive empty/None candle responses

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

    _LOGIN_RETRY_SECS = 120  # retry failed login after 2 minutes

    def _ensure_logged_in(self, force: bool = False) -> bool:
        """
        Auto-login using TOTP (no manual step needed).
        Tries stored JWT first; falls back to full generateSession if expired.
        Failed logins are retried after 2 min (not blocked all day).

        Set force=True to bypass the cooldown on user-initiated actions.
        """
        from core.utils import now_ist
        now = now_ist()
        today = now.date()

        if self._api is not None and self._login_date == today:
            return True
        if not force and (self._failed_at is not None and
                (now - self._failed_at).total_seconds() < self._LOGIN_RETRY_SECS):
            return False  # within cooldown window, skip retry

        with self._lock:
            if self._api is not None and self._login_date == today:
                return True
            if not force and (self._failed_at is not None and
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

                # Angel's python SDK currently points GTT create/modify/cancel at a
                # `/gtt-service/...` prefix that the gateway rejects with
                # "no Route matched with those values".  Fall back to the legacy
                # paths that still work.
                SmartConnect._routes["api.gtt.create"] = "/rest/secure/angelbroking/gtt/v1/createRule"
                SmartConnect._routes["api.gtt.modify"] = "/rest/secure/angelbroking/gtt/v1/modifyRule"
                SmartConnect._routes["api.gtt.cancel"] = "/rest/secure/angelbroking/gtt/v1/cancelRule"

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
                self._candle_failures = 0
                return resp["data"]

            err_code = (resp.get("errorCode") or "") if resp else ""
            err_msg  = (resp.get("message")   or "").lower() if resp else ""
            auth_err = (
                err_code == "AG8001"
                or "invalid token" in err_msg
                or "session"       in err_msg
                or "unauthorized"  in err_msg
                or "expired"       in err_msg
            )
            if auth_err:
                logger.warning("AngelFetcher._candle_data: auth error (code=%s), forcing re-login", err_code or err_msg)
                self._invalidate_token()
                self._candle_failures = 0
            else:
                self._candle_failures += 1
                logger.warning(
                    "AngelFetcher._candle_data empty response for %s %s (code=%s, fail#%d)",
                    token, angel_interval, err_code or "none", self._candle_failures,
                )
                if self._candle_failures >= 3:
                    logger.warning("AngelFetcher._candle_data: 3 consecutive failures — forcing re-login")
                    self._invalidate_token()
                    self._candle_failures = 0
            return None
        except Exception as e:
            msg = str(e)
            logger.warning("AngelFetcher._candle_data: %s", msg)
            # Invalidate session on auth errors AND on JSON parse failures
            # (Angel One returns HTML when session expires — smartapi raises parse error)
            if ("Invalid Token" in msg or "Unauthorized" in msg or "AG8001" in msg
                    or "parse" in msg.lower() or "json" in msg.lower()):
                self._invalidate_token()
                self._candle_failures = 0
            else:
                self._candle_failures += 1
                if self._candle_failures >= 3:
                    logger.warning("AngelFetcher._candle_data: 3 consecutive exceptions — forcing re-login")
                    self._invalidate_token()
                    self._candle_failures = 0
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
        for token in ("99919000", "99919003"):
            try:
                resp = self._api.ltpData(exchange="NSE", tradingsymbol="India VIX",
                                         symboltoken=token)
                if resp and resp.get("status") and resp.get("data"):
                    vix = float(resp["data"].get("ltp", 0))
                    if vix > 0:
                        logger.info("India VIX: %.2f (token %s)", vix, token)
                        return vix
                if resp and resp.get("errorCode") == "AG8001":
                    self._invalidate_token()
                    return None
                err = resp.get("errorcode", "") if resp else ""
                if err == "AB4046":
                    logger.debug("VIX token %s not in scrip master, trying next", token)
                    continue
            except Exception as e:
                logger.debug("AngelFetcher.fetch_vix token %s: %s", token, e)
        logger.debug("India VIX unavailable — both tokens failed")
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
            rows = (self._candle_data("99919000", "NSE", "ONE_DAY",
                                     f"{from_d} 09:15", f"{to_d} 15:30")
                    or self._candle_data("99919003", "NSE", "ONE_DAY",
                                        f"{from_d} 09:15", f"{to_d} 15:30"))
            if not rows:
                return None
            df = pd.DataFrame([[r[0], float(r[4])] for r in rows], columns=["date", "vix"])
            df["date"] = pd.to_datetime(df["date"]).dt.date
            return df.set_index("date")
        except Exception as e:
            logger.warning("AngelFetcher.fetch_vix_historical_df: %s", e)
            return None

    # ── Index LTP (NIFTY / BANKNIFTY spot price) ──────────────────────────────

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
            # Detect token-expiry-in-body and retry once with a fresh session.
            if self._is_auth_failure(resp):
                if self._ensure_logged_in():
                    resp = self._api.ltpData(
                        exchange=spot["exchange"],
                        tradingsymbol=spot["tradingsymbol"],
                        symboltoken=spot["token"],
                    )
            if resp and resp.get("status") and resp.get("data"):
                ltp = float(resp["data"].get("ltp", 0))
                return ltp if ltp > 0 else None
        except Exception as e:
            logger.warning("AngelFetcher.get_index_ltp %s: %s", symbol, e)
        return None

    # ── Options helpers ───────────────────────────────────────────────────────

    def _nfo_instruments(self) -> list:
        """Return cached NFO OPTIDX instruments (NIFTY/BANKNIFTY/FINNIFTY)."""
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
                if i.get("name") in ("NIFTY", "BANKNIFTY", "FINNIFTY")
                and i.get("instrumenttype") == "OPTIDX"
                and i.get("exch_seg") == "NFO"
            ]
            self._instruments_date = today
            logger.info("AngelFetcher: cached %d NFO instruments", len(self._instruments))
        except Exception as e:
            logger.error("AngelFetcher: instrument master download failed: %s", e)
            if self._instruments is None:
                self._instruments = []
        return self._instruments

    def _bfo_instruments(self) -> list:
        """Return cached BFO OPTIDX instruments (SENSEX) from Angel One master file."""
        from core.utils import now_ist
        today = now_ist().date()
        if hasattr(self, "_bfo_instruments_date") and self._bfo_instruments_date == today \
                and self._bfo_instruments_cache is not None:
            return self._bfo_instruments_cache
        try:
            import requests
            resp = requests.get(_MASTER_URL, timeout=30)
            resp.raise_for_status()
            all_inst = resp.json()
            self._bfo_instruments_cache = [
                i for i in all_inst
                if i.get("name") == "SENSEX"
                and i.get("instrumenttype") == "OPTIDX"
                and i.get("exch_seg") == "BFO"
            ]
            self._bfo_instruments_date = today
            logger.info("AngelFetcher: cached %d BFO instruments", len(self._bfo_instruments_cache))
        except Exception as e:
            logger.error("AngelFetcher: BFO instrument master download failed: %s", e)
            if not hasattr(self, "_bfo_instruments_cache") or self._bfo_instruments_cache is None:
                self._bfo_instruments_cache = []
        return self._bfo_instruments_cache

    @classmethod
    def nearest_weekly_expiry(cls) -> date:
        """Return nearest tradable NIFTY expiry from Angel One instrument master.

        Uses the master file as the authoritative source — no weekday filter,
        so holiday-moved expiries (e.g. Tuesday when Thursday is a market holiday)
        are handled correctly.
        """
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

    def _is_auth_failure(self, resp) -> bool:
        """Detect token-expiry / unauthorized in an Angel One response body.

        Angel One returns auth errors as HTTP 200 with a body like:
            {"success": False, "errorCode": "AG8001", "message": "Invalid Token"}
        — NOT as an exception. Without this check, ltpData / getMarketData /
        ltpData calls silently return None for the rest of the day after the
        24-hour JWT expires, even though _ensure_logged_in() thinks we're
        still logged in (it only checks the calendar date, not actual token
        validity). When this helper detects an auth failure it invalidates
        the cached _api so the NEXT call hits _ensure_logged_in() and forces
        a fresh TOTP re-login.
        """
        if not resp or not isinstance(resp, dict):
            return False
        ec  = str(resp.get("errorCode", "") or "")
        msg = str(resp.get("message", "") or "").lower()
        if (ec == "AG8001"
            or "invalid token" in msg
            or "unauthorized" in msg
            or "session expired" in msg):
            logger.warning(
                "Angel One auth failure detected in response (errorCode=%s msg=%s) "
                "— invalidating session so next call re-logs in",
                ec, resp.get("message"),
            )
            self._invalidate_token()
            return True
        return False

    def get_option_ltp(self, symbol: str, strike: int, option_type: str, expiry: date):
        """
        Return (tradingsymbol, last_traded_price) for a NIFTY/BN option.
        Returns (None, None) on failure.
        """
        if not self._ensure_logged_in():
            return None, None
        try:
            instruments = self._nfo_instruments()

            # Angel One master stores strike * 100 (e.g. 24550 → 2455000). Divide by 100 to compare.
            def _master_strike(i) -> int:
                return int(float(i.get("strike", 0))) // 100

            match = next((
                i for i in instruments
                if i.get("name") == symbol
                and _master_strike(i) == strike
                and i.get("instrumenttype") == "OPTIDX"
                and i.get("symbol", "").endswith(option_type)
                and _parse_expiry(i.get("expiry", "")) == expiry
            ), None)

            if match is None:
                # Exact strike not found — find nearest available strike for this expiry.
                # Hard limit: only accept strikes within 200 pts of requested (prevents deep ITM/OTM accidents).
                available = [
                    _master_strike(i)
                    for i in instruments
                    if i.get("name") == symbol
                    and i.get("instrumenttype") == "OPTIDX"
                    and i.get("symbol", "").endswith(option_type)
                    and _parse_expiry(i.get("expiry", "")) == expiry
                ]
                logger.info("get_option_ltp: available strikes for %s %s %s: %s",
                            symbol, expiry, option_type, sorted(available))
                if available:
                    nearest = min(available, key=lambda s: abs(s - strike))
                    deviation = abs(nearest - strike)
                    if deviation > 200:
                        logger.error(
                            "get_option_ltp: nearest strike %d is %d pts from %d for %s %s — refusing (max 200)",
                            nearest, deviation, strike, symbol, expiry,
                        )
                        return None, None
                    logger.warning(
                        "get_option_ltp: strike %d not in master for %s %s — using nearest %d (diff %d pts)",
                        strike, symbol, expiry, nearest, deviation,
                    )
                    match = next((
                        i for i in instruments
                        if i.get("name") == symbol
                        and _master_strike(i) == nearest
                        and i.get("instrumenttype") == "OPTIDX"
                        and i.get("symbol", "").endswith(option_type)
                        and _parse_expiry(i.get("expiry", "")) == expiry
                    ), None)

            if match is None:
                # Master lookup exhausted — try searchScrip with exact tradingsymbol
                ts = f"{symbol}{expiry.strftime('%d%b%y').upper()}{strike}{option_type}"
                logger.info("get_option_ltp: master miss, trying searchScrip for %s", ts)
                try:
                    sr = self._api.searchScrip(exchange="NFO", searchscrip=ts)
                    if sr and sr.get("status") and sr.get("data"):
                        item = next(
                            (x for x in sr["data"] if x.get("tradingsymbol") == ts),
                            sr["data"][0],
                        )
                        match = {"symbol": item["tradingsymbol"], "token": item["symboltoken"]}
                        logger.info("get_option_ltp: searchScrip found %s", item["tradingsymbol"])
                except Exception as se:
                    logger.warning("get_option_ltp: searchScrip failed for %s: %s", ts, se)

            if match is None:
                logger.warning("get_option_ltp: no instrument for %s %s %d %s",
                               symbol, expiry, strike, option_type)
                return None, None

            tradingsymbol = match["symbol"]
            token = match["token"]
            resp = self._api.ltpData(exchange="NFO", tradingsymbol=tradingsymbol, symboltoken=token)

            # Detect token expiry returned in response body (not as exception).
            # If detected, _api is invalidated → next call re-logs in. Retry
            # this single call once with the fresh session.
            if self._is_auth_failure(resp):
                if self._ensure_logged_in():
                    resp = self._api.ltpData(exchange="NFO", tradingsymbol=tradingsymbol, symboltoken=token)

            if not resp or not resp.get("status") or not resp.get("data"):
                logger.warning("get_option_ltp: LTP API failed for %s", tradingsymbol)
                return tradingsymbol, None
            ltp = float(resp["data"].get("ltp", 0))
            logger.info("get_option_ltp: %s = ₹%.2f", tradingsymbol, ltp)
            return tradingsymbol, (ltp if ltp > 0 else None)

        except Exception as e:
            msg = str(e)
            logger.error("AngelFetcher.get_option_ltp %s %d%s: %s", symbol, strike, option_type, msg)
            if "Invalid Token" in msg or "Unauthorized" in msg or "AG8001" in msg:
                self._api = None
            return None, None

    def get_option_token(self, tradingsymbol: str) -> Optional[str]:
        """Look up Angel One symboltoken for a given NFO tradingsymbol."""
        match = next((i for i in self._nfo_instruments() if i.get("symbol") == tradingsymbol), None)
        return match["token"] if match else None

    def get_option_quote(self, tradingsymbol: str, token: str, exchange: str = "NFO") -> Optional[dict]:
        """Fetch FULL market-data quote for a single option. Returns dict with
        ltp, bid, ask, volume, oi, etc., or None on failure."""
        if not self._ensure_logged_in():
            return None
        try:
            resp = self._api.getMarketData("FULL", {exchange: [token]})
            if self._is_auth_failure(resp):
                if self._ensure_logged_in():
                    resp = self._api.getMarketData("FULL", {exchange: [token]})
            if not resp or not resp.get("status"):
                logger.warning("get_option_quote: getMarketData failed for %s: %s", tradingsymbol, resp)
                return None
            fetched = resp.get("data", {}).get("fetched", [])
            if not fetched:
                return None
            q = fetched[0]
            return {
                "ltp": float(q.get("ltp", 0) or 0),
                "bid": float(q.get("bidPrice", 0) or 0),
                "ask": float(q.get("askPrice", 0) or 0),
                "volume": int(q.get("tradeVolume", 0) or 0),
                "oi": int(q.get("opnInterest", 0) or 0),
                "open": float(q.get("open", 0) or 0),
                "high": float(q.get("high", 0) or 0),
                "low": float(q.get("low", 0) or 0),
                "close": float(q.get("close", 0) or 0),
            }
        except Exception as e:
            logger.warning("get_option_quote %s: %s", tradingsymbol, e)
            return None

    def gtt_create_rule(self, tradingsymbol: str, token: str, exchange: str,
                        transactiontype: str, producttype: str, qty: int,
                        triggerprice: float, price: float,
                        disclosedqty: int = 0, timeperiod: int = 365) -> Optional[dict]:
        """Create a GTT rule (SL / target). Returns API response dict or None."""
        if not self._ensure_logged_in():
            return None
        try:
            payload = {
                "tradingsymbol": tradingsymbol,
                "symboltoken": token,
                "exchange": exchange,
                "producttype": producttype,
                "transactiontype": transactiontype,
                "price": str(price),
                "qty": str(qty),
                "disclosedqty": str(disclosedqty or qty),
                "triggerprice": str(triggerprice),
                "timeperiod": str(timeperiod),
            }
            resp = self._api._request("api.gtt.create", "POST", payload)
            if self._is_auth_failure(resp):
                if self._ensure_logged_in():
                    resp = self._api._request("api.gtt.create", "POST", payload)
            return resp
        except Exception as e:
            logger.error("gtt_create_rule failed: %s", e)
            return None

    def get_option_ltps_bulk(self, symbol: str, strikes: list[int],
                             option_type: str, expiry: date) -> dict[int, tuple[str, float]]:
        """Fetch LTPs for many strikes in ONE API call via getMarketData.

        Returns {strike: (tradingsymbol, ltp)} for every strike that resolved.

        Why this exists: the strategy's strike-search walks ATM ± 10 strikes
        looking for a premium in the target band. Doing that as 21 sequential
        ltpData calls hammers Angel One's per-second rate limit, causing
        roughly half the requests to return AB1004/silent-None during quiet
        periods — and the bot then falsely reports "Option LTP unavailable".
        getMarketData has a generous bulk allowance (the collector uses it to
        fetch 34 strikes every 5 min all session, ~2500 calls/day, no errors).
        """
        if not self._ensure_logged_in():
            return {}
        try:
            instruments = self._nfo_instruments()
            def _master_strike(i) -> int:
                return int(float(i.get("strike", 0))) // 100

            # Resolve strike → instrument row in the master.
            resolved: dict[int, dict] = {}
            for strike in strikes:
                m = next((
                    i for i in instruments
                    if i.get("name") == symbol
                    and _master_strike(i) == strike
                    and i.get("instrumenttype") == "OPTIDX"
                    and i.get("symbol", "").endswith(option_type)
                    and _parse_expiry(i.get("expiry", "")) == expiry
                ), None)
                if m:
                    resolved[strike] = m
            if not resolved:
                return {}

            tokens = [m["token"] for m in resolved.values()]
            resp = self._api.getMarketData("LTP", {"NFO": tokens})
            if self._is_auth_failure(resp):
                if self._ensure_logged_in():
                    resp = self._api.getMarketData("LTP", {"NFO": tokens})
            if not resp or not resp.get("status"):
                logger.warning("get_option_ltps_bulk: getMarketData failed: %s", resp)
                return {}

            # Build {token: ltp} from the fetched array
            quotes_by_token: dict[str, float] = {}
            for row in resp.get("data", {}).get("fetched", []):
                tok = str(row.get("symbolToken"))
                ltp = float(row.get("ltp", 0) or 0)
                if tok and ltp > 0:
                    quotes_by_token[tok] = ltp

            # Map back: strike → (tradingsymbol, ltp)
            out: dict[int, tuple[str, float]] = {}
            for strike, m in resolved.items():
                tok = str(m["token"])
                if tok in quotes_by_token:
                    out[strike] = (m["symbol"], quotes_by_token[tok])
            return out
        except Exception as e:
            logger.warning("get_option_ltps_bulk %s %s exp=%s: %s",
                           symbol, option_type, expiry, e)
            return {}

    def get_trade_book(self) -> list:
        """Fetch today's executed trades from Angel One tradeBook API.

        Returns list of dicts with keys: symbol, side, quantity, price, order_id, trade_time, exchange, product.
        Returns [] on error (never raises).
        """
        if not self._ensure_logged_in():
            return []
        try:
            resp = self._api.tradeBook()
            if not resp or not resp.get("status") or not resp.get("data"):
                return []
            trades = []
            for t in resp["data"]:
                trades.append({
                    "symbol":     t.get("tradingsymbol", ""),
                    "side":       "BUY" if t.get("transactiontype", "").upper() == "BUY" else "SELL",
                    "quantity":   int(t.get("quantity") or 0),
                    "price":      float(t.get("tradeprice") or 0),
                    "order_id":   t.get("orderid", ""),
                    "trade_time": t.get("tradetime", ""),
                    "exchange":   t.get("exchange", ""),
                    "product":    t.get("producttype", ""),
                })
            return trades
        except Exception as e:
            msg = str(e)
            logger.error("AngelFetcher.get_trade_book: %s", msg)
            if "Invalid Token" in msg or "Unauthorized" in msg or "AG8001" in msg or "parse" in msg.lower():
                self._invalidate_token()
            return []

    def get_margin_required(self, positions: list[dict]) -> Optional[float]:
        """Query Angel One margin API for a basket of positions.

        positions: list of dicts with keys:
            exchange, qty, price, productType, token, tradeType
        Returns totalMarginRequired (float) or None on failure.
        """
        if not positions:
            return 0.0
        if not self._ensure_logged_in():
            return None
        try:
            payload = {"positions": positions}
            resp = self._api.getMarginApi(payload)
            if not resp or not resp.get("status") or not resp.get("data"):
                logger.warning("get_margin_required: bad response: %s", resp)
                return None
            return float(resp["data"].get("totalMarginRequired", 0) or 0)
        except Exception as e:
            logger.error("get_margin_required failed: %s", e)
            return None

    def get_rms(self) -> Optional[dict]:
        """Fetch RMS limits from Angel One. Returns dict or None on failure."""
        if not self._ensure_logged_in():
            return None
        try:
            resp = self._api.rmsLimit()
            if not resp or not resp.get("status") or not resp.get("data"):
                logger.warning("get_rms: bad response: %s", resp)
                return None
            return resp["data"]
        except Exception as e:
            logger.error("get_rms failed: %s", e)
            return None

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
    """Parse Angel One expiry string. Handles both '24APR2024' (4-digit) and '24APR24' (2-digit) year."""
    if not s or len(s) < 5:
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
