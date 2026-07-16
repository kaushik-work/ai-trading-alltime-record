"""Vectorized Black-Scholes Greeks for large snapshot DataFrames.

The scalar `option_greeks` in `nse.data.greeks` is fine for live trading
( handful of candidates per tick ), but backfilling months of 5-min snapshots
needs array math.  This module computes IV + Greeks for tens of thousands of
rows in a few seconds.
"""

from __future__ import annotations

import numpy as np
from scipy import stats

from nse.data.greeks import RISK_FREE_RATE, DIVIDEND_YIELD, MIN_IV, MAX_IV


def _d1d2_array(
    spot: np.ndarray,
    strike: np.ndarray,
    t: np.ndarray,
    vol: np.ndarray,
    r: float = RISK_FREE_RATE,
    q: float = DIVIDEND_YIELD,
) -> tuple[np.ndarray, np.ndarray]:
    srt = vol * np.sqrt(t)
    d1 = (np.log(spot / strike) + (r - q + 0.5 * vol * vol) * t) / srt
    d2 = d1 - srt
    return d1, d2


def black_scholes_price_array(
    spot: np.ndarray,
    strike: np.ndarray,
    t: np.ndarray,
    option_type: np.ndarray,  # 1 = Call, 0 = Put
    vol: np.ndarray,
    r: float = RISK_FREE_RATE,
    q: float = DIVIDEND_YIELD,
) -> np.ndarray:
    """Vectorized BS price.  Returns array of prices, NaN where invalid."""
    # Expired options handled after the main formula.
    d1, d2 = _d1d2_array(spot, strike, t, vol, r, q)
    nd1 = stats.norm.cdf(d1)
    nd2 = stats.norm.cdf(d2)
    discount = np.exp(-r * t)
    dividend_disc = np.exp(-q * t)

    call_price = spot * dividend_disc * nd1 - strike * discount * nd2
    put_price = strike * discount * (1 - nd2) - spot * dividend_disc * (1 - nd1)
    price = np.where(option_type == 1, call_price, put_price)

    # Expired / zero time -> intrinsic
    intrinsic = np.where(option_type == 1, np.maximum(spot - strike, 0.0), np.maximum(strike - spot, 0.0))
    price = np.where(t <= 0, intrinsic, price)
    return price


def implied_volatility_array(
    spot: np.ndarray,
    strike: np.ndarray,
    t: np.ndarray,
    option_type: np.ndarray,
    market_price: np.ndarray,
    r: float = RISK_FREE_RATE,
    q: float = DIVIDEND_YIELD,
    iterations: int = 25,
) -> np.ndarray:
    """Vectorized bisection IV solve.

    Returns array of IVs; rows that violate no-arbitrage bounds or fail to
    converge are returned as NaN.
    """
    spot = np.asarray(spot, dtype=float)
    strike = np.asarray(strike, dtype=float)
    t = np.asarray(t, dtype=float)
    option_type = np.asarray(option_type, dtype=int)
    market_price = np.asarray(market_price, dtype=float)

    valid = (t > 0) & (market_price > 0) & (spot > 0) & (strike > 0)

    intrinsic = np.where(option_type == 1, np.maximum(spot - strike, 0.0), np.maximum(strike - spot, 0.0))
    max_price = np.where(option_type == 1, spot, strike)
    valid &= (market_price > intrinsic) & (market_price < max_price)

    n = spot.shape[0]
    low = np.full(n, MIN_IV)
    high = np.full(n, MAX_IV)
    vol = np.full(n, (MIN_IV + MAX_IV) / 2.0)
    vol = np.where(valid, vol, np.nan)

    for _ in range(iterations):
        if np.all(~valid):
            break
        price_mid = black_scholes_price_array(spot, strike, t, option_type, vol, r, q)
        diff = price_mid - market_price
        too_low = diff < 0
        low = np.where(valid & too_low, vol, low)
        high = np.where(valid & ~too_low, vol, high)
        vol = np.where(valid, (low + high) / 2.0, vol)

    # Mark failed rows as NaN.
    final_price = black_scholes_price_array(spot, strike, t, option_type, vol, r, q)
    converged = valid & (np.abs(final_price - market_price) < 0.01)
    vol = np.where(converged, vol, np.nan)
    return vol


def option_greeks_array(
    spot: np.ndarray,
    strike: np.ndarray,
    t: np.ndarray,
    option_type: np.ndarray,
    market_price: np.ndarray,
    r: float = RISK_FREE_RATE,
    q: float = DIVIDEND_YIELD,
) -> dict:
    """Return dict of arrays: iv, delta, gamma, theta, vega, rho."""
    iv = implied_volatility_array(spot, strike, t, option_type, market_price, r, q)

    # Recompute d1/d2 using solved IV; rows with NaN IV propagate NaN.
    d1, d2 = _d1d2_array(spot, strike, t, iv, r, q)
    nd1 = stats.norm.cdf(d1)
    nd2 = stats.norm.cdf(d2)
    npdf = stats.norm.pdf(d1)
    discount = np.exp(-r * t)
    dividend_disc = np.exp(-q * t)
    sqrt_t = np.sqrt(t)

    is_call = option_type == 1
    delta = np.where(is_call, dividend_disc * nd1, dividend_disc * (nd1 - 1.0))
    gamma = dividend_disc * npdf / (spot * iv * sqrt_t)

    common = -spot * dividend_disc * npdf * iv / (2.0 * sqrt_t)
    theta_call = common - r * strike * discount * nd2 + q * spot * dividend_disc * nd1
    theta_put = common + r * strike * discount * (1.0 - nd2) - q * spot * dividend_disc * (1.0 - nd1)
    theta = np.where(is_call, theta_call, theta_put) / 365.25

    vega = spot * dividend_disc * npdf * sqrt_t / 100.0

    rho_call = strike * t * discount * nd2 / 100.0
    rho_put = -strike * t * discount * (1.0 - nd2) / 100.0
    rho = np.where(is_call, rho_call, rho_put)

    return {
        "iv": iv,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": rho,
    }


def add_greeks_to_dataframe(
    df,
    r: float = RISK_FREE_RATE,
    q: float = DIVIDEND_YIELD,
) -> None:
    """Add iv/delta/gamma/theta/vega/rho columns to a snapshot DataFrame in-place.

    Required columns: spot, strike, side (CE/PE), expiry, mark (or ltp).
    Timestamp column is used for time-to-expiry if present; otherwise now.
    """
    import pandas as pd
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    now = datetime.now(ist)

    spot = pd.to_numeric(df["spot"], errors="coerce").to_numpy()
    strike = pd.to_numeric(df["strike"], errors="coerce").to_numpy()
    mark = pd.to_numeric(df.get("mark", df.get("ltp")), errors="coerce").to_numpy()
    side = df["side"].astype(str).str.upper().to_numpy()
    option_type = np.where(np.array([str(s).startswith("C") for s in side]), 1, 0)

    expiry = pd.to_datetime(df["expiry"])
    ts = pd.to_datetime(df.get("timestamp")) if "timestamp" in df.columns else pd.Series([now] * len(df))
    ts = ts.dt.tz_convert(ist)
    expiry = expiry.dt.tz_convert(ist)
    t = ((expiry - ts).dt.total_seconds() / (365.25 * 24 * 3600)).to_numpy()
    t = np.where(t < 0, 0.0, t)

    g = option_greeks_array(spot, strike, t, option_type, mark, r, q)
    for k, v in g.items():
        df[k] = v
