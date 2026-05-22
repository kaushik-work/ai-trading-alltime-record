"""
Shadow trade book for the Q5 ATM-Straddle signal.

Holds the in-flight shadow position in memory and mirrors every state change
to Mongo (collection: `shadow_trades`). One position at a time.

No real orders are ever placed by this module — it's a forward-test ledger.

Schema (Mongo `shadow_trades`):
    {
        _id:           ObjectId,
        signal_id:     "shadow_{date}_{HHMMSS}",   # unique per entry
        date:          "2026-05-22",
        side:          "CE",
        strike:        25400,
        entry_dt:      "2026-05-22 10:35:00",
        entry_premium: 152.40,
        sl_price:      142.40,
        tp_price:      182.40,
        threshold:     310.50,
        spot_at_entry: 25410.75,
        status:        "OPEN" | "CLOSED",
        exit_dt:       "2026-05-22 11:05:00",     # CLOSED only
        exit_premium:  182.40,                     # CLOSED only
        reason:        "TP" | "SL" | "EOD",        # CLOSED only
        pnl:           1950.00,                    # CLOSED only (lot_size * 1)
        lot_size:      65,
    }
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from typing import Optional

from strategies.feature_signals import SL_DIST, RR, SIDE

logger = logging.getLogger(__name__)

LOT_SIZE  = 65
EOD_LIMIT = dtime(15, 20)
MAX_TRADES_PER_DAY = 4   # hard cap per strategy per day


class ShadowBook:
    """One open position max PER STRATEGY. In-memory truth, Mongo for durability.

    Multiple ShadowBook instances run in parallel — one per signal — keyed by
    strategy_name. Each instance's open position is namespaced in Mongo via
    the `strategy` field on shadow_trades.
    """

    def __init__(self, strategy_name: str = "q5_straddle_level"):
        self.strategy = strategy_name
        self._open: Optional[dict] = None
        self._restored = False

    def _restore_from_mongo(self) -> None:
        """On bot startup, see if there's an OPEN shadow trade from today
        for THIS strategy (e.g. if the api container restarted mid-trade)."""
        if self._restored:
            return
        self._restored = True
        try:
            from core import mongo
            db = mongo.get_db()
            if db is None:
                return
            today = datetime.now().date().isoformat()
            doc = db.shadow_trades.find_one(
                {"date": today, "status": "OPEN", "strategy": self.strategy},
                sort=[("entry_dt", -1)],
            )
            if doc:
                self._open = doc
                logger.info("ShadowBook[%s]: restored OPEN position: %s @ Rs %.2f",
                            self.strategy, doc.get("signal_id"),
                            doc.get("entry_premium", 0))
        except Exception as e:
            logger.debug("ShadowBook[%s] restore failed (non-fatal): %s",
                         self.strategy, e)

    def has_open(self) -> bool:
        self._restore_from_mongo()
        return self._open is not None

    def trades_today(self) -> int:
        """Count of trades opened today for this strategy (OPEN or CLOSED).
        Used by the daily cap. Reads Mongo so multiple api containers stay in sync."""
        try:
            from core import mongo
            db = mongo.get_db()
            if db is None:
                return 0
            today = datetime.now().date().isoformat()
            return db.shadow_trades.count_documents({
                "strategy": self.strategy,
                "date":     today,
            })
        except Exception:
            return 0

    def cap_reached(self) -> bool:
        return self.trades_today() >= MAX_TRADES_PER_DAY

    def open(self, entry_dt: datetime, strike: int, side: str,
             premium: float, threshold: float, spot: float) -> dict:
        """Open a new shadow position. Caller should check has_open() first."""
        if self._open is not None:
            logger.warning("ShadowBook.open called while position already open — refusing")
            return self._open
        if self.cap_reached():
            logger.info("ShadowBook[%s]: daily cap %d reached — refusing new entry",
                        self.strategy, MAX_TRADES_PER_DAY)
            return {}

        # Risk budget gate: aggregate loss cap, per-strategy loss cap,
        # same-strike correlation guard
        try:
            from core import risk_budget
            allowed, reason = risk_budget.allow_entry(self.strategy,
                                                       strike=int(strike),
                                                       side=side)
            if not allowed:
                logger.info("ShadowBook[%s]: risk budget refused entry — %s",
                            self.strategy, reason)
                return {}
            lot_mult = risk_budget.current_lot_multiplier(self.strategy)
        except Exception as _rb_err:
            logger.debug("ShadowBook[%s]: risk_budget unavailable, defaulting to 1× — %s",
                         self.strategy, _rb_err)
            lot_mult = 1

        sl_price = round(premium - SL_DIST, 2)
        tp_price = round(premium + SL_DIST * RR, 2)
        signal_id = f"{self.strategy}_{entry_dt.strftime('%Y%m%d_%H%M%S')}"

        doc = {
            "signal_id":      signal_id,
            "strategy":       self.strategy,
            "date":           entry_dt.date().isoformat(),
            "side":           side,
            "strike":         int(strike),
            "entry_dt":       entry_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "entry_premium":  round(premium, 2),
            "sl_price":       sl_price,
            "tp_price":       tp_price,
            "threshold":      round(threshold, 2),
            "spot_at_entry":  round(spot, 2),
            "status":         "OPEN",
            "lot_size":       LOT_SIZE,
            "lot_multiplier": lot_mult,
        }
        self._open = doc

        try:
            from core import mongo
            mongo.mirror_shadow_open(doc)
        except Exception as e:
            logger.warning("ShadowBook mirror_open failed: %s", e)

        logger.info("ShadowBook[%s] OPEN  %s  strike=%d  prem=Rs %.2f  SL=%.2f  TP=%.2f",
                    self.strategy, signal_id, strike, premium, sl_price, tp_price)
        return doc

    def tick(self, now_dt: datetime, current_premium: float) -> Optional[dict]:
        """Check SL/TP/EOD against current ATM CE premium. Return closed doc or None.

        Caller is responsible for fetching the LTP of the OPEN position's strike+side.
        """
        if self._open is None:
            return None
        p = self._open
        if now_dt.time() >= EOD_LIMIT:
            return self._close(now_dt, current_premium, "EOD")
        if current_premium <= p["sl_price"]:
            return self._close(now_dt, p["sl_price"], "SL")
        if current_premium >= p["tp_price"]:
            return self._close(now_dt, p["tp_price"], "TP")
        return None

    def _close(self, exit_dt: datetime, exit_premium: float, reason: str) -> dict:
        if self._open is None:
            return {}
        p = self._open
        lot_mult = int(p.get("lot_multiplier", 1) or 1)
        pnl = round((exit_premium - p["entry_premium"]) * LOT_SIZE * lot_mult, 2)
        p.update({
            "status":       "CLOSED",
            "exit_dt":      exit_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "exit_premium": round(exit_premium, 2),
            "reason":       reason,
            "pnl":          pnl,
        })
        try:
            from core import mongo
            mongo.mirror_shadow_close(p["signal_id"], exit_dt.strftime("%Y-%m-%d %H:%M:%S"),
                                       round(exit_premium, 2), reason, pnl)
        except Exception as e:
            logger.warning("ShadowBook mirror_close failed: %s", e)
        logger.info("ShadowBook[%s] CLOSE %s  reason=%s  exit=Rs %.2f  pnl=Rs %+.2f",
                    self.strategy, p["signal_id"], reason, exit_premium, pnl)
        closed = p
        self._open = None
        return closed

    def open_position(self) -> Optional[dict]:
        """Read-only accessor for the current open position (for scheduler logic)."""
        return self._open
