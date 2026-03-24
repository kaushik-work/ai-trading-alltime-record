"""
Telegram notification module.
Uses the Bot API directly via requests — no extra dependencies.
All functions are silent-fail: a broken Telegram config never crashes the bot.

Setup:
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Message @userinfobot on Telegram → it shows your numeric Chat ID
  3. Add both to .env:
       TELEGRAM_BOT_TOKEN=123456:ABCdef...
       TELEGRAM_CHAT_ID=987654321
"""
import logging
import requests
import config

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"


def _send(text: str) -> None:
    """Send a Telegram message. Silently skipped if credentials are not set."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    try:
        resp = requests.post(
            _API.format(token=config.TELEGRAM_BOT_TOKEN),
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=5,
        )
        if not resp.ok:
            logger.warning("Telegram send failed (%d): %s", resp.status_code, resp.text[:100])
    except Exception as e:
        logger.warning("Telegram notification error: %s", e)


# ── Public notification functions ─────────────────────────────────────────────

def notify_bot_started(mode: str) -> None:
    emoji = "📄" if mode == "paper" else "🔴"
    _send(
        f"{emoji} <b>AI Trading Bot Started</b>\n"
        f"Mode: <b>{mode.upper()}</b>\n"
        f"Trading every 5 min during 09:15–15:30 IST"
    )


def notify_bot_stopped() -> None:
    _send("⛔ <b>AI Trading Bot Stopped</b>")


def notify_paused(reason: str = "Manual intervention") -> None:
    _send(f"⏸ <b>Bot PAUSED</b>\nReason: {reason}")


def notify_resumed() -> None:
    _send("▶️ <b>Bot RESUMED</b> — trading active")


def notify_trade(order: dict, decision: dict) -> None:
    side = order.get("side", "")
    symbol = order.get("symbol", "")
    qty = order.get("quantity", 0)
    price = order.get("price", 0)
    pnl = order.get("pnl")
    confidence = decision.get("confidence", 0)
    reasoning = decision.get("reasoning", "")
    risk = decision.get("risk_level", "")

    emoji = "🟢" if side == "BUY" else "🔴"
    pnl_line = f"\nP&L: <b>₹{pnl:,.2f}</b>" if pnl is not None else ""

    _send(
        f"{emoji} <b>{side}: {symbol}</b>\n"
        f"Qty: {qty} @ ₹{price:,.2f}{pnl_line}\n"
        f"Confidence: {confidence*100:.0f}% | Risk: {risk}\n"
        f"<i>{reasoning}</i>"
    )


def notify_stop_loss(symbol: str, pnl_pct: float, pnl_inr: float) -> None:
    _send(
        f"🛑 <b>STOP-LOSS triggered: {symbol}</b>\n"
        f"Loss: <b>{pnl_pct:.2f}%</b> (₹{pnl_inr:,.2f})\n"
        f"Position closed automatically."
    )


def notify_record_broken(records: list) -> None:
    if not records:
        return
    lines = "\n".join(f"  🏆 {r}" for r in records)
    _send(f"<b>ALL-TIME RECORD BROKEN!</b>\n{lines}")


def notify_daily_loss_limit(limit: float) -> None:
    _send(
        f"⚠️ <b>Daily Loss Limit Hit</b>\n"
        f"Loss exceeded ₹{limit:,.0f}. Bot auto-paused."
    )


def notify_eod_summary(summary: dict) -> None:
    date = summary.get("date", "today")
    trades = summary.get("trades", 0)
    broken = summary.get("broken_records", [])
    review = summary.get("review", "")

    record_line = f"\nRecords broken: {', '.join(broken)}" if broken else ""
    review_snippet = f"\n\n<i>{review[:300]}...</i>" if len(review) > 300 else f"\n\n<i>{review}</i>"

    _send(
        f"📊 <b>End-of-Day Summary — {date}</b>\n"
        f"Trades: {trades}{record_line}"
        f"{review_snippet}"
    )


def notify_force_trade(symbol: str, side: str, quantity: int, reason: str) -> None:
    emoji = "🟢" if side == "BUY" else "🔴"
    _send(
        f"{emoji} <b>MANUAL OVERRIDE: {side} {symbol}</b>\n"
        f"Qty: {quantity}\n"
        f"Reason: {reason}"
    )
