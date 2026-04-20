"""
Live ATR Backtest — uses the exact same score_symbol(mode="atr_only") as the live bot.

Instead of a simplified hand-coded scorer, this script:
  1. Resamples 5m bars → daily OHLCV for RSI/MACD/SMA/ATR/Bollinger
  2. Computes intraday indicators (VWAP, ORB, 15m trend, 15m RSI, PDH/PDL)
  3. Calls strategies.signal_scorer.score_symbol() with mode="atr_only"
  4. Enters when score >= +6 (BUY CE) or <= -6 (SELL → BUY PE)
  5. SL = STOP_LOSS_PCT% of option premium, TP = TAKE_PROFIT_PCT% of premium
  6. Uses the same paper option premium model as the live bot

Usage:
  python scripts/backtest_live_atr.py
  python scripts/backtest_live_atr.py --date 2026-04-10
  python scripts/backtest_live_atr.py --months 2026-04
  python scripts/backtest_live_atr.py --months 2026-01,2026-02,2026-03
  python scripts/backtest_live_atr.py --no-cache
"""

import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging
logging.basicConfig(level=logging.WARNING)

import pandas as pd
import numpy as np
from datetime import time, date as date_t

# ── Args ──────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--capital",    type=float, default=150_000.0)
parser.add_argument("--no-cache",   action="store_true")
parser.add_argument("--months",     type=str, default=None,
                    help="Comma-separated e.g. 2026-04 or 2026-01,2026-02,2026-03")
parser.add_argument("--date",       type=str, default=None,
                    help="Single date YYYY-MM-DD — show only that day")
args = parser.parse_args()

import config
INITIAL_CAPITAL  = args.capital
STOP_LOSS_PCT    = config.STOP_LOSS_PCT       # 1.5 %
TAKE_PROFIT_PCT  = config.TAKE_PROFIT_PCT     # 3.75 %
SIGNAL_THRESHOLD = config.MIN_SIGNAL_SCORE    # 6
LOT_SIZE         = config.LOT_SIZES["NIFTY"]  # 65
MIN_LOTS         = config.MIN_LOTS            # 3
MAX_DAILY_LOSS   = config.MAX_DAILY_LOSS      # 6250
TRADE_START      = time(9, 45)
TRADE_EXIT       = time(15, 10)
LUNCH_START      = time(12, 30)
LUNCH_END        = time(13, 30)

TARGET_MONTHS = (
    [m.strip() for m in args.months.split(",") if m.strip()]
    if args.months
    else (
        [args.date[:7]] if args.date
        else ["2026-01", "2026-02", "2026-03"]
    )
)

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtest_cache")


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_5m() -> pd.DataFrame:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, "NIFTY_5m_90d.csv")
    if os.path.exists(cache_path) and not args.no_cache:
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        print(f"  (cache) NIFTY_5m_90d.csv: {len(df)} bars")
        return _norm(df)
    print("  Fetching NIFTY 5m from Angel One (90d)...")
    from data.angel_fetcher import AngelFetcher
    df = AngelFetcher.get().fetch_historical_df("NIFTY", "5m", days=90)
    if df is None or len(df) < 100:
        raise ValueError("No data — check .env Angel One credentials.")
    df = _norm(df)
    df.to_csv(cache_path)
    print(f"  (fetch) {len(df)} bars — cached")
    return df


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    rename = {c: c.capitalize() for c in df.columns
              if c.lower() in {"open", "high", "low", "close", "volume"}}
    df.rename(columns=rename, inplace=True)
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col not in df.columns:
            df[col] = 0.0
    df.index = pd.to_datetime(df.index)
    df["_date"] = df.index.date
    df["_ym"]   = [str(d)[:7] for d in df["_date"]]
    return df


# ── Indicator computation (mirrors get_indicators + get_intraday_indicators) ──

def _daily_indicators(df5: pd.DataFrame, up_to_date) -> dict:
    """Resample 5m bars to daily and compute RSI/MACD/SMA/ATR/Bollinger — same as live bot's get_indicators()."""
    hist = df5[df5["_date"] <= up_to_date].copy()
    if len(hist) < 30:
        return {}

    daily = hist.resample("1D", on=hist.index if not hasattr(hist.index, 'date') else None).agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna(subset=["Close"])
    # resample on DatetimeIndex
    hist2 = hist.copy()
    hist2.index = pd.to_datetime(hist2.index)
    daily = hist2.resample("1D").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna(subset=["Close"])
    if len(daily) < 5:
        return {}

    closes  = daily["Close"].astype(float)
    highs   = daily["High"].astype(float)
    lows    = daily["Low"].astype(float)
    volumes = daily["Volume"].astype(float)
    price   = float(closes.iloc[-1])

    # SMAs / EMA
    sma20 = float(closes.rolling(min(20, len(closes))).mean().iloc[-1])
    sma50 = float(closes.rolling(min(50, len(closes))).mean().iloc[-1])
    ema9  = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])

    # RSI 14
    delta = closes.diff()
    gain  = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=14, adjust=False).mean()
    rsi   = float((100 - 100 / (1 + gain / loss.replace(0, 1e-9))).iloc[-1])

    # MACD
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    msig  = macd.ewm(span=9, adjust=False).mean()
    mhist = macd - msig

    # ATR 14
    prev_c = closes.shift(1)
    tr     = pd.concat([highs - lows,
                        (highs - prev_c).abs(),
                        (lows  - prev_c).abs()], axis=1).max(axis=1)
    atr    = float(tr.ewm(span=14, adjust=False).mean().iloc[-1])
    atr_pct = atr / price * 100 if price else 0

    # Bollinger 20,2
    sma20s = closes.rolling(min(20, len(closes))).mean()
    std20  = closes.rolling(min(20, len(closes))).std()
    bb_upper = float((sma20s + 2 * std20).iloc[-1])
    bb_lower = float((sma20s - 2 * std20).iloc[-1])

    # Volume ratio (today vs 20d avg)
    vol_avg = float(volumes.rolling(min(20, len(volumes))).mean().iloc[-1])
    vol_now = float(volumes.iloc[-1])
    vol_ratio = vol_now / vol_avg if vol_avg > 0 else 1.0

    # Change %
    prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else price
    change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0

    return {
        "price":            price,
        "rsi":              rsi,
        "macd":             float(macd.iloc[-1]),
        "macd_signal":      float(msig.iloc[-1]),
        "macd_histogram":   float(mhist.iloc[-1]),
        "sma_20":           sma20,
        "sma_50":           sma50,
        "ema_9":            ema9,
        "atr_14":           atr,
        "atr_pct":          atr_pct,
        "bollinger_upper":  bb_upper,
        "bollinger_lower":  bb_lower,
        "volume_ratio":     vol_ratio,
        "change_pct":       change_pct,
    }


def _intraday_indicators(day_bars: pd.DataFrame, pos: int,
                         prev_day_bars: pd.DataFrame | None) -> dict:
    """Compute VWAP, ORB, 15m trend, PDH/PDL — same as live bot's get_intraday_indicators()."""
    bars = day_bars.iloc[:pos + 1]
    if len(bars) == 0:
        return {}

    closes = bars["Close"].astype(float)
    highs  = bars["High"].astype(float)
    lows   = bars["Low"].astype(float)
    price  = float(closes.iloc[-1])

    # VWAP (typical price, no volume weight for index)
    tp    = (highs + lows + closes) / 3
    vwap  = float(tp.mean())

    # ORB — first 3 bars (15 min)
    orb_high = orb_low = None
    orb_broken_up = orb_broken_down = False
    if len(day_bars) >= 3:
        orb_bars = day_bars.iloc[:3]
        orb_high = float(orb_bars["High"].max())
        orb_low  = float(orb_bars["Low"].min())
        if pos >= 3:
            orb_broken_up   = price > orb_high
            orb_broken_down = price < orb_low

    # 15-min trend (resample to 15m, SMA9 vs SMA20)
    trend_15m = None
    rsi_15m   = None
    bars_full = bars.copy()
    bars_full.index = pd.to_datetime(bars_full.index)
    df15 = bars_full.resample("15min", label="right", closed="right").agg({
        "Close": "last"
    }).dropna()
    if len(df15) >= 9:
        c15    = df15["Close"].astype(float)
        sma9   = float(c15.rolling(min(9, len(c15))).mean().iloc[-1])
        sma20_ = float(c15.rolling(min(20, len(c15))).mean().iloc[-1])
        trend_15m = "uptrend" if sma9 > sma20_ else "downtrend"

        delta15 = c15.diff()
        g15 = delta15.clip(lower=0).ewm(span=14, adjust=False).mean()
        l15 = (-delta15.clip(upper=0)).ewm(span=14, adjust=False).mean()
        rsi_15m = float((100 - 100 / (1 + g15 / l15.replace(0, 1e-9))).iloc[-1])

    # PDH / PDL
    pdh = pdl = None
    if prev_day_bars is not None and len(prev_day_bars) > 0:
        pdh = float(prev_day_bars["High"].astype(float).max())
        pdl = float(prev_day_bars["Low"].astype(float).min())

    result = {
        "price":        price,
        "vwap":         vwap,
        "above_vwap":   price > vwap,
        "orb_high":     orb_high,
        "orb_low":      orb_low,
        "orb_broken_up":   orb_broken_up,
        "orb_broken_down": orb_broken_down,
        "trend_15m":    trend_15m,
        "rsi_15m":      rsi_15m,
        "atr_5m":       None,
    }
    if pdh is not None:
        result["pdh"] = pdh
        result["pdl"] = pdl
    return result


# ── Paper option premium model (same as TrendStrategy._estimate_paper_option_ltp) ─

def _option_premium(spot: float, strike: int, option_type: str) -> float:
    intrinsic = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
    distance  = abs(spot - strike)
    base_tv   = max(18.0, spot * 0.0035)
    time_val  = max(8.0, base_tv - distance * 0.45)
    return max(1.0, round(intrinsic * 0.55 + time_val, 2))


# ── Core backtest loop ────────────────────────────────────────────────────────

def _run_day(df5: pd.DataFrame, day, day_df: pd.DataFrame,
             prev_day_df, equity: float) -> tuple[list, float]:
    """Run one day. Returns (trades_list, end_equity)."""
    from strategies.signal_scorer import score_symbol

    trades       = []
    position     = None
    day_start_eq = equity

    day_idxs = list(range(len(day_df)))

    for local_pos, (ts, row) in enumerate(day_df.iterrows()):
        bar_time = ts.time() if hasattr(ts, "time") else time(12, 0)
        price    = float(row["Close"])

        # ── EOD square-off ───────────────────────────────────────────────────
        if bar_time >= TRADE_EXIT and position:
            exit_prem = _option_premium(price, position["strike"], position["option_type"])
            pnl = (exit_prem - position["entry_prem"]) * position["qty"]
            trades.append(_make_trade(day, position, exit_prem, round(pnl, 2), price, "EOD"))
            equity  += pnl
            position = None
            continue

        if bar_time < TRADE_START:
            continue

        # ── Manage open position ─────────────────────────────────────────────
        if position:
            curr_prem = _option_premium(price, position["strike"], position["option_type"])
            pnl_pct   = (curr_prem - position["entry_prem"]) / position["entry_prem"] * 100

            if pnl_pct <= -STOP_LOSS_PCT:
                exit_p = round(position["entry_prem"] * (1 - STOP_LOSS_PCT / 100), 2)
                pnl    = (exit_p - position["entry_prem"]) * position["qty"]
                trades.append(_make_trade(day, position, exit_p, round(pnl, 2), price, "SL"))
                equity  += pnl
                position = None
            elif pnl_pct >= TAKE_PROFIT_PCT:
                exit_p = round(position["entry_prem"] * (1 + TAKE_PROFIT_PCT / 100), 2)
                pnl    = (exit_p - position["entry_prem"]) * position["qty"]
                trades.append(_make_trade(day, position, exit_p, round(pnl, 2), price, "TP"))
                equity  += pnl
                position = None
            continue

        # ── Daily loss guard ─────────────────────────────────────────────────
        if equity - day_start_eq <= -MAX_DAILY_LOSS:
            break

        if LUNCH_START <= bar_time <= LUNCH_END:
            continue

        # Need enough history for indicators
        hist_5m = df5[df5["_date"] <= day].iloc[: df5[df5["_date"] <= day].index.get_loc(ts) + 1]
        if len(hist_5m) < 60:
            continue

        # ── Build indicators ─────────────────────────────────────────────────
        indic = _daily_indicators(df5, day)
        if not indic:
            continue
        indic["symbol"] = "NIFTY"
        indic["price"]  = price  # override with latest 5m close

        intra = _intraday_indicators(day_df, local_pos, prev_day_df)

        df_5m_slice = df5.iloc[: df5.index.get_loc(ts) + 1].tail(120)

        # ── Score using the real scorer ──────────────────────────────────────
        scored = score_symbol(indic, {}, {}, intra, df_5m=df_5m_slice, mode="atr_only")
        action = scored["action"]
        score  = scored["score"]

        if action not in ("BUY", "SELL"):
            continue

        # ── Enter ─────────────────────────────────────────────────────────────
        option_type = "CE" if action == "BUY" else "PE"
        strike      = int(round(price / 50) * 50)
        prem        = _option_premium(price, strike, option_type)
        qty         = LOT_SIZE * MIN_LOTS

        position = {
            "option_type": option_type,
            "strike":      strike,
            "entry_prem":  prem,
            "entry_spot":  price,
            "qty":         qty,
            "score":       score,
            "entry_time":  ts,
        }

    return trades, equity


def _make_trade(day, pos, exit_prem, pnl, exit_spot, reason):
    return {
        "date":        str(day),
        "option_type": pos["option_type"],
        "strike":      pos["strike"],
        "entry_spot":  pos["entry_spot"],
        "exit_spot":   exit_spot,
        "entry_prem":  pos["entry_prem"],
        "exit_prem":   exit_prem,
        "qty":         pos["qty"],
        "pnl":         pnl,
        "score":       pos["score"],
        "reason":      reason,
        "entry_time":  str(pos["entry_time"]),
    }


# ── Stats ─────────────────────────────────────────────────────────────────────

def _wr(trades):
    if not trades: return 0.0
    return round(sum(1 for t in trades if t["pnl"] > 0) / len(trades) * 100, 1)

def _pf(trades):
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    return round(gp / gl, 2) if gl > 0 else float("inf")

def _expectancy(trades):
    if not trades: return 0.0
    wins  = [t["pnl"] for t in trades if t["pnl"] > 0]
    loss  = [t["pnl"] for t in trades if t["pnl"] < 0]
    wr    = len(wins) / len(trades)
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = abs(sum(loss) / len(loss)) if loss else 0
    return round(wr * avg_w - (1 - wr) * avg_l, 2)


# ── Print helpers ─────────────────────────────────────────────────────────────

def _print_day_detail(trades: list, target_date: str):
    day_t = [t for t in trades if t["date"] == target_date]
    if not day_t:
        print(f"  No trades on {target_date}")
        return
    print(f"\n  {'#':>2}  {'Type':<6}  {'Strike':>6}  {'EPrem':>7}  {'XPrem':>7}  "
          f"{'ESpot':>7}  {'XSpot':>7}  {'Qty':>5}  {'P&L':>10}  {'Score':>6}  Reason")
    print(f"  {'-'*95}")
    total = 0
    for i, t in enumerate(day_t, 1):
        pnl  = t["pnl"]
        total += pnl
        flag = "WIN " if pnl > 0 else "LOSS"
        print(f"  {i:>2}  {t['option_type']:<6}  {t['strike']:>6}  "
              f"₹{t['entry_prem']:>6.1f}  ₹{t['exit_prem']:>6.1f}  "
              f"{t['entry_spot']:>7.0f}  {t['exit_spot']:>7.0f}  "
              f"{t['qty']:>5}  {pnl:>10,.0f}  {str(t['score']):>6}  "
              f"{t['reason']}  [{flag}]")
    print(f"  {'-'*95}")
    print(f"  {'TOTAL':>65}  {total:>10,.0f}  ({len(day_t)} trades, WR={_wr(day_t):.0f}%)")


def _print_month_table(rows: list, final_eq: float):
    total = round((final_eq - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 1)
    w = 90
    print(f"\n{'='*w}")
    print(f"  Live ATR Strategy Backtest  (score_symbol mode=atr_only)")
    print(f"  SL={STOP_LOSS_PCT}% TP={TAKE_PROFIT_PCT}% | Threshold={SIGNAL_THRESHOLD} | "
          f"Lots={MIN_LOTS}×{LOT_SIZE}={MIN_LOTS*LOT_SIZE} qty | Capital=₹{INITIAL_CAPITAL:,.0f}")
    print(f"{'='*w}")
    print(f"{'Month':>8}  {'#Tr':>4}  {'Win%':>6}  {'PF':>5}  {'Net%':>7}  "
          f"{'Exp₹/tr':>9}  {'StartEq':>10}  {'EndEq':>10}")
    print('-' * w)
    for r in rows:
        pf_s = f"{r['pf']:.2f}" if r['pf'] != float("inf") else ">99"
        flag = " ✓" if r["net_pct"] > 0 and r["wr"] >= 40 else ""
        print(f"{r['month']:>8}  {r['trades']:>4}  {r['wr']:>6.1f}  {pf_s:>5}  "
              f"{r['net_pct']:>+7.1f}  {r['exp']:>9,.0f}  "
              f"{r['start_eq']:>10,.0f}  {r['end_eq']:>10,.0f}{flag}")
    print(f"\n  ₹{INITIAL_CAPITAL:,.0f} → ₹{final_eq:,.0f}  ({total:+.1f}% total)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    period = args.date or " / ".join(TARGET_MONTHS)
    print(f"\n{'='*60}")
    print(f"  Live ATR Strategy Backtest")
    print(f"  Period: {period}  |  Capital: ₹{INITIAL_CAPITAL:,.0f}")
    print(f"{'='*60}\n")

    print("Loading data...")
    df5 = _load_5m()

    available_months = sorted(set(df5["_ym"].unique()).intersection(TARGET_MONTHS))
    if not available_months:
        print(f"ERROR: No data for {TARGET_MONTHS}. Available: {sorted(df5['_ym'].unique())}")
        return

    print(f"  Months in data: {available_months}")
    print(f"  Total bars: {len(df5)}")
    print(f"\nRunning... ", end="", flush=True)

    all_dates = sorted(df5["_date"].unique())
    equity    = INITIAL_CAPITAL
    rows      = []
    all_trades = []

    for ym in TARGET_MONTHS:
        if ym not in available_months:
            continue
        month_trades = []
        month_eq_start = equity

        month_dates = [d for d in all_dates if str(d)[:7] == ym]
        if args.date:
            from datetime import date as _date
            target = _date.fromisoformat(args.date)
            month_dates = [d for d in month_dates if d == target]

        for day_i, day in enumerate(month_dates):
            day_df   = df5[df5["_date"] == day]
            # find prev trading day index in all_dates
            all_day_i = all_dates.index(day)
            prev_day_df = df5[df5["_date"] == all_dates[all_day_i - 1]] if all_day_i > 0 else None

            day_trades, equity = _run_day(df5, day, day_df, prev_day_df, equity)
            month_trades.extend(day_trades)
            print(".", end="", flush=True)

        all_trades.extend(month_trades)
        rows.append({
            "month":    ym,
            "trades":   len(month_trades),
            "wr":       _wr(month_trades),
            "pf":       _pf(month_trades),
            "net_pct":  round((equity - month_eq_start) / month_eq_start * 100, 1),
            "exp":      _expectancy(month_trades),
            "start_eq": round(month_eq_start, 0),
            "end_eq":   round(equity, 0),
        })

    print()
    _print_month_table(rows, equity)

    if args.date:
        _print_day_detail(all_trades, args.date)
    elif all_trades:
        print(f"\n  Overall: {len(all_trades)} trades | "
              f"WR={_wr(all_trades):.1f}% | PF={_pf(all_trades):.2f} | "
              f"Exp=₹{_expectancy(all_trades):,.0f}/trade")
    print()


if __name__ == "__main__":
    main()
