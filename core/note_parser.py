"""
Parse trader notes from the Day Bias panel.

Three outcomes:
  FORCE_TRADE  — explicit direction + option type + strike (+ optional SL/TP)
                 Example: "BUY CE 22500 SL 50 TP 150"
  BIAS         — natural language directional intent, no specific strike
                 Example: "take trades today towards downwards"
  UNCLEAR      — attempted trade instruction but missing required fields
  NONE         — no actionable content (plain note / empty)
"""
import re

_DIR_PAT    = re.compile(r'\b(buy|sell)\b', re.IGNORECASE)
_OPT_PAT    = re.compile(r'\b(ce|call|pe|put)\b', re.IGNORECASE)
_STRIKE_PAT = re.compile(r'\b(\d{4,6})\b')
_SL_PAT     = re.compile(r'\b(?:sl|stoploss|stop[\s-]?loss|stop)\s*[:\s@]\s*(\d+(?:\.\d+)?)', re.IGNORECASE)
_TP_PAT     = re.compile(r'\b(?:tp|target|tgt|take[\s-]?profit)\s*[:\s@]\s*(\d+(?:\.\d+)?)', re.IGNORECASE)

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
      strike      : int
      sl          : float | None
      tp          : float | None
      symbol      : "NIFTY" (default)
      --- unclear fields ---
      missing     : list[str]
    """
    note = (note or "").strip()
    if not note:
        return {"type": "none", "explanation": ""}

    dir_m    = _DIR_PAT.search(note)
    opt_m    = _OPT_PAT.search(note)
    strike_m = _STRIKE_PAT.search(note)
    sl_m     = _SL_PAT.search(note)
    tp_m     = _TP_PAT.search(note)

    # ── Force trade: has direction + option type + strike ─────────────────────
    if dir_m and opt_m and strike_m:
        direction   = dir_m.group(1).upper()
        option_type = _norm_option(opt_m.group(1))
        strike      = int(strike_m.group(1))
        sl          = float(sl_m.group(1)) if sl_m else None
        tp          = float(tp_m.group(1)) if tp_m else None
        missing     = []
        if sl is None:
            missing.append("SL (stop loss amount)")
        if tp is None:
            missing.append("TP (target amount)")

        if missing:
            return {
                "type":        "unclear",
                "missing":     missing,
                "partial":     {"direction": direction, "option_type": option_type, "strike": strike},
                "explanation": (
                    f"Partially understood: {direction} {option_type} at strike {strike}. "
                    f"Missing: {', '.join(missing)}. "
                    f"Full example: {direction} {option_type} {strike} SL 50 TP 150"
                ),
            }

        bias = "BULLISH" if option_type == "CE" else "BEARISH"
        return {
            "type":        "force_trade",
            "direction":   direction,
            "option_type": option_type,
            "strike":      strike,
            "sl":          sl,
            "tp":          tp,
            "symbol":      "NIFTY",
            "bias":        bias,
            "explanation": (
                f"Force trade queued: {direction} {option_type} {strike}, "
                f"SL ₹{sl:.0f}, TP ₹{tp:.0f} — bypasses signal scorer."
            ),
        }

    # ── Partial: has direction + option but no strike ─────────────────────────
    if dir_m and opt_m:
        direction   = dir_m.group(1).upper()
        option_type = _norm_option(opt_m.group(1))
        return {
            "type":        "unclear",
            "missing":     ["strike price"],
            "partial":     {"direction": direction, "option_type": option_type},
            "explanation": (
                f"Understood {direction} {option_type} but missing strike price. "
                f"Example: {direction} {option_type} 22500 SL 50 TP 150"
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
