"""Breakout+retest executed as OPTIONS, with a Rs 50k budget and a trade ledger.

The signal is the index breakout-retest that survived the hold-out
(test_breakout_retest.py). This layer answers the question that actually
matters: what happens to the money when you express that view by BUYING an
option instead of the index.

Rules
  strike        the contract whose premium sits in PREMIUM_BAND at entry.
                CE for a long signal, PE for a short signal.
  size          floor(BUDGET / (premium x lot)) lots, capped by budget.
  entry/exit    entry at the option's price when the index signal fires;
                exit at the option's price in the minute the INDEX hits its
                stop or target, or at square-off.
  P&L           (exit premium - entry premium) x lots x lot size.

Why the exit is driven by the index, not the option: the thesis is
directional. The stop belongs where the thesis is wrong, which is an index
level. An option-premium stop would fire on vol collapse while the index
thesis is still intact.

Costs are EXCLUDED by instruction — this is gross. The cost model in
nse/backtest/costs.py gives the deduction when you want it.

Usage:
    python -m nse.backtest.breakout_options_ledger
    python -m nse.backtest.breakout_options_ledger --years 2025 2026 --show 40
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from nse.backtest.nifty_loader import DEFAULT_ROOT, clean_day, load_spot
from nse.backtest.test_breakout_retest import prepare, ENTRY_START, ENTRY_END, SQUARE_OFF
from nse.config import LOT_SIZES

# pandas resample labels a bar by its START. A bar labelled 09:30 covers
# 09:30:00-09:34:59, and the signal is only confirmed at its CLOSE. Reading the
# option price at the bar's label is therefore 5 minutes of LOOKAHEAD - it was
# letting trades exit at a price from BEFORE the adverse move that stopped
# them, which is how 138 stops produced 106 winners. Both entry and exit fills
# are taken at bar close.
BAR = pd.Timedelta(minutes=5)

BUDGET = 50_000.0
PREMIUM_LO, PREMIUM_HI = 180.0, 200.0
LOT = LOT_SIZES.get("NIFTY", 65)   # from nse/config.py, not hardcoded
SL_PTS, RR = 20.0, 3.0        # the config that held up across all three periods


def index_signals(bars: pd.DataFrame, sl_pts: float, rr: float,
                  vol_mult: float = 1.2, max_trades: int = 2) -> pd.DataFrame:
    """Entry/exit MINUTES and levels from the index rule, no option layer yet."""
    out: list[dict] = []
    for date, d in bars.groupby("date", sort=True):
        d = d.reset_index(drop=True)
        pos, pending, taken = None, None, 0
        for i in range(len(d)):
            row = d.iloc[i]
            t = row["hhmm"]
            if pos is not None:
                hi, lo = row["high"], row["low"]
                res = None
                if pos["dir"] > 0:
                    if lo <= pos["sl"]:   res = (pos["sl"], "stop")
                    elif hi >= pos["tp"]: res = (pos["tp"], "target")
                else:
                    if hi >= pos["sl"]:   res = (pos["sl"], "stop")
                    elif lo <= pos["tp"]: res = (pos["tp"], "target")
                if res is None and t >= SQUARE_OFF:
                    res = (row["close"], "eod")
                if res:
                    out.append({**pos, "exit_dt": row["datetime"],
                                "exit_idx": res[0], "reason": res[1]})
                    pos = None
                continue
            if taken >= max_trades or t >= SQUARE_OFF or not (ENTRY_START <= t <= ENTRY_END):
                continue
            if not np.isfinite(row["hh"]) or not np.isfinite(row["ll"]):
                continue
            vol_ok = row["volume"] >= vol_mult * (row["vol_ma"] or 0)
            if pending is None:
                if row["close"] > row["hh"] and vol_ok:
                    pending = {"dir": 1, "level": row["hh"], "bar": i}
                elif row["close"] < row["ll"] and vol_ok:
                    pending = {"dir": -1, "level": row["ll"], "bar": i}
                continue
            if i - pending["bar"] > 6:
                pending = None
                continue
            dirn, lvl = pending["dir"], pending["level"]
            retested = row["low"] <= lvl <= row["high"]
            held = (row["close"] > lvl) if dirn > 0 else (row["close"] < lvl)
            aligned = ((row["close"] > row["ema20"] and row["close"] > row["vwap"])
                       if dirn > 0 else
                       (row["close"] < row["ema20"] and row["close"] < row["vwap"]))
            if retested and held and aligned:
                e = row["close"]
                pos = {"date": date, "dir": dirn, "entry_dt": row["datetime"],
                       "entry_idx": e, "sl": e - dirn * sl_pts,
                       "tp": e + dirn * sl_pts * rr}
                taken += 1
                pending = None
        if pos is not None:
            out.append({**pos, "exit_dt": d.iloc[-1]["datetime"],
                        "exit_idx": d.iloc[-1]["close"], "reason": "eod"})
    return pd.DataFrame(out)


def option_chain_for(day) -> pd.DataFrame | None:
    hits = list(DEFAULT_ROOT.glob(f"*/NIFTY_{day}_1m.csv"))
    if not hits:
        return None
    try:
        d = clean_day(pd.read_csv(hits[0], usecols=[
            "datetime", "option_type", "close", "volume", "strike_price", "spot"]))
    except Exception:
        return None
    if d.empty:
        return None
    d["datetime"] = pd.to_datetime(d["datetime"], errors="coerce")
    d = d.dropna(subset=["datetime"])
    d["is_call"] = d["option_type"].astype(str).str.upper().str.startswith("C")
    return d


def pick_strike(chain: pd.DataFrame, at, want_call: bool) -> tuple[float, float] | None:
    """(strike, premium) for the contract priced inside the band at time `at`."""
    snap = chain[(chain["datetime"] <= at) & (chain["is_call"] == want_call)]
    if snap.empty:
        return None
    snap = snap.sort_values("datetime").drop_duplicates("strike_price", keep="last")
    snap = snap[snap["volume"] > 0]                       # must actually trade
    band = snap[(snap["close"] >= PREMIUM_LO) & (snap["close"] <= PREMIUM_HI)]
    if band.empty:
        return None
    # Closest to the middle of the band.
    mid = (PREMIUM_LO + PREMIUM_HI) / 2
    row = band.iloc[(band["close"] - mid).abs().argmin()]
    return float(row["strike_price"]), float(row["close"])


def premium_at(chain: pd.DataFrame, at, strike: float, want_call: bool) -> float | None:
    s = chain[(chain["strike_price"] == strike) & (chain["is_call"] == want_call)
              & (chain["datetime"] <= at)]
    if s.empty:
        return None
    return float(s.sort_values("datetime").iloc[-1]["close"])


_CHAIN_CACHE: dict = {}


def cached_chain(day):
    """Option files are the slow part; a sweep re-reads the same days."""
    if day not in _CHAIN_CACHE:
        _CHAIN_CACHE[day] = option_chain_for(day)
    return _CHAIN_CACHE[day]


def simulate(bars, sl_pts, rr, band_lo, band_hi):
    """One (SL, RR) configuration -> ledger DataFrame."""
    global PREMIUM_LO, PREMIUM_HI
    PREMIUM_LO, PREMIUM_HI = band_lo, band_hi
    sig = index_signals(bars, sl_pts, rr)
    rows = []
    for _, s in sig.iterrows():
        chain = cached_chain(s["date"])
        if chain is None:
            continue
        want_call = s["dir"] > 0
        fill_in = pd.Timestamp(s["entry_dt"]) + BAR
        fill_out = pd.Timestamp(s["exit_dt"]) + BAR
        picked = pick_strike(chain, fill_in, want_call)
        if picked is None:
            continue
        strike, entry_prem = picked
        lots = int(BUDGET // (entry_prem * LOT))
        if lots < 1:
            continue
        exit_prem = premium_at(chain, fill_out, strike, want_call)
        if exit_prem is None:
            continue
        qty = lots * LOT
        rows.append({"date": s["date"], "pnl": (exit_prem - entry_prem) * qty,
                     "reason": s["reason"]})
    return pd.DataFrame(rows)


def split_of(d):
    y = pd.Timestamp(d).year
    return "TRAIN" if y <= 2023 else ("VALID" if y == 2024 else "TEST")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--years", nargs="+", default=None)
    ap.add_argument("--show", type=int, default=30, help="ledger rows to print")
    ap.add_argument("--sl", type=float, default=SL_PTS)
    ap.add_argument("--rr", type=float, default=RR)
    args = ap.parse_args()

    print("Preparing index bars ...", flush=True)
    bars = prepare(load_spot())
    if args.sweep:
        print()
        print("=" * 96)
        print(f"SL / R:R SWEEP — option band Rs {PREMIUM_LO:.0f}-{PREMIUM_HI:.0f}, "
              f"budget Rs {BUDGET:,.0f}   (GROSS)")
        print("=" * 96)
        print(f"  {'SL':>4}{'RR':>6}{'target':>8}{'trades':>8}{'WR':>7}"
              f"{'TRAIN':>12}{'VALID':>12}{'TEST':>12}{'TOTAL':>12}")
        for sl in (10, 15, 20, 25, 30):
            for rr in (1.5, 2, 3, 5):
                t = simulate(bars, float(sl), float(rr), 180.0, 200.0)
                if t.empty:
                    continue
                t["sp"] = t["date"].map(split_of)
                cells = [t[t["sp"] == k]["pnl"].sum() for k in ("TRAIN", "VALID", "TEST")]
                print(f"  {sl:>4}{rr:>6.1f}{sl * rr:>8.0f}{len(t):>8}"
                      f"{(t['pnl'] > 0).mean() * 100:>6.0f}%"
                      + "".join(f"{c:>12,.0f}" for c in cells)
                      + f"{t['pnl'].sum():>12,.0f}")
        print()
        print("  All three columns must be positive for the config to be believable.")
        return
    if args.years:
        bars = bars[bars["datetime"].dt.year.astype(str).isin(args.years)]
    sig = index_signals(bars, args.sl, args.rr)
    print(f"  {len(sig)} index signals\n")

    rows: list[dict] = []
    for _, s in sig.iterrows():
        chain = option_chain_for(s["date"])
        if chain is None:
            continue
        want_call = s["dir"] > 0
        fill_in = pd.Timestamp(s["entry_dt"]) + BAR   # bar CLOSE, not label
        fill_out = pd.Timestamp(s["exit_dt"]) + BAR
        picked = pick_strike(chain, fill_in, want_call)
        if picked is None:
            rows.append({"date": s["date"], "side": "CE" if want_call else "PE",
                         "skipped": "no strike in band"})
            continue
        strike, entry_prem = picked
        lots = int(BUDGET // (entry_prem * LOT))
        if lots < 1:
            rows.append({"date": s["date"], "side": "CE" if want_call else "PE",
                         "skipped": "budget < 1 lot"})
            continue
        exit_prem = premium_at(chain, fill_out, strike, want_call)
        if exit_prem is None:
            continue
        qty = lots * LOT
        pnl = (exit_prem - entry_prem) * qty
        rows.append({
            "date": s["date"], "entry_t": fill_in.strftime("%H:%M"),
            "exit_t": fill_out.strftime("%H:%M"),
            "side": "CE" if want_call else "PE", "strike": int(strike),
            "entry": round(entry_prem, 2), "exit": round(exit_prem, 2),
            "lots": lots, "deployed": round(entry_prem * qty),
            "pnl": round(pnl), "reason": s["reason"],
        })

    t = pd.DataFrame(rows)
    done = t[t.get("skipped").isna()] if "skipped" in t else t
    skipped = len(t) - len(done)

    print("=" * 104)
    print(f"BREAKOUT+RETEST via OPTIONS — premium band Rs {PREMIUM_LO:.0f}-{PREMIUM_HI:.0f}, "
          f"budget Rs {BUDGET:,.0f}, lot {LOT}")
    print("=" * 104)
    print(f"  index rule: SL {args.sl:.0f} pts, target {args.rr:.0f}R, "
          f"window {ENTRY_START}-{ENTRY_END}")
    print(f"  {len(done)} trades taken, {skipped} skipped (no strike in band / budget)\n")

    if done.empty:
        print("no trades")
        return

    print(f"  {'date':12}{'in':>6}{'out':>6}{'sd':>4}{'strike':>8}{'entry':>8}"
          f"{'exit':>8}{'lots':>5}{'deployed':>10}{'P&L':>11}{'why':>8}")
    for _, r in done.head(args.show).iterrows():
        print(f"  {str(r['date']):12}{r['entry_t']:>6}{r['exit_t']:>6}{r['side']:>4}"
              f"{r['strike']:>8}{r['entry']:>8.2f}{r['exit']:>8.2f}{r['lots']:>5}"
              f"{r['deployed']:>10,}{r['pnl']:>11,}{r['reason']:>8}")
    if len(done) > args.show:
        print(f"  ... {len(done) - args.show} more")

    wins = done[done["pnl"] > 0]
    print("\n" + "=" * 104)
    print(f"  trades {len(done)}   wins {len(wins)} ({len(wins) / len(done) * 100:.1f}%)   "
          f"NET Rs {done['pnl'].sum():,.0f}   avg Rs {done['pnl'].mean():,.0f}")
    print(f"  best Rs {done['pnl'].max():,.0f}   worst Rs {done['pnl'].min():,.0f}   "
          f"avg deployed Rs {done['deployed'].mean():,.0f}")
    print(f"  exits: {done['reason'].value_counts().to_dict()}")

    done["q"] = pd.to_datetime(done["date"]).dt.to_period("Q").astype(str)
    print(f"\n  QUARTERLY\n    {'quarter':10}{'trades':>8}{'WR':>8}{'net Rs':>13}")
    for q, g in done.groupby("q"):
        print(f"    {q:10}{len(g):>8}{(g['pnl'] > 0).mean() * 100:>7.0f}%"
              f"{g['pnl'].sum():>13,.0f}")

    done["year"] = pd.to_datetime(done["date"]).dt.year
    done["period"] = np.where(done["year"] <= 2023, "TRAIN 21-23",
                       np.where(done["year"] == 2024, "VALID 24", "TEST 25-26"))
    print(f"\n  HOLD-OUT\n    {'period':14}{'trades':>8}{'WR':>8}{'net Rs':>13}")
    for p in ("TRAIN 21-23", "VALID 24", "TEST 25-26"):
        g = done[done["period"] == p]
        if not g.empty:
            print(f"    {p:14}{len(g):>8}{(g['pnl'] > 0).mean() * 100:>7.0f}%"
                  f"{g['pnl'].sum():>13,.0f}")
    print("\n  GROSS of brokerage, STT, exchange, GST, stamp and spread.")


if __name__ == "__main__":
    main()
