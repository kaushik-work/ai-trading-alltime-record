"""
NIFTY Raijin — Scalping Options Strategy
============================================

Target    : 30-40% monthly net return on deployed capital
Risk/trade: 4% of equity per trade
R:R       : 1 : 2.0  (SL = 0.6× ATR, TP = 1.2× ATR — quick snaps)
Timeframe : 5-minute bars
Max trades: 3 per day (strict limit — each costs premium + charges)
Entry window: 9:45–10:45  (morning momentum burst)
             14:15–14:45  (closing momentum burst)
Exit        : 15:00 EOD hard close (earlier than intraday)

Core Logic:
-----------
This strategy exploits MEAN REVERSION from VWAP extremes.

When NIFTY gets overextended from its session VWAP (at the 2σ band),
it almost always snaps back toward VWAP. We catch that snap by:

1. VWAP EXTREME : Price reaches VWAP ± 2σ band (overextended)
2. HA REVERSAL  : Heikin-Ashi candle changes colour (momentum reversal)
3. RSI EXTREME  : RSI(9) < 30 (at lower band) OR > 70 (at upper band)
4. VOLUME SPIKE : Volume ≥ 1.5× average (institutional activity, not noise)
5. BODY CLOSE   : Current bar must close in reversal direction (body, not wick)

Score system (0–10):
  Price at/beyond 2σ band  : +3.0
  Price between 1σ–2σ band : +1.5  (softer setup)
  HA colour reversal        : +2.5
  RSI < 30 or > 70          : +2.0
  RSI < 35 or > 65          : +1.0  (softer)
  Volume ≥ 1.5×             : +1.5
  Body close in rev. dir.   : +1.0

Minimum entry score: 8.5 / 10

Target: VWAP (snap back to anchor)
SL    : 0.6× ATR below/above the extreme wick tip

Why this works:
  When NIFTY drops to VWAP−2σ, sellers are exhausted and trapped. The HA
  flip + volume shows smart money stepping in. We ride the mean-reversion
  snap back to VWAP — a move that typically happens within 2-4 bars (10-20 min).
  Using options (CE on bottom snap, PE on top snap) gives leverage with defined risk.
"""

import numpy as np
from datetime import time
from typing import Optional

from strategies.indicators import (
    heikin_ashi, ha_consecutive, ha_color_changed,
    compute_vwap_bands, atr, rsi, is_engulfing, volume_ratio,
)

# ── Timing constants ──────────────────────────────────────────────────────────
TRADE_START_AM  = time(9, 45)
TRADE_END_AM    = time(11, 15)   # extended from 10:45 — captures mid-morning setups
TRADE_START_PM  = time(14, 15)
TRADE_END_PM    = time(14, 45)
EOD_EXIT        = time(15, 0)
MAX_TRADES_DAY  = 3

# ── Thresholds ────────────────────────────────────────────────────────────────
SCORE_THRESHOLD  = 7.5   # lowered from 8.5 — allows ±1σ + RSI soft + HA flip setups
MIN_VOLUME_RATIO = 1.5
RSI_OVERSOLD     = 30
RSI_OVERBOUGHT   = 70
RSI_SOFT_OS      = 35
RSI_SOFT_OB      = 65


def in_entry_window(bar_time: time) -> bool:
    """True if we are in an allowed scalp entry window."""
    return (TRADE_START_AM <= bar_time <= TRADE_END_AM or
            TRADE_START_PM <= bar_time <= TRADE_END_PM)


def score_signal(
    day_opens:   np.ndarray,
    day_highs:   np.ndarray,
    day_lows:    np.ndarray,
    day_closes:  np.ndarray,
    day_volumes: np.ndarray,
    all_closes:  np.ndarray,   # full history for RSI stability
    pcr: float = None,         # live PCR from oi_data.get_pcr() — None = no filter
) -> dict:
    """
    Compute the Raijin signal score for the current 5-min bar.

    Returns dict with:
        action      : 'BUY' | 'SELL' | 'HOLD'
        score       : float 0–10
        buy_score   : float
        sell_score  : float
        details     : component breakdown
        atr_val     : ATR(7) on 5-min bars for SL/TP
        vwap        : session VWAP
        upper2, lower2 : VWAP ± 2σ bands
    """
    if len(day_closes) < 6 or len(all_closes) < 12:
        return _hold("insufficient bars")

    price  = float(day_closes[-1])
    price_o = float(day_opens[-1])
    atr7   = atr(day_highs, day_lows, day_closes, 7)

    # VWAP + bands
    vwap, u1, l1, u2, l2 = compute_vwap_bands(
        day_highs, day_lows, day_closes, day_volumes
    )

    # Heikin-Ashi
    ha_o, ha_h, ha_l, ha_c = heikin_ashi(
        day_opens, day_highs, day_lows, day_closes
    )
    ha_flipped = ha_color_changed(ha_o, ha_c)
    consec     = ha_consecutive(ha_o, ha_c)

    # RSI(9) — faster for scalp
    rsi9 = rsi(all_closes, 9)

    # Volume
    vol_r = volume_ratio(day_volumes, lookback=20)

    # Engulfing on last 2 candles
    if len(day_opens) >= 2:
        engulf = is_engulfing(
            day_opens[-2], day_closes[-2],
            day_opens[-1], day_closes[-1],
        )
    else:
        engulf = "none"

    # Current bar: did price close in reversal direction?
    bull_body = price > price_o     # green close
    bear_body = price < price_o     # red close

    # ── BULLISH score (snap up from lower band → buy CE) ─────────────────────
    buy_score   = 0.0
    buy_details = {}

    # 1. VWAP band extreme: the primary trigger
    if price <= l2:
        buy_score += 3.0
        buy_details["vwap_band"] = 3.0
    elif price <= l1:
        buy_score += 1.5
        buy_details["vwap_band"] = 1.5
    else:
        buy_details["vwap_band"] = 0.0

    # 2. HA reversal: colour just changed to bull
    if ha_flipped and consec >= 1:
        buy_score += 2.5
        buy_details["ha_reversal"] = 2.5
    elif consec >= 1:   # already bull but didn't just flip
        buy_score += 1.0
        buy_details["ha_reversal"] = 1.0
    else:
        buy_details["ha_reversal"] = 0.0

    # 3. RSI extreme: sellers exhausted
    if rsi9 <= RSI_OVERSOLD:
        buy_score += 2.0
        buy_details["rsi_extreme"] = 2.0
    elif rsi9 <= RSI_SOFT_OS:
        buy_score += 1.0
        buy_details["rsi_extreme"] = 1.0
    else:
        buy_details["rsi_extreme"] = 0.0

    # 4. Volume spike: real activity, not noise
    if vol_r >= MIN_VOLUME_RATIO:
        buy_score += 1.5
        buy_details["volume"] = 1.5
    elif vol_r >= 1.2:
        buy_score += 0.7
        buy_details["volume"] = 0.7
    else:
        buy_details["volume"] = 0.0

    # 5. Body close confirms reversal direction
    if bull_body:
        buy_score += 1.0
        buy_details["body_close"] = 1.0
    else:
        buy_details["body_close"] = 0.0

    # 6. Engulfing bonus
    if engulf == "bullish":
        buy_score += 0.5
        buy_details["engulfing"] = 0.5
    else:
        buy_details["engulfing"] = 0.0

    # ── BEARISH score (snap down from upper band → buy PE) ───────────────────
    sell_score   = 0.0
    sell_details = {}

    if price >= u2:
        sell_score += 3.0
        sell_details["vwap_band"] = 3.0
    elif price >= u1:
        sell_score += 1.5
        sell_details["vwap_band"] = 1.5
    else:
        sell_details["vwap_band"] = 0.0

    if ha_flipped and consec <= -1:
        sell_score += 2.5
        sell_details["ha_reversal"] = 2.5
    elif consec <= -1:
        sell_score += 1.0
        sell_details["ha_reversal"] = 1.0
    else:
        sell_details["ha_reversal"] = 0.0

    if rsi9 >= RSI_OVERBOUGHT:
        sell_score += 2.0
        sell_details["rsi_extreme"] = 2.0
    elif rsi9 >= RSI_SOFT_OB:
        sell_score += 1.0
        sell_details["rsi_extreme"] = 1.0
    else:
        sell_details["rsi_extreme"] = 0.0

    if vol_r >= MIN_VOLUME_RATIO:
        sell_score += 1.5
        sell_details["volume"] = 1.5
    elif vol_r >= 1.2:
        sell_score += 0.7
        sell_details["volume"] = 0.7
    else:
        sell_details["volume"] = 0.0

    if bear_body:
        sell_score += 1.0
        sell_details["body_close"] = 1.0
    else:
        sell_details["body_close"] = 0.0

    if engulf == "bearish":
        sell_score += 0.5
        sell_details["engulfing"] = 0.5
    else:
        sell_details["engulfing"] = 0.0

    # ── Resolve ───────────────────────────────────────────────────────────────
    action  = "HOLD"
    score   = 0.0
    details = {}

    if buy_score >= sell_score and buy_score >= SCORE_THRESHOLD:
        action  = "BUY"
        score   = buy_score
        details = buy_details
    elif sell_score > buy_score and sell_score >= SCORE_THRESHOLD:
        action  = "SELL"
        score   = sell_score
        details = sell_details

    # ── PCR gate (Sensibull) ──────────────────────────────────────────────────
    if pcr is not None and action == "BUY" and pcr < 0.8:
        action, score, details = "HOLD", 0.0, {}
    elif pcr is not None and action == "SELL" and pcr > 1.3:
        action, score, details = "HOLD", 0.0, {}

    return {
        "action":     action,
        "score":      round(score, 2),
        "buy_score":  round(buy_score, 2),
        "sell_score": round(sell_score, 2),
        "details":    details,
        "atr":        round(atr7, 2),
        "vwap":       round(vwap, 2),
        "upper1":     round(u1, 2),
        "lower1":     round(l1, 2),
        "upper2":     round(u2, 2),
        "lower2":     round(l2, 2),
        "rsi9":       round(rsi9, 2),
        "ha_consec":  consec,
        "ha_flipped": ha_flipped,
        "vol_ratio":  vol_r,
        "price":      round(price, 2),
    }


def _hold(reason: str = "") -> dict:
    return {
        "action": "HOLD", "score": 0.0,
        "buy_score": 0.0, "sell_score": 0.0,
        "details": {}, "atr": 0.0, "vwap": 0.0,
        "upper1": 0.0, "lower1": 0.0,
        "upper2": 0.0, "lower2": 0.0,
        "rsi9": 50.0, "ha_consec": 0,
        "ha_flipped": False, "vol_ratio": 1.0,
        "price": 0.0, "reason": reason,
    }
