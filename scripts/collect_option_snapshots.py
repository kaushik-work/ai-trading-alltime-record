"""
Option Chain Snapshot Collector — production-hardened.

Three things that make this not fail silently:
  1. Fresh login every morning — token is never stale
  2. Auto re-login on AG8001 auth errors during the day
  3. Daily summary written to db/collector_summary.csv — eyeball each evening

Output: db/oi_snapshots/YYYY-MM-DD_SYMBOL.csv
Summary: db/collector_summary.csv  (one row per day, append-only)
Log:     logs/YYYY-MM-DD/collector_SYMBOL.log

Usage:
  python scripts/collect_option_snapshots.py
  python scripts/collect_option_snapshots.py --symbol BANKNIFTY
  python scripts/collect_option_snapshots.py --interval 1
  python scripts/collect_option_snapshots.py --dry-run   (skip market hours check)
"""

import argparse
import csv
import logging
import os
import platform
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

p = argparse.ArgumentParser()
p.add_argument("--symbol",   default="NIFTY",     choices=["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"])
p.add_argument("--interval", type=int, default=5, help="snapshot interval minutes")
p.add_argument("--strikes",  type=int, default=8, help="ATM +/- N strikes (default 8 => 17 strikes x 2 sides = 34 contracts per bar)")
p.add_argument("--expiries", type=int, default=1, help="number of nearest weekly expiries to collect (default 1)")
# Greeks, VIX and futures default ON. They were opt-in, and the archive we
# actually accumulated has none of them — an analysis is only as good as the
# field nobody remembered to enable. Storage is cheap; a re-collected year is
# not obtainable at any price.
p.add_argument("--no-greeks", dest="greeks", action="store_false",
               help="skip Black-Scholes Greeks + IV")
p.add_argument("--no-vix",    dest="vix",    action="store_false",
               help="skip India VIX")
p.add_argument("--no-futures", dest="futures", action="store_false",
               help="skip near-month futures quote")
# Accepted and ignored: these were store_true flags before these fields became
# default-on. docker-compose.yml passes "--greeks --vix" to every collector
# service, and argparse exit(2)s on an unrecognised flag — which the compose
# command swallows into `|| sleep 60`, so the collector would crash-loop
# silently for a whole session. Deleting a flag someone else's config still
# passes is a breaking change; keep them as no-ops.
p.add_argument("--greeks", dest="greeks", action="store_true",
               help=argparse.SUPPRESS)
p.add_argument("--vix", dest="vix", action="store_true",
               help=argparse.SUPPRESS)
p.add_argument("--futures", dest="futures", action="store_true",
               help=argparse.SUPPRESS)
p.set_defaults(greeks=True, vix=True, futures=True)
p.add_argument("--dry-run",  action="store_true", help="skip market hours check")
args = p.parse_args()

# ── OS gate ──────────────────────────────────────────────────────────────────
# This collector is the canonical option-chain snapshot writer and lives in
# the droplet's `collector` container. Running it on a Windows / macOS laptop
# (e.g. via Task Scheduler or a cron) AT THE SAME TIME as the droplet causes
# duplicate Mongo writes and competing Angel One API calls. Refuse to run
# anywhere except Linux unless explicitly overridden.
if platform.system() != "Linux" and os.environ.get("ENABLE_LOCAL_SCHEDULERS") != "1":
    print(
        f"collect_option_snapshots: REFUSING to run on {platform.system()}. "
        f"This script is intended for the cloud droplet only.\n"
        f"If you really need to run it locally for debugging, set "
        f"ENABLE_LOCAL_SCHEDULERS=1 and prefer TRADING_MODE=paper.\n"
        f"Otherwise: disable the Windows Task Scheduler entry that launched this."
    )
    sys.exit(0)

IST          = ZoneInfo("Asia/Kolkata")
BASE         = Path(__file__).parent.parent
SNAP_DIR     = BASE / "db" / "oi_snapshots"
LOG_DIR      = BASE / "logs"
SUMMARY_FILE = BASE / "db" / "collector_summary.csv"
SNAP_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

SYMBOL       = args.symbol
STEP         = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "SENSEX": 100}[SYMBOL]
EXCHANGE     = "BFO" if SYMBOL == "SENSEX" else "NFO"
INTERVAL_SEC = args.interval * 60
# NSE extended derivatives close to 15:40 on 2026-08-03; BSE stayed at 15:30.
# We collect 5 min past the bell so the closing prints land in the file.
EXCHANGE_CLOSE = dtime(15, 30) if EXCHANGE == "BFO" else dtime(15, 40)
MARKET_OPEN  = dtime(9, 10)
MARKET_CLOSE = dtime(15, 35) if EXCHANGE == "BFO" else dtime(15, 45)
today_str    = date.today().isoformat()

# ── Logging to disk + stdout ──────────────────────────────────────────────────
log_file = LOG_DIR / today_str / f"collector_{SYMBOL}.log"
log_file.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("collector")

# ── Holiday + weekend check ───────────────────────────────────────────────────
from core.ipc import is_market_holiday

if not args.dry_run:
    if date.today().weekday() >= 5:
        log.info("Weekend — exiting."); sys.exit(0)
    is_hol, hol_label = is_market_holiday(today_str)
    if is_hol:
        log.info("Market holiday: %s — exiting.", hol_label); sys.exit(0)

# ── Fresh login ───────────────────────────────────────────────────────────────
from data.angel_fetcher import AngelFetcher

def fresh_login(max_retries: int = 5) -> AngelFetcher:
    """Always creates a brand-new session. Retries with backoff."""
    af = AngelFetcher.get()
    af._api        = None   # force re-auth
    af._login_date = None
    for attempt in range(1, max_retries + 1):
        try:
            if not af._ensure_logged_in():
                raise RuntimeError("_ensure_logged_in returned False")
            ltp = af.get_index_ltp(SYMBOL)
            if not ltp:
                raise RuntimeError(f"{SYMBOL} LTP probe returned None")
            log.info("Login OK (attempt %d) | %s LTP=%.0f", attempt, SYMBOL, ltp)
            return af
        except Exception as e:
            log.warning("Login attempt %d failed: %s", attempt, e)
            time.sleep(5 * attempt)
    raise RuntimeError(f"Login failed after {max_retries} attempts")


def session_alive(af: AngelFetcher) -> bool:
    try:
        return bool(af.get_index_ltp(SYMBOL))
    except Exception:
        return False

# ── Instrument helpers ────────────────────────────────────────────────────────
def _parse_expiry(s: str):
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def _instruments(af: AngelFetcher) -> list:
    return af._bfo_instruments() if SYMBOL == "SENSEX" else af._nfo_instruments()


def nearest_expiry(af: AngelFetcher) -> date:
    return near_expiries(af, 1)[0]


def near_expiries(af: AngelFetcher, n: int = 1) -> list[date]:
    expiries = sorted({
        _parse_expiry(i["expiry"])
        for i in _instruments(af)
        if i.get("name") == SYMBOL
        and i.get("expiry")
        and _parse_expiry(i["expiry"]) is not None
        and _parse_expiry(i["expiry"]) >= date.today()
    })
    if not expiries:
        return [date.today() + timedelta(days=7)]
    return expiries[:n]


def build_tokens(af: AngelFetcher, expiry: date, atm: int) -> list:
    tokens = []
    for k in range(-args.strikes, args.strikes + 1):
        strike = atm + k * STEP
        for ot in ("CE", "PE"):
            m = next((
                i for i in _instruments(af)
                if i.get("name") == SYMBOL
                and int(float(i.get("strike", 0))) // 100 == strike
                and i.get("instrumenttype") == "OPTIDX"
                and i.get("symbol", "").endswith(ot)
                and _parse_expiry(i.get("expiry", "")) == expiry
            ), None)
            if m:
                tokens.append({"token": m["token"], "strike": strike, "option_type": ot})
    return tokens


# Angel caps FULL mode at 50 tokens per request. A 17-strike chain is 34
# contracts and fits, but --strikes 16 or --expiries 2 does not, and an
# over-long request returns a partial fetch rather than an error. Batch always.
FULL_MODE_MAX_TOKENS = 50

# One list drives both the CSV header and every row, so they cannot drift.
# A header/row mismatch silently shifts every column and is invisible until
# something downstream reads volume as OI.
MARKET_FIELDS = [
    "ltp", "bid", "ask", "mid", "spread", "spread_pct",
    "bid_qty", "ask_qty", "book_imbalance",
    "open", "high", "low", "close", "avg_price",
    "volume", "oi", "last_trade_qty",
    "net_change", "pct_change", "lower_circuit", "upper_circuit",
    "tot_buy_qty", "tot_sell_qty",
    "exch_feed_time", "exch_trade_time",
]
# Depth is 5 levels x 3 numbers x 2 sides. Flattened for CSV, nested for Mongo.
DEPTH_FIELDS = [f"{side}{i}_{k}"
                for side in ("b", "a") for i in range(1, 6)
                for k in ("px", "qty", "ord")]


_FUT_TOKEN: dict = {}


def futures_token(af: AngelFetcher) -> dict | None:
    """Near-month index futures. Cached — the master is large and static.

    WHY THIS IS COLLECTED
        The delta-hedged straddle test had to hedge with the SPOT INDEX, which
        cannot be traded. Futures are the real hedging instrument, and they
        carry a basis that spot does not: F = S*e^((r-q)T). Without the futures
        mark, every hedge P&L we compute is off by the basis and its drift,
        and no put-call-parity or synthetic-forward work is possible at all.
    """
    if SYMBOL in _FUT_TOKEN:
        return _FUT_TOKEN[SYMBOL]
    futs = [i for i in _instruments(af)
            if i.get("name") == SYMBOL and i.get("instrumenttype") == "FUTIDX"
            and _parse_expiry(i.get("expiry", ""))]
    if not futs:
        _FUT_TOKEN[SYMBOL] = None
        return None
    nearest = min(futs, key=lambda i: _parse_expiry(i["expiry"]))
    _FUT_TOKEN[SYMBOL] = {"token": str(nearest["token"]),
                          "symbol": nearest.get("symbol"),
                          "expiry": str(_parse_expiry(nearest["expiry"]))}
    return _FUT_TOKEN[SYMBOL]


def _fetch_full(af: AngelFetcher, tokens: list[str]) -> dict:
    """FULL quotes for any number of tokens, batched under Angel's cap."""
    quotes: dict[str, dict] = {}
    for i in range(0, len(tokens), FULL_MODE_MAX_TOKENS):
        batch = tokens[i:i + FULL_MODE_MAX_TOKENS]
        resp = af._api.getMarketData("FULL", {EXCHANGE: batch})
        if not resp or not resp.get("status"):
            raise RuntimeError(f"getMarketData failed: {resp}")
        for r in resp.get("data", {}).get("fetched", []) or []:
            quotes[str(r.get("symbolToken"))] = r
    return quotes


def take_snapshot(af: AngelFetcher, token_map: list, expiry: date, out_file: Path) -> int:
    spot = af.get_index_ltp(SYMBOL)
    if not spot:
        raise RuntimeError("Spot is None")
    fut = futures_token(af) if args.futures else None
    tokens = [t["token"] for t in token_map] + ([fut["token"]] if fut else [])
    quotes = _fetch_full(af, tokens)

    # Futures mark + basis, identical for every contract in this snapshot.
    fut_ltp, fut_bid, fut_ask, basis = 0.0, 0.0, 0.0, 0.0
    if fut and fut["token"] in quotes:
        from data.angel_fetcher import _quote_fields as _qf
        fq = _qf(quotes[fut["token"]])
        fut_ltp, fut_bid, fut_ask = fq["ltp"], fq["bid"], fq["ask"]
        basis = fut_ltp - spot if fut_ltp > 0 else 0.0
    ts = datetime.now(IST)
    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
    rows = 0
    mongo_docs = []
    vix_value = None
    if args.vix:
        try:
            vix_value = af.fetch_vix()
        except Exception as e:
            log.debug("VIX fetch failed (non-fatal): %s", e)

    # Prepare a single timestamp object for Greek calculations.
    greek_ts = ts.replace(tzinfo=IST)

    with open(out_file, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        from data.angel_fetcher import _quote_fields
        for t in token_map:
            q = quotes.get(str(t["token"]))
            if q is None:
                continue                       # contract not in the response
            f_ = _quote_fields(q)
            ltp = f_["ltp"]

            # A contract with no LTP has usually not traded yet — but it can
            # still be QUOTED, and a live two-sided book is exactly what we
            # need to measure spread at the open. Dropping these rows was
            # discarding the quietest contracts, which is a biased sample.
            # Keep them and flag instead.
            if ltp <= 0 and f_["bid"] <= 0 and f_["ask"] <= 0:
                continue                       # genuinely nothing to record
            no_trade = ltp <= 0

            csv_row = [ts_str, SYMBOL, str(expiry), t["strike"],
                       t["option_type"], round(spot, 2), int(no_trade),
                       fut_ltp, fut_bid, fut_ask, round(basis, 2)]
            csv_row += [f_[k] for k in MARKET_FIELDS]
            csv_row += [lvl[k] for side in ("depth_buy", "depth_sell")
                        for lvl in f_[side] for k in ("price", "qty", "orders")]

            doc = {
                "timestamp":    ts_str,
                "date":         today_str,
                "symbol":       SYMBOL,
                "expiry":       str(expiry),
                "strike":       t["strike"],
                "option_type":  t["option_type"],
                "spot":         round(spot, 2),
                "no_trade":     no_trade,
                "fut_ltp":      fut_ltp,
                "fut_bid":      fut_bid,
                "fut_ask":      fut_ask,
                "basis":        round(basis, 2),
                "fut_expiry":   (fut or {}).get("expiry"),
                **f_,                          # nested depth kept as-is in Mongo
            }

            if args.greeks:
                from nse.data.greeks import option_greeks
                g = option_greeks(
                    spot=spot,
                    strike=t["strike"],
                    option_type=t["option_type"],
                    expiry=datetime.combine(expiry, EXCHANGE_CLOSE).replace(tzinfo=IST),
                    mark=ltp,
                    timestamp=greek_ts,
                )
                for k in ("iv", "delta", "gamma", "theta", "vega", "rho"):
                    v = g.get(k)
                    csv_row.append(round(v, 6) if v is not None else "")
                    doc[k] = v

            if vix_value is not None:
                doc["vix"] = round(vix_value, 2)
                csv_row.append(round(vix_value, 2))

            w.writerow(csv_row)
            mongo_docs.append(doc)
            rows += 1
    # Mirror snapshot batch to Mongo (fire-and-forget — never blocks CSV write)
    if mongo_docs:
        try:
            from core import mongo as _mongo
            _mongo.mirror_option_snapshot(mongo_docs)
        except Exception as e:
            log.debug("mongo mirror failed (non-fatal): %s", e)
    return rows


def write_summary(status: str, snaps: int, rows: int, errors: int, relogins: int):
    """Append one row to db/collector_summary.csv so you can eyeball it each evening."""
    write_hdr = not SUMMARY_FILE.exists()
    with open(SUMMARY_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_hdr:
            w.writerow(["date", "symbol", "status", "snapshots",
                        "rows_written", "errors", "relogins", "log_file"])
        w.writerow([today_str, SYMBOL, status, snaps, rows, errors, relogins, str(log_file)])

# ── Main ──────────────────────────────────────────────────────────────────────
log.info("=" * 60)
log.info("Collector | %s | %s | every %d min", SYMBOL, today_str, args.interval)

try:
    af = fresh_login()
except RuntimeError as e:
    log.critical("Cannot login: %s — aborting.", e)
    write_summary("LOGIN_FAILED", 0, 0, 1, 0)
    sys.exit(1)

expiries        = []
token_maps      = {}
last_atm        = None
last_health     = time.time()
total_rows      = 0
snaps_taken     = 0
error_count     = 0
relogin_count   = 0

out_file = SNAP_DIR / f"{today_str}_{SYMBOL}.csv"
if not out_file.exists():
    with open(out_file, "w", newline="", encoding="utf-8") as f:
        hdr = ["timestamp", "symbol", "expiry", "strike", "option_type",
               "spot", "no_trade", "fut_ltp", "fut_bid", "fut_ask",
               "basis"] + MARKET_FIELDS + DEPTH_FIELDS
        if args.greeks:
            hdr += ["iv", "delta", "gamma", "theta", "vega", "rho"]
        if args.vix:
            hdr += ["vix"]
        csv.writer(f).writerow(hdr)

log.info("Waiting for market open (09:10)...")

try:
    while True:
        now   = datetime.now(IST)
        t_now = now.time()

        if not args.dry_run and t_now < MARKET_OPEN:
            time.sleep(30)
            continue
        if t_now > MARKET_CLOSE:
            log.info("%s reached — collection complete.", MARKET_CLOSE.strftime("%H:%M"))
            break

        # Health check every 10 min
        if time.time() - last_health > 600:
            if not session_alive(af):
                log.warning("Session dead (health check) — re-logging in.")
                try:
                    af = fresh_login()
                    token_maps = {}; last_atm = None
                    relogin_count += 1
                except RuntimeError as e:
                    log.error("Re-login failed: %s", e)
                    error_count += 1
                    time.sleep(60)
                    continue
            last_health = time.time()

        # Refresh tokens when ATM moves
        try:
            spot = af.get_index_ltp(SYMBOL)
            if not spot:
                raise RuntimeError("spot None")
        except Exception as e:
            log.warning("Spot fetch failed: %s", e); error_count += 1
            time.sleep(30); continue

        atm = int(round(spot / STEP)) * STEP
        if not expiries:
            expiries = near_expiries(af, args.expiries)
            log.info("Expiries: %s", expiries)
        if last_atm is None or abs(atm - last_atm) >= STEP:
            token_maps = {exp: build_tokens(af, exp, atm) for exp in expiries}
            total_tokens = sum(len(v) for v in token_maps.values())
            last_atm  = atm
            log.info("Tokens rebuilt: %d instruments (ATM=%d)", total_tokens, atm)

        # Snapshot each expiry
        for exp in expiries:
            tm = token_maps.get(exp, [])
            if not tm:
                continue
            try:
                rows = take_snapshot(af, tm, exp, out_file)
                total_rows  += rows
                snaps_taken += 1
                log.info("Snap #%d | exp=%s | spot=%.0f | %d rows | total=%d",
                         snaps_taken, exp, spot, rows, total_rows)
            except Exception as e:
                err_str = str(e)
                log.error("Snapshot error: %s", err_str)
                error_count += 1
                if any(k in err_str for k in ("AG8001", "Invalid Token", "401")):
                    log.warning("Auth error — re-logging in.")
                    try:
                        af = fresh_login()
                        token_maps = {}; last_atm = None
                        relogin_count += 1
                    except RuntimeError as re_e:
                        log.error("Re-login failed: %s", re_e)

        # Sleep until next interval
        next_tick  = now + timedelta(seconds=INTERVAL_SEC)
        sleep_secs = (next_tick - datetime.now(IST)).total_seconds()
        if sleep_secs > 0:
            time.sleep(sleep_secs)

except KeyboardInterrupt:
    log.info("Stopped by Ctrl+C.")

# ── Summary ───────────────────────────────────────────────────────────────────
status = "OK" if snaps_taken >= 50 else ("PARTIAL" if snaps_taken > 0 else "FAILED")
write_summary(status, snaps_taken, total_rows, error_count, relogin_count)

log.info("=" * 60)
log.info("SUMMARY | status=%-8s snaps=%d rows=%d errors=%d relogins=%d",
         status, snaps_taken, total_rows, error_count, relogin_count)
log.info("Summary appended to: %s", SUMMARY_FILE)
log.info("=" * 60)
