"""
S&D + ATR Trailing SL Backtest — NIFTY (Jan / Feb / Mar 2026)

Strategies compared:
  1. atr_fixed    : ATR Intraday signal, fixed SL=1×ATR, TP=RR×ATR       (current live baseline)
  2. atr_trail    : ATR Intraday signal, trailing SL (ATR×1 trail)
  3. ict_sd_htf   : ICT (order blocks + liq sweeps) + S&D zones from daily/60m bars, trailing SL

Trailing SL rule:
  trail_distance  = ATR × 1.0  (full ATR, less whipsaw than ATR/2)
  For BUY : new_sl = max(current_sl, current_bar_high - trail_distance)   — only moves UP
  For SELL: new_sl = min(current_sl, current_bar_low  + trail_distance)   — only moves DOWN

S&D zone filter:
  BUY  entries: must be AT or approaching a higher-timeframe demand zone
  SELL entries: must be AT or approaching a higher-timeframe supply zone
  Higher timeframes: 60m + Daily
  Trigger: 5m entry confirmation candle

Timeframe:
  Default 5m.  Pass --tf 15m to resample to 15-min bars.

Capital: ₹1,50,000

Usage:
  python scripts/backtest_sd_atr.py
  python scripts/backtest_sd_atr.py --tf 15m
  python scripts/backtest_sd_atr.py --no-cache
  python scripts/backtest_sd_atr.py --rr 3.0
"""

import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging
logging.basicConfig(level=logging.WARNING)

import pandas as pd
import numpy as np
from datetime import time, date

# ── Args ──────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--capital",   type=float, default=150_000.0)
parser.add_argument("--rr",        type=float, default=2.5)
parser.add_argument("--risk",      type=float, default=2.0,   help="Risk %% of equity per trade")
parser.add_argument("--tf",        type=str,   default="5m",  choices=["5m", "15m"])
parser.add_argument("--no-cache",  action="store_true")
parser.add_argument("--no-lunch",  action="store_true")
parser.add_argument("--daily-loss",type=float, default=5.0)
parser.add_argument("--months",    type=str,   default=None,
                    help="Comma-separated months to test e.g. 2026-04 or 2026-03,2026-04")
parser.add_argument("--date",      type=str,   default=None,
                    help="Single date YYYY-MM-DD — show only that day's trades")
args = parser.parse_args()

INITIAL_CAPITAL = args.capital
RR_RATIO        = args.rr
RISK_PCT        = args.risk
TIMEFRAME       = args.tf
NO_LUNCH        = args.no_lunch
DAILY_LOSS_PCT  = args.daily_loss
FORCE_REFETCH   = args.no_cache

LOT_SIZE        = 65
TRADE_START     = time(9, 45)
TRADE_EXIT      = time(15, 10)
LUNCH_START     = time(12, 30)
LUNCH_END       = time(13, 30)
ATR_PERIOD      = 14
OF_WINDOW       = 60      # bars for order-flow rolling window
SD_WINDOW       = 120     # bars for legacy/local S&D detection
SD_HTF_LOOKBACKS = {"60min": 120, "1D": 60}

# Entry thresholds (match live bot)
ATR_THRESHOLD   = 6
ICT_THRESHOLD   = 2
COMBINED_MIN    = 4       # ATR+ICT+SD combined minimum to fire

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtest_cache")

TARGET_MONTHS = (
    [m.strip() for m in args.months.split(",") if m.strip()]
    if args.months
    else ["2026-01", "2026-02", "2026-03"]
)


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_5m() -> pd.DataFrame:
    os.makedirs(CACHE_DIR, exist_ok=True)
    for fname in ["NIFTY_5m_150d.csv", "NIFTY_5m_90d.csv"]:
        path = os.path.join(CACHE_DIR, fname)
        if os.path.exists(path) and not FORCE_REFETCH:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            df = _norm(df)
            print(f"  (cache) {fname}: {len(df)} bars")
            return df

    print("  Fetching NIFTY 5m from Zerodha (90d)...")
    from data.zerodha_fetcher import ZerodhaFetcher
    df = ZerodhaFetcher.get().fetch_historical_df("NIFTY", "5m", days=90)
    if df is None or len(df) < 100:
        raise ValueError("No data. Run scripts/get_token.py first.")
    df = _norm(df)
    df.to_csv(os.path.join(CACHE_DIR, "NIFTY_5m_90d.csv"))
    print(f"  (fetch) NIFTY 5m 90d: {len(df)} bars — cached")
    return df


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    rename = {c: c.capitalize() for c in df.columns
              if c.lower() in {"open", "high", "low", "close", "volume"}}
    df.rename(columns=rename, inplace=True)
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col not in df.columns:
            df[col] = 0.0
    df["_date"] = pd.to_datetime(df.index).date
    df["_ym"]   = [str(d)[:7] for d in df["_date"]]
    return df


def _resample_15m(df5: pd.DataFrame) -> pd.DataFrame:
    """Resample 5m OHLCV to 15m. Preserves _date and _ym."""
    df = df5.drop(columns=["_date", "_ym"], errors="ignore").copy()
    df.index = pd.to_datetime(df.index)
    df15 = df.resample("15min", label="right", closed="right").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna(subset=["Close"])
    df15["_date"] = df15.index.date
    df15["_ym"]   = [str(d)[:7] for d in df15["_date"]]
    return df15


# ── ATR ───────────────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, pos: int, period: int = ATR_PERIOD) -> float:
    w      = df.iloc[max(0, pos - period * 3): pos + 1]
    closes = w["Close"].astype(float)
    highs  = w["High"].astype(float)
    lows   = w["Low"].astype(float)
    prev_c = closes.shift(1)
    tr     = pd.concat([highs - lows,
                        (highs - prev_c).abs(),
                        (lows  - prev_c).abs()], axis=1).max(axis=1)
    atr    = float(tr.ewm(span=period, adjust=False).mean().iloc[-1])
    price  = float(closes.iloc[-1])
    return atr if atr > 0 else price * 0.005


# ── ATR Intraday score (replicates signal_scorer atr_only mode) ──────────────

def _atr_score(df: pd.DataFrame, pos: int, day_start: int, prev_day_df) -> int:
    score = 0
    w     = df.iloc[max(0, pos - 120): pos + 1]
    closes = w["Close"].astype(float)
    n      = len(closes)
    price  = float(closes.iloc[-1])

    # 1. SMA50 trend
    score += 2 if price > float(closes.rolling(min(100, n)).mean().iloc[-1]) else -2
    # 2. SMA20 trend
    score += 1 if price > float(closes.rolling(min(40, n)).mean().iloc[-1]) else -1
    # 3. EMA9 momentum
    score += 1 if price > float(closes.ewm(span=18, adjust=False).mean().iloc[-1]) else -1

    # 4. RSI
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rsi   = float((100 - 100 / (1 + gain / loss.replace(0, 1e-10))).iloc[-1])
    if   35 <= rsi <= 55: score += 2
    elif rsi < 30:        score += 1
    elif rsi > 75:        score -= 3
    elif rsi > 65:        score -= 2

    # 5. MACD
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    sig   = macd.ewm(span=9, adjust=False).mean()
    hist  = float((macd - sig).iloc[-1])
    mv, sv = float(macd.iloc[-1]), float(sig.iloc[-1])
    if mv > sv and hist > 0:
        score += 2 if hist > abs(mv) * 0.1 else 1
    elif mv < sv and hist < 0:
        score -= 2 if abs(hist) > abs(mv) * 0.1 else 1

    # 6. ATR volatility filter
    atr_pct = _atr(df, pos) / price * 100 if price else 0
    if   atr_pct < 0.3:  score -= 2
    elif atr_pct > 2.5:  score -= 1
    else:                 score += 1

    # 7. VWAP (simple typical price mean, no volume on index)
    day_bars = df.iloc[day_start: pos + 1]
    vwap = float(((day_bars["High"].astype(float) +
                   day_bars["Low"].astype(float) +
                   day_bars["Close"].astype(float)) / 3).mean())
    score += 2 if price > vwap else -2

    # 8. ORB
    bars_in = pos - day_start
    if bars_in >= 3:
        orb = df.iloc[day_start: day_start + 3]
        if   price > float(orb["High"].max()): score += 3
        elif price < float(orb["Low"].min()):  score -= 3

    # 9. 15m trend (SMA9 vs SMA20)
    sma9  = float(closes.rolling(min(9,  n)).mean().iloc[-1])
    sma20 = float(closes.rolling(min(20, n)).mean().iloc[-1])
    score += 1 if sma9 > sma20 else -1

    # 10. 15m RSI bands
    if   35 <= rsi <= 55: score += 1
    elif rsi > 72:        score -= 2
    elif rsi < 28:        score += 2

    # 11. PDH/PDL
    if prev_day_df is not None and len(prev_day_df) > 0:
        pdh = float(prev_day_df["High"].astype(float).max())
        pdl = float(prev_day_df["Low"].astype(float).min())
        if   price > pdh:                        score += 2
        elif price < pdl:                        score -= 2
        elif abs(price - pdh) / pdh < 0.003:    score -= 1
        elif abs(price - pdl) / pdl < 0.003:    score += 1

    return max(-10, min(10, score))


# ── ICT + S&D scores ──────────────────────────────────────────────────────────

def _sd_entry_confirmation(df: pd.DataFrame, pos: int, side: str, sd_res: dict) -> bool:
    """Entry trigger on the base timeframe after HTF zone interaction."""
    if pos < 1 or side not in {"BUY", "SELL"}:
        return False

    curr = df.iloc[pos]
    prev = df.iloc[pos - 1]
    close_v = float(curr["Close"])
    open_v = float(curr["Open"])
    high_v = float(curr["High"])
    low_v = float(curr["Low"])
    prev_high = float(prev["High"])
    prev_low = float(prev["Low"])
    rng = max(high_v - low_v, 1e-9)
    close_pos = (close_v - low_v) / rng

    zone_lo = min(sd_res.get("sd_proximal") or close_v, sd_res.get("sd_distal") or close_v)
    zone_hi = max(sd_res.get("sd_proximal") or close_v, sd_res.get("sd_distal") or close_v)
    touched_zone = low_v <= zone_hi and high_v >= zone_lo

    if side == "BUY":
        return (
            close_v > prev_high
            or (close_v > open_v and close_pos >= 0.65)
            or (touched_zone and close_v > open_v and close_pos >= 0.55)
        )

    return (
        close_v < prev_low
        or (close_v < open_v and close_pos <= 0.35)
        or (touched_zone and close_v < open_v and close_pos <= 0.45)
    )


def _of_sd_scores(df: pd.DataFrame, pos: int, symbol: str = "NIFTY") -> dict:
    """Returns ICT score plus HTF S&D score confirmed by the base timeframe candle."""
    hist = df.iloc[: pos + 1]
    ict_window = hist.tail(OF_WINDOW)
    if len(hist) < 12:
        return {"ict": 0, "sd_score": 0, "sd_zone_type": None, "sd_tf": None, "sd_confirmed": False}

    price = float(df.iloc[pos]["Close"])
    try:
        from strategies.order_flow import (
            find_ict_signals, find_htf_sd_zones, htf_sd_zone_signal,
        )

        ict_res = find_ict_signals(ict_window)
        ict = ict_res.get("ict_liq_score", 0) + ict_res.get("ict_ob_score", 0)

        zones_by_tf = find_htf_sd_zones(hist, symbol=symbol, lookbacks=SD_HTF_LOOKBACKS)
        sd_res = htf_sd_zone_signal(price, zones_by_tf, symbol=symbol)
        sd_score = sd_res.get("sd_score", 0)
        side = "BUY" if sd_score > 0 else "SELL" if sd_score < 0 else None
        confirmed = _sd_entry_confirmation(df, pos, side, sd_res) if side else False
        if not confirmed:
            sd_score = 0

        return {
            "ict": ict,
            "sd_score": sd_score,
            "sd_zone_type": sd_res.get("sd_zone_type"),
            "sd_tf": sd_res.get("sd_tf"),
            "sd_confirmed": confirmed,
        }
    except Exception:
        return {"ict": 0, "sd_score": 0, "sd_zone_type": None, "sd_tf": None, "sd_confirmed": False}


# ── Core backtest loop ────────────────────────────────────────────────────────

def _run(df: pd.DataFrame, strategy: str, equity_start: float, trade_month: str | None = None) -> dict:
    """
    strategy: "atr_fixed" | "atr_trail" | "ict_sd_htf"

    Trailing SL rule:
      trail_dist = ATR × 1.0
      BUY : sl = max(sl, bar_high - trail_dist)
      SELL: sl = min(sl, bar_low  + trail_dist)
    """
    equity       = equity_start
    trades       = []
    equity_curve = []

    all_dates  = sorted(df["_date"].unique())
    date_start = {}
    for d in all_dates:
        day_df = df[df["_date"] == d]
        date_start[d] = df.index.get_loc(day_df.index[0])

    for day_i, day in enumerate(all_dates):
        day_df   = df[df["_date"] == day]
        day_idxs = [df.index.get_loc(ts) for ts in day_df.index]
        if len(day_idxs) < 5:
            continue
        if trade_month and str(day)[:7] != trade_month:
            continue
        if args.date and str(day) != args.date:
            continue

        day_start_pos    = day_idxs[0]
        prev_day_df      = df[df["_date"] == all_dates[day_i - 1]] if day_i > 0 else None
        day_equity_start = equity
        daily_loss_limit = day_equity_start * DAILY_LOSS_PCT / 100
        position         = None

        for local_pos, (ts, row) in enumerate(day_df.iterrows()):
            bar_time = ts.time() if hasattr(ts, "time") else time(12, 0)
            full_pos = day_idxs[local_pos]
            price    = float(row["Close"])
            bar_high = float(row["High"])
            bar_low  = float(row["Low"])

            # ── EOD force-close ──────────────────────────────────────────────
            if bar_time >= TRADE_EXIT and position:
                sign = 1 if position["side"] == "BUY" else -1
                pnl  = sign * (price - position["entry"]) * LOT_SIZE * position["qty"]
                trades.append({**_trade_meta(day, position), "exit": price,
                                "pnl": round(pnl, 2), "reason": "EOD"})
                equity  += pnl
                position = None
                continue

            if bar_time < TRADE_START:
                continue

            # ── Manage open position ─────────────────────────────────────────
            if position:
                use_trail = strategy != "atr_fixed"
                if use_trail:
                    atr_now    = _atr(df, full_pos)
                    trail_dist = atr_now * 1.0
                    if position["side"] == "BUY":
                        position["sl"] = max(position["sl"], round(bar_high - trail_dist, 2))
                    else:
                        position["sl"] = min(position["sl"], round(bar_low + trail_dist, 2))

                if position["side"] == "BUY":
                    if bar_low <= position["sl"]:
                        _close(trades, equity, day, position, position["sl"], "SL")
                        equity  += (position["sl"] - position["entry"]) * LOT_SIZE * position["qty"]
                        position = None
                    elif bar_high >= position["tp"]:
                        _close(trades, equity, day, position, position["tp"], "TP")
                        equity  += (position["tp"] - position["entry"]) * LOT_SIZE * position["qty"]
                        position = None
                else:
                    if bar_high >= position["sl"]:
                        _close(trades, equity, day, position, position["sl"], "SL")
                        equity  += (position["entry"] - position["sl"]) * LOT_SIZE * position["qty"]
                        position = None
                    elif bar_low <= position["tp"]:
                        _close(trades, equity, day, position, position["tp"], "TP")
                        equity  += (position["entry"] - position["tp"]) * LOT_SIZE * position["qty"]
                        position = None
                continue

            # ── Daily loss guard ─────────────────────────────────────────────
            if equity - day_equity_start <= -daily_loss_limit:
                break

            if NO_LUNCH and LUNCH_START <= bar_time <= LUNCH_END:
                continue

            min_hist = max(OF_WINDOW, 120)
            if full_pos < min_hist:
                continue

            # ── Compute signal ───────────────────────────────────────────────
            atr = _atr(df, full_pos)
            if atr <= 0 or price <= 0:
                continue

            atr_sig = _atr_score(df, full_pos, day_start_pos, prev_day_df)
            of_sd   = None  # lazy — only compute when needed

            if strategy == "atr_fixed":
                score     = atr_sig
                threshold = ATR_THRESHOLD

            elif strategy == "atr_trail":
                score     = atr_sig
                threshold = ATR_THRESHOLD

            elif strategy == "ict_sd_htf":
                of_sd = _of_sd_scores(df, full_pos)
                ict   = of_sd["ict"]
                sd    = of_sd["sd_score"]
                # ICT must fire AND HTF S&D must agree on direction
                if abs(ict) < ICT_THRESHOLD or abs(sd) == 0:
                    continue
                if (ict > 0) != (sd > 0):
                    continue
                score     = ict + sd
                threshold = ICT_THRESHOLD

            else:
                continue

            # ── Size and enter ───────────────────────────────────────────────
            risk_amt = equity * (RISK_PCT / 100)
            sl_dist  = atr
            tp_dist  = atr * RR_RATIO
            qty      = max(1, int(risk_amt / (sl_dist * LOT_SIZE)))

            if score >= threshold:
                position = {
                    "side":  "BUY",
                    "entry": price,
                    "sl":    round(price - sl_dist, 2),
                    "tp":    round(price + tp_dist, 2),
                    "qty":   qty,
                    "score": score,
                }
            elif score <= -threshold:
                position = {
                    "side":  "SELL",
                    "entry": price,
                    "sl":    round(price + sl_dist, 2),
                    "tp":    round(price - tp_dist, 2),
                    "qty":   qty,
                    "score": score,
                }

        equity_curve.append({"date": str(day), "equity": round(equity, 2)})

    return {"trades": trades, "final_equity": equity, "equity_curve": equity_curve}


def _trade_meta(day, pos):
    return {"date": str(day), "side": pos["side"], "entry": pos["entry"], "qty": pos["qty"], "score": pos.get("score")}


def _close(trades, equity, day, position, exit_p, reason):
    trades.append({
        **_trade_meta(day, position),
        "exit":   exit_p,
        "pnl":    round(
            (exit_p - position["entry"]) * LOT_SIZE * position["qty"]
            if position["side"] == "BUY"
            else (position["entry"] - exit_p) * LOT_SIZE * position["qty"],
            2,
        ),
        "reason": reason,
    })


# ── Stats helpers ─────────────────────────────────────────────────────────────

def _wr(trades):
    if not trades: return 0.0
    return round(sum(1 for t in trades if t["pnl"] > 0) / len(trades) * 100, 1)

def _pf(trades):
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    return round(gp / gl, 2) if gl > 0 else float("inf")

def _maxdd(eq_curve):
    peak, dd = 0.0, 0.0
    for pt in eq_curve:
        eq = pt["equity"]
        if eq > peak: peak = eq
        d = (peak - eq) / peak * 100 if peak > 0 else 0
        if d > dd: dd = d
    return round(dd, 1)

def _avg_hold(trades):
    """Approximate average hold bars (no timestamps — use trade index spacing)."""
    return "—"

def _expectancy(trades):
    if not trades: return 0.0
    wins  = [t["pnl"] for t in trades if t["pnl"] > 0]
    loss  = [t["pnl"] for t in trades if t["pnl"] < 0]
    wr    = len(wins) / len(trades)
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = abs(sum(loss) / len(loss)) if loss else 0
    return round(wr * avg_w - (1 - wr) * avg_l, 2)


# ── Run one strategy across months ───────────────────────────────────────────

def _run_months(df: pd.DataFrame, strategy: str) -> tuple:
    equity = INITIAL_CAPITAL
    rows   = []
    all_trades = []

    for ym in TARGET_MONTHS:
        mdf = df[df["_ym"] <= ym].copy()
        if len(mdf) < 10:
            print(f"  [{strategy}] {ym}: no data, skipping")
            continue

        res = _run(mdf, strategy, equity, trade_month=ym)
        fin = res["final_equity"]
        net = round((fin - equity) / equity * 100, 1)
        t   = res["trades"]
        all_trades.extend(t)
        rows.append({
            "month":    ym,
            "trades":   len(t),
            "wr":       _wr(t),
            "pf":       _pf(t),
            "dd":       _maxdd(res["equity_curve"]),
            "net_pct":  net,
            "start_eq": round(equity, 0),
            "end_eq":   round(fin, 0),
            "exp":      _expectancy(t),
        })
        equity = fin
        print(".", end="", flush=True)

    return rows, equity, all_trades


# ── Print table ───────────────────────────────────────────────────────────────

def _print_table(label: str, rows: list, final_eq: float):
    total = round((final_eq - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 1)
    w = 100
    print(f"\n{'='*w}")
    print(f"  {label}  [{TIMEFRAME}]  RR={RR_RATIO}  Capital=₹{INITIAL_CAPITAL:,.0f}")
    print(f"{'='*w}")
    print(f"{'Month':>8}  {'#Tr':>4}  {'Win%':>6}  {'PF':>5}  {'MaxDD%':>7}  "
          f"{'Net%':>7}  {'Exp₹':>8}  {'StartEq':>10}  {'EndEq':>10}")
    print('-' * w)
    for r in rows:
        pf_s  = f"{r['pf']:.2f}" if r["pf"] != float("inf") else ">99"
        flag  = " ✓" if r["net_pct"] > 0 and r["wr"] >= 35 and r["dd"] <= 20 else ""
        print(f"{r['month']:>8}  {r['trades']:>4}  {r['wr']:>6.1f}  {pf_s:>5}  "
              f"{r['dd']:>7.1f}  {r['net_pct']:>+7.1f}  "
              f"{r['exp']:>8,.0f}  {r['start_eq']:>10,.0f}  {r['end_eq']:>10,.0f}{flag}")
    print(f"\n  ₹{INITIAL_CAPITAL:,.0f} → ₹{final_eq:,.0f}  ({total:+.1f}% total over 3 months)")
    print(f"  ✓ = net positive + WR≥35% + MaxDD≤20%")
    return total


def _print_comparison(results: dict):
    """Side-by-side monthly comparison for all strategies."""
    strats = list(results.keys())
    w = 110
    print(f"\n{'='*w}")
    print(f"  STRATEGY COMPARISON  [{TIMEFRAME}]  Jan/Feb/Mar 2026")
    print(f"{'='*w}")
    print(f"  {'Month':>8}", end="")
    for s in strats:
        print(f"  │  {s:<14} {'Net%':>5} {'WR%':>5} {'DD%':>5}", end="")
    print()
    print('-' * w)

    for ym in TARGET_MONTHS:
        print(f"  {ym:>8}", end="")
        for s in strats:
            rows = {r["month"]: r for r in results[s]["rows"]}
            r = rows.get(ym)
            if r:
                flag = " ✓" if r["net_pct"] > 0 else "  "
                print(f"  │  {r['trades']:>3}tr {r['net_pct']:>+6.1f}% {r['wr']:>5.1f} {r['dd']:>5.1f}{flag}", end="")
            else:
                print(f"  │  {'—':>14}", end="")
        print()

    print()
    print(f"  {'TOTAL':>8}", end="")
    for s in strats:
        final = results[s]["final"]
        total = round((final - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 1)
        print(f"  │  {total:>+6.1f}% total              ", end="")
    print()
    print(f"\n  Best strategy by total return: ", end="")
    best = max(results.items(), key=lambda x: x[1]["final"])
    print(f"{best[0].upper()}  (₹{best[1]['final']:,.0f}  {round((best[1]['final']-INITIAL_CAPITAL)/INITIAL_CAPITAL*100,1):+.1f}%)")


# ── Main ──────────────────────────────────────────────────────────────────────

def _print_day_trades(strategy: str, trades: list, target_date: str):
    day_trades = [t for t in trades if t.get("date") == target_date]
    if not day_trades:
        print(f"  No trades fired on {target_date}")
        return
    print(f"\n  {'#':>2}  {'Side':<5}  {'Entry':>8}  {'Exit':>8}  {'Qty':>4}  {'P&L (₹)':>10}  {'Score':>6}  Reason")
    print(f"  {'-'*70}")
    total_pnl = 0
    for i, t in enumerate(day_trades, 1):
        pnl = t.get("pnl", 0)
        total_pnl += pnl
        flag = "WIN " if pnl > 0 else "LOSS"
        print(f"  {i:>2}  {t['side']:<5}  {t['entry']:>8.1f}  {t['exit']:>8.1f}  "
              f"{t['qty']:>4}  {pnl:>10,.0f}  {str(t.get('score','—')):>6}  {t.get('reason','—')}  [{flag}]")
    print(f"  {'-'*70}")
    print(f"  {'TOTAL':>46}  {total_pnl:>10,.0f}  ({len(day_trades)} trades, WR={_wr(day_trades):.0f}%)")


def main():
    # --date implies --months for that month
    if args.date and not args.months:
        TARGET_MONTHS.clear()
        TARGET_MONTHS.append(args.date[:7])

    period_label = args.date if args.date else " / ".join(TARGET_MONTHS)

    print(f"\n{'='*60}")
    print(f"  ATR / ICT + S&D HTF Backtest  (3 strategies)")
    print(f"  Timeframe: {TIMEFRAME}  |  Capital: ₹{INITIAL_CAPITAL:,.0f}  |  RR: {RR_RATIO}  |  Trail: ATR×1")
    print(f"  Period: {period_label}")
    print(f"{'='*60}\n")

    print("Loading data...")
    df5 = _load_5m()

    if TIMEFRAME == "15m":
        print("Resampling to 15m...")
        df = _resample_15m(df5)
    else:
        df = df5

    available = sorted(set(df["_ym"].unique()).intersection(TARGET_MONTHS))
    if not available:
        print(f"\n  ERROR: No data found for {TARGET_MONTHS}")
        print(f"  Available months in cache: {sorted(df5['_ym'].unique())}")
        print(f"  Re-run with --no-cache to fetch fresh data.")
        return

    print(f"  Months in data: {available}")
    print(f"  Total bars (with warmup history): {len(df)}\n")

    strategies = [
        ("atr_fixed",   "1. ATR Fixed SL      (baseline)"),
        ("atr_trail",   "2. ATR Trailing SL   (ATR×1 trail)"),
        ("ict_sd_htf",  "3. ICT + S&D HTF     (daily/60m zones, trailing SL)"),
    ]

    results = {}
    totals  = {}

    for key, label in strategies:
        print(f"\nRunning {label} ", end="", flush=True)
        rows, final_eq, all_trades = _run_months(df, key)
        results[key] = {"rows": rows, "final": final_eq, "trades": all_trades}
        totals[key]  = _print_table(label, rows, final_eq)
        if args.date:
            _print_day_trades(key, all_trades, args.date)

    if not args.date:
        _print_comparison(results)

        print(f"\n{'='*60}")
        print("  RECOMMENDATION")
        print(f"{'='*60}")
        ranked = sorted(results.items(), key=lambda x: x[1]["final"], reverse=True)
        for i, (key, res) in enumerate(ranked, 1):
            all_t = res["trades"]
            pct   = round((res["final"] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 1)
            print(f"  #{i} {key:<16} {pct:>+6.1f}%  WR={_wr(all_t):.1f}%  PF={_pf(all_t):.2f}  "
                  f"Trades={len(all_t)}  Exp=₹{_expectancy(all_t):,.0f}/trade")
    print()


if __name__ == "__main__":
    main()
