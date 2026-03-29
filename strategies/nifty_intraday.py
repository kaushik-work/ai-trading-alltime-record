"""
NIFTY Musashi — Intraday Options Strategy
==============================================

Target    : 30-40% monthly net return on deployed capital
Risk/trade: 4% of equity per trade
R:R       : 1 : 2.5  (SL = 1.25× ATR, TP = 3.125× ATR)
Timeframe : 15-minute bars
Max trades: 2 per day (quality over quantity)
Entry window: 9:45–11:30  +  13:30–14:30 (avoid lunch chop)
Exit        : 15:10 EOD hard close

Core logic (multi-layer confluence):
--------------------------------------
1. EMA STACK  : EMA8 > EMA21 (bullish alignment) OR EMA8 < EMA21 (bearish)
2. VWAP BIAS  : Price above/below session VWAP → only trade in VWAP direction
3. PULLBACK   : Price has pulled back to/near EMA21 (not extended, not overbought entry)
4. HA CONFIRM : ≥ 2 consecutive Heikin-Ashi candles in trade direction
5. RSI FILTER : RSI(14) in 35–65 zone (not at extremes at entry)
6. VOLUME     : Volume ≥ 1.2× 20-bar average (real conviction behind the move)
7. STRUCTURE  : 15-min swing structure aligned (uptrend for CE, downtrend for PE)

Score system (0–10):
  EMA8 > EMA21           : +2.5
  Price above VWAP       : +2.0
  Pullback to EMA21 zone : +2.0
  HA consecutive ≥ 2     : +1.5
  RSI 38–62              : +1.0   (RSI near extremes = 0)
  Volume ≥ 1.2×          : +1.0
  Swing structure aligned: bonus +0.5 if uptrend confirmed

Minimum entry score: 8.5 / 10

Pin bar or engulfing at EMA21 adds +1.0 bonus (can push marginal setups over threshold).

Bearish mirror: all conditions reversed → buy PE.
"""

import numpy as np
from datetime import time
from typing import Optional

from strategies.indicators import (
    heikin_ashi, ha_consecutive, compute_vwap_bands,
    detect_swing_structure, ema, ema_series, atr, rsi,
    is_pin_bar, is_engulfing, volume_ratio,
)

# ── Timing constants ──────────────────────────────────────────────────────────
TRADE_START_AM  = time(9, 45)
TRADE_END_AM    = time(11, 30)
TRADE_START_PM  = time(13, 30)
TRADE_END_PM    = time(14, 30)
EOD_EXIT        = time(15, 10)
MAX_TRADES_DAY  = 2

# ── Thresholds ────────────────────────────────────────────────────────────────
SCORE_THRESHOLD     = 7.5
PULLBACK_ZONE_PCT   = 0.004    # price within 0.4% of EMA21 = "at the level"
RSI_MIN             = 35       # don't buy when RSI < 35 (possible continued weakness)
RSI_MAX             = 65       # don't buy when RSI > 65 (overbought at entry)
MIN_VOLUME_RATIO    = 1.2      # volume must be at least 1.2× average


def in_entry_window(bar_time: time) -> bool:
    """True if we are in an allowed entry window."""
    return (TRADE_START_AM <= bar_time <= TRADE_END_AM or
            TRADE_START_PM <= bar_time <= TRADE_END_PM)


def score_signal(
    day_opens: np.ndarray,
    day_highs: np.ndarray,
    day_lows:  np.ndarray,
    day_closes: np.ndarray,
    day_volumes: np.ndarray,
    all_closes: np.ndarray,   # full history up to this bar (for EMA/RSI stability)
    pcr: float = None,        # live PCR from oi_data.get_pcr() — None = no filter
) -> dict:
    """
    Compute the Musashi signal score for the current bar.

    Args:
        day_*    : arrays for the current trading day up to and including current bar
        all_closes: all closes from the loaded history up to the current bar

    Returns dict with:
        buy_score   : float 0–10+
        sell_score  : float 0–10+
        action      : 'BUY' | 'SELL' | 'HOLD'
        details     : dict of individual component scores
        atr_val     : ATR(14) on 15-min bars for SL/TP sizing
        vwap        : session VWAP
    """
    if len(day_closes) < 5 or len(all_closes) < 22:
        return _hold("insufficient bars")

    # ── Raw indicators ────────────────────────────────────────────────────────
    price  = float(day_closes[-1])
    atr14  = atr(day_highs, day_lows, day_closes, 14)

    # EMAs from full history (stable)
    ema8_val  = ema(all_closes, 8)
    ema21_val = ema(all_closes, 21)
    ema50_val = ema(all_closes, 50)
    rsi14     = rsi(all_closes, 14)

    # VWAP + bands from day-open
    vwap, u1, l1, u2, l2 = compute_vwap_bands(
        day_highs, day_lows, day_closes, day_volumes
    )

    # Heikin-Ashi
    ha_o, ha_h, ha_l, ha_c = heikin_ashi(
        day_opens, day_highs, day_lows, day_closes
    )
    consec = ha_consecutive(ha_o, ha_c)

    # Volume
    vol_r = volume_ratio(day_volumes, lookback=20)

    # Swing structure
    structure = detect_swing_structure(day_highs, day_lows, lookback=min(30, len(day_highs)))

    # Last two candles for pattern detection
    if len(day_opens) >= 2:
        pin    = is_pin_bar(day_opens[-1], day_highs[-1], day_lows[-1], day_closes[-1])
        engulf = is_engulfing(
            day_opens[-2], day_closes[-2],
            day_opens[-1], day_closes[-1],
        )
    else:
        pin, engulf = "none", "none"

    # ── BULLISH scoring (CE) ─────────────────────────────────────────────────
    buy_score = 0.0
    buy_details = {}

    # 1. EMA stack: EMA8 > EMA21 (short-term momentum up)
    if ema8_val > ema21_val:
        buy_score += 2.5
        buy_details["ema_stack"] = 2.5
    else:
        buy_details["ema_stack"] = 0.0

    # 2. VWAP bias: price above VWAP = intraday bulls in control
    if price > vwap:
        buy_score += 2.0
        buy_details["vwap_bias"] = 2.0
    else:
        buy_details["vwap_bias"] = 0.0

    # 3. Pullback to EMA21 zone: best entries are ON the EMA, not far away
    pullback_pct = abs(price - ema21_val) / ema21_val if ema21_val > 0 else 1.0
    if pullback_pct <= PULLBACK_ZONE_PCT and price >= ema21_val:
        buy_score += 2.0
        buy_details["pullback_ema21"] = 2.0
    elif pullback_pct <= PULLBACK_ZONE_PCT * 2 and price >= ema21_val:
        buy_score += 1.0
        buy_details["pullback_ema21"] = 1.0
    else:
        buy_details["pullback_ema21"] = 0.0

    # 4. HA consecutive: ≥2 bull HA candles = momentum confirmed
    if consec >= 2:
        buy_score += 1.5
        buy_details["ha_consec"] = 1.5
    elif consec == 1:
        buy_score += 0.5
        buy_details["ha_consec"] = 0.5
    else:
        buy_details["ha_consec"] = 0.0

    # 5. RSI filter: not overbought at entry
    if RSI_MIN <= rsi14 <= RSI_MAX:
        buy_score += 1.0
        buy_details["rsi"] = 1.0
    elif rsi14 < RSI_MIN:
        buy_score += 0.3   # potential bounce but weak momentum
        buy_details["rsi"] = 0.3
    else:
        buy_details["rsi"] = 0.0  # overbought — no bonus

    # 6. Volume: needs real buyers
    if vol_r >= MIN_VOLUME_RATIO:
        buy_score += 1.0
        buy_details["volume"] = 1.0
    elif vol_r >= 1.0:
        buy_score += 0.3
        buy_details["volume"] = 0.3
    else:
        buy_details["volume"] = 0.0

    # 7. Swing structure alignment bonus
    if structure == "uptrend":
        buy_score += 0.5
        buy_details["structure"] = 0.5
    else:
        buy_details["structure"] = 0.0

    # 8. Price action pattern bonus (at EMA21 = high conviction)
    if pin == "bullish" or engulf == "bullish":
        buy_score += 1.0
        buy_details["pa_pattern"] = 1.0
    else:
        buy_details["pa_pattern"] = 0.0

    # ── BEARISH scoring (PE) ─────────────────────────────────────────────────
    sell_score = 0.0
    sell_details = {}

    if ema8_val < ema21_val:
        sell_score += 2.5
        sell_details["ema_stack"] = 2.5
    else:
        sell_details["ema_stack"] = 0.0

    if price < vwap:
        sell_score += 2.0
        sell_details["vwap_bias"] = 2.0
    else:
        sell_details["vwap_bias"] = 0.0

    pullback_short = abs(price - ema21_val) / ema21_val if ema21_val > 0 else 1.0
    if pullback_short <= PULLBACK_ZONE_PCT and price <= ema21_val:
        sell_score += 2.0
        sell_details["pullback_ema21"] = 2.0
    elif pullback_short <= PULLBACK_ZONE_PCT * 2 and price <= ema21_val:
        sell_score += 1.0
        sell_details["pullback_ema21"] = 1.0
    else:
        sell_details["pullback_ema21"] = 0.0

    if consec <= -2:
        sell_score += 1.5
        sell_details["ha_consec"] = 1.5
    elif consec == -1:
        sell_score += 0.5
        sell_details["ha_consec"] = 0.5
    else:
        sell_details["ha_consec"] = 0.0

    if RSI_MIN <= rsi14 <= RSI_MAX:
        sell_score += 1.0
        sell_details["rsi"] = 1.0
    elif rsi14 > RSI_MAX:
        sell_score += 0.3
        sell_details["rsi"] = 0.3
    else:
        sell_details["rsi"] = 0.0

    if vol_r >= MIN_VOLUME_RATIO:
        sell_score += 1.0
        sell_details["volume"] = 1.0
    elif vol_r >= 1.0:
        sell_score += 0.3
        sell_details["volume"] = 0.3
    else:
        sell_details["volume"] = 0.0

    if structure == "downtrend":
        sell_score += 0.5
        sell_details["structure"] = 0.5
    else:
        sell_details["structure"] = 0.0

    if pin == "bearish" or engulf == "bearish":
        sell_score += 1.0
        sell_details["pa_pattern"] = 1.0
    else:
        sell_details["pa_pattern"] = 0.0

    # ── Resolve action ────────────────────────────────────────────────────────
    action = "HOLD"
    score  = 0.0
    details = {}

    if buy_score >= sell_score and buy_score >= SCORE_THRESHOLD:
        action  = "BUY"
        score   = buy_score
        details = buy_details
    elif sell_score > buy_score and sell_score >= SCORE_THRESHOLD:
        action  = "SELL"
        score   = sell_score
        details = sell_details

    # ── EMA50 trend gate (AishDoc) ────────────────────────────────────────────
    # Only trade in the direction EMA50 permits — eliminates counter-trend entries.
    if action == "BUY" and price < ema50_val:
        action, score, details = "HOLD", 0.0, {}
    elif action == "SELL" and price > ema50_val:
        action, score, details = "HOLD", 0.0, {}

    # ── PCR gate (Sensibull) ──────────────────────────────────────────────────
    # Skip BUY when market is too bearish; skip SELL when too bullish.
    if pcr is not None and action == "BUY" and pcr < 0.8:
        action, score, details = "HOLD", 0.0, {}
    elif pcr is not None and action == "SELL" and pcr > 1.3:
        action, score, details = "HOLD", 0.0, {}

    return {
        "action":    action,
        "score":     round(score, 2),
        "buy_score": round(buy_score, 2),
        "sell_score": round(sell_score, 2),
        "details":   details,
        "atr":       round(atr14, 2),
        "vwap":      round(vwap, 2),
        "ema8":      round(ema8_val, 2),
        "ema21":     round(ema21_val, 2),
        "ema50":     round(ema50_val, 2),
        "rsi":       round(rsi14, 2),
        "ha_consec": consec,
        "vol_ratio": vol_r,
        "structure": structure,
        "price":     round(price, 2),
    }


def _hold(reason: str = "") -> dict:
    return {
        "action": "HOLD", "score": 0.0,
        "buy_score": 0.0, "sell_score": 0.0,
        "details": {}, "atr": 0.0, "vwap": 0.0,
        "ema8": 0.0, "ema21": 0.0, "rsi": 50.0,
        "ha_consec": 0, "vol_ratio": 1.0,
        "structure": "sideways", "price": 0.0,
        "reason": reason,
    }
