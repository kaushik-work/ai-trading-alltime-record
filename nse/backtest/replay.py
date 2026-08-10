"""Rebuild historical MarketSnapshots so live lens code replays unchanged.

This is the bridge that makes a lens backtestable. A lens takes a
MarketSnapshot and returns a verdict; it must not be able to tell whether that
snapshot came from the Angel socket or from a CSV written in 2021. Everything
here exists to make the two indistinguishable.

The archived schema is NOT the live schema, and the differences are all traps:

    live                    archive              handled by
    ─────────────────────────────────────────────────────────────────
    option_type CE/PE       CALL/PUT             _OPTION_TYPE map
    strike                  strike_price         renamed
    expiry column           absent entirely      measured expiry calendar
    bid/ask/depth           absent entirely      left absent, never faked
    ltp                     close of the 1m bar  fill-at-close rule below

TWO RULES THIS MODULE ENFORCES, both bought with real money:

1. FILL AT THE BAR'S CLOSE, NEVER AT ITS LABEL. A bar stamped 09:30 covers
   09:30:00-09:30:59; its close is not knowable until 09:30:59. Reading the
   option price at the label gave five minutes of lookahead and turned
   -Rs 6,110 into +Rs 377,749. See RESEARCH_LEARNINGS section 1.2.

2. A MISSING STRIKE IS A RECORDED MISS, NOT A DROPPED SESSION. The archived
   ladder is ATM-relative and re-centres intraday, so a wing quoted at 09:30
   can be gone by 15:20 precisely BECAUSE the index moved. Dropping those
   sessions discards the losing days and manufactures an edge — it produced a
   fake +19.67pt result in commit da08fff. Snapshots are emitted with the gap
   present and `missing_strikes` counted.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterator, Optional

import numpy as np
import pandas as pd

from nse.config import STEP_SIZES, market_close_for
from nse.snapshot import MarketSnapshot, expiry_at_close

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# The archive spells option type in full.
_OPTION_TYPE = {"CALL": "CE", "PUT": "PE", "CE": "CE", "PE": "PE"}

# Three-way split. TEST is spent ONCE, at the end, on one candidate — with ~23
# hypotheses roughly one clears p<0.05 by chance, and a split you peek at is
# not a hold-out. See RESEARCH_LEARNINGS section 2.1.
SPLITS: dict[str, tuple[date, date]] = {
    "TRAIN":    (date(2021, 1, 1), date(2023, 12, 31)),
    "VALIDATE": (date(2024, 1, 1), date(2024, 12, 31)),
    "TEST":     (date(2025, 1, 1), date(2026, 12, 31)),
}


def split_of(d: date) -> Optional[str]:
    for name, (lo, hi) in SPLITS.items():
        if lo <= d <= hi:
            return name
    return None


# ── expiry, from the MEASURED calendar ───────────────────────────────────────
_expiry_dates: Optional[list[date]] = None


def expiry_dates() -> list[date]:
    """Every measured expiry date, ascending.

    Derived empirically in nse/quant/expiry_calendar.py rather than assumed
    from a weekday: NIFTY expiry moved from Thursday to Tuesday on 2025-09-02,
    so `weekday == 3` is wrong for the last nine months of this dataset — and
    expiry sets T, which sets every Greek. See RESEARCH_LEARNINGS section 1.8.
    """
    global _expiry_dates
    if _expiry_dates is None:
        from nse.quant.expiry_calendar import load_expiries
        df = load_expiries()
        _expiry_dates = sorted(
            pd.to_datetime(df[df["is_expiry"]]["date"]).dt.date.tolist())
    return _expiry_dates


def next_expiry_on_or_after(d: date) -> Optional[date]:
    for e in expiry_dates():
        if e >= d:
            return e
    return None


# ── snapshot construction ────────────────────────────────────────────────────
def normalise_day(day_df: pd.DataFrame, session: date,
                  symbol: str = "NIFTY") -> pd.DataFrame:
    """Archive frame -> the column names MarketSnapshot reads."""
    df = day_df.copy()
    df["option_type"] = df["option_type"].astype(str).str.upper().map(_OPTION_TYPE)
    df = df.dropna(subset=["option_type"])
    if "strike_price" in df.columns:
        df = df.rename(columns={"strike_price": "strike"})
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.dropna(subset=["strike"])
    df["strike"] = df["strike"].astype(int)

    # The bar's CLOSE is the only price knowable at decision time. `ltp` is set
    # from it deliberately — never from `open`, which is the bar label's price.
    df["ltp"] = pd.to_numeric(df["close"], errors="coerce")
    df["mark"] = df["ltp"]
    df["side"] = df["option_type"]

    exp = next_expiry_on_or_after(session)
    if exp is None:
        raise ValueError(f"no measured expiry on or after {session}")
    df["expiry"] = pd.Timestamp(expiry_at_close(exp, symbol))
    df["symbol"] = symbol

    # Open interest CHANGE since the session's first print, per contract.
    # The live feed supplies this directly (SNAP_QUOTE carries
    # open_interest_change_percentage); the archive has only the level, so it
    # is derived here. Day-scoped on purpose: it is baked into each snapshot,
    # so a lens still reads one immutable observation and stays stateless.
    if "oi" in df.columns:
        df["oi"] = pd.to_numeric(df["oi"], errors="coerce").fillna(0)
        first = (df.sort_values("datetime")
                   .groupby(["strike", "option_type"])["oi"].first()
                   .rename("oi_open"))
        df = df.merge(first, on=["strike", "option_type"], how="left")
        df["oi_change"] = df["oi"] - df["oi_open"]
        df["oi_change_pct"] = np.where(
            df["oi_open"] > 0, df["oi_change"] / df["oi_open"] * 100.0, 0.0)
    return df


def prior_session_bars(session: date, symbol: str = "NIFTY",
                       bar_minutes: int = 5) -> pd.DataFrame:
    """The previous trading session's bars, for `MarketSnapshot.prior_bars`.

    The live path supplies these so that momentum/ict_smc are not blind for the
    first half of every session. Replay MUST supply them too: a lens that sees
    yesterday live but not in backtest is a lens whose measured record describes
    a different system from the one trading, which is the whole failure mode the
    shared-MarketSnapshot design exists to prevent.

    Returns empty when the previous session is missing from the archive, which
    is the honest answer — never silently reaches further back, because a
    "previous session" three weeks stale is not the level anyone is watching.
    """
    from nse.backtest.nifty_loader import load_option_day

    prev = [d for d in available_sessions() if d < session]
    if not prev:
        return pd.DataFrame()
    try:
        pdf = load_option_day(max(prev).isoformat())
    except FileNotFoundError:
        return pd.DataFrame()
    if pdf is None or pdf.empty:
        return pd.DataFrame()

    df = normalise_day(pdf, max(prev), symbol)
    per_minute = (df.groupby("datetime", as_index=False)
                    .agg(close=("spot", "first"), volume=("volume", "sum"))
                    .sort_values("datetime")
                    .set_index("datetime"))
    return (per_minute.resample(f"{bar_minutes}min")
                      .agg(close=("close", "last"), high=("close", "max"),
                           low=("close", "min"), open=("close", "first"),
                           volume=("volume", "sum"))
                      .dropna().reset_index())


def snapshots_for_day(session: date, symbol: str = "NIFTY",
                      every_minutes: int = 5,
                      strikes_around: int = 10,
                      bar_minutes: int = 5,
                      day_df: Optional[pd.DataFrame] = None,
                      with_prior: bool = True,
                      ) -> Iterator[MarketSnapshot]:
    """Emit one MarketSnapshot per sampled minute of a session.

    `every_minutes` sets the decision grid. The crypto bot evaluates every
    minute and that is the honest default, but a 5-minute grid over 1,255
    sessions is already ~94k snapshots, so the coarser grid is the practical
    starting point for a sweep. State the grid whenever you report a result:
    assuming a fill on every candle is its own form of lookahead.
    """
    from nse.backtest.nifty_loader import load_option_day

    if day_df is None:
        try:
            day_df = load_option_day(session.isoformat())
        except FileNotFoundError:
            return

    if day_df is None or day_df.empty:
        return

    df = normalise_day(day_df, session, symbol)
    step = STEP_SIZES.get(symbol, 50)
    close_t = market_close_for(symbol, session)

    # Index bars for the structural lenses. Built once for the whole day, then
    # sliced STRICTLY up to each snapshot's own timestamp — handing a lens the
    # full day's bars would be lookahead of the most flattering kind, since a
    # session's POC or swing high is only knowable once the session is over.
    #
    # THE HIGHS AND LOWS HERE ARE EXTREMES OF ONE-MINUTE CLOSES, NOT TRUE
    # INTRABAR EXTREMES. The archive stores `spot` as a single close per minute
    # (RESEARCH_LEARNINGS §4), so resampling to 5 minutes gives the max and min
    # of five closes rather than the real wick. They are genuine values, not
    # fabricated ones — but they are systematically SHALLOWER than live, so any
    # lens keying on wicks (ict_smc, sweeps) will under-fire on replay relative
    # to live and validate worse than it performs. Stated here rather than
    # discovered later.
    per_minute = (df.groupby("datetime", as_index=False)
                    .agg(close=("spot", "first"), volume=("volume", "sum"))
                    .sort_values("datetime")
                    .set_index("datetime"))
    minute_bars = (per_minute.resample(f"{bar_minutes}min")
                             .agg(close=("close", "last"),
                                  high=("close", "max"),
                                  low=("close", "min"),
                                  open=("close", "first"),
                                  volume=("volume", "sum"))
                             .dropna()
                             .reset_index())

    prior = (prior_session_bars(session, symbol, bar_minutes)
             if with_prior else pd.DataFrame())

    for ts, bar in df.groupby("datetime", sort=True):
        if not isinstance(ts, pd.Timestamp):
            continue
        if ts.minute % every_minutes != 0:
            continue
        if ts.time() > close_t:
            continue

        spot = pd.to_numeric(bar["spot"], errors="coerce").dropna()
        if spot.empty:
            continue
        spot_v = float(spot.iloc[0])
        if spot_v <= 0:
            continue
        atm = int(round(spot_v / step)) * step

        # Trim to the strikes a live snapshot would carry, but do NOT require
        # them all to be present — see the module docstring.
        lo, hi = atm - strikes_around * step, atm + strikes_around * step
        chain = bar[(bar["strike"] >= lo) & (bar["strike"] <= hi)].copy()
        if chain.empty:
            continue

        expected = 2 * (2 * strikes_around + 1)
        missing = max(0, expected - len(chain))

        ts_utc = (ts.tz_localize(IST) if ts.tzinfo is None else ts).tz_convert("UTC")

        # A resampled bar is LABELLED BY ITS START. The bar labelled 12:30
        # covers 12:30:00-12:34:59 and does not close until 12:35, so including
        # it in a decision made at 12:30 hands the lens five minutes of its own
        # future. That is precisely the resample-label bug that turned
        # -Rs 6,110 into +Rs 377,749 (RESEARCH_LEARNINGS 1.2), and it reappeared
        # here the moment per-minute rows were resampled into OHLC.
        #
        # Only bars that have CLOSED are visible: label + bar_minutes <= ts.
        cutoff = ts - pd.Timedelta(minutes=bar_minutes)
        bars = minute_bars[minute_bars["datetime"] <= cutoff]
        yield MarketSnapshot(
            symbol=symbol,
            ts=ts_utc.to_pydatetime(),
            spot=spot_v,
            expiry=chain["expiry"].iloc[0].to_pydatetime(),
            atm=atm,
            chain=chain.reset_index(drop=True),
            bars=bars.reset_index(drop=True),
            prior_bars=prior,
            vix=None,                      # not in the archive; never faked
            source="replay",
        ), missing


def tradeable(snap: MarketSnapshot, strike: int, option_type: str) -> Optional[float]:
    """Price we could actually have transacted at, or None.

    Returns None for a no-trade bar. A minute with O=H=L=C and zero volume did
    not trade — the row simply repeats the last print — and treating those as
    fillable is the main way this dataset flatters a backtest.
    See RESEARCH_LEARNINGS section 4.
    """
    row = snap.at(strike, option_type)
    if row is None:
        return None
    if bool(row.get("no_trade", False)):
        return None
    px = row.get("ltp")
    return float(px) if px is not None and pd.notna(px) and float(px) > 0 else None


def available_sessions(limit: Optional[int] = None,
                       split: Optional[str] = None) -> list[date]:
    """Session dates present in the archive, optionally restricted to a split."""
    from nse.backtest.nifty_loader import DEFAULT_ROOT
    out = []
    for f in sorted(DEFAULT_ROOT.glob("*/NIFTY_*_1m.csv")):
        try:
            d = datetime.strptime(f.name[6:16], "%Y-%m-%d").date()
        except ValueError:
            continue
        if split and split_of(d) != split:
            continue
        out.append(d)
    return out[:limit] if limit else out
