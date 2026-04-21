"""
Parse trader notes from the Day Bias panel.

Three outcomes:
  FORCE_TRADE  — explicit direction + option type (+ optional strike, SL, TP, price)
                 Examples:
                   "BUY CE 22500 SL 50 TP 150"
                   "BUY 24500 CE at market"
                   "BUY CE 23500 at 150 SL 145"
                   "SELL PE"               ← uses ATM strike, bot defaults SL/TP
  BIAS         — natural language directional intent, no specific strike
                 Example: "take trades today towards downwards"
  UNCLEAR      — parser couldn't find direction or option type
  NONE         — no actionable content (plain note / empty)
"""
import re

_DIR_PAT    = re.compile(r'\b(buy|sell)\b', re.IGNORECASE)
_OPT_PAT    = re.compile(r'\b(ce|call|pe|put)\b', re.IGNORECASE)
_STRIKE_PAT = re.compile(r'\b(\d{4,6})\b')
_SL_PAT     = re.compile(r'\b(?:sl|stoploss|stop[\s-]?loss|stop)\s*[:\s@]\s*(\d+(?:\.\d+)?)', re.IGNORECASE)
_TP_PAT     = re.compile(r'\b(?:tp|target|tgt|take[\s-]?profit)\s*[:\s@]\s*(\d+(?:\.\d+)?)', re.IGNORECASE)
_PRICE_PAT  = re.compile(r'\bat\s+(\d+(?:\.\d+)?)\b', re.IGNORECASE)  # "at 150"
_MARKET_PAT = re.compile(r'\b(market|mkt|cmp)\b', re.IGNORECASE)       # "at market"

_BEAR_WORDS = {
    "down", "bear", "bearish", "sell", "short", "negative",
    "fall", "crash", "drop", "downside", "downward", "downwards",
    "lower", "decline", "weak", "weakness", "puts", "pe",
}
_BULL_WORDS = {
    "up", "bull", "bullish", "buy", "long", "positive",
    "rise", "rally", "upside", "upward", "upwards", "higher",
    "breakout", "strong", "strength", "calls", "ce",
}


def _norm_option(s: str) -> str:
    return "CE" if s.lower() in ("call", "ce") else "PE"


def parse_trade_note(note: str) -> dict:
    """
    Returns dict:
      type        : "force_trade" | "bias" | "unclear" | "none"
      explanation : human-readable string shown in the UI flash
      bias        : present when type=="bias" or inferred for force_trade direction
      --- force_trade fields ---
      direction   : "BUY" | "SELL"
      option_type : "CE" | "PE"
      strike      : int | None  (None = use ATM)
      price       : float | None  (None = market order)
      sl          : float | None  (None = bot uses default 50% of premium)
      tp          : float | None  (None = bot uses default 100% of premium)
      symbol      : "NIFTY" (default)
    """
    note = (note or "").strip()
    if not note:
        return {"type": "none", "explanation": ""}

    dir_m     = _DIR_PAT.search(note)
    opt_m     = _OPT_PAT.search(note)
    strike_m  = _STRIKE_PAT.search(note)
    sl_m      = _SL_PAT.search(note)
    tp_m      = _TP_PAT.search(note)
    price_m   = _PRICE_PAT.search(note)
    is_market = bool(_MARKET_PAT.search(note))

    # ── Force trade: requires direction + option type at minimum ─────────────
    if dir_m and opt_m:
        direction   = dir_m.group(1).upper()
        option_type = _norm_option(opt_m.group(1))
        strike      = int(strike_m.group(1)) if strike_m else None
        sl          = float(sl_m.group(1)) if sl_m else None
        tp          = float(tp_m.group(1)) if tp_m else None

        # "at 150" → limit price (only if not also the strike)
        price = None
        if price_m and not is_market:
            candidate = float(price_m.group(1))
            # Ignore if it matches the strike (e.g. "BUY CE at 24500")
            if strike is None or candidate != strike:
                price = candidate

        if is_market:
            price = None  # explicit market order

        bias = "BULLISH" if option_type == "CE" else "BEARISH"
        strike_str = str(strike) if strike else "ATM"
        price_str  = f"at ₹{price:.0f}" if price else "at market"
        sl_str     = f"SL ₹{sl:.0f}" if sl else "SL default(50%)"
        tp_str     = f"TP ₹{tp:.0f}" if tp else "TP default(100%)"

        return {
            "type":        "force_trade",
            "direction":   direction,
            "option_type": option_type,
            "strike":      strike,
            "price":       price,
            "sl":          sl,
            "tp":          tp,
            "symbol":      "NIFTY",
            "bias":        bias,
            "explanation": (
                f"Force trade queued: {direction} {option_type} {strike_str} {price_str}, "
                f"{sl_str}, {tp_str} — bypasses signal scorer."
            ),
        }

    # ── Natural language bias ─────────────────────────────────────────────────
    words = set(re.findall(r'\b\w+\b', note.lower()))
    bear  = len(words & _BEAR_WORDS)
    bull  = len(words & _BULL_WORDS)

    if bear > bull:
        return {
            "type":        "bias",
            "bias":        "BEARISH",
            "explanation": "Bias set to BEARISH — bot will analyze and take BUY PE if signal confirms.",
        }
    if bull > bear:
        return {
            "type":        "bias",
            "bias":        "BULLISH",
            "explanation": "Bias set to BULLISH — bot will analyze and take BUY CE if signal confirms.",
        }

    # ── Pure note, no actionable content ─────────────────────────────────────
    return {"type": "none", "explanation": ""}
