"""Vision lens — the bot actually looks at the chart.

Renders the chart as it stood at the decision bar (core/chart/render.py) and asks
a vision model what it sees. This is the one lens that reads a picture rather
than a number.

IT IS MEASURABLE, WHICH IS THE WHOLE ARGUMENT FOR ALLOWING IT

The standing objection to a vision lens was that it can never be backtested —
no historical screenshots exist, so there is nothing to replay, and an
unmeasurable strategy is one this repo has learned not to deploy. That
objection is wrong, and it is worth being precise about why: screenshots are
not the only way to obtain a chart. A chart is a rendering of OHLC, and 115,200
bars per crypto symbol are on disk. Rendering bar i from its preceding window
reproduces exactly what a trader would have seen, and the outcome is already
known. So this lens runs through the same harness, the same splits and the same
mix-matched baseline as every numeric one.

WHAT IS DIFFERENT: REPLAY COSTS MONEY

Every other lens replays for free. This one bills per call — a rendered chart is
of the order of 4,000 image tokens, so a few thousand sweeps is real money. Two
consequences shape the design:

  * It is only ever called at CANDIDATES the deterministic layer has already
    flagged, never as a scanner across every bar.
  * A measurement run samples rather than sweeping the full history.

CLAUDE.md SAYS NO LLM IN SIGNAL GENERATION

That rule is deliberately bent here, and only here. The lens starts pinned at
weight 0 in SHADOW, cannot initiate a trade, and can only earn a vote through
the same measured attribution as everything else. The deterministic core still
decides every trade until live evidence says otherwise.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from nse.lenses.base import BaseLens, Direction, LensVerdict, abstain
from nse.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5"

# Bars in frame. Enough to show structure, few enough to stay legible.
WINDOW_BARS = 120

# The verdict shape. Structured outputs guarantee this parses — no JSON-forcing
# prefill (which is rejected outright on this model) and no regex salvage.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["LONG", "SHORT", "NEUTRAL"]},
        "confidence": {"type": "number"},
        "pattern": {"type": "string"},
        "rationale": {"type": "string"},
        "invalidation": {"type": "string"},
    },
    "required": ["direction", "confidence", "pattern", "rationale", "invalidation"],
    "additionalProperties": False,
}

PROMPT = """You are reading a price chart to judge direction over the next
roughly one hour of trading. You can see only what is in this image — the bars
shown are every bar that had printed at the moment of the decision, and there
is deliberately nothing after them.

Report what the CHART shows, not what you know about the instrument. Judge:

- Market structure: higher highs and higher lows, or lower highs and lower lows,
  or neither.
- Where price sits against the levels it has respected before.
- Whether the most recent bars show a rejection wick, an absorption, or a clean
  continuation.
- Volume behaviour where it disagrees with price.

Then commit to a direction with a calibrated confidence:

  0.0-0.2  nothing readable here
  0.2-0.5  a lean, easily wrong
  0.5-0.8  a clear setup with a defined invalidation
  0.8-1.0  textbook, rare

Most charts are NOT textbook. If the picture is ambiguous, say NEUTRAL with low
confidence — that is a useful answer and it is scored as one. Guessing to seem
decisive is the failure mode here, because a confident wrong read is weighted
more heavily against you than an honest abstention.

`invalidation` is the price level at which your read is simply wrong."""


class VisionLens(BaseLens):
    name = "vision"

    # Replayable, but every replay bills. Distinct from the numeric lenses in
    # cost, not in kind — the harness treats it identically.
    backtestable = True

    def __init__(self, client=None, model: str = MODEL,
                 window: int = WINDOW_BARS, max_calls: Optional[int] = None):
        self._client = client
        self.model = model
        self.window = window
        # Hard ceiling on billed calls for one process. A runaway replay loop
        # over 115k bars would be an expensive way to discover a bug.
        self.max_calls = max_calls
        self.calls = 0
        self.errors = 0

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def _evaluate(self, snap: MarketSnapshot) -> LensVerdict:
        if self.max_calls is not None and self.calls >= self.max_calls:
            return abstain(self.name,
                           f"call budget exhausted ({self.max_calls})")

        bars = snap.bars
        if bars is None or len(bars) < 40:
            return abstain(self.name,
                           f"{0 if bars is None else len(bars)} bars — too few to draw")

        from core.chart.render import render

        chart = render(bars, window=self.window,
                       title=f"{snap.symbol} @ {snap.ts:%Y-%m-%d %H:%M} UTC")
        if chart is None:
            return abstain(self.name, "chart did not render")

        try:
            data = self._ask(chart.b64)
        except Exception as e:
            self.errors += 1
            logger.exception("vision lens: API call failed: %s", e)
            return abstain(self.name, f"vision call failed: {type(e).__name__}",
                           error=f"{type(e).__name__}: {e}")
        if data is None:
            return abstain(self.name, "model declined to read this chart")

        direction = {"LONG": Direction.LONG, "SHORT": Direction.SHORT,
                     "NEUTRAL": Direction.NEUTRAL}.get(
                         str(data.get("direction", "")).upper(), Direction.NEUTRAL)

        return LensVerdict(
            lens=self.name,
            direction=direction,
            confidence=float(data.get("confidence") or 0.0),
            rationale=str(data.get("rationale", ""))[:400],
            features={
                "pattern": str(data.get("pattern", ""))[:120],
                "invalidation": str(data.get("invalidation", ""))[:80],
                "n_bars_shown": chart.n_bars,
                "png_bytes": len(chart.png),
                "model": self.model,
                "dte": round(snap.dte, 3),
                "spot": round(snap.spot, 2),
            },
        )

    def _ask(self, image_b64: str) -> Optional[dict]:
        """One vision call. None when the model declines the request."""
        client = self._get_client()
        self.calls += 1

        resp = client.messages.create(
            model=self.model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema",
                                      "schema": VERDICT_SCHEMA}},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png",
                                "data": image_b64}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
        )

        # Check the stop reason BEFORE touching content: a declined request
        # returns HTTP 200 with an empty or partial content list, so indexing
        # content[0] blindly raises on exactly the case worth handling.
        if getattr(resp, "stop_reason", None) == "refusal":
            logger.warning("vision lens: request declined (%s)",
                           getattr(getattr(resp, "stop_details", None), "category", "?"))
            return None

        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            return None
        return json.loads(text)

    def stats(self) -> dict:
        return {"calls": self.calls, "errors": self.errors,
                "budget": self.max_calls, "model": self.model}
