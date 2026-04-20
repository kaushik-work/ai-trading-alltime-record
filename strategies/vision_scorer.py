"""
vision_scorer.py — sends auto-generated NIFTY chart to Claude Vision API.

Claude reads the chart like an ICT/SMC trader:
  BOS, CHOCH, FVG, IDM, liquidity sweeps, order flow direction.

Returns a structured signal dict compatible with Signal Radar.
"""

import base64
import json
import logging
import re

import config

logger = logging.getLogger(__name__)

_PROMPT = """You are an expert SMC (Smart Money Concepts) / ICT (Inner Circle Trader) analyst.
You are looking at a live NIFTY 50 (NSE India) intraday candlestick chart, auto-generated from real market data.

Chart legend:
- Dashed YELLOW lines = Previous Day High (PDH) and Previous Day Low (PDL)
- Dotted BLUE line = Today's session open price
- Volume bars at the bottom panel

You know these specific entry models — look for them on the chart:

MODEL 1 — SUPPLY & DEMAND ENTRY:
  Bearish: Price makes a Swing High → breaks down (BOS) → retraces back up into the supply zone
  (Bearish Order Block, above 50% of the swing) → SHORT from supply.
  Bullish: Mirror — price makes Swing Low → BOS up → retraces into demand zone → LONG.

MODEL 2 — BREAKER BLOCK ENTRY:
  Bearish: Price makes higher highs → BOS down → rallies back up to retest the previous
  support level that got broken (now a Breaker = resistance) → SHORT at breaker retest.
  Bullish: Mirror — lower lows → BOS up → retest of broken resistance (now demand breaker) → LONG.

MODEL 3 — AMD (Accumulation · Manipulation · Distribution):
  Bearish: Price consolidates in a range (Accumulation) → spikes UP through range highs
  (Manipulation / stop hunt of buy-side liquidity) → reverses hard DOWN (Distribution to target).
  Bullish: Mirror — consolidation → spike DOWN (stop hunt of sell-side) → reverses UP.

Also check for:
- BOS (Break of Structure) and CHOCH (Change of Character) in market structure
- FVG (Fair Value Gaps / imbalances) that price may be drawn to fill
- IDM (Inducement) — fake moves before the real directional move
- Equal highs / equal lows as liquidity targets
- HTF liquidity grabs above PDH or below PDL

Based on ALL of the above, determine the current setup on this chart.

Respond ONLY with valid JSON — no markdown fences, no explanation outside the JSON object:
{
  "direction": "BUY" or "SELL" or "HOLD",
  "score": <integer -10 to +10: +10=very strong BUY, -10=very strong SELL, 0=no setup>,
  "confidence": "low" or "medium" or "high",
  "structure": "bullish" or "bearish" or "ranging",
  "order_flow": "bullish" or "bearish" or "neutral",
  "model_detected": "supply_demand" or "breaker_block" or "amd" or "none" or "multiple",
  "observations": ["...", "...", "..."],
  "invalidation": "..."
}"""


def score_vision(interval: str = "5m", symbol: str = "NIFTY") -> dict:
    """
    Auto-generate chart → send to Claude Vision → return signal dict.

    Returns dict with keys: score, direction, action, confidence, structure,
    order_flow, observations, invalidation, error (if failed).
    """
    from data.chart_generator import generate_chart

    png_bytes = generate_chart(symbol=symbol, interval=interval)
    if not png_bytes:
        return _error_result("Chart generation failed — no data")

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        b64 = base64.standard_b64encode(png_bytes).decode("utf-8")

        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": b64},
                    },
                    {"type": "text", "text": _PROMPT},
                ],
            }],
        )

        raw = response.content[0].text.strip()
        parsed = _parse_response(raw)

        logger.info(
            "Vision-ICT [%s]: direction=%s score=%d confidence=%s structure=%s",
            interval, parsed.get("direction"), parsed.get("score", 0),
            parsed.get("confidence"), parsed.get("structure"),
        )
        return parsed

    except Exception as e:
        logger.error("vision_scorer.score_vision [%s]: %s", interval, e)
        return _error_result(str(e))


def _parse_response(raw: str) -> dict:
    """Extract JSON from Claude's response, handle markdown fences."""
    try:
        # Strip markdown code fences if present
        text = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        data = json.loads(text)
    except Exception:
        logger.warning("vision_scorer: could not parse JSON, raw=%r", raw[:200])
        return _error_result(f"JSON parse failed: {raw[:100]}")

    score = int(data.get("score", 0))
    score = max(-10, min(10, score))  # clamp

    direction = data.get("direction", "HOLD").upper()
    if direction not in ("BUY", "SELL", "HOLD"):
        direction = "HOLD"

    # Align score sign with direction
    if direction == "BUY"  and score < 0: score = abs(score)
    if direction == "SELL" and score > 0: score = -abs(score)
    if direction == "HOLD": score = 0

    return {
        "score":          score,
        "direction":      direction,
        "action":         direction,
        "confidence":     data.get("confidence", "low"),
        "structure":      data.get("structure", "ranging"),
        "order_flow":     data.get("order_flow", "neutral"),
        "model_detected": data.get("model_detected", "none"),
        "observations":   data.get("observations", []),
        "invalidation":   data.get("invalidation", ""),
        "threshold":      6,
        "will_trade":     abs(score) >= 6,
    }


def _error_result(msg: str) -> dict:
    return {
        "score": 0, "direction": "HOLD", "action": "HOLD",
        "confidence": "low", "structure": "ranging", "order_flow": "neutral",
        "observations": [], "invalidation": "",
        "threshold": 6, "will_trade": False,
        "error": msg,
    }
