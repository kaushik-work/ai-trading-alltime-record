"""
Candlestick pattern detection — AishDoc price action style.
Works on a list of OHLCV candle dicts: {open, high, low, close, volume}.
Returns detected patterns, directional bias, and a strength score.
"""


def _body(c: dict) -> float:
    return abs(c["close"] - c["open"])


def _upper_wick(c: dict) -> float:
    return c["high"] - max(c["open"], c["close"])


def _lower_wick(c: dict) -> float:
    return min(c["open"], c["close"]) - c["low"]


def _is_bull(c: dict) -> bool:
    return c["close"] > c["open"]


def _is_bear(c: dict) -> bool:
    return c["close"] < c["open"]


def _range(c: dict) -> float:
    return c["high"] - c["low"] if c["high"] != c["low"] else 0.0001


def detect_patterns(candles: list) -> dict:
    """
    Detect patterns from the last 3 candles.
    Returns:
      patterns  : list of pattern names detected
      bias      : 'bullish' | 'bearish' | 'neutral'
      strength  : int, positive = bullish, negative = bearish
    """
    if len(candles) < 3:
        return {"patterns": [], "bias": "neutral", "strength": 0}

    c1 = candles[-3]   # 3 candles ago
    c2 = candles[-2]   # yesterday / previous
    c3 = candles[-1]   # latest / current

    patterns = []
    strength = 0

    # ── Single-candle patterns (current candle) ───────────────────────────────

    body3 = _body(c3)
    rng3 = _range(c3)
    uw3 = _upper_wick(c3)
    lw3 = _lower_wick(c3)

    # Doji — open ≈ close (body < 10% of range)
    if body3 < 0.10 * rng3:
        patterns.append("doji")
        # Neutral by itself — context dependent

    # Hammer — lower wick >= 2× body, upper wick tiny, at low (potential reversal up)
    elif lw3 >= 2.0 * body3 and uw3 <= 0.3 * body3 and body3 > 0:
        patterns.append("hammer")
        strength += 2

    # Shooting star — upper wick >= 2× body, lower wick tiny (potential reversal down)
    elif uw3 >= 2.0 * body3 and lw3 <= 0.3 * body3 and body3 > 0:
        patterns.append("shooting_star")
        strength -= 2

    # Marubozu (strong candle, very small wicks)
    if uw3 <= 0.05 * rng3 and lw3 <= 0.05 * rng3 and body3 > 0:
        if _is_bull(c3):
            patterns.append("bullish_marubozu")
            strength += 2
        else:
            patterns.append("bearish_marubozu")
            strength -= 2

    # Spinning top — small body with large wicks on both sides
    if body3 < 0.25 * rng3 and uw3 > 0.25 * rng3 and lw3 > 0.25 * rng3:
        patterns.append("spinning_top")
        # Indecision — neutral

    # ── Two-candle patterns ───────────────────────────────────────────────────

    body2 = _body(c2)

    # Bullish engulfing — c2 bearish, c3 bullish and engulfs c2's body
    if (_is_bear(c2) and _is_bull(c3)
            and c3["open"] <= c2["close"]
            and c3["close"] >= c2["open"]
            and body3 > body2):
        patterns.append("bullish_engulfing")
        strength += 3

    # Bearish engulfing — c2 bullish, c3 bearish and engulfs c2's body
    elif (_is_bull(c2) and _is_bear(c3)
            and c3["open"] >= c2["close"]
            and c3["close"] <= c2["open"]
            and body3 > body2):
        patterns.append("bearish_engulfing")
        strength -= 3

    # Bullish harami — large bear c2, small bull c3 inside c2's body
    if (_is_bear(c2) and _is_bull(c3)
            and c3["open"] > c2["close"]
            and c3["close"] < c2["open"]
            and body3 < 0.5 * body2):
        patterns.append("bullish_harami")
        strength += 1

    # Bearish harami — large bull c2, small bear c3 inside c2's body
    if (_is_bull(c2) and _is_bear(c3)
            and c3["open"] < c2["close"]
            and c3["close"] > c2["open"]
            and body3 < 0.5 * body2):
        patterns.append("bearish_harami")
        strength -= 1

    # Tweezer bottom — two lows very close (support)
    if (abs(c2["low"] - c3["low"]) / (c2["low"] + 0.0001) < 0.002
            and _is_bear(c2) and _is_bull(c3)):
        patterns.append("tweezer_bottom")
        strength += 2

    # Tweezer top — two highs very close (resistance)
    if (abs(c2["high"] - c3["high"]) / (c2["high"] + 0.0001) < 0.002
            and _is_bull(c2) and _is_bear(c3)):
        patterns.append("tweezer_top")
        strength -= 2

    # ── Three-candle patterns ─────────────────────────────────────────────────

    body1 = _body(c1)
    mid_price_c1 = (c1["open"] + c1["close"]) / 2

    # Morning star — bear c1, small-body c2 (gap down), bull c3 closing above mid of c1
    if (_is_bear(c1)
            and body2 < 0.35 * body1
            and _is_bull(c3)
            and c3["close"] > mid_price_c1):
        patterns.append("morning_star")
        strength += 4

    # Evening star — bull c1, small-body c2 (gap up), bear c3 closing below mid of c1
    if (_is_bull(c1)
            and body2 < 0.35 * body1
            and _is_bear(c3)
            and c3["close"] < mid_price_c1):
        patterns.append("evening_star")
        strength -= 4

    # Three white soldiers — three consecutive bull candles, each closing higher
    if (_is_bull(c1) and _is_bull(c2) and _is_bull(c3)
            and c2["close"] > c1["close"]
            and c3["close"] > c2["close"]
            and c2["open"] > c1["open"]
            and c3["open"] > c2["open"]):
        patterns.append("three_white_soldiers")
        strength += 4

    # Three black crows — three consecutive bear candles, each closing lower
    if (_is_bear(c1) and _is_bear(c2) and _is_bear(c3)
            and c2["close"] < c1["close"]
            and c3["close"] < c2["close"]
            and c2["open"] < c1["open"]
            and c3["open"] < c2["open"]):
        patterns.append("three_black_crows")
        strength -= 4

    # ── Final bias ────────────────────────────────────────────────────────────
    strength = max(-8, min(8, strength))
    bias = "bullish" if strength > 0 else "bearish" if strength < 0 else "neutral"

    return {
        "patterns": patterns,
        "bias": bias,
        "strength": strength,
    }


def get_candles_from_df(df) -> list:
    """Convert a yfinance DataFrame to candle dicts for pattern detection."""
    candles = []
    for _, row in df.iterrows():
        candles.append({
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": int(row["Volume"]),
        })
    return candles
