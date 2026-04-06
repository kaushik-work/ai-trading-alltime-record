"""
Full 4-Strategy Backtest — NIFTY 5m (90 days, month-on-month)

Each strategy runs INDEPENDENTLY with its own entry logic and capital.
This is the definitive pre-live comparison.

Strategy 1 — ATR Intraday
    Signals from 5m data: SMA trend, EMA momentum, RSI, MACD, ATR filter,
    VWAP (intraday), ORB breakout, 15-min trend, PDH/PDL
    Entry threshold : score >= +6 (BUY) or <= -6 (SELL)
    Note: volume/PCR/candlestick patterns unavailable for NIFTY index → neutral (0)

Strategy A — Delta Direction
    Both session_delta and d_session_delta point same direction
    Entry threshold : delta_score >= +2 or <= -2

Strategy B — Delta + Trendline Channel (DP Sir HPS-T)
    delta_score + tl_score combined
    Entry threshold : >= +2 or <= -2

Strategy C — Delta + TL + ICT (Order Blocks + Liquidity Sweeps)
    delta + tl + liq_sweep + ob_retest
    Entry threshold : >= +2 or <= -2

Usage:
    python scripts/backtest_full.py
    python scripts/backtest_full.py --rr 2.5 --no-lunch
    python scripts/backtest_full.py --no-cache
"""

import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging
logging.basicConfig(level=logging.WARNING)

import pandas as pd
import numpy as np
from datetime import time

parser = argparse.ArgumentParser()
parser.add_argument("--capital",    type=float, default=50_000.0)
parser.add_argument("--rr",         type=float, default=2.5,
                    help="Risk:Reward ratio (default 2.5 — backtest optimal)")
parser.add_argument("--risk",       type=float, default=2.0)
parser.add_argument("--no-cache",   action="store_true")
parser.add_argument("--no-lunch",   action="store_true",
                    help="Skip entries 12:30-13:30 (NSE lunch chop)")
parser.add_argument("--min-lots",   type=int, default=1,
                    help="Minimum lots per trade (default 1)")
parser.add_argument("--vix-filter", type=float, default=0.0,
                    help="Skip trading days where VIX > threshold (0 = off). "
                         "Use --vix-filter 20 to replicate live bot behaviour.")
parser.add_argument("--daily-loss", type=float, default=5.0,
                    help="Daily loss limit as %% of day equity (default 5%%). "
                         "Use 0 to disable. Live bot: 5%% = Rs6,250 on Rs1.25L.")
args = parser.parse_args()

INITIAL_CAPITAL = args.capital
RR_RATIO        = args.rr
RISK_PCT        = args.risk
FORCE_REFETCH   = args.no_cache
NO_LUNCH        = args.no_lunch
MIN_LOTS        = args.min_lots
VIX_FILTER      = args.vix_filter   # 0 = disabled
DAILY_LOSS_PCT  = args.daily_loss   # 0 = no limit

LOT_SIZE    = 65   # NIFTY lot size revised Feb 2026
TRADE_START = time(9, 45)
TRADE_EXIT  = time(15, 10)
LUNCH_START = time(12, 30)
LUNCH_END   = time(13, 30)
OF_WINDOW   = 60

ATR_THRESHOLD = 6    # ATR Intraday entry threshold (matches live bot MIN_SIGNAL_SCORE)
OF_THRESHOLD  = 2    # Order flow strategies threshold

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtest_cache")


# ── Data ─────────────────────────────────────────────────────────────────────

def _load_nifty_5m():
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_150 = os.path.join(CACHE_DIR, "NIFTY_5m_150d.csv")
    cache_90  = os.path.join(CACHE_DIR, "NIFTY_5m_90d.csv")

    if os.path.exists(cache_150) and not FORCE_REFETCH:
        df = pd.read_csv(cache_150, index_col=0, parse_dates=True)
        if "_date" not in df.columns:
            df["_date"] = df.index.date
        dates = sorted(df["_date"].unique())[-90:]
        df = df[df["_date"].isin(dates)]
        print(f"  (cache) NIFTY 5m 90d (from 150d): {len(df)} bars")
        return df

    if os.path.exists(cache_90) and not FORCE_REFETCH:
        df = pd.read_csv(cache_90, index_col=0, parse_dates=True)
        if "_date" not in df.columns:
            df["_date"] = df.index.date
        print(f"  (cache) NIFTY 5m 90d: {len(df)} bars")
        return df

    print("  Fetching NIFTY 5m 90d from Zerodha...")
    from data.zerodha_fetcher import ZerodhaFetcher
    df = ZerodhaFetcher.get().fetch_historical_df("NIFTY", "5m", days=90)
    if df is None or len(df) < 100:
        raise ValueError("Insufficient data. Run scripts/get_token.py first.")
    if "_date" not in df.columns:
        df["_date"] = df.index.date
    df.to_csv(cache_90)
    print(f"  (fetch) NIFTY 5m 90d: {len(df)} bars — cached")
    return df


# ── VIX daily data ────────────────────────────────────────────────────────────

def _load_vix_daily() -> dict:
    """
    Return {date: vix_close} for the last 120 days.
    Cached to backtest_cache/INDIA_VIX_daily.csv.

    Sources (tried in order):
      1. Zerodha KiteConnect  (needs valid access token)
      2. NSE India public API (no auth required — fallback)
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, "INDIA_VIX_daily.csv")

    if os.path.exists(cache_path) and not FORCE_REFETCH:
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        result = {pd.Timestamp(k).date(): float(v) for k, v in df["vix"].items()}
        print(f"  (cache) India VIX daily: {len(result)} days")
        return result

    # ── 1. Zerodha ──────────────────────────────────────────────────────────
    print("  Fetching India VIX daily from Zerodha...")
    try:
        from data.zerodha_fetcher import ZerodhaFetcher
        df_z = ZerodhaFetcher.get().fetch_vix_historical_df(days=120)
        if df_z is not None and len(df_z) >= 10:
            df_z.to_csv(cache_path)
            result = {pd.Timestamp(k).date(): float(v) for k, v in df_z["vix"].items()}
            print(f"  (zerodha) India VIX daily: {len(result)} days — cached")
            return result
    except Exception:
        pass

    # ── 2. NSE India (session-based, no auth required) ──────────────────────
    print("  Zerodha unavailable — trying NSE India...")
    try:
        import requests, json
        from datetime import datetime as dt
        today = dt.today()
        start = (today - pd.Timedelta(days=130)).strftime("%d-%b-%Y")
        end   = today.strftime("%d-%b-%Y")
        hdrs  = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/",
        }
        sess = requests.Session()
        sess.get("https://www.nseindia.com", headers=hdrs, timeout=10)
        sess.get("https://www.nseindia.com/market-data/india-vix-index-historical",
                 headers=hdrs, timeout=10)
        url  = (f"https://www.nseindia.com/api/historical/vixhistory?"
                f"startDate={start}&endDate={end}")
        resp = sess.get(url, headers=hdrs, timeout=15)
        resp.raise_for_status()
        records = resp.json().get("data", [])
        if not records:
            raise ValueError("Empty response from NSE")
        rows = []
        for r in records:
            date_str = r.get("EOD_TIMESTAMP", "")
            vix_val  = r.get("VIX_CLOSE") or r.get("CLOSE") or 0
            if date_str and vix_val:
                try:
                    d = pd.to_datetime(date_str, format="%d-%b-%Y").date()
                    rows.append({"date": d, "vix": float(str(vix_val).replace(",", ""))})
                except Exception:
                    pass
        if not rows:
            raise ValueError("No parseable rows from NSE")
        df_n = pd.DataFrame(rows).set_index("date")
        df_n.to_csv(cache_path)
        result = {r["date"]: r["vix"] for r in rows}
        print(f"  (NSE) India VIX daily: {len(result)} days — cached")
        return result
    except Exception as e:
        print(f"  [WARN] NSE VIX fetch failed ({e})")
        print(f"  Run scripts/get_token.py first to fetch VIX via Zerodha.")
        return {}


# ── ATR ───────────────────────────────────────────────────────────────────────

def _atr(df, pos, period=14):
    w      = df.iloc[max(0, pos - period * 2): pos + 1]
    closes = w["Close"].astype(float)
    highs  = w["High"].astype(float)
    lows   = w["Low"].astype(float)
    prev_c = closes.shift(1)
    tr     = pd.concat([highs - lows, (highs - prev_c).abs(),
                        (lows - prev_c).abs()], axis=1).max(axis=1)
    atr    = float(tr.ewm(span=period, adjust=False).mean().iloc[-1])
    return atr if atr > 0 else float(closes.iloc[-1]) * 0.005


# ── Strategy 1: ATR Intraday signal score ─────────────────────────────────────

def _atr_score(df_full, pos, day_start_pos, prev_day_df):
    """
    Reconstruct ATR Intraday score from 5m NIFTY data.
    Replicates signal_scorer.py logic using only data available at bar `pos`.

    Approximations vs live bot:
      - SMA50/20/EMA9 use 5m rolling periods (100/40/18 bars) instead of daily
      - VWAP = simple mean of typical price since session open (no volume on index)
      - Volume/PCR/candlestick signals unavailable → neutral (0)
    """
    score      = 0
    curr_price = float(df_full.iloc[pos]["Close"])
    w          = df_full.iloc[max(0, pos - 120): pos + 1]
    closes     = w["Close"].astype(float)
    n          = len(closes)

    # ── 1. SMA50 trend (proxy: SMA100 on 5m = ~8h ≈ 1 trading day) ───────────
    sma100 = float(closes.rolling(min(100, n)).mean().iloc[-1])
    score += 2 if curr_price > sma100 else -2

    # ── 2. SMA20 trend (proxy: SMA40 on 5m = ~3.3h) ──────────────────────────
    sma40 = float(closes.rolling(min(40, n)).mean().iloc[-1])
    score += 1 if curr_price > sma40 else -1

    # ── 3. EMA9 momentum (proxy: EMA18 on 5m = 90 min) ───────────────────────
    ema18 = float(closes.ewm(span=18, adjust=False).mean().iloc[-1])
    score += 1 if curr_price > ema18 else -1

    # ── 4. RSI 14 ─────────────────────────────────────────────────────────────
    delta  = closes.diff()
    gain   = delta.clip(lower=0).rolling(14).mean()
    loss   = (-delta.clip(upper=0)).rolling(14).mean()
    rs     = gain / loss.replace(0, 1e-10)
    rsi    = float((100 - 100 / (1 + rs)).iloc[-1])
    if 35 <= rsi <= 55:
        score += 2
    elif rsi < 30:
        score += 1
    elif rsi > 75:
        score -= 3
    elif rsi > 65:
        score -= 2

    # ── 5. MACD (12, 26, 9) ───────────────────────────────────────────────────
    ema12       = closes.ewm(span=12, adjust=False).mean()
    ema26       = closes.ewm(span=26, adjust=False).mean()
    macd_line   = ema12 - ema26
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_val    = float(macd_line.iloc[-1])
    sig_val     = float(macd_signal.iloc[-1])
    hist_val    = macd_val - sig_val
    if macd_val > sig_val and hist_val > 0:
        score += 2 if hist_val > abs(macd_val) * 0.1 else 1
    elif macd_val < sig_val and hist_val < 0:
        score -= 2 if abs(hist_val) > abs(macd_val) * 0.1 else 1

    # ── 6. ATR volatility filter ──────────────────────────────────────────────
    atr     = _atr(df_full, pos)
    atr_pct = atr / curr_price * 100 if curr_price > 0 else 0
    if atr_pct < 0.3:
        score -= 2
    elif atr_pct > 2.5:
        score -= 1
    else:
        score += 1

    # ── 7. VWAP (intraday, no volume — uses simple typical price mean) ────────
    day_bars = df_full.iloc[day_start_pos: pos + 1]
    typical  = ((day_bars["High"].astype(float) +
                 day_bars["Low"].astype(float) +
                 day_bars["Close"].astype(float)) / 3)
    vwap     = float(typical.mean())
    score   += 2 if curr_price > vwap else -2

    # ── 8. ORB (first 3 bars = 9:15–9:25, 15-min opening range) ─────────────
    # Only score ORB after it's established (bar 3+ of the day)
    bars_into_day = pos - day_start_pos
    if bars_into_day >= 3:
        orb = df_full.iloc[day_start_pos: day_start_pos + 3]
        orb_high = float(orb["High"].max())
        orb_low  = float(orb["Low"].min())
        if curr_price > orb_high:
            score += 3
        elif curr_price < orb_low:
            score -= 3

    # ── 9. 15-min trend (SMA9 vs SMA20 on 5m bars) ───────────────────────────
    sma9  = float(closes.rolling(min(9, n)).mean().iloc[-1])
    sma20 = float(closes.rolling(min(20, n)).mean().iloc[-1])
    if sma9 > sma20:
        score += 1
    elif sma9 < sma20:
        score -= 1

    # ── 10. 15-min RSI (same RSI series, different thresholds) ───────────────
    if 35 <= rsi <= 55:
        score += 1
    elif rsi > 72:
        score -= 2
    elif rsi < 28:
        score += 2

    # ── 11. PDH / PDL ─────────────────────────────────────────────────────────
    if prev_day_df is not None and len(prev_day_df) > 0:
        pdh = float(prev_day_df["High"].astype(float).max())
        pdl = float(prev_day_df["Low"].astype(float).min())
        if curr_price > pdh:
            score += 2
        elif curr_price < pdl:
            score -= 2
        elif abs(curr_price - pdh) / pdh < 0.003:
            score -= 1
        elif abs(curr_price - pdl) / pdl < 0.003:
            score += 1

    return max(-10, min(10, score))


# ── Order-flow scores (A, B, C) ───────────────────────────────────────────────

def _of_scores(df_full, pos, symbol="NIFTY"):
    from strategies.order_flow import analyse as of_analyse
    from strategies.order_flow import find_ict_signals

    start  = max(0, pos - OF_WINDOW + 1)
    window = df_full.iloc[start: pos + 1]
    if len(window) < 6:
        return 0, 0, 0

    current_price = float(df_full.iloc[pos]["Close"])
    try:
        result = of_analyse(window, current_price, symbol)
    except Exception:
        return 0, 0, 0

    sess_delta = result.get("session_delta", 0)
    d_delta    = result.get("d_session_delta", 0)
    delta_score = +2 if (sess_delta > 0 and d_delta > 0) else \
                  -2 if (sess_delta < 0 and d_delta < 0) else 0

    tl_score       = result.get("tl_score_delta", 0)
    liq_score      = result.get("ict_liq_score", 0)
    ob_score       = result.get("ict_ob_score", 0)

    score_b = delta_score + tl_score
    score_c = score_b + liq_score + ob_score

    return delta_score, score_b, score_c


# ── Backtest loop ─────────────────────────────────────────────────────────────

def _run(df, strategy, equity_start, rr=RR_RATIO, risk_pct=RISK_PCT,
         daily_loss_pct=3.0, symbol="NIFTY", vix_map: dict = None, vix_threshold: float = 0.0):
    """
    strategy: "atr" | "delta" | "combined" | "ict"
    vix_map: {date: vix_close} — if provided and vix_threshold > 0, skip days where VIX > threshold
    """
    equity       = equity_start
    trades       = []
    equity_curve = []
    dates        = sorted(df["_date"].unique())

    # Build date → (start_idx, prev_day_df) lookup
    all_dates  = sorted(df["_date"].unique())
    date_start = {}
    for i, d in enumerate(all_dates):
        day_df = df[df["_date"] == d]
        date_start[d] = df.index.get_loc(day_df.index[0])

    for day_i, day in enumerate(dates):
        day_df   = df[df["_date"] == day]
        day_idxs = [df.index.get_loc(ts) for ts in day_df.index]
        if len(day_idxs) < 5:
            continue

        # VIX filter — skip entire day if VIX exceeds threshold
        if vix_map and vix_threshold > 0:
            day_vix = vix_map.get(day)
            if day_vix is not None and day_vix > vix_threshold:
                equity_curve.append({"date": str(day), "equity": round(equity, 2)})
                continue  # sit out this day

        day_start_pos    = day_idxs[0]
        prev_day_df      = df[df["_date"] == all_dates[day_i - 1]] if day_i > 0 else None
        day_equity_start = equity
        daily_loss_limit = (day_equity_start * daily_loss_pct / 100) if daily_loss_pct > 0 else float("inf")
        position         = None

        for local_pos, (ts, row) in enumerate(day_df.iterrows()):
            bar_time = ts.time() if hasattr(ts, "time") else time(12, 0)
            full_pos = day_idxs[local_pos]

            # EOD force-close
            if bar_time >= TRADE_EXIT and position:
                exit_p = float(row["Close"])
                sign   = 1 if position["side"] == "BUY" else -1
                pnl    = sign * (exit_p - position["entry"]) * LOT_SIZE * position["qty"]
                trades.append({"date": str(day), "side": position["side"],
                                "entry": position["entry"], "exit": exit_p,
                                "qty": position["qty"], "pnl": round(pnl, 2),
                                "reason": "EOD"})
                equity  += pnl
                position = None
                continue

            if bar_time < TRADE_START:
                continue

            # Manage open position
            if position:
                price = float(row["Close"])
                if position["side"] == "BUY":
                    if price <= position["sl"] or price >= position["tp"]:
                        reason = "SL" if price <= position["sl"] else "TP"
                        exit_p = position["sl"] if reason == "SL" else position["tp"]
                        pnl    = (exit_p - position["entry"]) * LOT_SIZE * position["qty"]
                        trades.append({"date": str(day), "side": "BUY",
                                       "entry": position["entry"], "exit": exit_p,
                                       "qty": position["qty"], "pnl": round(pnl, 2),
                                       "reason": reason})
                        equity  += pnl
                        position = None
                else:
                    if price >= position["sl"] or price <= position["tp"]:
                        reason = "SL" if price >= position["sl"] else "TP"
                        exit_p = position["sl"] if reason == "SL" else position["tp"]
                        pnl    = (position["entry"] - exit_p) * LOT_SIZE * position["qty"]
                        trades.append({"date": str(day), "side": "SELL",
                                       "entry": position["entry"], "exit": exit_p,
                                       "qty": position["qty"], "pnl": round(pnl, 2),
                                       "reason": reason})
                        equity  += pnl
                        position = None
                continue

            if equity - day_equity_start <= -daily_loss_limit:
                break

            if NO_LUNCH and LUNCH_START <= bar_time <= LUNCH_END:
                continue

            # Need enough history for indicators
            if full_pos < max(OF_WINDOW, 120):
                continue

            # Compute score
            if strategy == "atr":
                score = _atr_score(df, full_pos, day_start_pos, prev_day_df)
                threshold = ATR_THRESHOLD
            else:
                d_s, b_s, c_s = _of_scores(df, full_pos, symbol)
                if strategy == "delta":
                    score = d_s
                elif strategy == "combined":
                    score = b_s
                else:  # ict
                    score = c_s
                threshold = OF_THRESHOLD

            if score == 0:
                continue

            atr   = _atr(df, full_pos)
            price = float(row["Close"])
            if price <= 0 or atr <= 0:
                continue

            risk_amt = equity * (risk_pct / 100)
            sl_dist  = atr
            tp_dist  = atr * rr
            qty      = max(MIN_LOTS, int(risk_amt / (sl_dist * LOT_SIZE)))

            if score >= threshold:
                position = {"side": "BUY", "entry": price,
                            "sl": round(price - sl_dist, 2),
                            "tp": round(price + tp_dist, 2), "qty": qty}
            elif score <= -threshold:
                position = {"side": "SELL", "entry": price,
                            "sl": round(price + sl_dist, 2),
                            "tp": round(price - tp_dist, 2), "qty": qty}

        equity_curve.append({"date": str(day), "equity": round(equity, 2)})

    return {"trades": trades, "final_equity": equity, "equity_curve": equity_curve}


# ── Stats ─────────────────────────────────────────────────────────────────────

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


def _print_table(rows, title, start_cap, final_eq):
    total = round((final_eq - start_cap) / start_cap * 100, 1)
    print(f"\n{'='*84}")
    print(f"  {title}")
    print(f"{'='*84}")
    print(f"{'Month':>8}  {'#Tr':>4}  {'Win%':>6}  {'PF':>5}  {'DD%':>5}  "
          f"{'Net%':>6}  {'StartEq':>10}  {'EndEq':>10}")
    print('-' * 84)
    for r in rows:
        pf_s = f"{r['pf']:.2f}" if r["pf"] != float("inf") else ">99"
        ok   = " ***" if r["net_pct"] > 0 and r["wr"] >= 35 and r["dd"] <= 20 else ""
        print(f"{r['month']:>8}  {r['trades']:>4}  {r['wr']:>6.1f}  {pf_s:>5}  "
              f"{r['dd']:>5.1f}  {r['net_pct']:>6.1f}  "
              f"{r['start_eq']:>10,.0f}  {r['end_eq']:>10,.0f}{ok}")
    print(f"\n  Start: Rs{start_cap:,.0f}  ->  End: Rs{final_eq:,.0f}  ({total:+.1f}% total)")
    print("  *** = net positive + WR>=35% + MaxDD<=20%")
    return total


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_scenario(df, months, strategy_key, vix_map, vix_threshold, daily_loss_pct=0.0):
    """Run one strategy for all months. Returns (rows, final_equity, summary_dict)."""
    equity = INITIAL_CAPITAL
    rows   = []
    for ym in months:
        month_df = df[df["_ym"] == ym].drop(columns=["_ym"])
        if "_date" not in month_df.columns:
            month_df = month_df.copy()
            month_df["_date"] = month_df.index.date
        if len(month_df) < 20:
            continue
        try:
            res = _run(month_df, strategy_key, equity,
                       vix_map=vix_map, vix_threshold=vix_threshold,
                       daily_loss_pct=daily_loss_pct)
            fin = res["final_equity"]
            net = round((fin - equity) / equity * 100, 1)
            rows.append({
                "month": str(ym), "trades": len(res["trades"]),
                "wr": _wr(res["trades"]), "pf": _pf(res["trades"]),
                "dd": _maxdd(res["equity_curve"]),
                "net_pct": net,
                "start_eq": round(equity, 0), "end_eq": round(fin, 0),
            })
            equity = fin
            print(".", end="", flush=True)
        except Exception as e:
            print(f"\n  [SKIP] {ym}: {e}")
    n_months = max(len(rows), 1)
    s = {
        "total":           round((equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 1),
        "final":           equity,
        "avg_wr":          round(sum(r["wr"] for r in rows) / n_months, 1) if rows else 0,
        "avg_dd":          round(sum(r["dd"] for r in rows) / n_months, 1) if rows else 0,
        "months_positive": sum(1 for r in rows if r["net_pct"] > 0),
        "total_trades":    sum(r["trades"] for r in rows),
        "rows":            rows,
    }
    return rows, equity, s


def _print_vix_comparison(label, title, rows_off, rows_on, vix_thr, n_months):
    w = 90
    print(f"\n{'='*w}")
    print(f"  {title}")
    print(f"{'='*w}")
    hdr = f"{'Month':>8}  {'#Tr(off)':>8} {'WR%':>6} {'DD%':>5} {'Net%':>6}  │  {'#Tr(on)':>7} {'WR%':>6} {'DD%':>5} {'Net%':>6}  {'VIX>':>5}{vix_thr}"
    print(hdr)
    print('-' * w)

    # align by month
    months_off = {r["month"]: r for r in rows_off}
    months_on  = {r["month"]: r for r in rows_on}
    all_months = sorted(set(list(months_off.keys()) + list(months_on.keys())))

    for m in all_months:
        ro = months_off.get(m)
        rv = months_on.get(m)
        def fmt(r):
            if r is None: return f"{'—':>8} {'—':>6} {'—':>5} {'—':>6}"
            ok = " *" if r["net_pct"] > 0 else "  "
            return f"{r['trades']:>8} {r['wr']:>6.1f} {r['dd']:>5.1f} {r['net_pct']:>+6.1f}{ok}"
        print(f"{m:>8}  {fmt(ro)}  │  {fmt(rv)}")

    print()


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    lunch_tag = " + no-lunch" if NO_LUNCH else ""
    vix_thr   = VIX_FILTER if VIX_FILTER > 0 else 25.0   # default comparison threshold
    dl_tag    = f" | daily-loss {DAILY_LOSS_PCT:.0f}%" if DAILY_LOSS_PCT > 0 else " | no daily-loss limit"

    print(f"\nATR + C-ICT Backtest — Daily Loss Rule: OFF vs {DAILY_LOSS_PCT:.0f}% | Month-on-Month")
    print(f"  Capital: Rs{INITIAL_CAPITAL:,.0f}  |  R:R {RR_RATIO}  |  Risk {RISK_PCT}%/trade  |  Min lots {MIN_LOTS}{lunch_tag}\n")

    df = _load_nifty_5m()
    df.index    = pd.to_datetime(df.index)
    df["_ym"]   = df.index.to_period("M")
    df["_date"] = df.index.date

    vix_map = _load_vix_daily()
    if not vix_map:
        print("  [WARN] No VIX data — VIX filter disabled\n")

    months   = sorted(df["_ym"].unique())
    n_months = max(len(months), 1)
    print(f"  Months: {len(months)}  |  Bars: {len(df)}  |  VIX days loaded: {len(vix_map)}\n")

    strategies = [
        ("atr", "ATR Intraday", "Strategy 1 — ATR Intraday  (VWAP + ORB + SMA + RSI + MACD + PDH/PDL)"),
        ("ict", "C-ICT",        "Strategy C — Delta + TL + ICT OB + Sweep"),
    ]

    final_summary = {}

    for key, label, title in strategies:
        # Scenario A: no daily loss limit
        print(f"Running {label} — no daily-loss limit ...", end="", flush=True)
        rows_nodl, _, s_nodl = _run_scenario(
            df, months, key,
            vix_map=vix_map, vix_threshold=vix_thr,
            daily_loss_pct=0.0,
        )
        print()

        # Scenario B: with daily loss limit
        dl_label = f"{DAILY_LOSS_PCT:.0f}% limit"
        print(f"Running {label} — {dl_label} ...", end="", flush=True)
        rows_dl, _, s_dl = _run_scenario(
            df, months, key,
            vix_map=vix_map, vix_threshold=vix_thr,
            daily_loss_pct=DAILY_LOSS_PCT,
        )
        print()

        # Print month-on-month side-by-side
        w = 90
        print(f"\n{'='*w}")
        print(f"  {title}")
        print(f"  No daily-loss limit  vs  {dl_label}  (VIX filter >{vix_thr:.0f})")
        print(f"{'='*w}")
        hdr = (f"{'Month':>8}  {'#Tr(no-lim)':>11} {'WR%':>6} {'DD%':>5} {'Net%':>6}"
               f"  │  {'#Tr(limit)':>10} {'WR%':>6} {'DD%':>5} {'Net%':>6}")
        print(hdr)
        print('-' * w)
        m_nodl = {r["month"]: r for r in rows_nodl}
        m_dl   = {r["month"]: r for r in rows_dl}
        for m in sorted(set(list(m_nodl) + list(m_dl))):
            def fmt(r):
                if r is None: return f"{'—':>11} {'—':>6} {'—':>5} {'—':>6}"
                ok = " *" if r["net_pct"] > 0 else "  "
                return f"{r['trades']:>11} {r['wr']:>6.1f} {r['dd']:>5.1f} {r['net_pct']:>+6.1f}{ok}"
            print(f"{m:>8}  {fmt(m_nodl.get(m))}  │  {fmt(m_dl.get(m))}")
        print()

        final_summary[label] = {"nodl": s_nodl, "dl": s_dl}

    # ── Final comparison table ────────────────────────────────────────────────
    w = 95
    print(f"\n{'='*w}")
    print(f"  FINAL SUMMARY  |  {MIN_LOTS} lots  |  R:R {RR_RATIO}  |  VIX>{vix_thr:.0f}  |  Daily-loss limit: {DAILY_LOSS_PCT:.0f}%")
    print(f"{'='*w}")
    print(f"{'Strategy':<14}  {'Scenario':>16}  {'Total%':>8}  {'Final Eq':>11}  "
          f"{'AvgWR%':>7}  {'AvgDD%':>7}  {'Trades':>7}  {'Net/mo':>10}")
    print('-' * w)

    for label, sc in final_summary.items():
        for tag, s in [("No limit", sc["nodl"]), (f"DL {DAILY_LOSS_PCT:.0f}%", sc["dl"])]:
            monthly_avg = round((s["final"] - INITIAL_CAPITAL) / n_months, 0)
            charges     = round((s["total_trades"] / n_months) * MIN_LOTS * 150, 0)
            net_mo      = monthly_avg - charges
            print(f"{label:<14}  {tag:>16}  {s['total']:>+8.1f}%  "
                  f"Rs{s['final']:>9,.0f}  {s['avg_wr']:>7.1f}%  {s['avg_dd']:>7.1f}%  "
                  f"{s['total_trades']:>7}  Rs{net_mo:>8,.0f}/mo")
        print()

    print(f"  VIX data coverage: {len(vix_map)} days loaded from cache/Zerodha")
    print(f"  Days skipped by VIX filter: visible as missing months in ON column above")
    print()
