import logging
from datetime import datetime, timedelta
from typing import Optional
import config

logger = logging.getLogger(__name__)

# yfinance symbol mapping for NSE
# Most stocks: append .NS  |  Indices: special tickers
_YF_MAP = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
}

def _yf_symbol(symbol: str) -> str:
    return _YF_MAP.get(symbol, f"{symbol}.NS")


# Fallback mock prices if yfinance is unavailable
_MOCK_BASE = {
    "RELIANCE": 2500, "TCS": 3800, "INFY": 1700,
    "HDFCBANK": 1600, "ICICIBANK": 1100,
    "NIFTY": 22000, "BANKNIFTY": 48000,
    "SBIN": 800, "WIPRO": 500, "AXISBANK": 1050,
}


class RealMarketData:
    """
    Real market data using yfinance (free, no auth needed).
    Computes RSI, MACD, SMA, EMA, Bollinger Bands from actual NSE candles.
    Falls back to mock data if yfinance is unavailable.
    """

    # Cache: symbol -> (fetched_at, DataFrame)
    _cache: dict = {}
    _CACHE_TTL_SECONDS = 300  # re-fetch every 5 min max

    def _get_df(self, symbol: str):
        """Fetch OHLCV DataFrame from yfinance with caching."""
        now = datetime.now()
        cached = self._cache.get(symbol)
        if cached:
            fetched_at, df = cached
            if (now - fetched_at).seconds < self._CACHE_TTL_SECONDS:
                return df

        try:
            import yfinance as yf
            ticker = yf.Ticker(_yf_symbol(symbol))
            df = ticker.history(period="3mo", interval="1d", auto_adjust=True)
            if df.empty:
                logger.warning("yfinance returned empty data for %s", symbol)
                return None
            self._cache[symbol] = (now, df)
            logger.debug("Fetched %d candles for %s via yfinance", len(df), symbol)
            return df
        except Exception as e:
            logger.warning("yfinance failed for %s: %s — using mock fallback", symbol, e)
            return None

    def get_quote(self, symbol: str) -> dict:
        df = self._get_df(symbol)
        if df is not None and not df.empty:
            last = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else last
            change = float(last["Close"]) - float(prev["Close"])
            return {
                "symbol": symbol,
                "last_price": round(float(last["Close"]), 2),
                "open": round(float(last["Open"]), 2),
                "high": round(float(last["High"]), 2),
                "low": round(float(last["Low"]), 2),
                "volume": int(last["Volume"]),
                "change": round(change, 2),
                "change_pct": round((change / float(prev["Close"])) * 100, 2) if prev["Close"] else 0,
                "timestamp": datetime.now().isoformat(),
                "source": "yfinance",
            }
        # Fallback mock quote
        import random
        base = _MOCK_BASE.get(symbol, 1000)
        price = round(base * (1 + random.uniform(-0.01, 0.01)), 2)
        return {
            "symbol": symbol, "last_price": price,
            "open": price, "high": price, "low": price,
            "volume": 500000, "change": 0, "change_pct": 0,
            "timestamp": datetime.now().isoformat(), "source": "mock_fallback",
        }

    def get_indicators(self, symbol: str) -> dict:
        """Compute real technical indicators from OHLCV candle data."""
        df = self._get_df(symbol)

        if df is None or len(df) < 26:
            logger.warning("Insufficient data for %s — using mock indicators", symbol)
            return self._mock_indicators(symbol)

        import pandas as pd
        closes = df["Close"].astype(float)
        price = float(closes.iloc[-1])

        # ── SMA ───────────────────────────────────────────────────────────
        sma_20 = float(closes.rolling(20).mean().iloc[-1])
        sma_50 = float(closes.rolling(50).mean().iloc[-1]) if len(closes) >= 50 else sma_20

        # ── EMA ───────────────────────────────────────────────────────────
        ema_9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])

        # ── RSI(14) ───────────────────────────────────────────────────────
        delta = closes.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-9)
        rsi = float((100 - (100 / (1 + rs))).iloc[-1])

        # ── MACD(12, 26, 9) ───────────────────────────────────────────────
        ema_12 = closes.ewm(span=12, adjust=False).mean()
        ema_26 = closes.ewm(span=26, adjust=False).mean()
        macd_line = ema_12 - ema_26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        macd = float(macd_line.iloc[-1])
        macd_signal = float(signal_line.iloc[-1])
        macd_hist = round(macd - macd_signal, 4)

        # ── Bollinger Bands(20, 2σ) ────────────────────────────────────────
        bb_mid = closes.rolling(20).mean()
        bb_std = closes.rolling(20).std()
        bb_upper = float((bb_mid + 2 * bb_std).iloc[-1])
        bb_lower = float((bb_mid - 2 * bb_std).iloc[-1])

        # ── ATR(14) ── AishDoc: volatility-based SL and position sizing ───
        # True Range = max(H-L, |H-PrevC|, |L-PrevC|)
        high  = df["High"].astype(float)
        low   = df["Low"].astype(float)
        prev_close = closes.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr_14 = float(tr.ewm(span=14, adjust=False).mean().iloc[-1])
        atr_pct = round((atr_14 / price) * 100, 2)  # ATR as % of price

        # ── Volume ────────────────────────────────────────────────────────
        volume = int(df["Volume"].iloc[-1])
        avg_volume = int(df["Volume"].rolling(20).mean().iloc[-1])

        quote = self.get_quote(symbol)

        return {
            "symbol": symbol,
            "price": round(price, 2),
            "rsi": round(rsi, 2),
            "macd": round(macd, 4),
            "macd_signal": round(macd_signal, 4),
            "macd_histogram": macd_hist,
            "sma_20": round(sma_20, 2),
            "sma_50": round(sma_50, 2),
            "ema_9": round(ema_9, 2),
            "bollinger_upper": round(bb_upper, 2),
            "bollinger_lower": round(bb_lower, 2),
            "price_vs_sma20": round(((price - sma_20) / sma_20) * 100, 2),
            "atr_14": round(atr_14, 2),        # absolute ATR value in ₹
            "atr_pct": atr_pct,                # ATR as % of price
            "sl_price": round(price - atr_14, 2),   # 1× ATR stop-loss level
            "tp_price": round(price + 2 * atr_14, 2),  # 2× ATR take-profit (1:2 R:R)
            "volume": volume,
            "avg_volume_20d": avg_volume,
            "volume_ratio": round(volume / avg_volume, 2) if avg_volume else 1.0,
            "change_pct": quote["change_pct"],
            "timestamp": datetime.now().isoformat(),
            "source": "yfinance",
        }

    def get_intraday_indicators(self, symbol: str) -> dict:
        """
        AishDoc intraday indicators using 5-min and 15-min candles.
        Returns: VWAP, ORB levels, PDH/PDL, intraday trend, 15-min RSI.
        """
        try:
            import yfinance as yf
            import pandas as pd
            yf_sym = _yf_symbol(symbol)

            # Today's 5-min candles (for VWAP + ORB)
            df_5m = yf.Ticker(yf_sym).history(period="1d", interval="5m", auto_adjust=True)
            # Last 5 days 15-min candles (for intermediate trend)
            df_15m = yf.Ticker(yf_sym).history(period="5d", interval="15m", auto_adjust=True)
            # Previous day daily candle (for PDH/PDL)
            df_daily = self._get_df(symbol)

            result = {}

            # ── VWAP (Volume Weighted Average Price) ──────────────────────────
            # AishDoc: price above VWAP = bullish, below = bearish
            if df_5m is not None and not df_5m.empty:
                typical = (df_5m["High"] + df_5m["Low"] + df_5m["Close"]) / 3
                vwap = (typical * df_5m["Volume"]).cumsum() / df_5m["Volume"].cumsum()
                result["vwap"] = round(float(vwap.iloc[-1]), 2)
                result["price"] = round(float(df_5m["Close"].iloc[-1]), 2)
                result["above_vwap"] = result["price"] > result["vwap"]

                # ── Opening Range Breakout (first ORB_WINDOW_MINS of the day) ─
                orb_candles = df_5m.head(max(1, config.ORB_WINDOW_MINS // 5))
                result["orb_high"] = round(float(orb_candles["High"].max()), 2)
                result["orb_low"]  = round(float(orb_candles["Low"].min()), 2)
                result["orb_broken_up"]   = result["price"] > result["orb_high"]
                result["orb_broken_down"] = result["price"] < result["orb_low"]

                # ── Intraday range progress ───────────────────────────────────
                result["day_high"] = round(float(df_5m["High"].max()), 2)
                result["day_low"]  = round(float(df_5m["Low"].min()), 2)

            # ── Previous Day High / Low (key S/R levels) ──────────────────────
            if df_daily is not None and len(df_daily) >= 2:
                prev = df_daily.iloc[-2]
                result["pdh"] = round(float(prev["High"]), 2)   # previous day high
                result["pdl"] = round(float(prev["Low"]), 2)    # previous day low
                result["pdc"] = round(float(prev["Close"]), 2)  # previous day close

            # ── ATR(14) on 5-min candles (intraday volatility) ────────────────
            # AishDoc uses intraday ATR for precise SL placement
            if df_5m is not None and len(df_5m) >= 14:
                h5 = df_5m["High"].astype(float)
                l5 = df_5m["Low"].astype(float)
                c5 = df_5m["Close"].astype(float)
                pc5 = c5.shift(1)
                tr5 = pd.concat([(h5 - l5), (h5 - pc5).abs(), (l5 - pc5).abs()], axis=1).max(axis=1)
                atr_5m = float(tr5.ewm(span=14, adjust=False).mean().iloc[-1])
                result["atr_5m"] = round(atr_5m, 2)
                result["atr_5m_pct"] = round((atr_5m / result.get("price", 1)) * 100, 3)
                # Dynamic SL/TP from intraday ATR (1× ATR SL, 2× ATR TP = 1:2 R:R)
                price_now = result.get("price", 0)
                if price_now:
                    result["atr_sl"] = round(price_now - atr_5m, 2)
                    result["atr_tp"] = round(price_now + 2 * atr_5m, 2)

            # ── 15-min trend (AishDoc: use 15-min for trend direction) ────────
            if df_15m is not None and len(df_15m) >= 20:
                closes_15m = df_15m["Close"].astype(float)
                sma9_15m  = float(closes_15m.ewm(span=9, adjust=False).mean().iloc[-1])
                sma20_15m = float(closes_15m.rolling(20).mean().iloc[-1])
                price_15m = float(closes_15m.iloc[-1])

                result["sma9_15m"]  = round(sma9_15m, 2)
                result["sma20_15m"] = round(sma20_15m, 2)
                result["trend_15m"] = "uptrend" if sma9_15m > sma20_15m else "downtrend"
                result["price_vs_sma20_15m"] = round(((price_15m - sma20_15m) / sma20_15m) * 100, 2)

                # 15-min RSI
                delta = closes_15m.diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = (-delta.clip(upper=0)).rolling(14).mean()
                rs = gain / loss.replace(0, 1e-9)
                result["rsi_15m"] = round(float((100 - 100 / (1 + rs)).iloc[-1]), 2)

            result["symbol"] = symbol
            result["timestamp"] = datetime.now().isoformat()
            result["source"] = "yfinance_intraday"
            return result

        except Exception as e:
            logger.warning("Intraday indicators failed for %s: %s", symbol, e)
            return {"symbol": symbol, "error": str(e), "source": "error"}

    def _mock_indicators(self, symbol: str) -> dict:
        """Last-resort fallback with clearly labelled mock data."""
        import random
        base = _MOCK_BASE.get(symbol, 1000)
        price = round(base * (1 + random.uniform(-0.01, 0.01)), 2)
        return {
            "symbol": symbol, "price": price,
            "rsi": round(random.uniform(40, 60), 2),
            "macd": 0.0, "macd_signal": 0.0, "macd_histogram": 0.0,
            "sma_20": price, "sma_50": price, "ema_9": price,
            "bollinger_upper": round(price * 1.02, 2),
            "bollinger_lower": round(price * 0.98, 2),
            "price_vs_sma20": 0.0,
            "volume": 500000, "avg_volume_20d": 500000, "volume_ratio": 1.0,
            "change_pct": 0.0,
            "timestamp": datetime.now().isoformat(),
            "source": "mock_fallback",
        }


class LiveMarketData:
    """Live market data via jugaad-trader."""

    def __init__(self, broker):
        self.broker = broker

    def get_quote(self, symbol: str) -> dict:
        return self.broker.get_quote(symbol)

    def get_ohlcv(self, symbol: str, interval: str = "day", days: int = 60) -> list:
        try:
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            to_date = datetime.now().strftime("%Y-%m-%d")
            data = self.broker.broker.historical_data(
                instrument_token=self._get_token(symbol),
                from_date=from_date,
                to_date=to_date,
                interval=interval,
            )
            return data
        except Exception as e:
            logger.error("Failed to fetch OHLCV for %s: %s", symbol, e)
            return []

    def get_indicators(self, symbol: str) -> dict:
        candles = self.get_ohlcv(symbol, days=60)
        if not candles or len(candles) < 26:
            return {"symbol": symbol, "error": "insufficient data", "price": 0}

        import pandas as pd
        closes = pd.Series([c["close"] for c in candles], dtype=float)
        price = float(closes.iloc[-1])

        sma_20 = float(closes.rolling(20).mean().iloc[-1])
        sma_50 = float(closes.rolling(50).mean().iloc[-1]) if len(closes) >= 50 else sma_20
        ema_9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])

        delta = closes.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-9)
        rsi = float((100 - (100 / (1 + rs))).iloc[-1])

        ema_12 = closes.ewm(span=12, adjust=False).mean()
        ema_26 = closes.ewm(span=26, adjust=False).mean()
        macd_line = ema_12 - ema_26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()

        bb_mid = closes.rolling(20).mean()
        bb_std = closes.rolling(20).std()

        quote = self.get_quote(symbol)
        return {
            "symbol": symbol,
            "price": round(price, 2),
            "rsi": round(rsi, 2),
            "macd": round(float(macd_line.iloc[-1]), 4),
            "macd_signal": round(float(signal_line.iloc[-1]), 4),
            "macd_histogram": round(float(macd_line.iloc[-1]) - float(signal_line.iloc[-1]), 4),
            "sma_20": round(sma_20, 2),
            "sma_50": round(sma_50, 2),
            "ema_9": round(ema_9, 2),
            "bollinger_upper": round(float((bb_mid + 2 * bb_std).iloc[-1]), 2),
            "bollinger_lower": round(float((bb_mid - 2 * bb_std).iloc[-1]), 2),
            "price_vs_sma20": round(((price - sma_20) / sma_20) * 100, 2),
            "volume": quote.get("volume", 0),
            "change_pct": quote.get("change_pct", 0),
            "timestamp": datetime.now().isoformat(),
            "source": "jugaad_live",
        }

    def _get_token(self, symbol: str) -> int:
        # Cache instrument tokens to avoid re-downloading on every call
        if not hasattr(self, "_token_cache"):
            self._token_cache = {}
        if symbol not in self._token_cache:
            instruments = self.broker.broker.instruments("NSE")
            self._token_cache = {i["tradingsymbol"]: i["instrument_token"] for i in instruments}
        if symbol not in self._token_cache:
            raise ValueError(f"Instrument token not found for {symbol}")
        return self._token_cache[symbol]


def get_market_data(broker=None):
    """Factory — returns real (yfinance) or live (jugaad) market data."""
    if config.IS_PAPER:
        return RealMarketData()
    else:
        return LiveMarketData(broker)
