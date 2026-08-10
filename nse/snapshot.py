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

    #: The PREVIOUS session's bars, for lenses that read continuous structure.
    #: Kept separate from `bars` rather than concatenated into it, because the
    #: two are not interchangeable and the difference is not cosmetic:
    #:
    #:   vwap      is SESSION-ANCHORED by definition. Feeding it yesterday's
    #:             prints would compute a two-day VWAP and call it a session
    #:             anchor — a different indicator wearing the same name.
    #:   momentum  a 20-bar range does not reset at 09:15. Yesterday's high is
    #:             the level traders are watching at the open.
    #:   ict_smc   order blocks and fair-value gaps are MORE valid the older
    #:             they are, up to a point. Discarding them nightly discards
    #:             the structure the lens exists to read.
    #:
    #: So the lens decides, via `continuous_bars`. Concatenating centrally would
    #: silently redefine VWAP; leaving it out entirely leaves three lenses blind
    #: until midday. See `continuous_bars` for the warm-up arithmetic.
    prior_bars: pd.DataFrame = field(default_factory=pd.DataFrame)

    vix: Optional[float] = None
    futures_basis: Optional[float] = None
    source: str = "live"              # "live" | "replay"

    # ── derived ──────────────────────────────────────────────────────────────
    def continuous_bars(self, min_bars: int = 40) -> pd.DataFrame:
        """Session bars, extended backwards with yesterday's only if needed.

        THE PROBLEM THIS SOLVES. On 5-minute bars a session starts with zero
        history and accumulates twelve bars an hour. `momentum` and `vwap` need
        30 (≈11:45 IST) and `ict_smc` needs 40 (≈12:35 IST) — so three of eight
        lenses were structurally silent through the entire first half of every
        session, including the open, which is where the day's range is usually
        set. That is not a tuning preference; it is a third of the roster
        switched off during the most active hours.

        Only as many prior bars as are actually needed are prepended, so by
        midday the window is pure session data and behaves exactly as it did
        before. The overnight gap is real and is NOT smoothed over — a gap
        through yesterday's range is a genuine breakout and momentum should see
        it as one.

        Callers that must not cross the session boundary use `bars` directly.
        """
        if self.bars is None or len(self.bars) >= min_bars:
            return self.bars if self.bars is not None else pd.DataFrame()
        if self.prior_bars is None or self.prior_bars.empty:
            return self.bars
        need = min_bars - len(self.bars)
        tail = self.prior_bars.tail(need)
        cols = [c for c in self.bars.columns if c in tail.columns] or list(tail.columns)
        return pd.concat([tail[cols], self.bars[cols]], ignore_index=True)

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


def _normalise_bars(df):
    """Angel's bar frame -> the schema every lens and the replay path use.

    `AngelFetcher._rows_to_df` returns CAPITALISED Open/High/Low/Close/Volume on
    a `Date` index. Lenses and nse/backtest/replay.py both use lowercase columns
    plus an explicit `datetime` column.

    Live bars were therefore structurally unreadable by vwap, momentum and
    ict_smc — and it stayed hidden because each of those lenses checks its
    minimum BAR COUNT before it touches a column. For the first half of every
    session they abstained with "0 bars" / "too few bars", which looks like
    warm-up; only once enough bars accumulated did the real message appear
    ("bars lack close/volume"). A guard that fails for a plausible-looking
    reason hides the actual one.

    Normalised here, at the single live-bar boundary, rather than in each lens:
    three lenses independently coping with two schemas is three chances to cope
    differently.
    """
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    out = df.copy()
    if out.index.name in ("Date", "datetime", "date"):
        out = out.reset_index()
    out.columns = [str(c).lower() for c in out.columns]
    if "datetime" not in out.columns:
        for cand in ("date", "timestamp", "time"):
            if cand in out.columns:
                out = out.rename(columns={cand: "datetime"})
                break
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.dropna(subset=["datetime"]).sort_values("datetime")
    return out.reset_index(drop=True)


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

    fresh = _normalise_bars(fresh)
    if fresh is not None and not fresh.empty:
        _bars_cache[key] = (now, fresh)
        return fresh

    if cached:
        logger.debug("build_live: serving cached bars for %s (fetch returned nothing)",
                     symbol)
        return cached[1]
    return pd.DataFrame()


#: Prior-session bars change once a day, so they are cached for the session.
_prior_cache: dict = {}

#: Session-open open interest per contract, for deriving `oi_change` live.
#: (session_date, symbol) -> {(strike, option_type): first observed oi}
_oi_baseline: dict = {}


def _attach_oi_change(chain: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Derive `oi_change` / `oi_change_pct` for a LIVE chain.

    THE BUG THIS FIXES, WHICH ONLY PRODUCTION COULD REVEAL.

    `volume_oi` scores three components: OI walls, volume-profile position, and
    OI BUILD. The build component reads `oi_change`, a column that
    `nse/backtest/replay.py:normalise_day` computes from the session's first
    print — so it exists in every backtest and the lens measured +1.66/+1.49 bps
    WITH it.

    The live chain comes from the REST FULL-mode quote path, which carries `oi`
    as a level and no change column of any kind. (`angel_stream` decodes
    `open_interest_change_percentage` into `oi_change_pct`, but that is the
    socket tick cache, not the chain the snapshot is built from.) So in
    production the lens silently ran on two of its three components, permanently
    — a backtest/live divergence in the ONLY lens carrying weight.

    Deriving it here rather than in the lens keeps replay and live producing the
    same MarketSnapshot, which is the property the whole architecture rests on.

    HONEST LIMITATION: the baseline is the first OI this PROCESS observed, not
    the exchange's session open. They coincide only if the council starts before
    09:15. Start it late and the build component measures "change since we
    started watching", which is a smaller number than the backtest saw. The
    field is left ABSENT for the first observation of each contract rather than
    written as zero, because a fabricated zero would read as "no build" and be
    indistinguishable from a real flat reading.
    """
    from datetime import date as _date

    if chain is None or chain.empty or "oi" not in chain.columns:
        return chain

    key = (_date.today(), symbol)
    # Drop other days so this cannot grow without bound across a long-lived
    # process, and so a stale baseline never leaks into a new session.
    for k in [k for k in _oi_baseline if k != key]:
        _oi_baseline.pop(k, None)
    base = _oi_baseline.setdefault(key, {})

    out = chain.copy()
    oi = pd.to_numeric(out["oi"], errors="coerce").fillna(0.0)
    type_col = "option_type" if "option_type" in out.columns else "side"

    changes, pcts = [], []
    for i, row in enumerate(out.itertuples(index=False)):
        ck = (getattr(row, "strike", None), str(getattr(row, type_col, "")).upper())
        now = float(oi.iloc[i])
        if ck not in base:
            base[ck] = now
            changes.append(float("nan"))
            pcts.append(float("nan"))
            continue
        first = base[ck]
        changes.append(now - first)
        pcts.append(((now - first) / first * 100.0) if first > 0 else 0.0)

    out["oi_change"] = changes
    out["oi_change_pct"] = pcts
    return out


def _cached_prior_bars(fetcher, symbol: str, interval: str, today):
    """The previous session's bars, fetched once per day.

    Only the rows STRICTLY BEFORE today's first bar are kept. Angel's
    multi-day endpoint returns a window that includes today, and letting those
    rows through would duplicate the session's own bars inside `prior_bars`,
    quietly double-counting the morning in any lens that concatenates.
    """
    import datetime as _dt

    key = (symbol, interval, _dt.date.today())
    if key in _prior_cache:
        return _prior_cache[key]

    out = pd.DataFrame()
    try:
        hist = _normalise_bars(fetcher.fetch_historical_df(symbol, interval, days=5))
        if hist is not None and not hist.empty:
            if "datetime" in hist.columns:
                hist["datetime"] = pd.to_datetime(hist["datetime"])
                if today is not None and not today.empty and "datetime" in today.columns:
                    first = pd.to_datetime(today["datetime"]).min()
                    hist = hist[hist["datetime"] < first]
                else:
                    cutoff = pd.Timestamp(_dt.date.today())
                    if hist["datetime"].dt.tz is not None:
                        cutoff = cutoff.tz_localize(hist["datetime"].dt.tz)
                    hist = hist[hist["datetime"] < cutoff]
            out = hist.reset_index(drop=True)
    except Exception as e:
        logger.debug("build_live: prior bars unavailable for %s: %s", symbol, e)

    _prior_cache[key] = out
    return out


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

        # Live chains carry oi as a LEVEL only; the build component needs the
        # change. Derived here so replay and live emit the same schema.
        chain = _attach_oi_change(chain, symbol)

        bars = _cached_bars(cache.fetcher, symbol, bars_interval)
        prior = _cached_prior_bars(cache.fetcher, symbol, bars_interval, bars)

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
            prior_bars=prior if prior is not None else pd.DataFrame(),
            vix=vix,
            source="live",
        )
    except Exception as e:
        logger.exception("build_live failed for %s: %s", symbol, e)
        return None
