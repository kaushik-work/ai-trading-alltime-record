"""The single market observation every lens reads.

One snapshot per tick, built once and fanned out to every lens. This is the
reason the lenses do not each fetch their own data: four lenses pulling the
identical option chain is four times the Angel API load for zero extra
information, and the rate limits are real (see docs/ANGEL_ONE_API_NOTES.md).

The same dataclass is produced by BOTH paths:

    live      build_live()          Angel SmartAPI
    replay    nse/backtest/         Mongo / the 1m NIFTY dataset

That is deliberate and load-bearing. A lens takes a MarketSnapshot and returns
a verdict; it cannot tell which path built it, which is what makes every lens
replayable against history. A lens that reaches around this object to fetch its
own data is not backtestable and must not ship.

Time-to-expiry is computed against the MEASURED exchange close, not a guess:
NFO moved to 15:40 on 2026-08-03 while BFO still closes 15:30, and the NIFTY
expiry weekday changed mid-dataset (Thursday until 2025-08-28, Tuesday from
2025-09-02). Both are handled by nse.config.market_close_for and the measured
calendar in nse/quant/expiry_calendar.py. See RESEARCH_LEARNINGS 1.8.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from nse.config import EXCHANGE, STEP_SIZES, market_close_for

logger = logging.getLogger(__name__)

# Calendar days per year. Implied vol prices the whole calendar, not just the
# session: the overnight gap is 42.3% of total variance, so discounting it by
# using trading days would understate T and therefore every Greek.
# See docs/OPTIONS_GREEKS_LEARNINGS.md section 6.
DAYS_PER_YEAR = 365.0

# Risk-free rate used for repricing. A dial, not a magic number.
RISK_FREE_RATE = 0.065

# Below this many days to expiry, stored/analytic Greeks stop being usable:
# gamma explodes, delta flips violently around the strike, and Black-Scholes at
# T->0 produces hedge ratios that lost 3-5x the credit on measured sessions.
# Lenses must check `snap.greeks_trustworthy` before acting on a Greek.
# See docs/OPTIONS_GREEKS_LEARNINGS.md section 3 and RESEARCH_LEARNINGS open item 10.
MIN_TRUSTWORTHY_DTE = 2.0


@dataclass(frozen=True)
class MarketSnapshot:
    """An immutable observation of one underlying at one instant.

    `chain` carries one row per contract with the collector's field set —
    ltp/open/high/low/close, volume, oi, bid, ask, mid, spread, spread_pct,
    depth_buy, depth_sell, bid_qty, ask_qty, book_imbalance, exch_feed_time.
    Fields a lens needs but that may be absent on older history (iv, delta,
    gamma, theta, vega) are computed by the replay loader, never assumed.

    `bars` carries recent index OHLCV for the structural lenses (VWAP, volume
    profile, ICT/SMC).

    CAUTION for replay: in the archived 1m dataset `spot` is a CLOSE, not OHLC.
    Resampled highs/lows are extremes of closes, so wick-touch rules under-fire
    versus live. Any lens keying on wicks must declare that limitation.
    See RESEARCH_LEARNINGS section 4.
    """

    symbol: str
    ts: datetime                      # UTC, when we observed
    spot: float
    expiry: datetime                  # UTC, at that contract's exchange close
    atm: int
    chain: pd.DataFrame
    bars: pd.DataFrame = field(default_factory=pd.DataFrame)
    vix: Optional[float] = None
    futures_basis: Optional[float] = None
    source: str = "live"              # "live" | "replay"

    # ── derived ──────────────────────────────────────────────────────────────
    @property
    def dte(self) -> float:
        """Days to expiry as a float, against the real exchange close."""
        return max(0.0, (self.expiry - self.ts).total_seconds() / 86400.0)

    @property
    def T(self) -> float:
        """Year fraction to expiry, for Black-Scholes."""
        return max(1e-6, self.dte / DAYS_PER_YEAR)

    @property
    def greeks_trustworthy(self) -> bool:
        """False inside the zone where analytic Greeks stop meaning anything."""
        return self.dte >= MIN_TRUSTWORTHY_DTE

    @property
    def is_expiry_day(self) -> bool:
        return self.dte < 1.0

    @property
    def step(self) -> int:
        return STEP_SIZES.get(self.symbol, 50)

    @property
    def exchange(self) -> str:
        return EXCHANGE.get(self.symbol, "NFO")

    def side(self, option_type: str) -> pd.DataFrame:
        """All rows for one side of the chain, sorted by strike."""
        col = "option_type" if "option_type" in self.chain.columns else "side"
        if self.chain.empty or col not in self.chain.columns:
            return pd.DataFrame()
        out = self.chain[self.chain[col].astype(str).str.upper() == option_type.upper()]
        return out.sort_values("strike") if "strike" in out.columns else out

    def ce(self) -> pd.DataFrame:
        return self.side("CE")

    def pe(self) -> pd.DataFrame:
        return self.side("PE")

    def at(self, strike: int, option_type: str) -> Optional[pd.Series]:
        """One contract, or None when that strike is not quoted.

        Returning None rather than raising matters: the recorded ladder
        re-centres intraday, so a wing strike quoted at 09:30 can be gone by
        15:20 on a big move. Silently dropping those rows is exactly the
        survivorship bug that manufactured a +19.67pt edge from nothing
        (RESEARCH_LEARNINGS section 3.4 / commit da08fff) — callers must handle
        the None and count the miss, not skip the session.
        """
        rows = self.side(option_type)
        if rows.empty or "strike" not in rows.columns:
            return None
        hit = rows[rows["strike"] == strike]
        return None if hit.empty else hit.iloc[0]

    def atm_straddle_premium(self) -> Optional[float]:
        ce, pe = self.at(self.atm, "CE"), self.at(self.atm, "PE")
        if ce is None or pe is None:
            return None
        c, p = _mark(ce), _mark(pe)
        return None if c is None or p is None else c + p

    def strikes(self) -> list[int]:
        if self.chain.empty or "strike" not in self.chain.columns:
            return []
        return sorted({int(s) for s in self.chain["strike"].dropna().unique()})

    def is_stale(self, max_age_sec: float = 30.0) -> bool:
        """Has the exchange gone quiet on us?

        Uses the exchange feed clock, not our own request time: our timestamp
        says when we ASKED, the feed clock says when the exchange last spoke.
        The gap is how stale the quote actually is, measured rather than
        proxied by volume > 0.
        """
        if self.chain.empty or "exch_feed_time" not in self.chain.columns:
            return False
        feed = pd.to_datetime(self.chain["exch_feed_time"], errors="coerce", utc=True)
        newest = feed.max()
        if pd.isna(newest):
            return False
        return (self.ts - newest.to_pydatetime()).total_seconds() > max_age_sec

    def describe(self) -> str:
        return (f"{self.symbol} spot={self.spot:.2f} atm={self.atm} "
                f"dte={self.dte:.2f} vix={self.vix} n={len(self.chain)} "
                f"src={self.source}")


def _mark(row: pd.Series) -> Optional[float]:
    """Best available price for a contract: mid if the book is real, else LTP.

    Mid is preferred because it is what a marketable order actually pays into,
    but the book must be genuine — Mongo bid/ask were all ZERO until the
    collector fix on 2026-08-04, so any historical row falls through to LTP
    rather than reporting a mid of zero. See RESEARCH_LEARNINGS section 4.
    """
    for key in ("mid", "ltp", "mark", "close"):
        if key in row.index:
            v = row.get(key)
            if v is not None and pd.notna(v) and float(v) > 0:
                return float(v)
    return None


def expiry_at_close(expiry_date, symbol: str) -> datetime:
    """Turn an expiry DATE into the UTC instant that contract actually dies.

    A date alone is not an expiry: NFO settles 15:40 from 2026-08-03 and 15:30
    before it, BFO still settles 15:30. Getting this wrong shifts T, and T sets
    every Greek.
    """
    if isinstance(expiry_date, datetime):
        d = expiry_date.date()
    else:
        d = expiry_date
    close = market_close_for(symbol, d)
    naive = datetime.combine(d, close)
    ist = pd.Timestamp(naive, tz="Asia/Kolkata")
    return ist.tz_convert("UTC").to_pydatetime()


#: Intraday bars, cached per (symbol, interval). Angel's historical-candle
#: endpoint is rate limited hard and `build_live` runs every cycle, so fetching
#: bars on every snapshot earns "Access denied because of exceeding access rate"
#: and returns nothing. That silently blinded THREE lenses — vwap, ict_smc and
#: momentum all abstained with "0 bars" on the first live run while the option
#: chain looked perfectly healthy.
#:
#: A 5-minute bar cannot change more than once every 5 minutes, so re-fetching
#: faster than the TTL below buys no information and costs the whole quota.
_BARS_TTL_SEC = 60.0
_bars_cache: dict = {}


def _cached_bars(fetcher, symbol: str, interval: str):
    """Intraday bars with a TTL, serving the last good frame on failure.

    Returning the previous frame rather than an empty one matters: a transient
    rate-limit reply would otherwise turn every bar-reading lens into an
    abstention for that cycle, which reads on the dashboard as "the lenses have
    nothing to say" when the truth is "we asked too fast".
    """
    import time as _time

    key = (symbol, interval)
    now = _time.monotonic()
    cached = _bars_cache.get(key)
    if cached and now - cached[0] < _BARS_TTL_SEC:
        return cached[1]

    try:
        fresh = fetcher.fetch_intraday_df(symbol, interval)
    except Exception as e:
        logger.debug("build_live: bars fetch failed for %s: %s", symbol, e)
        fresh = None

    if fresh is not None and not getattr(fresh, "empty", True):
        _bars_cache[key] = (now, fresh)
        return fresh

    if cached:
        logger.debug("build_live: serving cached bars for %s (fetch returned nothing)",
                     symbol)
        return cached[1]
    return pd.DataFrame()


def build_live(symbol: str, fetcher=None, strikes_around: int = 8,
               bars_interval: str = "5m") -> Optional[MarketSnapshot]:
    """Build a snapshot from Angel SmartAPI. Returns None if the market is unreadable.

    Returning None rather than a half-populated snapshot is deliberate: a lens
    handed a chain with a missing spot would produce a confident verdict from
    garbage, and the aggregator has no way to tell the difference.
    """
    from nse.data.option_chain import OptionChainCache

    try:
        cache = OptionChainCache(symbol, fetcher)
        spot = cache.get_underlying_ltp()
        if spot is None or spot <= 0:
            logger.warning("build_live: no spot for %s", symbol)
            return None

        expiry_d = cache.nearest_expiry(min_days=0)
        if expiry_d is None:
            logger.warning("build_live: no expiry for %s", symbol)
            return None

        step = STEP_SIZES[symbol]
        atm = int(round(spot / step)) * step
        chain = cache.get_snapshot(expiry_d, atm, strikes_around=strikes_around, full=True)
        if chain is None or chain.empty:
            logger.warning("build_live: empty chain for %s", symbol)
            return None

        bars = _cached_bars(cache.fetcher, symbol, bars_interval)

        vix = None
        try:
            vix = cache.fetcher.fetch_vix()
        except Exception as e:
            logger.debug("build_live: vix unavailable: %s", e)

        return MarketSnapshot(
            symbol=symbol,
            ts=datetime.now(timezone.utc),
            spot=float(spot),
            expiry=expiry_at_close(expiry_d, symbol),
            atm=atm,
            chain=chain,
            bars=bars if bars is not None else pd.DataFrame(),
            vix=vix,
            source="live",
        )
    except Exception as e:
        logger.exception("build_live failed for %s: %s", symbol, e)
        return None
