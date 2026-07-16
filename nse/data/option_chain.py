"""Option-chain helpers for NSE synthetic-forward strategy.

Provides:
  - OptionChainCache: live lookups via Angel One SmartAPI.
  - load_snapshots_csv / load_snapshots_mongo: historical snapshot loaders.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from data.angel_fetcher import AngelFetcher
from nse.config import EXCHANGE, STEP_SIZES, SYMBOLS

logger = logging.getLogger(__name__)


def _parse_expiry(s: str) -> Optional[date]:
    """Parse Angel One expiry strings like '24Jul2026' or '24Jul26'."""
    if not s or len(s) < 5:
        return None
    for fmt in ("%d%b%Y", "%d%b%y"):
        try:
            return datetime.strptime(s.upper(), fmt).date()
        except ValueError:
            continue
    return None


def _format_expiry(d: date) -> str:
    return d.strftime("%d%b%Y").upper()


class OptionChainCache:
    """Caches instrument master and resolves strikes/tokens for a symbol."""

    def __init__(self, symbol: str, fetcher: Optional[AngelFetcher] = None):
        if symbol not in SYMBOLS:
            raise ValueError(f"Unsupported symbol: {symbol}")
        self.symbol = symbol
        self.fetcher = fetcher or AngelFetcher.get()
        self._instrument_list: Optional[list] = None
        self._exchange = EXCHANGE[symbol]
        self._step = STEP_SIZES[symbol]

    def _instruments(self) -> list:
        if self._instrument_list is None:
            if self._exchange == "BFO":
                self._instrument_list = self.fetcher._bfo_instruments()
            else:
                self._instrument_list = self.fetcher._nfo_instruments()
        return self._instrument_list

    def nearest_expiry(self, min_days: int = 0) -> Optional[date]:
        """Return nearest tradable expiry on or after today + min_days."""
        today = date.today()
        min_date = today + timedelta(days=min_days)
        expiries = sorted({
            _parse_expiry(i["expiry"])
            for i in self._instruments()
            if i.get("name") == self.symbol
            and i.get("expiry")
            and _parse_expiry(i["expiry"]) is not None
            and _parse_expiry(i["expiry"]) >= min_date
        })
        return expiries[0] if expiries else None

    def _master_strike(self, instrument: dict) -> int:
        return int(float(instrument.get("strike", 0))) // 100

    def resolve_leg(self, strike: int, option_type: str, expiry: date):
        """Resolve a single option leg to (tradingsymbol, token).

        option_type: 'CE' or 'PE'.
        Returns (tradingsymbol, token) or (None, None).
        """
        option_type = option_type.upper()
        expiry_dt = _parse_expiry if isinstance(expiry, str) else lambda x: expiry
        if isinstance(expiry, str):
            expiry = _parse_expiry(expiry) or datetime.strptime(expiry, "%Y-%m-%d").date()
        match = next((
            i for i in self._instruments()
            if i.get("name") == self.symbol
            and self._master_strike(i) == strike
            and i.get("instrumenttype") == "OPTIDX"
            and i.get("symbol", "").endswith(option_type)
            and _parse_expiry(i.get("expiry", "")) == expiry
        ), None)
        if match is None:
            logger.warning("OptionChainCache: no instrument for %s %s %d %s",
                           self.symbol, expiry, strike, option_type)
            return None, None
        return match["symbol"], match["token"]

    def resolve_combo(self, atm: int, expiry: date, combo_side: str, lots: int):
        """Resolve a synthetic-forward combo.

        combo_side: 'long'  → buy CE @ K, sell PE @ K
                    'short' → sell CE @ K, buy PE @ K
        Returns list[ComboLeg] ready for order placement.
        """
        from nse.models import ComboLeg
        legs = []
        if combo_side == "long":
            legs.append(ComboLeg("BUY", "CE", atm, expiry, "", "", lots))
            legs.append(ComboLeg("SELL", "PE", atm, expiry, "", "", lots))
        elif combo_side == "short":
            legs.append(ComboLeg("SELL", "CE", atm, expiry, "", "", lots))
            legs.append(ComboLeg("BUY", "PE", atm, expiry, "", "", lots))
        else:
            raise ValueError(f"Invalid combo_side: {combo_side}")
        for leg in legs:
            ts, tok = self.resolve_leg(leg.strike, leg.option_type, leg.expiry)
            if ts is None or tok is None:
                return []
            leg.tradingsymbol = ts
            leg.token = tok
        return legs

    def get_underlying_ltp(self) -> Optional[float]:
        return self.fetcher.get_index_ltp(self.symbol)

    def get_option_ltps_bulk(self, strikes: list[int], option_type: str,
                             expiry: date) -> dict[int, tuple[str, float]]:
        """Return {strike: (tradingsymbol, ltp)} for a set of strikes."""
        return self.fetcher.get_option_ltps_bulk(self.symbol, strikes, option_type, expiry)

    def get_snapshot(self, expiry: date, atm: int, strikes_around: int = 8):
        """Fetch a full snapshot for backtest/signal use. Returns DataFrame."""
        step = self._step
        strikes = [atm + k * step for k in range(-strikes_around, strikes_around + 1)]
        ce = self.get_option_ltps_bulk(strikes, "CE", expiry)
        pe = self.get_option_ltps_bulk(strikes, "PE", expiry)
        spot = self.get_underlying_ltp()
        if spot is None:
            logger.warning("OptionChainCache.get_snapshot: spot unavailable for %s", self.symbol)
            return pd.DataFrame()
        rows = []
        now = datetime.now(timezone.utc)
        for strike, (ts, ltp) in ce.items():
            rows.append({
                "timestamp": now,
                "symbol": self.symbol,
                "expiry": expiry,
                "strike": strike,
                "option_type": "CE",
                "ltp": ltp,
                "spot": spot,
                "tradingsymbol": ts,
            })
        for strike, (ts, ltp) in pe.items():
            rows.append({
                "timestamp": now,
                "symbol": self.symbol,
                "expiry": expiry,
                "strike": strike,
                "option_type": "PE",
                "ltp": ltp,
                "spot": spot,
                "tradingsymbol": ts,
            })
        return pd.DataFrame(rows)


def load_snapshots_csv(symbol: str, db_dir: Optional[Path] = None,
                       compute_greeks: bool = True) -> pd.DataFrame:
    """Load historical snapshots from db/oi_snapshots/YYYY-MM-DD_SYMBOL.csv."""
    if db_dir is None:
        db_dir = Path(__file__).resolve().parents[2] / "db" / "oi_snapshots"
    files = sorted(db_dir.glob(f"*_{symbol}.csv"))
    if not files:
        raise FileNotFoundError(f"no snapshot files at {db_dir}/*_{symbol}.csv")
    dfs = []
    for f in files:
        try:
            dfs.append(pd.read_csv(f))
        except Exception as e:
            logger.warning("load_snapshots_csv: skip %s: %s", f.name, e)
    if not dfs:
        return pd.DataFrame()
    df = pd.concat(dfs, ignore_index=True)
    return _normalize_historical_df(df, symbol, compute_greeks=compute_greeks)


def load_snapshots_mongo(symbol: str, compute_greeks: bool = True) -> pd.DataFrame:
    """Load historical snapshots from MongoDB option_snapshots collection."""
    url = os.environ.get("MONGODB_URL")
    db_name = os.environ.get("MONGODB_DB_NAME")
    if not url or not db_name:
        raise RuntimeError("MONGODB_URL / MONGODB_DB_NAME not set")
    from pymongo import MongoClient
    client = MongoClient(url, serverSelectionTimeoutMS=5000)
    col = client[db_name]["option_snapshots"]
    rows = list(col.find({"symbol": symbol}, {"_id": 0}))
    if not rows:
        return pd.DataFrame()
    return _normalize_historical_df(pd.DataFrame(rows), symbol, compute_greeks=compute_greeks)


def _normalize_historical_df(df: pd.DataFrame, symbol: str,
                             compute_greeks: bool = True) -> pd.DataFrame:
    """Normalize collector CSV / Mongo documents to a common schema.

    If compute_greeks is True and the source lacks iv/delta/theta/vega, they are
    computed on the fly using Black-Scholes.  This keeps old Mongo snapshots
    usable without a full backfill job.
    """
    if df.empty:
        return df
    rename = {
        "option_type": "side",
        "ltp": "mark",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    # Parse timestamp. Collector stores IST string; convert to UTC.
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize("Asia/Kolkata")
        df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")
    else:
        df["timestamp"] = pd.NaT

    # Expiry: string -> date -> UTC 15:30 IST close.
    if "expiry" in df.columns:
        df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce")
        if df["expiry"].dt.tz is None:
            df["expiry"] = df["expiry"].dt.tz_localize("Asia/Kolkata") + pd.Timedelta(hours=15, minutes=30)
            df["expiry"] = df["expiry"].dt.tz_convert("UTC")
    else:
        df["expiry"] = pd.NaT

    # Normalize side to CE/PE.
    df["side"] = df["side"].astype(str).str.upper()
    df.loc[df["side"].str.startswith("C"), "side"] = "CE"
    df.loc[df["side"].str.startswith("P"), "side"] = "PE"

    for col in ["strike", "mark", "spot"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Compute Greeks on the fly if missing.
    greek_cols = ["iv", "delta", "gamma", "theta", "vega", "rho"]
    if compute_greeks and not all(c in df.columns for c in greek_cols):
        from nse.data.greeks_vectorized import add_greeks_to_dataframe
        try:
            add_greeks_to_dataframe(df)
        except Exception as e:
            logger.warning("_normalize_historical_df: Greek computation failed: %s", e)

    df = df.dropna(subset=["timestamp", "expiry", "strike", "mark", "side"])
    df["symbol"] = symbol
    base_cols = ["timestamp", "symbol", "expiry", "strike", "side", "mark", "spot"]
    extra_cols = [c for c in ("bid", "ask", "volume", "oi", "iv", "delta", "gamma", "theta", "vega", "rho", "vix") if c in df.columns]
    return df[base_cols + extra_cols]
