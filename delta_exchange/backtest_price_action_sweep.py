"""Price-action S/R retest strategy — BTC + ETH sweep with walk-forward test.

Usage:
    .venv/Scripts/python backtest_price_action_sweep.py
    .venv/Scripts/python backtest_price_action_sweep.py --btc-subdir 3m_btc --eth-subdir 3m_eth
    .venv/Scripts/python backtest_price_action_sweep.py --retest-mode wick_touch --body-pos 0.70 --wick-touch-tol 0.0007

Selected configs (Apr–Jun 2026):
    BTCUSD SL 0.40% / 1:5  -> zone: +11.03% PF1.23 MaxDD8.19%
                              wick_touch (7bps, bp0.70): +17.09% PF1.80 MaxDD2.20%
    ETHUSD SL 0.50% / 1:7  -> zone: +10.09% PF1.25 MaxDD5.40%
                              wick_touch (7bps, bp0.70): +12.59% PF1.72 MaxDD3.06%
"""
from __future__ import annotations
import sys, os, argparse
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path
import numpy as np
import pandas as pd

START_USD       = 10_000.0
CAPITAL_USE_PCT = 0.10
LEVERAGE        = 10
PERP_FEE_BPS    = 5.0
SLIPPAGE_BPS    = 2.0

LOOKBACK        = 240
TREND_LOOKBACK  = 1440
RANGE_PCT_MAX   = 0.015
RANGE_PCT_MIN   = 0.0
ZONE_PCT        = 0.004
BODY_MULT       = 1.3
WICK_RATIO_MAX  = 0.45
MIN_VOLUME_MULT = 1.0
COOLDOWN_CANDLES= 60
BREAKEVEN_R     = 1.0
MAX_HOLD_CANDLES= 240

# Optional WR-boost filters
RSI_PERIOD      = 14
RSI_LONG_MAX    = 100
RSI_SHORT_MIN   = 0
TREND_SLOPE_CANDLES = 0
TREND_SLOPE_MIN_PCT = 0.0
TRADING_HOURS   = "all"   # e.g. "0-4,13-21"
HTF_ALIGN       = False   # require 15m trend alignment
REQUIRE_ENGULFING = False
PIN_BAR_WICK_RATIO = 0.0
BLOCK_AFTER_LOSS_CANDLES = 0


def _parse_trading_hours(s: str):
    """Parse '0-4,13-21' into list of (start, end) UTC hour tuples."""
    if not s or s.lower() == "all":
        return []
    ranges = []
    for part in s.split(","):
        part = part.strip()
        if "-" not in part: continue
        a, b = part.split("-", 1)
        ranges.append((int(a), int(b)))
    return ranges


def _rsi(series: pd.Series, period: int = 14):
    """Pandas RSI."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _is_bull_engulfing(o, h, l, c):
    """Current green candle engulfs previous red candle body."""
    prev_green = np.r_[False, c[:-1] > o[:-1]]
    prev_red = np.r_[False, c[:-1] < o[:-1]]
    curr_green = c > o
    prev_body_top = np.r_[np.nan, np.maximum(c[:-1], o[:-1])]
    prev_body_bot = np.r_[np.nan, np.minimum(c[:-1], o[:-1])]
    return curr_green & prev_red & (o <= prev_body_bot) & (c >= prev_body_top)


def _is_bear_engulfing(o, h, l, c):
    """Current red candle engulfs previous green candle body."""
    prev_green = np.r_[False, c[:-1] > o[:-1]]
    prev_red = np.r_[False, c[:-1] < o[:-1]]
    curr_red = c < o
    prev_body_top = np.r_[np.nan, np.maximum(c[:-1], o[:-1])]
    prev_body_bot = np.r_[np.nan, np.minimum(c[:-1], o[:-1])]
    return curr_red & prev_green & (o >= prev_body_top) & (c <= prev_body_bot)


def load_perp(subdir: str, sym: str):
    base = Path(__file__).parent / "data" / subdir
    df = pd.read_csv(base / "perp" / f"{sym}_mark_1m.csv")
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("timestamp").sort_index()
    if df["volume"].isna().all():
        try:
            px = pd.read_csv(base / "perp" / f"{sym}_price_1m.csv")
            px["timestamp"] = pd.to_datetime(px["time"], unit="s", utc=True)
            px = px.set_index("timestamp").sort_index()
            df = df.join(px[["volume"]].rename(columns={"volume": "real_volume"}), how="left")
        except Exception:
            df["real_volume"] = np.nan
    else:
        df["real_volume"] = df["volume"]
    return df


def prepare(df: pd.DataFrame, use_trend: bool = True,
            retest_mode: str = "zone",
            body_pos_threshold: float = 0.65,
            wick_touch_tol: float = 0.0005,
            min_volume_mult: float = 1.0,
            rsi_period: int = 14,
            rsi_long_max: float = 100,
            rsi_short_min: float = 0,
            trend_slope_candles: int = 0,
            trend_slope_min_pct: float = 0.0,
            range_pct_min: float = 0.0,
            trading_hours: str = "all",
            htf_align: bool = False,
            require_engulfing: bool = False,
            pin_bar_wick_ratio: float = 0.0):
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    v = df["real_volume"].fillna(0).values

    body = np.abs(c - o)
    rng = h - l
    green = c > o
    red = c < o
    with np.errstate(divide="ignore", invalid="ignore"):
        close_pos = np.where(rng > 0, (c - l) / rng, 0.5)
        upper_wick = h - np.maximum(c, o)
        lower_wick = np.minimum(c, o) - l
        wick_pct = np.where(rng > 0, (upper_wick + lower_wick) / rng, 0)

    avg_body = pd.Series(body).rolling(LOOKBACK, min_periods=20).mean().values
    avg_volume = pd.Series(v).rolling(LOOKBACK, min_periods=20).mean().values

    r_high = pd.Series(h).rolling(LOOKBACK, min_periods=LOOKBACK).max().values
    r_low = pd.Series(l).rolling(LOOKBACK, min_periods=LOOKBACK).min().values
    width_pct = (r_high - r_low) / c
    in_range = (width_pct <= RANGE_PCT_MAX)
    if range_pct_min > 0:
        in_range &= (width_pct >= range_pct_min)

    trend_ma = pd.Series(c).rolling(TREND_LOOKBACK, min_periods=60).mean().values
    if use_trend:
        allow_long = c > trend_ma
        allow_short = c < trend_ma
    else:
        allow_long = np.ones(len(c), dtype=bool)
        allow_short = np.ones(len(c), dtype=bool)

    # trend slope filter
    if trend_slope_candles > 0 and trend_slope_min_pct > 0:
        slope = (trend_ma - np.roll(trend_ma, trend_slope_candles)) / trend_ma
        slope[:trend_slope_candles] = 0
        allow_long &= (slope >= trend_slope_min_pct)
        allow_short &= (slope <= -trend_slope_min_pct)

    # volume filter
    vol_ok = (avg_volume <= 0) | (v >= min_volume_mult * avg_volume)

    # RSI momentum filter
    if rsi_period > 0:
        rsi_vals = _rsi(pd.Series(c), rsi_period).values
        rsi_ok = (rsi_vals >= 0) & (rsi_vals <= 100)  # valid
        rsi_long_ok = (rsi_vals <= rsi_long_max) | np.isnan(rsi_vals)
        rsi_short_ok = (rsi_vals >= rsi_short_min) | np.isnan(rsi_vals)
    else:
        rsi_ok = rsi_long_ok = rsi_short_ok = np.ones(len(c), dtype=bool)

    # higher-timeframe (15m) trend alignment
    if htf_align:
        df_15m = df.resample('15min').agg({'open':'first','high':'max','low':'min','close':'last'})
        htf_close = df_15m['close'].reindex(df.index, method='ffill').values
        htf_ma = pd.Series(htf_close).rolling(20, min_periods=5).mean().values
        htf_long = c > htf_ma
        htf_short = c < htf_ma
    else:
        htf_long = htf_short = np.ones(len(c), dtype=bool)

    # candlestick patterns
    if require_engulfing:
        bull_engulf = _is_bull_engulfing(o, h, l, c)
        bear_engulf = _is_bear_engulfing(o, h, l, c)
    else:
        bull_engulf = bear_engulf = np.ones(len(c), dtype=bool)

    if pin_bar_wick_ratio > 0:
        # long lower wick + small body = hammer at support
        # long upper wick + small body = shooting star at resistance
        pin_bar_body_max = 0.35
        with np.errstate(divide="ignore", invalid="ignore"):
            lower_wick_ratio = np.where(rng > 0, lower_wick / rng, 0)
            upper_wick_ratio = np.where(rng > 0, upper_wick / rng, 0)
            body_ratio = np.where(rng > 0, body / rng, 0)
        bull_pin = (green & (lower_wick_ratio >= pin_bar_wick_ratio) &
                    (body_ratio <= pin_bar_body_max))
        bear_pin = (red & (upper_wick_ratio >= pin_bar_wick_ratio) &
                    (body_ratio <= pin_bar_body_max))
    else:
        bull_pin = bear_pin = np.zeros(len(c), dtype=bool)

    # time-of-day filter
    hours = df.index.hour.values
    time_allowed = np.ones(len(c), dtype=bool)
    hour_ranges = _parse_trading_hours(trading_hours)
    if hour_ranges:
        time_allowed = np.zeros(len(c), dtype=bool)
        for start, end in hour_ranges:
            if start < end:
                time_allowed |= (hours >= start) & (hours < end)
            else:
                time_allowed |= (hours >= start) | (hours < end)

    # retest-zone logic depends on mode
    if retest_mode == "zone":
        near_high = (r_high - c) / c <= ZONE_PCT
        near_low = (c - r_low) / c <= ZONE_PCT
    elif retest_mode == "wick_touch":
        # wick must touch or pierce the 4h S/R level
        near_high = (r_high - h) / c <= wick_touch_tol
        near_low = (l - r_low) / c <= wick_touch_tol
    elif retest_mode == "strong_rejection":
        # wick touches level AND candle closes strongly away from it
        near_high = ((r_high - h) / c <= wick_touch_tol) & (close_pos <= (1 - body_pos_threshold))
        near_low = ((l - r_low) / c <= wick_touch_tol) & (close_pos >= body_pos_threshold)
    elif retest_mode == "two_touch":
        # at least 2 of the last 3 candles touched the level; current candle confirms
        long_touch = (l - r_low) / c <= ZONE_PCT
        short_touch = (r_high - h) / c <= ZONE_PCT
        touches_long = pd.Series(long_touch).rolling(3, min_periods=2).sum().values
        touches_short = pd.Series(short_touch).rolling(3, min_periods=2).sum().values
        near_low = (touches_long >= 2) & (close_pos >= body_pos_threshold)
        near_high = (touches_short >= 2) & (close_pos <= (1 - body_pos_threshold))
    else:
        raise ValueError(f"Unknown retest_mode: {retest_mode}")

    pattern_long = np.zeros(len(c), dtype=bool)
    pattern_short = np.zeros(len(c), dtype=bool)
    if require_engulfing:
        pattern_long |= bull_engulf
        pattern_short |= bear_engulf
    if pin_bar_wick_ratio > 0:
        pattern_long |= bull_pin
        pattern_short |= bear_pin
    pattern_required = require_engulfing or pin_bar_wick_ratio > 0
    pattern_ok_long = pattern_long if pattern_required else np.ones(len(c), dtype=bool)
    pattern_ok_short = pattern_short if pattern_required else np.ones(len(c), dtype=bool)

    strong_green = (green & (body >= BODY_MULT * avg_body) & (wick_pct <= WICK_RATIO_MAX) &
                    (close_pos >= body_pos_threshold) & vol_ok & rsi_long_ok & htf_long & pattern_ok_long)
    strong_red = (red & (body >= BODY_MULT * avg_body) & (wick_pct <= WICK_RATIO_MAX) &
                  (close_pos <= (1 - body_pos_threshold)) & vol_ok & rsi_short_ok & htf_short & pattern_ok_short)

    retest_long = in_range & allow_long & near_low & strong_green & time_allowed
    retest_short = in_range & allow_short & near_high & strong_red & time_allowed

    return {"o": o, "h": h, "l": l, "c": c,
            "retest_long": retest_long, "retest_short": retest_short,
            "r_high": r_high, "r_low": r_low}


def run_asset(subdir: str, sym: str, sl_pct: float, rr: float,
              use_trend: bool = True, trail_be: bool = True,
              date_start: pd.Timestamp | None = None,
              date_end: pd.Timestamp | None = None,
              retest_mode: str = "zone",
              body_pos_threshold: float = 0.65,
              wick_touch_tol: float = 0.0005,
              min_volume_mult: float = 1.0,
              rsi_period: int = 14,
              rsi_long_max: float = 100,
              rsi_short_min: float = 0,
              trend_slope_candles: int = 0,
              trend_slope_min_pct: float = 0.0,
              range_pct_min: float = 0.0,
              trading_hours: str = "all",
              htf_align: bool = False,
              require_engulfing: bool = False,
              pin_bar_wick_ratio: float = 0.0,
              cooldown_candles: int = 60,
              block_after_loss_candles: int = 0):
    df = load_perp(subdir, sym)
    if date_start:
        df = df[df.index >= date_start]
    if date_end:
        df = df[df.index < date_end]
    if len(df) < max(LOOKBACK, TREND_LOOKBACK) + 100:
        return [], START_USD, np.array([START_USD])

    s = prepare(df, use_trend=use_trend, retest_mode=retest_mode,
                body_pos_threshold=body_pos_threshold,
                wick_touch_tol=wick_touch_tol,
                min_volume_mult=min_volume_mult,
                rsi_period=rsi_period,
                rsi_long_max=rsi_long_max,
                rsi_short_min=rsi_short_min,
                trend_slope_candles=trend_slope_candles,
                trend_slope_min_pct=trend_slope_min_pct,
                range_pct_min=range_pct_min,
                trading_hours=trading_hours,
                htf_align=htf_align,
                require_engulfing=require_engulfing,
                pin_bar_wick_ratio=pin_bar_wick_ratio)
    o, h, l, c = s["o"], s["h"], s["l"], s["c"]
    ts = df.index
    n = len(df)
    long_sig, short_sig = s["retest_long"], s["retest_short"]

    equity = START_USD
    trades = []
    pos = None
    cooldown = -1
    block_long_until = -1
    block_short_until = -1
    equity_curve = [equity]

    start_i = max(LOOKBACK, TREND_LOOKBACK) + 10
    for i in range(start_i, n - 1):
        t = ts[i]
        ci = c[i]

        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            hi, lo = h[i], l[i]

            if (sign > 0 and hi >= pos["tp"]) or (sign < 0 and lo <= pos["tp"]):
                pnl = sign * (pos["tp"] - pos["entry"]) / pos["entry"]
                trades.append({**pos, "exit": pos["tp"], "exit_time": t, "pnl": pnl, "reason": "tp"})
                equity *= (1 + pnl * LEVERAGE * CAPITAL_USE_PCT)
                pos = None
                cooldown = i + cooldown_candles
                if pnl <= 0 and block_after_loss_candles > 0:
                    block_long_until = i + block_after_loss_candles if sign > 0 else block_long_until
                    block_short_until = i + block_after_loss_candles if sign < 0 else block_short_until
                equity_curve.append(equity)
                continue

            stop = pos["be_sl"] if pos.get("trail_be", False) else pos["sl"]
            if (sign > 0 and lo <= stop) or (sign < 0 and hi >= stop):
                pnl = sign * (stop - pos["entry"]) / pos["entry"]
                trades.append({**pos, "exit": stop, "exit_time": t, "pnl": pnl, "reason": "sl"})
                equity *= (1 + pnl * LEVERAGE * CAPITAL_USE_PCT)
                pos = None
                cooldown = i + cooldown_candles
                if pnl <= 0 and block_after_loss_candles > 0:
                    block_long_until = i + block_after_loss_candles if sign > 0 else block_long_until
                    block_short_until = i + block_after_loss_candles if sign < 0 else block_short_until
                equity_curve.append(equity)
                continue

            if i - pos["entry_idx"] >= MAX_HOLD_CANDLES:
                pnl = sign * (ci - pos["entry"]) / pos["entry"]
                trades.append({**pos, "exit": ci, "exit_time": t, "pnl": pnl, "reason": "hold"})
                equity *= (1 + pnl * LEVERAGE * CAPITAL_USE_PCT)
                pos = None
                cooldown = i + cooldown_candles
                if pnl <= 0 and block_after_loss_candles > 0:
                    block_long_until = i + block_after_loss_candles if sign > 0 else block_long_until
                    block_short_until = i + block_after_loss_candles if sign < 0 else block_short_until
                equity_curve.append(equity)
                continue

            if trail_be and not pos.get("trail_be", False):
                be_price = pos["entry"] * (1 + sign * sl_pct * BREAKEVEN_R)
                if (sign > 0 and ci >= be_price) or (sign < 0 and ci <= be_price):
                    pos["trail_be"] = True
                    pos["be_sl"] = pos["entry"]
            continue

        if i < cooldown:
            continue

        tp_pct = sl_pct * rr
        next_open = o[i + 1]

        if long_sig[i] and i >= block_long_until:
            entry = next_open * (1 + SLIPPAGE_BPS / 10_000)
            stop_level = l[i] * (1 - SLIPPAGE_BPS / 10_000)
            sl_dist = max(sl_pct, (entry - stop_level) / entry)
            sl = entry * (1 - sl_dist)
            tp = entry * (1 + tp_pct)
            pos = {
                "side": "long", "entry": entry, "sl": sl, "tp": tp,
                "entry_idx": i + 1, "entry_time": ts[i + 1],
                "rr": rr, "sl_pct": sl_dist, "tp_pct": tp_pct,
                "trail_be": False, "be_sl": None,
            }
            continue

        if short_sig[i] and i >= block_short_until:
            entry = next_open * (1 - SLIPPAGE_BPS / 10_000)
            stop_level = h[i] * (1 + SLIPPAGE_BPS / 10_000)
            sl_dist = max(sl_pct, (stop_level - entry) / entry)
            sl = entry * (1 + sl_dist)
            tp = entry * (1 - tp_pct)
            pos = {
                "side": "short", "entry": entry, "sl": sl, "tp": tp,
                "entry_idx": i + 1, "entry_time": ts[i + 1],
                "rr": rr, "sl_pct": sl_dist, "tp_pct": tp_pct,
                "trail_be": False, "be_sl": None,
            }
            continue

    if pos:
        sign = 1 if pos["side"] == "long" else -1
        pnl = sign * (c[-1] - pos["entry"]) / pos["entry"]
        trades.append({**pos, "exit": c[-1], "exit_time": ts[-1], "pnl": pnl, "reason": "eof"})
        equity *= (1 + pnl * LEVERAGE * CAPITAL_USE_PCT)
        equity_curve.append(equity)

    return trades, equity, np.array(equity_curve)


def metrics(trades, equity, equity_curve):
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp, gl = sum(t["pnl"] for t in wins), abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else float("inf")
    wr = len(wins) / len(trades) * 100

    max_cl = cl = 0
    for t in trades:
        if t["pnl"] <= 0:
            cl += 1
            max_cl = max(max_cl, cl)
        else:
            cl = 0

    peak = np.maximum.accumulate(equity_curve)
    dd = (peak - equity_curve) / peak
    max_dd = dd.max() * 100

    total_ret = (equity - START_USD) / START_USD * 100
    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0
    return {
        "trades": len(trades), "wr": wr, "pf": pf,
        "pnl": equity - START_USD, "ret_pct": total_ret,
        "max_dd_pct": max_dd, "max_cl": max_cl,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "equity": equity,
    }


def report(trades, equity, equity_curve, label):
    m = metrics(trades, equity, equity_curve)
    if m["trades"] == 0:
        print(f"{label}: no trades")
        return
    print(f"{label}: trades={m['trades']:3d}  WR={m['wr']:5.1f}%  PF={m['pf']:5.2f}  "
          f"P&L=${m['pnl']:+.2f} ({m['ret_pct']:+.2f}%)  MaxDD={m['max_dd_pct']:5.2f}%  "
          f"MaxCL={m['max_cl']:2d}")


def sweep_asset(subdir, sym):
    print(f"\n--- {sym} ({subdir}) ---")
    configs = [(0.003, 10), (0.004, 7), (0.005, 7), (0.005, 10)]
    for sl, rr in configs:
        trades, equity, curve = run_asset(subdir, sym, sl, rr, use_trend=True, trail_be=True)
        report(trades, equity, curve, f"SL={sl*100:.2f}% R:R 1:{rr}")


def walk_forward(subdir, sym, sl, rr, **run_kw):
    df = load_perp(subdir, sym)
    mid = df.index[int(len(df) * 0.4)]
    end = df.index[-1]
    kw_first = {k: v for k, v in run_kw.items() if k not in ("date_start", "date_end")}
    kw_last = dict(kw_first, date_start=mid, date_end=end)
    t1, e1, c1 = run_asset(subdir, sym, sl, rr, **kw_first, date_end=mid)
    t2, e2, c2 = run_asset(subdir, sym, sl, rr, **kw_last)
    print(f"  First 40% :", end=" ")
    report(t1, e1, c1, "")
    print(f"  Last  60% :", end=" ")
    report(t2, e2, c2, "")


def monthly_breakdown(trades):
    """Group trades by entry month and print P&L / WR per month."""
    if not trades:
        return
    months: dict[str, list] = {}
    for t in trades:
        m = t["entry_time"].strftime("%Y-%m")
        months.setdefault(m, []).append(t)
    print(f"\n  Monthly breakdown:")
    print(f"  {'Month':8} {'Trades':>6} {'Wins':>5} {'Losses':>6} {'WR':>7} {'Gross+ $':>10} {'Gross- $':>10} {'Net $':>10}")
    for m in sorted(months):
        tt = months[m]
        wins = [t for t in tt if t["pnl"] > 0]
        losses = [t for t in tt if t["pnl"] <= 0]
        gp = sum(t["pnl"] for t in wins) * START_USD * CAPITAL_USE_PCT * LEVERAGE
        gl = sum(t["pnl"] for t in losses) * START_USD * CAPITAL_USE_PCT * LEVERAGE
        net = gp + gl
        wr = len(wins) / len(tt) * 100
        print(f"  {m:8} {len(tt):>6} {len(wins):>5} {len(losses):>6} {wr:>6.1f}% {gp:>10.2f} {gl:>10.2f} {net:>10.2f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--btc-subdir", default="june_btc", help="BTC data subdir under data/")
    parser.add_argument("--eth-subdir", default="june_eth", help="ETH data subdir under data/")
    parser.add_argument("--date-start", default=None, help="Optional start date YYYY-MM-DD")
    parser.add_argument("--date-end", default=None, help="Optional end date YYYY-MM-DD")
    parser.add_argument("--retest-mode", default="wick_touch",
                        choices=["zone", "wick_touch", "strong_rejection", "two_touch"],
                        help="How strict the S/R retest entry must be")
    parser.add_argument("--body-pos", type=float, default=0.70,
                        help="Candle body-position threshold (long close_pos >= x, short <= 1-x)")
    parser.add_argument("--wick-touch-tol", type=float, default=0.0007,
                        help="Wick touch tolerance vs S/R level (default 7 bps)")
    # WR-boost filter knobs
    parser.add_argument("--min-volume-mult", type=float, default=1.0,
                        help="Volume >= x * 4h avg volume (default 1.0)")
    parser.add_argument("--rsi-period", type=int, default=14, help="RSI lookback")
    parser.add_argument("--rsi-long-max", type=float, default=100,
                        help="Max RSI allowed for long entries")
    parser.add_argument("--rsi-short-min", type=float, default=0,
                        help="Min RSI allowed for short entries")
    parser.add_argument("--trend-slope-candles", type=int, default=0,
                        help="Trend MA slope lookback (0 = disabled)")
    parser.add_argument("--trend-slope-min-pct", type=float, default=0.0,
                        help="Min |trend MA slope %%| over lookback")
    parser.add_argument("--range-pct-min", type=float, default=0.0,
                        help="Min 4h range width %%")
    parser.add_argument("--trading-hours", default="all",
                        help="UTC hour ranges, e.g. '0-4,13-21' (default all)")
    parser.add_argument("--htf-align", action="store_true",
                        help="Require 15m trend alignment")
    parser.add_argument("--require-engulfing", action="store_true",
                        help="Require engulfing candle pattern")
    parser.add_argument("--pin-bar-wick-ratio", type=float, default=0.0,
                        help="Min wick/range for pin-bar pattern (0 = disabled)")
    parser.add_argument("--cooldown-candles", type=int, default=60,
                        help="Candle cooldown between signals")
    parser.add_argument("--block-after-loss-candles", type=int, default=180,
                        help="Block same-side re-entry after a losing trade")
    parser.add_argument("--filter-experiment", action="store_true",
                        help="Run selected configs across filter presets and print comparison")
    parser.add_argument("--retest-experiment", action="store_true",
                        help="Run selected configs across all retest modes and print comparison")
    args = parser.parse_args()

    date_start = pd.Timestamp(args.date_start, tz="UTC") if args.date_start else None
    date_end = pd.Timestamp(args.date_end, tz="UTC") if args.date_end else None

    run_kw = dict(
        use_trend=True, trail_be=True,
        date_start=date_start, date_end=date_end,
        retest_mode=args.retest_mode,
        body_pos_threshold=args.body_pos,
        wick_touch_tol=args.wick_touch_tol,
        min_volume_mult=args.min_volume_mult,
        rsi_period=args.rsi_period,
        rsi_long_max=args.rsi_long_max,
        rsi_short_min=args.rsi_short_min,
        trend_slope_candles=args.trend_slope_candles,
        trend_slope_min_pct=args.trend_slope_min_pct,
        range_pct_min=args.range_pct_min,
        trading_hours=args.trading_hours,
        htf_align=args.htf_align,
        require_engulfing=args.require_engulfing,
        pin_bar_wick_ratio=args.pin_bar_wick_ratio,
        cooldown_candles=args.cooldown_candles,
        block_after_loss_candles=args.block_after_loss_candles,
    )

    selected = [(args.btc_subdir, "BTCUSD", 0.004, 5), (args.eth_subdir, "ETHUSD", 0.005, 7)]

    if args.retest_experiment:
        print("=" * 100)
        print("Retest-quality experiment — selected configs across modes")
        print(f"body_pos={args.body_pos}, wick_touch_tol={args.wick_touch_tol}")
        print("=" * 100)
        for subdir, sym, sl, rr in selected:
            print(f"\n--- {sym} SL={sl*100:.2f}% R:R 1:{rr} ---")
            print(f"{'Mode':16} {'Trades':>7} {'WR':>7} {'PF':>7} {'P&L $':>10} {'Ret%':>8} {'MaxDD%':>8} {'MaxCL':>6}")
            for mode in ["zone", "wick_touch", "strong_rejection", "two_touch"]:
                run_kw_mode = dict(run_kw, retest_mode=mode)
                trades, equity, curve = run_asset(subdir, sym, sl, rr, **run_kw_mode)
                m = metrics(trades, equity, curve)
                if m["trades"] == 0:
                    print(f"{mode:16} no trades")
                    continue
                print(f"{mode:16} {m['trades']:7d} {m['wr']:6.1f}% {m['pf']:7.2f} "
                      f"${m['pnl']:>+9.2f} {m['ret_pct']:>+7.2f}% {m['max_dd_pct']:>7.2f}% {m['max_cl']:>6d}")
        return

    if args.filter_experiment:
        presets = [
            ("baseline", {}),
            ("volume 1.5x", {"min_volume_mult": 1.5}),
            ("volume 2.0x", {"min_volume_mult": 2.0}),
            ("rsi 60/40", {"rsi_long_max": 60, "rsi_short_min": 40}),
            ("rsi 55/45", {"rsi_long_max": 55, "rsi_short_min": 45}),
            ("trend slope 20/0.02%", {"trend_slope_candles": 20, "trend_slope_min_pct": 0.0002}),
            ("trend slope 30/0.02%", {"trend_slope_candles": 30, "trend_slope_min_pct": 0.0002}),
            ("range min 0.5%", {"range_pct_min": 0.005}),
            ("range min 0.7%", {"range_pct_min": 0.007}),
            ("hours 0-4,13-21", {"trading_hours": "0-4,13-21"}),
            ("hours 13-21", {"trading_hours": "13-21"}),
            ("15m HTF align", {"htf_align": True}),
            ("engulfing", {"require_engulfing": True}),
            ("pin bar 0.6", {"pin_bar_wick_ratio": 0.6}),
            ("pin bar 0.7", {"pin_bar_wick_ratio": 0.7}),
            ("cooldown 90", {"cooldown_candles": 90}),
            ("cooldown 120", {"cooldown_candles": 120}),
            ("block-after-loss 120", {"block_after_loss_candles": 120}),
            ("block-after-loss 180", {"block_after_loss_candles": 180}),
        ]
        print("=" * 100)
        print("WR-boost filter experiment — one filter at a time")
        print(f"base: retest={args.retest_mode}, body_pos={args.body_pos}, wick_tol={args.wick_touch_tol}")
        print("=" * 100)
        for subdir, sym, sl, rr in selected:
            print(f"\n--- {sym} SL={sl*100:.2f}% R:R 1:{rr} ---")
            print(f"{'Preset':24} {'Trades':>7} {'WR':>7} {'PF':>7} {'P&L $':>10} {'Ret%':>8} {'MaxDD%':>8} {'MaxCL':>6}")
            for name, overrides in presets:
                kw = dict(run_kw, **overrides)
                trades, equity, curve = run_asset(subdir, sym, sl, rr, **kw)
                m = metrics(trades, equity, curve)
                if m["trades"] == 0:
                    print(f"{name:24} no trades")
                    continue
                print(f"{name:24} {m['trades']:7d} {m['wr']:6.1f}% {m['pf']:7.2f} "
                      f"${m['pnl']:>+9.2f} {m['ret_pct']:>+7.2f}% {m['max_dd_pct']:>7.2f}% {m['max_cl']:>6d}")
        return

    print("=" * 100)
    print("Price-action S/R retest — full sweep + walk-forward + monthly")
    print("=" * 100)

    assets = [(args.btc_subdir, "BTCUSD"), (args.eth_subdir, "ETHUSD")]

    for subdir, sym in assets:
        print(f"\n--- {sym} ({subdir}) ---")
        for sl in [0.003, 0.004, 0.005]:
            print(f"\nSL = {sl*100:.2f}%")
            for rr in [5, 7, 10]:
                trades, equity, curve = run_asset(subdir, sym, sl, rr, **run_kw)
                report(trades, equity, curve, f"R:R 1:{rr}")

    print("\n" + "=" * 100)
    print("Month-wise results for selected configs")
    print("=" * 100)
    for subdir, sym, sl, rr in selected:
        print(f"\n{sym} SL={sl*100:.2f}% R:R 1:{rr}  mode={args.retest_mode}")
        trades, equity, curve = run_asset(subdir, sym, sl, rr, **run_kw)
        report(trades, equity, curve, "Total")
        monthly_breakdown(trades)

    print("\n" + "=" * 100)
    print("Walk-forward test (40% / 60% split) — selected configs")
    print("=" * 100)
    for subdir, sym, sl, rr in selected:
        print(f"\n{sym} ({subdir}) SL={sl*100:.2f}% R:R 1:{rr}  mode={args.retest_mode}")
        walk_forward(subdir, sym, sl, rr, **run_kw)


if __name__ == "__main__":
    main()
