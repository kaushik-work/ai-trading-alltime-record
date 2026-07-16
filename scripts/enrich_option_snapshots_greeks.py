"""Backfill Black-Scholes Greeks on existing option_snapshots documents.

Angel One does not publish Greeks, so any historical snapshot taken before
--greeks was enabled lacks iv/delta/theta/vega.  This script reads every
option_snapshots document without an `iv` field, computes the Greeks from the
stored spot/strike/expiry/ltp/option_type, and writes them back.

Usage:
    python scripts/enrich_option_snapshots_greeks.py          # enrich all symbols
    python scripts/enrich_option_snapshots_greeks.py --symbol NIFTY
    python scripts/enrich_option_snapshots_greeks.py --dry-run  # count only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pymongo import MongoClient
from pymongo.errors import BulkWriteError

from nse.data.greeks import option_greeks
from nse.config import SYMBOLS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def _env() -> tuple[str, str]:
    from dotenv import dotenv_values
    env = dotenv_values(Path(__file__).parent.parent / ".env")
    url = os.getenv("MONGODB_URL") or env.get("MONGODB_URL")
    db_name = os.getenv("MONGODB_DB_NAME") or env.get("MONGODB_DB_NAME")
    if not url or not db_name:
        raise RuntimeError("MONGODB_URL / MONGODB_DB_NAME not configured")
    return url, db_name


def _parse_ts(ts):
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def _parse_expiry(expiry):
    if isinstance(expiry, datetime):
        return expiry
    # Collector stores expiry as 'YYYY-MM-DD' string; market closes 15:30 IST.
    from zoneinfo import ZoneInfo
    d = datetime.strptime(str(expiry), "%Y-%m-%d").date()
    return datetime.combine(d, time(15, 30)).replace(tzinfo=ZoneInfo("Asia/Kolkata"))


def enrich_symbol(col, symbol: str, dry_run: bool = False) -> int:
    query = {"symbol": symbol, "iv": {"$exists": False}}
    cursor = col.find(query, {"_id": 1, "spot": 1, "strike": 1, "option_type": 1,
                               "expiry": 1, "ltp": 1, "timestamp": 1})
    total = col.count_documents(query)
    if total == 0:
        log.info("%s: nothing to enrich", symbol)
        return 0

    log.info("%s: %d documents to enrich", symbol, total)
    updated = 0
    bulk = []

    pbar = cursor

    for doc in pbar:
        try:
            ts = _parse_ts(doc["timestamp"])
            expiry = _parse_expiry(doc["expiry"])
            g = option_greeks(
                spot=float(doc["spot"]),
                strike=int(doc["strike"]),
                option_type=str(doc["option_type"]).upper(),
                expiry=expiry,
                mark=float(doc["ltp"]),
                timestamp=ts,
            )
            if g.get("iv") is None:
                continue
            updates = {k: g[k] for k in ("iv", "delta", "gamma", "theta", "vega", "rho") if g[k] is not None}
            if updates:
                bulk.append({"update_one": {"filter": {"_id": doc["_id"]}, "update": {"$set": updates}}})
                updated += 1
        except Exception as e:
            log.debug("skip doc %s: %s", doc.get("_id"), e)

        if not dry_run and len(bulk) >= 500:
            try:
                col.bulk_write(bulk, ordered=False)
            except BulkWriteError as bwe:
                log.warning("bulk_write partial failure: %s", bwe.details.get("writeErrors", [])[:3])
            bulk = []

    if not dry_run and bulk:
        try:
            col.bulk_write(bulk, ordered=False)
        except BulkWriteError as bwe:
            log.warning("bulk_write partial failure: %s", bwe.details.get("writeErrors", [])[:3])

    log.info("%s: enriched %d / %d documents", symbol, updated, total)
    return updated


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", choices=SYMBOLS)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    url, db_name = _env()
    client = MongoClient(url, serverSelectionTimeoutMS=10000)
    col = client[db_name]["option_snapshots"]

    symbols = [args.symbol] if args.symbol else list(SYMBOLS)
    total = 0
    for sym in symbols:
        total += enrich_symbol(col, sym, dry_run=args.dry_run)
    log.info("Done. %s documents.", "Would update" if args.dry_run else "Updated")


if __name__ == "__main__":
    main()
