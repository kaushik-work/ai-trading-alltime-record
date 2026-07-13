"""
ICT / Smart Money Concepts strategy for ETHUSD perp.

Core concepts used:
  1. Market Structure: swing highs/lows on 15m; BOS (continuation) / CHOCH (reversal).
  2. Liquidity Sweep: price takes out a recent swing high/low then reverses.
  3. Order Block: the last opposing candle before the sweep impulse.
  4. Fair Value Gap: 3-candle imbalance used for entry confluence.
  5. Kill Zones: London (07-10 UTC) and NY (13-16 UTC) sessions.

Entry rules (long example):
  - In an overall bullish structure (higher highs, higher lows).
  - Price sweeps below a recent swing low (takes liquidity).
  - The sweep candle is followed by a strong bullish reversal candle.
  - The reversal leaves a bull FVG or returns to a fresh bullish order block.
  - Time is inside a kill zone.
  → Enter long at the FVG/order-block retest, SL below the sweep low, TP at next swing high.

All calculations use 15m candles built from 1m mark data.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
import numpy as np
import pandas as pd

from backtest_price_action_sweep import load_perp

SYMBOL = "ETHUSD"
BUDGET_INR = 50_000.0
LEVERAGE = 15.0
PERP_FEE_BPS = 5.0
SLIPPAGE_BPS = 2.0

# ICT dials
SWING_LOOKBACK = 5          # bars on each side for swing high/low
KILL_ZONES = [(7, 10), (13, 16)]  # UTC hour ranges
MIN_IMPULSE_BODY_PCT = 0.0015  # sweep reversal candle body >= 0.15%
MAX_HOLD_CANDLES = 16       # 16 x 15m = 4h


def load_eth_15m():
    dfs = []
    for subdir in ["eth", "july_eth"]:
        try:
            df = load_perp(subdir, SYMBOL)
            dfs.append(df)
        except Exception:
            pass
    df = pd.concat(dfs).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[df.index >= pd.Timestamp("2026-04-01", tz="UTC")]
    # Resample to 15m
    bars = df.resample("15min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "real_volume": "sum",
    }).dropna()
    return bars


def detect_swings(o, h, l, c, lookback):
    """Return indices of swing highs and lows."""
    n = len(c)
    swing_highs = []
    swing_lows = []
    for i in range(lookback, n - lookback):
        if h[i] == max(h[i-lookback:i+lookback+1]):
            swing_highs.append(i)
        if l[i] == min(l[i-lookback:i+lookback+1]):
            swing_lows.append(i)
    return swing_highs, swing_lows


def detect_fvg(o, h, l, c):
    """Detect bullish and bearish FVGs. Returns list of (idx, top, bottom, side)."""
    n = len(c)
    bull = []
    bear = []
    for i in range(1, n - 1):
        # Bull FVG: current low > previous high
        if l[i] > h[i-1]:
            bull.append((i, l[i], h[i-1], "bull"))
        # Bear FVG: current high < previous low
        if h[i] < l[i-1]:
            bear.append((i, l[i-1], h[i], "bear"))
    return bull + bear


def detect_order_blocks(o, h, l, c, swing_highs, swing_lows):
    """
    Find order blocks: the last opposing candle before a strong impulse that
    created a swing high/low.
    Bullish OB: last bearish candle before a swing low → strong up move.
    Bearish OB: last bullish candle before a swing high → strong down move.
    """
    obs = []
    # Bullish OBs: before swing lows
    for si in swing_lows:
        if si < 2:
            continue
        # Find the last bearish candle before the swing low that started the up move
        for j in range(si - 1, max(si - 10, 0), -1):
            if c[j] < o[j]:  # bearish candle
                obs.append({
                    "idx": j, "top": max(o[j], c[j]), "bottom": min(o[j], c[j]),
                    "side": "bull",
                })
                break
    # Bearish OBs: before swing highs
    for si in swing_highs:
        if si < 2:
            continue
        for j in range(si - 1, max(si - 10, 0), -1):
            if c[j] > o[j]:  # bullish candle
                obs.append({
                    "idx": j, "top": max(o[j], c[j]), "bottom": min(o[j], c[j]),
                    "side": "bear",
                })
                break
    return obs


def in_kill_zone(ts: pd.Timestamp) -> bool:
    hour = ts.hour
    for start, end in KILL_ZONES:
        if start <= hour < end:
            return True
    return False


def run_ict():
    bars = load_eth_15m()
    ts = bars.index
    o = bars["open"].values
    h = bars["high"].values
    l = bars["low"].values
    c = bars["close"].values
    n = len(c)

    swing_highs, swing_lows = detect_swings(o, h, l, c, SWING_LOOKBACK)
    fvgs = detect_fvg(o, h, l, c)
    obs = detect_order_blocks(o, h, l, c, swing_highs, swing_lows)

    # Build recent swing levels for liquidity sweep detection
    recent_swings_high = {}
    recent_swings_low = {}
    for idx in swing_highs:
        recent_swings_high[idx] = h[idx]
    for idx in swing_lows:
        recent_swings_low[idx] = l[idx]

    equity = BUDGET_INR
    peak = equity
    trades = []
    pos = None
    cooldown = -1

    for i in range(50, n - 1):
        t = ts[i]

        # Exit management on 15m close (approximate bracket orders)
        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            exit_px = None
            reason = None
            if (sign > 0 and h[i] >= pos["tp"]) or (sign < 0 and l[i] <= pos["tp"]):
                exit_px = pos["tp"]
                reason = "tp"
            elif (sign > 0 and l[i] <= pos["sl"]) or (sign < 0 and h[i] >= pos["sl"]):
                exit_px = pos["sl"]
                reason = "sl"
            elif i - pos["entry_idx"] >= MAX_HOLD_CANDLES:
                exit_px = c[i]
                reason = "hold"

            if reason is not None:
                fill = exit_px * (1 - sign * SLIPPAGE_BPS / 1e4)
                gross = sign * (fill - pos["entry"]) / pos["entry"]
                net = gross - 2 * PERP_FEE_BPS / 1e4
                pnl = BUDGET_INR * LEVERAGE * net
                equity += pnl
                peak = max(peak, equity)
                trades.append({
                    "side": pos["side"], "entry": pos["entry"], "exit": fill,
                    "reason": reason, "pnl": pnl, "net_pct": net,
                    "entry_time": ts[pos["entry_idx"]], "exit_time": t,
                })
                pos = None
                cooldown = i + 1
            continue

        if i < cooldown:
            continue

        if not in_kill_zone(t):
            continue

        # Detect liquidity sweep + reversal
        # Long: current candle takes out a recent swing low (l[i] < prev swing low)
        #       but closes strong bullish near high
        # Short: current candle takes out a recent swing high, closes strong bearish

        # Find recent swing low within last 20 bars
        recent_low_idx = None
        recent_low_val = float("inf")
        for idx, val in recent_swings_low.items():
            if 0 < i - idx <= 20 and val < recent_low_val:
                recent_low_val = val
                recent_low_idx = idx

        recent_high_idx = None
        recent_high_val = 0
        for idx, val in recent_swings_high.items():
            if 0 < i - idx <= 20 and val > recent_high_val:
                recent_high_val = val
                recent_high_idx = idx

        body = abs(c[i] - o[i])
        rng = h[i] - l[i]
        close_pos = (c[i] - l[i]) / rng if rng > 0 else 0.5

        # LONG setup
        if (recent_low_idx is not None and
            l[i] < recent_low_val and
            c[i] > o[i] and
            body / c[i] >= MIN_IMPULSE_BODY_PCT and
            close_pos >= 0.70):

            # Check if price is near a bullish FVG or OB
            entry_zone_top = c[i]
            entry_zone_bot = o[i]

            # Look for overlapping bullish FVG or OB to confirm
            confluence = False
            for fvg in fvgs:
                if fvg[3] == "bull" and i - fvg[0] <= 5:
                    # FVG top/bottom overlap with current candle body
                    if not (entry_zone_top < fvg[2] or entry_zone_bot > fvg[1]):
                        confluence = True
                        break
            if not confluence:
                for ob in obs:
                    if ob["side"] == "bull" and i - ob["idx"] <= 20:
                        if not (entry_zone_top < ob["bottom"] or entry_zone_bot > ob["top"]):
                            confluence = True
                            break

            if confluence:
                sl = l[i] * 0.9995
                tp = c[i] + 2 * (c[i] - sl)  # 1:2 R:R
                pos = {
                    "side": "long", "entry": c[i] * 1.0002,
                    "sl": sl, "tp": tp, "entry_idx": i,
                }
                continue

        # SHORT setup
        if (recent_high_idx is not None and
            h[i] > recent_high_val and
            c[i] < o[i] and
            body / c[i] >= MIN_IMPULSE_BODY_PCT and
            close_pos <= 0.30):

            confluence = False
            for fvg in fvgs:
                if fvg[3] == "bear" and i - fvg[0] <= 5:
                    if not (entry_zone_top < fvg[2] or entry_zone_bot > fvg[1]):
                        confluence = True
                        break
            if not confluence:
                for ob in obs:
                    if ob["side"] == "bear" and i - ob["idx"] <= 20:
                        if not (entry_zone_top < ob["bottom"] or entry_zone_bot > ob["top"]):
                            confluence = True
                            break

            if confluence:
                sl = h[i] * 1.0005
                tp = c[i] - 2 * (sl - c[i])
                pos = {
                    "side": "short", "entry": c[i] * 0.9998,
                    "sl": sl, "tp": tp, "entry_idx": i,
                }
                continue

    max_dd = peak - equity
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "trades": len(trades),
        "wins": wins,
        "equity": equity,
        "max_dd": max_dd,
        "return_pct": 100 * (equity - BUDGET_INR) / BUDGET_INR,
        "trade_list": trades,
    }


def main():
    res = run_ict()
    print("=" * 80)
    print("ICT / Smart Money Concepts — ETHUSD backtest")
    print(f"Budget: ₹{BUDGET_INR:,.0f}, Leverage: {LEVERAGE:.0f}x, Fees: {PERP_FEE_BPS}bps/side")
    print(f"Kill zones: {KILL_ZONES}, R:R ~1:2, Max hold: {MAX_HOLD_CANDLES*15}m")
    print("=" * 80)
    print(f"  Trades: {res['trades']}")
    if res["trades"]:
        print(f"  Wins: {res['wins']} ({100*res['wins']/res['trades']:.1f}%)")
    print(f"  Final equity: ₹{res['equity']:,.0f}")
    print(f"  Return: {res['return_pct']:+.1f}%")
    print(f"  MaxDD: ₹{res['max_dd']:,.0f} ({100*res['max_dd']/BUDGET_INR:.1f}%)")
    print(f"\n  First/last 5 trades:")
    for t in res["trade_list"][:5] + res["trade_list"][-5:]:
        print(f"    {t['side']:5} entry {t['entry_time']} ₹{t['entry']:>10,.2f} "
              f"→ exit {t['exit_time']} ₹{t['exit']:>10,.2f} "
              f"reason={t['reason']:<5} pnl=₹{t['pnl']:>+8,.0f}")


if __name__ == "__main__":
    main()
