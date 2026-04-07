"""
Phase 6 — Backtesting Engine

Replays historical 15-min candle data (yfinance, up to 60 days) through
the AishDoc signal scorer. Simulates:
  - Entry:  score >= MIN_SIGNAL_SCORE during 9:45–15:10 window
  - SL:     1× ATR below entry price  (BUY) / above (SELL/short)
  - TP:     2× ATR above entry price  (BUY) / below (SELL/short)
  - EOD:    force-close at 15:10 if still open

No Claude API calls — pure rule-based replay.
"""

import logging
from datetime import time
from typing import Optional

import pandas as pd

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from strategies.signal_scorer import score_symbol
from strategies.patterns import detect_patterns

logger = logging.getLogger(__name__)

def _fetch_backtest_data(symbol: str, period: str, interval: str) -> pd.DataFrame:
    """
    Fetch historical OHLCV for backtesting.
    Priority: Zerodha → NSE India. No yfinance.
    """
    days = {"60d": 60, "30d": 30, "90d": 90}.get(period, 60)

    # 1. Zerodha
    try:
        from data.zerodha_fetcher import ZerodhaFetcher
        df = ZerodhaFetcher.get().fetch_historical_df(symbol, interval, days=days)
        if df is not None and len(df) >= 20:
            logger.info("Backtest data: Zerodha — %d bars for %s %s", len(df), symbol, interval)
            return df
    except Exception as e:
        logger.warning("Backtest fetch_data Zerodha failed: %s", e)

    raise ValueError(
        f"No data available for {symbol} {interval} {period}. "
        "Ensure Zerodha credentials are set (run scripts/get_token.py for today's token)."
    )

TRADE_START = time(9, 45)
TRADE_EXIT  = time(15, 10)


class BacktestEngine:
    """
    Run a vectorised bar-by-bar backtest for a single symbol.
    """

    def __init__(self, initial_capital: float = 20_000.0):
        self.initial_capital = initial_capital

    # ── Data ──────────────────────────────────────────────────────────────────

    def fetch_data(self, symbol: str, period: str = "60d",
                   interval: str = "15m") -> pd.DataFrame:
        """Fetch OHLCV via Zerodha → NSE India. No yfinance."""
        df = _fetch_backtest_data(symbol, period, interval)
        if "_date" not in df.columns:
            df["_date"] = df.index.date
        return df

    # ── Indicator computation (no lookahead) ──────────────────────────────────

    def _indicators(self, df: pd.DataFrame, up_to: int) -> dict:
        """Compute indicators on df.iloc[:up_to+1] — no future data used."""
        w       = df.iloc[: up_to + 1]
        closes  = w["Close"].astype(float)
        highs   = w["High"].astype(float)
        lows    = w["Low"].astype(float)
        n       = len(closes)
        price   = float(closes.iloc[-1])

        sma_20 = float(closes.rolling(20).mean().iloc[-1]) if n >= 20 else price
        sma_50 = float(closes.rolling(50).mean().iloc[-1]) if n >= 50 else sma_20
        ema_9  = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])

        # RSI(14)
        delta = closes.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, 1e-9)
        rsi   = float((100 - 100 / (1 + rs)).iloc[-1]) if n >= 14 else 50.0

        # MACD(12,26,9)
        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        macd  = float((ema12 - ema26).iloc[-1])
        msig  = float((ema12 - ema26).ewm(span=9, adjust=False).mean().iloc[-1])

        # Bollinger Bands(20,2σ)
        bb_mid = closes.rolling(20).mean()
        bb_std = closes.rolling(20).std()
        bb_upper = float((bb_mid + 2 * bb_std).iloc[-1]) if n >= 20 else price * 1.02
        bb_lower = float((bb_mid - 2 * bb_std).iloc[-1]) if n >= 20 else price * 0.98

        # ATR(14)
        prev_c = closes.shift(1)
        tr = pd.concat([
            highs - lows,
            (highs - prev_c).abs(),
            (lows  - prev_c).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.ewm(span=14, adjust=False).mean().iloc[-1]) if n >= 14 else price * 0.01
        atr_pct = round((atr / price) * 100, 2)

        # Volume
        vol     = int(w["Volume"].iloc[-1])
        avg_vol = int(w["Volume"].rolling(20).mean().iloc[-1]) if n >= 20 else vol
        vol_r   = round(vol / avg_vol, 2) if avg_vol else 1.0

        return {
            "symbol": "BACKTEST", "price": round(price, 2),
            "rsi": round(rsi, 2),
            "macd": round(macd, 4), "macd_signal": round(msig, 4),
            "macd_histogram": round(macd - msig, 4),
            "sma_20": round(sma_20, 2), "sma_50": round(sma_50, 2),
            "ema_9":  round(ema_9, 2),
            "bollinger_upper": round(bb_upper, 2),
            "bollinger_lower": round(bb_lower, 2),
            "price_vs_sma20": round(((price - sma_20) / sma_20) * 100, 2),
            "atr_14": round(atr, 2), "atr_pct": atr_pct,
            "volume": vol, "avg_volume_20d": avg_vol, "volume_ratio": vol_r,
            "change_pct": 0.0,
        }

    def _intraday(self, day_df: pd.DataFrame, pos: int,
                  pdh: Optional[float], pdl: Optional[float],
                  mins_per_bar: int = 15) -> dict:
        """Compute VWAP, ORB, 15-min trend, RSI for bars up to pos within day."""
        w     = day_df.iloc[: pos + 1]
        price = float(w["Close"].iloc[-1])

        # VWAP
        typical = (w["High"] + w["Low"] + w["Close"]) / 3
        vol_sum = w["Volume"].sum()
        vwap    = float((typical * w["Volume"]).sum() / vol_sum) if vol_sum > 0 else price

        # ORB — first N bars covering ORB_WINDOW_MINS (e.g. 3 bars on 5m, 1 bar on 15m)
        orb_n    = max(1, config.ORB_WINDOW_MINS // mins_per_bar)
        orb_win  = day_df.iloc[:orb_n]
        orb_high = float(orb_win["High"].max())
        orb_low  = float(orb_win["Low"].min())

        # 15-min rolling trend within the day
        closes_d  = w["Close"].astype(float)
        n_d       = len(closes_d)
        sma9_15m  = float(closes_d.ewm(span=min(9, n_d), adjust=False).mean().iloc[-1])
        sma20_15m = float(closes_d.rolling(min(20, n_d)).mean().iloc[-1]) if n_d >= 1 else price
        trend_15m = "uptrend" if sma9_15m > sma20_15m else "downtrend"

        # 15-min RSI
        delta_d = closes_d.diff()
        gain_d  = delta_d.clip(lower=0).rolling(min(14, n_d)).mean()
        loss_d  = (-delta_d.clip(upper=0)).rolling(min(14, n_d)).mean()
        rs_d    = gain_d / loss_d.replace(0, 1e-9)
        rsi_15m = float((100 - 100 / (1 + rs_d)).iloc[-1]) if n_d >= 4 else 50.0

        result = {
            "price": round(price, 2),
            "vwap":  round(vwap, 2),
            "orb_high": round(orb_high, 2),
            "orb_low":  round(orb_low, 2),
            "orb_broken_up":   price > orb_high,
            "orb_broken_down": price < orb_low,
            "trend_15m": trend_15m,
            "rsi_15m":   round(rsi_15m, 2),
        }
        if pdh is not None:
            result["pdh"] = pdh
        if pdl is not None:
            result["pdl"] = pdl
        return result

    # ── Core backtest loop ────────────────────────────────────────────────────

    def run(self, symbol: str, period: str = "60d",
            interval: str = "15m",
            min_score: int = 7,
            risk_pct: float = 2.0,
            daily_loss_limit_pct: float = 3.0,
            rr_ratio: float = 2.0,
            strategy: str = "ATR Intraday",
            _df=None) -> dict:
        """
        Bar-by-bar replay. Returns trades list + daily equity curve.

        Args:
            interval             : candle timeframe — '5m' or '15m'
            min_score            : signal score threshold (default 7 — high conviction only)
            risk_pct             : % of equity risked per trade (default 2%)
            daily_loss_limit_pct : stop trading for the day after losing this % of day-start equity
            _df                  : pre-fetched DataFrame slice (skips fetch_data if provided)
        """

        if strategy == "Raijin":
            return self.run_vwap_snap(symbol, period=period, interval=interval,
                                      risk_pct=risk_pct, rr_ratio=rr_ratio,
                                      daily_loss_limit_pct=daily_loss_limit_pct)

        mins_per_bar = {"1m":1,"2m":2,"5m":5,"15m":15,"30m":30}.get(interval, 15)
        logger.info("Backtest: %s  period=%s  interval=%s  min_score=%d  risk=%.1f%%",
                    symbol, period, interval, min_score, risk_pct)
        if _df is not None:
            df = _df.copy()
            if "_date" not in df.columns:
                df["_date"] = df.index.date
        else:
            df = self.fetch_data(symbol, period, interval=interval)
        days = sorted(df["_date"].unique())

        equity       = self.initial_capital
        equity_curve = []
        trades       = []
        position     = None  # None or open-trade dict
        threshold    = min_score

        # Pre-build a cumulative index mapping (day → start index in df)
        global_offset = 0
        day_offsets   = {}
        for d in days:
            day_offsets[d] = global_offset
            global_offset += int((df["_date"] == d).sum())

        # Previous-day high / low (updated at start of each day)
        pdh = pdl = None

        MAX_TRADES_PER_DAY = 3

        for day_idx, day in enumerate(days):
            day_df = df[df["_date"] == day].copy()
            g_start = day_offsets[day]
            day_trade_count = 0       # reset each day
            day_start_equity = equity # for daily loss limit

            # PDH / PDL from previous day
            if day_idx > 0:
                prev_day    = days[day_idx - 1]
                prev_day_df = df[df["_date"] == prev_day]
                pdh = round(float(prev_day_df["High"].max()), 2)
                pdl = round(float(prev_day_df["Low"].min()), 2)
            else:
                pdh = pdl = None

            for pos, (ts, row) in enumerate(day_df.iterrows()):
                bar_time = ts.time()
                g_idx    = g_start + pos   # index into full df

                # Options config (same for both strategies — both trade CE/PE)
                is_bn_atr    = symbol == "BANKNIFTY"
                lot_size_atr = config.LOT_SIZES.get("BANKNIFTY", 15) if is_bn_atr else config.LOT_SIZES.get("NIFTY", 25)
                opt_delta    = 0.45
                strike_gap   = 100 if is_bn_atr else 50

                from backtesting.charges import charges_for_trade as _chg

                def _close_atr(exit_px: float, reason: str):
                    nonlocal equity, day_trade_count, position
                    trade_delta = position.get("option_delta", opt_delta)
                    gross = self._pnl_options(position, exit_px, trade_delta, lot_size_atr)
                    chg   = _chg(
                        entry_atr = position["atr"],
                        gross_pnl = gross,
                        lot_size  = lot_size_atr,
                        num_lots  = position["quantity"],
                        opt_delta = trade_delta,
                        dte       = position.get("dte_at_entry", 5.0),
                    )
                    units        = lot_size_atr * position["quantity"]
                    prem_change  = gross / (opt_delta * units) if units > 0 else 0.0
                    exit_premium = round(max(0.05, position["entry_premium"] + prem_change), 2)
                    net = gross - chg["total"]
                    equity += net
                    day_trade_count += 1
                    trades.append({
                        **position,
                        "exit_time":         str(ts),
                        "exit_price":        round(exit_px, 2),
                        "exit_premium":      exit_premium,
                        "exit_reason":       reason,
                        "pnl_gross":         round(gross, 2),
                        "charges":           round(chg["total"], 2),
                        "charges_breakdown": chg,
                        "pnl":               round(net, 2),
                        "equity":            round(equity, 2),
                    })
                    position = None

                # ── EOD square-off ────────────────────────────────────────
                if position and bar_time >= TRADE_EXIT:
                    _close_atr(float(row["Close"]), "EOD")
                    continue

                # ── SL / TP check on open position ────────────────────────
                if position:
                    hi = float(row["High"])
                    lo = float(row["Low"])
                    if position["side"] == "BUY":
                        if lo <= position["sl"]:
                            _close_atr(position["sl"], "SL"); continue
                        if hi >= position["tp"]:
                            _close_atr(position["tp"], "TP"); continue
                    else:
                        if hi >= position["sl"]:
                            _close_atr(position["sl"], "SL"); continue
                        if lo <= position["tp"]:
                            _close_atr(position["tp"], "TP"); continue

                # ── Entry: only in trading window, no open position ────────
                if bar_time < TRADE_START or bar_time >= TRADE_EXIT:
                    continue
                if position:
                    continue
                if day_trade_count >= MAX_TRADES_PER_DAY:
                    continue

                # Daily loss limit — stop trading for the day
                daily_loss_pct = (day_start_equity - equity) / day_start_equity * 100
                if daily_loss_pct >= daily_loss_limit_pct:
                    continue

                # Need enough history for indicators
                if g_idx < 25:
                    continue

                inds   = self._indicators(df, g_idx)
                intrad = self._intraday(day_df, pos, pdh, pdl, mins_per_bar)

                # ── Trend filter — only trade with the trend ───────────────
                price_now = inds["price"]
                sma50     = inds["sma_50"]
                trend_up  = price_now > sma50

                # Candlestick patterns from last 3 bars of day (up to now)
                slice_rows = day_df.iloc[max(0, pos - 2): pos + 1]
                candles = [
                    {"open":   float(r["Open"]),  "high":  float(r["High"]),
                     "low":    float(r["Low"]),   "close": float(r["Close"]),
                     "volume": int(r["Volume"])}
                    for _, r in slice_rows.iterrows()
                ]
                pat = detect_patterns(candles)

                oi     = {"sentiment": "neutral", "pcr": 1.0}
                result = score_symbol(inds, oi, pat, intrad)
                action = result["action"]
                score  = result["score"]

                if action not in ("BUY", "SELL"):
                    continue

                # ── Score threshold (was computed but never applied — bug fix) ──
                if score < threshold:
                    continue

                # Apply trend filter — skip counter-trend signals
                if action == "BUY"  and not trend_up:
                    continue
                if action == "SELL" and trend_up:
                    continue

                entry = float(row["Close"])
                atr   = inds["atr_14"]
                if atr <= 0:
                    continue

                # Options position sizing: SL = 1×ATR, risk via lot count
                risk_amt         = equity * (risk_pct / 100)
                max_loss_per_lot = atr * opt_delta * lot_size_atr
                num_lots         = max(1, int(risk_amt / max_loss_per_lot))

                if action == "BUY":
                    sl = round(entry - atr, 2)
                    tp = round(entry + rr_ratio * atr, 2)
                else:
                    sl = round(entry + atr, 2)
                    tp = round(entry - rr_ratio * atr, 2)

                # Option metadata — strike chosen so premium ≈ target range
                option_type      = "CE" if action == "BUY" else "PE"
                expiry_date, dte = self._expiry_for_trade(ts.date(), symbol)
                t_min, t_max     = self.PREMIUM_TARGET.get(symbol, (180, 200))
                strike, entry_premium, opt_delta_trade = self._select_strike(
                    entry, atr, dte, strike_gap, action, interval, t_min, t_max
                )

                # Re-size with the actual delta of the chosen strike
                max_loss_per_lot  = atr * opt_delta_trade * lot_size_atr
                num_lots          = max(1, int(risk_amt / max(1, max_loss_per_lot)))
                cost_per_lot      = max(1.0, entry_premium * lot_size_atr)
                max_lots_capital  = max(1, int(config.MAX_TRADE_AMOUNT / cost_per_lot))
                num_lots          = min(num_lots, max_lots_capital)

                position = {
                    "symbol":        symbol,
                    "side":          action,
                    "option_type":   option_type,
                    "strike":        strike,
                    "expiry":        str(expiry_date),
                    "dte_at_entry":  dte,
                    "entry_premium": round(entry_premium, 2),
                    "option_delta":  opt_delta_trade,
                    "entry_time":    str(ts),
                    "entry_price":   round(entry, 2),
                    "quantity":      num_lots,
                    "lot_size":      lot_size_atr,
                    "sl":            sl,
                    "tp":            tp,
                    "score":         score,
                    "atr":           round(atr, 2),
                }

            # Record equity at end of day
            equity_curve.append({"date": str(day), "equity": round(equity, 2)})

        return {
            "symbol":          symbol,
            "period":          period,
            "initial_capital": self.initial_capital,
            "final_equity":    round(equity, 2),
            "trades":          trades,
            "equity_curve":    equity_curve,
        }

    # ── Strike selection ─────────────────────────────────────────────────────

    # Target premium range per symbol (₹) — ATM weekly options.
    # WHY ₹180-220 for NIFTY (not lower):
    #   Same 20pt adverse spot move on a ₹200 option = ₹9 drop = 4.5% loss.
    #   Same move on a ₹80 option = ₹9 drop = 11.25% loss — SL hits on noise.
    #   Low-premium (OTM/near-expiry) options decay faster proportionally.
    #   ₹180-220 = ATM, 4-7 DTE, proper delta 0.45 — maximum theta/delta efficiency.
    # Capital implication: 1 lot NIFTY @ ₹200 × 65 units = ₹13,000 per lot.
    #   Min recommended capital: ₹50K (1 lot = 26% of capital).
    PREMIUM_TARGET = {
        "NIFTY":     (150, 220),
        "BANKNIFTY": (300, 450),
    }

    @staticmethod
    def _select_strike(spot: float, atr: float, dte: float,
                       strike_gap: int, action: str, interval: str,
                       target_min: float, target_max: float):
        """
        Find the strike whose estimated premium falls in [target_min, target_max].

        ATM premium:
            P_ATM = C_tf × ATR_bar × √DTE
            C_tf: empirical bar→daily ATR multiplier × 0.4 (Black-Scholes coeff)
                  5m=1.80, 15m=1.40, 2m=2.20, 30m=1.10

        OTM premium — moneyness-based Gaussian decay (matches weekly NSE options):
            σ_dollar = P_ATM / 0.4   (dollar volatility for the DTE window)
            k        = n × strike_gap / σ_dollar
            P(n)     = P_ATM × exp(−0.5 × k² × 4.0)
            Δ(n)     = 0.45  × exp(−0.5 × k² × 5.2)

        ITM (ATM premium below target): premium rises going ITM.
            P_ITM(n) = P_ATM × exp(+0.4 × k_itm)
            Δ_ITM(n) = min(0.92, 0.45 + n × 0.06)

        Returns: (strike, entry_premium, option_delta)
        """
        import math
        C_tf         = {"2m": 2.20, "5m": 1.80, "15m": 1.40, "30m": 1.10}.get(interval, 1.40)
        atm_premium  = C_tf * atr * math.sqrt(max(dte, 1))
        atm_strike   = round(spot / strike_gap) * strike_gap
        target_mid   = (target_min + target_max) / 2
        is_call      = action == "BUY"
        sigma_dollar = max(1.0, atm_premium / 0.4)

        best_n     = 0
        best_diff  = abs(atm_premium - target_mid)
        best_prem  = atm_premium
        best_delta = 0.45
        best_itm   = False

        # Scan OTM direction
        for n in range(1, 40):
            k     = (n * strike_gap) / sigma_dollar
            prem  = atm_premium * math.exp(-0.5 * k * k * 4.0)
            delta = max(0.04, 0.45 * math.exp(-0.5 * k * k * 5.2))
            diff  = abs(prem - target_mid)
            if diff < best_diff:
                best_diff  = diff
                best_n     = n
                best_prem  = prem
                best_delta = delta
                best_itm   = False
            if prem < target_min * 0.4:
                break

        # Scan ITM direction (when ATM too cheap)
        if atm_premium < target_min:
            for n in range(1, 12):
                k_itm  = (n * strike_gap) / sigma_dollar
                prem   = atm_premium * math.exp(0.4 * k_itm)
                delta  = min(0.92, 0.45 + n * 0.06)
                diff   = abs(prem - target_mid)
                if diff < best_diff:
                    best_diff  = diff
                    best_n     = n
                    best_prem  = prem
                    best_delta = delta
                    best_itm   = True
                if prem > target_max * 1.5:
                    break

        if best_itm:
            # ITM: CE moves down, PE moves up
            strike = atm_strike - best_n * strike_gap if is_call \
                     else atm_strike + best_n * strike_gap
        else:
            # OTM: CE moves up, PE moves down
            strike = atm_strike + best_n * strike_gap if is_call \
                     else atm_strike - best_n * strike_gap

        return strike, round(best_prem, 2), round(best_delta, 3)

    # ── Expiry helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _weekly_expiry(ref_date, weekday: int):
        """Next occurrence of `weekday` on or after `ref_date`. Mon=0 … Sun=6."""
        from datetime import timedelta
        delta = (weekday - ref_date.weekday()) % 7
        return ref_date + timedelta(days=delta)

    @staticmethod
    def _expiry_for_trade(trade_date, symbol: str):
        """
        Return (expiry_date, dte_calendar) for the option to buy on `trade_date`.

        NSE weekly expiry schedule:
            NIFTY     → Thursday  (weekday 3)
            BANKNIFTY → Wednesday (weekday 2)

        Rule: if DTE to current weekly expiry ≤ 3 calendar days,
              switch to next week's expiry.
              This ensures we always have ≥ 4 calendar days of time value
              (avoids buying 0-1 DTE options that decay extremely fast).
        """
        from datetime import timedelta
        expiry_wd = 2 if symbol == "BANKNIFTY" else 3   # Wed or Thu

        current_expiry = BacktestEngine._weekly_expiry(trade_date, expiry_wd)
        dte = (current_expiry - trade_date).days

        if dte <= 3:
            # Too close — buy next week's expiry
            next_expiry = current_expiry + timedelta(days=7)
            dte = (next_expiry - trade_date).days
            return next_expiry, dte

        return current_expiry, dte

    # ── Mining in Water (scalp) backtest ─────────────────────────────────────

    def _run_scalp(self, symbol: str, period: str = "60d",
                   interval: str = "5m", min_score: int = 7,
                   risk_pct: float = 2.0, daily_loss_limit_pct: float = 3.0,
                   rr_ratio: float = 2.0) -> dict:
        import numpy as np
        from strategies.scalp_3min import (
            compute_heikin_ashi, detect_w_pattern, detect_m_pattern,
            compute_vwap, compute_atr, compute_ema, compute_rsi, score_scalp,
            TRADE_START as SCALP_START, TRADE_EXIT as SCALP_EXIT,
            MAX_TRADES_DAY,
        )
        from backtesting.charges import charges_for_trade

        # ── Per-symbol config ────────────────────────────────────────────────
        # BankNifty is ~2.5x more volatile than Nifty on 5m — wider SL needed.
        # Options lot sizes from config.LOT_SIZES (NIFTY=65, BANKNIFTY=30 as of Feb 2026).
        # ATM option delta ≈ 0.45 (used to convert spot-point P&L → premium P&L).
        is_bn        = symbol == "BANKNIFTY"
        sl_mult      = 1.5  if is_bn else 1.0   # ATR multiplier for SL
        lot_size     = config.LOT_SIZES.get("BANKNIFTY", 15) if is_bn else config.LOT_SIZES.get("NIFTY", 25)
        opt_delta    = 0.45                      # ATM CE/PE delta approximation
        # Enforce a floor on min_score per symbol regardless of UI setting:
        #   NIFTY    ≥ 8.5  → forces W/M + HAC + VWAP (4+3+1.5)
        #   BankNifty ≥ 9.0 → forces W/M + HAC + VWAP + 1 more confirmation
        score_floor  = 9.0  if is_bn else 8.5
        eff_min      = max(float(min_score), score_floor)

        df   = self.fetch_data(symbol, period, interval=interval)
        days = sorted(df["_date"].unique())

        opens  = df["Open"].astype(float).values
        highs  = df["High"].astype(float).values
        lows   = df["Low"].astype(float).values
        closes = df["Close"].astype(float).values
        vols   = df["Volume"].astype(float).values

        # Pre-compute global indicators (no lookahead)
        ha_open, ha_close, ha_bull, ha_bear = compute_heikin_ashi(opens, highs, lows, closes)
        atr_arr   = compute_atr(highs, lows, closes, period=14)
        ema50_arr = compute_ema(closes, period=50)
        rsi_arr   = compute_rsi(closes, period=14)

        global_offset = 0
        day_offsets: dict = {}
        for d in days:
            day_offsets[d] = global_offset
            global_offset += int((df["_date"] == d).sum())

        equity       = self.initial_capital
        equity_curve = []
        trades: list = []

        for day in days:
            day_df          = df[df["_date"] == day].copy()
            g_start         = day_offsets[day]
            day_trade_count = 0
            day_start_eq    = equity
            position        = None
            consec_bull     = 0
            consec_bear     = 0

            # Pre-extract intraday arrays for pattern detection (avoids cross-day contamination)
            day_lows  = day_df["Low"].astype(float).values
            day_highs = day_df["High"].astype(float).values

            for pos, (ts, row) in enumerate(day_df.iterrows()):
                bar_time = ts.time()
                g_idx    = g_start + pos

                # Track consecutive HA candles
                if g_idx > 0:
                    if ha_bull[g_idx]:
                        consec_bull = consec_bull + 1 if ha_bull[g_idx - 1] else 1
                        consec_bear = 0
                    elif ha_bear[g_idx]:
                        consec_bear = consec_bear + 1 if ha_bear[g_idx - 1] else 1
                        consec_bull = 0
                    else:
                        consec_bull = consec_bear = 0

                # ── Helper: close position, apply charges, record trade ───────
                def _close(exit_px: float, reason: str):
                    nonlocal equity, day_trade_count, position
                    trade_delta = position.get("option_delta", opt_delta)
                    gross = self._pnl_options(position, exit_px, trade_delta, lot_size)
                    chg   = charges_for_trade(
                        entry_atr  = position["atr"],
                        gross_pnl  = gross,
                        lot_size   = lot_size,
                        num_lots   = position["quantity"],
                        opt_delta  = trade_delta,
                        dte        = position.get("dte_at_entry", 5.0),
                    )
                    # Derive exit premium from gross P&L
                    units         = lot_size * position["quantity"]
                    prem_change   = gross / (opt_delta * units) if units > 0 else 0.0
                    exit_premium  = round(max(0.05, position["entry_premium"] + prem_change), 2)

                    net = gross - chg["total"]
                    equity += net
                    day_trade_count += 1
                    trades.append({
                        **position,
                        "exit_time":         str(ts),
                        "exit_price":        round(exit_px,       2),
                        "exit_premium":      exit_premium,
                        "exit_reason":       reason,
                        "pnl_gross":         round(gross,         2),
                        "charges":           round(chg["total"],  2),
                        "charges_breakdown": chg,
                        "pnl":               round(net,           2),
                        "equity":            round(equity,        2),
                    })
                    position = None

                # EOD square-off
                if position and bar_time >= SCALP_EXIT:
                    _close(float(row["Close"]), "EOD")
                    continue

                # SL / TP check
                if position:
                    hi = float(row["High"])
                    lo = float(row["Low"])
                    if position["side"] == "BUY":
                        if lo <= position["sl"]:
                            _close(position["sl"], "SL"); continue
                        if hi >= position["tp"]:
                            _close(position["tp"], "TP"); continue
                    else:
                        if hi >= position["sl"]:
                            _close(position["sl"], "SL"); continue
                        if lo <= position["tp"]:
                            _close(position["tp"], "TP"); continue

                # Entry gate
                if bar_time < SCALP_START or bar_time >= SCALP_EXIT:
                    continue
                if position or day_trade_count >= MAX_TRADES_DAY:
                    continue
                daily_loss = (day_start_eq - equity) / day_start_eq * 100
                if daily_loss >= daily_loss_limit_pct:
                    continue
                if g_idx < 20:
                    continue

                # Require ≥2 consecutive HA candles in the signal direction —
                # single-candle reversals are noise, especially on BankNifty
                consec = consec_bull if ha_bull[g_idx] else (consec_bear if ha_bear[g_idx] else 0)
                if consec < 2:
                    continue

                price = float(row["Close"])
                atr   = float(atr_arr[g_idx])
                if atr <= 0:
                    continue

                above_ema50 = price > float(ema50_arr[g_idx])
                rsi_val     = float(rsi_arr[g_idx])

                # Intraday VWAP (resets each day)
                d_hi  = day_highs[:pos + 1]
                d_lo  = day_lows[:pos + 1]
                d_cl  = day_df["Close"].astype(float).values[:pos + 1]
                d_vol = day_df["Volume"].astype(float).values[:pos + 1]
                vwap  = float(compute_vwap(d_hi, d_lo, d_cl, d_vol)[-1])
                above_vwap = price > vwap

                # Intraday-only W/M patterns — avoids cross-day false signals
                w_pat = detect_w_pattern(day_lows[:pos + 1], price)
                m_pat = detect_m_pattern(day_highs[:pos + 1], price)

                # Volume ratio
                avg_vol   = float(np.mean(vols[max(0, g_idx - 20):g_idx])) if g_idx >= 20 else float(vols[g_idx])
                vol_ratio = float(vols[g_idx]) / avg_vol if avg_vol > 0 else 1.0

                sig = score_scalp(
                    ha_bull=bool(ha_bull[g_idx]),
                    ha_bear=bool(ha_bear[g_idx]),
                    w_pattern=w_pat,
                    m_pattern=m_pat,
                    price_above_vwap=above_vwap,
                    price_above_ema50=above_ema50,
                    vol_ratio=vol_ratio,
                    consecutive_ha=consec,
                    rsi=rsi_val,
                )

                if sig["action"] not in ("BUY", "SELL") or sig["score"] < eff_min:
                    continue

                action  = sig["action"]
                sl_dist = sl_mult * atr   # BankNifty: 1.5×ATR, Nifty: 1.0×ATR

                # Options position sizing:
                #   max_loss_per_lot = sl_dist × delta × lot_size
                #   num_lots         = risk_amount / max_loss_per_lot
                risk_amt         = equity * (risk_pct / 100)
                max_loss_per_lot = sl_dist * opt_delta * lot_size
                num_lots         = max(1, int(risk_amt / max_loss_per_lot))

                if action == "BUY":
                    sl = round(price - sl_dist, 2)
                    tp = round(price + rr_ratio * sl_dist, 2)
                else:
                    sl = round(price + sl_dist, 2)
                    tp = round(price - rr_ratio * sl_dist, 2)

                # Option metadata — strike chosen so premium ≈ target range
                # Rule: always buy with DTE > 3 calendar days.
                option_type             = "CE" if action == "BUY" else "PE"
                expiry_date, dte        = self._expiry_for_trade(ts.date(), symbol)
                t_min, t_max            = self.PREMIUM_TARGET.get(symbol, (180, 200))
                strike, entry_premium, opt_delta_trade = self._select_strike(
                    price, atr, dte, strike_gap, action, interval, t_min, t_max
                )

                # Re-size with actual delta of the chosen strike
                max_loss_per_lot  = sl_dist * opt_delta_trade * lot_size
                num_lots          = max(1, int(risk_amt / max(1, max_loss_per_lot)))
                cost_per_lot      = max(1.0, entry_premium * lot_size)
                max_lots_capital  = max(1, int(config.MAX_TRADE_AMOUNT / cost_per_lot))
                num_lots          = min(num_lots, max_lots_capital)

                position = {
                    "symbol":        symbol,
                    "side":          action,
                    "option_type":   option_type,
                    "strike":        strike,
                    "expiry":        str(expiry_date),
                    "dte_at_entry":  dte,
                    "entry_premium": round(entry_premium, 2),
                    "option_delta":  opt_delta_trade,
                    "entry_time":    str(ts),
                    "entry_price":   round(price, 2),
                    "quantity":      num_lots,
                    "lot_size":      lot_size,
                    "sl":            sl,
                    "tp":            tp,
                    "score":         sig["score"],
                    "atr":           round(atr, 2),
                }

            equity_curve.append({"date": str(day), "equity": round(equity, 2)})

        return {
            "symbol":          symbol,
            "period":          period,
            "initial_capital": self.initial_capital,
            "final_equity":    round(equity, 2),
            "trades":          trades,
            "equity_curve":    equity_curve,
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    # ── Raijin backtest (NIFTY scalp, 5-min) ───────────────────────────────

    def run_vwap_snap(
        self,
        symbol: str = "NIFTY",
        period: str = "60d",
        interval: str = "5m",
        risk_pct: float = 4.0,
        rr_ratio: float = 2.0,
        daily_loss_limit_pct: float = 8.0,
        min_score: float = None,
        _df: pd.DataFrame = None,
    ) -> dict:
        """
        Raijin — NIFTY scalp options backtest.

        Signal: Mean reversion from VWAP ±2σ bands with HA flip + RSI extreme
        Timeframe: 5-minute bars
        R:R: 1:2.0 (SL = 0.6× ATR, TP = 1.2× ATR)
        Max 3 trades/day | Entry window 9:45-10:45 + 14:15-14:45
        """
        import numpy as np
        from strategies.nifty_scalp import (
            score_signal as scalp_score,
            in_entry_window,
            MAX_TRADES_DAY,
            EOD_EXIT,
        )
        from backtesting.charges import charges_for_trade

        lot_size   = config.LOT_SIZES.get("NIFTY", 25)
        opt_delta  = 0.45
        strike_gap = 50
        sl_mult    = 0.6  # tight SL for scalp

        df   = _df if _df is not None else self.fetch_data(symbol, period, interval=interval)
        days = sorted(df["_date"].unique())

        global_offset = 0
        day_offsets: dict = {}
        for d in days:
            day_offsets[d] = global_offset
            global_offset += int((df["_date"] == d).sum())

        all_closes = df["Close"].astype(float).values

        equity       = self.initial_capital
        equity_curve = []
        trades: list = []

        for day_idx, day in enumerate(days):
            day_df          = df[df["_date"] == day].copy()
            g_start         = day_offsets[day]
            day_trade_count = 0
            day_start_eq    = equity
            position        = None

            d_opens  = day_df["Open"].astype(float).values
            d_highs  = day_df["High"].astype(float).values
            d_lows   = day_df["Low"].astype(float).values
            d_closes = day_df["Close"].astype(float).values
            d_vols   = day_df["Volume"].astype(float).values

            def _close_vs(exit_px: float, reason: str):
                nonlocal equity, day_trade_count, position
                trade_delta  = position.get("option_delta", opt_delta)
                gross        = self._pnl_options(position, exit_px, trade_delta, lot_size)
                chg          = charges_for_trade(
                    entry_atr=position["atr"], gross_pnl=gross,
                    lot_size=lot_size, num_lots=position["quantity"],
                    opt_delta=trade_delta, dte=position.get("dte_at_entry", 7.0),
                )
                units        = lot_size * position["quantity"]
                prem_change  = gross / (trade_delta * units) if units > 0 else 0.0
                exit_premium = round(max(0.05, position["entry_premium"] + prem_change), 2)
                net          = gross - chg["total"]
                equity      += net
                day_trade_count += 1
                trades.append({
                    **position,
                    "exit_time":         str(ts),
                    "exit_price":        round(exit_px,      2),
                    "exit_premium":      exit_premium,
                    "exit_reason":       reason,
                    "pnl_gross":         round(gross,        2),
                    "charges":           round(chg["total"], 2),
                    "charges_breakdown": chg,
                    "pnl":               round(net,          2),
                    "equity":            round(equity,       2),
                })
                position = None

            for pos, (ts, row) in enumerate(day_df.iterrows()):
                bar_time = ts.time()
                g_idx    = g_start + pos

                # EOD square-off
                if position and bar_time >= EOD_EXIT:
                    _close_vs(float(row["Close"]), "EOD")
                    continue

                # SL / TP
                if position:
                    hi, lo = float(row["High"]), float(row["Low"])
                    if position["side"] == "BUY":
                        if lo <= position["sl"]:
                            _close_vs(position["sl"], "SL"); continue
                        if hi >= position["tp"]:
                            _close_vs(position["tp"], "TP"); continue
                    else:
                        if hi >= position["sl"]:
                            _close_vs(position["sl"], "SL"); continue
                        if lo <= position["tp"]:
                            _close_vs(position["tp"], "TP"); continue

                # Entry gate
                if position or day_trade_count >= MAX_TRADES_DAY:
                    continue
                if not in_entry_window(bar_time):
                    continue
                daily_loss = (day_start_eq - equity) / day_start_eq * 100 if day_start_eq > 0 else 0
                if daily_loss >= daily_loss_limit_pct:
                    continue
                if g_idx < 20 or pos < 6:
                    continue

                # Score signal
                sig = scalp_score(
                    day_opens=d_opens[:pos + 1],
                    day_highs=d_highs[:pos + 1],
                    day_lows=d_lows[:pos + 1],
                    day_closes=d_closes[:pos + 1],
                    day_volumes=d_vols[:pos + 1],
                    all_closes=all_closes[:g_idx + 1],
                )

                action = sig["action"]
                if action not in ("BUY", "SELL"):
                    continue
                if min_score is not None and sig["score"] < min_score:
                    continue

                entry  = float(row["Close"])
                atr_v  = sig["atr"]
                if atr_v <= 0:
                    continue

                sl_dist  = sl_mult * atr_v
                risk_amt = equity * (risk_pct / 100)

                if action == "BUY":
                    sl = round(entry - sl_dist, 2)
                    tp = round(entry + rr_ratio * sl_dist, 2)
                else:
                    sl = round(entry + sl_dist, 2)
                    tp = round(entry - rr_ratio * sl_dist, 2)

                option_type              = "CE" if action == "BUY" else "PE"
                expiry_date, dte         = self._expiry_for_trade(ts.date(), symbol)
                t_min, t_max             = self.PREMIUM_TARGET.get(symbol, (180, 200))
                strike, entry_prem, delta_trade = self._select_strike(
                    entry, atr_v, dte, strike_gap, action, interval, t_min, t_max
                )

                max_loss_lot      = sl_dist * delta_trade * lot_size
                num_lots          = max(1, int(risk_amt / max(1, max_loss_lot)))
                cost_per_lot      = max(1.0, entry_prem * lot_size)
                max_lots_capital  = max(1, int(config.MAX_TRADE_AMOUNT / cost_per_lot))
                num_lots          = min(num_lots, max_lots_capital)

                position = {
                    "symbol":        symbol,
                    "strategy":      "Raijin",
                    "side":          action,
                    "option_type":   option_type,
                    "strike":        strike,
                    "expiry":        str(expiry_date),
                    "dte_at_entry":  dte,
                    "entry_premium": round(entry_prem, 2),
                    "option_delta":  delta_trade,
                    "entry_time":    str(ts),
                    "entry_price":   round(entry, 2),
                    "quantity":      num_lots,
                    "lot_size":      lot_size,
                    "sl":            sl,
                    "tp":            tp,
                    "score":         sig["score"],
                    "atr":           round(atr_v, 2),
                    "vwap_at_entry": sig["vwap"],
                    "rsi_at_entry":  sig["rsi9"],
                    "vwap_upper2":   sig["upper2"],
                    "vwap_lower2":   sig["lower2"],
                }

            equity_curve.append({"date": str(day), "equity": round(equity, 2)})

        return {
            "symbol":          symbol,
            "strategy":        "Raijin",
            "period":          period,
            "interval":        interval,
            "risk_pct":        risk_pct,
            "rr_ratio":        rr_ratio,
            "initial_capital": self.initial_capital,
            "final_equity":    round(equity, 2),
            "trades":          trades,
            "equity_curve":    equity_curve,
        }

    def _pnl(self, position: dict, exit_price: float) -> float:
        """Direct instrument P&L (used by ATR Intraday strategy)."""
        qty = position["quantity"]
        if position["side"] == "BUY":
            return (exit_price - position["entry_price"]) * qty
        else:
            return (position["entry_price"] - exit_price) * qty

    def _pnl_options(self, position: dict, exit_spot: float,
                     delta: float, lot_size: int) -> float:
        """
        Options P&L using delta approximation.
        Signal is generated from spot price; actual trade is ATM CE/PE.
        P&L = spot_move × delta × lot_size × num_lots
        BUY signal → bought CE (profit when spot rises)
        SELL signal → bought PE (profit when spot falls)
        """
        spot_move = exit_spot - position["entry_price"]
        if position["side"] == "SELL":
            spot_move = -spot_move   # PE profits when spot falls
        return spot_move * delta * lot_size * position["quantity"]
