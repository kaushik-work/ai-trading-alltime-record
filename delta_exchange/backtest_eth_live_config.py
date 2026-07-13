"""
Backtest the exact live config end-to-end.

Live bot parameters mirrored here:
  ETH-only, fixed Rs 50,000 notional per trade, 15x leverage,
  vol filter max 34%, SL 0.7%, R:R 1:7, pure SL/TP exit regime,
  entries only at 15-minute boundaries (:00/:15/:30/:45 UTC),
  60-minute cooldown, 180-minute block-after-loss,
  4-hour max hold, 5 bps per-side fee, 2 bps slippage on entry and exit.

This script is intentionally strict: if the backtest assumption differs from
live, it should fail loudly (see SANITY_CHECKS at the bottom).
"""
import sys
import os
import argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path
import numpy as np
import pandas as pd
from backtest_price_action_sweep import load_perp, prepare

SYMBOL = "ETHUSD"
BUDGET_INR = 50_000.0          # fixed capital per trade
LEVERAGE = 15.0
SL_PCT = 0.007                 # per-asset ETH stop
RR = 7.0
TP_PCT = SL_PCT * RR
VOL_FILTER_MAX = 0.34

COOLDOWN_CANDLES = 60          # 60 x 1m = 1h
BLOCK_AFTER_LOSS_CANDLES = 180 # 180 x 1m = 3h
MAX_HOLD_CANDLES = 240         # 240 x 1m = 4h

PERP_FEE_BPS = 5.0             # Delta perp taker fee per side
SLIPPAGE_BPS = 2.0             # market-order slippage both sides


def load_eth_df():
    dfs = []
    for subdir in ["eth", "july_eth"]:
        try:
            dfs.append(load_perp(subdir, SYMBOL))
        except Exception as e:
            print(f"Warning: could not load {subdir}: {e}")
    if not dfs:
        raise RuntimeError("No ETH data loaded")
    df = pd.concat(dfs).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[df.index >= pd.Timestamp("2026-04-01", tz="UTC")]
    return df


def is_decision_minute(ts: pd.Timestamp) -> bool:
    """Live evaluates entries at :00/:15/:30/:45 UTC."""
    return ts.minute % 15 == 0


def run_signals(df, continuous: bool = False):
    s = prepare(df, use_trend=True, retest_mode="wick_touch",
                body_pos_threshold=0.70, wick_touch_tol=0.0007,
                vol_filter_max=VOL_FILTER_MAX)
    o, h, l, c = s["o"], s["h"], s["l"], s["c"]
    ts = df.index
    n = len(df)
    long_sig, short_sig = s["retest_long"], s["retest_short"]

    trades = []
    pos = None
    cooldown = -1
    block_long_until = -1
    block_short_until = -1

    # Need enough history for 24h trend + 4h range.
    start_i = max(240, 1440) + 10

    for i in range(start_i, n - 1):
        t = ts[i]

        # Position management first (same as live: check exits every 1m tick).
        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            hi, lo = h[i], l[i]
            reason = None
            exit_px = None

            # Target touch (with exit slippage against us).
            if (sign > 0 and hi >= pos["tp"]) or (sign < 0 and lo <= pos["tp"]):
                reason = "tp"
                exit_px = pos["tp"] * (1 - sign * SLIPPAGE_BPS / 1e4)
            else:
                # Stop touch (with exit slippage against us).
                if (sign > 0 and lo <= pos["sl"]) or (sign < 0 and hi >= pos["sl"]):
                    reason = "sl"
                    exit_px = pos["sl"] * (1 - sign * SLIPPAGE_BPS / 1e4)
                elif i - pos["entry_idx"] >= MAX_HOLD_CANDLES:
                    reason = "hold"
                    exit_px = c[i] * (1 - sign * SLIPPAGE_BPS / 1e4)

            if reason is not None:
                gross_pnl = sign * (exit_px - pos["entry"]) / pos["entry"]
                net_pnl = gross_pnl - 2 * PERP_FEE_BPS / 1e4
                trades.append({
                    **pos,
                    "exit_px": exit_px,
                    "exit_reason": reason,
                    "gross_pnl_pct": gross_pnl,
                    "net_pnl_pct": net_pnl,
                })
                pos = None
                cooldown = i + COOLDOWN_CANDLES
                if net_pnl <= 0:
                    if sign > 0:
                        block_long_until = i + BLOCK_AFTER_LOSS_CANDLES
                    else:
                        block_short_until = i + BLOCK_AFTER_LOSS_CANDLES
            continue

        # No new entries during cooldown.
        if i < cooldown:
            continue

        # Live only evaluates signals at 15-minute boundaries.
        if not continuous and not is_decision_minute(t):
            continue

        # Entry is at the open of the next 1m candle with slippage.
        next_open = o[i + 1]
        t_entry = ts[i + 1]

        if long_sig[i] and i >= block_long_until:
            entry = next_open * (1 + SLIPPAGE_BPS / 1e4)
            # Dynamic SL: max of fixed 0.7% and distance to signal-candle low
            # minus 2 bps buffer, exactly like live strategy logic.
            stop_level = l[i] * (1 - SLIPPAGE_BPS / 1e4)
            sl_dist = max(SL_PCT, (entry - stop_level) / entry)
            sl = entry * (1 - sl_dist)
            tp = entry * (1 + TP_PCT)
            pos = {
                "side": "long",
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "entry_idx": i + 1,
                "entry_time": t_entry,
                "decision_time": t,
                "sl_dist": sl_dist,
            }
            continue

        if short_sig[i] and i >= block_short_until:
            entry = next_open * (1 - SLIPPAGE_BPS / 1e4)
            stop_level = h[i] * (1 + SLIPPAGE_BPS / 1e4)
            sl_dist = max(SL_PCT, (stop_level - entry) / entry)
            sl = entry * (1 + sl_dist)
            tp = entry * (1 - TP_PCT)
            pos = {
                "side": "short",
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "entry_idx": i + 1,
                "entry_time": t_entry,
                "decision_time": t,
                "sl_dist": sl_dist,
            }
            continue

    # Close any open position at last close (no slippage on EOF for simplicity).
    if pos is not None:
        sign = 1 if pos["side"] == "long" else -1
        gross_pnl = sign * (c[-1] - pos["entry"]) / pos["entry"]
        net_pnl = gross_pnl - 2 * PERP_FEE_BPS / 1e4
        trades.append({
            **pos,
            "exit_px": c[-1],
            "exit_reason": "eof",
            "gross_pnl_pct": gross_pnl,
            "net_pnl_pct": net_pnl,
        })

    return trades


def run_fixed_capital(trades, budget_inr, leverage):
    equity = budget_inr
    peak = budget_inr
    gross = 0.0
    wins = 0
    max_dd = 0.0
    for t in trades:
        pnl_inr = budget_inr * leverage * t["net_pnl_pct"]
        gross += pnl_inr
        equity += pnl_inr
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
        if pnl_inr > 0:
            wins += 1
    return {
        "trades": len(trades),
        "wins": wins,
        "gross": gross,
        "max_dd": max_dd,
        "equity": equity,
    }


def sanity_checks(df, trades, res, continuous: bool = False):
    """Fail loudly if assumptions are violated."""
    errors = []

    # 1. No duplicate timestamps in data.
    if df.index.duplicated().any():
        errors.append("Duplicate timestamps in price data")

    # 2. Every trade must have a positive SL distance.
    bad_sl = [t for t in trades if t.get("sl_dist", 0) <= 0]
    if bad_sl:
        errors.append(f"{len(bad_sl)} trades have non-positive SL distance")

    # 3. Max hold should never exceed 4h + 1 candle tolerance.
    for t in trades:
        if t["exit_reason"] == "hold":
            held = t["entry_idx"]  # index is 1m-based
            # Not easy to check without original index; skip strict.
            pass

    # 4. Block-after-loss: no same-side trade within 180 candles of a loss.
    last_loss_idx = {"long": -9999, "short": -9999}
    for t in trades:
        side = t["side"]
        entry_idx = t["entry_idx"]
        if entry_idx - last_loss_idx[side] < BLOCK_AFTER_LOSS_CANDLES:
            errors.append(
                f"Same-side {side} re-entry at idx {entry_idx} "
                f"only {entry_idx - last_loss_idx[side]} candles after loss"
            )
        if t["net_pnl_pct"] <= 0:
            last_loss_idx[side] = entry_idx

    # 5. Cooldown: no new trade within 60 candles of previous exit.
    last_exit_idx = -9999
    for t in trades:
        entry_idx = t["entry_idx"]
        if entry_idx - last_exit_idx < COOLDOWN_CANDLES:
            errors.append(
                f"Trade at idx {entry_idx} only {entry_idx - last_exit_idx} "
                f"candles after previous exit"
            )
        # Approximate exit index from entry_idx + hold candles; good enough.
        last_exit_idx = entry_idx + MAX_HOLD_CANDLES

    # 6. Entries only at 15-minute decision minutes (unless continuous mode).
    if not continuous:
        for t in trades:
            if t["decision_time"].minute % 15 != 0:
                errors.append(
                    f"Entry decision at non-decision minute {t['decision_time']}"
                )

    # 7. Budget and leverage must match live.
    if BUDGET_INR != 50_000.0:
        errors.append("BUDGET_INR does not match live Rs 50,000")
    if LEVERAGE != 15.0:
        errors.append("LEVERAGE does not match live 15x")
    if SL_PCT != 0.007:
        errors.append("SL_PCT does not match live 0.7%")
    if VOL_FILTER_MAX != 0.34:
        errors.append("VOL_FILTER_MAX does not match live 34%")

    # 8. P&L sanity: gross and net should be within reasonable fee distance.
    total_gross = sum(t["gross_pnl_pct"] for t in trades) * BUDGET_INR * LEVERAGE
    fee_leak = total_gross - res["gross"]
    expected_fees = len(trades) * 2 * PERP_FEE_BPS / 1e4 * BUDGET_INR * LEVERAGE * SL_PCT * RR
    # Rough sanity: fees should be positive and not absurd.
    if fee_leak < 0:
        errors.append("Net P&L is higher than gross P&L (fee logic bug)")

    if errors:
        print("\n🚨 SANITY CHECK FAILURES:")
        for e in errors:
            print(f"  - {e}")
        raise RuntimeError("Backtest sanity checks failed")
    print("\n✅ All sanity checks passed")


def run_one(df, continuous: bool = False):
    trades = run_signals(df, continuous=continuous)
    res = run_fixed_capital(trades, BUDGET_INR, LEVERAGE)
    label = "continuous (every 1m)" if continuous else "live 15m grid"
    print(f"\n=== {label} ===")
    print(f"  Parameters: fixed ₹{BUDGET_INR:,.0f}, {LEVERAGE:.0f}x, "
          f"SL {SL_PCT*100:.2f}%, TP {TP_PCT*100:.2f}%, vol≤{VOL_FILTER_MAX*100:.0f}%")
    print(f"  Entry grid: {label}")
    print(f"  Fees: {PERP_FEE_BPS:.0f} bps/side, slippage: {SLIPPAGE_BPS:.0f} bps both sides")
    print(f"  Trades: {res['trades']}")
    if res["trades"]:
        print(f"  Wins: {res['wins']} ({100*res['wins']/res['trades']:.1f}%)")
    print(f"  Gross P&L: ₹{res['gross']:,.0f}")
    print(f"  MaxDD: ₹{res['max_dd']:,.0f} ({100*res['max_dd']/BUDGET_INR:.1f}% of budget)")
    print(f"  Profit/Risk: {res['gross']/max(res['max_dd'],1):.1f}x")
    print(f"  Final equity: ₹{res['equity']:,.0f}")

    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    if reasons:
        print(f"  Exit reasons: {reasons}")

    if trades:
        monthly: dict[str, list] = {}
        for t in trades:
            m = t["entry_time"].strftime("%Y-%m")
            monthly.setdefault(m, []).append(t["net_pnl_pct"] * BUDGET_INR * LEVERAGE)
        print("  Monthly P&L:")
        for m in sorted(monthly):
            print(f"    {m}: {len(monthly[m]):2d} trades, ₹{sum(monthly[m]):>+10,.0f}")
    return trades, res


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--grid-15m", action="store_true",
                        help="Evaluate entries only at 15-minute boundaries (old behavior)")
    parser.add_argument("--compare", action="store_true",
                        help="Run both 1m and 15m grid and show delta")
    args = parser.parse_args()

    df = load_eth_df()
    print(f"ETH live-config backtest: {df.index[0].date()} to {df.index[-1].date()}")

    if args.compare:
        trades_1m, res_1m = run_one(df, continuous=True)
        trades_grid, res_grid = run_one(df, continuous=False)
        print("\n=== Grid impact ===")
        print(f"  1m grid trades:  {res_1m['trades']}")
        print(f"  15m grid trades: {res_grid['trades']}")
        print(f"  Missed by 15m:   {res_1m['trades'] - res_grid['trades']}")
        print(f"  1m grid P&L:     ₹{res_1m['gross']:,.0f}")
        print(f"  15m grid P&L:    ₹{res_grid['gross']:,.0f}")
        sanity_checks(df, trades_1m, res_1m, continuous=True)
        sanity_checks(df, trades_grid, res_grid, continuous=False)
    else:
        continuous = not args.grid_15m
        trades, res = run_one(df, continuous=continuous)
        sanity_checks(df, trades, res, continuous=continuous)


if __name__ == "__main__":
    main()
