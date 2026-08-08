"""Measured backtest results, and the brain weights they earn.

A lens does not get a weight because it was built. It gets one because it was
measured. This module is the single place those measurements are written down,
so the number a lens trades on can always be traced to the run that produced it.

PROTOCOL EVERY ROW BELOW WENT THROUGH

    390 NIFTY sessions (260 TRAIN 2021-23, 130 VALIDATE 2024), 30-minute
    decision grid, 60-minute forward horizon, 10 strikes either side of ATM,
    identical MarketSnapshots for every lens, entry edge measured against a
    mix-matched baseline (same long/short mix, timing removed).

    TEST (2025-26) IS UNSPENT and stays that way until one assembled candidate
    is ready. See RESEARCH_LEARNINGS section 2.1.

WHAT THE MEASUREMENT SAID: ONE LENS OF FOUR SURVIVED

Three of the four numeric lenses measured NEGATIVE, and the combined vote did
not beat the survivor alone. The roster stays in the code — a lens costs
microseconds and journals its opinion whether or not it can trade — but only
volume_oi carries weight, and only on PROBATION.

DO NOT FLIP A NEGATIVE LENS'S SIGN TO RESCUE IT

A `train_signed` scheme (each lens's TRAIN sign taken as its convention, weight
|edge|) scored VALIDATE +1.90 bps against the champion's +1.49 and looks like
the combination working. It is not:

  - it agreed with volume_oi alone on 82.5% of decisions — it IS the champion,
    levered, because sign-flipped vwap correlates +0.77 with it;
  - it handed 48.5% of its weight to ict_smc, the WORST lens, purely because
    weighting by |edge| rewards whichever lens was most wrong;
  - with volume_oi removed, the three flipped lenses scored VALIDATE +0.91 bps
    at p=0.2118 — nothing.

The +0.40 bps "gain" is a fitted sign convention, and by then VALIDATE had been
looked at roughly ten times, which puts p=0.0139 well inside what multiple
comparisons produce by chance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from nse.brain import BrainState, Lifecycle, load, log_transition, save
from nse.config import LENS_PROBATION_WEIGHT_CAP

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Measurement:
    """One lens's measured entry edge, and the weight it earns."""

    lens: str
    train_bps: Optional[float]
    train_p: Optional[float]
    validate_bps: Optional[float]
    validate_p: Optional[float]
    lifecycle: Lifecycle
    bootstrap_weight: float
    note: str

    @property
    def signs_agree(self) -> bool:
        if self.train_bps is None or self.validate_bps is None:
            return False
        return (self.train_bps > 0) == (self.validate_bps > 0)


#: Measured 2026-08-08. Re-run nse/backtest/lens_harness.py to refresh, and
#: change these numbers ONLY from a run, never from an expectation.
MEASURED: dict[str, Measurement] = {
    "volume_oi": Measurement(
        lens="volume_oi",
        train_bps=+1.66, train_p=0.0012,
        validate_bps=+1.49, validate_p=0.0527,
        lifecycle=Lifecycle.PROBATION,
        bootstrap_weight=LENS_PROBATION_WEIGHT_CAP,
        note=("the only survivor: positive in both splits, signs agree, "
              "break-even half-spread 0.70% against a measured near-ATM p90 of "
              "0.157%. PROBATION not ACTIVE — VALIDATE is p=0.0527, and the "
              "edge moved 1.80 -> 1.66 bps on a change of bar construction "
              "alone, which is more fragility than a full weight deserves."),
    ),
    "vwap": Measurement(
        lens="vwap",
        train_bps=-2.31, train_p=0.0014,
        validate_bps=-1.06, validate_p=0.2585,
        lifecycle=Lifecycle.SHADOW,
        bootstrap_weight=0.0,
        note=("negative, and NOT independent: -0.769 correlated with "
              "volume_oi, agreeing with it on only 18.4% of decisions. This is "
              "largely volume_oi's volume-profile component with the opposite "
              "sign convention, so its negative result is not separate "
              "evidence and giving it weight would double-count the champion."),
    ),
    "ict_smc": Measurement(
        lens="ict_smc",
        train_bps=-4.25, train_p=0.0000,
        validate_bps=-0.68, validate_p=0.5609,
        lifecycle=Lifecycle.SHADOW,
        bootstrap_weight=0.0,
        note=("significantly negative on TRAIN and abstaining on 57% of "
              "snapshots. The pre-registered caveat — archived wicks are "
              "extremes of one-minute CLOSES, so this lens under-fires on "
              "replay — would make a WEAK result ambiguous, but this result is "
              "not weak-ambiguous: where it fired, it lost. Re-measure if a "
              "true-OHLC spot series ever lands."),
    ),
    "greeks": Measurement(
        lens="greeks",
        train_bps=-0.54, train_p=0.4485,
        validate_bps=-1.08, validate_p=0.1405,
        lifecycle=Lifecycle.SHADOW,
        bootstrap_weight=0.0,
        note=("no edge in either split. Note VALIDATE went 86.6% long against "
              "TRAIN's 56.4%: SKEW_NEUTRAL was calibrated on TRAIN and does not "
              "hold in 2024, so the lens is measuring the constant, not the "
              "skew."),
    ),
    "vision": Measurement(
        lens="vision",
        train_bps=None, train_p=None,
        validate_bps=None, validate_p=None,
        lifecycle=Lifecycle.SHADOW,
        bootstrap_weight=0.0,
        note=("unmeasurable by replay — there are no historical screenshots, so "
              "it can never earn a bootstrap weight the way a numeric lens "
              "does. Weight 0 until LIVE attribution over 30+ closed trades "
              "says otherwise. This is the pin that keeps CLAUDE.md's "
              "'no LLM in signal generation' rule true in practice."),
    ),
}


def seed(force: bool = False) -> dict[str, BrainState]:
    """Write the measured bootstrap weights into each lens's brain.

    Idempotent and conservative: a brain that has already accumulated live
    closed trades is LEFT ALONE unless `force`, because a backtest number must
    never overwrite a live track record. Seeding is for cold start only.
    """
    out: dict[str, BrainState] = {}
    for name, m in MEASURED.items():
        state = load(name, backtestable=(m.train_bps is not None),
                     bootstrap_weight=m.bootstrap_weight)

        if state.n_closed > 0 and not force:
            logger.info("lens %s: %d live closed trades — seed skipped",
                        name, state.n_closed)
            out[name] = state
            continue

        state.bootstrap_weight = m.bootstrap_weight
        state.lifecycle = m.lifecycle
        state.weight = m.bootstrap_weight if m.lifecycle.can_vote else 0.0
        state.notes = m.note
        save(state)
        log_transition(state, f"seeded from backtest: {m.lifecycle.value} "
                              f"weight {state.weight:.2f}")
        out[name] = state
    return out


def summary() -> str:
    rows = [f"{'lens':<11} {'TRAIN':>16} {'VALIDATE':>16} "
            f"{'lifecycle':<10} {'weight':>7}",
            "-" * 66]
    for m in MEASURED.values():
        tr = ("not measured" if m.train_bps is None
              else f"{m.train_bps:+.2f}bps p={m.train_p:.4f}")
        va = ("not measured" if m.validate_bps is None
              else f"{m.validate_bps:+.2f}bps p={m.validate_p:.4f}")
        rows.append(f"{m.lens:<11} {tr:>16} {va:>16} "
                    f"{m.lifecycle.value:<10} {m.bootstrap_weight:>7.2f}")
    return "\n".join(rows)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(summary())
    print()
    for name, st in seed().items():
        print(f"  {name:<11} -> {st.lifecycle.value:<10} weight {st.weight:.2f}")
