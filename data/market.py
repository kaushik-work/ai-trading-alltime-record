"""
MarketData — OHLCV data and technical indicators for NSE strategies.

Data source priority (yfinance removed entirely):
  1. ZerodhaFetcher  — jugaad-trader, real NSE bars, real volume
  2. NseFetcher      — NSE India public API, official OHLCV

Both daily and intraday data route through the same fetchers used by
Musashi and Raijin in bot_runner, so all strategies see consistent data.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import config

logger = logging.getLogger(__name__)

# Fallback mock prices (last resort — only if both sources fail)
_MOCK_BASE = {
    "NIFTY": 22000, "BANKNIFTY": 48000,
    "RELIANCE": 2500, "TCS": 3800, "INFY": 1700,
    "HDFCBANK": 1600, "ICICIBANK": 1100,
    "SBIN": 800, "WIPRO": 500, "AXISBANK": 1050,
}


def _get_daily_df(symbol: str):
    """
    Fetch daily OHLCV DataFrame via Zerodha → NSE fallback.
    Returns a pandas DataFrame or None.
    """
    # 1. Zerodha
    try:
        from data.zerodha_fetcher import ZerodhaFetcher
        df = ZerodhaFetcher.get().fetch_daily_df(symbol)
        if df is not None and len(df) >= 10:
            return df
    except Exception as e:
        logger.warning("_get_daily_df Zerodha failed for %s: %s", symbol, e)

    # 2. NSE India
    try:
        from data.nse_fetcher import NseFetcher
        df = NseFetcher.get().fetch_daily_df(symbol)
        if df is not None and len(df) >= 10:
            logger.info("_get_daily_df: using NSE fallback for %s", symbol)
            return df
    except Exception as e:
        logger.warning("_get_daily_df NSE failed for %s: %s", symbol, e)

    return None


def _get_intraday_df(symbol: str, interval: str):
    """
    Fetch today's intraday OHLCV DataFrame via Zerodha → NSE fallback.
    Returns a pandas DataFrame or None.
    """
    # 1. Zerodha
    try:
        from data.zerodha_fetcher import ZerodhaFetcher
        df = ZerodhaFetcher.get().fetch_intraday_df(symbol, interval)
        if df is not None and len(df) >= 3:
            return df
    except Exception as e:
        logger.warning("_get_intraday_df Zerodha failed for %s %s: %s", symbol, interval, e)

    # 2. NSE India
    try:
        from data.nse_fetcher import NseFetcher
        df = NseFetcher.get().fetch_intraday_df(symbol, interval)
        if df is not None and len(df) >= 3:
            logger.info("_get_intraday_df: using NSE fallback for %s %s", symbol, interval)
            return df
    except Exception as e:
        logger.warning("_get_intraday_df NSE failed for %s %s: %s", symbol, interval, e)

    return None


class RealMarketData:
    """
    Technical indicators computed from real NSE data.
    No yfinance. Data comes from ZerodhaFetcher / NseFetcher.
    """

    def get_quote(self, symbol: str) -> dict:
        """Latest price from daily data (last bar close)."""
        df = _get_daily_df(symbol)
        if df is not None and len(df) >= 2:
            last  = df.iloc[-1]
            prev  = df.iloc[-2]
            price = float(last["Close"])
            prev_c = float(prev["Close"])
            change = price - prev_c
            return {
                "symbol":     symbol,
                "last_price": round(price, 2),
                "open":       round(float(last["Open"]), 2),
                "high":       round(float(last["High"]), 2),
                "low":        round(float(last["Low"]),  2),
                "volume":     int(last["Volume"]),
                "change":     round(change, 2),
                "change_pct": round((change / prev_c) * 100, 2) if prev_c else 0,
                "timestamp":  datetime.now().isoformat(),
                "source":     "zerodha_or_nse",
            }
        # Last-resort mock
        import random
        base  = _MOCK_BASE.get(symbol, 1000)
        price = round(base * (1 + random.uniform(-0.01, 0.01)), 2)
        return {
            "symbol": symbol, "last_price": price,
            "open": price, "high": price, "low": price,
            "volume": 0, "change": 0, "change_pct": 0,
            "timestamp": datetime.now().isoformat(), "source": "mock_fallback",
        }

    def get_indicators(self, symbol: str) -> dict:
        """
        Daily technical indicators: RSI, MACD, SMA, EMA, Bollinger, ATR, volume.
        Used by ATR Intraday (TrendStrategy + signal_scorer).
        """
        import pandas as pd

        df = _get_daily_df(symbol)
        if df is None or len(df) < 26:
            logger.warning("get_indicators: insufficient daily data for %s", symbol)
            return self._mock_indicators(symbol)

        closes = df["Close"].astype(float)
        price  = float(closes.iloc[-1])

        # SMA
        sma_20 = float(closes.rolling(20).mean().iloc[-1])
        sma_50 = float(closes.rolling(50).mean().iloc[-1]) if len(closes) >= 50 else sma_20

        # EMA
        ema_9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])

        # RSI(14) — Wilder's via EWM
        delta = closes.diff()
        gain  = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(span=14, adjust=False).mean()
        rs    = gain / loss.replace(0, 1e-9)
        rsi   = float((100 - 100 / (1 + rs)).iloc[-1])

        # MACD(12,26,9)
        ema_12      = closes.ewm(span=12, adjust=False).mean()
        ema_26      = closes.ewm(span=26, adjust=False).mean()
        macd_line   = ema_12 - ema_26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        macd        = float(macd_line.iloc[-1])
        macd_sig    = float(signal_line.iloc[-1])

        # Bollinger Bands(20, 2σ)
        bb_mid   = closes.rolling(20).mean()
        bb_std   = closes.rolling(20).std()
        bb_upper = float((bb_mid + 2 * bb_std).iloc[-1])
        bb_lower = float((bb_mid - 2 * bb_std).iloc[-1])

        # ATR(14)
        high  = df["High"].astype(float)
        low   = df["Low"].astype(float)
        pc    = closes.shift(1)
        tr    = pd.concat([(high - low), (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
        atr14 = float(tr.ewm(span=14, adjust=False).mean().iloc[-1])

        # Volume
        volume     = int(df["Volume"].iloc[-1])
        avg_volume = int(df["Volume"].rolling(20).mean().iloc[-1]) if len(df) >= 20 else volume

        quote = self.get_quote(symbol)

        return {
            "symbol":          symbol,
            "price":           round(price, 2),
            "rsi":             round(rsi, 2),
            "macd":            round(macd, 4),
            "macd_signal":     round(macd_sig, 4),
            "macd_histogram":  round(macd - macd_sig, 4),
            "sma_20":          round(sma_20, 2),
            "sma_50":          round(sma_50, 2),
            "ema_9":           round(ema_9, 2),
            "bollinger_upper": round(bb_upper, 2),
            "bollinger_lower": round(bb_lower, 2),
            "price_vs_sma20":  round(((price - sma_20) / sma_20) * 100, 2),
            "atr_14":          round(atr14, 2),
            "atr_pct":         round((atr14 / price) * 100, 2),
            "sl_price":        round(price - atr14, 2),
            "tp_price":        round(price + 2 * atr14, 2),
            "volume":          volume,
            "avg_volume_20d":  avg_volume,
            "volume_ratio":    round(volume / avg_volume, 2) if avg_volume else 1.0,
            "change_pct":      quote["change_pct"],
            "timestamp":       datetime.now().isoformat(),
            "source":          "zerodha_or_nse",
        }

    def get_intraday_indicators(self, symbol: str) -> dict:
        """
        Intraday indicators: VWAP, ORB, PDH/PDL, 15-min trend + RSI, intraday ATR.
        Used by ATR Intraday strategy.
        """
        import pandas as pd
        result = {"symbol": symbol, "timestamp": datetime.now().isoformat(), "source": "zerodha_or_nse"}

        # ── 5-min bars (VWAP, ORB, intraday ATR) ─────────────────────────────
        df_5m = _get_intraday_df(symbol, "5m")
        if df_5m is not None and len(df_5m) >= 3:
            c5 = df_5m["Close"].astype(float)
            h5 = df_5m["High"].astype(float)
            l5 = df_5m["Low"].astype(float)
            v5 = df_5m["Volume"].astype(float)

            typical = (h5 + l5 + c5) / 3
            cum_vol = v5.cumsum().replace(0, 1)
            vwap = float((typical * v5).cumsum().iloc[-1] / cum_vol.iloc[-1])
            result["vwap"]       = round(vwap, 2)
            result["price"]      = round(float(c5.iloc[-1]), 2)
            result["above_vwap"] = result["price"] > vwap

            # ORB (first N 5-min candles)
            n_orb = max(1, config.ORB_WINDOW_MINS // 5)
            orb   = df_5m.head(n_orb)
            result["orb_high"]        = round(float(orb["High"].max()), 2)
            result["orb_low"]         = round(float(orb["Low"].min()),  2)
            result["orb_broken_up"]   = result["price"] > result["orb_high"]
            result["orb_broken_down"] = result["price"] < result["orb_low"]
            result["day_high"]        = round(float(h5.max()), 2)
            result["day_low"]         = round(float(l5.min()),  2)

            # Intraday ATR(14) on 5-min bars
            if len(df_5m) >= 14:
                pc5 = c5.shift(1)
                tr5 = pd.concat([(h5 - l5), (h5 - pc5).abs(), (l5 - pc5).abs()], axis=1).max(axis=1)
                atr5 = float(tr5.ewm(span=14, adjust=False).mean().iloc[-1])
                result["atr_5m"]     = round(atr5, 2)
                result["atr_5m_pct"] = round((atr5 / result["price"]) * 100, 3)
                result["atr_sl"]     = round(result["price"] - atr5, 2)
                result["atr_tp"]     = round(result["price"] + 2 * atr5, 2)

        # ── 15-min bars (trend + RSI) ─────────────────────────────────────────
        df_15m = _get_intraday_df(symbol, "15m")
        if df_15m is None or len(df_15m) < 5:
            # Fallback: use last 5 days of 15-min bars from Zerodha
            try:
                from data.zerodha_fetcher import ZerodhaFetcher
                from zoneinfo import ZoneInfo
                IST = ZoneInfo("Asia/Kolkata")
                now = datetime.now(IST)
                from_d = (now - timedelta(days=5)).strftime("%Y-%m-%d")
                to_d   = now.strftime("%Y-%m-%d")
                from data.zerodha_fetcher import _TOKENS, _INTERVAL_MAP
                token = _TOKENS.get(symbol)
                if token:
                    zf = ZerodhaFetcher.get()
                    if zf._ensure_logged_in():
                        recs = zf._broker.historical_data(
                            instrument_token=token,
                            from_date=f"{from_d} 09:15:00",
                            to_date=f"{to_d} 15:30:00",
                            interval="15minute",
                        )
                        if recs and len(recs) >= 5:
                            import pandas as pd
                            df_15m = pd.DataFrame(recs)
                            df_15m.rename(columns={"close": "Close"}, inplace=True)
            except Exception:
                pass

        if df_15m is not None and len(df_15m) >= 20:
            c15    = df_15m["Close"].astype(float)
            sma9   = float(c15.ewm(span=9, adjust=False).mean().iloc[-1])
            sma20  = float(c15.rolling(20).mean().iloc[-1])
            p15    = float(c15.iloc[-1])
            result["sma9_15m"]         = round(sma9, 2)
            result["sma20_15m"]        = round(sma20, 2)
            result["trend_15m"]        = "uptrend" if sma9 > sma20 else "downtrend"
            result["price_vs_sma20_15m"] = round(((p15 - sma20) / sma20) * 100, 2)

            delta = c15.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            rs    = gain / loss.replace(0, 1e-9)
            result["rsi_15m"] = round(float((100 - 100 / (1 + rs)).iloc[-1]), 2)

        # ── Previous day high/low from daily bars ────────────────────────────
        df_d = _get_daily_df(symbol)
        if df_d is not None and len(df_d) >= 2:
            prev = df_d.iloc[-2]
            result["pdh"] = round(float(prev["High"]),  2)
            result["pdl"] = round(float(prev["Low"]),   2)
            result["pdc"] = round(float(prev["Close"]), 2)

        return result

    def _get_df(self, symbol: str, interval: str = "5m"):
        """Return intraday OHLCV DataFrame. Used by trend_strategy for pattern detection and ICT scoring."""
        return _get_intraday_df(symbol, interval)

    def get_raw_candles(self, symbol: str, interval: str, limit: int = 30) -> list:
        """
        Return last `limit` OHLCV bars as a list of dicts for the brain to read directly.
        Time is formatted as HH:MM (IST) for readability.
        """
        df = _get_intraday_df(symbol, interval)
        if df is None or len(df) == 0:
            return []
        df = df.tail(limit)
        rows = []
        for idx, row in df.iterrows():
            try:
                t = idx.strftime("%H:%M") if hasattr(idx, "strftime") else str(idx)[-8:][:5]
            except Exception:
                t = str(idx)
            rows.append({
                "t": t,
                "o": round(float(row["Open"]),   2),
                "h": round(float(row["High"]),   2),
                "l": round(float(row["Low"]),    2),
                "c": round(float(row["Close"]),  2),
                "v": int(row["Volume"]) if "Volume" in row else 0,
            })
        return rows

    def _mock_indicators(self, symbol: str) -> dict:
        """Last-resort fallback — clearly labelled mock data."""
        import random
        base  = _MOCK_BASE.get(symbol, 1000)
        price = round(base * (1 + random.uniform(-0.01, 0.01)), 2)
        return {
            "symbol": symbol, "price": price,
            "rsi": round(random.uniform(40, 60), 2),
            "macd": 0.0, "macd_signal": 0.0, "macd_histogram": 0.0,
            "sma_20": price, "sma_50": price, "ema_9": price,
            "bollinger_upper": round(price * 1.02, 2),
            "bollinger_lower": round(price * 0.98, 2),
            "price_vs_sma20": 0.0,
            "atr_14": 0.0, "atr_pct": 0.0,
            "sl_price": 0.0, "tp_price": 0.0,
            "volume": 0, "avg_volume_20d": 0, "volume_ratio": 1.0,
            "change_pct": 0.0,
            "timestamp": datetime.now().isoformat(),
            "source": "mock_fallback",
        }


class LiveMarketData:
    """Live market data via jugaad-trader (used in LIVE mode)."""

    def __init__(self, broker):
        self.broker = broker

    def get_quote(self, symbol: str) -> dict:
        return self.broker.get_quote(symbol)

    def get_indicators(self, symbol: str) -> dict:
        # In live mode, still use the same data pipeline for indicators
        return RealMarketData().get_indicators(symbol)

    def get_intraday_indicators(self, symbol: str) -> dict:
        return RealMarketData().get_intraday_indicators(symbol)


def get_market_data(broker=None):
    """Factory — returns real or live market data."""
    if config.IS_PAPER:
        return RealMarketData()
    else:
        return LiveMarketData(broker)
