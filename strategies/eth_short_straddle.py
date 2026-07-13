"""
ETH short straddle — live options strategy.

Sells one ATM call + one ATM put at a fixed target DTE, holds until:
  • 50% of the entry credit is captured (buy back at 50% of credit), or
  • the combined mark reaches 200% of the entry credit (loss = 100% of credit), or
  • expiry is within a few hours.

Research file: delta_exchange/backtest_eth_short_straddle_portfolio.py
Selected config: 5 DTE, 50% profit target, 200% stop.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from strategies.crypto_base import CryptoStrategy, OptionsSignalDecision
from core.risk_management import (
    USD_INR_RATE,
    OPTIONS_TARGET_DTE,
    OPTIONS_PROFIT_PCT,
    OPTIONS_STOP_MULT,
    OPTIONS_MARGIN_PCT_PER_LEG,
    OPTIONS_FIXED_CAPITAL_INR,
    OPTIONS_FEE_BPS,
    OPTIONS_SLIPPAGE_BPS,
    OPTIONS_MAX_MARGIN_PCT_PER_POSITION,
)

logger = logging.getLogger(__name__)


class ETHShortStraddleSignal(CryptoStrategy):
    """Short ATM straddle on ETH options traded on Delta India."""

    name = "eth_short_straddle"
    symbol = "ETHUSD"  # underlying perp symbol for context

    def __init__(self, broker=None):
        super().__init__(broker=broker)
        self._last_entry_date: Optional[str] = None

    @staticmethod
    def _parse_expiry(value) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            s = value.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(s)
            except Exception:
                pass
            # Try common formats, e.g. "31 Jul, 2026"
            for fmt in ("%d %b, %Y", "%d %b %Y"):
                try:
                    return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                except Exception:
                    pass
        return None

    def _pick_atm_pair(self, chain: list[dict], spot: float) -> Optional[dict]:
        """From a normalized option chain, pick the nearest-DTE ATM call+put."""
        now = datetime.now(timezone.utc)
        candidates = []
        for o in chain:
            expiry = self._parse_expiry(o.get("expiry"))
            if expiry is None:
                continue
            dte = (expiry - now).total_seconds() / 86400
            if dte < 0.5 or dte > OPTIONS_TARGET_DTE + 2:
                continue
            strike = o.get("strike_price")
            if strike is None:
                continue
            try:
                strike = float(strike)
            except (TypeError, ValueError):
                continue
            candidates.append({**o, "expiry": expiry, "dte": dte, "strike": strike})
        if not candidates:
            return None

        # Group by expiry, prefer expiry closest to TARGET_DTE
        by_expiry: dict[datetime, list[dict]] = {}
        for c in candidates:
            by_expiry.setdefault(c["expiry"], []).append(c)
        best_expiry = min(by_expiry.keys(), key=lambda e: abs(
            (e - now).total_seconds() / 86400 - OPTIONS_TARGET_DTE))
        grp = by_expiry[best_expiry]

        # Pick ATM strike for this expiry
        atm_strike = min(grp, key=lambda c: abs(c["strike"] - spot))["strike"]
        calls = [c for c in grp if c.get("contract_type") == "call_options"
                 and abs(c["strike"] - atm_strike) < 1e-9]
        puts = [c for c in grp if c.get("contract_type") == "put_options"
                and abs(c["strike"] - atm_strike) < 1e-9]
        if not calls or not puts:
            return None
        call = max(calls, key=lambda c: c.get("oi") or 0)
        put = max(puts, key=lambda c: c.get("oi") or 0)
        return {
            "expiry": best_expiry,
            "call": call,
            "put": put,
            "strike": atm_strike,
            "dte": (best_expiry - now).total_seconds() / 86400,
        }

    def _compute_signal(self) -> Optional[OptionsSignalDecision]:
        """Return a short-straddle decision if today has not yet entered."""
        today = datetime.now(timezone.utc).date().isoformat()
        if self._last_entry_date == today:
            return None

        spot = self.broker.get_perp_mark(self.symbol)
        if spot is None or spot <= 0:
            logger.debug("%s: no spot mark", self.name)
            return None

        chain = self.broker.get_option_chain("ETH")
        if len(chain) < 6:
            logger.debug("%s: option chain too small (%d)", self.name, len(chain))
            return None

        pair = self._pick_atm_pair(chain, spot)
        if pair is None:
            logger.debug("%s: no suitable ATM pair", self.name)
            return None

        call_mark = float(pair["call"].get("mark") or 0)
        put_mark = float(pair["put"].get("mark") or 0)
        if call_mark <= 0 or put_mark <= 0:
            logger.debug("%s: invalid marks call=%s put=%s", self.name, call_mark, put_mark)
            return None

        contract_size_raw = pair["call"].get("contract_size") or pair["call"].get("lot_size")
        if contract_size_raw is None:
            # Fallback: assume 1 ETH per contract if the broker did not report
            # a size. The runner will warn loudly so the operator can correct it.
            contract_size = 1.0
        else:
            try:
                contract_size = float(contract_size_raw)
            except (TypeError, ValueError):
                contract_size = 1.0

        # Entry credit and margin
        credit = call_mark + put_mark
        leg_notional = spot * contract_size
        margin_per_straddle = 2 * leg_notional * OPTIONS_MARGIN_PCT_PER_LEG

        capital_usd = OPTIONS_FIXED_CAPITAL_INR / USD_INR_RATE
        if margin_per_straddle <= 0:
            return None

        max_by_capital = int(capital_usd / margin_per_straddle)
        max_by_concentration = int(
            (capital_usd * OPTIONS_MAX_MARGIN_PCT_PER_POSITION) / margin_per_straddle
        )
        qty = max(1, min(max_by_capital, max_by_concentration))

        # If capital cannot afford even one straddle, do not trade and warn.
        if max_by_capital < 1:
            logger.warning(
                "%s: fixed capital $%.2f (₹%.0f) cannot cover one straddle "
                "margin $%.2f at spot %.2f; skipping",
                self.name, capital_usd, OPTIONS_FIXED_CAPITAL_INR,
                margin_per_straddle, spot,
            )
            return None

        total_margin = qty * margin_per_straddle
        self._last_entry_date = today

        logger.info(
            "%s: signal expiry=%s K=%.2f qty=%d credit=%.4f margin=%.2f "
            "call=%s put=%s",
            self.name, pair["expiry"].isoformat(), pair["strike"], qty,
            credit, total_margin, pair["call"]["symbol"], pair["put"]["symbol"],
        )

        return OptionsSignalDecision(
            name=self.name,
            underlying="ETH",
            expiry=pair["expiry"].isoformat(),
            call_symbol=pair["call"]["symbol"],
            put_symbol=pair["put"]["symbol"],
            call_strike=pair["strike"],
            put_strike=pair["strike"],
            call_mark=call_mark,
            put_mark=put_mark,
            spot_mark=spot,
            contract_size=contract_size,
            qty=qty,
            margin_per_straddle=margin_per_straddle,
            total_margin=total_margin,
            profit_pct=OPTIONS_PROFIT_PCT,
            stop_mult=OPTIONS_STOP_MULT,
            fee_bps=OPTIONS_FEE_BPS,
            slippage_bps=OPTIONS_SLIPPAGE_BPS,
            metadata={"dte": pair["dte"]},
        )
