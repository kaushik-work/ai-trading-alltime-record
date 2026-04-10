"""
Order Flow Analysis — HPS / DHPS Zone Detection + ICT Signals

HPS  (High Point of Support)  : price zones where cumulative session delta
                                 is strongly positive — buyers dominated here,
                                 acts as support on pullbacks.

DHPS (Dynamic HPS)             : same concept but rolling over the last N candles,
                                 so the zones update as price discovers new levels
                                 during the session.

Negative counterparts:
  HRS  (High Point of Resistance) : strong sell zones (high negative delta)
  DHRS (Dynamic HRS)              : rolling sell zones

ICT signals (Strategy C):
  Order Blocks    : last bearish candle before bullish impulse / vice versa
  Liquidity Sweeps: SSL/BSL — wick beyond swing level that closes back inside

All computed from OHLCV only — no tick data required.
Delta fallback for NIFTY index (Volume=0): price-position delta used instead.
"""

import logging
from typing import Optional
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# Price bin width for delta zone grouping (NOT lot size — that's in config.py)
# NIFTY: 25-point bins → groups 5m closes into zones for HPS/DHPS profiling
_TICK_SIZES = {
    "NIFTY": 25,
}
_DEFAULT_TICK = 25


# ── Core delta computation ─────────────────────────────────────────────────────

def candle_delta(df: pd.DataFrame) -> np.ndarray:
    """
    Approximate buy/sell delta for each candle.

    When real Volume is available (futures/equity):
        delta ≈ ((close-low) - (high-close)) / (high-low) × volume
        → +volume when candle closes at high (all buying)
        → -volume when candle closes at low (all selling)

    When Volume is zero (index data — NIFTY/BANKNIFTY spot):
        Fallback to price-position delta: same formula without volume scaling.
        Values are in [-1, +1].  Cumulative sum still shows directional bias.
    """
    highs  = df["High"].astype(float).values
    lows   = df["Low"].astype(float).values
    closes = df["Close"].astype(float).values
    vols   = df["Volume"].astype(float).values
    ranges = highs - lows

    # Raw price-position imbalance (works with or without volume)
    price_pos = np.where(ranges > 0,
                         (closes - lows) - (highs - closes),
                         0.0)

    total_vol = vols.sum()
    if total_vol > 0:
        # Volume-weighted delta (preferred — futures/equity data)
        delta = np.where(ranges > 0,
                         price_pos / ranges * vols,
                         0.0)
    else:
        # Price-position delta (index data with no volume)
        delta = np.where(ranges > 0,
                         price_pos / ranges,
                         0.0)

    return delta


def cumulative_delta(df: pd.DataFrame) -> np.ndarray:
    """Running sum of candle delta — shows if session is buyer- or seller-dominated."""
    return np.cumsum(candle_delta(df))


# ── Zone detection ─────────────────────────────────────────────────────────────

def _build_delta_profile(
    df: pd.DataFrame,
    tick_size: float,
    weights: Optional[np.ndarray] = None,
) -> dict:
    """
    Accumulate weighted delta at each price level (bin).
    Returns {price_level: delta_sum}.
    """
    closes = df["Close"].astype(float).values
    delta  = candle_delta(df)

    if weights is not None:
        delta = delta * weights

    # Bin each close to nearest tick
    bins = np.round(closes / tick_size) * tick_size

    profile: dict = {}
    for price_bin, d in zip(bins, delta):
        profile[price_bin] = profile.get(price_bin, 0.0) + d

    return profile


def find_hps_zones(
    df: pd.DataFrame,
    symbol: str = "NIFTY",
    n_zones: int = 3,
    min_strength_pct: float = 2.0,
) -> dict:
    """
    HPS / HRS zones from the full session data so far.

    Returns:
        {
          "buy_zones":  [(price, strength_pct), ...],   # sorted strongest first
          "sell_zones": [(price, strength_pct), ...],
          "session_delta": float,                        # +ve = buyer session, -ve = seller
          "cum_delta_series": [float, ...],              # cumulative delta per bar
        }
    """
    tick = _TICK_SIZES.get(symbol, _DEFAULT_TICK)
    profile = _build_delta_profile(df, tick)

    # Normalise by total absolute delta (works for both volume and price-position delta)
    total_vol = df["Volume"].astype(float).sum()
    if total_vol > 0:
        normaliser = total_vol
    else:
        delta_arr = candle_delta(df)
        normaliser = float(np.abs(delta_arr).sum())

    if normaliser == 0:
        return {"buy_zones": [], "sell_zones": [], "session_delta": 0.0, "cum_delta_series": []}

    # Convert delta to % of total activity (strength score)
    scored = {price: delta / normaliser * 100 for price, delta in profile.items()}

    buy_zones = sorted(
        [(p, round(s, 2)) for p, s in scored.items() if s >= min_strength_pct],
        key=lambda x: x[1], reverse=True,
    )[:n_zones]

    sell_zones = sorted(
        [(p, round(s, 2)) for p, s in scored.items() if s <= -min_strength_pct],
        key=lambda x: x[1],
    )[:n_zones]

    session_delta = float(candle_delta(df).sum())
    cum_series    = [round(v, 0) for v in cumulative_delta(df).tolist()]

    return {
        "buy_zones":         buy_zones,
        "sell_zones":        sell_zones,
        "session_delta":     round(session_delta, 0),
        "cum_delta_series":  cum_series,
    }


def find_dhps_zones(
    df: pd.DataFrame,
    symbol: str = "NIFTY",
    lookback: int = 12,
    n_zones: int = 3,
    min_strength_pct: float = 1.5,
    decay: float = 0.85,
) -> dict:
    """
    DHPS / DHRS — Dynamic zones from the last `lookback` candles.

    Uses exponential decay so most-recent candles have more weight,
    making zones shift as price action evolves during the session.

    `decay` = weight multiplier per step back from current bar.
    decay=0.85: bar N-1 has 85% weight of bar N, bar N-2 has 72.25%, etc.

    Returns same structure as find_hps_zones, prefixed with "d_".
    """
    if len(df) < 2:
        return {"d_buy_zones": [], "d_sell_zones": [], "d_session_delta": 0.0}

    recent = df.iloc[-lookback:] if len(df) >= lookback else df.copy()
    n      = len(recent)

    # Weights: most recent bar = 1.0, oldest = decay^(n-1)
    weights = np.array([decay ** (n - 1 - i) for i in range(n)])

    tick    = _TICK_SIZES.get(symbol, _DEFAULT_TICK)
    profile = _build_delta_profile(recent, tick, weights=weights)
    total_w = weights.sum()

    # Normalise by weighted total activity (volume if available, else abs delta)
    total_wvol = (recent["Volume"].astype(float).values * weights).sum()
    if total_wvol > 0:
        normaliser_d = total_wvol
    else:
        raw_delta = candle_delta(recent)
        normaliser_d = float(np.abs(raw_delta * weights).sum())

    if normaliser_d == 0:
        return {"d_buy_zones": [], "d_sell_zones": [], "d_session_delta": 0.0}

    scored = {price: delta / normaliser_d * 100 for price, delta in profile.items()}

    d_buy = sorted(
        [(p, round(s, 2)) for p, s in scored.items() if s >= min_strength_pct],
        key=lambda x: x[1], reverse=True,
    )[:n_zones]

    d_sell = sorted(
        [(p, round(s, 2)) for p, s in scored.items() if s <= -min_strength_pct],
        key=lambda x: x[1],
    )[:n_zones]

    d_delta = float((candle_delta(recent) * weights).sum())

    return {
        "d_buy_zones":    d_buy,
        "d_sell_zones":   d_sell,
        "d_session_delta": round(d_delta, 0),
    }


# ── Zone proximity check ───────────────────────────────────────────────────────

def zone_signal(
    current_price: float,
    hps: dict,
    dhps: dict,
    symbol: str = "NIFTY",
    proximity_ticks: int = 2,
) -> dict:
    """
    Check if current price is AT or NEAR a buy/sell zone.

    Returns:
        {
          "at_hps":   bool,   # at a static HPS buy zone
          "at_dhps":  bool,   # at a dynamic DHPS buy zone
          "at_hrs":   bool,   # at a static HRS sell zone
          "at_dhrs":  bool,   # at a dynamic DHRS sell zone
          "nearest_buy_zone":  (price, strength) or None,
          "nearest_sell_zone": (price, strength) or None,
          "score_delta": int, # net signal contribution: +2 buy zone, -2 sell zone, 0 neutral
        }
    """
    tick      = _TICK_SIZES.get(symbol, _DEFAULT_TICK)
    proximity = tick * proximity_ticks

    def _near(zones, price):
        for zone_price, strength in zones:
            if abs(price - zone_price) <= proximity:
                return (zone_price, strength)
        return None

    near_hps  = _near(hps.get("buy_zones", []),   current_price)
    near_hrs  = _near(hps.get("sell_zones", []),  current_price)
    near_dhps = _near(dhps.get("d_buy_zones", []), current_price)
    near_dhrs = _near(dhps.get("d_sell_zones", []), current_price)

    at_buy  = near_hps  is not None or near_dhps is not None
    at_sell = near_hrs  is not None or near_dhrs is not None

    # Net score contribution
    score_delta = 0
    if at_buy and not at_sell:
        score_delta = +2
    elif at_sell and not at_buy:
        score_delta = -2
    # Conflicting zones → 0 (indecision)

    nearest_buy  = near_dhps or near_hps   # prefer dynamic
    nearest_sell = near_dhrs or near_hrs

    return {
        "at_hps":            near_hps  is not None,
        "at_dhps":           near_dhps is not None,
        "at_hrs":            near_hrs  is not None,
        "at_dhrs":           near_dhrs is not None,
        "nearest_buy_zone":  nearest_buy,
        "nearest_sell_zone": nearest_sell,
        "score_delta":       score_delta,
    }


# ── Trendline channel detection (DP Sir's HPS method) ─────────────────────────

def find_trendline_channels(
    df: pd.DataFrame,
    symbol: str = "NIFTY",
    lookback: int = 40,
    n_pivots: int = 3,
    proximity_pct: float = 0.003,
) -> dict:
    """
    DP Sir's HPS methodology: two diagonal trendlines forming a price channel.

    Lower trendline (swing lows connected)  → HPS / support  → buy zone
    Upper trendline (swing highs connected) → HRS / resistance → sell zone

    Pivots are 3-bar swing points: center bar must be strictly higher (or lower)
    than both neighbours.  A linear regression through the last `n_pivots` pivot
    points projects the trendline value at the current bar.

    proximity_pct : fraction of price within which "near zone" fires (default 0.3%)

    Returns:
        tl_support     : projected lower-TL value at current bar (or None)
        tl_resistance  : projected upper-TL value at current bar (or None)
        tl_slope_sup   : slope of lower TL (positive = rising support)
        tl_slope_res   : slope of upper TL (negative = falling resistance)
        tl_buy_signal  : True if price is within proximity of lower TL
        tl_sell_signal : True if price is within proximity of upper TL
        tl_score_delta : +2 near support, -2 near resistance, 0 otherwise
    """
    empty = {
        "tl_support": None, "tl_resistance": None,
        "tl_slope_sup": None, "tl_slope_res": None,
        "tl_buy_signal": False, "tl_sell_signal": False,
        "tl_score_delta": 0,
    }
    if len(df) < 6:
        return empty

    recent = df.iloc[-lookback:] if len(df) >= lookback else df.copy()
    n      = len(recent)
    highs  = recent["High"].astype(float).values
    lows   = recent["Low"].astype(float).values

    # 3-bar swing pivots
    swing_highs: list = []
    swing_lows:  list = []
    for i in range(1, n - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            swing_highs.append((i, highs[i]))
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            swing_lows.append((i, lows[i]))

    def _project(pivots) -> tuple:
        """Fit line through last n_pivots pivots, return (projected_at_end, slope)."""
        pts = pivots[-n_pivots:] if len(pivots) >= n_pivots else pivots
        if len(pts) < 2:
            return None, None
        xs = np.array([p[0] for p in pts], dtype=float)
        ys = np.array([p[1] for p in pts], dtype=float)
        slope, intercept = np.polyfit(xs, ys, 1)
        return round(slope * (n - 1) + intercept, 2), round(slope, 4)

    tl_support,    slope_sup = _project(swing_lows)
    tl_resistance, slope_res = _project(swing_highs)

    current_price = float(df["Close"].iloc[-1])
    prox          = current_price * proximity_pct

    tl_buy  = tl_support    is not None and abs(current_price - tl_support)    <= prox
    tl_sell = tl_resistance is not None and abs(current_price - tl_resistance) <= prox

    tl_score = 0
    if tl_buy and not tl_sell:
        tl_score = +2
    elif tl_sell and not tl_buy:
        tl_score = -2
    # conflicting (price at both TLs simultaneously) → 0

    return {
        "tl_support":    tl_support,
        "tl_resistance": tl_resistance,
        "tl_slope_sup":  slope_sup,
        "tl_slope_res":  slope_res,
        "tl_buy_signal":  tl_buy,
        "tl_sell_signal": tl_sell,
        "tl_score_delta": tl_score,
    }


# ── ICT Signals: Order Blocks + Liquidity Sweeps ─────────────────────────────

def find_ict_signals(
    df: pd.DataFrame,
    liq_lookback: int = 12,
    ob_lookback: int = 20,
    ob_impulse_pct: float = 0.003,
) -> dict:
    """
    ICT (Inner Circle Trader) confluence signals.

    Liquidity Sweep (SSL/BSL):
      SSL: current candle wicks below prior swing low and closes back above it
           → retail sell-stops hunted, institutions buying → +1
      BSL: current candle wicks above prior swing high and closes back below it
           → retail buy-stops hunted, institutions selling → -1

    Order Block (OB):
      Bullish OB: most recent bearish candle followed by a bullish impulse
                  (close of next bar breaks above OB candle's high by ob_impulse_pct).
                  When price retests the OB range → +1
      Bearish OB: most recent bullish candle followed by a bearish impulse → -1

    Returns:
        ict_liq_score  : +1 SSL sweep | -1 BSL sweep | 0
        ict_ob_score   : +1 bullish OB retest | -1 bearish OB retest | 0
        ict_score      : sum of above (range -2 to +2)
        ict_liq_signal : "SSL_SWEEP" | "BSL_SWEEP" | None
        ict_ob_level   : (ob_low, ob_high) of the active OB or None
    """
    empty = {
        "ict_liq_score": 0, "ict_ob_score": 0, "ict_score": 0,
        "ict_liq_signal": None, "ict_ob_level": None,
    }
    if len(df) < max(liq_lookback, ob_lookback) + 4:
        return empty

    curr = df.iloc[-1]
    curr_low   = float(curr["Low"])
    curr_high  = float(curr["High"])
    curr_close = float(curr["Close"])

    # ── Liquidity Sweep ───────────────────────────────────────────────────────
    liq_window  = df.iloc[-liq_lookback - 1: -1]   # exclude current bar
    swing_low   = float(liq_window["Low"].astype(float).min())
    swing_high  = float(liq_window["High"].astype(float).max())

    liq_score  = 0
    liq_signal = None
    if curr_low < swing_low and curr_close > swing_low:
        liq_score  = +1
        liq_signal = "SSL_SWEEP"
    elif curr_high > swing_high and curr_close < swing_high:
        liq_score  = -1
        liq_signal = "BSL_SWEEP"

    # ── Order Blocks ──────────────────────────────────────────────────────────
    ob_window = df.iloc[-ob_lookback - 4: -1]    # exclude current bar
    n         = len(ob_window)
    ob_score  = 0
    ob_level  = None

    # Bullish OB: scan back for last bearish candle before bullish impulse
    for i in range(n - 3, -1, -1):
        bar    = ob_window.iloc[i]
        b_open = float(bar["Open"])
        b_cls  = float(bar["Close"])
        b_high = float(bar["High"])
        b_low  = float(bar["Low"])
        if b_cls >= b_open:
            continue   # not bearish
        future      = ob_window.iloc[i + 1: i + 4]
        if len(future) < 1:
            continue
        future_high = float(future["High"].astype(float).max())
        if (future_high - b_cls) / b_cls >= ob_impulse_pct:
            # Valid bullish OB found — check if current price is retesting it
            if b_low <= curr_close <= b_high:
                ob_score = +1
                ob_level = (round(b_low, 2), round(b_high, 2))
            break

    if ob_score == 0:
        # Bearish OB: scan back for last bullish candle before bearish impulse
        for i in range(n - 3, -1, -1):
            bar    = ob_window.iloc[i]
            b_open = float(bar["Open"])
            b_cls  = float(bar["Close"])
            b_high = float(bar["High"])
            b_low  = float(bar["Low"])
            if b_cls <= b_open:
                continue   # not bullish
            future     = ob_window.iloc[i + 1: i + 4]
            if len(future) < 1:
                continue
            future_low = float(future["Low"].astype(float).min())
            if (b_open - future_low) / b_open >= ob_impulse_pct:
                if b_low <= curr_close <= b_high:
                    ob_score = -1
                    ob_level = (round(b_low, 2), round(b_high, 2))
                break

    ict_score = max(-2, min(2, liq_score + ob_score))

    return {
        "ict_liq_score":  liq_score,
        "ict_ob_score":   ob_score,
        "ict_score":      ict_score,
        "ict_liq_signal": liq_signal,
        "ict_ob_level":   ob_level,
    }


# ── Supply & Demand Zone Detection ────────────────────────────────────────────

def find_sd_zones(
    df: pd.DataFrame,
    symbol: str = "NIFTY",
    lookback: int = 120,
    min_impulse_pct: float = 0.003,
    max_base_candles: int = 5,
    min_impulse_mult: float = 1.5,
) -> list:
    """
    Detect institutional Supply & Demand zones from OHLCV data.

    Patterns (institutional logic: large orders leave unfilled limit orders at base):
      DBR — Drop-Base-Rally  → Demand zone, REVERSAL   (highest quality)
      RBR — Rally-Base-Rally → Demand zone, CONTINUATION
      RBD — Rally-Base-Drop  → Supply zone, REVERSAL   (highest quality)
      DBD — Drop-Base-Drop   → Supply zone, CONTINUATION

    Each zone:
      zone_type : "demand" | "supply"
      pattern   : "DBR" | "RBR" | "RBD" | "DBD"
      quality   : "reversal" | "continuation"
      proximal  : near edge (price touches this first on return)
      distal    : far edge  (breach = zone invalidated)
      strength  : impulse_ratio × quality_multiplier
      fresh     : True if price has not returned to proximal since formation
    """
    if df is None or len(df) < 10:
        return []

    src = df.iloc[-lookback:].reset_index(drop=True) if len(df) >= lookback else df.reset_index(drop=True)
    n = len(src)

    opens  = src["Open"].astype(float).values
    closes = src["Close"].astype(float).values
    highs  = src["High"].astype(float).values
    lows   = src["Low"].astype(float).values
    bodies = np.abs(closes - opens)
    avg_body = float(np.median(bodies[bodies > 0])) if (bodies > 0).any() else 1.0

    def _is_impulse(i):
        body = bodies[i]
        price = closes[i]
        if price == 0 or body < avg_body * min_impulse_mult:
            return False, ""
        if body / price < min_impulse_pct:
            return False, ""
        bull = closes[i] > opens[i]
        rng = highs[i] - lows[i]
        if rng == 0:
            return False, ""
        # Must close in top 40% (bull) or bottom 40% (bear) of candle range
        if bull and (highs[i] - closes[i]) / rng > 0.4:
            return False, ""
        if not bull and (closes[i] - lows[i]) / rng > 0.4:
            return False, ""
        return True, ("bull" if bull else "bear")

    def _is_base(i):
        return bodies[i] < avg_body * 0.6

    zones = []
    i = 2
    while i < n - 3:
        if not _is_base(i):
            i += 1
            continue

        base_start = i
        base_end   = i
        while (base_end + 1 < n - 1
               and _is_base(base_end + 1)
               and (base_end - base_start) < max_base_candles):
            base_end += 1

        if base_end >= n - 1:
            i = base_end + 1
            continue

        base_high = float(src["High"].astype(float).iloc[base_start:base_end + 1].max())
        base_low  = float(src["Low"].astype(float).iloc[base_start:base_end + 1].min())

        # What came BEFORE the base?
        before_imp, before_dir = False, ""
        for b in range(base_start - 1, max(base_start - 3, -1), -1):
            imp, d = _is_impulse(b)
            if imp:
                before_imp, before_dir = True, d
                break

        # What comes AFTER the base?
        after_imp, after_dir = False, ""
        for a in range(base_end + 1, min(base_end + 3, n)):
            imp, d = _is_impulse(a)
            if imp:
                after_imp, after_dir = True, d
                break

        if not before_imp or not after_imp:
            i = base_end + 1
            continue

        pattern    = f"{'R' if before_dir == 'bull' else 'D'}B{'R' if after_dir == 'bull' else 'D'}"
        is_demand  = after_dir == "bull"
        is_reversal = before_dir != after_dir

        # Proximal = edge price touches FIRST when returning to zone
        proximal = round(base_high, 2) if is_demand else round(base_low, 2)
        distal   = round(base_low,  2) if is_demand else round(base_high, 2)

        after_body    = bodies[base_end + 1] if base_end + 1 < n else avg_body
        impulse_ratio = float(after_body / avg_body) if avg_body > 0 else 1.0
        strength      = round(impulse_ratio * (1.5 if is_reversal else 1.0), 2)

        # Fresh = price has NOT revisited proximal since zone formed
        future = closes[base_end + 2:]
        fresh  = not any(c <= proximal for c in future) if is_demand else not any(c >= proximal for c in future)

        zones.append({
            "zone_type": "demand" if is_demand else "supply",
            "pattern":   pattern,
            "quality":   "reversal" if is_reversal else "continuation",
            "proximal":  proximal,
            "distal":    distal,
            "strength":  strength,
            "fresh":     fresh,
        })
        i = base_end + 2

    # Strongest fresh zones first
    zones.sort(key=lambda z: (z["fresh"], z["strength"]), reverse=True)
    return zones[:8]


def sd_zone_signal(
    current_price: float,
    zones: list,
    proximity_pts: float = 50.0,
) -> dict:
    """
    Check if current price is inside or approaching a S&D zone.

    Returns:
      sd_score     : +2 inside demand, -2 inside supply; +1/-1 approaching; fresh adds ±1
      sd_zone_type : "demand" | "supply" | None
      sd_pattern   : e.g. "DBR"
      sd_fresh     : bool
      sd_proximal  : float or None
      sd_strength  : float or None
    """
    best_score, best_zone = 0, None

    for z in zones:
        lo = min(z["proximal"], z["distal"])
        hi = max(z["proximal"], z["distal"])
        inside      = lo <= current_price <= hi
        approaching = (not inside) and abs(current_price - z["proximal"]) <= proximity_pts

        if inside:
            raw = 2 if z["zone_type"] == "demand" else -2
        elif approaching:
            raw = 1 if z["zone_type"] == "demand" else -1
        else:
            continue

        if z["fresh"]:
            raw = max(-3, min(3, raw + (1 if raw > 0 else -1)))

        if abs(raw) > abs(best_score):
            best_score, best_zone = raw, z

    if best_zone:
        return {
            "sd_score":     best_score,
            "sd_zone_type": best_zone["zone_type"],
            "sd_pattern":   best_zone["pattern"],
            "sd_fresh":     best_zone["fresh"],
            "sd_proximal":  best_zone["proximal"],
            "sd_distal":    best_zone["distal"],
            "sd_strength":  best_zone["strength"],
        }
    return {"sd_score": 0, "sd_zone_type": None, "sd_pattern": None,
            "sd_fresh": False, "sd_proximal": None, "sd_distal": None, "sd_strength": None}


def _resample_ohlcv(
    df: pd.DataFrame,
    timeframe: str,
) -> pd.DataFrame:
    """Resample OHLCV data to a higher timeframe for HTF S&D detection."""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    src = df.copy()
    if "Date" in src.columns:
        src = src.set_index("Date")
    src.index = pd.to_datetime(src.index)
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col not in src.columns:
            src[col] = 0.0

    out = src[["Open", "High", "Low", "Close", "Volume"]].resample(
        timeframe, label="right", closed="right"
    ).agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna(subset=["Close"])
    return out


def find_htf_sd_zones(
    df: pd.DataFrame,
    symbol: str = "NIFTY",
    timeframes: tuple[str, ...] = ("60min", "1D"),
    lookbacks: Optional[dict] = None,
) -> dict:
    """
    Detect supply/demand zones on higher timeframes.

    Returns:
      {
        "60min": [...zones...],
        "1D":    [...zones...],
      }
    """
    lookbacks = lookbacks or {"60min": 120, "1D": 60}
    zones_by_tf: dict = {}
    for timeframe in timeframes:
        htf_df = _resample_ohlcv(df, timeframe)
        lb = int(lookbacks.get(timeframe, 120))
        zones_by_tf[timeframe] = find_sd_zones(htf_df, symbol=symbol, lookback=lb)
    return zones_by_tf


def htf_sd_zone_signal(
    current_price: float,
    zones_by_tf: dict,
    symbol: str = "NIFTY",
    proximity_map: Optional[dict] = None,
) -> dict:
    """
    Select the best higher-timeframe S&D zone touching or approaching current price.

    Priority:
      1. Stronger absolute score
      2. Daily zones over hourly zones on ties
      3. Fresh zones
      4. Stronger departure strength
    """
    tick = _TICK_SIZES.get(symbol, _DEFAULT_TICK)
    proximity_map = proximity_map or {"60min": tick * 4, "1D": tick * 8}

    best = None
    best_rank = (-1, -1, -1, -1.0)

    for timeframe, zones in zones_by_tf.items():
        sig = sd_zone_signal(current_price, zones, proximity_pts=proximity_map.get(timeframe, tick * 4))
        raw = sig.get("sd_score", 0)
        if raw == 0:
            continue
        rank = (
            abs(raw),
            1 if timeframe in {"1D", "D", "daily"} else 0,
            1 if sig.get("sd_fresh") else 0,
            float(sig.get("sd_strength") or 0),
        )
        if rank > best_rank:
            best_rank = rank
            best = {**sig, "sd_tf": timeframe}

    if best:
        return best
    return {
        "sd_score": 0,
        "sd_zone_type": None,
        "sd_pattern": None,
        "sd_fresh": False,
        "sd_proximal": None,
        "sd_distal": None,
        "sd_strength": None,
        "sd_tf": None,
    }


# ── All-in-one helper ──────────────────────────────────────────────────────────

def analyse(
    df: pd.DataFrame,
    current_price: float,
    symbol: str = "NIFTY",
    proximity_ticks: int = 4,
) -> dict:
    """
    Full order flow analysis in one call.

    Returns merged dict of hps + dhps + zone_signal + trendline + ict + sd_zones.
    """
    hps  = find_hps_zones(df, symbol)
    dhps = find_dhps_zones(df, symbol)
    sig  = zone_signal(current_price, hps, dhps, symbol, proximity_ticks=proximity_ticks)
    tl   = find_trendline_channels(df, symbol=symbol)
    ict  = find_ict_signals(df)

    tick = _TICK_SIZES.get(symbol, _DEFAULT_TICK)
    zones = find_sd_zones(df, symbol)
    sd   = sd_zone_signal(current_price, zones, proximity_pts=tick * 2)

    return {**hps, **dhps, **sig, **tl, **ict, **sd, "sd_zones": zones}
