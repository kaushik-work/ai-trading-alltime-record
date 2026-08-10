"""Replay crypto perps through the same lenses, on the same protocol.

Deliberately reuses `nse.backtest.lens_harness.measure_entry` rather than
writing a crypto scorer. A second implementation of "what is this lens's edge"
is a second set of conventions — baseline, sign, abstention handling — that
will drift from the first, and then two numbers in the same repo will mean
subtly different things while looking comparable.

WHAT IS DIFFERENT FROM NSE, AND WHY IT MATTERS

    24/7            no sessions, no close, no expiry. `prior_bars` is a rolling
                    window of bars rather than yesterday's session, and the
                    "session end" exit does not exist.

    REAL VOLUME     every bar carries traded volume. On NIFTY the index prints a
                    level, not a trade, so vwap and composite_profile were
                    computed from a zero or synthetic column and abstained live.
                    Here they finally measure the thing they claim to.

    FORWARD RETURN  on the PERP itself, not on an option. There is no premium,
                    no spread-to-premium conversion, no delta. That removes the
                    largest source of cost uncertainty in the NSE numbers — and
                    also removes the excuse that costs killed a real edge.

THE SPLIT IS FRESH AND TEST IS UNSPENT

Crypto history is its own dataset; the NSE splits do not apply. TRAIN is the
first nine months, VALIDATE the next three, TEST the last five weeks and it
stays sealed until one candidate is final. Roughly 23 hypotheses have already
been tested in this repo, so a split that has been peeked at is not a hold-out
(RESEARCH_LEARNINGS section 2.1).

NO LOOKAHEAD, ENFORCED THE SAME WAY. A snapshot at bar i contains bars 0..i
inclusive and nothing after. Bar i has CLOSED by construction — these are
completed candles from the exchange, not a partially-formed current bar — and
the forward return is measured from bar i's close to bar (i+h)'s close.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Iterator, Optional, Sequence

import numpy as np
import pandas as pd

from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

#: Crypto's own three-way split. TEST spent once, at the end.
CRYPTO_SPLITS: dict[str, tuple[date, date]] = {
    "TRAIN":    (date(2025, 7, 1), date(2026, 3, 31)),
    "VALIDATE": (date(2026, 4, 1), date(2026, 6, 30)),
    "TEST":     (date(2026, 7, 1), date(2026, 12, 31)),
}

#: PER-SYMBOL splits, for instruments whose history does not cover the default.
#:
#: XAUTUSD lists from 2026-04-17 and has ZERO bars in the shared TRAIN window.
#: Measuring it on the default split would mean calibrating on VALIDATE and
#: TEST -- i.e. spending both hold-outs on the first look, which is precisely
#: what section 2.1 exists to prevent. It gets its own three-way split carved
#: from its own coverage instead.
#:
#: THE SAMPLE IS THIN AND SPANS ONE REGIME. Four months of one instrument is
#: not four months of evidence about all regimes, and any XAUT result should be
#: read as "held in this period" rather than "holds". Stated here so a positive
#: number is not mistaken for the same grade of evidence as ETH's thirteen
#: months.
SYMBOL_SPLITS: dict[str, dict[str, tuple[date, date]]] = {
    "XAUTUSD": {
        "TRAIN":    (date(2026, 4, 17), date(2026, 6, 30)),
        "VALIDATE": (date(2026, 7, 1), date(2026, 7, 31)),
        "TEST":     (date(2026, 8, 1), date(2026, 12, 31)),
    },
}

#: Bars handed to a lens as its "recent" window, and as prior history.
WINDOW_BARS = 400
PRIOR_BARS = 7 * 24 * 12          # seven days of 5-minute bars


def splits_for(symbol: str) -> dict:
    """The three-way split this symbol is measured on."""
    return SYMBOL_SPLITS.get(symbol.upper(), CRYPTO_SPLITS)


def split_of(d: date, symbol: str = "") -> Optional[str]:
    for name, (lo, hi) in splits_for(symbol).items():
        if lo <= d <= hi:
            return name
    return None


def load_bars(symbol: str = "ETHUSD", interval: str = "5m") -> Optional[pd.DataFrame]:
    from core.chart.ohlc import load_cached

    df = load_cached("delta", symbol, interval)
    if df is None or df.empty:
        return None
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    if "datetime" not in df.columns:
        df = df.reset_index().rename(columns={df.index.name or "index": "datetime"})
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce", utc=True)
    return df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)


def snapshots(symbol: str = "ETHUSD", interval: str = "5m",
              every_bars: int = 6, horizon_bars: int = 12,
              limit: Optional[int] = None,
              bars: Optional[pd.DataFrame] = None
              ) -> Iterator[tuple[MarketSnapshot, float, str]]:
    """Emit (snapshot, forward_return_bps, split) walking the bar series.

    `every_bars=6` is a decision every 30 minutes on 5-minute bars, matching the
    NSE grid so the two venues' numbers are comparable. `horizon_bars=12` is the
    60-minute horizon the NSE edge was measured at, for the same reason.
    """
    if bars is None:
        bars = load_bars(symbol, interval)
    if bars is None or bars.empty:
        logger.error("crypto replay: no bars for %s %s", symbol, interval)
        return

    closes = pd.to_numeric(bars["close"], errors="coerce").to_numpy(float)
    times = bars["datetime"].to_numpy()
    n = len(bars)
    emitted = 0

    start = max(WINDOW_BARS, PRIOR_BARS)
    for i in range(start, n - horizon_bars, every_bars):
        fwd = closes[i + horizon_bars]
        px = closes[i]
        if not (np.isfinite(px) and np.isfinite(fwd)) or px <= 0:
            continue

        ts = pd.Timestamp(times[i]).to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        sp = split_of(ts.date(), symbol)
        if sp is None:
            continue

        # Bars 0..i INCLUSIVE. Bar i has closed — these are completed exchange
        # candles, never a forming one — so this carries no future information.
        window = bars.iloc[max(0, i - WINDOW_BARS + 1):i + 1].reset_index(drop=True)
        prior = bars.iloc[max(0, i - WINDOW_BARS + 1 - PRIOR_BARS):
                          max(0, i - WINDOW_BARS + 1)].reset_index(drop=True)

        snap = MarketSnapshot(
            symbol=symbol, ts=ts, spot=float(px),
            expiry=None, atm=0,
            chain=pd.DataFrame(), bars=window, prior_bars=prior,
            source="replay",
        )
        yield snap, float((fwd - px) / px * 10_000.0), sp

        emitted += 1
        if limit and emitted >= limit:
            return


def replay_lens(lens, symbol: str = "ETHUSD", interval: str = "5m",
                every_bars: int = 6, horizon_bars: int = 12,
                limit: Optional[int] = None,
                bars: Optional[pd.DataFrame] = None,
                progress_every: int = 5000) -> list:
    """Run one lens over the series, returning lens_harness Observations."""
    from nse.backtest.lens_harness import Observation

    out: list = []
    for k, (snap, fwd_bps, sp) in enumerate(
            snapshots(symbol, interval, every_bars, horizon_bars, limit, bars), 1):
        v = lens.safe_evaluate(snap)
        d = 0 if v.abstained else int(v.direction)
        out.append(Observation(
            ts=snap.ts, session=snap.ts.date(), split=sp,
            spot=snap.spot, atm=0, dte=0.0,
            direction=d, confidence=float(v.confidence),
            # Signed BY THE LENS's direction, exactly as the NSE harness does,
            # so measure_entry's baseline arithmetic is unchanged.
            fwd_return_bps=(d * fwd_bps) if d else None,
            raw_move_bps=abs(fwd_bps),
            abstained=bool(v.abstained),
            features=dict(v.features or {}),
        ))
        if progress_every and k % progress_every == 0:
            logger.info("crypto replay %s: %d snapshots", lens.name, k)
    return out


def report(lens, observations: Sequence, symbol: str) -> None:
    """Same shape as the NSE report, so the two are read the same way."""
    from nse.backtest.lens_harness import measure_entry

    n_abs = sum(1 for o in observations if o.abstained)
    print("=" * 92)
    print(f"LENS: {lens.name}   {symbol}   observations={len(observations)}   "
          f"abstained={n_abs}")
    print("=" * 92)
    for sp in ("TRAIN", "VALIDATE", "TEST"):
        e = measure_entry(observations, sp)
        if e is None:
            print(f"  {sp:<9} insufficient data")
            continue
        if sp == "TEST":
            # Printed only as a count. TEST is spent once, on one candidate, at
            # the end -- reporting its edge here would spend it by accident.
            print(f"  {sp:<9} n={e.n:>5}  (held back — not scored)")
            continue
        print(e.line())
    tr = measure_entry(observations, "TRAIN")
    va = measure_entry(observations, "VALIDATE")
    if tr and va:
        agree = (tr.edge_bps > 0) == (va.edge_bps > 0)
        print(f"  TRAIN/VALIDATE sign agreement: {'YES' if agree else 'NO'}")
