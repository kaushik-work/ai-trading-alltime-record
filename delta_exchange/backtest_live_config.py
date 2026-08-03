"""Backtest the LIVE price-action S/R config, month by month.

The point of this file is parity. Every dial is imported from the production
modules rather than restated here, so the backtest cannot silently drift from
what the bot actually does:

    core.risk_management        LEVERAGE, FIXED_CAPITAL_INR, USD_INR_RATE, EXIT_REGIME
    strategies.price_action_sr  lookbacks, zone, body/wick gates, SL/RR per asset,
                                max-hold, cooldown, block-after-loss, vol filter

Differences from backtest_price_action_sweep.py, all of them deliberate:

  1. Sizing is LIVE sizing. Live deploys a FIXED Rs 50k notional per trade, so
     P&L is Rs 50k x price-return. The sweep compounds a fraction of equity,
     which produces a different equity curve and a different MaxDD.
     Note: at fixed notional, LEVERAGE does not change P&L at all - it only
     sets the margin locked and the liquidation distance.

  2. Exit regime is pure_sltp, matching EXIT_REGIME. The sweep runs with
     trail_be=True, so its headline numbers include a breakeven trail that
     production does not execute.

  3. Exit slippage is charged. The sweep applies slippage on entry only and
     fills exits exactly at the stop/target.

  4. Equity is marked to market on every bar, so MaxDD includes open-trade
     drawdown. The sweep only samples equity on trade close, which understates
     drawdown.

  5. Liquidation is modelled: at LEVERAGE with Delta's 0.25% maintenance
     margin, a position dies at (1/LEV - 0.0025) adverse. With a 0.7% stop
     this should never trigger, but a gap can jump the stop.

Fees are reported BOTH ways - gross and net - because the fee decision is the
user's, and the size of the gap is the thing worth seeing.

Usage:
    python backtest_live_config.py                    # all assets, 6m data
    python backtest_live_config.py --subdir 6m --symbols ETHUSD
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd

import backtest_price_action_sweep as sweep
from strategies import price_action_sr as live
from core.risk_management import (
    LEVERAGE, FIXED_CAPITAL_INR, USD_INR_RATE, EXIT_REGIME,
)

# Delta BTC/ETH perps: 0.5% initial, 0.25% maintenance margin.
MAINTENANCE_MARGIN = 0.0025
# Round-trip taker fee, in bps per side. Reported separately, never silently applied.
FEE_BPS_PER_SIDE = 5.0
# Adverse slippage on every fill, in bps.
SLIPPAGE_BPS = 2.0


def sync_sweep_to_live() -> None:
    """Force the vectorised signal builder onto the live strategy's dials."""
    sweep.LOOKBACK = live.LOOKBACK_CANDLES
    sweep.TREND_LOOKBACK = live.TREND_CANDLES
    sweep.RANGE_PCT_MAX = live.RANGE_PCT_MAX
    sweep.RANGE_PCT_MIN = live.RANGE_PCT_MIN
    sweep.ZONE_PCT = live.ZONE_PCT
    sweep.BODY_MULT = live.BODY_MULT
    sweep.WICK_RATIO_MAX = live.WICK_RATIO_MAX


def live_dials_for(symbol: str) -> dict:
    """SL / RR / vol filter exactly as the live strategy resolves them."""
    underlying = symbol.replace("USD", "")
    d = live.ASSET_DIALS.get(underlying,
                             {"sl_pct": live.SL_PCT, "rr_ratio": live.RR_RATIO})
    vol_max = 0.0
    if underlying == "ETH":
        vol_max = live.ETHPriceActionSRSignal.vol_filter_max
    elif underlying == "BTC":
        vol_max = live.BTCPriceActionSRSignal.vol_filter_max
    return {"sl_pct": d["sl_pct"], "rr_ratio": d["rr_ratio"],
            "vol_filter_max": vol_max, "tuned": underlying in live.ASSET_DIALS}


def run(df: pd.DataFrame, sl_pct: float, rr: float, vol_filter_max: float) -> dict:
    """Walk the bars under the LIVE exit regime. Returns trades + equity curve."""
    sync_sweep_to_live()
    s = sweep.prepare(
        df, use_trend=True,
        retest_mode=live.RETEST_MODE,
        body_pos_threshold=live.BODY_POS_THRESHOLD,
        wick_touch_tol=live.WICK_TOUCH_TOL,
        min_volume_mult=live.MIN_VOLUME_MULT,
        rsi_period=live.RSI_PERIOD,
        rsi_long_max=live.RSI_LONG_MAX,
        rsi_short_min=live.RSI_SHORT_MIN,
        trend_slope_candles=live.TREND_SLOPE_CANDLES,
        trend_slope_min_pct=live.TREND_SLOPE_MIN_PCT,
        range_pct_min=live.RANGE_PCT_MIN,
        trading_hours=live.TRADING_HOURS,
        htf_align=live.HTF_ALIGN,
        htf_1h_slope_min_pct=live.HTF_1H_SLOPE_MIN_PCT,
        vol_filter_max=vol_filter_max,
        require_engulfing=live.REQUIRE_ENGULFING,
        pin_bar_wick_ratio=live.PIN_BAR_WICK_RATIO,
    )
    o, h, l, c = s["o"], s["h"], s["l"], s["c"]
    long_sig, short_sig = s["retest_long"], s["retest_short"]
    ts = df.index
    n = len(df)

    notional = FIXED_CAPITAL_INR / USD_INR_RATE           # USD per trade, fixed
    liq_move = (1.0 / LEVERAGE) - MAINTENANCE_MARGIN
    slip = SLIPPAGE_BPS / 10_000.0

    pnl_usd = 0.0
    trades: list[dict] = []
    mtm_curve: list[float] = []                            # marked every bar
    pos = None
    cooldown = -1
    block_long_until = -1
    block_short_until = -1

    start_i = max(live.LOOKBACK_CANDLES, live.TREND_CANDLES) + 10

    for i in range(start_i, n - 1):
        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            hi, lo, ci = h[i], l[i], c[i]
            exit_px = None
            reason = None

            # Liquidation first: a gap can jump straight past the stop.
            liq_px = pos["entry"] * (1 - sign * liq_move)
            if (sign > 0 and lo <= liq_px) or (sign < 0 and hi >= liq_px):
                exit_px, reason = liq_px, "liquidated"
            # Stop before target: the stop is far nearer, so on a bar that
            # spans both it is overwhelmingly the one that filled first.
            elif (sign > 0 and lo <= pos["sl"]) or (sign < 0 and hi >= pos["sl"]):
                exit_px, reason = pos["sl"], "stop"
            elif (sign > 0 and hi >= pos["tp"]) or (sign < 0 and lo <= pos["tp"]):
                exit_px, reason = pos["tp"], "target"
            elif i - pos["entry_idx"] >= live.MAX_HOLD_MINUTES:
                exit_px, reason = ci, "max_hold"

            if exit_px is not None:
                fill = exit_px * (1 - sign * slip)          # adverse on exit
                ret = sign * (fill - pos["entry"]) / pos["entry"]
                pnl_usd += ret * notional
                trades.append({**pos, "exit": fill, "exit_time": ts[i],
                               "ret": ret, "pnl_usd": ret * notional,
                               "reason": reason,
                               "held_min": i - pos["entry_idx"]})
                pos = None
                cooldown = i + live.COOLDOWN_MINUTES
                if ret <= 0 and live.BLOCK_AFTER_LOSS_MINUTES > 0:
                    if sign > 0: block_long_until = i + live.BLOCK_AFTER_LOSS_MINUTES
                    else:        block_short_until = i + live.BLOCK_AFTER_LOSS_MINUTES
                mtm_curve.append(pnl_usd)
                continue

            unreal = sign * (c[i] - pos["entry"]) / pos["entry"]
            mtm_curve.append(pnl_usd + unreal * notional)
            continue

        mtm_curve.append(pnl_usd)
        if i < cooldown:
            continue

        nxt = o[i + 1]
        if long_sig[i] and i >= block_long_until:
            entry = nxt * (1 + slip)
            stop_level = l[i] * (1 - slip)
            dist = max(sl_pct, (entry - stop_level) / entry)
            pos = {"side": "long", "entry": entry, "sl": entry * (1 - dist),
                   "tp": entry * (1 + dist * rr), "entry_idx": i + 1,
                   "entry_time": ts[i + 1], "sl_dist": dist}
        elif short_sig[i] and i >= block_short_until:
            entry = nxt * (1 - slip)
            stop_level = h[i] * (1 + slip)
            dist = max(sl_pct, (stop_level - entry) / entry)
            pos = {"side": "short", "entry": entry, "sl": entry * (1 + dist),
                   "tp": entry * (1 - dist * rr), "entry_idx": i + 1,
                   "entry_time": ts[i + 1], "sl_dist": dist}

    return {"trades": trades, "curve": np.array(mtm_curve), "notional": notional}


def metrics(trades: list[dict], curve: np.ndarray, notional: float) -> dict:
    if not trades:
        return {"trades": 0}
    rets = np.array([t["ret"] for t in trades])
    gross = np.array([t["pnl_usd"] for t in trades])
    fee_per_trade = 2 * FEE_BPS_PER_SIDE / 10_000.0 * notional
    net = gross - fee_per_trade

    wins, losses = gross[gross > 0], gross[gross <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    nw, nl = net[net > 0], net[net <= 0]
    pf_net = nw.sum() / abs(nl.sum()) if nl.sum() != 0 else float("inf")

    peak = np.maximum.accumulate(curve) if len(curve) else np.array([0.0])
    dd_usd = float((peak - curve).max()) if len(curve) else 0.0

    max_cl = cl = 0
    for g in gross:
        cl = cl + 1 if g <= 0 else 0
        max_cl = max(max_cl, cl)

    return {
        "trades": len(trades),
        "wr": len(wins) / len(trades) * 100,
        "pf": pf, "pf_net": pf_net,
        "gross_usd": float(gross.sum()), "net_usd": float(net.sum()),
        "gross_inr": float(gross.sum()) * USD_INR_RATE,
        "net_inr": float(net.sum()) * USD_INR_RATE,
        "fees_inr": float(fee_per_trade * len(trades)) * USD_INR_RATE,
        "maxdd_inr": dd_usd * USD_INR_RATE,
        "max_cl": max_cl,
        "liquidations": sum(1 for t in trades if t["reason"] == "liquidated"),
        "avg_win_pct": float(rets[rets > 0].mean() * 100) if (rets > 0).any() else 0.0,
        "avg_loss_pct": float(rets[rets <= 0].mean() * 100) if (rets <= 0).any() else 0.0,
        "reasons": pd.Series([t["reason"] for t in trades]).value_counts().to_dict(),
    }


def monthly(trades: list[dict], notional: float) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    fee = 2 * FEE_BPS_PER_SIDE / 10_000.0 * notional * USD_INR_RATE
    rows = []
    for t in trades:
        rows.append({"month": pd.Timestamp(t["entry_time"]).strftime("%Y-%m"),
                     "inr": t["pnl_usd"] * USD_INR_RATE,
                     "win": t["ret"] > 0})
    df = pd.DataFrame(rows)
    g = df.groupby("month").agg(trades=("inr", "size"), wins=("win", "sum"),
                                gross_inr=("inr", "sum"))
    g["wr"] = g["wins"] / g["trades"] * 100
    g["fees_inr"] = g["trades"] * fee
    g["net_inr"] = g["gross_inr"] - g["fees_inr"]
    return g


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--subdir", default="6m")
    p.add_argument("--symbols", nargs="+", default=["BTCUSD", "ETHUSD", "XAUTUSD"])
    args = p.parse_args()

    print("=" * 92)
    print("LIVE-CONFIG BACKTEST — dials imported from production modules")
    print("=" * 92)
    print(f"  exit regime      {EXIT_REGIME}  (no breakeven trail)")
    print(f"  sizing           Rs {FIXED_CAPITAL_INR:,.0f} FIXED notional per trade "
          f"@ {LEVERAGE}x  -> margin Rs {FIXED_CAPITAL_INR / LEVERAGE:,.0f}")
    print(f"  liquidation at   {((1 / LEVERAGE) - MAINTENANCE_MARGIN) * 100:.2f}% adverse")
    print(f"  retest           {live.RETEST_MODE}, tol {live.WICK_TOUCH_TOL * 1e4:.0f}bps, "
          f"body_pos {live.BODY_POS_THRESHOLD}")
    print(f"  max hold {live.MAX_HOLD_MINUTES}m · cooldown {live.COOLDOWN_MINUTES}m · "
          f"block-after-loss {live.BLOCK_AFTER_LOSS_MINUTES}m")
    print(f"  slippage {SLIPPAGE_BPS:.0f}bps each way · fees {FEE_BPS_PER_SIDE:.0f}bps/side "
          f"(shown separately)")
    print()

    for sym in args.symbols:
        d = live_dials_for(sym)
        try:
            df = sweep.load_perp(args.subdir, sym)
        except Exception as e:
            print(f"--- {sym}: no data ({e})\n")
            continue

        r = run(df, d["sl_pct"], d["rr_ratio"], d["vol_filter_max"])
        m = metrics(r["trades"], r["curve"], r["notional"])
        tag = "" if d["tuned"] else "  [NO TUNED DIALS — using defaults]"
        print("-" * 92)
        print(f"{sym}  SL {d['sl_pct'] * 100:.2f}%  RR 1:{d['rr_ratio']:.0f}  "
              f"vol_filter {d['vol_filter_max'] or 'off'}{tag}")
        print(f"  data {df.index.min():%Y-%m-%d} -> {df.index.max():%Y-%m-%d} "
              f"({len(df):,} bars)")
        if m["trades"] == 0:
            print("  no trades\n")
            continue
        print(f"  trades {m['trades']}  WR {m['wr']:.1f}%  PF {m['pf']:.2f} "
              f"(net {m['pf_net']:.2f})  maxCL {m['max_cl']}  liquidations {m['liquidations']}")
        print(f"  GROSS Rs {m['gross_inr']:>12,.0f}    fees Rs {m['fees_inr']:>10,.0f}"
              f"    NET Rs {m['net_inr']:>12,.0f}")
        print(f"  MaxDD (mark-to-market) Rs {m['maxdd_inr']:,.0f}   "
              f"avg win {m['avg_win_pct']:+.2f}%  avg loss {m['avg_loss_pct']:+.2f}%")
        print(f"  exits: {m['reasons']}")

        mo = monthly(r["trades"], r["notional"])
        if not mo.empty:
            print(f"\n  {'month':9}{'trades':>7}{'WR':>8}{'gross Rs':>13}{'fees Rs':>10}{'net Rs':>13}")
            for month, row in mo.iterrows():
                print(f"  {month:9}{int(row['trades']):>7}{row['wr']:>7.0f}%"
                      f"{row['gross_inr']:>13,.0f}{row['fees_inr']:>10,.0f}{row['net_inr']:>13,.0f}")
        print()


if __name__ == "__main__":
    main()
