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
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

p = argparse.ArgumentParser()
p.add_argument("--symbol",   default="NIFTY",     choices=["NIFTY", "BANKNIFTY"])
p.add_argument("--interval", type=int, default=5, help="snapshot interval minutes")
p.add_argument("--strikes",  type=int, default=8, help="ATM +/- N strikes")
p.add_argument("--dry-run",  action="store_true", help="skip market hours check")
args = p.parse_args()

IST          = ZoneInfo("Asia/Kolkata")
BASE         = Path(__file__).parent.parent
SNAP_DIR     = BASE / "db" / "oi_snapshots"
LOG_DIR      = BASE / "logs"
SUMMARY_FILE = BASE / "db" / "collector_summary.csv"
SNAP_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

SYMBOL       = args.symbol
STEP         = 50 if SYMBOL == "NIFTY" else 100
INTERVAL_SEC = args.interval * 60
MARKET_OPEN  = dtime(9, 10)
MARKET_CLOSE = dtime(15, 35)
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


def nearest_expiry(af: AngelFetcher) -> date:
    expiries = sorted({
        _parse_expiry(i["expiry"])
        for i in af._nfo_instruments()
        if i.get("name") == SYMBOL
        and i.get("expiry")
        and _parse_expiry(i["expiry"]) is not None
        and _parse_expiry(i["expiry"]) >= date.today()
    })
    return expiries[0] if expiries else date.today() + timedelta(days=7)


def build_tokens(af: AngelFetcher, expiry: date, atm: int) -> list:
    tokens = []
    for k in range(-args.strikes, args.strikes + 1):
        strike = atm + k * STEP
        for ot in ("CE", "PE"):
            m = next((
                i for i in af._nfo_instruments()
                if i.get("name") == SYMBOL
                and int(float(i.get("strike", 0))) // 100 == strike
                and i.get("instrumenttype") == "OPTIDX"
                and i.get("symbol", "").endswith(ot)
                and _parse_expiry(i.get("expiry", "")) == expiry
            ), None)
            if m:
                tokens.append({"token": m["token"], "strike": strike, "option_type": ot})
    return tokens


def take_snapshot(af: AngelFetcher, token_map: list, expiry: date, out_file: Path) -> int:
    spot = af.get_index_ltp(SYMBOL)
    if not spot:
        raise RuntimeError("Spot is None")
    resp = af._api.getMarketData("FULL", {"NFO": [t["token"] for t in token_map]})
    if not resp or not resp.get("status"):
        raise RuntimeError(f"getMarketData failed: {resp}")
    quotes = {
        str(r["symbolToken"]): r
        for r in resp.get("data", {}).get("fetched", [])
    }
    ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    rows = 0
    with open(out_file, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for t in token_map:
            q = quotes.get(str(t["token"]), {})
            ltp = float(q.get("ltp", 0) or 0)
            if ltp <= 0:
                continue
            w.writerow([
                ts, SYMBOL, expiry, t["strike"], t["option_type"],
                ltp,
                float(q.get("bidPrice",    0) or 0),
                float(q.get("askPrice",    0) or 0),
                int(  q.get("tradeVolume", 0) or 0),
                int(  q.get("opnInterest", 0) or 0),
                round(spot, 2),
            ])
            rows += 1
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

expiry          = None
token_map       = []
last_atm        = None
last_health     = time.time()
total_rows      = 0
snaps_taken     = 0
error_count     = 0
relogin_count   = 0

out_file = SNAP_DIR / f"{today_str}_{SYMBOL}.csv"
if not out_file.exists():
    with open(out_file, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            "timestamp", "symbol", "expiry", "strike", "option_type",
            "ltp", "bid", "ask", "volume", "oi", "spot",
        ])

log.info("Waiting for market open (09:10)...")

try:
    while True:
        now   = datetime.now(IST)
        t_now = now.time()

        if not args.dry_run and t_now < MARKET_OPEN:
            time.sleep(30)
            continue
        if t_now > MARKET_CLOSE:
            log.info("15:35 reached — collection complete.")
            break

        # Health check every 10 min
        if time.time() - last_health > 600:
            if not session_alive(af):
                log.warning("Session dead (health check) — re-logging in.")
                try:
                    af = fresh_login()
                    token_map = []; last_atm = None
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
        if expiry is None:
            expiry = nearest_expiry(af)
            log.info("Expiry: %s", expiry)
        if last_atm is None or abs(atm - last_atm) >= STEP:
            token_map = build_tokens(af, expiry, atm)
            last_atm  = atm
            log.info("Tokens rebuilt: %d instruments (ATM=%d)", len(token_map), atm)

        # Snapshot
        try:
            rows = take_snapshot(af, token_map, expiry, out_file)
            total_rows  += rows
            snaps_taken += 1
            log.info("Snap #%d | spot=%.0f | %d rows | total=%d",
                     snaps_taken, spot, rows, total_rows)
        except Exception as e:
            err_str = str(e)
            log.error("Snapshot error: %s", err_str)
            error_count += 1
            if any(k in err_str for k in ("AG8001", "Invalid Token", "401")):
                log.warning("Auth error — re-logging in.")
                try:
                    af = fresh_login()
                    token_map = []; last_atm = None
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
