"""
TradeAnalyst — uses Claude to generate entry and exit remarks for each trade.

Entry remark: why the bot took this trade (signal confluence, score breakdown)
Exit remark:  how the trade went, trailing profit analysis, strategy improvement notes
"""

import logging
import config

logger = logging.getLogger(__name__)

_ENTRY_PROMPT = """You are a sharp NSE options trading analyst. A trade was just entered.
Write a 2-3 sentence entry remark covering:
1. Why this trade made sense (key signals that fired, which strategy fired it)
2. What the risk/reward setup looks like
3. What to watch for during the trade

Strategy context:
- MUSASHI (15m trend): EMA50 aligned + VWAP bias + HA flip. Threshold 7.5/10. ATR≈55pts.
- RAIJIN (5m mean-reversion): Price at VWAP ±2σ or ≥30pts from VWAP. Threshold 6.0/10. ATR≈20pts.
- ATR INTRADAY: Multi-signal -10 to +10 scale. Threshold ±5. Claude-confirmed.
- NIFTY lot size = 65 (Feb 2026) | ATM premium ₹180–220 | 1 lot = ₹13K | Charges ≈ ₹220/round-trip

Be concise, specific, and trader-focused. No fluff.

Trade details:
{details}
"""

_EXIT_PROMPT = """You are a sharp NSE options trading analyst reviewing a completed trade.
Write a 3-4 sentence post-trade analysis covering:
1. Whether the exit (SL/TP/EOD) was the right call
2. Would trailing the stop have made more profit, or was the fixed TP the right choice?
3. What this trade tells us about the strategy — any improvement suggested?
4. Overall verdict: good trade execution or not?

Strategy context:
- MUSASHI (15m trend): EMA50 aligned + VWAP bias + HA flip. Threshold 7.5/10. ATR≈55pts.
- RAIJIN (5m mean-reversion): Price at VWAP ±2σ or ≥30pts from VWAP. Threshold 6.0/10. ATR≈20pts.
- ATR INTRADAY: Multi-signal -10 to +10 scale. Threshold ±5. Claude-confirmed.
- NIFTY lot size = 65 (Feb 2026) | ATM premium ₹180–220 | 1 lot = ₹13K | Charges ≈ ₹220/trade
- Break-even: R:R 2.0 needs >33% win rate; charges add ~3–5% to that requirement

Be honest, specific, and constructive. No fluff.

Trade details:
{details}
"""


def _call_claude(prompt: str) -> str:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.warning("TradeAnalyst Claude call failed: %s", e)
        return ""


def generate_entry_remark(pos: dict, sig: dict) -> str:
    """Generate entry remark after opening a trade."""
    details = (
        f"Strategy: {pos.get('strategy')}\n"
        f"Symbol: NIFTY {pos.get('option_type')} {pos.get('strike')}\n"
        f"Side: {pos.get('side')} | Entry: ₹{pos.get('entry'):.2f}\n"
        f"SL: ₹{pos.get('sl'):.2f} | TP: ₹{pos.get('tp'):.2f}\n"
        f"Score: {pos.get('score'):.1f}/10\n"
        f"Signal breakdown: {sig.get('details', {})}\n"
        f"VWAP: {sig.get('vwap', sig.get('vwap', '—'))} | "
        f"RSI: {sig.get('rsi', sig.get('rsi9', '—'))} | "
        f"HA consecutive: {sig.get('ha_consec', '—')} | "
        f"Volume ratio: {sig.get('vol_ratio', '—')}x\n"
        f"Structure: {sig.get('structure', '—')}"
    )
    return _call_claude(_ENTRY_PROMPT.format(details=details))


def generate_exit_remark(pos: dict, close_price: float, pnl: float, reason: str) -> str:
    """Generate exit remark after closing a trade."""
    entry   = pos.get("entry", 0)
    sl      = pos.get("sl", 0)
    tp      = pos.get("tp", 0)
    side    = pos.get("side", "BUY")
    pnl_pct = ((close_price - entry) / entry * 100) if entry else 0
    if side == "SELL":
        pnl_pct = -pnl_pct

    # How far did price go toward TP before exit?
    if side == "BUY":
        tp_progress = ((close_price - entry) / (tp - entry) * 100) if tp != entry else 0
        sl_distance = ((entry - sl) / entry * 100) if entry else 0
    else:
        tp_progress = ((entry - close_price) / (entry - tp) * 100) if entry != tp else 0
        sl_distance = ((sl - entry) / entry * 100) if entry else 0

    details = (
        f"Strategy: {pos.get('strategy')}\n"
        f"Symbol: NIFTY {pos.get('option_type')} {pos.get('strike')}\n"
        f"Side: {side} | Entry: ₹{entry:.2f} | Exit: ₹{close_price:.2f}\n"
        f"SL was: ₹{sl:.2f} | TP was: ₹{tp:.2f}\n"
        f"Close reason: {reason}\n"
        f"P&L: ₹{pnl:.2f} ({pnl_pct:+.2f}%)\n"
        f"Progress toward TP at exit: {tp_progress:.1f}%\n"
        f"SL distance from entry: {sl_distance:.2f}%\n"
        f"Score at entry: {pos.get('score', '—')}/10"
    )
    return _call_claude(_EXIT_PROMPT.format(details=details))
