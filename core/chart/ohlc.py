"""True OHLC for both venues, cached to disk.

The archived NIFTY option dataset carries `spot` as a CLOSE-only series, so its
resampled highs and lows are extremes of closes rather than real intrabar
extremes (RESEARCH_LEARNINGS section 4). Every rule in core/chart is about
wicks. That data cannot be used here, and quietly using it anyway would produce
a sweep detector that finds almost nothing and a backtest that looks clean
because it never traded.

These two sources return genuine highs and lows:

    Delta   GET /v2/history/candles   public, no auth, generous history
    Angel   getCandleData             authenticated, per-request day limits

Both are normalised to one schema — datetime, open, high, low, close, volume,
oldest first — so core/chart cannot tell them apart.

Cached as CSV under db/ohlc_cache/. Angel in particular rate-limits and chunks
awkwardly, and re-pulling five years of 5-minute bars on every experiment is
both slow and rude.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parents[2] / "db" / "ohlc_cache"
COLUMNS = ["datetime", "open", "high", "low", "close", "volume"]

# Angel refuses very long ranges in one call; chunk requests to this many days.
ANGEL_CHUNK_DAYS = 25


def _cache_path(venue: str, symbol: str, interval: str) -> Path:
    return CACHE_DIR / f"{venue}_{symbol}_{interval}.csv"


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """One schema, oldest first, no duplicate timestamps, no missing bars."""
    if df is None or df.empty:
        return pd.DataFrame(columns=COLUMNS)
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    ren = {"time": "datetime", "date": "datetime", "timestamp": "datetime",
           "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    out = out.rename(columns={k: v for k, v in ren.items() if k in out.columns})
    for col in COLUMNS:
        if col not in out.columns:
            out[col] = 0.0 if col != "datetime" else pd.NaT
    # Delta returns epoch SECONDS; Angel returns ISO strings. Passing raw epoch
    # ints to to_datetime without a unit makes pandas read them as NANOseconds,
    # which silently dates every 2026 bar to 1970-01-01 and destroys ordering,
    # session boundaries and any time-of-day feature computed downstream.
    dt = out["datetime"]
    if pd.api.types.is_numeric_dtype(dt):
        m = pd.to_numeric(dt, errors="coerce").abs().max()
        unit = "ms" if m and m > 1e12 else "s"
        out["datetime"] = pd.to_datetime(dt, errors="coerce", utc=True, unit=unit)
    else:
        out["datetime"] = pd.to_datetime(dt, errors="coerce", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = (out.dropna(subset=["datetime", "open", "high", "low", "close"])
              .drop_duplicates("datetime")
              .sort_values("datetime")
              .reset_index(drop=True))
    # A bar whose high is below its low, or whose close sits outside the range,
    # is corrupt. Dropped rather than repaired: a fabricated wick is precisely
    # what this module exists to avoid.
    bad = ((out["high"] < out["low"])
           | (out["close"] > out["high"]) | (out["close"] < out["low"])
           | (out["open"] > out["high"]) | (out["open"] < out["low"]))
    if bad.any():
        logger.warning("ohlc: dropping %d malformed bars", int(bad.sum()))
        out = out[~bad].reset_index(drop=True)
    return out[COLUMNS]


def load_cached(venue: str, symbol: str, interval: str) -> Optional[pd.DataFrame]:
    p = _cache_path(venue, symbol, interval)
    if not p.exists():
        return None
    try:
        return _normalise(pd.read_csv(p))
    except Exception as e:
        logger.warning("ohlc: cache read failed for %s: %s", p.name, e)
        return None


def save_cache(df: pd.DataFrame, venue: str, symbol: str, interval: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = _cache_path(venue, symbol, interval)
    _normalise(df).to_csv(p, index=False)
    logger.info("ohlc: cached %d bars -> %s", len(df), p.name)


# ── Delta ────────────────────────────────────────────────────────────────────
def fetch_delta(symbol: str = "ETHUSD", interval: str = "5m",
                days: int = 180, use_cache: bool = True) -> pd.DataFrame:
    """Delta perp candles. Public endpoint, so no credentials needed."""
    if use_cache:
        c = load_cached("delta", symbol, interval)
        if c is not None and len(c) > 100:
            return c

    from core.brokers.delta_crypto import DeltaCryptoBroker

    broker = DeltaCryptoBroker(mode="paper")

    # Page BACKWARDS with explicit start/end windows. broker.get_candles()
    # only takes lookback_hours and always measures from now, so calling it
    # repeatedly returns the same recent window every time — the endpoint caps
    # around 4,000 rows, so that silently yields ~14 days of 5m bars however
    # many days were asked for.
    frames = []
    end_ts = int(datetime.now(timezone.utc).timestamp())
    floor_ts = end_ts - days * 86400
    while end_ts > floor_ts:
        start_ts = max(floor_ts, end_ts - 10 * 86400)
        try:
            data = broker._request(
                "GET", "/v2/history/candles",
                params={"symbol": symbol, "resolution": interval,
                        "start": start_ts, "end": end_ts})
            rows = data.get("result", []) if data.get("success") else []
        except Exception as e:
            logger.error("fetch_delta %s window %s..%s: %s",
                         symbol, start_ts, end_ts, e)
            break
        if not rows:
            break
        frames.append(pd.DataFrame(rows))
        oldest = min(int(r.get("time", end_ts)) for r in rows)
        if oldest >= end_ts:
            break                       # no progress; stop rather than spin
        end_ts = oldest - 1

    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    df = _normalise(pd.concat(frames, ignore_index=True))
    if len(df):
        save_cache(df, "delta", symbol, interval)
    return df


# ── Angel ────────────────────────────────────────────────────────────────────
def fetch_angel(symbol: str = "NIFTY", interval: str = "5m",
                days: int = 180, use_cache: bool = True) -> pd.DataFrame:
    """Angel index candles via getCandleData, chunked and cached."""
    if use_cache:
        c = load_cached("angel", symbol, interval)
        if c is not None and len(c) > 100:
            return c

    from data.angel_fetcher import _INTERVAL_MAP, _SPOT_TOKENS, AngelFetcher

    f = AngelFetcher.get()
    if not f._ensure_logged_in():
        logger.error("fetch_angel: not logged in")
        return pd.DataFrame(columns=COLUMNS)

    spot = _SPOT_TOKENS.get(symbol)
    ang_int = _INTERVAL_MAP.get(interval)
    if spot is None or ang_int is None:
        logger.error("fetch_angel: unsupported %s / %s", symbol, interval)
        return pd.DataFrame(columns=COLUMNS)

    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    end = datetime.now(ist)
    frames = []
    remaining = days
    while remaining > 0:
        chunk = min(ANGEL_CHUNK_DAYS, remaining)
        start = end - timedelta(days=chunk)
        try:
            rows = f._candle_data(spot["token"], spot["exchange"], ang_int,
                                  start.strftime("%Y-%m-%d 09:15"),
                                  end.strftime("%Y-%m-%d 15:30"))
            if rows:
                frames.append(pd.DataFrame(
                    rows, columns=["datetime", "open", "high", "low", "close", "volume"]))
        except Exception as e:
            logger.warning("fetch_angel chunk %s..%s: %s", start.date(), end.date(), e)
        end = start
        remaining -= chunk

    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    df = _normalise(pd.concat(frames, ignore_index=True))
    if len(df):
        save_cache(df, "angel", symbol, interval)
    return df


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate to a higher timeframe with TRUE extremes.

    high = max of highs and low = min of lows, which is only correct because
    the source carries real intrabar extremes. Resampling a close-only series
    this way produces highs that are maxima of closes — the exact defect that
    makes the option archive unusable for wick rules.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=COLUMNS)
    s = df.set_index("datetime")
    out = s.resample(rule).agg({"open": "first", "high": "max", "low": "min",
                                "close": "last", "volume": "sum"}).dropna()
    return out.reset_index()
