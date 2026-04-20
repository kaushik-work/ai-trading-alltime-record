"""
paper_seller.py — Side-by-side paper comparison: Option Buyer vs Option Seller.

Every time a strategy fires will_trade=True, this module opens a mirror paper trade:

  BUYER:  buys CE (BUY signal) or PE (SELL signal) at ATM strike — standard directional bet
  SELLER: sells PE (BUY signal) or CE (SELL signal) at same ATM — collects premium, profits from decay

Both use real Angel One LTP for entry and every-5-min mark-to-market.
No real orders placed. Pure paper comparison.

Buyer  SL/TP: option drops 50% → SL  |  option gains 100% → TP
Seller SL/TP: sold option doubles → SL  |  sold option decays 65% → TP

Positions capped at 1 per strategy (new one only after previous closes).
All trades saved to logs/paper_comparison.json.
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

LOGS_DIR    = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
OUTPUT_FILE = os.path.join(LOGS_DIR, "paper_comparison.json")

LOT_SIZE = 65
LOTS     = 3

# Buyer SL/TP — as fraction of entry premium
BUYER_SL_FRAC = 0.50   # option drops to 50% of entry → stop out
BUYER_TP_FRAC = 2.00   # option doubles → take profit

# Seller SL/TP — as fraction of entry premium
SELLER_SL_FRAC = 2.00  # sold option doubles → stop out (pay to buy back)
SELLER_TP_FRAC = 0.35  # sold option decays to 35% → take profit


def _now_ist() -> str:
    return datetime.now(IST).isoformat()


def _load_trades() -> list:
    try:
        with open(OUTPUT_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_trades(trades: list):
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(trades, f, indent=2, default=str)


class PaperSeller:
    """
    Tracks paper buyer + seller positions for every strategy signal.
    Call on_signal() after each strategy cycle.
    Call mark_to_market() every 5 min.
    Call eod_close() at 15:10.
    """

    def __init__(self):
        self._open: dict = {}   # strategy_name → position dict
        self._trades: list = _load_trades()  # all closed trades

    # ── Public interface ──────────────────────────────────────────────────────

    def on_signal(self, strategy: str, signal: dict):
        """
        Called after each strategy cycle.
        Opens a new paper comparison trade if:
          - signal.will_trade is True
          - No open position already exists for this strategy
        """
        if not signal.get("will_trade"):
            return
        if strategy in self._open:
            return   # already tracking this strategy

        direction = signal.get("direction", "HOLD")
        score     = signal.get("score", 0)
        if direction not in ("BUY", "SELL"):
            return

        pos = self._open_position(strategy, direction, score)
        if pos:
            self._open[strategy] = pos
            logger.info(
                "PaperSeller: opened %s | %s | buyer=%s@₹%.0f seller=%s@₹%.0f",
                strategy, direction,
                pos["buyer"]["option_type"], pos["buyer"]["entry_premium"],
                pos["seller"]["option_type"], pos["seller"]["entry_premium"],
            )

    def mark_to_market(self):
        """
        Fetch current LTPs for all open paper positions.
        Close any that hit SL/TP.
        """
        if not self._open:
            return

        from data.angel_fetcher import AngelFetcher
        af = AngelFetcher.get()

        for strategy, pos in list(self._open.items()):
            try:
                self._update_position(af, strategy, pos)
            except Exception as e:
                logger.warning("PaperSeller.mark_to_market [%s]: %s", strategy, e)

    def eod_close(self):
        """Force-close all open paper positions at current LTP (EOD)."""
        if not self._open:
            return

        from data.angel_fetcher import AngelFetcher
        af = AngelFetcher.get()

        for strategy, pos in list(self._open.items()):
            try:
                self._close_position(af, strategy, pos, "EOD")
            except Exception as e:
                logger.warning("PaperSeller.eod_close [%s]: %s", strategy, e)

    def get_open_positions(self) -> list:
        return list(self._open.values())

    def get_all_trades(self) -> list:
        return self._trades.copy()

    def get_summary(self) -> dict:
        closed = [t for t in self._trades if t.get("status") == "CLOSED"]
        if not closed:
            return {
                "total_trades": 0,
                "buyer_total_pnl": 0, "buyer_wins": 0, "buyer_losses": 0,
                "seller_total_pnl": 0, "seller_wins": 0, "seller_losses": 0,
                "open_count": len(self._open),
            }
        buyer_pnls  = [t["buyer"]["pnl"] for t in closed]
        seller_pnls = [t["seller"]["pnl"] for t in closed]
        return {
            "total_trades":    len(closed),
            "buyer_total_pnl":  round(sum(buyer_pnls), 2),
            "buyer_wins":       sum(1 for p in buyer_pnls if p > 0),
            "buyer_losses":     sum(1 for p in buyer_pnls if p <= 0),
            "buyer_avg_pnl":    round(sum(buyer_pnls) / len(closed), 2),
            "seller_total_pnl": round(sum(seller_pnls), 2),
            "seller_wins":      sum(1 for p in seller_pnls if p > 0),
            "seller_losses":    sum(1 for p in seller_pnls if p <= 0),
            "seller_avg_pnl":   round(sum(seller_pnls) / len(closed), 2),
            "open_count":       len(self._open),
            "winner":           "BUYER" if sum(buyer_pnls) >= sum(seller_pnls) else "SELLER",
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _open_position(self, strategy: str, direction: str, score: int) -> Optional[dict]:
        try:
            from data.angel_fetcher import AngelFetcher
            af = AngelFetcher.get()

            nifty_ltp = af.get_index_ltp("NIFTY")
            if not nifty_ltp:
                logger.warning("PaperSeller: no NIFTY LTP, skipping %s", strategy)
                return None

            strike    = round(nifty_ltp / 50) * 50  # nearest 50
            expiry    = af.nearest_weekly_expiry()

            # Buyer buys CE (BUY) or PE (SELL)
            buyer_type  = "CE" if direction == "BUY" else "PE"
            # Seller sells PE (BUY) or CE (SELL) — opposite option, same directional view
            seller_type = "PE" if direction == "BUY" else "CE"

            buyer_sym,  buyer_ltp  = af.get_option_ltp("NIFTY", strike, buyer_type,  expiry)
            seller_sym, seller_ltp = af.get_option_ltp("NIFTY", strike, seller_type, expiry)

            if not buyer_ltp or not seller_ltp:
                logger.warning(
                    "PaperSeller: LTP unavailable %s strike=%d buyer=%s seller=%s",
                    strategy, strike, buyer_ltp, seller_ltp,
                )
                return None

            return {
                "id":            f"{strategy}-{datetime.now(IST).strftime('%Y%m%d%H%M%S')}",
                "strategy":      strategy,
                "direction":     direction,
                "score":         score,
                "strike":        strike,
                "expiry":        expiry,
                "nifty_entry":   round(nifty_ltp, 2),
                "lot_size":      LOT_SIZE,
                "lots":          LOTS,
                "entry_time":    _now_ist(),
                "exit_time":     None,
                "status":        "OPEN",

                "buyer": {
                    "option_type":     buyer_type,
                    "symbol":          buyer_sym or "",
                    "entry_premium":   round(buyer_ltp, 2),
                    "current_premium": round(buyer_ltp, 2),
                    "sl":  round(buyer_ltp  * BUYER_SL_FRAC, 2),
                    "tp":  round(buyer_ltp  * BUYER_TP_FRAC, 2),
                    "pnl": 0.0,
                    "status":          "OPEN",
                    "exit_premium":    None,
                    "exit_reason":     None,
                },

                "seller": {
                    "option_type":     seller_type,
                    "symbol":          seller_sym or "",
                    "entry_premium":   round(seller_ltp, 2),
                    "current_premium": round(seller_ltp, 2),
                    "sl":  round(seller_ltp * SELLER_SL_FRAC, 2),
                    "tp":  round(seller_ltp * SELLER_TP_FRAC, 2),
                    "pnl": 0.0,
                    "status":          "OPEN",
                    "exit_premium":    None,
                    "exit_reason":     None,
                },
            }

        except Exception as e:
            logger.error("PaperSeller._open_position [%s]: %s", strategy, e)
            return None

    def _update_position(self, af, strategy: str, pos: dict):
        expiry = pos["expiry"]
        strike = pos["strike"]

        buyer_side  = pos["buyer"]
        seller_side = pos["seller"]

        both_closed = (
            buyer_side["status"] != "OPEN" and
            seller_side["status"] != "OPEN"
        )
        if both_closed:
            self._finalize(strategy, pos)
            return

        # Fetch current LTPs
        if buyer_side["status"] == "OPEN":
            _, buyer_ltp = af.get_option_ltp("NIFTY", strike, buyer_side["option_type"], expiry)
            if buyer_ltp:
                buyer_side["current_premium"] = round(buyer_ltp, 2)
                buyer_side["pnl"] = round(
                    (buyer_ltp - buyer_side["entry_premium"]) * LOT_SIZE * LOTS, 2
                )
                # Check SL/TP
                if buyer_ltp <= buyer_side["sl"]:
                    buyer_side["status"]       = "CLOSED"
                    buyer_side["exit_premium"] = round(buyer_ltp, 2)
                    buyer_side["exit_reason"]  = "SL"
                elif buyer_ltp >= buyer_side["tp"]:
                    buyer_side["status"]       = "CLOSED"
                    buyer_side["exit_premium"] = round(buyer_ltp, 2)
                    buyer_side["exit_reason"]  = "TP"

        if seller_side["status"] == "OPEN":
            _, seller_ltp = af.get_option_ltp("NIFTY", strike, seller_side["option_type"], expiry)
            if seller_ltp:
                seller_side["current_premium"] = round(seller_ltp, 2)
                # Seller profits when option DECAYS (entry - current)
                seller_side["pnl"] = round(
                    (seller_side["entry_premium"] - seller_ltp) * LOT_SIZE * LOTS, 2
                )
                # Check SL/TP
                if seller_ltp >= seller_side["sl"]:
                    seller_side["status"]       = "CLOSED"
                    seller_side["exit_premium"] = round(seller_ltp, 2)
                    seller_side["exit_reason"]  = "SL"
                elif seller_ltp <= seller_side["tp"]:
                    seller_side["status"]       = "CLOSED"
                    seller_side["exit_premium"] = round(seller_ltp, 2)
                    seller_side["exit_reason"]  = "TP"

        # If both sides now closed, finalize
        if buyer_side["status"] != "OPEN" and seller_side["status"] != "OPEN":
            self._finalize(strategy, pos)

    def _close_position(self, af, strategy: str, pos: dict, reason: str):
        """Force-close both sides (used for EOD)."""
        expiry = pos["expiry"]
        strike = pos["strike"]

        for side_key, option_type in [("buyer", pos["buyer"]["option_type"]),
                                       ("seller", pos["seller"]["option_type"])]:
            side = pos[side_key]
            if side["status"] != "OPEN":
                continue
            _, ltp = af.get_option_ltp("NIFTY", strike, option_type, expiry)
            exit_prem = ltp or side["current_premium"]
            side["status"]       = "CLOSED"
            side["exit_premium"] = round(exit_prem, 2)
            side["exit_reason"]  = reason
            if side_key == "buyer":
                side["pnl"] = round(
                    (exit_prem - side["entry_premium"]) * LOT_SIZE * LOTS, 2
                )
            else:
                side["pnl"] = round(
                    (side["entry_premium"] - exit_prem) * LOT_SIZE * LOTS, 2
                )

        self._finalize(strategy, pos)

    def _finalize(self, strategy: str, pos: dict):
        pos["status"]    = "CLOSED"
        pos["exit_time"] = _now_ist()
        self._trades.append(pos)
        _save_trades(self._trades)
        self._open.pop(strategy, None)
        logger.info(
            "PaperSeller closed %s | buyer_pnl=₹%.0f(%s) | seller_pnl=₹%.0f(%s)",
            strategy,
            pos["buyer"]["pnl"],  pos["buyer"]["exit_reason"],
            pos["seller"]["pnl"], pos["seller"]["exit_reason"],
        )


# ── Singleton ─────────────────────────────────────────────────────────────────

_seller: Optional[PaperSeller] = None


def get_paper_seller() -> PaperSeller:
    global _seller
    if _seller is None:
        _seller = PaperSeller()
    return _seller
