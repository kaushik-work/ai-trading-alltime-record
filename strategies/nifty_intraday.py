"""
NIFTY Musashi — Intraday Options Strategy
==============================================

Target    : 30-40% monthly net return on deployed capital
Risk/trade: 2% of equity per trade (backtest optimal)
R:R       : 1 : 2.5  (SL = 1.25× ATR, TP = 3.125× ATR)
Timeframe : 5-minute bars
Max trades: 2 per day (quality over quantity)
Entry window: 9:45–11:30  +  14:00–14:30 (PM tightened to avoid post-lunch chop)
Exit        : 15:10 EOD hard close

Core logic — PULLBACK TO SUPPORT (not momentum chase):
------------------------------------------------------
The strategy only enters when price RETURNS to the EMA21 level after a trend
has been established. Entries far from EMA21 are chases — blocked by hard gate.

1. EMA STACK    : EMA8 > EMA21 (bullish alignment) — trend established
2. VWAP BIAS    : Price above/below VWAP — intraday trend direction
3. EMA21 SLOPE  : EMA21 must be rising (BUY) or falling (SELL) — trend health
4. PULLBACK     : Price within 1.0% of EMA21 — HARD GATE, no trade if beyond
5. HA FLIP      : Heikin-Ashi colour changed at EMA21 — reversal confirmed
6. HA CONSEC    : ≥1 consecutive HA in trade direction — momentum aligned
7. RSI FILTER   : RSI(14) in 38–62 zone — not at extremes at entry
8. VOLUME       : Volume ≥ 1.2× 20-bar average — real conviction
9. STRUCTURE    : Swing structure aligned — macro trend confirmation
10. EMA50 GATE  : Price must be above EMA50 for BUY, below for SELL

Score system (0–13):
  EMA stack aligned         : +2.0
  VWAP bias correct         : +2.0
  Pullback ≤ 0.3% of EMA21 : +2.5   (≤ 0.6%: +1.5, ≤ 1.0%: +0.5)
  EMA21 slope in direction  : +1.0   (NEW — prevents entries on flat/declining EMA)
  HA colour flip at EMA21   : +1.5   (NEW — fresh reversal = actual entry signal)
  HA consecutive ≥ 1        : +0.5   (confirmation that flip held)
  RSI 38–62                 : +1.0
  Volume ≥ 1.2×             : +1.0
  Swing structure aligned   : +0.5
  Pin bar or engulfing      : +1.0   (bonus — high-conviction pattern at EMA21)

Minimum entry score: 8.5 / 13
Hard gates (HOLD regardless of score):
  - Price > 1.0% from EMA21 (chase entry — blocked)
  - Price on wrong side of EMA50
  - PCR extreme (< 0.8 for BUY, > 1.3 for SELL)

Why this works:
  Trend-pullback entries at EMA21 have a defined support level immediately below.
  SL sits just below EMA21 (= ATR distance), so risk is quantified.
  Without the pullback gate, the strategy was entering momentum moves mid-air
  with no support — 28% win rate. With the gate, we only enter at tested support.
"""

import numpy as np
from datetime import time
from typing import Optional

from strategies.indicators import (
    heikin_ashi, ha_consecutive, ha_color_changed, compute_vwap_bands,
    detect_swing_structure, ema, ema_series, atr, rsi,
    is_pin_bar, is_engulfing, volume_ratio,
)

# ── Timing constants ──────────────────────────────────────────────────────────
TRADE_START_AM  = time(9, 45)
TRADE_END_AM    = time(11, 30)
TRADE_START_PM  = time(14, 0)    # was 13:30 — post-lunch chop filtered out
TRADE_END_PM    = time(14, 30)
EOD_EXIT        = time(15, 10)
MAX_TRADES_DAY  = 2

# ── Thresholds ────────────────────────────────────────────────────────────────
SCORE_THRESHOLD      = 8.5
PULLBACK_TIGHT_PCT   = 0.003   # ≤0.3% of EMA21 → full pullback score
PULLBACK_WIDE_PCT    = 0.006   # ≤0.6% of EMA21 → partial score
PULLBACK_GATE_PCT    = 0.010   # >1.0% of EMA21 → hard gate, no trade
RSI_MIN              = 38      # tightened from 35
RSI_MAX              = 62      # tightened from 65
MIN_VOLUME_RATIO     = 1.2


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
    vix: float = None,        # India VIX — passed to output for brain context only
) -> dict:
    """
    Compute the Musashi signal score for the current bar.

    Returns dict with:
        buy_score   : float 0–13
        sell_score  : float 0–13
        action      : 'BUY' | 'SELL' | 'HOLD'
        details     : dict of individual component scores
        atr_val     : ATR(14) on 5-min bars for SL/TP sizing
        vwap        : session VWAP
    """
    if len(day_closes) < 5 or len(all_closes) < 52:
        return _hold("insufficient bars")

    # ── Raw indicators ────────────────────────────────────────────────────────
    price  = float(day_closes[-1])
    atr14  = atr(day_highs, day_lows, day_closes, 14)

    # EMAs from full history (stable)
    ema8_val  = ema(all_closes, 8)
    ema21_val = ema(all_closes, 21)
    ema50_val = ema(all_closes, 50)
    rsi14     = rsi(all_closes, 14)

    # EMA21 slope: compare last value to 3 bars ago
    ema21_series = ema_series(all_closes, 21)
    ema21_rising  = float(ema21_series[-1]) > float(ema21_series[-4]) if len(ema21_series) >= 4 else False
    ema21_falling = float(ema21_series[-1]) < float(ema21_series[-4]) if len(ema21_series) >= 4 else False

    # VWAP from day-open
    vwap, u1, l1, u2, l2 = compute_vwap_bands(
        day_highs, day_lows, day_closes, day_volumes
    )

    # Heikin-Ashi
    ha_o, ha_h, ha_l, ha_c = heikin_ashi(
        day_opens, day_highs, day_lows, day_closes
    )
    consec    = ha_consecutive(ha_o, ha_c)
    ha_flipped = ha_color_changed(ha_o, ha_c)

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

    # ── Pullback distance (shared for buy/sell) ───────────────────────────────
    pullback_pct = abs(price - ema21_val) / ema21_val if ema21_val > 0 else 1.0

    # ── HARD GATE: if price too far from EMA21, no trade regardless of score ──
    if pullback_pct > PULLBACK_GATE_PCT:
        return _hold("price too far from EMA21 — chase entry blocked")

    # ── BULLISH scoring (CE) ─────────────────────────────────────────────────
    buy_score = 0.0
    buy_details = {}

    # 1. EMA stack: short-term momentum up
    if ema8_val > ema21_val:
        buy_score += 2.0
        buy_details["ema_stack"] = 2.0
    else:
        buy_details["ema_stack"] = 0.0

    # 2. VWAP bias: intraday bulls in control
    if price > vwap:
        buy_score += 2.0
        buy_details["vwap_bias"] = 2.0
    else:
        buy_details["vwap_bias"] = 0.0

    # 3. Pullback zone: best entries are ON the EMA, not extended
    if pullback_pct <= PULLBACK_TIGHT_PCT and price >= ema21_val:
        buy_score += 2.5
        buy_details["pullback_ema21"] = 2.5
    elif pullback_pct <= PULLBACK_WIDE_PCT and price >= ema21_val:
        buy_score += 1.5
        buy_details["pullback_ema21"] = 1.5
    elif pullback_pct <= PULLBACK_GATE_PCT and price >= ema21_val:
        buy_score += 0.5
        buy_details["pullback_ema21"] = 0.5
    else:
        buy_details["pullback_ema21"] = 0.0

    # 4. EMA21 slope: must be rising — flat/declining EMA21 = no trend to ride
    if ema21_rising:
        buy_score += 1.0
        buy_details["ema21_slope"] = 1.0
    else:
        buy_details["ema21_slope"] = 0.0

    # 5. HA flip: fresh reversal candle at EMA21 = actual entry trigger
    if ha_flipped and consec >= 1:
        buy_score += 1.5
        buy_details["ha_flip"] = 1.5
    else:
        buy_details["ha_flip"] = 0.0

    # 6. HA consecutive: momentum confirmed after flip
    if consec >= 1:
        buy_score += 0.5
        buy_details["ha_consec"] = 0.5
    else:
        buy_details["ha_consec"] = 0.0

    # 7. RSI filter: not overbought at entry (tighter: 38–62)
    if RSI_MIN <= rsi14 <= RSI_MAX:
        buy_score += 1.0
        buy_details["rsi"] = 1.0
    else:
        buy_details["rsi"] = 0.0

    # 8. Volume: real buyers
    if vol_r >= MIN_VOLUME_RATIO:
        buy_score += 1.0
        buy_details["volume"] = 1.0
    elif vol_r >= 1.0:
        buy_score += 0.3
        buy_details["volume"] = 0.3
    else:
        buy_details["volume"] = 0.0

    # 9. Swing structure bonus
    if structure == "uptrend":
        buy_score += 0.5
        buy_details["structure"] = 0.5
    else:
        buy_details["structure"] = 0.0

    # 10. Price action pattern bonus
    if pin == "bullish" or engulf == "bullish":
        buy_score += 1.0
        buy_details["pa_pattern"] = 1.0
    else:
        buy_details["pa_pattern"] = 0.0

    # ── BEARISH scoring (PE) ─────────────────────────────────────────────────
    sell_score = 0.0
    sell_details = {}

    if ema8_val < ema21_val:
        sell_score += 2.0
        sell_details["ema_stack"] = 2.0
    else:
        sell_details["ema_stack"] = 0.0

    if price < vwap:
        sell_score += 2.0
        sell_details["vwap_bias"] = 2.0
    else:
        sell_details["vwap_bias"] = 0.0

    if pullback_pct <= PULLBACK_TIGHT_PCT and price <= ema21_val:
        sell_score += 2.5
        sell_details["pullback_ema21"] = 2.5
    elif pullback_pct <= PULLBACK_WIDE_PCT and price <= ema21_val:
        sell_score += 1.5
        sell_details["pullback_ema21"] = 1.5
    elif pullback_pct <= PULLBACK_GATE_PCT and price <= ema21_val:
        sell_score += 0.5
        sell_details["pullback_ema21"] = 0.5
    else:
        sell_details["pullback_ema21"] = 0.0

    if ema21_falling:
        sell_score += 1.0
        sell_details["ema21_slope"] = 1.0
    else:
        sell_details["ema21_slope"] = 0.0

    if ha_flipped and consec <= -1:
        sell_score += 1.5
        sell_details["ha_flip"] = 1.5
    else:
        sell_details["ha_flip"] = 0.0

    if consec <= -1:
        sell_score += 0.5
        sell_details["ha_consec"] = 0.5
    else:
        sell_details["ha_consec"] = 0.0

    if RSI_MIN <= rsi14 <= RSI_MAX:
        sell_score += 1.0
        sell_details["rsi"] = 1.0
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

    # ── EMA50 trend gate ──────────────────────────────────────────────────────
    # Only trade in the direction EMA50 permits — eliminates counter-trend entries.
    if action == "BUY" and price < ema50_val:
        action, score, details = "HOLD", 0.0, {}
    elif action == "SELL" and price > ema50_val:
        action, score, details = "HOLD", 0.0, {}

    # ── PCR gate (Sensibull) ──────────────────────────────────────────────────
    if pcr is not None and action == "BUY" and pcr < 0.8:
        action, score, details = "HOLD", 0.0, {}
    elif pcr is not None and action == "SELL" and pcr > 1.3:
        action, score, details = "HOLD", 0.0, {}

    return {
        "action":      action,
        "score":       round(score, 2),
        "buy_score":   round(buy_score, 2),
        "sell_score":  round(sell_score, 2),
        "details":     details,
        "atr":         round(atr14, 2),
        "vwap":        round(vwap, 2),
        "ema8":        round(ema8_val, 2),
        "ema21":       round(ema21_val, 2),
        "ema50":       round(ema50_val, 2),
        "ema21_slope": "rising" if ema21_rising else ("falling" if ema21_falling else "flat"),
        "rsi":         round(rsi14, 2),
        "ha_consec":   consec,
        "ha_flipped":  ha_flipped,
        "vol_ratio":   vol_r,
        "structure":   structure,
        "price":       round(price, 2),
        "vix":         vix,
    }


def _hold(reason: str = "") -> dict:
    return {
        "action": "HOLD", "score": 0.0,
        "buy_score": 0.0, "sell_score": 0.0,
        "details": {}, "atr": 0.0, "vwap": 0.0,
        "ema8": 0.0, "ema21": 0.0, "ema50": 0.0,
        "ema21_slope": "flat", "rsi": 50.0,
        "ha_consec": 0, "ha_flipped": False, "vol_ratio": 1.0,
        "structure": "sideways", "price": 0.0,
        "vix": None, "reason": reason,
    }
