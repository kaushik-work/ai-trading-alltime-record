"""
Compatibility shim — all existing imports of ZerodhaFetcher, _TOKENS, _INTERVAL_MAP
from this module continue to work without changes across the entire codebase.
The underlying implementation is now AngelFetcher (Angel One SmartAPI).
"""
from data.angel_fetcher import AngelFetcher as ZerodhaFetcher, _INTERVAL_MAP  # noqa: F401

# Kept for market.py compat (was Zerodha instrument tokens; now Angel One index tokens)
_TOKENS = {
    "NIFTY":     99926000,
    "BANKNIFTY": 99926009,
}

__all__ = ["ZerodhaFetcher", "_TOKENS", "_INTERVAL_MAP"]
