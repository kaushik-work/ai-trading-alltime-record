"""
Historical multi-strategy replay — the most realistic forward-test preview.

Replays all 3 shadow signals (q5_straddle_level, q5_straddle_mom3,
q5_pcr_mom3) against the 13 days of option_snapshots, applying:

  • 4 trades/day cap per strategy
  • Daily loss cap: ₹2,000 per strategy
  • Aggregate daily loss cap: ₹3,500
  • Lot multiplier scaling (only kicks in after 30 closed trades per strategy)

What you'd see live if these rules had been in place across the sample.

Usage:
  python scripts/replay_multi_strategy.py
  python scripts/replay_multi_strategy.py --no-loss-caps   (disable risk caps)
  python scripts/replay_multi_strategy.py --csv replay.csv
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: F401
import numpy as np
import pandas as pd
from core import mongo  # noqa: E402

from backtest_straddle_signal import (
    _walk_forward, LOT_SIZE,
)


def _load_snapshots(db, symbol: str) -> pd.DataFrame:
    """Same as backtest_straddle_signal._load_snapshots but INCLUDES OI
    (needed for PCR mom3 signal)."""
    print(f"Loading option_snapshots for {symbol} ...", flush=True)
    cur = db.option_snapshots.find(
        {"symbol": symbol},
        projection={"_id": 0, "date": 1, "timestamp": 1, "strike": 1,
                    "option_type": 1, "ltp": 1, "oi": 1, "spot": 1},
    )
    df = pd.DataFrame(list(cur))
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["ts", "ltp", "spot"])
    df["strike"] = df["strike"].astype(int)
    df["oi"]     = df["oi"].fillna(0).astype(int)
    df["ltp"]    = df["ltp"].astype(float)
    df["spot"]   = df["spot"].astype(float)
    print(f"  loaded {len(df):,} rows across {df['date'].nunique()} days "
          f"(with OI)", flush=True)
    return df
from core.risk_budget import (
    DAILY_AGG_LOSS_CAP, PER_STRAT_LOSS_CAP,
    SCALE_UP_MIN_CLOSED, SCALE_UP_MIN_WR, SCALE_UP_MIN_PF,
    SCALE_DOWN_SL_STREAK, MAX_LOT_MULTIPLIER,
)

MAX_TRADES_PER_DAY = 4
EOD_LIMIT = dtime(15, 20)


# ── Transaction-cost model (Angel One, NIFTY options, post-Budget-2024) ──────
# Per round trip (BUY entry + SELL exit) at 1 lot of 65 contracts:
#   Brokerage       Rs 20 per order             = Rs 40
#   STT             0.1% of sell-side premium   ~= entry_premium*65*0.001
#   Exch. txn       0.053% on premium turnover  ~= (entry+exit)*65*0.00053
#   GST             18% on (brokerage + exch)
#   SEBI + stamp    ~= Rs 0.30
# Net per round trip averages ~Rs 65-75 at typical NIFTY premium levels.
BROKERAGE_PER_ORDER = 20.0
STT_RATE            = 0.001       # 0.1% on sell premium (options, FY25 rate)
EXCH_TXN_RATE       = 0.00053     # 0.053% on premium turnover
GST_RATE            = 0.18
MISC_PER_TRIP       = 0.30


def _round_trip_cost(entry_premium: float, exit_premium: float,
                      lot_size: int = LOT_SIZE, lots: int = 1) -> float:
    """Approximate all-in cost of one buy+sell round trip on a NIFTY option."""
    qty = lot_size * lots
    brokerage = BROKERAGE_PER_ORDER * 2   # buy + sell
    stt       = exit_premium * qty * STT_RATE
    exch_txn  = (entry_premium + exit_premium) * qty * EXCH_TXN_RATE
    gst       = (brokerage + exch_txn) * GST_RATE
    return round(brokerage + stt + exch_txn + gst + MISC_PER_TRIP, 2)


def _bars_with_full_chain(df: pd.DataFrame) -> dict:
    """Build {(date, ts): [row, ...]} of all snapshot rows per bar."""
    out: dict = {}
    for r in df.itertuples(index=False):
        out.setdefault((r.date, r.ts), []).append({
            "strike":      int(r.strike),
            "option_type": r.option_type,
            "ltp":         float(r.ltp),
            "oi":          int(r.oi) if hasattr(r, "oi") and r.oi else 0,
            "spot":        float(r.spot),
        })
    return out


def _atm_strike(spot: float) -> int:
    return int(round(spot / 50)) * 50


def _atm_straddle(rows, atm):
    ce = next((r["ltp"] for r in rows
               if r["strike"] == atm and r["option_type"] == "CE"), None)
    pe = next((r["ltp"] for r in rows
               if r["strike"] == atm and r["option_type"] == "PE"), None)
    if ce is None or pe is None:
        return None
    return ce + pe


def _pcr_oi(rows):
    ce_oi = sum(r["oi"] for r in rows if r["option_type"] == "CE")
    pe_oi = sum(r["oi"] for r in rows if r["option_type"] == "PE")
    if ce_oi == 0:
        return None
    return pe_oi / ce_oi


def _compute_signal(name, rows, history_rows):
    """Same logic as strategies/feature_signals.py — kept inline here so the
    backtest stays self-contained and doesn't need to import that module's
    Mongo-tied threshold cache."""
    if not rows:
        return None
    spot = rows[0].get("spot")
    if not spot:
        return None
    atm = _atm_strike(spot)
    if name == "q5_straddle_level":
        return _atm_straddle(rows, atm)
    if name == "q5_straddle_mom3":
        if history_rows is None or len(history_rows) < 3:
            return None
        cur = _atm_straddle(rows, atm)
        if cur is None:
            return None
        past_rows = history_rows[0]   # 3 bars ago
        past_spot = past_rows[0].get("spot")
        if past_spot is None:
            return None
        past_atm = _atm_strike(past_spot)
        past = _atm_straddle(past_rows, past_atm)
        if past is None:
            return None
        return cur - past
    if name == "q5_pcr_mom3":
        if history_rows is None or len(history_rows) < 3:
            return None
        cur = _pcr_oi(rows)
        past = _pcr_oi(history_rows[0])
        if cur is None or past is None:
            return None
        return cur - past
    return None


def _build_thresholds(bars: dict, signal_name: str, n_days: int = 5,
                      pct: float = 0.70) -> dict:
    """{date: threshold} from trailing n_days. None for warmup."""
    dates_sorted = sorted(set(d for (d, _) in bars.keys()))
    thresholds = {}

    # Pre-compute feature values per bar
    by_day_bars: dict = defaultdict(list)
    for (d, ts) in sorted(bars.keys()):
        by_day_bars[d].append((ts, bars[(d, ts)]))
    for d in by_day_bars:
        by_day_bars[d].sort(key=lambda x: x[0])

    feature_by_date_bar: dict = {}
    for d, bar_list in by_day_bars.items():
        for i, (ts, rows) in enumerate(bar_list):
            # history[0] must be 3-bars-ago, history[2] must be 1-bar-ago,
            # matching strategies/feature_signals.py._today_history().
            history = [bar_list[i - k][1] for k in (3, 2, 1)] if i >= 3 else None
            v = _compute_signal(signal_name, rows, history)
            feature_by_date_bar[(d, ts)] = v

    for i, d in enumerate(dates_sorted):
        if i < n_days:
            thresholds[d] = None
            continue
        prior_dates = dates_sorted[i - n_days:i]
        sample = [feature_by_date_bar[(d2, ts)]
                  for (d2, ts) in feature_by_date_bar
                  if d2 in prior_dates and feature_by_date_bar[(d2, ts)] is not None]
        if len(sample) < 30:
            thresholds[d] = None
            continue
        sample.sort()
        idx = int(pct * (len(sample) - 1))
        thresholds[d] = sample[idx]
    return thresholds, feature_by_date_bar


def _index_premium_series(df: pd.DataFrame) -> dict:
    by_key: dict = defaultdict(list)
    for r in df.itertuples(index=False):
        by_key[(r.date, int(r.strike), r.option_type)].append(
            (r.ts.to_pydatetime(), float(r.ltp))
        )
    for k in by_key:
        by_key[k].sort()
    return by_key


def _classify_regime_inline(bar_list, current_idx, window_bars=6,
                             trend_thr=0.0015) -> str:
    """Same logic as strategies/feature_signals._classify_regime() but
    inlined for the replay (works on the per-day bar list rather than
    a Mongo query)."""
    if current_idx < window_bars:
        return "warmup"
    spots = [bar_list[current_idx - k][1][0]["spot"]
             for k in range(window_bars, -1, -1)]
    if any(s is None or s <= 0 for s in spots):
        return "warmup"
    ret = spots[-1] / spots[0] - 1.0
    import numpy as _np
    x = _np.arange(len(spots))
    slope = float(_np.polyfit(x, spots, 1)[0])
    if ret >= trend_thr and slope > 0:
        return "trend_up"
    if ret <= -trend_thr and slope < 0:
        return "trend_down"
    return "chop"


def _trade_qualifies(state: dict, strategy: str, date: str, atm: int,
                     apply_loss_caps: bool) -> tuple:
    """Risk-budget gate. Returns (allowed, reason, lot_mult)."""
    # Per-day per-strategy trade count cap
    if state["count_today"][(date, strategy)] >= MAX_TRADES_PER_DAY:
        return False, "4/day cap", 1

    # Same-strike correlation guard: another strategy already has this
    # strike + CE side open today
    for other_s, pos in state["open"].items():
        if other_s == strategy:
            continue
        if pos.get("strike") == atm and pos.get("side") == "CE":
            return False, "strike already held", 1

    if not apply_loss_caps:
        return True, "", 1

    # Per-strategy daily loss cap
    if state["strat_pnl_today"][(date, strategy)] <= -PER_STRAT_LOSS_CAP:
        return False, f"{strategy} loss cap", 1

    # Aggregate daily loss cap
    if state["agg_pnl_today"][date] <= -DAILY_AGG_LOSS_CAP:
        return False, "agg loss cap", 1

    # Lot multiplier
    closed = state["closed_trades"][strategy]
    if len(closed) < SCALE_UP_MIN_CLOSED:
        return True, "", 1

    last_n = closed[-10:]
    # De-scale
    sl_streak = closed[-SCALE_DOWN_SL_STREAK:]
    if (len(sl_streak) == SCALE_DOWN_SL_STREAK and
            all(t.get("reason") == "SL" for t in sl_streak)):
        return True, "", 1

    wins   = [t for t in last_n if t["pnl"] > 0]
    losses = [t for t in last_n if t["pnl"] < 0]
    wr = len(wins) / len(last_n)
    gw = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = (gw / gl) if gl > 0 else float("inf")
    if wr > SCALE_UP_MIN_WR and pf > SCALE_UP_MIN_PF:
        return True, "", MAX_LOT_MULTIPLIER
    return True, "", 1


def _replay(apply_loss_caps: bool, sl_dist: float = 10.0, rr: float = 3.0,
            strike_offset_steps: int = 0):
    """strike_offset_steps: 0 = ATM, -1 = ITM by 1 step (50 pts), +1 = OTM by 1 step."""
    db = mongo.get_db()
    if db is None:
        print("Mongo unreachable.")
        sys.exit(1)
    df = _load_snapshots(db, "NIFTY")
    if df.empty:
        print("No snapshots.")
        return [], {}

    bars = _bars_with_full_chain(df)
    premium_series = _index_premium_series(df)

    strategies = ["q5_straddle_level", "q5_straddle_mom3", "q5_pcr_mom3"]
    thresholds_by_sig = {}
    features_by_sig   = {}
    for s in strategies:
        thr, feats = _build_thresholds(bars, s)
        thresholds_by_sig[s] = thr
        features_by_sig[s]   = feats

    state = {
        "count_today":      defaultdict(int),
        "strat_pnl_today":  defaultdict(float),
        "agg_pnl_today":    defaultdict(float),
        "closed_trades":    defaultdict(list),  # per strategy, oldest-first
        "open":             {},   # strategy -> open trade dict
    }

    trades = []
    refused = defaultdict(int)
    sorted_bars = sorted(bars.keys())

    # Build per-day bar lists for regime classification (need 6 prior bars)
    by_day_bars: dict = defaultdict(list)
    for (date, ts) in sorted_bars:
        by_day_bars[date].append((ts, bars[(date, ts)]))
    # Index lookup so we know the current bar's position within its day
    day_bar_index: dict = {}
    for date, bar_list in by_day_bars.items():
        for i, (ts, _) in enumerate(bar_list):
            day_bar_index[(date, ts)] = i

    for (date, ts) in sorted_bars:
        rows = bars[(date, ts)]
        bar_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        spot = rows[0]["spot"]
        atm = _atm_strike(spot)

        # ── First: tick any open positions
        for s in strategies:
            if s not in state["open"]:
                continue
            pos = state["open"][s]
            key = (pos["date"], int(pos["strike"]), "CE")
            series = premium_series.get(key)
            if not series:
                continue
            # find ltp at this bar
            ltp = None
            for dt, p in series:
                if dt == bar_dt:
                    ltp = p
                    break
            if ltp is None:
                continue
            # Check SL/TP/EOD
            exit_premium = None
            reason = None
            if bar_dt.time() >= EOD_LIMIT:
                exit_premium = ltp
                reason = "EOD"
            elif ltp <= pos["sl_price"]:
                exit_premium = pos["sl_price"]
                reason = "SL"
            elif ltp >= pos["tp_price"]:
                exit_premium = pos["tp_price"]
                reason = "TP"
            if reason:
                lot_mult = pos["lot_multiplier"]
                gross_pnl = round((exit_premium - pos["entry_premium"])
                                   * LOT_SIZE * lot_mult, 2)
                cost = _round_trip_cost(pos["entry_premium"], exit_premium,
                                          lots=lot_mult)
                pnl = round(gross_pnl - cost, 2)
                pos.update({
                    "exit_dt": bar_dt, "exit_premium": exit_premium,
                    "reason": reason,
                    "gross_pnl": gross_pnl,
                    "cost":      cost,
                    "pnl":       pnl,    # net of costs
                })
                trades.append(pos)
                state["closed_trades"][s].append(pos)
                state["strat_pnl_today"][(date, s)] += pnl
                state["agg_pnl_today"][date] += pnl
                del state["open"][s]

        # Compute current regime once per bar (shared across all strategies)
        bar_idx = day_bar_index.get((date, ts), -1)
        day_bars_list = by_day_bars.get(date, [])
        regime = _classify_regime_inline(day_bars_list, bar_idx)

        # ── Then: check each strategy for a new entry
        for s in strategies:
            if s in state["open"]:
                continue
            thr = thresholds_by_sig[s].get(date)
            if thr is None:
                continue
            feat = features_by_sig[s].get((date, ts))
            if feat is None or feat <= thr:
                continue
            # Regime gate: refuse fires in trend_up (PF 1.13 — noise regime)
            if regime == "trend_up":
                refused["trend_up regime"] += 1
                continue
            # Strike selection: ATM by default; ITM by -1 step for CE means
            # strike below spot (more intrinsic, higher delta).
            chosen_strike = atm + strike_offset_steps * 50
            ok, why, lot_mult = _trade_qualifies(state, s, date, chosen_strike,
                                                  apply_loss_caps)
            if not ok:
                refused[why] += 1
                continue
            # Entry premium: chosen-strike CE LTP at this bar
            key = (date, chosen_strike, "CE")
            series = premium_series.get(key)
            if not series:
                refused["no premium data for chosen strike"] = refused.get(
                    "no premium data for chosen strike", 0) + 1
                continue
            entry_premium = next((p for (dt, p) in series if dt == bar_dt), None)
            if entry_premium is None:
                continue
            pos = {
                "strategy":       s,
                "date":           date,
                "entry_dt":       bar_dt,
                "strike":         chosen_strike,
                "side":           "CE",
                "entry_premium":  entry_premium,
                "sl_price":       round(entry_premium - sl_dist, 2),
                "tp_price":       round(entry_premium + sl_dist * rr, 2),
                "threshold":      thr,
                "spot_at_entry":  spot,
                "lot_multiplier": lot_mult,
                "feature_value":  feat,
            }
            state["open"][s] = pos
            state["count_today"][(date, s)] += 1

    return trades, refused


def _print_day_by_day(trades: list, refused: dict):
    """Trade-level ledger grouped by day. Every trade shown with full detail."""
    if not trades:
        print("\n  (no trades to show)")
        return

    by_day: dict = defaultdict(list)
    for t in trades:
        by_day[t["date"]].append(t)
    for d in by_day:
        by_day[d].sort(key=lambda x: x["entry_dt"])

    print(f"\n{'='*92}")
    print("=== Day-by-day ledger (every trade) ===")
    print(f"{'='*92}")
    cum = 0.0
    drawdown_low = 0.0
    drawdown_peak = 0.0

    for d in sorted(by_day.keys()):
        day_trades = by_day[d]
        day_pnl = sum(t["pnl"] for t in day_trades)
        wins = sum(1 for t in day_trades if t["pnl"] > 0)
        cum += day_pnl
        drawdown_peak = max(drawdown_peak, cum)
        dd = cum - drawdown_peak
        drawdown_low = min(drawdown_low, dd)

        # Per-day banner
        sign = "WIN" if day_pnl > 0 else ("LOSS" if day_pnl < 0 else "FLAT")
        bar = "+" * max(1, int(abs(day_pnl) / 500)) if day_pnl != 0 else "."
        print(f"\n  {d}   {sign:>4}   day P&L Rs {day_pnl:>+8,.0f}   "
              f"cum Rs {cum:>+9,.0f}   dd Rs {dd:>+8,.0f}")
        print(f"  {'-'*88}")
        print(f"  {'time':>8}  {'strategy':>20}  {'strike':>6}  "
              f"{'entry':>7}  {'exit':>7}  {'reason':>5}  {'pnl':>9}  {'mult':>4}")
        for t in day_trades:
            entry_t = t["entry_dt"].strftime("%H:%M:%S") if hasattr(t["entry_dt"], "strftime") else str(t["entry_dt"])[11:19]
            exit_t  = (t["exit_dt"].strftime("%H:%M:%S")
                       if t.get("exit_dt") and hasattr(t["exit_dt"], "strftime")
                       else str(t.get("exit_dt", ""))[11:19])
            print(f"  {entry_t:>8}  {t['strategy']:>20}  {t['strike']:>6}  "
                  f"Rs{t['entry_premium']:>5.1f}  Rs{t['exit_premium']:>5.1f}  "
                  f"{t['reason']:>5}  Rs{t['pnl']:>+7,.0f}  {t.get('lot_multiplier', 1):>3}x")
        # Day summary line
        print(f"  {'-'*88}")
        print(f"  {'':>8}  trades={len(day_trades)}  wins={wins}  losses={len(day_trades)-wins}  "
              f"WR={wins/len(day_trades)*100:>4.1f}%  day P&L Rs {day_pnl:+,.0f}")

    print(f"\n{'='*92}")
    print(f"  Peak cumulative: Rs {drawdown_peak:+,.0f}")
    print(f"  Max drawdown:    Rs {drawdown_low:+,.0f}  ({abs(drawdown_low)/50000*100:.1f}% of Rs 50K capital)")
    print(f"  Final cumulative: Rs {cum:+,.0f}")
    if refused:
        print(f"\n  Entries refused by risk budget:")
        for r, c in sorted(refused.items(), key=lambda x: -x[1]):
            print(f"    {r}:  {c}")


def _summarise(trades: list, label: str, lot_size_assumed: int = 65):
    print(f"\n{'='*72}")
    print(f"=== {label} ===")
    print(f"{'='*72}")
    if not trades:
        print("  no trades")
        return
    pnls = [t["pnl"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = sum(pnls)
    gw = sum(wins); gl = abs(sum(losses))
    pf = (gw / gl) if gl > 0 else float("inf")
    print(f"  Trades: {len(trades)}    Wins: {len(wins)}    Losses: {len(losses)}")
    print(f"  WR: {len(wins)/len(pnls)*100:.1f}%    PF: {pf:.2f}    "
          f"Net: Rs {total:+,.0f}    Exp: Rs {total/len(trades):+,.0f}/trade")

    # Per-strategy
    by_s: dict = defaultdict(list)
    for t in trades:
        by_s[t["strategy"]].append(t)
    print(f"\n  Per-strategy:")
    print(f"    {'strategy':>20}  {'n':>3}  {'WR%':>5}  {'PF':>6}  {'Net Rs':>10}")
    for s, lst in by_s.items():
        ps = [t["pnl"] for t in lst]
        w = [p for p in ps if p > 0]
        l = [p for p in ps if p < 0]
        gw_s = sum(w); gl_s = abs(sum(l))
        pf_s = (gw_s / gl_s) if gl_s > 0 else float("inf")
        pf_str = "inf" if pf_s == float("inf") else f"{pf_s:.2f}"
        print(f"    {s:>20}  {len(lst):>3}  {len(w)/len(lst)*100:>5.1f}  "
              f"{pf_str:>6}  {sum(ps):>+10,.0f}")

    # Per-day
    by_d: dict = defaultdict(float)
    for t in trades:
        by_d[t["date"]] += t["pnl"]
    print(f"\n  Per-day P&L:")
    cum = 0.0
    for d, p in sorted(by_d.items()):
        cum += p
        print(f"    {d}   Rs {p:>+8,.0f}    (cum Rs {cum:>+8,.0f})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-loss-caps", action="store_true",
                    help="Disable risk-budget loss caps (4/day cap still applies)")
    ap.add_argument("--sl", type=float, default=10.0, help="SL distance in premium points")
    ap.add_argument("--rr", type=float, default=2.25, help="R:R ratio (default 2.25 — matches production)")
    ap.add_argument("--strike-offset", type=int, default=-1,
                    help="Strike offset in steps (default -1 ITM-50 — matches production). "
                         "0=ATM, -1=ITM by 50, -2=ITM by 100, +1=OTM by 50.")
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()

    print("=" * 72)
    print("Multi-strategy historical replay")
    print(f"  Capital baseline:  Rs {50000:,}")
    print(f"  4 trades/day/strategy cap: ON")
    print(f"  Loss caps:         {'OFF (--no-loss-caps)' if args.no_loss_caps else 'ON'}")
    if not args.no_loss_caps:
        print(f"    per-strategy:    Rs -{PER_STRAT_LOSS_CAP:,}/day")
        print(f"    aggregate:       Rs -{DAILY_AGG_LOSS_CAP:,}/day")

    strike_label = (f"ITM by {-args.strike_offset * 50}" if args.strike_offset < 0
                    else f"OTM by {args.strike_offset * 50}" if args.strike_offset > 0
                    else "ATM")
    print(f"  SL={args.sl}  RR={args.rr}  (TP at +Rs {args.sl * args.rr:.0f})")
    print(f"  Strike:  {strike_label}  (offset {args.strike_offset:+d} step{'s' if abs(args.strike_offset) != 1 else ''})")
    trades, refused = _replay(apply_loss_caps=not args.no_loss_caps,
                               sl_dist=args.sl, rr=args.rr,
                               strike_offset_steps=args.strike_offset)
    label = ("3-strategy portfolio + risk budget"
             if not args.no_loss_caps else
             "3-strategy portfolio (4/day cap only, no loss caps)")
    _summarise(trades, label)
    _print_day_by_day(trades, refused)

    if False and refused:  # day_by_day already printed it
        print(f"\n  Refused entries (by reason):")
        for r, c in sorted(refused.items(), key=lambda x: -x[1]):
            print(f"    {r}:  {c}")

    if args.csv and trades:
        df = pd.DataFrame(trades)
        df["entry_dt"] = df["entry_dt"].astype(str)
        df.to_csv(args.csv, index=False)
        print(f"\nLedger written to {args.csv}")


if __name__ == "__main__":
    main()
