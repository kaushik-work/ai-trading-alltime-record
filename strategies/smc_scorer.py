"""
smc_scorer.py — Smart Money Concepts pattern detector.

Detects 5 SMC/ICT patterns algorithmically from OHLCV data:
  1. CHOCH   — Change of Character (trend flip)           ±3
  2. OB      — Order Block (supply/demand zones)          ±2
  3. Breaker — Breaker Block (broken S/R flip)            ±2
  4. FVG     — Fair Value Gap (price imbalance)           ±2
  5. AMD     — Accumulation-Manipulation-Distribution     ±2
  Bonus: ±1 if 3+ patterns agree same direction

Total range: -10 to +10. Threshold: 6 (same as other strategies).
Completely standalone — no interaction with ATR/C-ICT/Fib-OF/Vision-ICT.
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── Swing detection ───────────────────────────────────────────────────────────

def _find_swings(df: pd.DataFrame, n: int = 3) -> tuple[list, list]:
    """
    Return (swing_highs, swing_lows) as lists of (index_position, price).
    A swing high at bar i: high[i] is the highest within [i-n, i+n].
    Only returns CONFIRMED swings (n bars have passed since the bar).
    """
    highs = df["High"].values
    lows  = df["Low"].values
    swing_highs, swing_lows = [], []

    for i in range(n, len(df) - n):
        window_h = highs[i - n: i + n + 1]
        window_l = lows[i - n: i + n + 1]
        if highs[i] == window_h.max() and list(window_h).count(highs[i]) == 1:
            swing_highs.append((i, highs[i]))
        if lows[i] == window_l.min() and list(window_l).count(lows[i]) == 1:
            swing_lows.append((i, lows[i]))

    return swing_highs, swing_lows


# ── Pattern 1: CHOCH (Change of Character) ───────────────────────────────────

def _detect_choch(df: pd.DataFrame) -> tuple[int, str]:
    """
    Downtrend (lower highs, lower lows) → close breaks above most recent swing high → BUY +3
    Uptrend (higher highs, higher lows) → close breaks below most recent swing low  → SELL -3
    Returns (score, detail_string).
    """
    if len(df) < 20:
        return 0, "CHOCH: insufficient bars"

    swing_highs, swing_lows = _find_swings(df, n=3)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return 0, "CHOCH: not enough swings"

    last_close = float(df["Close"].iloc[-1])

    # Last 3 confirmed swing highs and lows
    recent_sh = [p for _, p in swing_highs[-3:]]
    recent_sl = [p for _, p in swing_lows[-3:]]

    # Detect downtrend: each swing high lower than previous
    in_downtrend = all(recent_sh[i] < recent_sh[i - 1] for i in range(1, len(recent_sh)))
    # Detect uptrend: each swing low higher than previous
    in_uptrend   = all(recent_sl[i] > recent_sl[i - 1] for i in range(1, len(recent_sl)))

    last_sh = recent_sh[-1]  # most recent swing high
    last_sl = recent_sl[-1]  # most recent swing low

    if in_downtrend and last_close > last_sh:
        return 3, f"CHOCH: downtrend broken → BUY (close {last_close:.0f} > swing high {last_sh:.0f})"
    if in_uptrend and last_close < last_sl:
        return -3, f"CHOCH: uptrend broken → SELL (close {last_close:.0f} < swing low {last_sl:.0f})"

    return 0, "CHOCH: no character change detected"


# ── Pattern 2: Order Block (Supply & Demand) ─────────────────────────────────

def _detect_order_block(df: pd.DataFrame) -> tuple[int, str]:
    """
    Bearish OB: last bullish candle before a strong bearish BOS move.
                Price returning to that OB zone = SELL.
    Bullish OB: last bearish candle before a strong bullish BOS move.
                Price returning to that OB zone = BUY.
    Returns (score, detail_string).
    """
    if len(df) < 15:
        return 0, "OB: insufficient bars"

    closes = df["Close"].values
    opens  = df["Open"].values
    highs  = df["High"].values
    lows   = df["Low"].values
    last_close = closes[-1]

    # Look back up to 40 bars for OB formations
    lookback = min(40, len(df) - 5)
    ob_zones = []  # (ob_high, ob_low, direction, bar_idx)

    for i in range(len(df) - lookback, len(df) - 3):
        # Bearish OB: bullish candle → followed by strong move down
        if closes[i] > opens[i]:   # bullish candle = potential bearish OB
            future_low = lows[i + 1: i + 6].min()
            swing_range = highs[i] - lows[i]
            if swing_range > 0 and (highs[i] - future_low) > 1.5 * swing_range:
                ob_high = highs[i]
                ob_low  = min(opens[i], closes[i])  # body bottom
                # 50% retracement level
                retracement_50 = ob_low + (ob_high - ob_low) * 0.5
                if last_close >= retracement_50 and last_close <= ob_high * 1.002:
                    ob_zones.append((ob_high, ob_low, "bearish", i))

        # Bullish OB: bearish candle → followed by strong move up
        if closes[i] < opens[i]:   # bearish candle = potential bullish OB
            future_high = highs[i + 1: i + 6].max()
            swing_range = highs[i] - lows[i]
            if swing_range > 0 and (future_high - lows[i]) > 1.5 * swing_range:
                ob_high = max(opens[i], closes[i])  # body top
                ob_low  = lows[i]
                retracement_50 = ob_low + (ob_high - ob_low) * 0.5
                if last_close <= retracement_50 and last_close >= ob_low * 0.998:
                    ob_zones.append((ob_high, ob_low, "bullish", i))

    if not ob_zones:
        return 0, "OB: no active order block"

    # Most recent OB takes priority
    ob_zones.sort(key=lambda x: x[3], reverse=True)
    ob_high, ob_low, direction, idx = ob_zones[0]

    if direction == "bearish":
        return -2, f"OB: price in bearish OB ({ob_low:.0f}–{ob_high:.0f}) → SELL"
    else:
        return 2, f"OB: price in bullish OB ({ob_low:.0f}–{ob_high:.0f}) → BUY"


# ── Pattern 3: Breaker Block ──────────────────────────────────────────────────

def _detect_breaker(df: pd.DataFrame) -> tuple[int, str]:
    """
    Bearish Breaker: a previous swing LOW got broken (bearish BOS),
                     price rallied back UP to that broken level → SELL.
    Bullish Breaker: a previous swing HIGH got broken (bullish BOS),
                     price pulled back DOWN to that broken level → BUY.
    Returns (score, detail_string).
    """
    if len(df) < 20:
        return 0, "Breaker: insufficient bars"

    swing_highs, swing_lows = _find_swings(df, n=3)
    last_close = float(df["Close"].iloc[-1])
    closes = df["Close"].values
    tolerance = 0.002  # 0.2% proximity

    breakers = []

    # Bearish breaker: swing low that was violated (close broke below it)
    for idx, level in swing_lows[:-1]:
        # Was there a close below this level after the swing?
        broke_below = any(closes[j] < level for j in range(idx + 1, len(df) - 1))
        if broke_below:
            # Is current price retesting this broken level from below?
            if level * (1 - tolerance) <= last_close <= level * (1 + tolerance * 3):
                breakers.append(("bearish", level, idx))

    # Bullish breaker: swing high that was violated (close broke above it)
    for idx, level in swing_highs[:-1]:
        broke_above = any(closes[j] > level for j in range(idx + 1, len(df) - 1))
        if broke_above:
            if level * (1 - tolerance * 3) <= last_close <= level * (1 + tolerance):
                breakers.append(("bullish", level, idx))

    if not breakers:
        return 0, "Breaker: no active breaker block"

    # Most recent breaker
    breakers.sort(key=lambda x: x[2], reverse=True)
    direction, level, _ = breakers[0]

    if direction == "bearish":
        return -2, f"Breaker: bearish breaker at {level:.0f} (broken support retested) → SELL"
    else:
        return 2, f"Breaker: bullish breaker at {level:.0f} (broken resistance retested) → BUY"


# ── Pattern 4: FVG (Fair Value Gap) ───────────────────────────────────────────

def _detect_fvg(df: pd.DataFrame) -> tuple[int, str]:
    """
    Bearish FVG: candle[i-2].low > candle[i].high — gap between bars 1 and 3.
                 Price returning into that gap = SELL.
    Bullish FVG: candle[i-2].high < candle[i].low — gap between bars 1 and 3.
                 Price returning into that gap = BUY.
    Returns (score, detail_string).
    """
    if len(df) < 10:
        return 0, "FVG: insufficient bars"

    highs = df["High"].values
    lows  = df["Low"].values
    last_close = float(df["Close"].iloc[-1])

    active_fvgs = []  # (fvg_high, fvg_low, direction, bar_idx)

    # Scan recent bars (skip last 2 since we need confirmation)
    for i in range(2, len(df) - 1):
        # Bearish FVG: high of bar i < low of bar i-2
        if highs[i] < lows[i - 2]:
            fvg_low  = highs[i]
            fvg_high = lows[i - 2]
            # Not yet mitigated (price hasn't filled it)
            mitigated = any(highs[j] >= fvg_low for j in range(i + 1, len(df)))
            if not mitigated and last_close <= fvg_high and last_close >= fvg_low * 0.998:
                active_fvgs.append((fvg_high, fvg_low, "bearish", i))

        # Bullish FVG: low of bar i > high of bar i-2
        if lows[i] > highs[i - 2]:
            fvg_low  = highs[i - 2]
            fvg_high = lows[i]
            mitigated = any(lows[j] <= fvg_high for j in range(i + 1, len(df)))
            if not mitigated and last_close >= fvg_low and last_close <= fvg_high * 1.002:
                active_fvgs.append((fvg_high, fvg_low, "bullish", i))

    if not active_fvgs:
        return 0, "FVG: no price inside active gap"

    # Most recent unmitigated FVG
    active_fvgs.sort(key=lambda x: x[3], reverse=True)
    fvg_high, fvg_low, direction, _ = active_fvgs[0]

    if direction == "bearish":
        return -2, f"FVG: price in bearish gap ({fvg_low:.0f}–{fvg_high:.0f}) → SELL"
    else:
        return 2, f"FVG: price in bullish gap ({fvg_low:.0f}–{fvg_high:.0f}) → BUY"


# ── Pattern 5: AMD (Accumulation-Manipulation-Distribution) ──────────────────

def _detect_amd(df: pd.DataFrame) -> tuple[int, str]:
    """
    1. Accumulation: identify a tight consolidation range (last 10-15 bars,
       range < 0.6× ATR(14)).
    2. Manipulation: a spike candle that breaks out of the range (> 1.3× range).
    3. Distribution: price now reversing back through the range midpoint.
    Returns (score, detail_string).
    """
    if len(df) < 25:
        return 0, "AMD: insufficient bars"

    highs  = df["High"].values
    lows   = df["Low"].values
    closes = df["Close"].values

    # ATR(14)
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(abs(highs[1:] - closes[:-1]), abs(lows[1:] - closes[:-1]))
    )
    atr = float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr))

    # Look for accumulation in bars [-20:-5]
    acc_window = slice(-20, -4)
    acc_high = float(np.max(highs[acc_window]))
    acc_low  = float(np.min(lows[acc_window]))
    acc_range = acc_high - acc_low

    if acc_range > 0.7 * atr * 3:  # not tight enough = not accumulation
        return 0, "AMD: no accumulation range"

    midpoint = (acc_high + acc_low) / 2
    last_close = float(closes[-1])

    # Check for manipulation spike in bars [-5:-1]
    manip_window = slice(-5, -1)
    manip_high = float(np.max(highs[manip_window]))
    manip_low  = float(np.min(lows[manip_window]))

    # Bearish AMD: manipulation spike UP (above acc_high), now distributing down
    if manip_high > acc_high + 0.3 * acc_range and last_close < midpoint:
        return -2, (f"AMD: bearish — range {acc_low:.0f}–{acc_high:.0f}, "
                    f"manipulation spike to {manip_high:.0f}, distributing → SELL")

    # Bullish AMD: manipulation spike DOWN (below acc_low), now distributing up
    if manip_low < acc_low - 0.3 * acc_range and last_close > midpoint:
        return 2, (f"AMD: bullish — range {acc_low:.0f}–{acc_high:.0f}, "
                   f"manipulation spike to {manip_low:.0f}, distributing → BUY")

    return 0, "AMD: manipulation or distribution not confirmed"


# ── Master scorer ─────────────────────────────────────────────────────────────

def score_smc(df: pd.DataFrame) -> dict:
    """
    Run all 5 SMC pattern detectors on the given OHLCV DataFrame.
    Returns a signal dict compatible with Signal Radar and bot_runner.
    """
    if df is None or len(df) < 25:
        return {
            "score": 0, "direction": "HOLD", "action": "HOLD",
            "threshold": 6, "will_trade": False,
            "patterns": {}, "signals": [],
            "note": "SMC: insufficient data",
        }

    results = {}
    try: results["choch"]   = _detect_choch(df)
    except Exception as e:
        results["choch"] = (0, f"CHOCH error: {e}")

    try: results["ob"]      = _detect_order_block(df)
    except Exception as e:
        results["ob"] = (0, f"OB error: {e}")

    try: results["breaker"] = _detect_breaker(df)
    except Exception as e:
        results["breaker"] = (0, f"Breaker error: {e}")

    try: results["fvg"]     = _detect_fvg(df)
    except Exception as e:
        results["fvg"] = (0, f"FVG error: {e}")

    try: results["amd"]     = _detect_amd(df)
    except Exception as e:
        results["amd"] = (0, f"AMD error: {e}")

    scores   = {k: v[0] for k, v in results.items()}
    details  = {k: v[1] for k, v in results.items()}
    signals  = [v[1] for v in results.values() if v[0] != 0]

    raw_score = sum(scores.values())

    # Bonus: +1/-1 if 3+ patterns agree on direction
    bullish_count = sum(1 for s in scores.values() if s > 0)
    bearish_count = sum(1 for s in scores.values() if s < 0)
    if bullish_count >= 3:
        raw_score += 1
        signals.append(f"Confluence bonus: {bullish_count} bullish patterns agree")
    elif bearish_count >= 3:
        raw_score -= 1
        signals.append(f"Confluence bonus: {bearish_count} bearish patterns agree")

    # Clamp to -10/+10
    final_score = max(-10, min(10, raw_score))

    if final_score >= 6:
        direction = "BUY"
    elif final_score <= -6:
        direction = "SELL"
    else:
        direction = "HOLD"

    logger.info(
        "SMC: score=%d direction=%s | CHOCH=%d OB=%d Breaker=%d FVG=%d AMD=%d",
        final_score, direction,
        scores["choch"], scores["ob"], scores["breaker"],
        scores["fvg"], scores["amd"],
    )

    return {
        "score":     final_score,
        "direction": direction,
        "action":    direction,
        "threshold": 6,
        "will_trade": abs(final_score) >= 6,
        "patterns":  scores,
        "signals":   signals,
        "details":   details,
        "note": f"SMC | CHOCH={scores['choch']:+d} OB={scores['ob']:+d} "
                f"Breaker={scores['breaker']:+d} FVG={scores['fvg']:+d} "
                f"AMD={scores['amd']:+d}",
    }
