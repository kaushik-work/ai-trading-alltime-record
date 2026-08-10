"""Liquidity lens — who is actually present, read from participation.

Every other lens reads what price DID. This one reads whether anyone was there
when it did it, from three columns no other lens touches:

    no_trade    the archive's own flag for a bar that printed without trading
    volume      per contract, so its CONCENTRATION across the chain is readable
    oi          alongside volume, to separate new positioning from churn

WHY THIS IS A DIFFERENT OPINION AND NOT A RESTATEMENT

`volume_oi` reads WHERE the volume and open interest sit — walls, value area,
build. This lens reads HOW MUCH there is and HOW BROADLY it is spread. A chain
can have a textbook wall structure on almost no participation, and that is
precisely the setup where the wall is decoration. Same raw columns, different
question — but "different question" is a claim, so the pairwise correlation with
volume_oi gets measured before both ever hold weight (section 3.12).

CONVENTION, DECLARED BEFORE MEASUREMENT

This lens does NOT predict direction. It reports whether the tape is worth
trading, so it emits NEUTRAL with a confidence that means "how present is the
market", and the council reads it as a CONTEXT lens.

That makes it the first lens whose value cannot show up in a directional entry
edge at all — the harness measures signed forward return, and a lens that never
signs anything scores exactly zero there. It is therefore measured differently:
by whether the LEAD lens does better on the bars this one calls liquid. If that
conditional edge is not there, this lens has no job.

Recorded up front because a zero in the standard harness would otherwise read as
failure when it is actually the lens working as designed.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from nse.lenses.base import BaseLens, Direction, LensVerdict, abstain
from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

MIN_CONTRACTS = 8

#: Fraction of the visible chain that must have actually traded. MEASURED on
#: TRAIN: p25 of 1,892 observations. The first version used an absolute 0.30,
#: which the archive clears at the 5th percentile (0.738) — so the lens rated
#: 99.7% of snapshots "liquid" and discriminated nothing.
#:
#: These are set at TRAIN PERCENTILES rather than absolute levels for the same
#: reason the regime signal in RESEARCH_LEARNINGS section 3.15 had to be: an
#: absolute cut means a different thing in every regime, and stops transferring
#: the moment participation shifts. A percentile keeps the same FRACTION of the
#: tape on either side by construction.
MIN_TRADED_FRACTION = 0.857

#: Herfindahl concentration above which volume is hiding in a handful of
#: strikes. MEASURED on TRAIN: p75 (0.071). The previous 0.25 sat above the 95th
#: percentile (0.113), so breadth was pinned at 1.0 on every snapshot.
#: High concentration is not automatically bad — expiry-day flow is legitimately
#: concentrated — so it lowers breadth rather than vetoing.
CONCENTRATED_HHI = 0.071


class LiquidityLens(BaseLens):
    name = "liquidity"
    backtestable = True

    def _evaluate(self, snap: MarketSnapshot) -> LensVerdict:
        chain = snap.chain
        if chain is None or len(chain) < MIN_CONTRACTS:
            return abstain(self.name,
                           f"only {0 if chain is None else len(chain)} contracts visible")

        n = len(chain)
        vol_col = pd.to_numeric(chain.get("volume"), errors="coerce").fillna(0.0)

        if "no_trade" in chain.columns:
            # Archive path: the loader computed this from O=H=L=C with zero
            # volume, which is stricter than volume alone.
            traded = ~chain["no_trade"].fillna(False).astype(bool)
        elif "volume" in chain.columns:
            # LIVE path. `no_trade` is an archive-only column — the Angel
            # SNAP_QUOTE feed has no equivalent — and this lens abstained on
            # 100% of live snapshots until that was noticed. Contracts with zero
            # volume are the live stand-in.
            #
            # The two are NOT the same measurement and the difference is one
            # direction only: the archive flag also catches bars that printed a
            # repeated price on nonzero volume, so live `traded_frac` reads
            # slightly HIGHER than replay on identical tape. Stated because a
            # lens whose scale silently shifts between backtest and production
            # is a lens whose measured thresholds do not transfer — and this
            # one's thresholds are TRAIN percentiles.
            traded = vol_col > 0
        else:
            # Never assume everything traded. That assumption is what makes a
            # backtest fillable everywhere.
            return abstain(self.name, "chain carries neither no_trade nor volume")

        traded_frac = float(traded.mean())

        vol = vol_col
        total_vol = float(vol.sum())
        if total_vol <= 0:
            return LensVerdict(
                lens=self.name, direction=Direction.NEUTRAL, confidence=0.0,
                rationale="zero volume across the visible chain — nobody is here",
                features={"traded_fraction": round(traded_frac, 4),
                          "total_volume": 0.0, "hhi": 1.0, "n_contracts": n,
                          "dte": round(snap.dte, 3)})

        share = (vol / total_vol).to_numpy(float)
        hhi = float(np.sum(share ** 2))          # 1/n = perfectly spread, 1 = one strike

        breadth = float(np.clip((CONCENTRATED_HHI - hhi) / CONCENTRATED_HHI, 0.0, 1.0))
        presence = float(np.clip(
            (traded_frac - MIN_TRADED_FRACTION) / (1.0 - MIN_TRADED_FRACTION),
            0.0, 1.0))

        # Both must hold. A chain can be broadly quoted and dead, or busy in one
        # strike and useless for anything else — the geometric mean punishes
        # either failing in a way an average would paper over.
        score = float(np.sqrt(max(presence, 0.0) * max(breadth, 0.0)))

        state = ("liquid" if score >= 0.5 else
                 "thin" if score >= 0.2 else "illiquid")
        return LensVerdict(
            lens=self.name,
            direction=Direction.NEUTRAL,        # context only — see the docstring
            confidence=score,
            rationale=(f"{state}: {traded_frac:.0%} of {n} contracts traded, "
                       f"volume HHI {hhi:.3f} ({'concentrated' if hhi > CONCENTRATED_HHI else 'spread'})"),
            features={
                "traded_fraction": round(traded_frac, 4),
                "total_volume": round(total_vol, 2),
                "hhi": round(hhi, 5),
                "presence": round(presence, 4),
                "breadth": round(breadth, 4),
                "score": round(score, 4),
                "state": state,
                "n_contracts": n,
                "n_no_trade": int((~traded).sum()),
                "dte": round(snap.dte, 3),
                "spot": round(snap.spot, 2),
            },
        )

    def _deliberate(self, snap: MarketSnapshot, own: LensVerdict,
                    peers: dict, journal) -> LensVerdict:
        """This lens never revises, and that is deliberate.

        It reports how present the market is. That is a fact about the tape, not
        an opinion about direction, and no amount of disagreement from a
        directional lens makes the chain busier or thinner than it actually was.
        A context lens that let itself be talked out of its reading would stop
        being context.

        It never defers either: the others consume its reading during their own
        round 1, so standing down would remove the input they are reacting to.
        """
        return own


