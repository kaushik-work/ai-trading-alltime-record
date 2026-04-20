"""
Fib-OF Pullback backtest for NIFTY intraday.

Tests timeframe variants:
  fib_15_5  : 15m Fibonacci anchor, 5m entry confirmation
  fib_5_5   : 5m Fibonacci anchor, 5m entry confirmation
  fib_15_15 : 15m Fibonacci anchor, 15m entry confirmation

The strategy is intentionally standalone so we can tune it before wiring it
into live BotRunner/TrendStrategy.
"""

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import time
from itertools import product as iterproduct

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from strategies.order_flow import analyse as order_flow_analyse


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "backtest_cache")

LOT_SIZE = 65
TRADE_START = time(9, 45)
TRADE_EXIT = time(15, 10)
LUNCH_START = time(12, 30)
LUNCH_END = time(13, 30)


@dataclass(frozen=True)
class Variant:
    key: str
    anchor_tf: str
    entry_tf: str


VARIANTS = [
    Variant("fib_15_5", "15m", "5m"),
    Variant("fib_5_5", "5m", "5m"),
    Variant("fib_15_15", "15m", "15m"),
]


def _load_nifty_5m(force_refetch: bool = False) -> pd.DataFrame:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_90 = os.path.join(CACHE_DIR, "NIFTY_5m_90d.csv")
    cache_150 = os.path.join(CACHE_DIR, "NIFTY_5m_150d.csv")

    if os.path.exists(cache_150) and not force_refetch:
        df = pd.read_csv(cache_150, index_col=0, parse_dates=True)
        df = _normalise_ohlcv(df)
        dates = sorted(df["_date"].unique())[-90:]
        return df[df["_date"].isin(dates)].copy()

    if os.path.exists(cache_90) and not force_refetch:
        return _normalise_ohlcv(pd.read_csv(cache_90, index_col=0, parse_dates=True))

    from data.angel_fetcher import AngelFetcher
    df = AngelFetcher.get().fetch_historical_df("NIFTY", "5m", days=90)
    if df is None or len(df) < 100:
        raise ValueError("Insufficient Angel One data. Check .env credentials.")
    df = _normalise_ohlcv(df)
    df.to_csv(cache_90)
    return df


def _normalise_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    rename = {c: c.capitalize() for c in df.columns if c.lower() in {"open", "high", "low", "close", "volume"}}
    df.rename(columns=rename, inplace=True)
    needed = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise ValueError(f"Missing OHLCV columns: {missing}")
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df["_date"] = df.index.date
    return df


def _resample_15m(df_5m: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for _, day in df_5m.groupby("_date"):
        ohlc = day[["Open", "High", "Low", "Close", "Volume"]].resample("15min", label="right", closed="right").agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }).dropna()
        ohlc["_date"] = ohlc.index.date
        frames.append(ohlc)
    return pd.concat(frames).sort_index()


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < 2:
        return 0.0
    w = df.tail(period * 2)
    high = w["High"].astype(float)
    low = w["Low"].astype(float)
    close = w["Close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return float(tr.ewm(span=period, adjust=False).mean().iloc[-1])


def _trend(df: pd.DataFrame) -> str:
    if len(df) < 20:
        return "none"
    close = df["Close"].astype(float)
    ema9 = float(close.ewm(span=9, adjust=False).mean().iloc[-1])
    sma20 = float(close.rolling(20).mean().iloc[-1])
    if ema9 > sma20:
        return "up"
    if ema9 < sma20:
        return "down"
    return "none"


def _fib_setup(anchor: pd.DataFrame, current_price: float, lookback: int, min_impulse_pct: float) -> dict | None:
    if len(anchor) < max(20, lookback):
        return None
    recent = anchor.tail(lookback)
    trend = _trend(anchor)
    atr = _atr(anchor)

    if trend == "up":
        low_ts = recent["Low"].idxmin()
        after_low = recent.loc[low_ts:]
        if len(after_low) < 2:
            return None
        high_ts = after_low["High"].idxmax()
        swing_low = float(recent.loc[low_ts, "Low"])
        swing_high = float(after_low.loc[high_ts, "High"])
        diff = swing_high - swing_low
        if diff <= 0:
            return None
        zone_low = swing_high - diff * 0.618
        zone_high = swing_high - diff * 0.382
        impulse_ok = (diff / swing_low * 100) >= min_impulse_pct or diff >= atr * 1.2
        if impulse_ok and zone_low <= current_price <= zone_high:
            return {"side": "BUY", "low": swing_low, "high": swing_high, "zone_low": zone_low,
                    "zone_high": zone_high, "mid": swing_high - diff * 0.5, "atr": atr}

    if trend == "down":
        high_ts = recent["High"].idxmax()
        after_high = recent.loc[high_ts:]
        if len(after_high) < 2:
            return None
        low_ts = after_high["Low"].idxmin()
        swing_high = float(recent.loc[high_ts, "High"])
        swing_low = float(after_high.loc[low_ts, "Low"])
        diff = swing_high - swing_low
        if diff <= 0:
            return None
        zone_low = swing_low + diff * 0.382
        zone_high = swing_low + diff * 0.618
        impulse_ok = (diff / swing_high * 100) >= min_impulse_pct or diff >= atr * 1.2
        if impulse_ok and zone_low <= current_price <= zone_high:
            return {"side": "SELL", "low": swing_low, "high": swing_high, "zone_low": zone_low,
                    "zone_high": zone_high, "mid": swing_low + diff * 0.5, "atr": atr}

    return None


def _confirm(entry: pd.DataFrame, side: str) -> bool:
    if len(entry) < 2:
        return False
    prev = entry.iloc[-2]
    curr = entry.iloc[-1]
    rng = max(float(curr["High"] - curr["Low"]), 1e-9)
    close_pos = (float(curr["Close"]) - float(curr["Low"])) / rng
    if side == "BUY":
        return bool(float(curr["Close"]) > float(prev["High"]) or (close_pos >= 0.7 and curr["Close"] > curr["Open"]))
    return bool(float(curr["Close"]) < float(prev["Low"]) or (close_pos <= 0.3 and curr["Close"] < curr["Open"]))


def _order_flow_score(entry_5m: pd.DataFrame, price: float, side: str) -> tuple[int, dict]:
    if len(entry_5m) < 25:
        return 0, {}
    window = entry_5m.tail(60)
    try:
        of = order_flow_analyse(window, price, "NIFTY", proximity_ticks=3)
    except Exception:
        return 0, {}

    score = 0
    if side == "BUY":
        if of.get("at_hps") or of.get("at_dhps"):
            score += 1
        if of.get("session_delta", 0) > 0 or of.get("d_session_delta", 0) > 0:
            score += 1
        if of.get("ict_liq_score", 0) > 0 or of.get("ict_ob_score", 0) > 0:
            score += 1
        if of.get("at_hrs") or of.get("at_dhrs"):
            score -= 2
    else:
        if of.get("at_hrs") or of.get("at_dhrs"):
            score -= 1
        if of.get("session_delta", 0) < 0 or of.get("d_session_delta", 0) < 0:
            score -= 1
        if of.get("ict_liq_score", 0) < 0 or of.get("ict_ob_score", 0) < 0:
            score -= 1
        if of.get("at_hps") or of.get("at_dhps"):
            score += 2
    return score, of


def _vwap(df_5m_upto: pd.DataFrame) -> float:
    """Cumulative VWAP for the day up to this bar."""
    tp = (df_5m_upto["High"] + df_5m_upto["Low"] + df_5m_upto["Close"]) / 3.0
    vol = df_5m_upto["Volume"].astype(float)
    total_vol = vol.sum()
    if total_vol == 0:
        return float(df_5m_upto["Close"].iloc[-1])
    return float((tp * vol).sum() / total_vol)


def _rsi(df: pd.DataFrame, period: int = 14) -> float:
    """RSI(period) on Close; returns 50 if insufficient data."""
    if len(df) < period + 1:
        return 50.0
    close = df["Close"].astype(float)
    delta = close.diff().dropna()
    gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    return float(100 - 100 / (1 + rs.iloc[-1]))


def _orb_range(df_5m_day: pd.DataFrame) -> tuple[float, float] | None:
    """Opening range: all 5m bars before TRADE_START (9:45)."""
    orb = df_5m_day[[t.time() < TRADE_START for t in df_5m_day.index]]
    if len(orb) < 2:
        return None
    return float(orb["Low"].min()), float(orb["High"].max())


def _sd_zones(anchor_upto: pd.DataFrame, lookback: int = 40) -> tuple[list[float], list[float]]:
    """Pivot lows = demand zones, pivot highs = supply zones."""
    if len(anchor_upto) < 6:
        return [], []
    data = anchor_upto.tail(lookback)
    highs = data["High"].astype(float).values
    lows = data["Low"].astype(float).values
    demand, supply = [], []
    for i in range(2, len(data) - 2):
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            demand.append(lows[i])
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            supply.append(highs[i])
    return demand, supply


def _score_signal(setup: dict, entry: pd.DataFrame, entry_5m: pd.DataFrame, price: float, threshold: int) -> dict:
    side = setup["side"]
    score = 0
    score += 2 if side == "BUY" else -2
    score += 2 if side == "BUY" else -2
    score += 2 if side == "BUY" else -2

    if _confirm(entry, side):
        score += 2 if side == "BUY" else -2

    of_delta, of = _order_flow_score(entry_5m, price, side)
    score += of_delta

    action = "HOLD"
    if score >= threshold:
        action = "BUY"
    elif score <= -threshold:
        action = "SELL"
    return {"score": score, "action": action, "order_flow": of, "of_delta": of_delta}


def _run_variant(
    df_5m: pd.DataFrame,
    df_15m: pd.DataFrame,
    variant: Variant,
    capital: float,
    risk_pct: float,
    rr: float,
    threshold: int,
    lookback: int,
    min_impulse_pct: float,
    no_lunch: bool,
    max_trades_day: int,
    carry_forward: bool,
    use_vwap: bool = False,
    use_orb: bool = False,
    use_rsi: bool = False,
    use_trend: bool = False,
    use_of_gate: bool = False,
    use_atr_min: bool = False,
    atr_min_pts: float = 35.0,
    use_sd: bool = False,
    use_fib_tp: bool = False,   # TP = swing extreme (0% level) instead of fixed R:R
    use_fib_sl: bool = False,   # SL = 78.6% level (tight, high R:R) instead of swing low
    combo_key: str = "",
) -> dict:
    anchor_all = df_15m if variant.anchor_tf == "15m" else df_5m
    entry_all = df_15m if variant.entry_tf == "15m" else df_5m
    equity = capital
    trades = []
    equity_curve = []
    position = None

    for day, entry_day in entry_all.groupby("_date"):
        day_start_equity = equity
        day_trades = 0
        anchor_day = anchor_all[anchor_all["_date"] == day]
        entry_5m_day = df_5m[df_5m["_date"] == day]
        if len(anchor_day) < 20 or len(entry_day) < 5:
            continue

        for ts, row in entry_day.iterrows():
            bar_time = ts.time()
            price = float(row["Close"])

            if position and not carry_forward and bar_time >= TRADE_EXIT:
                pnl = _close_position(position, price)
                equity += pnl
                trades.append({**position, "date": str(day), "exit_time": ts, "exit": price,
                               "pnl": round(pnl, 2), "reason": "EOD"})
                position = None
                continue

            if position:
                if position["side"] == "BUY" and (price <= position["sl"] or price >= position["tp"]):
                    exit_price = position["sl"] if price <= position["sl"] else position["tp"]
                    reason = "SL" if price <= position["sl"] else "TP"
                    pnl = _close_position(position, exit_price)
                    equity += pnl
                    trades.append({**position, "date": str(day), "exit_time": ts, "exit": exit_price,
                                   "pnl": round(pnl, 2), "reason": reason})
                    position = None
                elif position["side"] == "SELL" and (price >= position["sl"] or price <= position["tp"]):
                    exit_price = position["sl"] if price >= position["sl"] else position["tp"]
                    reason = "SL" if price >= position["sl"] else "TP"
                    pnl = _close_position(position, exit_price)
                    equity += pnl
                    trades.append({**position, "date": str(day), "exit_time": ts, "exit": exit_price,
                                   "pnl": round(pnl, 2), "reason": reason})
                    position = None
                continue

            if bar_time < TRADE_START or (not carry_forward and bar_time >= TRADE_EXIT):
                continue
            if no_lunch and LUNCH_START <= bar_time <= LUNCH_END:
                continue
            if day_trades >= max_trades_day:
                continue

            anchor_upto = anchor_day[anchor_day.index <= ts]
            entry_upto = entry_day[entry_day.index <= ts]
            entry_5m_upto = entry_5m_day[entry_5m_day.index <= ts]
            setup = _fib_setup(anchor_upto, price, lookback, min_impulse_pct)
            if not setup:
                continue

            sig = _score_signal(setup, entry_upto, entry_5m_upto, price, threshold)
            if sig["action"] != setup["side"]:
                continue

            # --- optional filters ---
            side = setup["side"]
            if use_vwap:
                vwap = _vwap(entry_5m_upto)
                if side == "BUY" and price <= vwap:
                    continue
                if side == "SELL" and price >= vwap:
                    continue
            if use_orb:
                orb = _orb_range(entry_5m_day)
                if orb is None:
                    continue
                orb_low, orb_high = orb
                if side == "BUY" and price <= orb_high:
                    continue
                if side == "SELL" and price >= orb_low:
                    continue
            if use_rsi:
                rsi_val = _rsi(entry_upto)
                if side == "BUY" and rsi_val > 65:
                    continue
                if side == "SELL" and rsi_val < 35:
                    continue
            if use_trend:
                t = _trend(anchor_upto)
                if side == "BUY" and t != "up":
                    continue
                if side == "SELL" and t != "down":
                    continue
            if use_of_gate:
                of_d = sig["of_delta"]
                if side == "BUY" and of_d <= 0:
                    continue
                if side == "SELL" and of_d >= 0:
                    continue
            if use_atr_min:
                if setup["atr"] < atr_min_pts:
                    continue
            if use_sd:
                demand, supply = _sd_zones(anchor_upto)
                atr_buf = setup["atr"] * 0.75
                if side == "BUY":
                    near_demand = any(abs(price - d) <= atr_buf for d in demand)
                    if not near_demand:
                        continue
                else:
                    near_supply = any(abs(price - s) <= atr_buf for s in supply)
                    if not near_supply:
                        continue
            # --- end filters ---

            atr = max(setup["atr"], _atr(entry_upto), price * 0.002)
            risk_amt = equity * risk_pct / 100
            diff = setup["high"] - setup["low"]   # full swing range

            if setup["side"] == "BUY":
                # SL: tight at 78.6% retracement (just below deep pullback level)
                #     or traditional (swing low)
                fib786_sl = setup["high"] - diff * 0.786 - atr * 0.25
                if use_fib_sl:
                    sl = fib786_sl
                else:
                    sl_dist = max(atr, abs(price - setup["zone_low"]))
                    sl = min(setup["low"], price - sl_dist)
                # TP: swing high (0% level = natural Fibonacci target)
                #     or traditional fixed R:R
                if use_fib_tp:
                    tp = setup["high"]
                else:
                    tp = price + (price - sl) * rr
            else:
                fib786_sl = setup["low"] + diff * 0.786 + atr * 0.25
                if use_fib_sl:
                    sl = fib786_sl
                else:
                    sl_dist = max(atr, abs(price - setup["zone_high"]))
                    sl = max(setup["high"], price + sl_dist)
                if use_fib_tp:
                    tp = setup["low"]
                else:
                    tp = price - (sl - price) * rr

            sl_dist = abs(price - sl)
            qty = max(1, int(risk_amt / (sl_dist * LOT_SIZE)) if sl_dist > 0 else 1)

            position = {
                "variant": variant.key,
                "side": setup["side"],
                "entry_time": ts,
                "entry": price,
                "sl": round(sl, 2),
                "tp": round(tp, 2),
                "qty": qty,
                "score": sig["score"],
                "zone_low": round(setup["zone_low"], 2),
                "zone_high": round(setup["zone_high"], 2),
            }
            day_trades += 1

        if position and not carry_forward:
            last_ts = entry_day.index[-1]
            last_price = float(entry_day.iloc[-1]["Close"])
            pnl = _close_position(position, last_price)
            equity += pnl
            trades.append({**position, "date": str(day), "exit_time": last_ts, "exit": last_price,
                           "pnl": round(pnl, 2), "reason": "DAY_END"})
            position = None

        equity_curve.append({"date": str(day), "equity": round(equity, 2),
                             "day_pnl": round(equity - day_start_equity, 2)})

    if position:
        final_ts = entry_all.index[-1]
        final_price = float(entry_all.iloc[-1]["Close"])
        pnl = _close_position(position, final_price)
        equity += pnl
        trades.append({**position, "date": str(entry_all.iloc[-1]["_date"]), "exit_time": final_ts,
                       "exit": final_price, "pnl": round(pnl, 2), "reason": "FINAL_EXIT"})

    key = combo_key if combo_key else variant.key
    return {"variant": key, "trades": trades, "equity_curve": equity_curve,
            "final_equity": equity}


def _close_position(position: dict, exit_price: float) -> float:
    sign = 1 if position["side"] == "BUY" else -1
    return sign * (exit_price - position["entry"]) * LOT_SIZE * position["qty"]


def _month_rows(result: dict, capital: float) -> list[dict]:
    trades = pd.DataFrame(result["trades"])
    if trades.empty:
        return []
    trades["month"] = pd.to_datetime(trades["exit_time"]).dt.to_period("M").astype(str)
    rows = []
    running = capital
    for month, group in trades.groupby("month"):
        start = running
        pnl = float(group["pnl"].sum())
        running += pnl
        wins = int((group["pnl"] > 0).sum())
        losses = int((group["pnl"] < 0).sum())
        gross_profit = float(group.loc[group["pnl"] > 0, "pnl"].sum())
        gross_loss = abs(float(group.loc[group["pnl"] < 0, "pnl"].sum()))
        rows.append({
            "month": month,
            "trades": len(group),
            "wr": round(wins / len(group) * 100, 1) if len(group) else 0,
            "pf": round(gross_profit / gross_loss, 2) if gross_loss else float("inf"),
            "net": round(pnl, 2),
            "net_pct": round(pnl / start * 100, 1) if start else 0,
            "end": round(running, 2),
            "wins": wins,
            "losses": losses,
        })
    return rows


def _print_results(results: list[dict], capital: float) -> None:
    print("\nFib-OF Pullback month-wise results")
    print("=" * 96)
    for result in results:
        rows = _month_rows(result, capital)
        total_pct = (result["final_equity"] - capital) / capital * 100
        print(f"\n{result['variant']} | final Rs{result['final_equity']:,.0f} | total {total_pct:+.1f}% | trades {len(result['trades'])}")
        print("-" * 96)
        print(f"{'Month':>8} {'#Tr':>5} {'WR%':>6} {'PF':>6} {'Net':>10} {'Net%':>7} {'EndEq':>12}")
        for row in rows:
            pf = f"{row['pf']:.2f}" if row["pf"] != float("inf") else ">99"
            print(f"{row['month']:>8} {row['trades']:>5} {row['wr']:>6.1f} {pf:>6} "
                  f"Rs{row['net']:>8,.0f} {row['net_pct']:>+6.1f}% Rs{row['end']:>10,.0f}")

    print("\nSummary")
    print("-" * 96)
    ranked = sorted(results, key=lambda r: r["final_equity"], reverse=True)
    for result in ranked:
        trades = result["trades"]
        wr = round(sum(1 for t in trades if t["pnl"] > 0) / len(trades) * 100, 1) if trades else 0
        total_pct = (result["final_equity"] - capital) / capital * 100
        print(f"{result['variant']:<10} total={total_pct:+6.1f}% final=Rs{result['final_equity']:>10,.0f} "
              f"trades={len(trades):>4} wr={wr:>5.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, default=125_000.0)
    parser.add_argument("--risk", type=float, default=2.0)
    parser.add_argument("--rr", type=float, default=2.0)
    parser.add_argument("--threshold", type=int, default=6)
    parser.add_argument("--lookback", type=int, default=16)
    parser.add_argument("--min-impulse-pct", type=float, default=0.25)
    parser.add_argument("--max-trades-day", type=int, default=10)
    parser.add_argument("--no-lunch", action="store_true", default=True)
    parser.add_argument("--fib-tp", action="store_true",
                        help="Use swing extreme (Fibonacci 0%% level) as TP instead of fixed R:R.")
    parser.add_argument("--fib-sl", action="store_true",
                        help="Use 78.6%% Fibonacci level as SL (tight, better R:R) instead of swing low.")
    parser.add_argument("--carry-forward", action="store_true",
                        help="Do not force-close at 15:10; hold until SL/TP or final bar.")
    parser.add_argument("--variant", choices=[v.key for v in VARIANTS], default=None,
                        help="Run only one timeframe variant.")
    parser.add_argument("--refetch", action="store_true")
    parser.add_argument("--filter", type=str, default="",
                        help="Comma-separated filters to apply: vwap,orb,rsi,trend,of,atr_min,sd")
    parser.add_argument("--combo-all", action="store_true",
                        help="Run all 16 filter combinations (2^4) for the selected variant.")
    parser.add_argument("--rsi-combos", action="store_true",
                        help="Run RSI + OF/ATR-min/S&D combos for the selected variant.")
    args = parser.parse_args()

    df_5m = _load_nifty_5m(force_refetch=args.refetch)
    df_15m = _resample_15m(df_5m)

    print(f"Loaded NIFTY data: 5m={len(df_5m)} bars, 15m={len(df_15m)} bars")
    print(f"Params: capital=Rs{args.capital:,.0f}, risk={args.risk}%, rr={args.rr}, "
          f"threshold={args.threshold}, lookback={args.lookback}, max/day={args.max_trades_day}, "
          f"carry_forward={args.carry_forward}")

    variants = [v for v in VARIANTS if args.variant in (None, v.key)]

    if args.rsi_combos:
        rsi_combos = [
            ("base",            dict()),
            ("RSI",             dict(use_rsi=True)),
            ("OF",              dict(use_of_gate=True)),
            ("RSI+OF",          dict(use_rsi=True, use_of_gate=True)),
            ("ATR-min",         dict(use_atr_min=True)),
            ("RSI+ATR-min",     dict(use_rsi=True, use_atr_min=True)),
            ("S&D",             dict(use_sd=True)),
            ("RSI+S&D",         dict(use_rsi=True, use_sd=True)),
            ("RSI+OF+S&D",      dict(use_rsi=True, use_of_gate=True, use_sd=True)),
            ("RSI+ATRmin+S&D",  dict(use_rsi=True, use_atr_min=True, use_sd=True)),
        ]
        results = []
        for label, flags in rsi_combos:
            for variant in variants:
                key = f"{variant.key}  [{label}]"
                results.append(_run_variant(
                    df_5m=df_5m, df_15m=df_15m, variant=variant,
                    capital=args.capital, risk_pct=args.risk, rr=args.rr,
                    threshold=args.threshold, lookback=args.lookback,
                    min_impulse_pct=args.min_impulse_pct, no_lunch=args.no_lunch,
                    max_trades_day=args.max_trades_day, carry_forward=args.carry_forward,
                    use_fib_tp=args.fib_tp, use_fib_sl=args.fib_sl,
                    **flags, combo_key=key,
                ))
    elif args.combo_all:
        # 2^4 = 16 combos: vwap × orb × rsi × trend
        filter_names = ["V(vwap)", "O(orb)", "R(rsi)", "T(trend)"]
        results = []
        for v, o, r, t in iterproduct([False, True], repeat=4):
            label = "+".join(n for n, f in zip(filter_names, [v, o, r, t]) if f) or "base"
            for variant in variants:
                key = f"{variant.key}  [{label}]"
                results.append(_run_variant(
                    df_5m=df_5m, df_15m=df_15m, variant=variant,
                    capital=args.capital, risk_pct=args.risk, rr=args.rr,
                    threshold=args.threshold, lookback=args.lookback,
                    min_impulse_pct=args.min_impulse_pct, no_lunch=args.no_lunch,
                    max_trades_day=args.max_trades_day, carry_forward=args.carry_forward,
                    use_fib_tp=args.fib_tp, use_fib_sl=args.fib_sl,
                    use_vwap=v, use_orb=o, use_rsi=r, use_trend=t, combo_key=key,
                ))
    else:
        active = set(args.filter.lower().replace("-", "_").split(",")) if args.filter else set()
        results = [
            _run_variant(
                df_5m=df_5m, df_15m=df_15m, variant=variant,
                capital=args.capital, risk_pct=args.risk, rr=args.rr,
                threshold=args.threshold, lookback=args.lookback,
                min_impulse_pct=args.min_impulse_pct, no_lunch=args.no_lunch,
                max_trades_day=args.max_trades_day, carry_forward=args.carry_forward,
                use_fib_tp=args.fib_tp, use_fib_sl=args.fib_sl,
                use_vwap="vwap" in active, use_orb="orb" in active,
                use_rsi="rsi" in active, use_trend="trend" in active,
                use_of_gate="of" in active or "of_gate" in active,
                use_atr_min="atr_min" in active or "atr" in active,
                use_sd="sd" in active,
            )
            for variant in variants
        ]

    _print_results(results, args.capital)


if __name__ == "__main__":
    main()
