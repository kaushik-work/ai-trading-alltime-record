"""Synthetic-forward signal generator.

Computes implied forward from put-call parity:
    F = C - P + K
and compares it to the underlying spot. A positive deviation means the
options market is pricing the index higher than spot; we fade it by going
short the synthetic forward (sell CE / buy PE). Negative deviation → long.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from nse.config import ENTRY_PCT, MIN_STRIKES, MONEYNESS, PERSIST_HOURS, TT_MAX_HOURS, TT_MIN_HOURS
from nse.models import SyntheticForwardSignal

logger = logging.getLogger(__name__)


class SyntheticForwardStrategy:
    """Stateless signal generator. Persistence history is managed by caller."""

    def __init__(self, symbol: str):
        self.symbol = symbol

    def compute(self, snapshot: pd.DataFrame, t: Optional[datetime] = None) -> list[SyntheticForwardSignal]:
        """Compute all synthetic-forward signals for a single snapshot."""
        if snapshot.empty:
            return []
        if t is None:
            t = datetime.now(timezone.utc)
        spot = float(snapshot["spot"].median()) if "spot" in snapshot.columns else None
        if spot is None or spot <= 0 or pd.isna(spot):
            logger.debug("SyntheticForwardStrategy.compute: spot unavailable for %s", self.symbol)
            return []

        out: list[SyntheticForwardSignal] = []
        expiries = sorted(snapshot["expiry"].dropna().unique())
        for exp in expiries:
            exp_ts = pd.Timestamp(exp).tz_convert("UTC") if pd.Timestamp(exp).tzinfo else pd.Timestamp(exp, tz="UTC")
            t_ts = pd.Timestamp(t).tz_convert("UTC") if pd.Timestamp(t).tzinfo else pd.Timestamp(t, tz="UTC")
            tte_h = (exp_ts - t_ts).total_seconds() / 3600
            if not (TT_MIN_HOURS <= tte_h <= TT_MAX_HOURS):
                continue
            same = snapshot[snapshot["expiry"] == exp]
            calls = same[same["side"] == "CE"].set_index("strike")
            puts = same[same["side"] == "PE"].set_index("strike")
            if calls.empty or puts.empty:
                continue
            common = sorted(set(calls.index) & set(puts.index))
            near = [K for K in common if abs(K - spot) / spot <= MONEYNESS]
            if len(near) < MIN_STRIKES:
                continue
            devs = []
            for K in near:
                cp = float(calls.loc[K, "mark"])
                pp = float(puts.loc[K, "mark"])
                if cp <= 0 or pp <= 0:
                    continue
                devs.append(((cp - pp + K) - spot) / spot)
            if len(devs) < MIN_STRIKES:
                continue
            pos = sum(1 for d in devs if d > 0)
            neg = sum(1 for d in devs if d < 0)
            if pos < MIN_STRIKES and neg < MIN_STRIKES:
                continue
            pred = float(np.median(devs))
            synth_f = spot * (1 + pred)
            side = "long" if pred < 0 else "short"
            exp_dt = pd.Timestamp(exp).tz_convert("UTC").to_pydatetime() if pd.Timestamp(exp).tzinfo else pd.Timestamp(exp, tz="UTC").to_pydatetime()
            out.append(SyntheticForwardSignal(
                symbol=self.symbol,
                expiry=exp_dt,
                pred=pred,
                n_strikes=len(devs),
                spot=spot,
                synth_forward=synth_f,
                side=side,
                timestamp=t if t.tzinfo else t.replace(tzinfo=timezone.utc),
                strikes_used=near,
            ))
        return out

    def gate(self, signal: SyntheticForwardSignal,
             sig_history: dict[datetime, list[tuple[datetime, float]]]) -> bool:
        """Apply entry gate + persistence filter."""
        if abs(signal.pred) < ENTRY_PCT:
            return False
        hist = sig_history.get(signal.expiry, [])
        cutoff = signal.timestamp - pd.Timedelta(hours=PERSIST_HOURS)
        recent = [(ti, pi) for ti, pi in hist if ti >= cutoff]
        if len(recent) < PERSIST_HOURS:
            return False
        same_dir = sum(1 for _, pi in recent if np.sign(pi) == np.sign(signal.pred))
        return same_dir >= PERSIST_HOURS

    def pick_best(self, signals: list[SyntheticForwardSignal]) -> Optional[SyntheticForwardSignal]:
        """Return the strongest signal by absolute deviation."""
        if not signals:
            return None
        return max(signals, key=lambda s: abs(s.pred))
