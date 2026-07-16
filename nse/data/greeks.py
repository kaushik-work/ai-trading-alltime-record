"""Black-Scholes Greeks and implied-volatility for European index options.

Angel One SmartAPI does not publish Greeks, so we compute them from the
snapshot fields (spot, strike, expiry, mark) using a standard
continuous-dividend Black-Scholes model.

Typical usage:
    from nse.data.greeks import option_greeks
    g = option_greeks(
        spot=24500, strike=24500, option_type="CE",
        expiry=datetime(...), mark=120, timestamp=datetime(...)
    )
    # g -> {"iv": 0.18, "delta": 0.52, "gamma": 0.0002,
    #       "theta": -45.0, "vega": 32.5, "rho": 12.3}

Defaults:
    risk_free_rate = 6.5% p.a.  (RBI repo proxy)
    dividend_yield = 0.0%        (index total return is close enough for intraday)
    theta is returned as INR per day (negative for long options).
    vega is INR for a 1 percentage-point move in IV.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

import numpy as np
from scipy import stats
from scipy.optimize import brentq

logger = logging.getLogger(__name__)

RISK_FREE_RATE = 0.065
DIVIDEND_YIELD = 0.0
MIN_IV = 0.001
MAX_IV = 2.50  # 250% -- covers almost any sane index option


def _years_to_expiry(
    timestamp: datetime,
    expiry: datetime,
) -> float:
    """Return time to expiry in years using calendar seconds.

    Both inputs may be tz-aware or tz-naive; mixed awareness is handled by
    treating naive timestamps as IST (the collector stores IST strings).
    """
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    timestamp = _as_ist(timestamp, ist)
    expiry = _as_ist(expiry, ist)
    secs = (expiry - timestamp).total_seconds()
    if secs <= 0:
        return 0.0
    return secs / (365.25 * 24 * 3600)


def _as_ist(dt: datetime, ist: ZoneInfo) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ist)
    return dt.astimezone(ist)


def _d1d2(
    spot: float,
    strike: float,
    t: float,
    r: float,
    q: float,
    vol: float,
) -> tuple[float, float]:
    if vol <= 0 or t <= 0:
        raise ValueError("vol and t must be positive")
    srt = vol * np.sqrt(t)
    d1 = (np.log(spot / strike) + (r - q + 0.5 * vol * vol) * t) / srt
    d2 = d1 - srt
    return d1, d2


def black_scholes_price(
    spot: float,
    strike: float,
    t: float,
    option_type: Literal["CE", "PE"],
    r: float = RISK_FREE_RATE,
    q: float = DIVIDEND_YIELD,
    vol: float = 0.20,
) -> float:
    """Return theoretical European option price."""
    if t <= 0:
        if option_type == "CE":
            return max(spot - strike, 0.0)
        return max(strike - spot, 0.0)

    d1, d2 = _d1d2(spot, strike, t, r, q, vol)
    nd1 = stats.norm.cdf(d1)
    nd2 = stats.norm.cdf(d2)
    discount = np.exp(-r * t)
    dividend_disc = np.exp(-q * t)

    if option_type == "CE":
        return spot * dividend_disc * nd1 - strike * discount * nd2
    # put
    return strike * discount * (1 - nd2) - spot * dividend_disc * (1 - nd1)


def implied_volatility(
    spot: float,
    strike: float,
    t: float,
    option_type: Literal["CE", "PE"],
    market_price: float,
    r: float = RISK_FREE_RATE,
    q: float = DIVIDEND_YIELD,
) -> float | None:
    """Solve for implied vol using Brent's method.

    Returns None when the market price lies outside the no-arbitrage bounds
    or the solver fails to converge.
    """
    if t <= 0 or market_price <= 0 or spot <= 0 or strike <= 0:
        return None

    intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
    if market_price < intrinsic:
        return None

    max_price = spot if option_type == "CE" else strike
    if market_price >= max_price:
        return None

    def objective(vol: float) -> float:
        return black_scholes_price(spot, strike, t, option_type, r, q, vol) - market_price

    # Check bracket signs; if price is super cheap, lower bound may already be
    # above market.  In that case IV is effectively MIN_IV.
    try:
        lo, hi = MIN_IV, MAX_IV
        f_lo, f_hi = objective(lo), objective(hi)
        if f_lo * f_hi > 0:
            if abs(f_lo) < abs(f_hi):
                return lo
            return hi
        return brentq(objective, lo, hi, xtol=1e-6, maxiter=100)
    except ValueError as e:
        logger.debug("IV solve failed for %s %.0f@%.0f price=%.2f t=%.4f: %s",
                     option_type, spot, strike, market_price, t, e)
        return None


def option_greeks(
    spot: float,
    strike: float,
    option_type: Literal["CE", "PE"],
    expiry: datetime,
    mark: float,
    timestamp: datetime | None = None,
    r: float = RISK_FREE_RATE,
    q: float = DIVIDEND_YIELD,
) -> dict:
    """Return a dict with iv, delta, gamma, theta, vega, rho for one option.

    Theta is INR per day (negative for a long option).
    Vega  is INR for a +1%-point move in implied vol.
    Rho   is INR for a +1%-point move in the risk-free rate.
    """
    if timestamp is None:
        timestamp = datetime.now()
    t = _years_to_expiry(timestamp, expiry)

    out: dict[str, float | None] = {
        "iv": None,
        "delta": None,
        "gamma": None,
        "theta": None,
        "vega": None,
        "rho": None,
    }

    if t <= 0 or mark <= 0 or spot <= 0 or strike <= 0:
        return out

    iv = implied_volatility(spot, strike, t, option_type, mark, r, q)
    if iv is None:
        return out

    try:
        d1, d2 = _d1d2(spot, strike, t, r, q, iv)
    except ValueError:
        return out

    nd1 = stats.norm.cdf(d1)
    nd2 = stats.norm.cdf(d2)
    npdf = stats.norm.pdf(d1)
    discount = np.exp(-r * t)
    dividend_disc = np.exp(-q * t)
    sqrt_t = np.sqrt(t)

    # Delta
    if option_type == "CE":
        delta = dividend_disc * nd1
    else:
        delta = dividend_disc * (nd1 - 1.0)

    # Gamma (same for call and put)
    gamma = dividend_disc * npdf / (spot * iv * sqrt_t)

    # Theta per year, then convert to per day
    common = -spot * dividend_disc * npdf * iv / (2.0 * sqrt_t)
    if option_type == "CE":
        theta_yr = common - r * strike * discount * nd2 + q * spot * dividend_disc * nd1
    else:
        theta_yr = common + r * strike * discount * (1.0 - nd2) - q * spot * dividend_disc * (1.0 - nd1)
    theta = theta_yr / 365.25

    # Vega per 1% vol point
    vega = spot * dividend_disc * npdf * sqrt_t / 100.0

    # Rho per 1% rate point
    if option_type == "CE":
        rho = strike * t * discount * nd2 / 100.0
    else:
        rho = -strike * t * discount * (1.0 - nd2) / 100.0

    out.update({
        "iv": float(iv),
        "delta": float(delta),
        "gamma": float(gamma),
        "theta": float(theta),
        "vega": float(vega),
        "rho": float(rho),
    })
    return out


def snapshot_greeks(
    df_rows: list[dict],
    timestamp: datetime | None = None,
    r: float = RISK_FREE_RATE,
    q: float = DIVIDEND_YIELD,
) -> list[dict]:
    """Compute Greeks for a list of snapshot rows.

    Each row must contain: spot, strike, option_type (CE/PE), expiry (datetime),
    mark (float).  Returns the rows with iv/delta/gamma/theta/vega/rho added.
    """
    results = []
    for row in df_rows:
        try:
            spot = float(row.get("spot") or row.get("ltp") or 0)
            strike = float(row.get("strike", 0))
            mark = float(row.get("mark") or row.get("ltp") or 0)
            option_type = str(row.get("option_type") or row.get("side", "")).upper()
            if option_type.startswith("C"):
                option_type = "CE"
            elif option_type.startswith("P"):
                option_type = "PE"
            expiry = row.get("expiry")
            if expiry is None:
                continue
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            ts = timestamp or row.get("timestamp") or datetime.now()
            g = option_greeks(spot, strike, option_type, expiry, mark, ts, r, q)
            new_row = dict(row)
            new_row.update(g)
            results.append(new_row)
        except Exception as e:
            logger.debug("snapshot_greeks skip row: %s", e)
            continue
    return results
