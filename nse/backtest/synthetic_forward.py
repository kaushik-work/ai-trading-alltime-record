"""NSE synthetic-forward backtest engine.

Reads historical option-chain snapshots (CSV or Mongo) and walks through
time applying the same signal + persistence + exit logic used by the live
strategy. Outputs per-symbol metrics and a trade CSV.

The synthetic forward is traded as an options combo:
  long  synthetic = +CE − PE
  short synthetic = −CE + PE

PnL is computed from the actual change in the combo's net premium (CE − PE
for long, PE − CE for short), multiplied by the number of lots and the
underlying lot size. This is a much closer proxy to live options trading
than spot-based PnL.

Realism notes vs. live trading:
  - Entry/exit apply bid-ask spread per leg from the snapshot.
  - Fixed per-leg fees approximate brokerage + STT + charges.
  - Liquidity filter drops snapshots where bid/ask are missing or too wide.
  - Lot sizing is based on the fixed estimated margin required for one
    synthetic-forward lot (MARGIN_PER_LOT_INR).  Trades are skipped if the
    allocated capital cannot cover that margin.  This is a backtest proxy;
    the live broker will enforce the exact SPAN + exposure margin.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from nse.config import (
    BACKTEST_MARGIN_FALLBACK_INR,
    ENTRY_PCT,
    FEE_BPS_PER_LEG,
    FIXED_CAPITAL_INR,
    LOT_SIZES,
    MAX_HOLD_HOURS,
    STEP_SIZES,
    STOP_LOSS_PCT,
    TARGET_PCT,
    TOTAL_CAPITAL_INR,
    TRAIL_GIVEBACK_PCT,
    TRAIL_PEAK_PCT,
)
from nse.data.option_chain import load_snapshots_csv, load_snapshots_mongo
from nse.strategies.synthetic_forward import SyntheticForwardStrategy

logger = logging.getLogger(__name__)

# Realism dials (backtest-only)
MAX_SPREAD_PCT = 0.10          # reject leg if (ask-bid)/ltp > 10%
MIN_LEG_VOLUME = 0             # minimum contracts traded; 0 = disabled
MIN_LEG_OI = 0                 # minimum open interest; 0 = disabled


def _bucket_to_interval(df: pd.DataFrame, minutes: int = 5) -> pd.DataFrame:
    """Snap snapshots to a regular interval for backtest decisions."""
    df = df.copy()
    df["t_bucket"] = df["timestamp"].dt.floor(f"{minutes}min")
    return df.sort_values("timestamp").drop_duplicates(
        subset=["t_bucket", "expiry", "strike", "side"], keep="last"
    )


def _liquidity_ok(row: pd.Series) -> bool:
    """Check a single option quote is liquid enough to trade."""
    ltp = float(row.get("mark", 0))
    bid = float(row.get("bid", 0) or 0)
    ask = float(row.get("ask", 0) or 0)
    if ltp <= 0:
        return False
    if bid > 0 and ask > 0:
        if (ask - bid) / ltp > MAX_SPREAD_PCT:
            return False
    if MIN_LEG_VOLUME > 0 and int(row.get("volume", 0) or 0) < MIN_LEG_VOLUME:
        return False
    if MIN_LEG_OI > 0 and int(row.get("oi", 0) or 0) < MIN_LEG_OI:
        return False
    return True


def _get_leg_quote(snap: pd.DataFrame, strike: int, option_type: str) -> dict | None:
    """Return quote dict for a strike+type, or None if illiquid."""
    rows = snap[(snap["strike"] == strike) & (snap["side"] == option_type)]
    if rows.empty:
        return None
    row = rows.iloc[0]
    if not _liquidity_ok(row):
        return None
    ltp = float(row["mark"])
    bid = float(row.get("bid", 0) or 0) or ltp
    ask = float(row.get("ask", 0) or 0) or ltp
    return {"ltp": ltp, "bid": bid, "ask": ask}


def _combo_value(ce_q: dict, pe_q: dict, side: str, use_exit: bool = False) -> float:
    """Net premium of the synthetic-forward combo.

    long synthetic  = +CE − PE  → entry buy CE@ask, sell PE@bid
    short synthetic = −CE + PE  → entry sell CE@bid, buy PE@ask
    """
    if side == "long":
        ce_px = ce_q["ask"] if not use_exit else ce_q["bid"]
        pe_px = pe_q["bid"] if not use_exit else pe_q["ask"]
        return ce_px - pe_px
    else:
        ce_px = ce_q["bid"] if not use_exit else ce_q["ask"]
        pe_px = pe_q["ask"] if not use_exit else pe_q["bid"]
        return pe_px - ce_px


def _fees_for_lots(lots: int) -> float:
    """Total fixed fees in INR for entry + exit of a 2-leg combo."""
    # 4 fills (CE entry, PE entry, CE exit, PE exit)
    # Fee per fill is a percentage of premium; here we approximate as a
    # fixed amount per lot to avoid premium-dependence. Override if needed.
    return lots * 40.0  # placeholder: ₹10 per fill per lot


def _exit_position(pos: dict, t: datetime, equity: float) -> tuple[float, dict]:
    """Close a position and return (new_equity, trade_event)."""
    entry_val = pos["entry_combo_value"]
    exit_val = pos["exit_combo_value"]
    qty = pos["lots"] * pos["lot_size"]
    # PnL = change in value of the combo position we hold.
    gross = (exit_val - entry_val) * qty
    fees = _fees_for_lots(pos["lots"])
    pnl = gross - fees
    equity += pnl
    event = {
        "entry_t": pos["entry_t"],
        "exit_t": t,
        "symbol": pos["symbol"],
        "signal_side": pos["signal_side"],
        "entry_px": pos["entry_underlying"],
        "exit_px": pos.get("exit_underlying", 0),
        "entry_combo": entry_val,
        "exit_combo": exit_val,
        "pred_pct": pos["pred_pct"],
        "n_strikes": pos["n_strikes"],
        "margin_used": pos["margin_used"],
        "lots": pos["lots"],
        "lot_size": pos["lot_size"],
        "pnl": pnl,
        "pnl_pct": pnl / pos["capital"] * 100 if pos["capital"] else 0,
        "reason": pos.get("exit_reason", "unknown"),
        "equity_after": equity,
        "entry_strike": pos["strike"],
    }
    return equity, event


def run_backtest(symbol: str, df: pd.DataFrame, capital: float = FIXED_CAPITAL_INR,
                 interval_minutes: int = 5) -> dict:
    """Run synthetic-forward backtest on a normalized snapshot DataFrame."""
    df = _bucket_to_interval(df, interval_minutes)
    buckets = sorted(df["t_bucket"].unique())
    if not buckets:
        return {"trades": 0, "pnl": 0.0, "equity": capital, "events": []}

    strategy = SyntheticForwardStrategy(symbol)
    sig_history: dict[datetime, list[tuple[datetime, float]]] = {}
    events = []
    equity = capital
    open_pos = None
    lot_size = LOT_SIZES.get(symbol, 1)
    step = STEP_SIZES.get(symbol, 50)

    spot_series = (df[["timestamp", "spot"]]
                   .dropna().drop_duplicates("timestamp")
                   .set_index("timestamp")["spot"].sort_index())

    for i, t in enumerate(buckets):
        snap = df[df["t_bucket"] == t]
        sigs = strategy.compute(snap, t)
        for s in sigs:
            sig_history.setdefault(s.expiry, []).append((t, s.pred))
        for e in list(sig_history.keys()):
            sig_history[e] = [(ti, pi) for ti, pi in sig_history[e]
                              if (t - ti).total_seconds() <= 6 * 3600]

        if t in spot_series.index:
            spot_t = float(spot_series.loc[t])
        else:
            idx = spot_series.index.get_indexer([t], method="nearest")[0]
            spot_t = float(spot_series.iloc[idx]) if idx >= 0 else None
        if spot_t is None or pd.isna(spot_t):
            continue

        # Manage open position using actual option marks.
        if open_pos:
            ce_q = _get_leg_quote(snap, open_pos["strike"], "CE")
            pe_q = _get_leg_quote(snap, open_pos["strike"], "PE")
            if ce_q is not None and pe_q is not None:
                current_val = _combo_value(ce_q, pe_q, open_pos["signal_side"], use_exit=True)
                # SL/TP are tracked on underlying spot movement (delta ≈ 1).
                unreal = (spot_t - open_pos["entry_underlying"]) / open_pos["entry_underlying"]
                if open_pos["signal_side"] == "short":
                    unreal = -unreal
                open_pos["peak"] = max(open_pos.get("peak", 0.0), unreal)
                open_pos["exit_combo_value"] = current_val
                open_pos["exit_underlying"] = spot_t

                reason = None
                held_h = (t - open_pos["entry_t"]).total_seconds() / 3600
                if t >= open_pos["expiry"]:
                    reason = "expiry"
                elif held_h >= MAX_HOLD_HOURS:
                    reason = "max_hold"
                elif unreal < -STOP_LOSS_PCT:
                    reason = "stop"
                elif open_pos["peak"] >= TRAIL_PEAK_PCT and (open_pos["peak"] - unreal) > TRAIL_GIVEBACK_PCT:
                    reason = "trail"
                elif unreal >= TARGET_PCT:
                    reason = "target"

                if reason:
                    open_pos["exit_reason"] = reason
                    equity, event = _exit_position(open_pos, t, equity)
                    events.append(event)
                    open_pos = None

        if open_pos:
            continue

        # Entry logic.
        candidates = sorted(sigs, key=lambda s: abs(s.pred), reverse=True)
        chosen = None
        for c in candidates:
            if not strategy.gate(c, sig_history):
                continue
            chosen = c
            break
        if chosen is None:
            continue

        atm = int(round(chosen.spot / step)) * step
        ce_q = _get_leg_quote(snap, atm, "CE")
        pe_q = _get_leg_quote(snap, atm, "PE")
        if ce_q is None or pe_q is None:
            continue

        combo_val = _combo_value(ce_q, pe_q, chosen.side, use_exit=False)
        if combo_val == 0:
            continue

        # Lot sizing based on backtest-only fallback margin per lot.
        margin_per_lot = BACKTEST_MARGIN_FALLBACK_INR.get(symbol, spot_t * lot_size * 0.12)
        lots = int(capital // margin_per_lot) if margin_per_lot > 0 else 0
        if lots <= 0:
            continue
        margin_used = lots * margin_per_lot

        open_pos = {
            "symbol": symbol,
            "signal_side": chosen.side,
            "entry_t": t,
            "entry_underlying": spot_t,
            "entry_combo_value": combo_val,
            "strike": atm,
            "expiry": chosen.expiry,
            "capital": capital,
            "margin_used": margin_used,
            "lots": lots,
            "lot_size": lot_size,
            "pred_pct": chosen.pred * 100,
            "n_strikes": chosen.n_strikes,
            "peak": 0.0,
            "exit_combo_value": combo_val,
            "exit_underlying": spot_t,
        }

    tdf = pd.DataFrame(events)
    metrics = _compute_metrics(tdf, equity, capital)
    metrics["events"] = events
    return metrics


def run_backtest_shared(symbol_dfs: dict[str, pd.DataFrame],
                        capital: float = TOTAL_CAPITAL_INR,
                        interval_minutes: int = 5) -> dict:
    """Shared-capital backtest across all symbols.

    Only one combo per symbol at a time, but all symbols draw from the same
    capital pool. At each global time bucket the strongest gated signal that
    fits in free margin is entered.
    """
    # Prepare per-symbol state.
    strategies: dict[str, SyntheticForwardStrategy] = {}
    bucketed: dict[str, pd.DataFrame] = {}
    bucket_sets: dict[str, set] = {}
    spot_series: dict[str, pd.Series] = {}
    sig_history: dict[str, dict[datetime, list[tuple[datetime, float]]]] = {}
    all_buckets: set[datetime] = set()

    for symbol, df in symbol_dfs.items():
        if df.empty:
            continue
        df = _bucket_to_interval(df, interval_minutes)
        bucketed[symbol] = df
        bucket_sets[symbol] = set(df["t_bucket"].unique())
        strategies[symbol] = SyntheticForwardStrategy(symbol)
        sig_history[symbol] = {}
        spot_series[symbol] = (df[["timestamp", "spot"]]
                               .dropna().drop_duplicates("timestamp")
                               .set_index("timestamp")["spot"].sort_index())
        all_buckets.update(bucket_sets[symbol])

    if not all_buckets:
        return {"trades": 0, "pnl": 0.0, "equity": capital, "events": []}

    equity = capital
    open_positions: dict[str, dict] = {}  # keyed by symbol
    events = []

    for t in sorted(all_buckets):
        # 1. Manage existing positions for symbols that have data at this bucket.
        for symbol in list(open_positions.keys()):
            df = bucketed.get(symbol)
            if df is None or t not in bucket_sets.get(symbol, set()):
                continue
            snap = df[df["t_bucket"] == t]
            pos = open_positions[symbol]
            spot_t = _spot_at_time(spot_series[symbol], t)
            if spot_t is None:
                continue
            ce_q = _get_leg_quote(snap, pos["strike"], "CE")
            pe_q = _get_leg_quote(snap, pos["strike"], "PE")
            if ce_q is None or pe_q is None:
                continue
            current_val = _combo_value(ce_q, pe_q, pos["signal_side"], use_exit=True)
            unreal = (spot_t - pos["entry_underlying"]) / pos["entry_underlying"]
            if pos["signal_side"] == "short":
                unreal = -unreal
            pos["peak"] = max(pos.get("peak", 0.0), unreal)
            pos["exit_combo_value"] = current_val
            pos["exit_underlying"] = spot_t

            reason = None
            held_h = (t - pos["entry_t"]).total_seconds() / 3600
            if t >= pos["expiry"]:
                reason = "expiry"
            elif held_h >= MAX_HOLD_HOURS:
                reason = "max_hold"
            elif unreal < -STOP_LOSS_PCT:
                reason = "stop"
            elif pos["peak"] >= TRAIL_PEAK_PCT and (pos["peak"] - unreal) > TRAIL_GIVEBACK_PCT:
                reason = "trail"
            elif unreal >= TARGET_PCT:
                reason = "target"

            if reason:
                pos["exit_reason"] = reason
                equity, event = _exit_position(pos, t, equity)
                events.append(event)
                del open_positions[symbol]

        # 2. Try entries for symbols with a bucket at this time.
        candidates = []
        for symbol, df in bucketed.items():
            if symbol in open_positions:
                continue
            if t not in bucket_sets.get(symbol, set()):
                continue
            snap = df[df["t_bucket"] == t]
            spot_t = _spot_at_time(spot_series[symbol], t)
            if spot_t is None:
                continue
            strategy = strategies[symbol]
            sigs = strategy.compute(snap, t)
            hist = sig_history[symbol]
            for s in sigs:
                hist.setdefault(s.expiry, []).append((t, s.pred))
            for e in list(hist.keys()):
                hist[e] = [(ti, pi) for ti, pi in hist[e]
                           if (t - ti).total_seconds() <= 6 * 3600]

            for c in sorted(sigs, key=lambda s: abs(s.pred), reverse=True):
                if strategy.gate(c, hist):
                    candidates.append((symbol, c, snap, spot_t))
                    break

        # Enter the strongest candidate that fits in free margin.
        candidates.sort(key=lambda x: abs(x[1].pred), reverse=True)
        for symbol, chosen, snap, spot_t in candidates:
            step = STEP_SIZES[symbol]
            lot_size = LOT_SIZES.get(symbol, 1)
            atm = int(round(chosen.spot / step)) * step
            ce_q = _get_leg_quote(snap, atm, "CE")
            pe_q = _get_leg_quote(snap, atm, "PE")
            if ce_q is None or pe_q is None:
                continue
            combo_val = _combo_value(ce_q, pe_q, chosen.side, use_exit=False)
            if combo_val == 0:
                continue

            margin_per_lot = BACKTEST_MARGIN_FALLBACK_INR.get(symbol, spot_t * lot_size * 0.12)
            free_margin = capital - sum(p.get("margin_used", 0.0) for p in open_positions.values())
            lots = int(free_margin // margin_per_lot) if margin_per_lot > 0 else 0
            if lots <= 0:
                continue
            margin_used = lots * margin_per_lot

            open_positions[symbol] = {
                "symbol": symbol,
                "signal_side": chosen.side,
                "entry_t": t,
                "entry_underlying": spot_t,
                "entry_combo_value": combo_val,
                "strike": atm,
                "expiry": chosen.expiry,
                "capital": capital,
                "margin_used": margin_used,
                "lots": lots,
                "lot_size": lot_size,
                "pred_pct": chosen.pred * 100,
                "n_strikes": chosen.n_strikes,
                "peak": 0.0,
                "exit_combo_value": combo_val,
                "exit_underlying": spot_t,
            }
            break  # only one entry per bucket

    tdf = pd.DataFrame(events)
    metrics = _compute_metrics(tdf, equity, capital)
    metrics["events"] = events
    return metrics


def _spot_at_time(spot_series: pd.Series, t: datetime) -> Optional[float]:
    if t in spot_series.index:
        return float(spot_series.loc[t])
    idx = spot_series.index.get_indexer([t], method="nearest")[0]
    return float(spot_series.iloc[idx]) if idx >= 0 else None


def _compute_metrics(tdf: pd.DataFrame, equity: float, capital: float) -> dict:
    if tdf.empty:
        return {
            "trades": 0,
            "wins": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "total_return_pct": 0.0,
            "profit_factor": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown_pct": 0.0,
            "equity": equity,
            "capital": capital,
        }
    n = len(tdf)
    wins = int((tdf["pnl"] > 0).sum())
    win_rate = wins / n * 100
    total_pnl = float(tdf["pnl"].sum())
    gross_profit = float(tdf.loc[tdf["pnl"] > 0, "pnl"].sum())
    gross_loss = abs(float(tdf.loc[tdf["pnl"] <= 0, "pnl"].sum()))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    avg_win = float(tdf.loc[tdf["pnl"] > 0, "pnl"].mean()) if wins else 0.0
    avg_loss = float(tdf.loc[tdf["pnl"] <= 0, "pnl"].mean()) if (n - wins) else 0.0
    equity_curve = capital + tdf["pnl"].cumsum()
    running_max = equity_curve.cummax()
    drawdowns = (equity_curve - running_max) / running_max
    max_dd = float(drawdowns.min() * 100)
    return {
        "trades": n,
        "wins": wins,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "total_return_pct": total_pnl / capital * 100,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "max_drawdown_pct": max_dd,
        "equity": equity,
        "capital": capital,
    }


def _to_ist(dt) -> str:
    """Convert a tz-aware UTC datetime to IST string."""
    if pd.isna(dt):
        return ""
    if isinstance(dt, str):
        dt = pd.to_datetime(dt)
    if dt.tzinfo is None:
        dt = dt.tz_localize("UTC")
    return dt.tz_convert("Asia/Kolkata").strftime("%Y-%m-%d %H:%M:%S")


def _monthly_table(events: list[dict]) -> pd.DataFrame:
    """Return a month-wise PnL / trade-count summary."""
    tdf = pd.DataFrame(events).copy()
    tdf["entry_t"] = pd.to_datetime(tdf["entry_t"], utc=True)
    tdf["month"] = tdf["entry_t"].dt.tz_convert("Asia/Kolkata").dt.strftime("%Y-%m")
    return tdf.groupby("month").agg(trades=("pnl", "size"), pnl=("pnl", "sum")).reset_index()


def _print_report(symbol: str, metrics: dict):
    print("=" * 70)
    print(f"Synthetic-Forward Backtest - {symbol}")
    print(f"Gate {ENTRY_PCT*100:.2f}%  SL {STOP_LOSS_PCT*100:.1f}%  TP {TARGET_PCT*100:.1f}%  "
          f"costs spread + fees")
    print("=" * 70)
    print(f"  trades    : {metrics['trades']}   wins {metrics['wins']}   "
          f"WR {metrics['win_rate']:.1f}%")
    print(f"  total PnL : INR {metrics['total_pnl']:,.0f}  ({metrics['total_return_pct']:+.2f}%)")
    print(f"  profit factor: {metrics['profit_factor']:.2f}")
    print(f"  avg win   : INR {metrics['avg_win']:,.0f}   avg loss INR {metrics['avg_loss']:,.0f}")
    print(f"  max DD    : {metrics['max_drawdown_pct']:.2f}%")
    print()
    if metrics["events"]:
        tdf = pd.DataFrame(metrics["events"]).copy()
        tdf["entry_ist"] = tdf["entry_t"].apply(_to_ist)
        tdf["exit_ist"] = tdf["exit_t"].apply(_to_ist)
        print("Month-wise:")
        print(_monthly_table(metrics["events"]).to_string(index=False))
        print()
        print("Exits by reason:")
        print(tdf.groupby("reason")["pnl"].agg(["count", "sum", "mean"]).to_string())
        print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="NIFTY", choices=["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"])
    parser.add_argument("--source", default="csv", choices=["csv", "mongo"])
    parser.add_argument("--all", action="store_true", help="run all four symbols")
    parser.add_argument("--shared", action="store_true", help="use one shared capital pool across symbols")
    parser.add_argument("--capital", type=float, default=FIXED_CAPITAL_INR)
    parser.add_argument("--interval", type=int, default=5, help="decision interval minutes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"] if args.all else [args.symbol]
    out_dir = Path(__file__).resolve().parents[2] / "db" / "nse_backtest"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Shared-capital mode: one global simulation across all symbols.
    if args.shared and args.all:
        symbol_dfs = {}
        for sym in symbols:
            try:
                df = load_snapshots_mongo(sym) if args.source == "mongo" else load_snapshots_csv(sym)
                if not df.empty:
                    symbol_dfs[sym] = df
            except FileNotFoundError:
                print(f"[SKIP] {sym}: no data")
        if symbol_dfs:
            metrics = run_backtest_shared(symbol_dfs, capital=args.capital, interval_minutes=args.interval)
            _print_report("SHARED CAPITAL", metrics)
            tdf = pd.DataFrame(metrics["events"]).copy()
            if not tdf.empty:
                tdf["entry_ist"] = tdf["entry_t"].apply(_to_ist)
                tdf["exit_ist"] = tdf["exit_t"].apply(_to_ist)
                cols = ["entry_ist", "exit_ist"] + [c for c in tdf.columns if c not in ("entry_ist", "exit_ist")]
                out_file = out_dir / "SHARED_synth_forward_trades.csv"
                tdf[cols].to_csv(out_file, index=False)
                print(f"  trade log: {out_file}\n")
        return

    all_events = []
    for sym in symbols:
        try:
            df = load_snapshots_mongo(sym) if args.source == "mongo" else load_snapshots_csv(sym)
            if df.empty:
                print(f"[SKIP] {sym}: no data")
                continue
            metrics = run_backtest(sym, df, capital=args.capital, interval_minutes=args.interval)
            _print_report(sym, metrics)
            tdf = pd.DataFrame(metrics["events"]).copy()
            if not tdf.empty:
                tdf["entry_ist"] = tdf["entry_t"].apply(_to_ist)
                tdf["exit_ist"] = tdf["exit_t"].apply(_to_ist)
                cols = ["entry_ist", "exit_ist"] + [c for c in tdf.columns if c not in ("entry_ist", "exit_ist")]
                out_file = out_dir / f"{sym}_synth_forward_trades.csv"
                tdf[cols].to_csv(out_file, index=False)
                print(f"  trade log: {out_file}\n")
                all_events.extend(metrics["events"])
        except FileNotFoundError as e:
            print(f"[SKIP] {sym}: {e}")
        except Exception as e:
            logger.error("Backtest failed for %s: %s", sym, e)
            raise

    if args.all and all_events:
        print("=" * 70)
        print("COMBINED MONTH-WISE SUMMARY (all symbols, independent capital)")
        print("=" * 70)
        combined = pd.DataFrame(all_events).copy()
        combined["entry_t"] = pd.to_datetime(combined["entry_t"], utc=True)
        combined["month"] = combined["entry_t"].dt.tz_convert("Asia/Kolkata").dt.strftime("%Y-%m")
        summary = combined.groupby("month").agg(trades=("pnl", "size"), pnl=("pnl", "sum"))
        print(summary.to_string())
        print(f"\n  Grand total PnL : INR {combined['pnl'].sum():,.0f}")
        print(f"  Return on budget: {combined['pnl'].sum() / args.capital * 100:+.2f}%")
        print("=" * 70)
        print()


if __name__ == "__main__":
    main()
