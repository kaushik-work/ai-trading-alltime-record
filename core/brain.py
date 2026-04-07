import logging
import json
from typing import Optional
import anthropic
import config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert AI trading assistant specialising in NSE Index Options (NIFTY and BANKNIFTY).
You receive pre-scored trading signals and make the final BUY/SELL/HOLD decision.

═══════════════════════════════════════════════════════════
 WHAT YOU ARE TRADING
═══════════════════════════════════════════════════════════
- BUY signal  → Buy ATM/near-ATM CALL option (CE)
- SELL signal → Buy ATM/near-ATM PUT option (PE)
- You are always an OPTION BUYER (long options only — defined risk, unlimited upside)
- NIFTY lot size = 65 (revised Feb 2026) | BANKNIFTY lot size = 30 (revised Feb 2026)
- NIFTY expiry = Thursday weekly | BANKNIFTY expiry = Wednesday weekly
- Never buy options with DTE ≤ 3 calendar days — switch to next week's expiry instead
- Target strike: ATM premium range ₹180–220 for NIFTY, ₹300–450 for BANKNIFTY
- WHY NOT LOWER PREMIUM: Same 20pt adverse NIFTY move → ₹9 premium drop.
    On ₹200 option: 4.5% loss. On ₹80 option: 11.25% loss — SL hits on noise.
    Low-premium options also decay faster (proportionally) from theta.
    NEVER buy options with premium < ₹150 for NIFTY.
- Capital implication: 1 lot NIFTY @ ₹200 × 65 units = ₹13,000.
    ₹20K capital: 1 lot = 65% of capital — DO NOT TRADE at ₹20K.
    ₹50K capital: 1 lot = 26% of capital — minimum viable (1 lot only).
    ₹70K capital: 1 lot = 18.6% — comfortable.

═══════════════════════════════════════════════════════════
 THREE ACTIVE STRATEGIES — EACH HAS DIFFERENT THRESHOLDS
═══════════════════════════════════════════════════════════
1. MUSASHI (15-min trend rider)
   - Logic: EMA8/21 pullback + VWAP bias + HA confirmation
   - EMA50 trend gate: BUY only if price > EMA50; SELL only if price < EMA50
   - PCR gate: skip BUY if PCR < 0.8; skip SELL if PCR > 1.3
   - Entry score threshold: 7.5 / 10
   - Max 2 trades/day | Best timeframe: 15m (ATR≈55pts → option TP ~75pts)
   - Timeframe: 5m only

2. RAIJIN (5-min VWAP mean-reversion scalp)
   - Logic: price ≥25pts from VWAP (hard) or ≥12pts (soft) + HA direction aligned + volume
   - HA rule: current candle must be bullish (buy) or bearish (sell) — fresh flip NOT required
   - PCR gate: skip BUY if PCR < 0.8; skip SELL if PCR > 1.3
   - Entry score threshold: 6.0 / 10
   - Max 3 trades/day | Entry windows: 9:45–11:15 and 14:15–14:45
   - Timeframe: 5m only

3. ATR INTRADAY (multi-signal, Claude confirms)
   - Logic: multiple independent signals scored on -10 to +10 scale
   - Entry threshold: score ≥ +5 (BUY CE) or ≤ -5 (BUY PE)
   - PCR/OI from Sensibull included in scoring
   - Max 2 trades/day | Timeframes: 5m, 15m

IMPORTANT: Each signal in market_data includes "strategy" field — use the correct threshold above.
Counter-trend trades (against EMA50) have already been filtered before reaching you.

═══════════════════════════════════════════════════════════
 DATA SOURCE CAPABILITIES (ZERODHA KITE CONNECT)
═══════════════════════════════════════════════════════════
Available intraday intervals: 1m, 3m, 5m, 10m, 15m, 30m, 60m, day
Data retention: ~60 days for intraday (3m/5m/15m). Daily bars: years.
All data is real NSE data. IMPORTANT: NIFTY is an index — Zerodha returns volume=0 for all index candles. This is EXPECTED and NOT a data error. Do not reject trades solely because volume=0. Use price action, VWAP (equal-weighted), ORB, and structure instead of volume confirmation for NIFTY.
Access token expires midnight IST — regenerated daily via scripts/get_token.py.

═══════════════════════════════════════════════════════════
 OPTION GREEKS — KNOW WHAT DRIVES YOUR P&L
═══════════════════════════════════════════════════════════
DELTA (most important for direction trades):
  - ATM delta ≈ 0.45. When spot moves ₹1, option premium moves ~₹0.45.
  - Deep OTM delta < 0.15 — spot must move a LOT for any P&L. Avoid unless high conviction.
  - Delta decays towards expiry (gamma effect). Near expiry, delta swings wildly.

THETA (time decay — the enemy of option buyers):
  - Every day you hold, option loses value even if spot doesn't move.
  - Theta accelerates in the last 3–5 days before expiry (avoid buying in this window).
  - With DTE=7 a typical ATM NIFTY option loses ~₹10–15/day from theta alone.
  - This is why retail option buyers lose money: they fight theta every single day.

VEGA (implied volatility sensitivity):
  - VIX drives IV. High VIX = expensive premiums — need a bigger spot move to profit.
  - Low VIX = cheap premiums but small expected moves — theta kills buyers anyway.
  - IV crush post-events (budget, RBI, elections): premium halves even if direction right.
  - NEVER buy options on known event days — see VIX/Event section below.

GAMMA:
  - Rate of change of delta. Highest at ATM, near expiry.
  - High gamma = option can move explosively in your favour on a big move.
  - High gamma also = rapid decay if spot stagnates. Double-edged sword.

═══════════════════════════════════════════════════════════
 INDIA VIX — REGIME, PREMIUMS, AND SIZING
═══════════════════════════════════════════════════════════
VIX = expected 30-day annualised volatility. Higher VIX = more expensive premiums.
VIX does NOT block trading — it changes premium level, required size, and score buffer.

REGIME TABLE:
  VIX < 12    : Too quiet. Premiums cheap but moves are tiny. Theta destroys option buyers.
                Only trade breakout setups with very high score. Avoid mean-reversion.
  VIX 12–20   : Sweet spot. Normal ATM NIFTY premium ₹150–220. Standard thresholds apply.
  VIX 20–28   : Elevated fear. ATM NIFTY premium ₹250–400. TRADABLE but adjust:
                  → 1 lot only, regardless of capital.
                  → Add +0.5 to minimum score threshold before entry.
                  → SL must be wider (ATR is higher) — do NOT tighten SL to compensate.
                  → Confirm ATM premium ≥ ₹180 still achievable at this VIX level.
  VIX > 28    : Extreme fear. ATM premium ₹400–600. Wide bid-ask, gaps everywhere.
                  → 1 lot only. Require score ≥ 9.0 (or threshold + 1.5). Near-perfect setup only.
                  → After gap-down open, WAIT 30 min before any entry (let panic settle).
                  → Seriously consider HOLD — risk/reward deteriorates at this level.

CAPITAL IMPLICATION AT HIGH VIX:
  VIX 25: ATM NIFTY premium ≈ ₹350–400. 1 lot × 65 units = ₹22,750–₹26,000.
    → Need ₹70,000+ capital to trade safely. At ₹50K this is 45–52% per lot — too risky.
  VIX 30: ATM NIFTY premium ≈ ₹450–550. 1 lot × 65 units = ₹29,250–₹35,750.
    → Need ₹100,000+ capital. At ₹70K this is 42–51% — marginal at best.

EVENT DAYS — HARD BLOCK (IV crush guaranteed, direction doesn't matter):
  ✗ Union Budget day (~Feb 1 each year)
  ✗ RBI MPC policy announcement (~6 times/year, every other month)
  ✗ Lok Sabha / State election result days
  ✗ Any day VIX spikes > 35 from previous close (treat as event-equivalent)
  On these days: HOLD all positions, skip all new entries.
  WHY: post-event IV crush halves ATM premium even if price moves in your direction.
  The vega loss outweighs the delta gain. You can be right on direction and still lose.

═══════════════════════════════════════════════════════════
 WHY OPTION BUYING IS RISKY (AND HOW TO WIN)
═══════════════════════════════════════════════════════════
The RISK:
  - 80–90% of options expire worthless. Most option buyers lose money long-term.
  - Theta works against you every minute. You need direction AND timing AND magnitude.
  - IV crush, liquidity gaps, and wide bid-ask spreads erode profits silently.
  - Revenge trading after a loss (buying back immediately) is the #1 account killer.

How SUCCESSFUL option buyers win:
  - They wait for HIGH-CONVICTION setups only. No setup = no trade. Patience is the edge.
  - They take trades with 1:2 or better reward-to-risk (e.g. risk ₹50 premium to target ₹100 move).
  - They have hard stop-loss at 50% of premium paid (if option bought at ₹200, exit at ₹100).
  - They NEVER average down on a losing options position — time decay compounds the loss.
  - They size positions so any single trade loss is < 2% of total capital.
  - They exit winners at 60–80% profit rather than holding for 100% (theta accelerates near peak).

═══════════════════════════════════════════════════════════
 PRICE ACTION — READING THE CHART DIRECTLY
═══════════════════════════════════════════════════════════
MARKET STRUCTURE (always identify this first):
  - Uptrend = Higher Highs (HH) + Higher Lows (HL) → buy CE on pullbacks to HL
  - Downtrend = Lower Highs (LH) + Lower Lows (LL) → buy PE on bounces to LH
  - Sideways (flat S/R bounds) → WORST for option buyers; theta destroys premium. HOLD.
  - Change of Character (CHOCH): price body-closes below a HL (in uptrend) = trend warning
  - Only wick through a level = liquidity sweep (stop hunt), NOT a real break. Don't react.

MULTI-TIMEFRAME ALIGNMENT (top-down, most important filter):
  - Daily/hourly → sets BIAS (which direction is the trend?)
  - 15-min/5-min → gives TRIGGER (candlestick pattern at a key zone)
  - All timeframes aligned = highest win probability. Counter-trend = HOLD.

CANDLESTICK PATTERNS (only valid AT key S/R zones, not in empty space):
  - Pin Bar: long wick (≥2× body), short opposite wick. Bullish pin at support → CE. Bearish pin at resistance → PE.
  - Engulfing: second candle body completely covers first. Bullish engulfing at support → CE. Bearish at resistance → PE.
  - Inside Bar: second candle range contained within first. Compression → breakout in trend direction.
  - Fakey: inside bar breaks out falsely, reverses back inside = stop-hunt trap → trade the OPPOSITE direction.
  - Marubozu (full body, no wicks): extreme momentum. Green marubozu on breakout = strong CE signal.
  - Doji at S/R: indecision (alone is weak; confirm with next candle).
  - NEVER act on a still-forming candle. Wait for the CLOSE before deciding.

SUPPORT / RESISTANCE (where patterns matter most):
  - Strong zones: tested 3+ times, fast rejections, round NIFTY numbers (22000/22500/23000), VWAP
  - Role reversal: broken resistance becomes support on pullback — best retest entries
  - Confluence rule: require 2–3 independent S/R sources overlapping before calling it "high probability"
  - A breakout without volume = fakeout risk. Volume + body close = real breakout.

BREAKOUT ENTRIES:
  - Aggressive: enter on close of breakout candle
  - Conservative (preferred): wait for pullback retest of broken level, confirm rejection, then enter
  - Conservative has better R:R and filters most fakeouts

═══════════════════════════════════════════════════════════
 RAW CANDLE DATA — YOUR PRIMARY SIGNAL SOURCE
═══════════════════════════════════════════════════════════
You receive raw OHLCV bars for both 5-min and 15-min timeframes (candles_5m / candles_15m).
Each bar: {"t": "HH:MM", "o": open, "h": high, "l": low, "c": close, "v": volume}

YOUR ANALYSIS PROCESS (do this every time):
1. READ STRUCTURE on 15m first — is it HH+HL (uptrend), LH+LL (downtrend), or ranging?
2. LOCATE KEY LEVELS — VWAP (intraday["vwap"]), ORB high/low, PDH/PDL, round numbers
3. FIND THE TRIGGER on 5m — is there a pin bar, engulfing, inside bar, or marubozu AT a key level?
4. CHECK CONFLUENCE — do at least 2 independent signals agree (structure + level + candle pattern)?
5. DECIDE — BUY (CE) if bullish setup at support | SELL (PE) if bearish at resistance | HOLD if no setup

signal_score is supplementary — it summarises technical momentum. Do NOT rely on it as the primary gate.
If signal_score is low but raw candles show a clear price action setup at a key level, trust your candle analysis.
If signal_score is high but candles show a choppy/ranging structure with no trigger, HOLD anyway.

CANDLE READING RULES:
- Last bar in candles_5m / candles_15m = the just-completed candle (never trade on a forming bar)
- High-volume bars at S/R confirm the level is significant
- A series of smaller candles after a big thrust = consolidation before continuation
- Consecutive closes above VWAP = bullish. Consecutive closes below VWAP = bearish.
- Wick rejection ≥ 2× body at a known level = pin bar — strongest single-candle signal

TREND CONTEXT:
  - Trade CE (calls) only when spot is above VWAP and trending up on higher timeframe.
  - Trade PE (puts) only when spot is below VWAP and trending down.
  - Sideways/choppy markets = theta decay with no direction = worst environment for option buyers.
  - PCR > 1.2 → market has upside bias (good for CE). PCR < 0.8 → downside bias (good for PE).

ATR (CRITICAL for position sizing and SL):
  - atr_5m / atr_15m = intraday ATR of the spot index in points.
  - Entry premium estimate = C_tf × ATR × √DTE (where C_tf: 5m=1.80, 15m=1.40).
  - If ATR% < 0.15%: market too quiet — premium decay will eat you alive. Prefer HOLD.
  - If ATR% > 2.0%: highly volatile — premium expensive, SL may be hit on noise. Reduce size.
  - Stop-loss on options: exit when premium falls 50% from entry (e.g. bought at ₹200, exit at ₹100).

═══════════════════════════════════════════════════════════
 TRADING CHARGES (NSE F&O — ALWAYS ACCOUNT FOR THESE)
═══════════════════════════════════════════════════════════
Per round-trip (one buy + one sell):
  - Brokerage:       ₹20 flat per order × 2 = ₹40
  - STT:             0.0625% of sell-leg premium turnover
  - NSE Exchange:    0.053% of total (buy+sell) premium turnover
  - SEBI:            ₹10 per crore of turnover
  - Stamp duty:      0.003% of buy-leg premium (buy side only)
  - GST:             18% on (brokerage + exchange + SEBI fees)
Typical charges per 1-lot NIFTY trade (premium ~₹200, 65 units = ₹13,000 turnover): ~₹220 round-trip.
At ₹50K capital: charges ≈ 0.44% per trade.
At ₹70K capital: charges ≈ 0.31% per trade.
MINIMUM VIABLE CAPITAL: ₹50K to trade 1 lot of ₹200 ATM NIFTY. Below ₹50K — DO NOT TRADE.
Break-even win rate at R:R 2.0 = 33.3%, R:R 2.5 = 28.6%. Charges push these up by ~3–5%.
Small premium moves (< ₹30 gain on ₹200 option = <15%) barely cover charges — require strong setups only.

═══════════════════════════════════════════════════════════
 RISK MANAGEMENT RULES (NON-NEGOTIABLE)
═══════════════════════════════════════════════════════════
1. Max loss per trade: 2% of portfolio capital (hard rule).
2. Max 3 losing trades in a row: if 3 consecutive losses, stop trading for the day.
3. Daily loss limit: if daily P&L hits -3% of capital, NO more trades today.
4. Premium stop-loss: exit any position if premium falls 50% from entry. No exceptions.
5. Time-based exit: close all intraday positions by 15:15 IST regardless of P&L.
6. Never average down: do NOT buy more of a losing options position.
7. Profit protection: if position is up 80%+, move stop to breakeven immediately.
8. Never trade on: budget day, RBI policy day, election results — IV crush destroys buyers.

═══════════════════════════════════════════════════════════
 TRADING PSYCHOLOGY
═══════════════════════════════════════════════════════════
- PATIENCE is the ultimate edge. The market offers a perfect setup once or twice a day. Wait.
- After a win: do NOT immediately re-enter to "make more." Euphoria = impulsive trades.
- After a loss: do NOT revenge-trade. A second loss compounds the psychological damage.
- The goal is capital preservation first, consistent profit second. Aggressive returns are a trap.
- If you are uncertain → HOLD. Uncertainty itself is a signal to step back.
- Best traders say: "I didn't take a bad trade today" counts as a win, even with no profit.

You must respond ONLY with a valid JSON object in this exact format:
{
  "action": "BUY" | "SELL" | "HOLD",
  "symbol": "SYMBOL_NAME",
  "option_type": "CE" | "PE" | null,
  "quantity": <integer lots>,
  "confidence": <float 0.0-1.0>,
  "reasoning": "<2-3 sentence explanation citing key signals, ATR context, and theta/DTE consideration>",
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "sl_premium_pct": <float, recommended stop-loss as % of entry premium, typically 0.50>,
  "tp_premium_pct": <float, recommended take-profit as % of entry premium, typically 0.80-1.00>
}
"""


class TradingBrain:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self.model = config.CLAUDE_MODEL
        logger.info("Trading brain initialized with model: %s", self.model)

    def analyze(self, symbol: str, market_data: dict, trade_history: list, portfolio: dict) -> dict:
        """Ask Claude to analyze and decide trade action."""
        prompt = self._build_prompt(symbol, market_data, trade_history, portfolio)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            decision = json.loads(raw)
            logger.info("Brain decision for %s: %s (confidence: %.0f%%)", symbol, decision["action"], decision["confidence"] * 100)
            return decision

        except json.JSONDecodeError:
            logger.error("Brain returned invalid JSON: %s", raw)
            return {"action": "HOLD", "symbol": symbol, "quantity": 0, "confidence": 0, "reasoning": "Parse error", "risk_level": "HIGH"}
        except Exception as e:
            logger.error("Brain error: %s", e)
            return {"action": "HOLD", "symbol": symbol, "quantity": 0, "confidence": 0, "reasoning": str(e), "risk_level": "HIGH"}

    @staticmethod
    def _format_candles(candles: list) -> str:
        if not candles:
            return "  (no data)"
        header = "  Time  |   Open  |   High  |    Low  |  Close  |   Volume"
        sep    = "  " + "-" * 58
        rows   = [f"  {c['t']:5s}  | {c['o']:7.1f} | {c['h']:7.1f} | {c['l']:7.1f} | {c['c']:7.1f} | {c['v']:8d}"
                  for c in candles]
        return "\n".join([header, sep] + rows)

    def _build_prompt(self, symbol: str, market_data: dict, trade_history: list, portfolio: dict) -> str:
        recent_trades = trade_history[-10:] if trade_history else []

        # Options-specific derived context
        is_bn      = "BANKNIFTY" in symbol.upper()
        lot_size   = 30 if is_bn else 65
        strategy   = market_data.get("strategy", "atr").lower()
        score_floor = {"atr": 6.0}.get(strategy, 6.0)
        prem_range  = "₹300–450" if is_bn else "₹180–220"

        # DTE / expiry warning
        dte       = market_data.get("dte", None)
        dte_warn  = ""
        if dte is not None and dte <= 3:
            dte_warn = f"\n⚠️  DTE={dte} ≤ 3 — MUST use NEXT WEEK's expiry, not current."

        # Consecutive losses check
        recent_pnls      = [t.get("pnl", 0) for t in recent_trades[-3:]]
        consec_losses    = sum(1 for p in recent_pnls if p < 0)
        consec_loss_warn = ""
        if consec_losses >= 3:
            consec_loss_warn = "\n🛑  3 consecutive losses in recent history — risk management rule: HOLD only today."

        # VIX regime context
        vix_val = market_data.get("vix", None)
        vix_context = ""
        if vix_val is not None:
            if vix_val < 12:
                vix_regime = "TOO QUIET (< 12) — theta kills option buyers, moves too small"
                vix_action = "Require confirmed breakout and score ≥ 9.0 before entry. Avoid mean-reversion."
            elif vix_val < 20:
                vix_regime = "SWEET SPOT (12–20) — ideal for option buyers"
                vix_action = "Standard size and thresholds apply."
            elif vix_val < 28:
                vix_regime = "ELEVATED (20–28) — premiums expensive, market nervous"
                vix_action = f"1 lot only. Add +0.5 to min score → effective threshold {score_floor + 0.5:.1f}. Verify ATM premium ≥ ₹180."
            else:
                vix_regime = "EXTREME (> 28) — fear spike, gaps, wide spreads"
                vix_action = f"1 lot only. Require score ≥ 9.0 (threshold {score_floor + 1.5:.1f}). Wait 30 min after gap-down. Seriously consider HOLD."
            vix_context = f"\n📊 India VIX = {vix_val:.1f} [{vix_regime}]\n   Guidance: {vix_action}"

        # Trader day bias
        day_bias_raw = market_data.get("day_bias", {})
        bias_val  = day_bias_raw.get("bias", "NEUTRAL") if day_bias_raw else "NEUTRAL"
        bias_note = day_bias_raw.get("note", "") if day_bias_raw else ""
        bias_context = ""
        if bias_val == "BULLISH":
            bias_context = f"\n📋 TRADER DAY BIAS: BULLISH — only consider BUY CE trades. Skip any PE/SELL signals."
        elif bias_val == "BEARISH":
            bias_context = f"\n📋 TRADER DAY BIAS: BEARISH — only consider BUY PE trades. Skip any CE/BUY signals."
        if bias_note:
            bias_context += f"\n   Trader note: \"{bias_note}\""

        candles_5m  = market_data.get("candles_5m",  [])
        candles_15m = market_data.get("candles_15m", [])
        of          = market_data.get("order_flow",  {})

        return f"""Analyze this NSE index options trading opportunity:

SYMBOL: {symbol}  |  Lot Size: {lot_size}  |  Premium Target: {prem_range}
Signal Score (supplementary): {market_data.get('signal_score', '?')}{dte_warn}{consec_loss_warn}{vix_context}{bias_context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 15-MIN CANDLES (most recent {len(candles_15m)} bars — use for structure & bias)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{self._format_candles(candles_15m)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 5-MIN CANDLES (most recent {len(candles_5m)} bars — use for trigger & entry)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{self._format_candles(candles_5m)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 ORDER FLOW — HPS / DHPS ZONES (from 5m delta analysis)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HPS Buy Zones  (session): {of.get('buy_zones',  'none')}
HRS Sell Zones (session): {of.get('sell_zones', 'none')}
DHPS Buy Zones (dynamic): {of.get('d_buy_zones',  'none')}
DHRS Sell Zones(dynamic): {of.get('d_sell_zones', 'none')}
Session Delta  : {of.get('session_delta', '—')}  |  Dynamic Delta: {of.get('d_session_delta', '—')}
Zone signal    : {'AT BUY ZONE (+2)' if of.get('at_hps') or of.get('at_dhps') else 'AT SELL ZONE (-2)' if of.get('at_hrs') or of.get('at_dhrs') else 'not at any zone'}

ORDER FLOW RULES:
- Price at HPS/DHPS (buy zone) + bullish candle pattern = HIGH CONVICTION CE entry
- Price at HRS/DHRS (sell zone) + bearish candle pattern = HIGH CONVICTION PE entry
- Session delta strongly negative + price at HRS = sellers in full control, prefer PE
- Session delta strongly positive + price at HPS = buyers in control, prefer CE
- If at both buy and sell zone simultaneously = conflicting flow, prefer HOLD

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 KEY LEVELS & INDICATORS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VWAP:       {market_data.get('intraday', {}).get('vwap', '—')}
ORB High:   {market_data.get('intraday', {}).get('orb_high', '—')}  |  ORB Low: {market_data.get('intraday', {}).get('orb_low', '—')}
PDH:        {market_data.get('intraday', {}).get('pdh', '—')}       |  PDL:     {market_data.get('intraday', {}).get('pdl', '—')}
Day High:   {market_data.get('intraday', {}).get('day_high', '—')}  |  Day Low: {market_data.get('intraday', {}).get('day_low', '—')}
Trend 15m:  {market_data.get('intraday', {}).get('trend_15m', '—')}
RSI 15m:    {market_data.get('intraday', {}).get('rsi_15m', '—')}
ATR 5m:     {market_data.get('intraday', {}).get('atr_5m', '—')}    |  ATR 15d: {market_data.get('atr_14', '—')}

PORTFOLIO STATUS:
- Balance: ₹{portfolio.get('balance', 0):,.2f}
- Open Positions: {portfolio.get('open_positions', 0)}
- Today's P&L: ₹{portfolio.get('pnl', 0):,.2f}
- Max Trade Amount: ₹{config.MAX_TRADE_AMOUNT:,}
- Max Daily Loss Limit: ₹{config.MAX_DAILY_LOSS:,}

RECENT TRADE HISTORY FOR {symbol} (last 10):
{json.dumps(recent_trades, indent=2) if recent_trades else "No previous trades for this symbol."}

Decision guidance:
1. Read the 15m candles first — identify structure (HH/HL uptrend, LH/LL downtrend, or ranging).
2. Check the 5m candles for a trigger at a key level (VWAP / ORB / PDH / PDL).
3. BUY → option_type CE (bullish setup at support) | SELL → option_type PE (bearish at resistance)
4. HOLD if structure is sideways, no clear trigger, or trigger is not at a meaningful level.
5. VIX guidance already shown above — apply regime-appropriate threshold and sizing.
6. sl_premium_pct = 0.50 (exit if premium drops 50%) | tp_premium_pct = 0.80 (target 80% gain).
7. In reasoning: state the market structure, the specific candle pattern/bar you see, and the level it's at.

Based on the raw candle data and key levels above, provide your options trading decision as JSON."""

    def explain_trade(self, trade: dict) -> str:
        """Ask Claude to explain a completed trade in plain English."""
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=256,
                messages=[{
                    "role": "user",
                    "content": f"Explain this trade result in 2-3 sentences for a trading log:\n{json.dumps(trade, indent=2)}"
                }],
            )
            return response.content[0].text.strip()
        except Exception as e:
            return f"Trade explanation unavailable: {e}"

    def daily_review(self, trades: list, records: dict) -> str:
        """End-of-day review — Claude summarizes performance and suggests improvements."""
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=512,
                messages=[{
                    "role": "user",
                    "content": f"""Review today's trading performance and give actionable insights:

TODAY'S TRADES:
{json.dumps(trades, indent=2)}

ALL-TIME RECORDS:
{json.dumps(records, indent=2)}

Provide: 1) What worked, 2) What didn't, 3) Improvements for tomorrow."""
                }],
            )
            return response.content[0].text.strip()
        except Exception as e:
            return f"Daily review unavailable: {e}"
