"""
Signal scoring engine — combines technical indicators, candlestick patterns,
and intraday levels into a single score from -10 to +10.

BUY threshold  : score >= +MIN_SCORE (default 5)
SELL threshold : score <= -MIN_SCORE
HOLD           : everything in between

Strategy: AishDoc intraday approach for NIFTY & BANKNIFTY
  → Trend on 15-min SMA
  → VWAP as key intraday level
  → ORB breakout as entry trigger
  → RSI pullback for timing
  → Candlestick confirmation
  → PDH/PDL as support/resistance
"""
import logging
import config

logger = logging.getLogger(__name__)


def _score_fib_of(indicators: dict, intraday: dict = None, df_5m=None) -> tuple[int, list, dict, dict]:
    """15m Fibonacci pullback + 15m confirmation + 5m order-flow filter.

    Uses multi-day history (last 5 days from _get_df) so fib swings are detectable
    from market open on day 1 — mirrors how the backtest engine feeds data.
    """
    if df_5m is None or len(df_5m) < 20:
        return 0, ["Fib-OF: insufficient bars (need 20+)"], {"fib_of": 0}, {}

    import pandas as pd

    df = df_5m.copy()
    if "Date" in df.columns:
        df = df.set_index("Date")
    df.index = pd.to_datetime(df.index)
    needed = ["Open", "High", "Low", "Close", "Volume"]
    if not all(c in df.columns for c in needed):
        return 0, ["Fib-OF: missing OHLCV columns"], {"fib_of": 0}, {}

    df_15 = df[needed].resample("15min", label="right", closed="right").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
    }).dropna()
    if len(df_15) < 20:
        return 0, ["Fib-OF: insufficient 15m bars"], {"fib_of": 0}, {}

    close = df_15["Close"].astype(float)
    ema9 = float(close.ewm(span=9, adjust=False).mean().iloc[-1])
    sma20 = float(close.rolling(20).mean().iloc[-1])
    trend = "up" if ema9 > sma20 else "down" if ema9 < sma20 else "none"
    if trend == "none":
        return 0, ["Fib-OF: flat 15m trend"], {"fib_trend": 0}, {}

    recent = df_15.tail(16)
    price = intraday.get("price", indicators.get("price", float(close.iloc[-1]))) if intraday else indicators.get("price", float(close.iloc[-1]))
    score = 0
    signals = []
    breakdown = {}

    setup = None
    if trend == "up":
        low_ts = recent["Low"].idxmin()
        after_low = recent.loc[low_ts:]
        high_ts = after_low["High"].idxmax()
        swing_low = float(recent.loc[low_ts, "Low"])
        swing_high = float(after_low.loc[high_ts, "High"])
        diff = swing_high - swing_low
        if diff > 0 and low_ts < high_ts:
            zone_low = swing_high - diff * 0.618
            zone_high = swing_high - diff * 0.382
            if zone_low <= price <= zone_high:
                setup = ("BUY", swing_low, swing_high, zone_low, zone_high)
    else:
        high_ts = recent["High"].idxmax()
        after_high = recent.loc[high_ts:]
        low_ts = after_high["Low"].idxmin()
        swing_high = float(recent.loc[high_ts, "High"])
        swing_low = float(after_high.loc[low_ts, "Low"])
        diff = swing_high - swing_low
        if diff > 0 and high_ts < low_ts:
            zone_low = swing_low + diff * 0.382
            zone_high = swing_low + diff * 0.618
            if zone_low <= price <= zone_high:
                setup = ("SELL", swing_low, swing_high, zone_low, zone_high)

    if not setup:
        return 0, [f"Fib-OF: no 15m fib-zone pullback ({trend})"], {"fib_zone": 0}, {}

    side, swing_low, swing_high, zone_low, zone_high = setup
    directional = 1 if side == "BUY" else -1
    score += directional * 6
    breakdown["fib_trend"] = directional * 2
    breakdown["fib_impulse"] = directional * 2
    breakdown["fib_zone"] = directional * 2
    signals.append(f"Fib-OF {side}: 15m trend + fib zone {zone_low:.0f}-{zone_high:.0f}")

    if len(df_15) >= 2:
        prev = df_15.iloc[-2]
        curr = df_15.iloc[-1]
        rng = max(float(curr["High"] - curr["Low"]), 1e-9)
        close_pos = (float(curr["Close"]) - float(curr["Low"])) / rng
        confirmed = (
            (side == "BUY" and (float(curr["Close"]) > float(prev["High"]) or (close_pos >= 0.7 and curr["Close"] > curr["Open"])))
            or (side == "SELL" and (float(curr["Close"]) < float(prev["Low"]) or (close_pos <= 0.3 and curr["Close"] < curr["Open"])))
        )
        if confirmed:
            score += directional * 2
            breakdown["fib_confirm"] = directional * 2
            signals.append("Fib-OF: 15m confirmation candle")

    order_flow = {}
    try:
        from strategies.order_flow import analyse as of_analyse
        symbol = indicators.get("symbol", "NIFTY")
        order_flow = of_analyse(df.tail(60), float(price), symbol, proximity_ticks=3)
        if side == "BUY":
            if order_flow.get("at_hps") or order_flow.get("at_dhps"):
                score += 1
                breakdown["fib_of_zone"] = 1
            if order_flow.get("session_delta", 0) > 0 or order_flow.get("d_session_delta", 0) > 0:
                score += 1
                breakdown["fib_delta"] = 1
            if order_flow.get("at_hrs") or order_flow.get("at_dhrs"):
                score -= 2
                breakdown["fib_conflict"] = -2
        else:
            if order_flow.get("at_hrs") or order_flow.get("at_dhrs"):
                score -= 1
                breakdown["fib_of_zone"] = -1
            if order_flow.get("session_delta", 0) < 0 or order_flow.get("d_session_delta", 0) < 0:
                score -= 1
                breakdown["fib_delta"] = -1
            if order_flow.get("at_hps") or order_flow.get("at_dhps"):
                score += 2
                breakdown["fib_conflict"] = 2
        signals.append("Fib-OF: 5m order-flow filter applied")
    except Exception as e:
        logger.debug("Fib-OF order flow failed: %s", e)

    return score, signals, breakdown, order_flow


def score_symbol(indicators: dict, oi_data: dict, patterns: dict,
                 intraday: dict = None, df_5m=None, mode: str = "full",
                 skip_sections: set = None) -> dict:
    """
    Score a symbol using all available signals.

    Args:
        indicators : dict from RealMarketData.get_indicators()
        oi_data    : dict from oi_data.get_pcr() or get_market_sentiment()
        patterns   : dict from patterns.detect_patterns()

    Returns:
        score      : int, -10 to +10
        action     : 'BUY' | 'SELL' | 'HOLD'
        confidence : float 0.0–1.0
        signals    : list of signal descriptions (for Claude context)
        breakdown  : dict of individual signal contributions
    """
    score = 0
    signals = []
    breakdown = {}
    _skip = (skip_sections or set()).__contains__   # fast membership test

    price    = indicators.get("price", 0)
    rsi      = indicators.get("rsi", 50)
    macd     = indicators.get("macd", 0)
    macd_sig = indicators.get("macd_signal", 0)
    macd_hist = indicators.get("macd_histogram", 0)
    vol_ratio = indicators.get("volume_ratio", 1.0)
    bb_upper = indicators.get("bollinger_upper", price * 1.02)
    bb_lower = indicators.get("bollinger_lower", price * 0.98)

    # ── Intraday MAs for atr_only mode ────────────────────────────────────────
    # Daily SMA50/SMA20/EMA9 reflect a multi-month trend (NIFTY above daily SMA50
    # for months = permanent +4 bullish headstart that blocks all PE signals).
    # In atr_only mode, compute these from 5m bars so they reflect today's trend.
    if mode == "atr_only" and df_5m is not None and len(df_5m) >= 50:
        try:
            import pandas as pd
            _c = df_5m["Close"].astype(float) if "Close" in df_5m.columns else df_5m.iloc[:, 3].astype(float)
            sma20 = float(_c.rolling(20).mean().iloc[-1])
            sma50 = float(_c.rolling(50).mean().iloc[-1])
            ema9  = float(_c.ewm(span=9, adjust=False).mean().iloc[-1])
        except Exception:
            sma20 = indicators.get("sma_20", price)
            sma50 = indicators.get("sma_50", price)
            ema9  = indicators.get("ema_9", price)
    else:
        sma20 = indicators.get("sma_20", price)
        sma50 = indicators.get("sma_50", price)
        ema9  = indicators.get("ema_9", price)

    # ── 1-3. Trend MAs: SMA50, SMA20, EMA9 ───────────────────────────────────
    if not _skip("sma"):
        if price > sma50:
            pts = 2
            signals.append(f"Price above SMA50 (bullish trend) +{pts}")
            breakdown["sma50_trend"] = pts
        else:
            pts = -2
            signals.append(f"Price below SMA50 (bearish trend) {pts}")
            breakdown["sma50_trend"] = pts
        score += breakdown["sma50_trend"]

        if price > sma20:
            pts = 1
            signals.append(f"Price above SMA20 +{pts}")
            breakdown["sma20_trend"] = pts
        else:
            pts = -1
            signals.append(f"Price below SMA20 {pts}")
            breakdown["sma20_trend"] = pts
        score += breakdown["sma20_trend"]

        if price > ema9:
            pts = 1
            signals.append(f"Price above EMA9 (momentum up) +{pts}")
            breakdown["ema9_momentum"] = pts
        else:
            pts = -1
            signals.append(f"Price below EMA9 (momentum down) {pts}")
            breakdown["ema9_momentum"] = pts
        score += breakdown["ema9_momentum"]

    # ── 4. RSI ────────────────────────────────────────────────────────────────
    if not _skip("rsi"):
        if 35 <= rsi <= 55:
            pts = 2
            signals.append(f"RSI {rsi:.0f} in healthy pullback zone (35-55) +{pts}")
            breakdown["rsi"] = pts
        elif rsi < 30:
            pts = 1
            signals.append(f"RSI {rsi:.0f} oversold — potential reversal +{pts}")
            breakdown["rsi"] = pts
        elif rsi > 75:
            pts = -3
            signals.append(f"RSI {rsi:.0f} severely overbought {pts}")
            breakdown["rsi"] = pts
        elif rsi > 65:
            pts = -2
            signals.append(f"RSI {rsi:.0f} overbought {pts}")
            breakdown["rsi"] = pts
        else:
            breakdown["rsi"] = 0
        score += breakdown.get("rsi", 0)

    # ── 5. MACD ───────────────────────────────────────────────────────────────
    if not _skip("macd"):
        if macd > macd_sig and macd_hist > 0:
            pts = 2 if macd_hist > abs(macd) * 0.1 else 1
            signals.append(f"MACD bullish {'crossover' if pts == 2 else 'above signal'} +{pts}")
            breakdown["macd"] = pts
        elif macd < macd_sig and macd_hist < 0:
            pts = -2 if abs(macd_hist) > abs(macd) * 0.1 else -1
            signals.append(f"MACD bearish {'crossover' if pts == -2 else 'below signal'} {pts}")
            breakdown["macd"] = pts
        else:
            breakdown["macd"] = 0
        score += breakdown.get("macd", 0)

    # ── 6. Volume ─────────────────────────────────────────────────────────────
    if not _skip("volume"):
        if vol_ratio >= 2.0:
            pts = 2
            signals.append(f"Volume {vol_ratio:.1f}x average — strong confirmation +{pts}")
            breakdown["volume"] = pts
        elif vol_ratio >= 1.4:
            pts = 1
            signals.append(f"Volume {vol_ratio:.1f}x average — moderate confirmation +{pts}")
            breakdown["volume"] = pts
        elif vol_ratio < 0.6:
            pts = -1
            signals.append(f"Volume {vol_ratio:.1f}x average — weak/low interest {pts}")
            breakdown["volume"] = pts
        else:
            breakdown["volume"] = 0
        score += breakdown.get("volume", 0)

    # ── 7. Bollinger Bands ────────────────────────────────────────────────────
    if not _skip("bb"):
        if price <= bb_lower:
            pts = 1
            signals.append(f"Price at/below lower Bollinger Band (oversold) +{pts}")
            breakdown["bollinger"] = pts
        elif price >= bb_upper:
            pts = -1
            signals.append(f"Price at/above upper Bollinger Band (overbought) {pts}")
            breakdown["bollinger"] = pts
        else:
            breakdown["bollinger"] = 0
        score += breakdown.get("bollinger", 0)

    # ── 8. Candlestick patterns ───────────────────────────────────────────────
    if not _skip("patterns"):
        pat_strength = patterns.get("strength", 0)
        pat_names = patterns.get("patterns", [])
        if pat_names:
            pts = pat_strength
            bias = patterns.get("bias", "neutral")
            signals.append(f"Patterns: {', '.join(pat_names)} ({bias}) {pts:+d}")
            breakdown["patterns"] = pts
            score += pts

    # ── 9. PCR / OI sentiment + OI delta ─────────────────────────────────────
    pcr = oi_data.get("pcr", 1.0)   # always read pcr — needed for herd gate below
    if not _skip("pcr"):
        sentiment = oi_data.get("sentiment", "neutral")
        if "very_bullish" in sentiment:
            pts = 2
            signals.append(f"PCR {pcr:.2f} — very bullish +{pts}")
            breakdown["pcr"] = pts
        elif "bullish" in sentiment:
            pts = 1
            signals.append(f"PCR {pcr:.2f} — bullish +{pts}")
            breakdown["pcr"] = pts
        elif "very_bearish" in sentiment:
            pts = -2
            signals.append(f"PCR {pcr:.2f} — very bearish {pts}")
            breakdown["pcr"] = pts
        elif "bearish" in sentiment:
            pts = -1
            signals.append(f"PCR {pcr:.2f} — bearish {pts}")
            breakdown["pcr"] = pts
        else:
            breakdown["pcr"] = 0
        score += breakdown.get("pcr", 0)

        oc_bias = oi_data.get("bias", "NEUTRAL")
        if oc_bias == "CE_FAVORED":
            pts = 2
            signals.append(f"OC bias CE FAVORED (PCR {pcr:.2f}) +{pts}")
            breakdown["oc_bias"] = pts
            score += pts
        elif oc_bias == "PE_FAVORED":
            pts = -2
            signals.append(f"OC bias PE FAVORED (PCR {pcr:.2f}) {pts}")
            breakdown["oc_bias"] = pts
            score += pts

        ce_wall = oi_data.get("ce_wall", 0)
        pe_wall = oi_data.get("pe_wall", 0)
        if ce_wall > 0 and pe_wall > 0:
            spot = oi_data.get("spot", price)
            if spot > 0:
                if 0 < (ce_wall - spot) / spot * 100 < 0.3:
                    pts = -1
                    signals.append(f"Near CE wall {ce_wall} — resistance {pts}")
                    breakdown["ce_wall"] = pts
                    score += pts
                elif 0 < (spot - pe_wall) / spot * 100 < 0.3:
                    pts = 1
                    signals.append(f"Near PE wall {pe_wall} — support +{pts}")
                    breakdown["pe_wall"] = pts
                    score += pts

        atm_ce_delta = oi_data.get("atm_ce_oi_delta", 0)
        atm_pe_delta = oi_data.get("atm_pe_oi_delta", 0)
        if atm_ce_delta != 0 or atm_pe_delta != 0:
            if score > 0 and atm_ce_delta < -500:
                pts = -2
                signals.append(f"OI SHIFT: CE OI -{abs(atm_ce_delta):,} (buyers leaving) {pts}")
                breakdown["oi_delta"] = pts
                score += pts
            elif score < 0 and atm_pe_delta < -500:
                pts = 2
                signals.append(f"OI SHIFT: PE OI -{abs(atm_pe_delta):,} (put buyers leaving) +{abs(pts)}")
                breakdown["oi_delta"] = pts
                score += pts
            elif score > 0 and atm_pe_delta > 500:
                pts = -1
                signals.append(f"OI SHIFT: PE OI +{atm_pe_delta:,} (hedging against rally) {pts}")
                breakdown["oi_delta_hedge"] = pts
                score += pts

    # ── 9c. Herd Behavior Detector ────────────────────────────────────────────
    if not _skip("herd"):
        if pcr < 0.68 and score > 0:
            pts = -3
            signals.append(f"HERD DANGER: PCR {pcr:.2f} extreme bullish crowd {pts}")
            breakdown["herd_danger"] = pts
            score += pts
        elif pcr > 1.40 and score < 0:
            pts = 3
            signals.append(f"HERD DANGER: PCR {pcr:.2f} extreme bearish crowd — contrarian +{abs(pts)}")
            breakdown["herd_danger"] = pts
            score += pts

    # ── 10. ATR volatility filter ─────────────────────────────────────────────
    if not _skip("atr_filter"):
        atr_pct = indicators.get("atr_pct", 0)
        if atr_pct > 0:
            if atr_pct < 0.3:
                pts = -2
                signals.append(f"ATR {atr_pct:.2f}% — market too quiet/choppy {pts}")
                breakdown["atr_filter"] = pts
                score += pts
            elif atr_pct > 2.5:
                pts = -1
                signals.append(f"ATR {atr_pct:.2f}% — extremely volatile {pts}")
                breakdown["atr_filter"] = pts
                score += pts
            else:
                pts = 1
                signals.append(f"ATR {atr_pct:.2f}% — healthy volatility +{pts}")
                breakdown["atr_filter"] = pts
                score += pts

    # ── 11. Intraday signals ──────────────────────────────────────────────────
    if intraday and not intraday.get("error"):
        price_now = intraday.get("price", price)

        if not _skip("vwap"):
            vwap = intraday.get("vwap")
            if vwap:
                pts = 2 if price_now > vwap else -2
                signals.append(f"{'Above' if pts > 0 else 'Below'} VWAP ₹{vwap:.0f} {'+' if pts>0 else ''}{pts}")
                breakdown["vwap"] = pts
                score += pts

        if not _skip("orb"):
            orb_high = intraday.get("orb_high")
            orb_low  = intraday.get("orb_low")
            if orb_high and orb_low:
                if intraday.get("orb_broken_up"):
                    pts = 3
                    signals.append(f"ORB broken UP ₹{orb_high:.0f} — trend confirmed +{pts}")
                    breakdown["orb"] = pts
                elif intraday.get("orb_broken_down"):
                    pts = -3
                    signals.append(f"ORB broken DOWN ₹{orb_low:.0f} — bearish confirmed {pts}")
                    breakdown["orb"] = pts
                else:
                    breakdown["orb"] = 0
                    signals.append(f"Inside ORB range (₹{orb_low:.0f}-₹{orb_high:.0f}) — wait for break")
                score += breakdown.get("orb", 0)

        if not _skip("trend_15m"):
            trend_15m = intraday.get("trend_15m")
            if trend_15m == "uptrend":
                pts = 1
                signals.append(f"15m trend UPTREND +{pts}")
                breakdown["trend_15m"] = pts
            elif trend_15m == "downtrend":
                pts = -1
                signals.append(f"15m trend DOWNTREND {pts}")
                breakdown["trend_15m"] = pts
            else:
                breakdown["trend_15m"] = 0
            score += breakdown.get("trend_15m", 0)

        if not _skip("rsi_15m"):
            rsi_15m = intraday.get("rsi_15m")
            if rsi_15m is not None:
                if 35 <= rsi_15m <= 55:
                    pts = 1
                    signals.append(f"15m RSI {rsi_15m:.0f} pullback zone +{pts}")
                    breakdown["rsi_15m"] = pts
                elif rsi_15m > 72:
                    pts = -2
                    signals.append(f"15m RSI {rsi_15m:.0f} overbought {pts}")
                    breakdown["rsi_15m"] = pts
                elif rsi_15m < 28:
                    pts = 2
                    signals.append(f"15m RSI {rsi_15m:.0f} oversold +{pts}")
                    breakdown["rsi_15m"] = pts
                else:
                    breakdown["rsi_15m"] = 0
                score += breakdown.get("rsi_15m", 0)

        if not _skip("pdh_pdl"):
            pdh = intraday.get("pdh")
            pdl = intraday.get("pdl")
            if pdh and pdl and price_now:
                if price_now > pdh:
                    pts = 2
                    signals.append(f"Above PDH ₹{pdh:.0f} — strong bullish +{pts}")
                    breakdown["pdh_pdl"] = pts
                elif price_now < pdl:
                    pts = -2
                    signals.append(f"Below PDL ₹{pdl:.0f} — strong bearish {pts}")
                    breakdown["pdh_pdl"] = pts
                elif abs(price_now - pdh) / pdh < 0.003:
                    pts = -1
                    signals.append(f"Near PDH ₹{pdh:.0f} resistance {pts}")
                    breakdown["pdh_pdl"] = pts
                elif abs(price_now - pdl) / pdl < 0.003:
                    pts = 1
                    signals.append(f"Near PDL ₹{pdl:.0f} support +{pts}")
                    breakdown["pdh_pdl"] = pts
                else:
                    breakdown["pdh_pdl"] = 0
                score += breakdown.get("pdh_pdl", 0)

    # ── 12. ICT Order Blocks + Liquidity Sweeps ───────────────────────────────
    if mode in {"ict_only", "fib_of_only"}:
        score = 0
        signals.clear()
        breakdown.clear()

    order_flow = {}
    if mode != "atr_only" and df_5m is not None and len(df_5m) >= 6:
        try:
            from strategies.order_flow import analyse as of_analyse
            symbol = indicators.get("symbol", "NIFTY")
            current_price = intraday.get("price", price) if intraday else price
            order_flow = of_analyse(df_5m, current_price, symbol)
            liq_score = order_flow.get("ict_liq_score", 0)
            ob_score  = order_flow.get("ict_ob_score", 0)
            liq_sig   = order_flow.get("ict_liq_signal")
            ob_level  = order_flow.get("ict_ob_level")
            if liq_score != 0:
                tag = liq_sig or ("SSL" if liq_score > 0 else "BSL")
                signals.append(f"ICT C: {tag} sweep {'bullish' if liq_score > 0 else 'bearish'} {liq_score:+d}")
                breakdown["ict_liq"] = liq_score
                score += liq_score
            if ob_score != 0:
                lvl  = f"₹{ob_level[0]:.0f}-{ob_level[1]:.0f}" if ob_level else "zone"
                kind = "bullish OB retest" if ob_score > 0 else "bearish OB retest"
                signals.append(f"ICT C: {kind} {lvl} {ob_score:+d}")
                breakdown["ict_ob"] = ob_score
                score += ob_score
        except Exception as e:
            logger.debug("Order flow analysis failed: %s", e)

    # ── 12b. S/R Level + Market Structure gate ────────────────────────────────
    if not _skip("sr_levels") and df_5m is not None and len(df_5m) >= 20:
        try:
            from core.sr_levels import get_cached as _sr_cached
            sr = _sr_cached(df_5m)
            _pos    = sr.get("position", "open_air")
            _struct = sr.get("structure", "ranging")
            _n_res  = sr.get("nearest_resistance")
            _n_sup  = sr.get("nearest_support")

            if score > 0:
                if _pos == "at_resistance":
                    pts = -3
                    signals.append(f"SR: AT RESISTANCE ₹{_n_res:.0f} — buying into wall {pts}")
                    breakdown["sr_resistance"] = pts
                    score += pts
                elif _pos == "breaking_down":
                    pts = -3
                    signals.append(f"SR: BREAKING DOWN — CE contra-trend {pts}")
                    breakdown["sr_breakdown"] = pts
                    score += pts
                elif _struct == "downtrend":
                    pts = -2
                    signals.append(f"SR: DOWNTREND — CE counter-trend {pts}")
                    breakdown["sr_downtrend"] = pts
                    score += pts
                elif _pos == "at_support":
                    pts = 2
                    signals.append(f"SR: AT SUPPORT ₹{_n_sup:.0f} — bounce zone +{pts}")
                    breakdown["sr_support"] = pts
                    score += pts
                elif _pos == "breaking_up":
                    pts = 2
                    signals.append(f"SR: BREAKING UP — momentum confirmed +{pts}")
                    breakdown["sr_breakup"] = pts
                    score += pts
            elif score < 0:
                if _pos == "at_support":
                    pts = 3
                    signals.append(f"SR: AT SUPPORT ₹{_n_sup:.0f} — PE contra-zone +{pts}")
                    breakdown["sr_at_support_sell"] = pts
                    score += pts
                elif _pos == "breaking_up":
                    pts = 3
                    signals.append(f"SR: BREAKING UP — PE contra-trend +{pts}")
                    breakdown["sr_breakup_sell"] = pts
                    score += pts
                elif _struct == "uptrend":
                    pts = 2
                    signals.append(f"SR: UPTREND — PE counter-trend +{pts}")
                    breakdown["sr_uptrend_sell"] = pts
                    score += pts
                elif _pos == "at_resistance":
                    pts = -2
                    signals.append(f"SR: AT RESISTANCE — PE rejection zone {pts}")
                    breakdown["sr_resistance_sell"] = pts
                    score += pts
                elif _pos == "breaking_down":
                    pts = -2
                    signals.append(f"SR: BREAKING DOWN — PE momentum {pts}")
                    breakdown["sr_breakdown_sell"] = pts
                    score += pts
        except Exception as _sr_e:
            logger.debug("SR scoring failed: %s", _sr_e)

    # ── Trap / wick rejection filter ──────────────────────────────────────────
    # If the last 5m bar closed in the wrong portion of its range, it's a
    # stop-hunt / fake breakout. Penalise before threshold check.
    if df_5m is not None and len(df_5m) >= 1:
        try:
            last = df_5m.iloc[-1]
            hi = float(last.get("High", last.get("high", 0)))
            lo = float(last.get("Low",  last.get("low",  0)))
            cl = float(last.get("Close", last.get("close", 0)))
            rng = hi - lo
            if rng > 0:
                close_pos = (cl - lo) / rng   # 0 = closed at low, 1 = closed at high
                if score > 0 and close_pos < 0.35:
                    # BUY signal but bar closed near the LOW → bearish trap candle
                    score -= 2
                    signals.append(f"TRAP: wick rejection (close_pos={close_pos:.2f}) -2")
                elif score < 0 and close_pos > 0.65:
                    # SELL signal but bar closed near the HIGH → bullish trap candle
                    score += 2
                    signals.append(f"TRAP: wick rejection (close_pos={close_pos:.2f}) -2")
        except Exception:
            pass

    # ── Clamp and resolve ─────────────────────────────────────────────────────
    if mode == "fib_of_only":
        score, signals, breakdown, order_flow = _score_fib_of(indicators, intraday, df_5m)

    score = max(-10, min(10, score))
    # ICT-only mode: Delta+TL+ICT combined, max ≈ ±6, threshold = 2
    if mode == "ict_only":
        threshold = 2
    elif mode == "fib_of_only":
        threshold = getattr(config, "FIB_OF_SIGNAL_SCORE", 6)
    else:
        threshold = getattr(config, "MIN_SIGNAL_SCORE", 6)

    if score >= threshold:
        action = "BUY"
    elif score <= -threshold:
        action = "SELL"
    else:
        action = "HOLD"

    confidence = round(abs(score) / 10, 2)

    logger.debug(
        "Score for %s: %d (%s) | signals: %s",
        indicators.get("symbol", "?"), score, action, "; ".join(signals)
    )

    return {
        "score":      score,
        "action":     action,
        "confidence": confidence,
        "signals":    signals,
        "breakdown":  breakdown,
        "threshold":  threshold,
        "order_flow": order_flow,
    }
