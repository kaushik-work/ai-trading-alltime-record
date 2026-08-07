"""Replay a lens over history and measure whether its ENTRY has an edge.

The order of operations here is the point, and it is the one methodology in
this repo that has actually worked:

    MEASURE THE ENTRY BEFORE TUNING THE EXIT.

No exit rule can harvest an edge the entry does not have. The primary output is
therefore not a P&L curve — it is the lens's signed forward return against a
baseline matched on its own long/short mix, with a t-statistic. That test is
cheap enough to run across dozens of hypotheses, and it is what revealed that
BTC's real 9-12 bps edge sat structurally below its ~14 bps cost floor.
See RESEARCH_LEARNINGS section 2.2.

Why a MIX-MATCHED baseline rather than zero: a lens that happens to be long 80%
of the time in a rising market will show a positive raw forward return that is
pure beta. The baseline reproduces the lens's own directional mix over the same
bars, so what survives is the timing, not the drift.

Costs are applied through nse/backtest/costs.py, which is date-aware because
STT stepped up twice inside this window. Spread is SWEPT rather than assumed:
no bid/ask exists anywhere in this dataset, and estimation leaves a 30x band
(0.03% tick floor to 0.9% Corwin-Schultz). A wrong-but-conservative figure
kills viable strategies just as unscientifically as a wrong-but-optimistic one
flatters dead ones, so the harness reports the BREAK-EVEN spread instead.
See RESEARCH_LEARNINGS section 2.3.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional, Sequence

from nse.backtest.replay import (
    SPLITS, available_sessions, snapshots_for_day, split_of, tradeable,
)
from nse.config import LOT_SIZES
from nse.lenses.base import Direction, LensVerdict

logger = logging.getLogger(__name__)

# Spread grid for the break-even sweep, as HALF-spread % of premium.
# 0.03 is one tick at ATM; 0.90 is the Corwin-Schultz upper estimate. The truth
# is somewhere inside, and which end it sits at decides several strategies.
SPREAD_GRID = (0.03, 0.10, 0.20, 0.30, 0.50, 0.70, 0.90)


@dataclass
class Observation:
    """One lens verdict, plus what the market did next."""

    ts: object
    session: date
    split: str
    spot: float
    atm: int
    dte: float
    direction: int
    confidence: float
    fwd_return_bps: Optional[float] = None    # signed by the lens's direction
    raw_move_bps: Optional[float] = None      # unsigned index move
    abstained: bool = False
    missing_strikes: int = 0
    features: dict = field(default_factory=dict)


@dataclass
class EntryEdge:
    """Does this lens's entry point anywhere useful?"""

    split: str
    n: int
    n_abstained: int
    long_frac: float
    mean_bps: float           # lens's signed forward return
    baseline_bps: float       # same directional mix, timing removed
    edge_bps: float           # mean - baseline. THE number.
    t_stat: float
    p_value: float
    sd_bps: float

    @property
    def significant(self) -> bool:
        return self.p_value < 0.05 and self.n >= 30

    def line(self) -> str:
        star = "*" if self.significant else " "
        return (f"  {self.split:<9} n={self.n:>5}  long={self.long_frac:>5.1%}  "
                f"mean={self.mean_bps:>+7.2f}  base={self.baseline_bps:>+7.2f}  "
                f"edge={self.edge_bps:>+7.2f}bps  t={self.t_stat:>+6.2f}  "
                f"p={self.p_value:.4f} {star}")


def replay_lens(lens, sessions: Sequence[date], *,
                symbol: str = "NIFTY",
                every_minutes: int = 5,
                horizon_minutes: int = 30,
                strikes_around: int = 10,
                progress_every: int = 50) -> list[Observation]:
    """Run a lens across sessions and record what happened next.

    Forward return is measured on the INDEX, not on an option. That is
    deliberate for an entry test: option P&L confounds direction with theta,
    vega and the strike-selection rule, and we are asking one question here —
    does this lens know which way the index goes?
    """
    out: list[Observation] = []
    for i, session in enumerate(sessions, 1):
        sp = split_of(session)
        if sp is None:
            continue

        # Materialise the day once; the forward look needs later snapshots.
        frames = list(snapshots_for_day(session, symbol=symbol,
                                        every_minutes=every_minutes,
                                        strikes_around=strikes_around))
        if not frames:
            continue

        spots = [(s.ts, s.spot) for s, _ in frames]
        for idx, (snap, missing) in enumerate(frames):
            verdict = lens.safe_evaluate(snap)
            obs = Observation(
                ts=snap.ts, session=session, split=sp, spot=snap.spot,
                atm=snap.atm, dte=snap.dte,
                direction=int(verdict.direction),
                confidence=verdict.confidence,
                abstained=verdict.abstained,
                missing_strikes=missing,
                features=verdict.features,
            )
            if not verdict.abstained:
                fwd = _forward_spot(spots, idx, horizon_minutes)
                if fwd is not None and snap.spot > 0:
                    raw = (fwd - snap.spot) / snap.spot * 10_000
                    obs.raw_move_bps = raw
                    obs.fwd_return_bps = raw * int(verdict.direction)
            out.append(obs)

        if progress_every and i % progress_every == 0:
            logger.info("replay %s: %d/%d sessions, %d observations",
                        getattr(lens, "name", "?"), i, len(sessions), len(out))
    return out


def _forward_spot(spots: list[tuple], idx: int, horizon_minutes: int) -> Optional[float]:
    """Index level `horizon_minutes` after the decision bar.

    Walks forward within the SAME session only. Returning None at the tail
    rather than clamping to the close matters: clamping would silently convert
    a 30-minute horizon into a 3-minute one for late-session signals, and those
    are exactly the bars where an intraday rule looks best.
    """
    t0 = spots[idx][0]
    for j in range(idx + 1, len(spots)):
        if (spots[j][0] - t0).total_seconds() >= horizon_minutes * 60:
            return spots[j][1]
    return None


def measure_entry(observations: Iterable[Observation],
                  split: Optional[str] = None) -> Optional[EntryEdge]:
    """Signed forward return against a mix-matched baseline."""
    rows = [o for o in observations
            if not o.abstained
            and o.fwd_return_bps is not None
            and o.direction != 0
            and (split is None or o.split == split)]
    n_abs = sum(1 for o in observations
                if o.abstained and (split is None or o.split == split))
    if len(rows) < 2:
        return None

    signed = [o.fwd_return_bps for o in rows]
    raw = [o.raw_move_bps for o in rows]
    n = len(signed)
    long_frac = sum(1 for o in rows if o.direction > 0) / n

    mean = sum(signed) / n
    # Baseline: the SAME long/short mix applied blind to the same bars. What a
    # coin weighted like this lens would have earned without timing anything.
    baseline = (2 * long_frac - 1) * (sum(raw) / n)
    edge = mean - baseline

    var = sum((x - mean) ** 2 for x in signed) / (n - 1)
    sd = math.sqrt(var) if var > 0 else 0.0
    t = edge / (sd / math.sqrt(n)) if sd > 0 else 0.0

    return EntryEdge(
        split=split or "ALL", n=n, n_abstained=n_abs, long_frac=long_frac,
        mean_bps=mean, baseline_bps=baseline, edge_bps=edge,
        t_stat=t, p_value=_two_sided_p(t, n - 1), sd_bps=sd,
    )


def _two_sided_p(t: float, dof: int) -> float:
    if dof <= 0 or t == 0 or not math.isfinite(t):
        return 1.0
    try:
        from scipy import stats
        return float(2 * (1 - stats.t.cdf(abs(t), dof)))
    except Exception:
        # Normal approximation; fine at the sample sizes here.
        return float(math.erfc(abs(t) / math.sqrt(2)))


def breakeven_spread(edge_bps: float, premium: float, lots: int = 1,
                     symbol: str = "NIFTY",
                     trade_date: Optional[date] = None,
                     grid: Sequence[float] = SPREAD_GRID) -> Optional[float]:
    """Largest half-spread at which this edge still clears its costs.

    Returns None when even the tick floor eats it — which is the honest answer
    for most option strategies and the one worth reporting loudly.
    """
    from nse.backtest.costs import round_trip_cost

    qty = lots * LOT_SIZES.get(symbol, 65)
    notional = premium * qty
    if notional <= 0:
        return None
    gross = notional * (edge_bps / 10_000.0)
    td = trade_date or date.today()

    survived = None
    for hs in grid:
        cost = round_trip_cost(premium, premium, qty, "BUY", td, hs)
        if gross > cost:
            survived = hs
        else:
            break
    return survived


def report(lens, observations: list[Observation], *,
           premium: float = 150.0, lots: int = 1) -> dict:
    """Per-split entry measurement plus the break-even spread. Prints and returns.

    TEST is measured but should be READ ONCE. If you are iterating, look at
    TRAIN and VALIDATE only — a hold-out you consult repeatedly is just another
    training set with extra steps.
    """
    name = getattr(lens, "name", "?")
    print(f"\n{'=' * 96}")
    print(f"LENS: {name}   observations={len(observations)}   "
          f"abstained={sum(1 for o in observations if o.abstained)}")
    missing = sum(o.missing_strikes for o in observations)
    if missing:
        print(f"  NOTE: {missing:,} strike-slots absent across the run — the ladder "
              f"re-centres intraday. Counted, not dropped.")
    print(f"{'=' * 96}")

    out: dict = {"lens": name, "splits": {}}
    for sp in ("TRAIN", "VALIDATE", "TEST"):
        edge = measure_entry(observations, sp)
        if edge is None:
            print(f"  {sp:<9} insufficient data")
            continue
        print(edge.line())
        be = breakeven_spread(edge.edge_bps, premium, lots)
        out["splits"][sp] = {
            "n": edge.n, "edge_bps": round(edge.edge_bps, 3),
            "t": round(edge.t_stat, 3), "p": round(edge.p_value, 5),
            "significant": edge.significant, "breakeven_half_spread_pct": be,
        }

    tr = out["splits"].get("TRAIN", {})
    va = out["splits"].get("VALIDATE", {})
    consistent = (tr.get("edge_bps", 0) > 0 and va.get("edge_bps", 0) > 0)
    print()
    print(f"  TRAIN/VALIDATE sign agreement: {'YES' if consistent else 'NO'}")
    be = tr.get("breakeven_half_spread_pct")
    print(f"  break-even half-spread on a Rs {premium:.0f} premium, {lots} lot: "
          f"{f'{be:.2f}%' if be else 'NONE — costs eat the edge at the tick floor'}")
    out["train_valid_agree"] = consistent
    return out


def quick(lens, n_sessions: int = 40, split: str = "TRAIN", **kw) -> dict:
    """Smallest useful run: a lens against a slice of TRAIN."""
    sessions = available_sessions(limit=n_sessions, split=split)
    obs = replay_lens(lens, sessions, **kw)
    return report(lens, obs)
