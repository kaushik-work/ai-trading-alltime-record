"""Black-Scholes pricing, Greeks and implied vol — vectorised.

WHAT THIS IS FOR
    Everything downstream needs a theoretical price and a set of sensitivities:
    the variance-premium work needs IV, any hedged structure needs delta, and
    position sizing on a short-vol book needs vega and gamma. This is that
    layer. It is deliberately dependency-light (numpy + scipy.norm only).

WHEN BLACK-SCHOLES IS APPROPRIATE
    - European exercise. NIFTY/BANKNIFTY index options are European, so the
      model is structurally correct here. Indian STOCK options are American,
      and BS will misprice them (early exercise on dividends).
    - Comparing prices ACROSS strikes and expiries on a common scale. This is
      its real job: it is a quoting convention, a way to turn a rupee premium
      into a vol number you can compare.
    - Computing hedge ratios. Delta from BS is close enough for practical
      hedging at index level.

WHEN IT IS WRONG, AND HOW IT FAILS
    - It assumes ONE volatility across all strikes. Reality has a smile/skew:
      NIFTY puts trade at materially higher IV than calls. So a single "the IV"
      does not exist - always quote IV per strike.
    - It assumes lognormal returns and continuous hedging. Index returns are
      fat-tailed and gap overnight; both make far-OTM options worth MORE than
      BS says. This is exactly why a naive short-vol book blows up: the model
      says the wing is nearly worthless, the market disagrees, and the market
      is right.
    - Near expiry, T -> 0 makes gamma and theta explode. On expiry day the
      numbers are numerically unstable and economically meaningless; the
      pricer clamps T rather than returning infinities.
    - Vega -> 0 for deep ITM/OTM, so Newton-Raphson on implied vol diverges
      there. implied_vol() falls back to bisection instead of returning noise.

    The honest summary: BS is a translation device between price and vol, not
    a statement about what an option is worth. Use it to normalise, never to
    conclude that something is "cheap" or "rich" on its own.

Usage:
    from nse.quant.black_scholes import price, greeks, implied_vol
    c = price(S=24000, K=24000, T=5/365, r=0.065, sigma=0.12, kind="C")
    g = greeks(S=24000, K=24000, T=5/365, r=0.065, sigma=0.12, kind="C")
    iv = implied_vol(market=180.0, S=24000, K=24000, T=5/365, r=0.065, kind="C")
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
from scipy.stats import norm

# Below this, time value is numerically meaningless — clamp rather than
# return inf/nan on expiry day.
MIN_T = 1e-6
MIN_SIGMA = 1e-6


def _d1_d2(S, K, T, r, sigma, q=0.0):
    S, K, T, sigma = map(np.asarray, (S, K, T, sigma))
    T = np.maximum(T, MIN_T)
    sigma = np.maximum(sigma, MIN_SIGMA)
    vs = sigma * np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / vs
    return d1, d1 - vs


def price(S, K, T, r, sigma, kind: str = "C", q: float = 0.0):
    """European option price. `kind` is C/CE/CALL or P/PE/PUT."""
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    T = np.maximum(np.asarray(T), MIN_T)
    disc, fwd = np.exp(-r * T), np.exp(-q * T)
    if str(kind).upper().startswith("C"):
        return S * fwd * norm.cdf(d1) - K * disc * norm.cdf(d2)
    return K * disc * norm.cdf(-d2) - S * fwd * norm.cdf(-d1)


@dataclass
class Greeks:
    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float

    def as_dict(self) -> dict:
        return asdict(self)


def greeks(S, K, T, r, sigma, kind: str = "C", q: float = 0.0) -> Greeks:
    """Analytical Greeks. vega per 1 vol POINT, theta per calendar DAY.

    Those two scalings are the market convention and differ from the raw
    partial derivatives (which are per unit sigma and per year). Mixing them
    up is the single most common sizing error on a vol book.
    """
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    T = np.maximum(np.asarray(T), MIN_T)
    sigma = np.maximum(np.asarray(sigma), MIN_SIGMA)
    sqrtT = np.sqrt(T)
    disc, fwd = np.exp(-r * T), np.exp(-q * T)
    pdf = norm.pdf(d1)
    is_call = str(kind).upper().startswith("C")

    delta = fwd * norm.cdf(d1) if is_call else fwd * (norm.cdf(d1) - 1)
    gamma = fwd * pdf / (S * sigma * sqrtT)
    vega = S * fwd * pdf * sqrtT / 100.0                    # per 1 vol point
    common = -(S * fwd * pdf * sigma) / (2 * sqrtT)
    if is_call:
        theta = (common - r * K * disc * norm.cdf(d2)
                 + q * S * fwd * norm.cdf(d1)) / 365.0
        rho = K * T * disc * norm.cdf(d2) / 100.0
    else:
        theta = (common + r * K * disc * norm.cdf(-d2)
                 - q * S * fwd * norm.cdf(-d1)) / 365.0
        rho = -K * T * disc * norm.cdf(-d2) / 100.0
    return Greeks(float(delta), float(gamma), float(vega), float(theta), float(rho))


def implied_vol(market: float, S: float, K: float, T: float, r: float,
                kind: str = "C", q: float = 0.0,
                tol: float = 1e-6, max_iter: int = 100) -> float:
    """Implied vol by Newton-Raphson with a bisection fallback.

    Newton uses vega as the gradient and converges in a handful of steps for
    near-the-money options. It DIVERGES where vega -> 0 (deep ITM/OTM, or
    almost no time left), so we detect that and bisect instead of returning a
    confident wrong number.
    """
    if not np.isfinite(market) or market <= 0 or T <= 0:
        return float("nan")

    # No-arbitrage bounds: outside these, no sigma can reproduce the price.
    disc, fwd = np.exp(-r * T), np.exp(-q * T)
    intrinsic = (max(S * fwd - K * disc, 0.0) if str(kind).upper().startswith("C")
                 else max(K * disc - S * fwd, 0.0))
    upper_bound = S * fwd if str(kind).upper().startswith("C") else K * disc
    if market < intrinsic - 1e-9 or market > upper_bound + 1e-9:
        return float("nan")

    sigma = 0.25
    for _ in range(max_iter):
        diff = float(price(S, K, T, r, sigma, kind, q)) - market
        if abs(diff) < tol:
            return float(sigma)
        v = greeks(S, K, T, r, sigma, kind, q).vega * 100.0   # back to per-unit
        if v < 1e-8:
            break                                            # vega dead — bisect
        sigma -= diff / v
        if sigma <= 0 or sigma > 10 or not np.isfinite(sigma):
            break

    lo, hi = 1e-6, 10.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        diff = float(price(S, K, T, r, mid, kind, q)) - market
        if abs(diff) < tol:
            return float(mid)
        if diff > 0:
            hi = mid
        else:
            lo = mid
    return float(0.5 * (lo + hi))


def put_call_parity_put(call: float, S: float, K: float, T: float,
                        r: float, q: float = 0.0) -> float:
    """P = C - S*e^-qT + K*e^-rT. One line of algebra, no second model."""
    return call - S * np.exp(-q * T) + K * np.exp(-r * T)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    S, K, r = 24000.0, 24000.0, 0.065
    print("=" * 84)
    print("BLACK-SCHOLES SELF-CHECK — NIFTY ATM, r=6.5%")
    print("=" * 84)

    print(f"\n{'DTE':>5}{'sigma':>8}{'call':>10}{'put':>10}{'parity put':>12}"
          f"{'delta':>8}{'gamma':>10}{'vega':>8}{'theta/day':>11}")
    for dte in (30, 7, 2, 1):
        T = dte / 365
        for sg in (0.12, 0.20):
            c = float(price(S, K, T, r, sg, "C"))
            p = float(price(S, K, T, r, sg, "P"))
            pp = put_call_parity_put(c, S, K, T, r)
            g = greeks(S, K, T, r, sg, "C")
            print(f"{dte:>5}{sg:>8.2f}{c:>10.2f}{p:>10.2f}{pp:>12.2f}"
                  f"{g.delta:>8.3f}{g.gamma:>10.6f}{g.vega:>8.2f}{g.theta:>11.2f}")

    print("\n  parity check: 'put' and 'parity put' must agree to the cent.")

    print("\nImplied-vol round trip (price -> IV -> price):")
    print(f"  {'moneyness':>12}{'true sigma':>12}{'recovered':>12}{'error':>12}")
    for mny, lab in ((1.00, "ATM"), (1.02, "2% OTM"), (1.10, "10% OTM"), (0.90, "10% ITM")):
        Kx, T, true = S * mny, 7 / 365, 0.145
        mkt = float(price(S, Kx, T, r, true, "C"))
        got = implied_vol(mkt, S, Kx, T, r, "C")
        print(f"  {lab:>12}{true:>12.4f}{got:>12.4f}{abs(got - true):>12.2e}")

    print("\nWhere the model stops being safe:")
    g_far = greeks(S, S * 1.10, 1 / 365, r, 0.12, "C")
    print(f"  10% OTM with 1 day left: vega {g_far.vega:.6f} -> Newton has no")
    print("  gradient to work with, so implied_vol falls back to bisection.")
    g_exp = greeks(S, S, 0.5 / 365, r, 0.12, "C")
    print(f"  ATM with half a day left: gamma {g_exp.gamma:.6f}, "
          f"theta {g_exp.theta:.2f}/day")
    print("  Both explode as T->0. On expiry day treat these as unusable.")
