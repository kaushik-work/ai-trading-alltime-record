"""Backtest engine for naked long-options strategy.

Trades a single ATM option (CE or PE) based on the synthetic-forward signal:
  F < spot  -> buy CE
  F > spot  -> buy PE

Exit rules are premium-based:
  - Stop loss when option loses SL_PCT of entry premium.
  - Target when option gains TP_PCT of entry premium.
  - Trailing stop optional.
  - Max hold or expiry.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from nse.config import (
    ENTRY_PCT,
    LOT_SIZES,
    MAX_HOLD_HOURS,
    SLIPPAGE_BPS,
    STEP_SIZES,
    SYMBOLS,
    TOTAL_CAPITAL_INR,
)
from nse.data.option_chain import load_snapshots_csv, load_snapshots_mongo
from nse.strategies.naked_options import NakedOptionsStrategy
from nse.strategies.greek_naked_options import GreekNakedOptionsStrategy, GreekFilters

logger = logging.getLogger(__name__)

# Strategy dials
MAX_SPREAD_PCT = 0.10      # reject if (ask-bid)/ltp > 10%
SL_PCT = 0.50              # stop loss: lose 50% of entry premium
TP_PCT = 1.00              # target: gain 100% of entry premium
TRAIL_TRIGGER_PCT = 0.50   # activate trailing stop at +50%
TRAIL_GIVEBACK_PCT = 0.25  # trail gives back 25% from peak
BUDGET_PER_TRADE_PCT = 0.20  # use 20% of total capital per trade
FEE_PER_LEG_INR = 40.0     # fixed approx fee per fill


def _bucket_to_interval(df: pd.DataFrame, minutes: int = 5) -> pd.DataFrame:
    df = df.copy()
    df["t_bucket"] = df["timestamp"].dt.floor(f"{minutes}min")
    return df.sort_values("timestamp").drop_duplicates(
        subset=["t_bucket", "expiry", "strike", "side"], keep="last"
    )


def _liquidity_ok(row: pd.Series) -> bool:
    ltp = float(row.get("mark", 0))
    bid = float(row.get("bid", 0) or 0)
    ask = float(row.get("ask", 0) or 0)
    if ltp <= 0:
        return False
    if bid > 0 and ask > 0:
        if (ask - bid) / ltp > MAX_SPREAD_PCT:
            return False
    return True


def _get_leg_quote(snap: pd.DataFrame, strike: int, option_type: str) -> dict | None:
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


def _fees_for_lots(lots: int) -> float:
    return lots * FEE_PER_LEG_INR * 2  # entry + exit


def run_backtest(symbol: str, df: pd.DataFrame, capital: float = TOTAL_CAPITAL_INR,
                 interval_minutes: int = 5, strategy_class = NakedOptionsStrategy,
                 strategy_kwargs: dict | None = None) -> dict:
    df = _bucket_to_interval(df, interval_minutes)
    buckets = sorted(df["t_bucket"].unique())
    if not buckets:
        return {"trades": 0, "pnl": 0.0, "equity": capital, "events": []}

    strategy = strategy_class(symbol, **(strategy_kwargs or {}))
    events = []
    equity = capital
    open_pos = None
    lot_size = LOT_SIZES.get(symbol, 1)
    step = STEP_SIZES.get(symbol, 50)

    spot_series = (df[["timestamp", "spot"]]
                   .dropna().drop_duplicates("timestamp")
                   .set_index("timestamp")["spot"].sort_index())

    for t in buckets:
        snap = df[df["t_bucket"] == t]
        spot_t = _spot_at_time(spot_series, t)
        if spot_t is None:
            continue

        # Manage open position.
        if open_pos:
            q = _get_leg_quote(snap, open_pos["strike"], open_pos["option_type"])
            if q is not None:
                current_px = q["bid"]  # exit at bid
                entry_px = open_pos["entry_px"]
                pnl_pct = (current_px - entry_px) / entry_px if entry_px > 0 else 0.0
                open_pos["peak_pnl_pct"] = max(open_pos.get("peak_pnl_pct", 0.0), pnl_pct)
                open_pos["exit_px"] = current_px

                reason = None
                held_h = (t - open_pos["entry_t"]).total_seconds() / 3600
                if t >= open_pos["expiry"]:
                    reason = "expiry"
                elif held_h >= MAX_HOLD_HOURS:
                    reason = "max_hold"
                elif pnl_pct <= -SL_PCT:
                    reason = "stop"
                elif pnl_pct >= TP_PCT:
                    reason = "target"
                elif (open_pos["peak_pnl_pct"] >= TRAIL_TRIGGER_PCT and
                      open_pos["peak_pnl_pct"] - pnl_pct > TRAIL_GIVEBACK_PCT):
                    reason = "trail"

                if reason:
                    gross = (current_px - entry_px) * open_pos["lots"] * lot_size
                    fees = _fees_for_lots(open_pos["lots"])
                    pnl = gross - fees
                    equity += pnl
                    events.append({
                        "entry_t": open_pos["entry_t"],
                        "exit_t": t,
                        "symbol": symbol,
                        "option_type": open_pos["option_type"],
                        "strike": open_pos["strike"],
                        "entry_px": entry_px,
                        "exit_px": current_px,
                        "lots": open_pos["lots"],
                        "premium_paid": open_pos["premium_paid"],
                        "pnl": pnl,
                        "pnl_pct": pnl / open_pos["premium_paid"] * 100 if open_pos["premium_paid"] else 0,
                        "reason": reason,
                        "equity_after": equity,
                    })
                    open_pos = None

        if open_pos:
            continue

        # Entry logic.
        idea = strategy.compute(snap, t)
        if idea is None:
            continue

        option_type = "CE" if idea["side"] == "call" else "PE"
        strike = idea.get("strike")
        if strike is None:
            strike = int(round(spot_t / step)) * step
        q = _get_leg_quote(snap, strike, option_type)
        if q is None:
            continue

        entry_px = q["ask"]  # buy at ask
        if entry_px <= 0:
            continue

        budget = capital * BUDGET_PER_TRADE_PCT
        premium_per_lot = entry_px * lot_size
        lots = int(budget // premium_per_lot) if premium_per_lot > 0 else 0
        if lots <= 0:
            continue

        open_pos = {
            "symbol": symbol,
            "option_type": option_type,
            "strike": strike,
            "entry_t": t,
            "entry_px": entry_px,
            "expiry": idea["expiry"],
            "lots": lots,
            "premium_paid": premium_per_lot * lots,
            "peak_pnl_pct": 0.0,
            "exit_px": entry_px,
        }

    tdf = pd.DataFrame(events)
    metrics = _compute_metrics(tdf, equity, capital)
    metrics["events"] = events
    return metrics


def _spot_at_time(spot_series: pd.Series, t: datetime) -> float | None:
    if t in spot_series.index:
        return float(spot_series.loc[t])
    idx = spot_series.index.get_indexer([t], method="nearest")[0]
    return float(spot_series.iloc[idx]) if idx >= 0 else None


def _compute_metrics(tdf: pd.DataFrame, equity: float, capital: float) -> dict:
    if tdf.empty:
        return {
            "trades": 0, "wins": 0, "win_rate": 0.0, "total_pnl": 0.0,
            "total_return_pct": 0.0, "profit_factor": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "max_drawdown_pct": 0.0,
            "equity": equity, "capital": capital,
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
        "trades": n, "wins": wins, "win_rate": win_rate,
        "total_pnl": total_pnl, "total_return_pct": total_pnl / capital * 100,
        "profit_factor": profit_factor, "avg_win": avg_win, "avg_loss": avg_loss,
        "max_drawdown_pct": max_dd, "equity": equity, "capital": capital,
    }


def _to_ist(dt) -> str:
    if pd.isna(dt):
        return ""
    if isinstance(dt, str):
        dt = pd.to_datetime(dt)
    if dt.tzinfo is None:
        dt = dt.tz_localize("UTC")
    return dt.tz_convert("Asia/Kolkata").strftime("%Y-%m-%d %H:%M:%S")


def _print_report(symbol: str, metrics: dict):
    print("=" * 70)
    print(f"Naked Long Options Backtest - {symbol}")
    print(f"Gate {ENTRY_PCT*100:.2f}%  SL {SL_PCT*100:.0f}%  TP {TP_PCT*100:.0f}%  budget {BUDGET_PER_TRADE_PCT*100:.0f}%")
    print("=" * 70)
    print(f"  trades    : {metrics['trades']}   wins {metrics['wins']}   WR {metrics['win_rate']:.1f}%")
    print(f"  total PnL : INR {metrics['total_pnl']:,.0f}  ({metrics['total_return_pct']:+.2f}%)")
    print(f"  profit factor: {metrics['profit_factor']:.2f}")
    print(f"  avg win   : INR {metrics['avg_win']:,.0f}   avg loss INR {metrics['avg_loss']:,.0f}")
    print(f"  max DD    : {metrics['max_drawdown_pct']:.2f}%")
    print()
    if metrics["events"]:
        tdf = pd.DataFrame(metrics["events"]).copy()
        tdf["entry_ist"] = tdf["entry_t"].apply(_to_ist)
        tdf["exit_ist"] = tdf["exit_t"].apply(_to_ist)
        print("Exits by reason:")
        print(tdf.groupby("reason")["pnl"].agg(["count", "sum", "mean"]).to_string())
        print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="NIFTY", choices=SYMBOLS)
    parser.add_argument("--source", default="csv", choices=["csv", "mongo"])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--capital", type=float, default=TOTAL_CAPITAL_INR)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--greek", action="store_true", help="use Greek-aware naked strategy")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    symbols = list(SYMBOLS) if args.all else [args.symbol]
    out_dir = Path(__file__).resolve().parents[2] / "db" / "nse_backtest"
    out_dir.mkdir(parents=True, exist_ok=True)

    for sym in symbols:
        try:
            df = load_snapshots_mongo(sym) if args.source == "mongo" else load_snapshots_csv(sym)
            if df.empty:
                print(f"[SKIP] {sym}: no data")
                continue
            strategy_class = GreekNakedOptionsStrategy if args.greek else NakedOptionsStrategy
            metrics = run_backtest(sym, df, capital=args.capital, interval_minutes=args.interval,
                                   strategy_class=strategy_class)
            _print_report(sym, metrics)
            tdf = pd.DataFrame(metrics["events"]).copy()
            if not tdf.empty:
                tdf["entry_ist"] = tdf["entry_t"].apply(_to_ist)
                tdf["exit_ist"] = tdf["exit_t"].apply(_to_ist)
                cols = ["entry_ist", "exit_ist"] + [c for c in tdf.columns if c not in ("entry_ist", "exit_ist")]
                out_file = out_dir / f"{sym}_naked_options_trades.csv"
                tdf[cols].to_csv(out_file, index=False)
                print(f"  trade log: {out_file}\n")
        except FileNotFoundError as e:
            print(f"[SKIP] {sym}: {e}")
        except Exception as e:
            logger.error("Backtest failed for %s: %s", sym, e)
            raise


if __name__ == "__main__":
    main()
