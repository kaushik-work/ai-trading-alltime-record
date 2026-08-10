"""Build a MarketSnapshot for a crypto perpetual.

THE SAME DATACLASS THE NSE COUNCIL READS, DELIBERATELY.

A second snapshot type for crypto would need a second council, a second
harness, and a second set of lenses that drift apart from the first. Instead a
perp is a MarketSnapshot with `expiry=None` and an empty `chain`, and that
absence does the work:

    expiry None  -> dte 0.0  -> greeks_trustworthy False -> greeks, smile and
                    gamma_exposure abstain on their own
    chain empty  -> volume_oi and liquidity abstain on their own

So the five option lenses stand down without a single `if venue == "crypto"`
anywhere in the lens code, and the five bar-only lenses — momentum, ict_smc,
vwap, composite_profile, and the structural half of the roster — run unchanged.

WHY THIS IS NOT JUST A PORT OF A FAILING SYSTEM

`vwap` and `composite_profile` both measured negative on NIFTY, and both are
structurally crippled there: an INDEX HAS NO TRADED VOLUME. NIFTY spot prints a
level, not a trade, so a volume-weighted average price and a volume profile are
computed from a volume column that is either zero or synthetic. `vwap` abstains
live with "no usable volume in the session so far".

Crypto perps have real traded volume on every bar. These two lenses get their
first honest test here rather than a repeat of a rigged one — and their NIFTY
results say nothing about how they will do.

That also means their NSE measurements must NOT be carried over as priors.
A crypto brain is a separate document keyed by a separate lens name, so
`vwap` on ETHUSD earns or loses its weight on ETHUSD evidence alone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

#: Bars to keep in the rolling window handed to the lenses. ict_smc needs 40,
#: composite_profile 60; 400 gives every lens a full window plus history for
#: ATR and swing detection without carrying the whole cache into every verdict.
WINDOW_BARS = 400

#: Sessions-equivalent of prior history for composite_profile. Crypto has no
#: sessions, so "7 days" is expressed in bars: 7 * 24 * 12 five-minute bars.
PRIOR_BARS_5M = 7 * 24 * 12


def build_crypto_snapshot(symbol: str = "ETHUSD", interval: str = "5m",
                          bars: Optional[pd.DataFrame] = None,
                          mark: Optional[float] = None) -> Optional[MarketSnapshot]:
    """One perp observation, shaped exactly like an NSE one.

    `bars` may be supplied directly (replay, tests); otherwise the cached OHLC
    fetcher is used, which is the same path the chart tooling already uses.

    Returns None rather than a half-built snapshot when the market is
    unreadable — a lens handed a snapshot with no price would produce a
    confident verdict from nothing, and the council cannot tell the difference.
    """
    if bars is None:
        try:
            from core.chart.ohlc import fetch_delta
            bars = fetch_delta(symbol=symbol, interval=interval)
        except Exception as e:
            logger.error("crypto snapshot: bar fetch failed for %s: %s", symbol, e)
            return None

    if bars is None or getattr(bars, "empty", True):
        logger.warning("crypto snapshot: no bars for %s", symbol)
        return None

    bars = bars.copy()
    bars.columns = [str(c).lower() for c in bars.columns]
    if "close" not in bars.columns:
        logger.warning("crypto snapshot: bars for %s lack a close", symbol)
        return None
    if "datetime" not in bars.columns:
        for cand in ("date", "timestamp", "time"):
            if cand in bars.columns:
                bars = bars.rename(columns={cand: "datetime"})
                break
    if "datetime" in bars.columns:
        bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
        bars = bars.dropna(subset=["datetime"]).sort_values("datetime")
    bars = bars.reset_index(drop=True)

    spot = float(mark) if mark else float(
        pd.to_numeric(bars["close"], errors="coerce").dropna().iloc[-1])
    if spot <= 0:
        logger.warning("crypto snapshot: no usable mark for %s", symbol)
        return None

    # Split into "recent window" and "prior", mirroring the NSE session/prior
    # split so continuous_bars() behaves identically on both venues.
    window = bars.tail(WINDOW_BARS).reset_index(drop=True)
    prior = bars.iloc[:-len(window)].tail(PRIOR_BARS_5M).reset_index(drop=True) \
        if len(bars) > len(window) else pd.DataFrame()

    return MarketSnapshot(
        symbol=symbol,
        ts=datetime.now(timezone.utc),
        spot=spot,
        expiry=None,                 # perpetual — see the module docstring
        atm=0,
        chain=pd.DataFrame(),        # no option chain; option lenses abstain
        bars=window,
        prior_bars=prior,
        source="live",
    )


def crypto_lenses():
    """The lenses that can read a perp: the bar-only half of the roster.

    Built by ASKING each lens rather than hardcoding a list, so a new bar-based
    lens joins crypto automatically and a new option lens does not. The filter
    is simply whether the lens produces anything on a chain-less snapshot.
    """
    from nse.lenses import ROSTER

    out = []
    probe = MarketSnapshot(symbol="PROBE", ts=datetime.now(timezone.utc),
                           spot=100.0, bars=pd.DataFrame(), chain=pd.DataFrame())
    for cls in ROSTER:
        if cls.name == "vision":
            continue
        lens = cls()
        v = lens.safe_evaluate(probe)
        # A lens that abstains for lack of BARS is bar-based and belongs here;
        # one that abstains for lack of a CHAIN or of Greeks does not.
        why = (v.rationale or "").lower()
        if any(t in why for t in ("bar", "atr", "structure", "anchor", "range",
                                  "composite")):
            out.append(lens)
    return out
