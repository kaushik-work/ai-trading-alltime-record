"""
Custom Technical Indicators for NIFTY Options Strategies.

Shared between Musashi (intraday) and Raijin (scalp).
All functions are strictly no-lookahead — they operate only on data[: up_to + 1].
"""

import numpy as np
import pandas as pd
from typing import Tuple


# ── Heikin-Ashi ───────────────────────────────────────────────────────────────

def heikin_ashi(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return HA open, high, low, close arrays."""
    n = len(closes)
    ha_close = (opens + highs + lows + closes) / 4.0
    ha_open  = np.empty(n)
    ha_open[0] = (opens[0] + closes[0]) / 2.0
    for i in range(1, n):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0
    ha_high = np.maximum(highs, np.maximum(ha_open, ha_close))
    ha_low  = np.minimum(lows,  np.minimum(ha_open, ha_close))
    return ha_open, ha_high, ha_low, ha_close


def ha_consecutive(ha_opens: np.ndarray, ha_closes: np.ndarray) -> int:
    """
    Return count of consecutive same-direction HA candles ending at the last bar.
    Positive = consecutive bulls, Negative = consecutive bears.
    """
    if len(ha_opens) == 0:
        return 0
    is_bull = ha_closes[-1] > ha_opens[-1]
    count = 0
    for i in range(len(ha_opens) - 1, -1, -1):
        if (ha_closes[i] > ha_opens[i]) == is_bull:
            count += 1
        else:
            break
    return count if is_bull else -count


def ha_color_changed(ha_opens: np.ndarray, ha_closes: np.ndarray) -> bool:
    """True if the last HA candle changed colour vs the previous one."""
    if len(ha_opens) < 2:
        return False
    was_bull = ha_closes[-2] > ha_opens[-2]
    is_bull  = ha_closes[-1] > ha_opens[-1]
    return was_bull != is_bull


# ── VWAP + Bands ──────────────────────────────────────────────────────────────

def compute_vwap_bands(
    day_highs: np.ndarray,
    day_lows: np.ndarray,
    day_closes: np.ndarray,
    day_volumes: np.ndarray,
) -> Tuple[float, float, float, float, float]:
    """
    Compute session VWAP and ±1σ / ±2σ bands from day-open.

    Returns: (vwap, upper1, lower1, upper2, lower2)
    """
    typical  = (day_highs + day_lows + day_closes) / 3.0
    cum_vol  = np.cumsum(day_volumes)
    cum_pv   = np.cumsum(typical * day_volumes)

    if cum_vol[-1] == 0:
        vwap = float(day_closes[-1])
    else:
        vwap = float(cum_pv[-1] / cum_vol[-1])

    # Intraday σ from (typical - VWAP) running deviation
    deviations = typical - vwap
    variance   = float(np.mean(deviations ** 2))
    sigma      = max(1.0, float(variance ** 0.5))

    return (
        round(vwap, 2),
        round(vwap + 1 * sigma, 2),
        round(vwap - 1 * sigma, 2),
        round(vwap + 2 * sigma, 2),
        round(vwap - 2 * sigma, 2),
    )


# ── Swing Structure ───────────────────────────────────────────────────────────

def detect_swing_structure(
    highs: np.ndarray,
    lows: np.ndarray,
    lookback: int = 20,
) -> str:
    """
    Identify market structure from recent swing points.

    Returns: 'uptrend' | 'downtrend' | 'sideways'

    Uptrend  = last swing high > prev swing high AND last swing low > prev swing low
    Downtrend = last swing high < prev swing high AND last swing low < prev swing low
    """
    if len(highs) < lookback:
        return "sideways"

    h = highs[-lookback:]
    l = lows[-lookback:]

    # Find local swing highs: bars where high[i] > high[i-1] and high[i] > high[i+1]
    sh_idx = [i for i in range(1, len(h) - 1) if h[i] > h[i - 1] and h[i] > h[i + 1]]
    sl_idx = [i for i in range(1, len(l) - 1) if l[i] < l[i - 1] and l[i] < l[i + 1]]

    if len(sh_idx) < 2 or len(sl_idx) < 2:
        return "sideways"

    sh1, sh2 = h[sh_idx[-2]], h[sh_idx[-1]]  # previous and last swing high
    sl1, sl2 = l[sl_idx[-2]], l[sl_idx[-1]]  # previous and last swing low

    hh_hl = (sh2 > sh1) and (sl2 > sl1)  # Higher Highs + Higher Lows
    lh_ll = (sh2 < sh1) and (sl2 < sl1)  # Lower Highs + Lower Lows

    if hh_hl:
        return "uptrend"
    if lh_ll:
        return "downtrend"
    return "sideways"


# ── EMA utilities ─────────────────────────────────────────────────────────────

def ema(closes: np.ndarray, period: int) -> float:
    """Return the last value of an EMA series."""
    if len(closes) == 0:
        return 0.0
    s = pd.Series(closes)
    return float(s.ewm(span=period, adjust=False).mean().iloc[-1])


def ema_series(closes: np.ndarray, period: int) -> np.ndarray:
    """Return the full EMA series as a numpy array."""
    s = pd.Series(closes)
    return s.ewm(span=period, adjust=False).mean().values


# ── ATR ───────────────────────────────────────────────────────────────────────

def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
        period: int = 14) -> float:
    """Compute ATR (Wilder's smoothing) and return the latest value."""
    if len(closes) < 2:
        return float(highs[-1] - lows[-1]) if len(highs) > 0 else 1.0
    h = pd.Series(highs)
    l = pd.Series(lows)
    c = pd.Series(closes)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return float(tr.ewm(span=period, adjust=False).mean().iloc[-1])


# ── RSI ───────────────────────────────────────────────────────────────────────

def rsi(closes: np.ndarray, period: int = 14) -> float:
    """Compute RSI and return the latest value."""
    if len(closes) < period + 1:
        return 50.0
    s = pd.Series(closes)
    delta = s.diff()
    gain  = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs    = gain / loss.replace(0, 1e-9)
    return float((100 - 100 / (1 + rs)).iloc[-1])


# ── Candlestick patterns ──────────────────────────────────────────────────────

def is_pin_bar(o: float, h: float, l: float, c: float,
               min_wick_ratio: float = 2.0) -> str:
    """
    Detect pin bar. Returns 'bullish', 'bearish', or 'none'.
    min_wick_ratio: wick must be >= this multiple of the body.
    """
    body     = abs(c - o)
    lower_w  = min(o, c) - l
    upper_w  = h - max(o, c)

    if body < 1e-6:
        return "none"

    if lower_w >= min_wick_ratio * body and upper_w < body:
        return "bullish"
    if upper_w >= min_wick_ratio * body and lower_w < body:
        return "bearish"
    return "none"


def is_engulfing(
    prev_o: float, prev_c: float,
    curr_o: float, curr_c: float,
) -> str:
    """
    Detect engulfing candle. Returns 'bullish', 'bearish', or 'none'.
    """
    prev_bull = prev_c > prev_o
    curr_bull = curr_c > curr_o
    if prev_bull and not curr_bull:
        # Bearish engulfing: current red body engulfs previous green body
        if curr_o >= prev_c and curr_c <= prev_o:
            return "bearish"
    if not prev_bull and curr_bull:
        # Bullish engulfing: current green body engulfs previous red body
        if curr_o <= prev_c and curr_c >= prev_o:
            return "bullish"
    return "none"


def is_inside_bar(
    prev_h: float, prev_l: float,
    curr_h: float, curr_l: float,
) -> bool:
    """True if current bar is an inside bar (range within previous bar)."""
    return curr_h <= prev_h and curr_l >= prev_l


def volume_ratio(volumes: np.ndarray, lookback: int = 20) -> float:
    """Current volume as multiple of lookback average."""
    if len(volumes) < 2:
        return 1.0
    avg = float(np.mean(volumes[-lookback - 1: -1])) if len(volumes) > lookback else float(np.mean(volumes[:-1]))
    if avg <= 0:
        return 1.0
    return round(float(volumes[-1]) / avg, 2)
