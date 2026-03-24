"""
Mining in Water — Scalping Strategy
Based on PTI "3 Mins Ka Kamal" concept by DP Sir

Core signals:
- W Pattern (Double Bottom)  → BUY
- M Pattern (Double Top)     → SELL
- Heikin Ashi Candle (HAC)   → confirmation
- RSI(14)                    → momentum filter  (>55 BUY / <45 SELL)
- VWAP                       → key intraday level (1.5 pts)
- 50 EMA                     → trend direction
- Volume ratio               → directional boost

Best timeframes: 2m or 5m
Max trades: 6/day
SL: 1.0x ATR  (wide enough to avoid noise)
TP: rr_ratio × ATR  (default 2.0 → 1:2 R:R)
Min score: 7
"""

import numpy as np
from datetime import time

TRADE_START    = time(9, 45)
TRADE_EXIT     = time(15, 0)
MAX_TRADES_DAY = 6


# ── Heikin Ashi ─────────────────────────────────────────────────────────────

def compute_heikin_ashi(opens, highs, lows, closes):
    n        = len(closes)
    ha_close = (opens + highs + lows + closes) / 4
    ha_open  = np.zeros(n, dtype=float)
    ha_open[0] = (opens[0] + closes[0]) / 2
    for i in range(1, n):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2
    ha_bull = ha_close > ha_open
    ha_bear = ha_close < ha_open
    return ha_open, ha_close, ha_bull, ha_bear


# ── W / M Pattern Detection ─────────────────────────────────────────────────

def detect_w_pattern(lows: np.ndarray, current_price: float,
                     lookback: int = 20) -> bool:
    """
    W (Double Bottom):
    - Two similar lows within lookback (diff < 1.5%)  — was 0.5%, too tight for NIFTY
    - Separation ≥ 2 bars (was 3)
    - Second low within last 7 bars (was 5)
    - Current price ≥ 0.1% above avg low
    """
    if len(lows) < lookback:
        return False

    recent     = lows[-lookback:]
    sorted_idx = np.argsort(recent)

    i1, i2 = sorted_idx[0], sorted_idx[1]
    v1, v2 = recent[i1], recent[i2]

    avg_low = (v1 + v2) / 2.0
    if abs(v1 - v2) / avg_low > 0.015:          # 1.5% tolerance
        return False
    if abs(i1 - i2) < 2:                        # at least 2-bar gap
        return False
    if lookback - max(i1, i2) > 7:              # recent bottom within last 7 bars
        return False
    if current_price <= avg_low * 1.001:         # price recovered 0.1% above lows
        return False
    return True


def detect_m_pattern(highs: np.ndarray, current_price: float,
                     lookback: int = 20) -> bool:
    """
    M (Double Top):
    - Two similar highs within lookback (diff < 1.5%)
    - Separation ≥ 2 bars
    - Second high within last 7 bars
    - Current price ≤ 0.1% below avg high
    """
    if len(highs) < lookback:
        return False

    recent     = highs[-lookback:]
    sorted_idx = np.argsort(recent)[::-1]

    i1, i2 = sorted_idx[0], sorted_idx[1]
    v1, v2 = recent[i1], recent[i2]

    avg_high = (v1 + v2) / 2.0
    if abs(v1 - v2) / avg_high > 0.015:
        return False
    if abs(i1 - i2) < 2:
        return False
    if lookback - max(i1, i2) > 7:
        return False
    if current_price >= avg_high * 0.999:
        return False
    return True


# ── VWAP ─────────────────────────────────────────────────────────────────────

def compute_vwap(highs, lows, closes, volumes):
    typical = (highs + lows + closes) / 3.0
    cum_tv  = np.cumsum(typical * volumes)
    cum_v   = np.cumsum(volumes)
    with np.errstate(divide='ignore', invalid='ignore'):
        vwap = np.where(cum_v > 0, cum_tv / cum_v, typical)
    return vwap


# ── ATR ──────────────────────────────────────────────────────────────────────

def compute_atr(highs, lows, closes, period: int = 14):
    prev_c = np.roll(closes, 1)
    prev_c[0] = closes[0]
    tr = np.maximum(highs - lows,
         np.maximum(np.abs(highs - prev_c), np.abs(lows - prev_c)))
    atr = np.zeros(len(tr))
    atr[0] = tr[0]
    for i in range(1, len(tr)):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


# ── EMA ──────────────────────────────────────────────────────────────────────

def compute_ema(closes, period: int):
    ema = np.zeros(len(closes))
    ema[0] = closes[0]
    k = 2.0 / (period + 1)
    for i in range(1, len(closes)):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema


# ── RSI ──────────────────────────────────────────────────────────────────────

def compute_rsi(closes, period: int = 14):
    n   = len(closes)
    rsi = np.full(n, 50.0)
    if n <= period:
        return rsi
    deltas = np.diff(closes.astype(float))
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = float(np.mean(gains[:period]))
    avg_l  = float(np.mean(losses[:period]))
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs    = avg_g / avg_l if avg_l > 0 else 100.0
        rsi[i + 1] = 100.0 - 100.0 / (1.0 + rs)
    return rsi


# ── Signal Scorer ────────────────────────────────────────────────────────────

def score_scalp(
    ha_bull: bool,
    ha_bear: bool,
    w_pattern: bool,
    m_pattern: bool,
    price_above_vwap: bool,
    price_above_ema50: bool,
    vol_ratio: float,
    consecutive_ha: int = 0,
    rsi: float = 50.0,
) -> dict:
    """
    Score scalping signal. Returns action and score (0–12.5).
    Minimum score 7 required to fire a trade.

    Weights:
      W/M pattern  : 4.0 pts
      HAC          : 3.0 pts  (+0.25 per consecutive candle, max 1.0 bonus)
      VWAP         : 1.5 pts  (most important intraday level)
      EMA-50       : 1.5 pts  (trend filter)
      RSI          : 1.0 pt   (momentum: >55 buy / <45 sell)
      Volume       : 0.5 pt   (directional — only boosts the scoring side)
    """
    buy_score  = 0.0
    sell_score = 0.0

    # Pattern (4 pts) — VWAP-gated: pattern only counts if price is on the correct
    # side of VWAP.  Prevents taking a "W bottom" entry when price is already
    # below VWAP (counter-trend), which is the #1 source of false signals.
    if w_pattern and price_above_vwap:
        buy_score  += 4.0
    if m_pattern and not price_above_vwap:
        sell_score += 4.0

    # HAC (3 pts + up to 1 pt consecutive bonus)
    if ha_bull:
        buy_score  += 3.0 + min(consecutive_ha * 0.25, 1.0)
    if ha_bear:
        sell_score += 3.0 + min(consecutive_ha * 0.25, 1.0)

    # VWAP (1.5 pts — most important for scalping)
    if price_above_vwap:
        buy_score  += 1.5
    else:
        sell_score += 1.5

    # EMA-50 trend (1.5 pts)
    if price_above_ema50:
        buy_score  += 1.5
    else:
        sell_score += 1.5

    # RSI momentum (1 pt — directional)
    if rsi > 55:
        buy_score  += 1.0
    elif rsi < 45:
        sell_score += 1.0

    # Volume directional boost (0.5 pt — only to the leading side)
    if vol_ratio >= 1.2:
        if buy_score > sell_score:
            buy_score  += 0.5
        else:
            sell_score += 0.5

    if buy_score >= 7.0 and buy_score > sell_score:
        return {"action": "BUY",  "score": round(buy_score, 1)}
    if sell_score >= 7.0 and sell_score > buy_score:
        return {"action": "SELL", "score": round(sell_score, 1)}
    return {"action": "HOLD", "score": round(max(buy_score, sell_score), 1)}
