"""NSE option execution costs — the thing that decides whether a strategy lives.

Every component here is date-aware, because a 5-year backtest spans two STT
increases. Applying today's rate to 2021 (or 2021's rate to today) silently
shifts every result.

Charges modelled, per leg:
  brokerage        flat per executed order (discount-broker standard)
  STT              on the SELL side only, on premium. Rate changed twice.
  exchange txn     on premium, both sides
  SEBI turnover    on premium, both sides
  GST 18%          on (brokerage + exchange txn + SEBI). NOT on STT or stamp.
  stamp duty       on the BUY side only, on premium
  spread           a PARAMETER, not a constant - see below

Why spread is a parameter:
  No bid/ask exists in any source we hold (the collector wrote zeros for
  months; the 5-year CSV has no quote columns). Estimation from OHLC leaves a
  band from ~0.03% (one tick at ATM) to ~0.9% (Corwin-Schultz) - a 30x range.
  Rather than pick a number and pretend, the harness sweeps it and reports the
  BREAK-EVEN spread per strategy.

VERIFY these against a real Angel One contract note before trusting a live
P&L projection. Rates are taken from public schedules, and brokers differ.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# ── Rates ────────────────────────────────────────────────────────────────────
BROKERAGE_PER_ORDER = 20.0        # Rs, flat per executed order
EXCHANGE_TXN_PCT    = 0.03503     # % of premium turnover, NSE options, both sides
SEBI_TURNOVER_PCT   = 0.0001      # % of premium turnover (Rs 10 per crore)
GST_PCT             = 18.0        # % on brokerage + exchange txn + SEBI
STAMP_DUTY_PCT      = 0.003       # % of premium, BUY side only

# STT on option premium, SELL side only. Two step-ups inside our data window.
_STT_SCHEDULE = [
    (date(2026, 4, 1), 0.15),     # Budget 2026
    (date(2024, 10, 1), 0.10),    # Budget 2024
    (date(1900, 1, 1), 0.0625),   # prior regime
]


def stt_pct_on(d: date) -> float:
    """STT rate (% of sell premium) applicable on a given trade date."""
    for start, rate in _STT_SCHEDULE:
        if d >= start:
            return rate
    return _STT_SCHEDULE[-1][1]


@dataclass
class LegCost:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    gst: float
    stamp: float
    spread: float

    @property
    def total(self) -> float:
        return (self.brokerage + self.stt + self.exchange
                + self.sebi + self.gst + self.stamp + self.spread)

    def breakdown(self) -> dict:
        return {"brokerage": self.brokerage, "stt": self.stt,
                "exchange": self.exchange, "sebi": self.sebi, "gst": self.gst,
                "stamp": self.stamp, "spread": self.spread, "total": self.total}


def leg_cost(premium: float, qty: int, side: str, trade_date: date,
             half_spread_pct: float) -> LegCost:
    """Cost in rupees for ONE option leg.

    premium          per-unit option price
    qty              contracts (lots x lot size)
    side             "BUY" or "SELL"
    half_spread_pct  half the bid-ask, as % of premium. This is the parameter
                     the harness sweeps.
    """
    turnover = abs(premium) * qty
    is_sell = side.upper().startswith("S")

    brokerage = BROKERAGE_PER_ORDER
    stt = turnover * stt_pct_on(trade_date) / 100 if is_sell else 0.0
    exch = turnover * EXCHANGE_TXN_PCT / 100
    sebi = turnover * SEBI_TURNOVER_PCT / 100
    gst = (brokerage + exch + sebi) * GST_PCT / 100
    stamp = 0.0 if is_sell else turnover * STAMP_DUTY_PCT / 100
    # Crossing the spread costs half of it, once, per leg.
    spread = turnover * half_spread_pct / 100
    return LegCost(brokerage, stt, exch, sebi, gst, stamp, spread)


def round_trip_cost(entry_premium: float, exit_premium: float, qty: int,
                    entry_side: str, trade_date: date,
                    half_spread_pct: float) -> float:
    """Total rupees to open AND close one leg."""
    exit_side = "SELL" if entry_side.upper().startswith("B") else "BUY"
    a = leg_cost(entry_premium, qty, entry_side, trade_date, half_spread_pct)
    b = leg_cost(exit_premium, qty, exit_side, trade_date, half_spread_pct)
    return a.total + b.total


def breakeven_move_pct(premium: float, qty: int, entry_side: str,
                       trade_date: date, half_spread_pct: float) -> float:
    """How far the premium must move, in %, just to cover costs.

    This is the number that kills most option strategies: on a cheap OTM
    contract the fixed brokerage alone can demand a several-percent move.
    """
    cost = round_trip_cost(premium, premium, qty, entry_side,
                           trade_date, half_spread_pct)
    notional = premium * qty
    return cost / notional * 100 if notional > 0 else float("inf")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    # Imported, never restated. A hardcoded 75 here contradicted nse/config.py's
    # 65 and put every rupee figure ~15% out — the same mistake recorded in
    # RESEARCH_LEARNINGS section 1.8, which is also why a single constant cannot
    # serve a five-year window: the NIFTY lot size changed inside it.
    from nse.config import LOT_SIZES
    LOT = LOT_SIZES["NIFTY"]

    print("=" * 92)
    print("NSE OPTION COST MODEL — break-even move required, by premium")
    print("=" * 92)
    print(f"  brokerage Rs {BROKERAGE_PER_ORDER:.0f}/order · exchange {EXCHANGE_TXN_PCT}% · "
          f"SEBI {SEBI_TURNOVER_PCT}% · GST {GST_PCT}% · stamp {STAMP_DUTY_PCT}% (buy)")
    print(f"  STT on sell premium: 0.0625% -> 0.10% (2024-10-01) -> 0.15% (2026-04-01)")
    print()
    for hs, lab in ((0.03, "tick floor"), (0.30, "mid"), (0.90, "Corwin-Schultz")):
        print(f"  half-spread {hs:.2f}%  ({lab})")
        print(f"    {'premium':>9}{'lots':>6}{'notional':>12}{'cost Rs':>10}"
              f"{'break-even move':>18}")
        for prem in (2, 10, 50, 100, 250):
            for lots in (1, 10):
                qty = lots * LOT
                c = round_trip_cost(prem, prem, qty, "BUY", date(2026, 8, 1), hs)
                be = breakeven_move_pct(prem, qty, "BUY", date(2026, 8, 1), hs)
                print(f"    {prem:>9.0f}{lots:>6}{prem * qty:>12,.0f}{c:>10,.0f}"
                      f"{be:>17.2f}%")
        print()

    print("=" * 92)
    print("Read this before designing any target: a 1-lot trade on a Rs 2 option needs")
    print("a double-digit % move just to break even, almost entirely from flat brokerage.")
    print("Cheap OTM options are a cost trap unless size is large.")
