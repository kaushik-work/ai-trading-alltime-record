"""Option chain fetcher — live OI data from Angel One (60s TTL cache)."""

import logging
import threading
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_ERROR_DICT = {
    "bias": "NEUTRAL", "pcr": 1.0, "sentiment": "neutral",
    "ce_wall": 0, "pe_wall": 0, "max_pain": 0,
    "spot": 0, "atm": 0, "strikes": [],
}


class OptionChainFetcher:
    _instance: Optional["OptionChainFetcher"] = None
    _singleton_lock = threading.Lock()

    def __init__(self):
        self._cache: Optional[dict] = None
        self._cache_time: Optional[datetime] = None
        self._lock = threading.Lock()

    @classmethod
    def get(cls) -> "OptionChainFetcher":
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def fetch(self, symbol: str = "NIFTY") -> dict:
        with self._lock:
            now = datetime.now()
            if (self._cache is not None and self._cache_time is not None
                    and (now - self._cache_time).total_seconds() < 60):
                return self._cache
            result = self._fetch_live(symbol)
            self._cache = result
            self._cache_time = now
            return result

    def _fetch_live(self, symbol: str) -> dict:
        try:
            from data.angel_fetcher import AngelFetcher, _parse_expiry
            fetcher = AngelFetcher.get()

            # 1. Spot price
            spot = fetcher.get_index_ltp(symbol)
            if not spot:
                return {**_ERROR_DICT, "error": "Could not fetch spot price", "fetched_at": datetime.now().isoformat()}

            # 2. ATM strike (nearest 50)
            atm = int(round(spot / 50)) * 50

            # 3. Build strikes list: ATM ±300 in steps of 50 (13 strikes)
            strikes_list = [atm + (i * 50) for i in range(-6, 7)]

            # 4. Expiry
            expiry = AngelFetcher.nearest_weekly_expiry()
            expiry_str = expiry.strftime("%d%b%Y").upper()

            # 5. Build token lookup from master instruments
            instruments = fetcher._nfo_instruments()

            def _master_strike(i) -> int:
                return int(float(i.get("strike", 0))) // 100

            ce_tokens = {}  # strike -> token
            pe_tokens = {}

            for strike in strikes_list:
                for inst in instruments:
                    if (inst.get("name") == symbol
                            and _master_strike(inst) == strike
                            and _parse_expiry(inst.get("expiry", "")) == expiry):
                        sym = inst.get("symbol", "")
                        if sym.endswith("CE"):
                            ce_tokens[strike] = inst["token"]
                        elif sym.endswith("PE"):
                            pe_tokens[strike] = inst["token"]

            all_tokens = list(ce_tokens.values()) + list(pe_tokens.values())

            if not all_tokens:
                logger.warning("OptionChainFetcher: no tokens found for %s expiry %s", symbol, expiry_str)
                return {**_ERROR_DICT, "error": f"No option tokens found for expiry {expiry_str}", "fetched_at": datetime.now().isoformat()}

            # 6. Ensure logged in and fetch market data
            if not fetcher._ensure_logged_in():
                return {**_ERROR_DICT, "error": "Angel One not logged in", "fetched_at": datetime.now().isoformat()}

            resp = fetcher._api.getMarketData(
                mode="QUOTE",
                exchangeTokens={"NFO": all_tokens},
            )

            fetched_list = []
            if resp and resp.get("data") and resp["data"].get("fetched"):
                fetched_list = resp["data"]["fetched"]

            # 7. Build token→data map
            token_data = {}
            for item in fetched_list:
                tok = str(item.get("symbolToken", ""))
                token_data[tok] = item

            # 8. Build per-strike rows
            strike_rows = []
            for strike in strikes_list:
                ce_tok = ce_tokens.get(strike)
                pe_tok = pe_tokens.get(strike)
                ce_item = token_data.get(str(ce_tok), {}) if ce_tok else {}
                pe_item = token_data.get(str(pe_tok), {}) if pe_tok else {}

                ce_ltp = float(ce_item.get("ltp", 0) or 0)
                pe_ltp = float(pe_item.get("ltp", 0) or 0)
                ce_oi  = int(ce_item.get("openInterest", 0) or 0)
                pe_oi  = int(pe_item.get("openInterest", 0) or 0)

                strike_rows.append({
                    "strike": strike,
                    "ce_ltp": ce_ltp,
                    "pe_ltp": pe_ltp,
                    "ce_oi": ce_oi,
                    "pe_oi": pe_oi,
                })

            # 9. Check OI data quality
            strikes_with_oi = sum(1 for r in strike_rows if r["ce_oi"] > 0 or r["pe_oi"] > 0)
            if strikes_with_oi < 4:
                logger.warning("OptionChainFetcher: only %d strikes have OI data — insufficient", strikes_with_oi)
                return {**_ERROR_DICT, "error": f"Insufficient OI data: only {strikes_with_oi} strikes have OI", "fetched_at": datetime.now().isoformat()}

            # 10. Compute metrics
            total_ce_oi = sum(r["ce_oi"] for r in strike_rows)
            total_pe_oi = sum(r["pe_oi"] for r in strike_rows)
            pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 1.0

            ce_wall = max(strike_rows, key=lambda r: r["ce_oi"])["strike"]
            pe_wall = max(strike_rows, key=lambda r: r["pe_oi"])["strike"]

            # Max pain: strike where total option buyer loss is maximised
            max_pain = atm
            min_loss = None
            for row_s in strike_rows:
                S = row_s["strike"]
                total_loss = sum(
                    max(0, K["strike"] - S) * K["ce_oi"] + max(0, S - K["strike"]) * K["pe_oi"]
                    for K in strike_rows
                )
                if min_loss is None or total_loss < min_loss:
                    min_loss = total_loss
                    max_pain = S

            if pcr > 1.3:
                sentiment = "very_bullish"
            elif pcr > 1.1:
                sentiment = "bullish"
            elif pcr < 0.7:
                sentiment = "very_bearish"
            elif pcr < 0.9:
                sentiment = "bearish"
            else:
                sentiment = "neutral"

            if spot > max_pain and pcr > 1.0:
                bias = "CE_FAVORED"
            elif spot < max_pain and pcr < 1.0:
                bias = "PE_FAVORED"
            else:
                bias = "NEUTRAL"

            result = {
                "pcr": round(pcr, 4),
                "sentiment": sentiment,
                "ce_wall": ce_wall,
                "pe_wall": pe_wall,
                "max_pain": max_pain,
                "bias": bias,
                "spot": spot,
                "atm": atm,
                "strikes": strike_rows,
                "fetched_at": datetime.now().isoformat(),
                "error": None,
            }
            logger.info(
                "OptionChainFetcher: %s spot=%.0f ATM=%d PCR=%.2f bias=%s max_pain=%d ce_wall=%d pe_wall=%d",
                symbol, spot, atm, pcr, bias, max_pain, ce_wall, pe_wall,
            )
            return result

        except Exception as e:
            logger.error("OptionChainFetcher._fetch_live %s: %s", symbol, e)
            return {**_ERROR_DICT, "error": str(e), "fetched_at": datetime.now().isoformat()}
