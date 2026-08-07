"""Trade a lens as an actual option, on recorded premiums, and see what is left.

THIS IS THE STEP THAT HAS KILLED EVERY STRATEGY IN THIS REPO.

    Breakout-retest (index)    +165 / +259 / +320 pts, positive in all three splits
    Breakout-retest (options)  VALIDATE  -Rs 80,769, not viable

Same signal. The index version cleared every hold-out; the option version lost
money. Theta, the strike-selection rule, the spread and a flat Rs 20 per order
consumed an edge that was genuinely there in the underlying. An index edge is a
hypothesis about direction; an option P&L is the only thing that pays.

So this harness deliberately adds NO new freedom over the entry test. The exit
horizon is the SAME horizon the entry was measured at. Sweeping exits here would
let a dead entry be resurrected by a lucky exit rule, which is precisely how you
end up with 23 hypotheses and one that clears p<0.05 by chance
(RESEARCH_LEARNINGS 2.1 and 2.2).

RULES ENFORCED

  * Fill at the bar CLOSE, never the bar label (learnings 1.2, worth Rs 384k).
  * A no-trade minute is not fillable — O=H=L=C with zero volume repeats the
    last print and did not trade (learnings section 4).
  * If the strike VANISHES mid-trade because the recorded ladder re-centred,
    the position is closed at its last observed price and flagged. It is never
    silently dropped: the ladder re-centres precisely when the index moves, so
    dropping those trades discards the losing ones (commit da08fff).
  * Costs are date-aware (STT stepped twice inside this window) and the spread
    is SWEPT, because no bid/ask exists anywhere in this dataset.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from nse.backtest.costs import round_trip_cost
from nse.backtest.replay import (
    available_sessions, snapshots_for_day, split_of, tradeable,
)
from nse.config import LOT_SIZES, PER_TRADE_CAPITAL_INR
from nse.lenses.base import Direction

logger = logging.getLogger(__name__)

SPREAD_GRID = (0.03, 0.10, 0.20, 0.30, 0.50, 0.70, 0.90)


@dataclass
class OptionTrade:
    session: date
    split: str
    entry_ts: object
    exit_ts: object
    direction: int
    option_type: str          # CE for a long call, PE for a long put
    strike: int
    lots: int
    qty: int
    entry_premium: float
    exit_premium: float
    spot_entry: float
    spot_exit: float
    confidence: float
    exit_reason: str
    strike_vanished: bool = False

    @property
    def gross(self) -> float:
        """Long option: profit is the premium gained, times contract quantity."""
        return (self.exit_premium - self.entry_premium) * self.qty

    def net(self, half_spread_pct: float) -> float:
        cost = round_trip_cost(self.entry_premium, self.exit_premium, self.qty,
                               "BUY", self.session, half_spread_pct)
        return self.gross - cost


@dataclass
class OptionResult:
    split: str
    n: int
    gross: float
    net: float
    win_rate: float
    avg_win: float
    avg_loss: float
    expectancy: float
    worst: float
    best: float
    vanished: int
    half_spread_pct: float

    def line(self) -> str:
        return (f"  {self.split:<9} n={self.n:>4}  WR {self.win_rate:>5.1%}  "
                f"gross {self.gross:>+10,.0f}  net {self.net:>+10,.0f}  "
                f"exp/trade {self.expectancy:>+8,.0f}  worst {self.worst:>+9,.0f}")


def _pick_strike(snap, direction: int, mode: str,
                 target_premium: float) -> Optional[tuple[int, str]]:
    """Choose the contract to buy.

    `atm`            the ATM strike. Most liquid, tightest spread, but also the
                     most theta per rupee of premium.
    `premium_band`   nearest to `target_premium`. This is what the repo's
                     earlier options work used, and it is worth knowing that it
                     does NOT select ATM: a Rs 180-200 band picked in-the-money
                     strikes whose realised delta measured 0.80 median, not the
                     0.5 that was assumed (learnings 1.7).
    """
    opt = "CE" if direction > 0 else "PE"
    if mode == "atm":
        return (snap.atm, opt) if tradeable(snap, snap.atm, opt) else None

    side = snap.side(opt)
    if side.empty:
        return None
    best, best_gap = None, None
    for _, row in side.iterrows():
        strike = int(row["strike"])
        px = tradeable(snap, strike, opt)
        if px is None:
            continue
        gap = abs(px - target_premium)
        if best_gap is None or gap < best_gap:
            best, best_gap = strike, gap
    return (best, opt) if best is not None else None


def run(lens, sessions: Sequence[date], *,
        symbol: str = "NIFTY",
        every_minutes: int = 30,
        hold_minutes: int = 60,
        strikes_around: int = 10,
        min_confidence: float = 0.0,
        strike_mode: str = "atm",
        target_premium: float = 150.0,
        lots: int = 1,
        one_at_a_time: bool = True,
        progress_every: int = 80) -> list[OptionTrade]:
    """Replay a lens and buy the option it points at.

    `hold_minutes` must match the horizon the entry was measured at. It is a
    parameter only so the two can be kept in step, not so it can be tuned.

    `one_at_a_time` mirrors a real book with a single unit of capital: a new
    signal is ignored while a position is open. Without it the same move gets
    counted several times over and the trade count flatters the result.
    """
    lot = LOT_SIZES.get(symbol, 65)
    qty = lots * lot
    trades: list[OptionTrade] = []

    for i, session in enumerate(sessions, 1):
        sp = split_of(session)
        if sp is None:
            continue
        frames = list(snapshots_for_day(session, symbol=symbol,
                                        every_minutes=every_minutes,
                                        strikes_around=strikes_around))
        if not frames:
            continue

        open_until = None
        for idx, (snap, _missing) in enumerate(frames):
            if one_at_a_time and open_until is not None and snap.ts < open_until:
                continue

            v = lens.safe_evaluate(snap)
            if v.abstained or v.direction == Direction.NEUTRAL:
                continue
            if v.confidence < min_confidence:
                continue

            pick = _pick_strike(snap, int(v.direction), strike_mode, target_premium)
            if pick is None:
                continue
            strike, opt = pick
            entry_px = tradeable(snap, strike, opt)
            if entry_px is None or entry_px <= 0:
                continue

            exit_snap, exit_px, reason, vanished = _resolve_exit(
                frames, idx, strike, opt, hold_minutes)
            if exit_px is None:
                continue

            trades.append(OptionTrade(
                session=session, split=sp,
                entry_ts=snap.ts, exit_ts=exit_snap.ts,
                direction=int(v.direction), option_type=opt, strike=strike,
                lots=lots, qty=qty,
                entry_premium=entry_px, exit_premium=exit_px,
                spot_entry=snap.spot, spot_exit=exit_snap.spot,
                confidence=v.confidence, exit_reason=reason,
                strike_vanished=vanished,
            ))
            open_until = exit_snap.ts

        if progress_every and i % progress_every == 0:
            logger.info("options replay %s: %d/%d sessions, %d trades",
                        getattr(lens, "name", "?"), i, len(sessions), len(trades))
    return trades


def _resolve_exit(frames, idx: int, strike: int, opt: str, hold_minutes: int):
    """Walk forward to the exit bar. Returns (snapshot, premium, reason, vanished).

    Three ways out, in priority order: the hold horizon elapses, the session
    ends, or the contract stops being quoted. That last one is real and is
    recorded rather than hidden — the strike goes missing exactly when the
    index has moved far enough to re-centre the ladder, so treating it as a
    non-event would quietly delete the trades that hurt.
    """
    t0 = frames[idx][0].ts
    last_snap, last_px = None, None

    for j in range(idx + 1, len(frames)):
        snap = frames[j][0]
        px = tradeable(snap, strike, opt)
        if px is not None:
            last_snap, last_px = snap, px
        elapsed = (snap.ts - t0).total_seconds() / 60.0
        if elapsed >= hold_minutes:
            if px is not None:
                return snap, px, "horizon", False
            if last_px is not None:
                return last_snap, last_px, "vanished_at_horizon", True
            return None, None, "no_exit", True

    # Session ended before the horizon.
    if last_px is not None:
        return last_snap, last_px, "session_end", False
    return None, None, "no_exit", True


def summarise(trades: Iterable[OptionTrade], split: Optional[str],
              half_spread_pct: float) -> Optional[OptionResult]:
    rows = [t for t in trades if split is None or t.split == split]
    if not rows:
        return None
    nets = [t.net(half_spread_pct) for t in rows]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    n = len(nets)
    return OptionResult(
        split=split or "ALL", n=n,
        gross=sum(t.gross for t in rows),
        net=sum(nets),
        win_rate=len(wins) / n,
        avg_win=(sum(wins) / len(wins)) if wins else 0.0,
        avg_loss=(sum(losses) / len(losses)) if losses else 0.0,
        expectancy=sum(nets) / n,
        worst=min(nets), best=max(nets),
        vanished=sum(1 for t in rows if t.strike_vanished),
        half_spread_pct=half_spread_pct,
    )


def report(lens, trades: list[OptionTrade], *, grid: Sequence[float] = SPREAD_GRID,
           label: str = "") -> dict:
    """Per-split P&L across the whole plausible spread band.

    The band is reported rather than a single number because no bid/ask exists
    in this data and estimation spans 0.03% to 0.9% — a 30x range. Picking one
    value and calling it the answer would be a guess wearing a decimal point
    (learnings 2.3).
    """
    name = getattr(lens, "name", "?")
    print(f"\n{'=' * 104}")
    print(f"OPTIONS P&L: {name} {label}   trades={len(trades)}")
    v = sum(1 for t in trades if t.strike_vanished)
    if v:
        print(f"  {v} trade(s) exited on a vanished strike — counted, not dropped")
    print(f"{'=' * 104}")

    out: dict = {"lens": name, "label": label, "n_trades": len(trades), "by_spread": {}}
    for hs in grid:
        print(f"\n  half-spread {hs:.2f}%")
        for sp in ("TRAIN", "VALIDATE"):
            r = summarise(trades, sp, hs)
            if r is None:
                print(f"  {sp:<9} no trades")
                continue
            print(r.line())
            out["by_spread"].setdefault(hs, {})[sp] = {
                "n": r.n, "net": round(r.net, 0),
                "expectancy": round(r.expectancy, 1),
                "win_rate": round(r.win_rate, 4), "worst": round(r.worst, 0),
            }

    # The headline: the widest spread at which BOTH splits still make money.
    survives = None
    for hs in grid:
        tr = summarise(trades, "TRAIN", hs)
        va = summarise(trades, "VALIDATE", hs)
        if tr and va and tr.net > 0 and va.net > 0:
            survives = hs
        else:
            break
    print()
    print(f"  VERDICT: profitable in BOTH splits up to a half-spread of "
          f"{f'{survives:.2f}%' if survives else 'NOTHING — loses even at the tick floor'}")
    out["survives_to_half_spread"] = survives
    return out
