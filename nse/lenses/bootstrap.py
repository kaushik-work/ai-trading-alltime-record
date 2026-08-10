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

WHAT THE MEASUREMENT SAID: ONE LENS OF EIGHT SURVIVED

Six of the seven backtestable lenses measured negative or flat, the eighth
(vision) cannot be replayed at all, and the combined vote did not beat the
survivor alone. The roster stays in the code — a lens costs microseconds and
journals its opinion whether or not it can trade — but only volume_oi carries
weight, and only on PROBATION.

Adding a lens is meant to be cheap and it is: a new one votes at weight 0 until
attribution promotes it, so a bad idea costs a journal entry rather than money.
Seven lenses have now cost exactly that, and the roster is more valuable for
having them measured and benched than it would be for never having tried.

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


#: Measured 2026-08-08; momentum and ict_smc re-measured 2026-08-10 after
#: prior-session bars changed what they can see. Re-run
#: nse/backtest/lens_harness.py to refresh, and
#: change these numbers ONLY from a run, never from an expectation.
MEASURED: dict[str, Measurement] = {
    "volume_oi": Measurement(
        lens="volume_oi",
        train_bps=+1.49, train_p=0.0036,
        validate_bps=+1.67, validate_p=0.0306,
        lifecycle=Lifecycle.PROBATION,
        bootstrap_weight=LENS_PROBATION_WEIGHT_CAP,
        note=("the only survivor. TWO components as of 2026-08-10: the OI-wall "
              "term was dropped after measuring null on BOTH splits (+0.26 "
              "p=0.6442 / -0.06 p=0.9422) while diluting the live two, since "
              "the lens averages whatever fires. Re-measured without it: "
              "TRAIN 1.66 -> 1.49, VALIDATE 1.49 -> 1.67, and VALIDATE crossed "
              "p=0.0527 -> p=0.0306. Break-even half-spread 0.50% against a "
              "measured near-ATM p50 of 0.1221% and p90 of 0.1562% — roughly "
              "3x headroom. "
              "STILL PROBATION, NOT ACTIVE. The removal is justified by a null "
              "that cannot be selected into, but the resulting +1.67 was "
              "obtained after looking at VALIDATE and is not a clean "
              "out-of-sample number. The edge also moved 1.80 -> 1.66 on a "
              "change of BAR CONSTRUCTION alone, which is more fragility than "
              "a full weight deserves. See RESEARCH_LEARNINGS 3.18."),
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
        train_bps=-1.41, train_p=0.0073,
        validate_bps=-0.15, validate_p=0.8546,
        lifecycle=Lifecycle.SHADOW,
        bootstrap_weight=0.0,
        note=("negative on TRAIN, near-zero on VALIDATE, signs disagree. The "
              "pre-registered caveat — archived wicks are extremes of "
              "one-minute CLOSES, so this lens under-fires on replay — would "
              "make a WEAK result ambiguous, but where it fires, it loses. "
              "RE-MEASURED with "
              "prior-session bars, which cut abstentions from 2,781 to 137 and "
              "raised TRAIN n from 828 to 2,532. On that much larger sample it "
              "is less bad (TRAIN -4.25 -> -1.41, VALIDATE -0.68 -> -0.15) but "
              "still negative and still sign-disagreeing, so the earlier result "
              "was not merely a small-sample artefact. Re-measure again if a "
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
    "smile": Measurement(
        lens="smile",
        train_bps=+0.53, train_p=0.6427,
        validate_bps=+0.18, validate_p=0.8707,
        lifecycle=Lifecycle.SHADOW,
        bootstrap_weight=0.0,
        note=("IV curvature (butterfly), orthogonal to the greeks lens's tilt "
              "and uncorrelated with the whole roster (|r| <= 0.10). Signs "
              "agree across splits but both edges are ~zero on n=815/743. "
              "FIRST MEASUREMENT WAS INVALID: BUTTERFLY_NEUTRAL was hardcoded "
              "0.0 when the TRAIN median is +0.0210, so the lens called wings "
              "rich 97.7% of the time and produced n=34 across three years. "
              "Same bug greeks fixed with SKEW_NEUTRAL. Also note long=2.5% on "
              "TRAIN: the tilt is nearly always negative, so this may still be "
              "measuring its own constant rather than the surface."),
    ),
    "momentum": Measurement(
        lens="momentum",
        train_bps=-1.06, train_p=0.5150,
        validate_bps=-0.73, validate_p=0.8163,
        lifecycle=Lifecycle.SHADOW,
        bootstrap_weight=0.0,
        note=("range breakout in ATR. No edge, signs disagree. Its main value "
              "is a CLEAN NEGATIVE: momentum is adjacent to inverting vwap's "
              "significantly-negative mean-reversion result, and it does not "
              "even show positive in-sample. That closes the "
              "'trend was the right convention' hypothesis rather than leaving "
              "it as an untested temptation. RE-MEASURED after prior-session "
              "bars removed the warm-up blindness: TRAIN n went 163 -> 305 and "
              "the edge went +1.38 -> -1.06, so the early-session breakouts it "
              "can now see are if anything worse than the midday ones it "
              "already saw. The warm-up fix was still right — a lens should be "
              "judged on the whole session — it simply did not rescue this one."),
    ),
    "liquidity": Measurement(
        lens="liquidity",
        train_bps=None, train_p=None,
        validate_bps=None, validate_p=None,
        lifecycle=Lifecycle.SHADOW,
        bootstrap_weight=0.0,
        note=("CONTEXT lens — emits NEUTRAL by design, so the directional "
              "harness scores it zero and that zero means nothing. Measured "
              "instead as a gate on the lead lens, and the splits CONTRADICT: "
              "TRAIN says the middle half is best (+2.05) while VALIDATE says "
              "the top quartile is (+3.27, middle +1.34). Its own score "
              "distribution also drifted (TRAIN top quartile >0.28 vs VALIDATE "
              ">0.51), so even percentile thresholds fitted on TRAIN do not "
              "transfer. No usable signal on this archive."),
    ),
    "composite_profile": Measurement(
        lens="composite_profile",
        train_bps=-1.93, train_p=0.0074,
        validate_bps=-0.78, validate_p=0.5729,
        lifecycle=Lifecycle.SHADOW,
        bootstrap_weight=0.0,
        note=("7-session composite POC/VAH/VAL plus naked POCs. Negative on "
              "TRAIN, near-zero on VALIDATE, signs disagree, on a real sample "
              "(n=1394/604 with only 24 abstentions). Correlates -0.395 with "
              "volume_oi -- related but not a duplicate, so this is a genuine "
              "independent read that simply does not predict. The mean-reversion "
              "convention was declared before measuring and is NOT to be "
              "flipped now."),
    ),
    "gamma_exposure": Measurement(
        lens="gamma_exposure",
        train_bps=None, train_p=None,
        validate_bps=None, validate_p=None,
        lifecycle=Lifecycle.SHADOW,
        bootstrap_weight=0.0,
        note=("NOT VALIDLY MEASURABLE on this data. Two implementations were "
              "degenerate in OPPOSITE directions -- v1 returned LONG on 97.8% "
              "of verdicts, v2 SHORT on 100% -- which is a constant, not a "
              "signal. Cause is the +/-10 strike window (21 strikes, ~3.6% of "
              "spot): a zero-gamma level is anchored by OI in the far wings, "
              "which the window excludes, so any crossing inside it reflects "
              "where the window was cut. Needs a wider chain, not a third "
              "formula. Its measured +0.26/+0.30 bps is discarded as noise "
              "rather than recorded as a weak positive."),
    ),
    "candle_flow": Measurement(
        lens="candle_flow",
        train_bps=+0.09, train_p=0.9337,
        validate_bps=-1.10, validate_p=0.5095,
        lifecycle=Lifecycle.SHADOW,
        bootstrap_weight=0.0,
        note=("wick rejection, close location, effort-vs-result. NIFTY: nothing, "
              "signs disagree. Measured on THREE further datasets and the "
              "picture is consistent only in being unusable -- ETHUSD "
              "-4.52/-3.30 and BTCUSD -1.58/-1.34 (both negative, signs "
              "agreeing), XAUTUSD -0.45/+0.67 and NIFTY +0.09/-1.10 (both "
              "disagreeing). The two liquid crypto symbols suggested the "
              "INVERSE convention might carry something; NIFTY and XAUT were "
              "then used as datasets that had NOT formed that hypothesis, and "
              "neither supports it. Closed rather than flipped. Note NIFTY "
              "replay reports has_volume on 4826/4826 because replay.py sums "
              "OPTION volume into index bars -- live it would run on wick "
              "geometry alone, so even this null overstates the live lens."),
    ),
    "extension": Measurement(
        lens="extension",
        train_bps=None, train_p=None,
        validate_bps=None, validate_p=None,
        lifecycle=Lifecycle.SHADOW,
        bootstrap_weight=0.0,
        note=("CONTEXT lens -- always NEUTRAL, so the directional harness "
              "correctly reports 'insufficient data'. Measured as a GATE on "
              "volume_oi instead, and the result INVERTED the hypothesis it was "
              "built for: entering when the move is already extended scored "
              "+2.04/+2.00 against an ungated +1.66/+1.49. volume_oi is a "
              "fader, so an extended move is what it trades. Neither bucket "
              "cleared the random-subset control (P=0.10/0.15), so this is a "
              "reason NOT to gate on extension rather than a gate to ship."),
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
