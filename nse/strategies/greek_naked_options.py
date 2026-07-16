"""Greek-aware naked long-options signal generator.

Uses the same synthetic-forward divergence as the base naked strategy, but
adds option-Greek filters so we only buy when options are "cheap" to hold and
not about to expire into a gamma/theta meat-grinder.

Key filters:
  1. IV rank / percentile — only buy when implied vol is in its lower regime.
  2. Minimum days to expiry — avoid the final 24h before expiry.
  3. Vega/theta ratio — want high vol exposure per day of time decay.
  4. Daily theta as % of premium — avoid bleeders.
  5. VIX spike guard — skip if India VIX has jumped >20% in 1 day.
  6. Delta confirmation — choose the strike closest to 0.50 delta (usually ATM).

The return type is a plain dict identical to `NakedOptionsStrategy.compute` so
live runners / backtests can swap the class with minimal change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from nse.strategies.naked_options import NakedOptionsStrategy


@dataclass
class GreekFilters:
    max_iv_rank: float = 0.70           # IV rank must be <= 70% (cheap-ish vol)
    min_days_to_expiry: float = 1.0     # avoid last 24h before expiry
    min_vega_theta_ratio: float = 2.0   # vega / (-theta) >= 2 (decent vol exposure per day)
    max_theta_pct: float = 0.20         # daily theta <= 20% of premium (avoid bleeders)
    max_vix_1d_change_pct: float = 20.0 # skip if VIX spiked >20% from prior day close
    target_delta_abs: float = 0.50      # pick strike nearest 0.50 |delta|
    delta_band: float = 0.15            # accept |delta| in [0.35, 0.65]


class GreekNakedOptionsStrategy(NakedOptionsStrategy):
    """Naked long options with Greek-aware filters.

    Inherits the divergence computation from `NakedOptionsStrategy` and layers
    Greek filters on top.  Returns the same dict shape as the base class.
    """

    def __init__(self, symbol: str, filters: Optional[GreekFilters] = None):
        super().__init__(symbol)
        self.filters = filters or GreekFilters()
        self._iv_history: dict[str, list[tuple[datetime, float]]] = {}

    def compute(self, snapshot: pd.DataFrame, t: datetime) -> Optional[dict]:
        """Return a filtered trade idea or None."""
        raw_sigs = self._sf.compute(snapshot, t)
        if not raw_sigs:
            return None

        f = self.filters
        best: Optional[dict] = None
        best_score = -float("inf")

        # Global VIX guard (same for every candidate expiry).
        vix_now = None
        if "vix" in snapshot.columns and not snapshot["vix"].isna().all():
            vix_now = float(snapshot["vix"].dropna().iloc[-1])
            vix_yest = self._prior_day_vix_close(t)
            if (vix_yest and vix_yest > 0 and
                    (vix_now - vix_yest) / vix_yest * 100 > f.max_vix_1d_change_pct):
                return None

        for chosen in raw_sigs:
            if abs(chosen.pred) < self._entry_pct:
                continue

            option_type = "CE" if chosen.side == "long" else "PE"
            expiry = chosen.expiry

            # Time to expiry filter.
            dte = (expiry - t).total_seconds() / 86400
            if dte < f.min_days_to_expiry:
                continue

            candidates = snapshot[
                (snapshot["side"] == option_type)
                & (snapshot["expiry"] == expiry)
            ].copy()
            if candidates.empty:
                continue

            # If no Greeks available, fall back to base ATM signal.
            need = {"iv", "delta", "theta", "vega"}
            if not need.issubset(candidates.columns):
                sig = self._to_dict(chosen, t)
                if sig and abs(chosen.pred) > best_score:
                    best, best_score = sig, abs(chosen.pred)
                continue

            candidates = candidates.dropna(subset=list(need))
            if candidates.empty:
                continue

            # Greek filters.
            candidates["vega_theta"] = candidates["vega"] / (-candidates["theta"])
            candidates = candidates[candidates["vega_theta"] >= f.min_vega_theta_ratio]
            if candidates.empty:
                continue

            candidates["theta_pct"] = (-candidates["theta"]) / candidates["mark"]
            candidates = candidates[candidates["theta_pct"] <= f.max_theta_pct]
            if candidates.empty:
                continue

            iv_rank = self._iv_rank(snapshot, t)
            if iv_rank is not None and iv_rank > f.max_iv_rank:
                continue

            # Pick strike closest to target delta within the band.
            if option_type == "CE":
                candidates["delta_dist"] = (candidates["delta"] - f.target_delta_abs).abs()
                band = candidates[candidates["delta"] >= f.target_delta_abs - f.delta_band]
            else:
                candidates["delta_dist"] = (candidates["delta"] + f.target_delta_abs).abs()
                band = candidates[candidates["delta"] <= -f.target_delta_abs + f.delta_band]

            chosen_row = band.loc[band["delta_dist"].idxmin()] if not band.empty else candidates.loc[candidates["delta_dist"].idxmin()]

            # Score: divergence magnitude * vega/theta (favor cheap vol exposure).
            score = abs(chosen.pred) * chosen_row["vega_theta"]
            if score <= best_score:
                continue

            best_score = score
            best = {
                "symbol": chosen.symbol,
                "expiry": expiry,
                "pred": chosen.pred,
                "spot": chosen.spot,
                "side": "call" if chosen.side == "long" else "put",
                "signal_side": chosen.side,
                "timestamp": t,
                "strike": int(chosen_row["strike"]),
                "iv": float(chosen_row["iv"]),
                "delta": float(chosen_row["delta"]),
                "theta": float(chosen_row["theta"]),
                "vega": float(chosen_row["vega"]),
                "iv_rank": iv_rank,
                "vega_theta": float(chosen_row["vega_theta"]),
            }
            if vix_now is not None:
                best["vix"] = vix_now

        return best

    @property
    def _entry_pct(self) -> float:
        from nse.config import ENTRY_PCT
        return ENTRY_PCT

    def _to_dict(self, sig, t: datetime) -> dict:
        return {
            "symbol": sig.symbol,
            "expiry": sig.expiry,
            "pred": sig.pred,
            "spot": sig.spot,
            "side": "call" if sig.side == "long" else "put",
            "signal_side": sig.side,
            "timestamp": t,
        }

    def _iv_rank(self, snapshot: pd.DataFrame, t: datetime) -> Optional[float]:
        """Rough IV rank over the last 60 days of snapshots for this symbol."""
        key = str(snapshot["symbol"].iloc[0]) if "symbol" in snapshot.columns else self.symbol
        if "iv" not in snapshot.columns:
            return None
        current_iv = snapshot["iv"].median()
        if pd.isna(current_iv):
            return None

        hist = self._iv_history.get(key, [])
        hist = [(tt, iv) for tt, iv in hist if (t - tt).total_seconds() <= 60 * 86400]
        hist.append((t, current_iv))
        self._iv_history[key] = hist

        if len(hist) < 20:
            return None
        ivs = [iv for _, iv in hist]
        lo, hi = np.percentile(ivs, [5, 95])
        if hi <= lo:
            return None
        return float((current_iv - lo) / (hi - lo))

    def _prior_day_vix_close(self, t: datetime) -> Optional[float]:
        """Live runner should override this with broker data; backtests pass VIX column."""
        return None
