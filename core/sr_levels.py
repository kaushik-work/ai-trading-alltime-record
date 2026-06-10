"""
S/R Level Detection and Market Structure Analysis.

Two-layer approach:
  Layer 1 (Swing S/R lines):
    Detect swing highs/lows (local max/min), cluster, score by touch count.
    These are the labelled R1/R2/S1/S2 horizontal lines on the chart.

  Layer 2 (Institutional supply/demand zones):
    Rally-Base-Drop  (RBD) → Supply zone: price rallied, consolidated, then DROPPED.
                              The base candle bodies = where sell orders sit.
    Drop-Base-Rally  (DBR) → Demand zone: price dropped, consolidated, then RALLIED.
                              The base candle bodies = where buy orders sit.
    Fresh zone = not revisited since formation = strongest.
"""

import logging
import time
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_cache: Optional[dict] = None
_cache_ts: float = 0.0
_CACHE_TTL = 900   # 15 minutes


def compute_sr_levels(
    df: pd.DataFrame,
    n_swing: int = 5,
    tolerance: float = 20.0,
    min_strength: int = 2,
    max_levels: int = 10,
    # RBD/DBR params
    impulse_atr_mult: float = 0.75,   # body >= N × ATR = "strong" bar
    base_body_mult: float  = 0.45,    # body <  N × ATR = "base" bar
    base_max_bars: int = 4,
    max_zones: int = 8,
) -> dict:
    """
    Compute S/R levels, institutional supply/demand zones, and market structure.

    Returns dict with:
      levels              list of {price, type, strength, zone_top, zone_bot, kind}
      support             levels below current price (sorted desc)
      resistance          levels above current price (sorted asc)
      supply_zones        [{top, bottom, price, strength}]
      demand_zones        [{top, bottom, price, strength}]
      structure           "uptrend" | "downtrend" | "ranging"
      position            "at_resistance" | "at_support" | "breaking_up" |
                          "breaking_down" | "open_air"
      current_price       float
      nearest_support     float | None
      nearest_resistance  float | None
    """
    if df is None or len(df) < n_swing * 2 + 1:
        return _empty()

    df = df.copy()
    for col in ["High", "Low", "Close", "Open"]:
        if col not in df.columns and col.lower() in df.columns:
            df[col] = df[col.lower()]
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["High", "Low", "Close", "Open"], inplace=True)

    highs  = df["High"].values.astype(float)
    lows   = df["Low"].values.astype(float)
    closes = df["Close"].values.astype(float)
    opens  = df["Open"].values.astype(float)
    n      = len(df)

    current_price = float(closes[-1])

    # ── Layer 1: Swing S/R horizontal lines ─────────────────────────────────
    swing_highs, swing_lows = _detect_swings(highs, lows, closes, opens, n, n_swing)
    levels = _build_levels(swing_highs, swing_lows, current_price, tolerance, min_strength, max_levels)

    support    = sorted([l for l in levels if l["type"] == "support"],
                        key=lambda x: x["price"], reverse=True)
    resistance = sorted([l for l in levels if l["type"] == "resistance"],
                        key=lambda x: x["price"])

    nearest_support    = support[0]["price"]    if support    else None
    nearest_resistance = resistance[0]["price"] if resistance else None

    # ── Layer 2: Institutional RBD/DBR zones ────────────────────────────────
    supply_zones, demand_zones = _detect_institutional_zones(
        highs, lows, closes, opens, n,
        impulse_atr_mult=impulse_atr_mult,
        base_body_mult=base_body_mult,
        base_max_bars=base_max_bars,
        max_zones=max_zones,
        current_price=current_price,
    )

    # Fallback to swing-body zones if RBD/DBR found nothing
    if not supply_zones:
        supply_zones = [
            {"top": l["zone_top"], "bottom": l["zone_bot"], "price": l["price"], "strength": l["strength"]}
            for l in levels if l["kind"] == "supply" and l["type"] == "resistance"
        ]
    if not demand_zones:
        demand_zones = [
            {"top": l["zone_top"], "bottom": l["zone_bot"], "price": l["price"], "strength": l["strength"]}
            for l in levels if l["kind"] == "demand" and l["type"] == "support"
        ]

    # ── Market structure ─────────────────────────────────────────────────────
    structure = _detect_structure(closes)

    # ── Current position vs nearest levels ──────────────────────────────────
    position = _classify_position(closes, nearest_support, nearest_resistance, prox=25)

    return {
        "levels":             levels,
        "support":            support,
        "resistance":         resistance,
        "supply_zones":       supply_zones,
        "demand_zones":       demand_zones,
        "structure":          structure,
        "position":           position,
        "current_price":      current_price,
        "nearest_support":    nearest_support,
        "nearest_resistance": nearest_resistance,
    }


# ── Swing detection ──────────────────────────────────────────────────────────

def _detect_swings(highs, lows, closes, opens, n, n_swing):
    swing_highs, swing_lows = [], []
    for i in range(n_swing, n - n_swing):
        wh = highs[i - n_swing: i + n_swing + 1]
        wl = lows[i - n_swing:  i + n_swing + 1]
        if highs[i] == wh.max():
            body_top = max(opens[i], closes[i])
            body_bot = min(opens[i], closes[i])
            swing_highs.append((i, float(highs[i]), float(body_top), float(body_bot)))
        if lows[i] == wl.min():
            body_top = max(opens[i], closes[i])
            body_bot = min(opens[i], closes[i])
            swing_lows.append((i, float(lows[i]), float(body_top), float(body_bot)))
    return swing_highs, swing_lows


def _cluster(swings, tolerance, price_idx=1):
    if not swings:
        return []
    sorted_s = sorted(swings, key=lambda x: x[price_idx])
    clusters, current = [], [sorted_s[0]]
    for s in sorted_s[1:]:
        if abs(s[price_idx] - current[-1][price_idx]) <= tolerance:
            current.append(s)
        else:
            clusters.append(current)
            current = [s]
    clusters.append(current)
    return clusters


def _build_levels(swing_highs, swing_lows, current_price, tolerance, min_strength, max_levels):
    levels = []
    for cl in _cluster(swing_highs, tolerance):
        strength = len(cl)
        if strength < min_strength:
            continue
        avg_price = sum(c[1] for c in cl) / strength
        zone_top  = max(c[2] for c in cl)
        zone_bot  = min(c[3] for c in cl)
        zone_bot  = max(zone_bot, avg_price - 30)
        levels.append({
            "price":    round(avg_price, 2),
            "type":     "resistance" if avg_price > current_price else "support",
            "strength": strength,
            "zone_top": round(zone_top, 2),
            "zone_bot": round(zone_bot, 2),
            "kind":     "supply",
        })
    for cl in _cluster(swing_lows, tolerance):
        strength = len(cl)
        if strength < min_strength:
            continue
        avg_price = sum(c[1] for c in cl) / strength
        zone_top  = max(c[2] for c in cl)
        zone_bot  = min(c[3] for c in cl)
        zone_top  = min(zone_top, avg_price + 30)
        levels.append({
            "price":    round(avg_price, 2),
            "type":     "support" if avg_price < current_price else "resistance",
            "strength": strength,
            "zone_top": round(zone_top, 2),
            "zone_bot": round(zone_bot, 2),
            "kind":     "demand",
        })
    levels.sort(key=lambda x: x["strength"], reverse=True)
    return levels[:max_levels]


# ── Institutional RBD/DBR zone detection ────────────────────────────────────

def _detect_institutional_zones(
    highs, lows, closes, opens, n,
    impulse_atr_mult, base_body_mult, base_max_bars, max_zones, current_price,
):
    """
    Rally-Base-Drop  → Supply zone (institutions sold into the base)
    Drop-Base-Rally  → Demand zone (institutions bought the base)

    Freshness: a zone is still valid if price has not closed clearly through it
    since it was formed.
    """
    # ATR (14-bar EMA of True Range)
    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i]  - closes[i - 1]))
    atr = pd.Series(tr).ewm(span=14, adjust=False).mean().values

    body = np.abs(closes - opens)

    def strong_bull(i):
        return (i >= 14 and closes[i] > opens[i]
                and body[i] >= impulse_atr_mult * atr[i]
                and atr[i] > 0)

    def strong_bear(i):
        return (i >= 14 and closes[i] < opens[i]
                and body[i] >= impulse_atr_mult * atr[i]
                and atr[i] > 0)

    def is_base(i):
        return (i >= 14 and atr[i] > 0
                and body[i] < base_body_mult * atr[i])

    supply_zones, demand_zones = [], []

    i = 14
    while i < n - 3 and (len(supply_zones) + len(demand_zones)) < max_zones * 2:

        # ── RBD: strong bull(s) → base(s) → strong bear(s) ─────────────────
        if strong_bull(i):
            j = i + 1
            while j < n - 1 and strong_bull(j):
                j += 1                      # skip extended impulse
            base_bars = []
            while len(base_bars) < base_max_bars and j < n - 1:
                if is_base(j):
                    base_bars.append(j)
                    j += 1
                else:
                    break
            if base_bars and j < n and strong_bear(j):
                zone_top = float(max(max(opens[b], closes[b]) for b in base_bars))
                zone_bot = float(min(min(opens[b], closes[b]) for b in base_bars))
                if zone_top > zone_bot:     # sanity: zone must have some height
                    formed_at = base_bars[-1]
                    # Only keep if price hasn't closed convincingly above zone since drop
                    if _zone_still_valid_supply(zone_top, closes, formed_at + 1):
                        mid = round((zone_top + zone_bot) / 2, 2)
                        supply_zones.append({
                            "top":      round(zone_top, 2),
                            "bottom":   round(zone_bot, 2),
                            "price":    mid,
                            "strength": len(base_bars),
                            "type":     "resistance" if mid > current_price else "support",
                        })
                i = j + 1
                continue

        # ── DBR: strong bear(s) → base(s) → strong bull(s) ─────────────────
        if strong_bear(i):
            j = i + 1
            while j < n - 1 and strong_bear(j):
                j += 1
            base_bars = []
            while len(base_bars) < base_max_bars and j < n - 1:
                if is_base(j):
                    base_bars.append(j)
                    j += 1
                else:
                    break
            if base_bars and j < n and strong_bull(j):
                zone_top = float(max(max(opens[b], closes[b]) for b in base_bars))
                zone_bot = float(min(min(opens[b], closes[b]) for b in base_bars))
                if zone_top > zone_bot:
                    formed_at = base_bars[-1]
                    if _zone_still_valid_demand(zone_bot, closes, formed_at + 1):
                        mid = round((zone_top + zone_bot) / 2, 2)
                        demand_zones.append({
                            "top":      round(zone_top, 2),
                            "bottom":   round(zone_bot, 2),
                            "price":    mid,
                            "strength": len(base_bars),
                            "type":     "support" if mid < current_price else "resistance",
                        })
                i = j + 1
                continue

        i += 1

    # Sort: zones closest to current price first, limit count
    supply_zones.sort(key=lambda z: abs(z["price"] - current_price))
    demand_zones.sort(key=lambda z: abs(z["price"] - current_price))
    return supply_zones[:max_zones], demand_zones[:max_zones]


def _zone_still_valid_supply(zone_top: float, closes, from_idx: int) -> bool:
    """Supply zone is broken if price closed > zone_top + 10 pts after the drop."""
    if from_idx >= len(closes):
        return True
    return not any(c > zone_top + 10 for c in closes[from_idx:])


def _zone_still_valid_demand(zone_bot: float, closes, from_idx: int) -> bool:
    """Demand zone is broken if price closed < zone_bot - 10 pts after the rally."""
    if from_idx >= len(closes):
        return True
    return not any(c < zone_bot - 10 for c in closes[from_idx:])


# ── Market structure ─────────────────────────────────────────────────────────

def _detect_structure(closes) -> str:
    if len(closes) < 20:
        return "ranging"
    recent      = closes[-20:]
    first_half  = recent[:10]
    second_half = recent[10:]
    if second_half.mean() > first_half.mean() * 1.003:
        return "uptrend"
    if second_half.mean() < first_half.mean() * 0.997:
        return "downtrend"
    return "ranging"


def _classify_position(closes, nearest_support, nearest_resistance, prox=25) -> str:
    current = float(closes[-1])
    if nearest_resistance and abs(current - nearest_resistance) <= prox:
        return "at_resistance"
    if nearest_support and abs(current - nearest_support) <= prox:
        return "at_support"
    if nearest_resistance or nearest_support:
        recent = closes[-3:]
        if nearest_resistance and any(c > nearest_resistance for c in recent):
            return "breaking_up"
        if nearest_support and any(c < nearest_support for c in recent):
            return "breaking_down"
    return "open_air"


# ── Cache ────────────────────────────────────────────────────────────────────

def _empty() -> dict:
    return {
        "levels": [], "support": [], "resistance": [],
        "supply_zones": [], "demand_zones": [],
        "structure": "ranging", "position": "open_air",
        "current_price": 0, "nearest_support": None, "nearest_resistance": None,
    }


def get_cached(df=None) -> dict:
    """Return cached S/R levels, recomputing if TTL expired or df provided."""
    global _cache, _cache_ts
    now = time.time()
    if df is not None and (now - _cache_ts > _CACHE_TTL or _cache is None):
        try:
            _cache    = compute_sr_levels(df)
            _cache_ts = now
        except Exception as e:
            logger.error("sr_levels.get_cached: %s", e)
            _cache = _empty()
    return _cache or _empty()
