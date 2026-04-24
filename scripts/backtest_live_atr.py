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
from datetime import time

# ── Args ──────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--capital",    type=float, default=150_000.0)
parser.add_argument("--no-cache",   action="store_true")
parser.add_argument("--months",     type=str, default=None,
                    help="Comma-separated e.g. 2026-04 or 2026-01,2026-02,2026-03")
parser.add_argument("--date",       type=str, default=None,
                    help="Single date YYYY-MM-DD — show only that day")
parser.add_argument("--sell",       action="store_true",
                    help="Simulate option SELLING instead of buying")
parser.add_argument("--trail",      action="store_true",
                    help="Use trailing SL (trails with best premium seen)")
parser.add_argument("--mode",       type=str, default="atr_only",
                    choices=["atr_only", "full"],
                    help="Scorer mode (default: atr_only)")
parser.add_argument("--lots",       type=int, default=None,
                    help="Number of lots per trade (overrides config MIN_LOTS)")
parser.add_argument("--dynamic-lots", action="store_true",
                    help="Scale lots with equity: 1 lot per ₹13K → compounds hard as capital grows")
parser.add_argument("--threshold",  type=int, default=None,
                    help="Signal score threshold (overrides config MIN_SIGNAL_SCORE, default 6)")
parser.add_argument("--rr",         type=float, default=None,
                    help="R:R ratio — TP = SL_dist × rr (overrides config ATR_RR_RATIO, default 3.0)")
parser.add_argument("--no-lunch",   action="store_true",
                    help="Disable lunch-hour skip (12:30–13:30) — trade all hours")
parser.add_argument("--vwap-min",   type=float, default=0.0,
                    help="Min NIFTY-spot distance from VWAP to enter (default 0 = off)")
parser.add_argument("--vwap-max",   type=float, default=9999.0,
                    help="Max NIFTY-spot distance from VWAP to enter (default 9999 = off)")
parser.add_argument("--max-hold",   type=int, default=0,
                    help="Exit if position still open after N bars with no TP progress (0 = off)")
parser.add_argument("--slippage",   type=float, default=3.0,
                    help="One-way slippage in premium points (applied on entry AND exit, default 3)")
parser.add_argument("--max-daily-trades", type=int, default=0,
                    help="Max entries per day — 0=unlimited (live gate is MAX_OPEN_POSITIONS=1)")
args = parser.parse_args()

import config
INITIAL_CAPITAL  = args.capital
SELL_MODE        = args.sell                      # True = option selling, False = buying
TRAIL_MODE       = args.trail                     # True = trailing SL
STOP_LOSS_PCT    = config.STOP_LOSS_PCT           # 1.5 % — fallback when ATR unavailable
ATR_RR_RATIO     = args.rr if args.rr is not None else config.ATR_RR_RATIO
MIN_OPTION_PREM  = config.MIN_OPTION_PREMIUM      # 150
MAX_OPTION_PREM  = config.MAX_OPTION_PREMIUM      # 170
SIGNAL_THRESHOLD = args.threshold if args.threshold is not None else config.MIN_SIGNAL_SCORE
config.MIN_SIGNAL_SCORE = SIGNAL_THRESHOLD   # patch so score_symbol() sees the override
LOT_SIZE         = config.LOT_SIZES["NIFTY"]  # 65
MIN_LOTS         = args.lots if args.lots is not None else config.MIN_LOTS  # default 3
MAX_DAILY_LOSS   = config.MAX_DAILY_LOSS      # 6250
TRADE_START      = time(9, 30)    # matches live: INTRADAY_START 09:30
TRADE_EXIT       = time(11, 20)   # matches live: INTRADAY_EXIT_BY 11:20
LUNCH_START      = time(23, 59)   # disabled — we exit at 11:20
LUNCH_END        = time(23, 59)
SLIPPAGE         = args.slippage   # pts per side (entry + exit = 2×SLIPPAGE per round trip)

TARGET_MONTHS = (
    [m.strip() for m in args.months.split(",") if m.strip()]
    if args.months
    else (
        [args.date[:7]] if args.date
        else ["2026-01", "2026-02", "2026-03", "2026-04"]
    )
)

CACHE_DIR  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtest_cache")
_PCR_FILE  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "pcr_historical.csv")


def _load_pcr_history() -> dict:
    """Load db/pcr_historical.csv → {date_str: pcr_float}. Empty dict if file missing."""
    if not os.path.exists(_PCR_FILE):
        return {}
    try:
        df = pd.read_csv(_PCR_FILE, usecols=["date", "pcr_weekly"])
        result = {row["date"]: float(row["pcr_weekly"]) for _, row in df.iterrows()}
        print(f"  PCR history: {len(result)} days loaded ({min(result)} to {max(result)})")
        return result
    except Exception as e:
        print(f"  WARN: could not load PCR history: {e}")
        return {}


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_5m() -> pd.DataFrame:
    os.makedirs(CACHE_DIR, exist_ok=True)
    # Prefer the larger 180d cache if it exists
    cache_path = os.path.join(CACHE_DIR, "NIFTY_5m_180d.csv")
    if not os.path.exists(cache_path):
        cache_path = os.path.join(CACHE_DIR, "NIFTY_5m_90d.csv")
    if os.path.exists(cache_path) and not args.no_cache:
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        print(f"  (cache) {os.path.basename(cache_path)}: {len(df)} bars")
        return _norm(df)
    print("  Fetching NIFTY 5m from Angel One (180d)...")
    from data.angel_fetcher import AngelFetcher
    df = AngelFetcher.get().fetch_historical_df("NIFTY", "5m", days=180)
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
             prev_day_df, equity: float,
             pcr_history: dict = None) -> tuple[list, float]:
    """Run one day. Returns (trades_list, end_equity)."""
    from strategies.signal_scorer import score_symbol

    trades       = []
    position     = None
    day_start_eq = equity

    # Build oi_data from historical PCR if available for this day
    day_pcr  = (pcr_history or {}).get(str(day), None)
    if day_pcr is not None:
        if day_pcr > 1.3:
            _sentiment = "very_bullish"
        elif day_pcr > 1.1:
            _sentiment = "bullish"
        elif day_pcr < 0.7:
            _sentiment = "very_bearish"
        elif day_pcr < 0.9:
            _sentiment = "bearish"
        else:
            _sentiment = "neutral"
        _oi_data = {"pcr": day_pcr, "sentiment": _sentiment, "bias": "NEUTRAL",
                    "ce_wall": 0, "pe_wall": 0, "max_pain": 0, "spot": 0,
                    "atm_ce_oi_delta": 0, "atm_pe_oi_delta": 0}
    else:
        _oi_data = {}   # no PCR data — scorer defaults to neutral (old behaviour)


    for local_pos, (ts, row) in enumerate(day_df.iterrows()):
        bar_time = ts.time() if hasattr(ts, "time") else time(12, 0)
        price    = float(row["Close"])

        # ── EOD square-off ───────────────────────────────────────────────────
        if bar_time >= TRADE_EXIT and position:
            model_prem = _option_premium(price, position["strike"], position["option_type"])
            exit_prem  = max(model_prem - SLIPPAGE, 0.5)   # market sell slips
            if not SELL_MODE:
                pnl = (exit_prem - position["entry_prem"]) * position["qty"]
            else:
                pnl = (position["entry_prem"] - exit_prem) * position["qty"]
            trades.append(_make_trade(day, position, exit_prem, round(pnl, 2), price, "EOD"))
            equity  += pnl
            position = None
            continue

        if bar_time < TRADE_START:
            continue

        # ── Manage open position ─────────────────────────────────────────────
        if position:
            bar_high  = float(row["High"])
            bar_low   = float(row["Low"])
            opt_type  = position["option_type"]
            strike_p  = position["strike"]
            curr_prem = _option_premium(price,    strike_p, opt_type)
            sl_dist   = position["sl_dist"]

            # Intrabar worst premium — SL-M on exchange fires the moment price
            # touches SL level, not just at bar close.
            # CE: premium drops when spot drops → bar Low is worst case.
            # PE: premium drops when spot rises → bar High is worst case.
            if not SELL_MODE:
                if opt_type == "CE":
                    worst_prem = _option_premium(bar_low,  strike_p, "CE")
                else:
                    worst_prem = _option_premium(bar_high, strike_p, "PE")
            else:
                worst_prem = curr_prem   # sell mode: simplified

            if not SELL_MODE:
                # SL: check intrabar worst against the SL level set at END OF PRIOR BAR.
                # Trail update happens AFTER this check (correct temporal order).
                sl_hit = worst_prem <= position["sl_price"]
                tp_hit = curr_prem  >= position["tp_price"]
                pnl_fn = lambda ep: (ep - position["entry_prem"]) * position["qty"]
            else:
                sl_hit = curr_prem >= position["sl_price"]
                tp_hit = curr_prem <= position["tp_price"]
                pnl_fn = lambda ep: (position["entry_prem"] - ep) * position["qty"]

            # ── Trailing SL: update on BAR CLOSE, AFTER the SL check ────────
            # Only update if not already stopped. Trail mirrors live bot's 60s
            # guardian — it sees bar-close prices, not intrabar extremes.
            if not sl_hit and TRAIL_MODE:
                if not SELL_MODE:
                    if curr_prem > position["best_prem"]:
                        position["best_prem"] = curr_prem
                        position["sl_price"]  = round(curr_prem - sl_dist, 1)
                else:
                    if curr_prem < position["best_prem"]:
                        position["best_prem"] = curr_prem
                        position["sl_price"]  = round(curr_prem + sl_dist, 1)

            position["bars_held"] += 1

            if sl_hit:
                # SL-M fills at SL price minus exit slippage (fast market, worst side)
                exit_p = max(position["sl_price"] - SLIPPAGE, 0.5)
                pnl    = pnl_fn(exit_p)
                trades.append(_make_trade(day, position, exit_p, round(pnl, 2), price, "SL"))
                equity  += pnl
                position = None
            elif tp_hit:
                # Limit TP: slippage is minimal but still apply for consistency
                exit_p = max(position["tp_price"] - SLIPPAGE, 0.5)
                pnl    = pnl_fn(exit_p)
                trades.append(_make_trade(day, position, exit_p, round(pnl, 2), price, "TP"))
                equity  += pnl
                position = None
            elif args.max_hold > 0 and position["bars_held"] >= args.max_hold:
                exit_p = max(curr_prem - SLIPPAGE, 0.5)
                pnl    = pnl_fn(exit_p)
                trades.append(_make_trade(day, position, exit_p, round(pnl, 2), price, "DECAY"))
                equity  += pnl
                position = None
            continue

        # ── Daily loss guard ─────────────────────────────────────────────────
        if equity - day_start_eq <= -MAX_DAILY_LOSS:
            break

        # ── Daily trade cap (matches live MAX_DAILY_TRADES) ───────────────────
        if args.max_daily_trades > 0 and len([t for t in trades if str(t["date"]) == str(day)]) >= args.max_daily_trades:
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
        scored = score_symbol(indic, _oi_data, {}, intra, df_5m=df_5m_slice, mode=args.mode)
        action = scored["action"]
        score  = scored["score"]

        if action not in ("BUY", "SELL"):
            continue

        # ── VWAP Distance Filter (Market Impact curve concept) ────────────────
        if args.vwap_min > 0 or args.vwap_max < 9999:
            vwap = intra.get("vwap", price)
            vwap_dist = abs(price - vwap)
            if not (args.vwap_min <= vwap_dist <= args.vwap_max):
                continue

        # ── Enter ─────────────────────────────────────────────────────────────
        option_type = "CE" if action == "BUY" else "PE"
        atm         = int(round(price / 50) * 50)

        # Search nearby strikes for one whose premium is in [MIN_OPTION_PREM, MAX_OPTION_PREM]
        strike, prem = atm, _option_premium(price, atm, option_type)
        mid_target   = (MIN_OPTION_PREM + MAX_OPTION_PREM) / 2
        for i in range(1, 11):
            for direction in [-1, 1]:
                candidate = atm + direction * i * 50
                candidate_prem = _option_premium(price, candidate, option_type)
                if MIN_OPTION_PREM <= candidate_prem <= MAX_OPTION_PREM:
                    strike, prem = candidate, candidate_prem
                    break
                if abs(candidate_prem - mid_target) < abs(prem - mid_target):
                    strike, prem = candidate, candidate_prem
            if MIN_OPTION_PREM <= prem <= MAX_OPTION_PREM:
                break
        # Apply entry slippage: market order fills worse than model price
        prem_actual = round(prem + SLIPPAGE, 1)

        # Dynamic lot sizing: scale lots as equity grows (compounding lever)
        if args.dynamic_lots:
            # 1 lot per ₹13,000 of equity, minimum MIN_LOTS, maximum 30
            dynamic = max(MIN_LOTS, min(30, int(equity / 13_000)))
            lots_now = dynamic
        else:
            lots_now = MIN_LOTS
        qty = LOT_SIZE * lots_now

        # ATR/2 as absolute SL distance; TP = entry + SL_dist × RR ratio
        atr_5m = intra.get("atr_5m") or 0
        if not atr_5m and len(hist_5m) >= 3:
            h = hist_5m["High"].astype(float)
            l = hist_5m["Low"].astype(float)
            c = hist_5m["Close"].astype(float)
            tr = pd.concat([(h - l), (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
            atr_5m = float(tr.ewm(span=14, adjust=False).mean().iloc[-1])
        sl_dist = round(atr_5m / 2, 1) if atr_5m else round(prem_actual * STOP_LOSS_PCT / 100, 1)
        sl_dist = max(sl_dist, 1.0)

        if not SELL_MODE:
            # Buying: SL/TP relative to actual slippage-adjusted fill
            sl_price = round(prem_actual - sl_dist, 1)
            tp_price = round(prem_actual + sl_dist * ATR_RR_RATIO, 1)
        else:
            # Selling: SL above entry (prem rises = loss), TP below entry (prem decays = profit)
            sl_price = round(prem_actual + sl_dist * ATR_RR_RATIO, 1)
            tp_price = max(round(prem_actual - sl_dist, 1), 1.0)

        position = {
            "option_type": option_type,
            "strike":      strike,
            "entry_prem":  prem_actual,   # actual fill including slippage
            "entry_spot":  price,
            "qty":         qty,
            "score":       score,
            "entry_time":  ts,
            "sl_price":    sl_price,
            "tp_price":    tp_price,
            "sl_dist":     sl_dist,
            "best_prem":   prem_actual,
            "atr_5m":      round(atr_5m, 1),
            "bars_held":   0,
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
    mode_label = ("OPTION SELLING + TRAIL SL" if (SELL_MODE and TRAIL_MODE)
                  else "OPTION SELLING" if SELL_MODE
                  else "OPTION BUYING + TRAIL SL" if TRAIL_MODE
                  else "OPTION BUYING")
    print(f"  Live ATR Strategy Backtest  ({mode_label}  |  score_symbol mode={args.mode})")
    print(f"  SL=ATR/2 pts  TP=ATR/2×{ATR_RR_RATIO:.0f} pts (1:{ATR_RR_RATIO:.0f}) | Threshold={SIGNAL_THRESHOLD} | "
          f"Strike ₹{MIN_OPTION_PREM}–₹{MAX_OPTION_PREM} | Lots={MIN_LOTS}×{LOT_SIZE} | Capital=₹{INITIAL_CAPITAL:,.0f}")
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
    df5        = _load_5m()
    pcr_history = _load_pcr_history()   # {date_str: pcr_float} — empty if no file

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

            day_trades, equity = _run_day(df5, day, day_df, prev_day_df, equity, pcr_history)
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
