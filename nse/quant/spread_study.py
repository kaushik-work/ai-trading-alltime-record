"""Measure the half-spread distribution. The number that decides NSE options.

WHY THIS EXISTS

`nse/backtest/costs.py` treats spread as a swept parameter because no bid/ask
existed in any source we held: the 5-year CSV has no quote columns, and the
collector wrote zeros for months. Estimation from OHLC left a band from 0.03%
(one tick at ATM) to 0.9% (Corwin-Schultz) — a 30x range — so every options
result has been reported as "profitable while half-spread < X" rather than as a
number (RESEARCH_LEARNINGS 2.3).

That was the right call while the input was unknown. It is no longer unknown:
the collector has stored real `depth.buy[]` / `depth.sell[]` since 2026-08-04.

WHAT IT DECIDES

The Volume/OI options strategy is profitable in both splits up to a 0.10%
half-spread. One morning's spot check of twenty contracts put the mean for the
contracts it actually buys at 0.1038% — sitting exactly on the line (§3.8). One
morning is not a distribution. This module builds the real one.

METHOD

Half-spread as a percentage of MID, per contract observation:

    half_spread_pct = (ask - bid) / 2 / mid * 100

Reported by moneyness and by time of day, because both matter and neither is
visible in a single average:

  * A strategy that buys ITM strikes does not care what the ATM spread is.
  * Spreads widen at the open and into the close. A mean over the whole session
    understates the cost of trading at 09:20 and overstates it at 12:30.

Rows are dropped, never repaired, when the book is one-sided, crossed, or
absurdly wide. A fabricated spread is exactly what this module exists to stop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Discard anything wider than this as a data artefact rather than a quote. A
# genuine 25% half-spread on a liquid index option is a stale or crossed book,
# not a price anyone traded at, and a handful of them would drag every mean.
MAX_PLAUSIBLE_HALF_SPREAD_PCT = 25.0

# Moneyness buckets, in strikes away from ATM.
MONEYNESS_BUCKETS = [
    ("deep ITM", -99, -6),
    ("ITM", -6, -2),
    ("near ATM", -2, 2),
    ("OTM", 2, 6),
    ("deep OTM", 6, 99),
]


@dataclass
class SpreadStats:
    n: int
    mean: float
    p25: float
    p50: float
    p75: float
    p90: float
    p95: float

    def as_row(self) -> dict:
        return {"n": self.n, "mean": round(self.mean, 4),
                "p25": round(self.p25, 4), "p50": round(self.p50, 4),
                "p75": round(self.p75, 4), "p90": round(self.p90, 4),
                "p95": round(self.p95, 4)}


def _stats(x: np.ndarray) -> Optional[SpreadStats]:
    if x.size == 0:
        return None
    return SpreadStats(
        n=int(x.size), mean=float(x.mean()),
        p25=float(np.percentile(x, 25)), p50=float(np.percentile(x, 50)),
        p75=float(np.percentile(x, 75)), p90=float(np.percentile(x, 90)),
        p95=float(np.percentile(x, 95)))


def load(symbol: Optional[str] = None, limit: int = 500_000) -> pd.DataFrame:
    """Pull observations that carry a genuine two-sided book.

    Everything before the collector fix on 2026-08-04 stored bid/ask as zero,
    so the `bid > 0 AND ask > 0` filter is also the date filter — no cutoff
    needs hardcoding, and if the fix is ever reverted the sample simply stops
    growing rather than silently filling with zeros.
    """
    from core.mongo import get_db

    db = get_db()
    if db is None:
        raise RuntimeError("Mongo unavailable")

    q: dict = {"bid": {"$gt": 0}, "ask": {"$gt": 0}}
    if symbol:
        q["symbol"] = symbol
    fields = {"_id": 0, "symbol": 1, "date": 1, "timestamp": 1, "strike": 1,
              "option_type": 1, "ltp": 1, "bid": 1, "ask": 1, "spot": 1,
              "oi": 1, "volume": 1}
    rows = list(db["option_snapshots"].find(q, fields).limit(limit))
    return pd.DataFrame(rows)


def prepare(df: pd.DataFrame, step: int = 50) -> pd.DataFrame:
    """Compute half-spread, moneyness and session minute. Drops bad books."""
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    for c in ("bid", "ask", "ltp", "spot", "strike"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["bid", "ask"])

    # A crossed book (bid > ask) is corrupt, not a negative spread.
    out = out[out["ask"] >= out["bid"]]
    out["mid"] = (out["bid"] + out["ask"]) / 2.0
    out = out[out["mid"] > 0]

    out["half_spread"] = (out["ask"] - out["bid"]) / 2.0
    out["half_spread_pct"] = out["half_spread"] / out["mid"] * 100.0
    out = out[out["half_spread_pct"] <= MAX_PLAUSIBLE_HALF_SPREAD_PCT]

    # Strikes from ATM, signed so a call and a put at the same strike land in
    # opposite buckets — a 24500 CE with spot at 24600 is ITM, the PE is OTM.
    if "spot" in out.columns and out["spot"].notna().any():
        atm = (out["spot"] / step).round() * step
        raw = (out["strike"] - atm) / step
        is_put = out["option_type"].astype(str).str.upper().str.startswith("P")
        out["strikes_from_atm"] = np.where(is_put, -raw, raw)
    else:
        out["strikes_from_atm"] = np.nan

    ts = pd.to_datetime(out.get("timestamp"), errors="coerce")
    out["minute_of_day"] = ts.dt.hour * 60 + ts.dt.minute
    out["session_bucket"] = pd.cut(
        out["minute_of_day"],
        bins=[0, 9 * 60 + 30, 10 * 60 + 30, 14 * 60, 15 * 60, 24 * 60],
        labels=["open 09:15-09:30", "early 09:30-10:30", "midday 10:30-14:00",
                "late 14:00-15:00", "close 15:00+"],
        include_lowest=True)
    return out


def by_moneyness(df: pd.DataFrame) -> dict[str, SpreadStats]:
    out: dict[str, SpreadStats] = {}
    for name, lo, hi in MONEYNESS_BUCKETS:
        sel = df[(df["strikes_from_atm"] >= lo) & (df["strikes_from_atm"] < hi)]
        s = _stats(sel["half_spread_pct"].to_numpy())
        if s:
            out[name] = s
    return out


def by_session(df: pd.DataFrame) -> dict[str, SpreadStats]:
    out: dict[str, SpreadStats] = {}
    for name, sel in df.groupby("session_bucket", observed=True):
        s = _stats(sel["half_spread_pct"].to_numpy())
        if s:
            out[str(name)] = s
    return out


def by_premium_band(df: pd.DataFrame, lo: float = 120.0,
                    hi: float = 190.0) -> Optional[SpreadStats]:
    """The band the Volume/OI strategy actually buys — the number that decides it."""
    px = df["ltp"] if "ltp" in df.columns else df["mid"]
    sel = df[(px >= lo) & (px <= hi)]
    return _stats(sel["half_spread_pct"].to_numpy())


def verdict(stats: Optional[SpreadStats], threshold: float) -> str:
    """Does the measured distribution clear the strategy's survival threshold?"""
    if stats is None:
        return "no observations in this band"
    if stats.p75 <= threshold:
        return f"CLEARS — 75% of quotes at or under {threshold:.2f}%"
    if stats.p50 <= threshold:
        return (f"MARGINAL — median {stats.p50:.4f}% clears but p75 "
                f"{stats.p75:.4f}% does not")
    return (f"FAILS — median {stats.p50:.4f}% already exceeds {threshold:.2f}%")
