"""
Polymarket Brain — Claude estimates probability of prediction market outcomes.

Claude reads:
  - The market question
  - Resolution criteria / source
  - Current date context
  - Recent price trajectory (are smart-money bettors moving it?)

And returns:
  - estimated_prob  : float 0–1 (Claude's best estimate of P(YES))
  - confidence      : float 0–1 (how certain Claude is)
  - reasoning       : str
  - edge            : float (claude_prob - market_price)
  - action          : "BUY_YES" | "BUY_NO" | "HOLD"
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Edge thresholds for trading signals
MIN_EDGE        = 0.08   # need ≥8% probability edge to act
MIN_CONFIDENCE  = 0.65   # Claude must be ≥65% confident
MIN_LIQUIDITY   = 1000   # skip markets with <$1k liquidity (slippage)


def _get_client():
    from anthropic import Anthropic
    return Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


def estimate_probability(
    question: str,
    description: str = "",
    resolution_source: str = "",
    end_date: str = "",
    current_price: float = 0.5,
    price_history_summary: str = "",
    current_date: str = "",
) -> dict:
    """
    Ask Claude to estimate P(YES) for a prediction market.

    Returns:
        {
          "estimated_prob": float,
          "confidence": float,
          "edge": float,            # claude_prob - market_price
          "action": str,            # BUY_YES | BUY_NO | HOLD
          "reasoning": str,
          "risk_flags": list[str],  # reasons to be cautious
        }
    """
    client = _get_client()

    price_context = ""
    if price_history_summary:
        price_context = f"\n\nPrice movement: {price_history_summary}"

    prompt = f"""You are analyzing a Polymarket prediction market to estimate the true probability of the outcome.

MARKET QUESTION:
{question}

RESOLUTION CRITERIA:
{description or "Not specified"}

RESOLUTION SOURCE: {resolution_source or "Not specified"}
MARKET CLOSES: {end_date or "Not specified"}
CURRENT DATE: {current_date or "Unknown"}
CURRENT MARKET PRICE (YES): {current_price:.2f} (implies market says {current_price*100:.0f}% probability){price_context}

Your job:
1. Estimate the TRUE probability that this market resolves YES
2. Compare against the current market price
3. Identify if there is a meaningful edge (your estimate vs market price)

IMPORTANT GUIDELINES:
- Be realistic. Don't chase events that have already resolved.
- Consider: is the end date far enough for the event to still happen?
- Flag if this relies on information you may not have (very recent events)
- Consider liquidity risk — thin markets can be manipulated
- Be honest about your uncertainty

Respond ONLY with valid JSON:
{{
  "estimated_prob": <float 0-1>,
  "confidence": <float 0-1, your certainty in the estimate>,
  "reasoning": "<2-3 sentence explanation>",
  "risk_flags": ["<risk1>", "<risk2>"],
  "key_factors": ["<factor driving your estimate>"]
}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()

        # Extract JSON
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()

        parsed = json.loads(text)
        est_prob   = float(parsed.get("estimated_prob", 0.5))
        confidence = float(parsed.get("confidence", 0.5))
        edge       = round(est_prob - current_price, 4)

        # Trading signal
        if confidence >= MIN_CONFIDENCE and abs(edge) >= MIN_EDGE:
            action = "BUY_YES" if edge > 0 else "BUY_NO"
        else:
            action = "HOLD"

        return {
            "estimated_prob":  round(est_prob, 4),
            "market_price":    round(current_price, 4),
            "confidence":      round(confidence, 4),
            "edge":            edge,
            "action":          action,
            "reasoning":       parsed.get("reasoning", ""),
            "risk_flags":      parsed.get("risk_flags", []),
            "key_factors":     parsed.get("key_factors", []),
        }

    except json.JSONDecodeError as e:
        logger.warning("JSON parse failed: %s | response: %s", e, text[:200])
        return _hold_response(current_price, "JSON parse error")
    except Exception as e:
        logger.error("Brain.estimate_probability failed: %s", e)
        return _hold_response(current_price, str(e))


def _hold_response(current_price: float, reason: str) -> dict:
    return {
        "estimated_prob": current_price,
        "market_price":   current_price,
        "confidence":     0.0,
        "edge":           0.0,
        "action":         "HOLD",
        "reasoning":      f"Analysis failed: {reason}",
        "risk_flags":     ["analysis_error"],
        "key_factors":    [],
    }


def batch_scan(markets: list, current_date: str = "") -> list:
    """
    Scan a list of market dicts, return only those with actionable edge.

    Each market dict should have: question, description, outcomePrices, liquidity
    """
    from polymarket.client import parse_market_price, parse_liquidity
    import time

    opportunities = []
    for m in markets:
        price = parse_market_price(m)
        if price is None:
            continue

        liquidity = parse_liquidity(m)
        if liquidity < MIN_LIQUIDITY:
            continue

        # Skip very high or very low confidence markets (already resolved in minds)
        if price < 0.03 or price > 0.97:
            continue

        analysis = estimate_probability(
            question=m.get("question", ""),
            description=m.get("description", ""),
            resolution_source=m.get("resolutionSource", ""),
            end_date=m.get("endDate", ""),
            current_price=price,
            current_date=current_date,
        )
        analysis["market_id"]   = m.get("id", "")
        analysis["question"]    = m.get("question", "")[:100]
        analysis["liquidity"]   = liquidity
        analysis["volume"]      = float(m.get("volume", 0) or 0)
        analysis["end_date"]    = m.get("endDate", "")

        if analysis["action"] != "HOLD":
            opportunities.append(analysis)
            logger.info("EDGE FOUND: %s | edge=%.2f conf=%.2f → %s",
                        analysis["question"][:60], analysis["edge"],
                        analysis["confidence"], analysis["action"])

        time.sleep(0.5)  # rate limit Claude API

    opportunities.sort(key=lambda x: abs(x["edge"]) * x["confidence"], reverse=True)
    return opportunities
