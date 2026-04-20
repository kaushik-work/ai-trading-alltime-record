"""
vix_filter.py — India VIX regime classifier.

Framework (Price + VIX):
  Price Up   + VIX Up   → STRONG_UP     (institutional conviction, ride the move)
  Price Up   + VIX Down → WEAK_UP       (weak rally, reversal risk, be cautious on BUY)
  Price Down + VIX Up   → STRONG_DOWN   (institutional panic, ride the fall)
  Price Down + VIX Down → WEAK_DOWN     (weak fall, support forming, cautious on SELL)
  Sideways   + VIX Up   → BIG_MOVE_LOADING (wait for breakout direction)
  Sideways   + VIX Down → BORING        (no edge, skip)

Returns regime + score_modifier applied to raw signal scores:
  STRONG_UP:         +1 to BUY signals,  SELL signals blocked
  STRONG_DOWN:       -1 to SELL signals, BUY signals blocked
  WEAK_UP:           BUY threshold raised by +1 (harder to enter)
  WEAK_DOWN:         SELL threshold raised by +1 (harder to enter)
  BIG_MOVE_LOADING:  all entries blocked (no direction yet)
  BORING:            all entries blocked (no edge)
  UNKNOWN:           no filter applied (VIX data unavailable)
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# How much VIX must move (%) to count as "up" or "down"
_VIX_MOVE_THRESHOLD = 2.0   # 2% VIX change = meaningful
# How much NIFTY must move (%) to count as "up" or "down" vs "sideways"
_PRICE_MOVE_THRESHOLD = 0.3  # 0.3% NIFTY move threshold

REGIMES = {
    "STRONG_UP",
    "STRONG_DOWN",
    "WEAK_UP",
    "WEAK_DOWN",
    "BIG_MOVE_LOADING",
    "BORING",
    "UNKNOWN",
}


def classify_regime(
    current_vix: float,
    prev_vix: float,
    current_price: float,
    prev_price: float,
) -> str:
    """
    Classify current market regime from VIX and price change.
    Returns one of the REGIMES strings.
    """
    if current_vix <= 0 or prev_vix <= 0:
        return "UNKNOWN"

    vix_pct   = (current_vix - prev_vix) / prev_vix * 100
    price_pct = (current_price - prev_price) / prev_price * 100

    vix_up   = vix_pct   >  _VIX_MOVE_THRESHOLD
    vix_down = vix_pct   < -_VIX_MOVE_THRESHOLD

    price_up   = price_pct >  _PRICE_MOVE_THRESHOLD
    price_down = price_pct < -_PRICE_MOVE_THRESHOLD
    sideways   = not price_up and not price_down

    if price_up and vix_up:     return "STRONG_UP"
    if price_up and vix_down:   return "WEAK_UP"
    if price_down and vix_up:   return "STRONG_DOWN"
    if price_down and vix_down: return "WEAK_DOWN"
    if sideways and vix_up:     return "BIG_MOVE_LOADING"
    if sideways and vix_down:   return "BORING"
    return "UNKNOWN"


def apply_regime_filter(signal: dict, regime: str) -> dict:
    """
    Modify a signal dict based on VIX regime.
    Returns a new dict with adjusted score, will_trade, and regime metadata.
    signal must have keys: score, direction, threshold.
    """
    score     = signal.get("score", 0)
    direction = signal.get("direction", "HOLD")
    threshold = signal.get("threshold", 6)

    blocked = False
    adjusted_score = score

    if regime == "STRONG_UP":
        if score > 0:
            adjusted_score = min(10, score + 1)   # boost BUY
        elif score < 0:
            adjusted_score = 0                     # block SELL
            blocked = True
    elif regime == "STRONG_DOWN":
        if score < 0:
            adjusted_score = max(-10, score - 1)   # boost SELL
        elif score > 0:
            adjusted_score = 0                     # block BUY
            blocked = True
    elif regime == "WEAK_UP":
        # BUY threshold +1 harder; SELL not affected
        if score > 0:
            threshold = threshold + 1
    elif regime == "WEAK_DOWN":
        # SELL threshold +1 harder; BUY not affected
        if score < 0:
            threshold = threshold + 1
    elif regime in ("BIG_MOVE_LOADING", "BORING"):
        adjusted_score = 0
        blocked = True

    # Recompute direction from adjusted score
    if adjusted_score >= threshold:
        adj_direction = "BUY"
    elif adjusted_score <= -threshold:
        adj_direction = "SELL"
    else:
        adj_direction = "HOLD"

    return {
        **signal,
        "score":      adjusted_score,
        "direction":  adj_direction,
        "action":     adj_direction,
        "threshold":  threshold,
        "will_trade": abs(adjusted_score) >= threshold,
        "vix_regime": regime,
        "vix_blocked": blocked,
    }


def get_live_regime() -> tuple[str, Optional[float]]:
    """
    Fetch live India VIX and yesterday's VIX, classify regime.
    Also needs today's NIFTY open vs current to determine price direction.
    Returns (regime_string, current_vix).
    Fails gracefully → ("UNKNOWN", None).
    """
    try:
        from data.angel_fetcher import AngelFetcher
        af = AngelFetcher.get()

        current_vix = af.fetch_vix()
        if not current_vix:
            return "UNKNOWN", None

        # Previous VIX from historical (last 2 days)
        vix_hist = af.fetch_vix_historical_df(days=5)
        if vix_hist is None or len(vix_hist) < 2:
            return "UNKNOWN", current_vix

        prev_vix = float(vix_hist["vix"].iloc[-2])

        # NIFTY: compare today's open to current
        ltp = af.get_index_ltp("NIFTY")
        if not ltp:
            return "UNKNOWN", current_vix

        nifty_hist = af.fetch_historical_df("NIFTY", "15m", days=2)
        if nifty_hist is None or len(nifty_hist) < 2:
            return "UNKNOWN", current_vix

        prev_close = float(nifty_hist["Close"].iloc[-2])
        regime = classify_regime(current_vix, prev_vix, ltp, prev_close)

        logger.info(
            "VIX regime: %s | VIX %.2f→%.2f | NIFTY %.0f→%.0f",
            regime, prev_vix, current_vix, prev_close, ltp,
        )
        return regime, current_vix

    except Exception as e:
        logger.warning("vix_filter.get_live_regime: %s", e)
        return "UNKNOWN", None
