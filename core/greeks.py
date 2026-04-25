"""
Black-Scholes option pricing and Greeks.

Used for:
  - Back-calculating Implied Volatility from market prices (bhavcopy)
  - Computing delta, gamma, theta, vega at entry to filter strike quality
  - IV Rank: compare current IV to 30-day historical range

All inputs: spot price in INR, strike in INR, time in years, rate as decimal.
"""

import math
from typing import Optional


def _d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    return (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))


def _d2(S: float, K: float, T: float, r: float, sigma: float) -> float:
    return _d1(S, K, T, r, sigma) - sigma * math.sqrt(T)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x ** 2) / math.sqrt(2 * math.pi)


def bs_price(S: float, K: float, T: float, r: float, sigma: float,
             option_type: str = "CE") -> float:
    """Black-Scholes theoretical price."""
    if T <= 0 or sigma <= 0:
        intrinsic = max(0, S - K) if option_type == "CE" else max(0, K - S)
        return intrinsic
    d1 = _d1(S, K, T, r, sigma)
    d2 = _d2(S, K, T, r, sigma)
    if option_type == "CE":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def implied_vol(market_price: float, S: float, K: float, T: float,
                r: float = 0.065, option_type: str = "CE",
                tol: float = 1e-5, max_iter: int = 100) -> Optional[float]:
    """
    Newton-Raphson IV solver. Returns IV as a decimal (0.15 = 15% annualised).
    Returns None if solution doesn't converge (e.g. deep ITM/OTM with bad price).
    """
    if T <= 0:
        return None
    intrinsic = max(0, S - K) if option_type == "CE" else max(0, K - S)
    if market_price <= intrinsic:
        return None

    sigma = 0.3   # starting guess 30%
    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, option_type)
        diff  = price - market_price
        if abs(diff) < tol:
            return round(sigma, 6)
        d1    = _d1(S, K, T, r, sigma)
        vega  = S * math.sqrt(T) * _norm_pdf(d1)
        if vega < 1e-10:
            return None
        sigma -= diff / vega
        if sigma <= 0 or sigma > 5:
            return None
    return None


def greeks(S: float, K: float, T: float, r: float, sigma: float,
           option_type: str = "CE") -> dict:
    """
    Compute all Greeks.

    Returns:
        delta : directional exposure per unit spot move
        gamma : rate of change of delta (acceleration)
        theta : time decay per calendar day (in rupee terms, approx)
        vega  : sensitivity to 1% change in IV
        iv    : implied vol used
    """
    if T <= 0 or sigma <= 0:
        return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0, "iv": sigma}

    d1 = _d1(S, K, T, r, sigma)
    d2 = _d2(S, K, T, r, sigma)

    gamma = _norm_pdf(d1) / (S * sigma * math.sqrt(T))

    if option_type == "CE":
        delta = _norm_cdf(d1)
        theta = (-(S * _norm_pdf(d1) * sigma / (2 * math.sqrt(T)))
                 - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365
    else:
        delta = _norm_cdf(d1) - 1
        theta = (-(S * _norm_pdf(d1) * sigma / (2 * math.sqrt(T)))
                 + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365

    vega = S * math.sqrt(T) * _norm_pdf(d1) * 0.01   # per 1% IV move

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega":  round(vega, 4),
        "iv":    round(sigma, 4),
    }


def days_to_expiry(expiry_date, today=None) -> float:
    """Calendar days to expiry, as a fraction of a year."""
    from datetime import date
    if today is None:
        today = date.today()
    if isinstance(expiry_date, str):
        expiry_date = date.fromisoformat(expiry_date)
    days = (expiry_date - today).days
    return max(days, 0) / 365.0
