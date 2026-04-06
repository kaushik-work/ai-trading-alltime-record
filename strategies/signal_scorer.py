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


def score_symbol(indicators: dict, oi_data: dict, patterns: dict,
                 intraday: dict = None, df_5m=None, mode: str = "full") -> dict:
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

    price    = indicators.get("price", 0)
    rsi      = indicators.get("rsi", 50)
    macd     = indicators.get("macd", 0)
    macd_sig = indicators.get("macd_signal", 0)
    macd_hist = indicators.get("macd_histogram", 0)
    sma20    = indicators.get("sma_20", price)
    sma50    = indicators.get("sma_50", price)
    ema9     = indicators.get("ema_9", price)
    vol_ratio = indicators.get("volume_ratio", 1.0)
    bb_upper = indicators.get("bollinger_upper", price * 1.02)
    bb_lower = indicators.get("bollinger_lower", price * 0.98)
    chg_pct  = indicators.get("change_pct", 0)

    # ── 1. Primary Trend — SMA50 (AishDoc: always trade with the trend) ───────
    if price > sma50:
        pts = 2
        signals.append(f"Price above SMA50 (bullish trend) +{pts}")
        breakdown["sma50_trend"] = pts
    else:
        pts = -2
        signals.append(f"Price below SMA50 (bearish trend) {pts}")
        breakdown["sma50_trend"] = pts
    score += breakdown["sma50_trend"]

    # ── 2. Secondary Trend — SMA20 ────────────────────────────────────────────
    if price > sma20:
        pts = 1
        signals.append(f"Price above SMA20 +{pts}")
        breakdown["sma20_trend"] = pts
    else:
        pts = -1
        signals.append(f"Price below SMA20 {pts}")
        breakdown["sma20_trend"] = pts
    score += breakdown["sma20_trend"]

    # ── 3. Short-term momentum — EMA9 ─────────────────────────────────────────
    if price > ema9:
        pts = 1
        signals.append(f"Price above EMA9 (momentum up) +{pts}")
        breakdown["ema9_momentum"] = pts
    else:
        pts = -1
        signals.append(f"Price below EMA9 (momentum down) {pts}")
        breakdown["ema9_momentum"] = pts
    score += breakdown["ema9_momentum"]

    # ── 4. RSI — AishDoc: buy the pullback, not the breakout ─────────────────
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

    # ── 5. MACD — momentum confirmation ───────────────────────────────────────
    if macd > macd_sig and macd_hist > 0:
        if macd_hist > abs(macd) * 0.1:  # histogram widening = strong signal
            pts = 2
            signals.append(f"MACD bullish crossover (histogram widening) +{pts}")
        else:
            pts = 1
            signals.append(f"MACD above signal +{pts}")
        breakdown["macd"] = pts
    elif macd < macd_sig and macd_hist < 0:
        if abs(macd_hist) > abs(macd) * 0.1:
            pts = -2
            signals.append(f"MACD bearish crossover (histogram widening) {pts}")
        else:
            pts = -1
            signals.append(f"MACD below signal {pts}")
        breakdown["macd"] = pts
    else:
        breakdown["macd"] = 0
    score += breakdown.get("macd", 0)

    # ── 6. Volume — AishDoc: volume must confirm the move ─────────────────────
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

    # ── 7. Bollinger Bands — mean reversion signals ───────────────────────────
    bb_position = (price - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5
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

    # ── 8. Candlestick patterns (AishDoc price action) ────────────────────────
    pat_strength = patterns.get("strength", 0)
    pat_names = patterns.get("patterns", [])
    if pat_names:
        pts = pat_strength
        bias = patterns.get("bias", "neutral")
        signals.append(f"Patterns: {', '.join(pat_names)} ({bias}) {pts:+d}")
        breakdown["patterns"] = pts
        score += pts

    # ── 9. PCR / OI sentiment (BeSensibull) ───────────────────────────────────
    sentiment = oi_data.get("sentiment", "neutral")
    pcr = oi_data.get("pcr", 1.0)
    if "very_bullish" in sentiment:
        pts = 2
        signals.append(f"PCR {pcr:.2f} — very bullish (market heavily hedged) +{pts}")
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

    # ── 10. ATR volatility filter (AishDoc: don't trade in dead/choppy market) ─
    atr_pct = indicators.get("atr_pct", 0)
    if atr_pct > 0:
        # For NIFTY/BANKNIFTY intraday: ATR < 0.3% = choppy/dead market, avoid
        # ATR > 2% = extremely volatile, size down / avoid
        if atr_pct < 0.3:
            pts = -2
            signals.append(f"ATR {atr_pct:.2f}% — market too quiet/choppy, low edge {pts}")
            breakdown["atr_filter"] = pts
            score += pts
        elif atr_pct > 2.5:
            pts = -1
            signals.append(f"ATR {atr_pct:.2f}% — extremely volatile, size down {pts}")
            breakdown["atr_filter"] = pts
            score += pts
        else:
            pts = 1
            signals.append(f"ATR {atr_pct:.2f}% — healthy volatility, good trading conditions +{pts}")
            breakdown["atr_filter"] = pts
            score += pts

    # ── 11. Intraday signals (AishDoc) — only when intraday data available ────
    if intraday and not intraday.get("error"):
        price_now = intraday.get("price", price)

        # VWAP — most important intraday level
        vwap = intraday.get("vwap")
        if vwap:
            if price_now > vwap:
                pts = 2
                signals.append(f"Price above VWAP ₹{vwap:.0f} (bullish) +{pts}")
                breakdown["vwap"] = pts
            else:
                pts = -2
                signals.append(f"Price below VWAP ₹{vwap:.0f} (bearish) {pts}")
                breakdown["vwap"] = pts
            score += breakdown["vwap"]

        # ORB breakout — AishDoc: ORB break with volume = strong signal
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
                signals.append(f"Inside ORB range (₹{orb_low:.0f}–₹{orb_high:.0f}) — wait for break")
            score += breakdown.get("orb", 0)

        # 15-min trend — intermediate direction
        trend_15m = intraday.get("trend_15m")
        if trend_15m == "uptrend":
            pts = 1
            signals.append(f"15-min trend: UPTREND (SMA9 > SMA20) +{pts}")
            breakdown["trend_15m"] = pts
        elif trend_15m == "downtrend":
            pts = -1
            signals.append(f"15-min trend: DOWNTREND (SMA9 < SMA20) {pts}")
            breakdown["trend_15m"] = pts
        else:
            breakdown["trend_15m"] = 0
        score += breakdown.get("trend_15m", 0)

        # 15-min RSI
        rsi_15m = intraday.get("rsi_15m")
        if rsi_15m is not None:
            if 35 <= rsi_15m <= 55:
                pts = 1
                signals.append(f"15-min RSI {rsi_15m:.0f} — pullback zone +{pts}")
                breakdown["rsi_15m"] = pts
            elif rsi_15m > 72:
                pts = -2
                signals.append(f"15-min RSI {rsi_15m:.0f} — overbought {pts}")
                breakdown["rsi_15m"] = pts
            elif rsi_15m < 28:
                pts = 2
                signals.append(f"15-min RSI {rsi_15m:.0f} — oversold +{pts}")
                breakdown["rsi_15m"] = pts
            else:
                breakdown["rsi_15m"] = 0
            score += breakdown.get("rsi_15m", 0)

        # Previous Day High/Low — key S/R (AishDoc: respect these levels)
        pdh = intraday.get("pdh")
        pdl = intraday.get("pdl")
        if pdh and pdl and price_now:
            if price_now > pdh:
                pts = 2
                signals.append(f"Price broke above PDH ₹{pdh:.0f} — strong bullish +{pts}")
                breakdown["pdh_pdl"] = pts
            elif price_now < pdl:
                pts = -2
                signals.append(f"Price broke below PDL ₹{pdl:.0f} — strong bearish {pts}")
                breakdown["pdh_pdl"] = pts
            elif abs(price_now - pdh) / pdh < 0.003:  # within 0.3% of PDH = resistance
                pts = -1
                signals.append(f"Price near PDH ₹{pdh:.0f} (resistance) {pts}")
                breakdown["pdh_pdl"] = pts
            elif abs(price_now - pdl) / pdl < 0.003:  # within 0.3% of PDL = support
                pts = 1
                signals.append(f"Price near PDL ₹{pdl:.0f} (support) +{pts}")
                breakdown["pdh_pdl"] = pts
            else:
                breakdown["pdh_pdl"] = 0
            score += breakdown.get("pdh_pdl", 0)

    # ── 12. Strategy C — ICT Order Blocks + Liquidity Sweeps ────────────────────
    # In ict_only mode: discard all technical scores — only ICT signals matter.
    if mode == "ict_only":
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

            # Strategy C — ICT Order Blocks + Liquidity Sweeps
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

    # ── Clamp and resolve ─────────────────────────────────────────────────────
    score = max(-10, min(10, score))
    # ICT-only mode has fewer signals — lower threshold needed (max score ≈ ±4)
    threshold = 2 if mode == "ict_only" else getattr(config, "MIN_SIGNAL_SCORE", 6)

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
