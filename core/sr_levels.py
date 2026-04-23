"""
S/R Level Detection and Market Structure Analysis.

Algorithm:
  1. Detect swing highs/lows (local max/min over N bars each side)
  2. Cluster levels within ±tolerance pts (merge nearby levels = same zone)
  3. Score by touch count (more touches = stronger level)
  4. Build supply zones (body range at swing high) and demand zones (at swing low)
  5. Classify current price position vs levels (at_support / at_resistance / open_air / breaking_up / breaking_down)
"""

import logging
from typing import Optional
import time

import pandas as pd

logger = logging.getLogger(__name__)

_cache: Optional[dict] = None
_cache_ts: float = 0.0
_CACHE_TTL = 900   # 15 minutes — S/R levels don't change that fast


def compute_sr_levels(df: pd.DataFrame, n_swing: int = 5,
                      tolerance: float = 20.0, min_strength: int = 2,
                      max_levels: int = 10) -> dict:
    """
    Compute S/R levels, supply/demand zones, and current market structure.

    Returns dict with:
      levels         : list of {price, type, strength, zone_top, zone_bot}
      support        : levels below current price (sorted desc)
      resistance     : levels above current price (sorted asc)
      supply_zones   : [{top, bottom, strength}]
      demand_zones   : [{top, bottom, strength}]
      structure      : "uptrend" | "downtrend" | "ranging"
      position       : "at_resistance" | "at_support" | "breaking_up" | "breaking_down" | "open_air"
      current_price  : float
      nearest_support: float | None
      nearest_resistance: float | None
    """
    if df is None or len(df) < n_swing * 2 + 1:
        return _empty()

    df = df.copy()
    for col in ["High", "Low", "Close", "Open"]:
        if col not in df.columns and col.lower() in df.columns:
            df[col] = df[col.lower()]
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["High", "Low", "Close", "Open"], inplace=True)

    highs  = df["High"].values
    lows   = df["Low"].values
    closes = df["Close"].values
    opens  = df["Open"].values
    n      = len(df)

    # ── Swing detection ──────────────────────────────────────────────────────
    swing_highs = []   # (index, price, candle_body_top, candle_body_bot)
    swing_lows  = []

    for i in range(n_swing, n - n_swing):
        window_h = highs[i - n_swing: i + n_swing + 1]
        window_l = lows[i - n_swing:  i + n_swing + 1]

        if highs[i] == max(window_h):
            body_top = max(opens[i], closes[i])
            body_bot = min(opens[i], closes[i])
            swing_highs.append((i, highs[i], body_top, body_bot))

        if lows[i] == min(window_l):
            body_top = max(opens[i], closes[i])
            body_bot = min(opens[i], closes[i])
            swing_lows.append((i, lows[i], body_top, body_bot))

    # ── Cluster nearby levels ─────────────────────────────────────────────────
    def cluster(swings, price_idx=1):
        if not swings:
            return []
        sorted_swings = sorted(swings, key=lambda x: x[price_idx])
        clusters = []
        current = [sorted_swings[0]]
        for s in sorted_swings[1:]:
            if abs(s[price_idx] - current[-1][price_idx]) <= tolerance:
                current.append(s)
            else:
                clusters.append(current)
                current = [s]
        clusters.append(current)
        return clusters

    high_clusters = cluster(swing_highs)
    low_clusters  = cluster(swing_lows)

    current_price = float(closes[-1])

    # ── Build level objects ──────────────────────────────────────────────────
    levels = []

    for cl in high_clusters:
        strength = len(cl)
        if strength < min_strength:
            continue
        avg_price   = sum(c[1] for c in cl) / strength
        zone_top    = max(c[2] for c in cl)   # highest body top
        zone_bot    = min(c[3] for c in cl)   # lowest body bottom
        zone_bot    = max(zone_bot, avg_price - 30)  # cap zone height
        levels.append({
            "price":    round(avg_price, 2),
            "type":     "resistance" if avg_price > current_price else "support",
            "strength": strength,
            "zone_top": round(zone_top, 2),
            "zone_bot": round(zone_bot, 2),
            "kind":     "supply",
        })

    for cl in low_clusters:
        strength = len(cl)
        if strength < min_strength:
            continue
        avg_price   = sum(c[1] for c in cl) / strength
        zone_top    = max(c[2] for c in cl)
        zone_bot    = min(c[3] for c in cl)
        zone_top    = min(zone_top, avg_price + 30)  # cap zone height
        levels.append({
            "price":    round(avg_price, 2),
            "type":     "support" if avg_price < current_price else "resistance",
            "strength": strength,
            "zone_top": round(zone_top, 2),
            "zone_bot": round(zone_bot, 2),
            "kind":     "demand",
        })

    # Sort and limit
    levels.sort(key=lambda x: x["strength"], reverse=True)
    levels = levels[:max_levels]

    support    = sorted([l for l in levels if l["type"] == "support"],
                        key=lambda x: x["price"], reverse=True)
    resistance = sorted([l for l in levels if l["type"] == "resistance"],
                        key=lambda x: x["price"])

    supply_zones = [{"top": l["zone_top"], "bottom": l["zone_bot"], "price": l["price"], "strength": l["strength"]}
                    for l in levels if l["kind"] == "supply" and l["type"] == "resistance"]
    demand_zones = [{"top": l["zone_top"], "bottom": l["zone_bot"], "price": l["price"], "strength": l["strength"]}
                    for l in levels if l["kind"] == "demand" and l["type"] == "support"]

    nearest_support    = support[0]["price"]    if support    else None
    nearest_resistance = resistance[0]["price"] if resistance else None

    # ── Market structure (trend) ─────────────────────────────────────────────
    # Use last 50 bars: compare recent swing highs/lows to older ones
    structure = _detect_structure(closes)

    # ── Current position vs levels ───────────────────────────────────────────
    position = "open_air"
    prox = 25  # within 25 pts = "at" a level

    if nearest_resistance and abs(current_price - nearest_resistance) <= prox:
        position = "at_resistance"
    elif nearest_support and abs(current_price - nearest_support) <= prox:
        position = "at_support"
    elif nearest_resistance and nearest_support:
        # Check if breaking: closed above resistance in last 3 bars
        recent = closes[-3:]
        if nearest_resistance and any(c > nearest_resistance for c in recent):
            position = "breaking_up"
        elif nearest_support and any(c < nearest_support for c in recent):
            position = "breaking_down"

    return {
        "levels":            levels,
        "support":           support,
        "resistance":        resistance,
        "supply_zones":      supply_zones,
        "demand_zones":      demand_zones,
        "structure":         structure,
        "position":          position,
        "current_price":     current_price,
        "nearest_support":   nearest_support,
        "nearest_resistance": nearest_resistance,
    }


def _detect_structure(closes) -> str:
    if len(closes) < 20:
        return "ranging"
    recent = closes[-20:]
    first_half  = recent[:10]
    second_half = recent[10:]
    if second_half.mean() > first_half.mean() * 1.003:
        return "uptrend"
    elif second_half.mean() < first_half.mean() * 0.997:
        return "downtrend"
    return "ranging"


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
            _cache = compute_sr_levels(df)
            _cache_ts = now
        except Exception as e:
            logger.error("sr_levels.get_cached: %s", e)
            _cache = _empty()
    return _cache or _empty()
