"""Specialist lenses — independent perspectives on one shared snapshot.

A lens is not a strategy. Every lens reads the SAME MarketSnapshot and reports
what it sees through its own perspective: volume profile, VWAP, Greeks, order-
flow structure. The aggregator turns those readings into one decision.

No lens may read another lens's verdict. That independence is enforced by the
signature — `evaluate(snapshot) -> LensVerdict` has nowhere to put another
lens's opinion — which is what keeps each one separately backtestable instead
of compounding into one unauditable black box.
"""

from nse.lenses.base import (
    BaseLens,
    Direction,
    Lens,
    LensVerdict,
    abstain,
)
from nse.lenses.composite_profile import CompositeProfileLens
from nse.lenses.gamma_exposure import GammaExposureLens
from nse.lenses.greeks import GreeksLens
from nse.lenses.ict_smc import ICTSMCLens
from nse.lenses.liquidity import LiquidityLens
from nse.lenses.momentum import MomentumLens
from nse.lenses.smile import SmileLens
from nse.lenses.volume_oi import VolumeOILens
from nse.lenses.vwap import VWAPLens
from nse.lenses.vision import VisionLens

#: The full roster. Each carries its OWN brain (nse/brain.py) — its own weight,
#: lifecycle, attribution history and health, keyed by `name` in Mongo. One
#: lens being suspended never touches another.
#:
#: Being on the roster earns a lens nothing. It votes at weight 0 until measured
#: attribution promotes it, so adding one here is free and is meant to be — the
#: cost of a bad idea should be a journal entry, not a loss.
ROSTER = [GreeksLens, VolumeOILens, VWAPLens, ICTSMCLens,
          SmileLens, MomentumLens, LiquidityLens,
          CompositeProfileLens, GammaExposureLens, VisionLens]

#: Lenses that report CONTEXT rather than direction. They emit NEUTRAL by
#: design, so the directional entry harness scores them at exactly zero and that
#: zero means nothing. They are measured by whether the lead lens does better on
#: the bars they favour — see nse/lenses/liquidity.py.
CONTEXT_LENSES = {"liquidity"}

__all__ = ["BaseLens", "Direction", "Lens", "LensVerdict", "abstain",
           "GreeksLens", "VolumeOILens", "VWAPLens", "ICTSMCLens",
           "SmileLens", "MomentumLens", "LiquidityLens",
           "CompositeProfileLens", "GammaExposureLens", "VisionLens",
           "ROSTER", "CONTEXT_LENSES"]
