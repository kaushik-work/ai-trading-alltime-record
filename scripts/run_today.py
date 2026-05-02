"""
Daily Market Runner — fully automated, runs the whole trading day.

What it does (in order):
  08:45  Start: refresh token, load instruments
  09:10  Begin 5-min option chain snapshots (NIFTY + BANKNIFTY)
  09:25  Evaluate gap signal for each symbol
  09:30  Log signal + real entry price from live snapshot
  15:35  Stop collection
  15:40  Run real-price backtest on today's data
  15:45  Append to strategy log, print daily report

Run manually:
  python scripts/run_today.py

To automate (Windows Task Scheduler):
  python scripts/run_today.py --setup-task    (run once as admin)

Strategies evaluated daily:
  1. Expiry Day Gap   (NIFTY Tuesday, BANKNIFTY Wednesday)
  2. Monday Next-Exp  (NIFTY Monday, Rs110 target premium)
  3. Any-Day Gap      (fires on significant gap + momentum any day)
"""

import argparse
import csv
import os
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

p = argparse.ArgumentParser()
p.add_argument("--setup-task", action="store_true", help="register Windows Task Scheduler job")
p.add_argument("--strikes",    type=int, default=6,   help="ATM +/- N strikes to snapshot")
p.add_argument("--interval",   type=int, default=5,   help="snapshot interval minutes")
p.add_argument("--dry-run",    action="store_true",   help="skip market hours check (for testing)")
args = p.parse_args()

IST      = ZoneInfo("Asia/Kolkata")
BASE     = Path(__file__).parent.parent
SNAP_DIR = BASE / "db" / "oi_snapshots"
LOG_FILE = BASE / "db" / "strategy_log.csv"
SNAP_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

SYMBOLS = [
    {"name": "NIFTY",     "lot": 65, "step": 50,  "expiry_day": 1},  # Tuesday
    {"name": "BANKNIFTY", "lot": 15, "step": 100, "expiry_day": 2},  # Wednesday
]

# ── Windows Task Scheduler setup ──────────────────────────────────────────────
if args.setup_task:
    script = Path(__file__).resolve()
    python = sys.executable
    cmd = (
        f'schtasks /create /tn "NiftyDailyRunner" /tr "{python} {script}" '
        f'/sc WEEKLY /d MON,TUE,WED,THU,FRI /st 08:45 /f'
    )
    print(f"\nRegistering Windows Task Scheduler job:")
    print(f"  {cmd}\n")
    ret = os.system(cmd)
    if ret == 0:
        print("  Task registered. Will run at 08:45 every weekday.")
        print("  To remove: schtasks /delete /tn NiftyDailyRunner /f")
    else:
        print("  Failed. Try running as Administrator.")
    sys.exit(0)

# ── Helpers ───────────────────────────────────────────────────────────────────
def now_ist() -> datetime:
    return datetime.now(IST)

def ist_time() -> dtime:
    return now_ist().time()

def log(msg: str):
    ts = now_ist().strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}", flush=True)

def is_trading_day() -> bool:
    return date.today().weekday() < 5   # Mon-Fri

# ── Login ─────────────────────────────────────────────────────────────────────
from data.angel_fetcher import AngelFetcher
af = AngelFetcher.get()

log("Logging in to Angel One...")
if not af._ensure_logged_in():
    log("LOGIN FAILED — check .env credentials and try again")
    sys.exit(1)
log("Logged in OK")

instruments = af._nfo_instruments()
log(f"Loaded {len(instruments)} NFO instruments")

# ── Token lookup ──────────────────────────────────────────────────────────────
def _parse_expiry(s: str):
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try: return datetime.strptime(s.strip(), fmt).date()
        except: pass
    return None

def nearest_expiry(symbol: str, from_date: date) -> date:
    expiries = sorted({
        _parse_expiry(i["expiry"])
        for i in instruments
        if i.get("name") == symbol
        and i.get("expiry")
        and _parse_expiry(i["expiry"]) is not None
        and _parse_expiry(i["expiry"]) >= from_date
    })
    return expiries[0] if expiries else from_date + timedelta(days=7)

def get_tokens(symbol: str, expiry: date, atm: int, step: int, n: int) -> list:
    tokens = []
    for k in range(-n, n + 1):
        strike = atm + k * step
        for ot in ("CE", "PE"):
            match = next((
                i for i in instruments
                if i.get("name") == symbol
                and int(float(i.get("strike", 0))) // 100 == strike
                and i.get("instrumenttype") == "OPTIDX"
                and i.get("symbol", "").endswith(ot)
                and _parse_expiry(i.get("expiry", "")) == expiry
            ), None)
            if match:
                tokens.append({
                    "token": match["token"],
                    "symbol": symbol,
                    "strike": strike,
                    "option_type": ot,
                })
    return tokens

def fetch_quotes(token_list: list) -> dict:
    if not token_list: return {}
    try:
        resp = af._api.getMarketData("FULL", {"NFO": [t["token"] for t in token_list]})
        if not resp or not resp.get("status"): return {}
        result = {}
        for row in resp.get("data", {}).get("fetched", []):
            tok = str(row.get("symbolToken", ""))
            result[tok] = {
                "ltp":    float(row.get("ltp", 0) or 0),
                "bid":    float(row.get("bidPrice", 0) or 0),
                "ask":    float(row.get("askPrice", 0) or 0),
                "volume": int(row.get("tradeVolume", 0) or 0),
                "oi":     int(row.get("opnInterest", 0) or 0),
            }
        return result
    except Exception as e:
        log(f"getMarketData error: {e}")
        return {}

# ── Signal logic ──────────────────────────────────────────────────────────────
def gap_signal(gap: float, net15m: float, cfg: dict):
    G = cfg.get("gap_thresh", 50)
    M = cfg.get("mom_thresh", 75)
    R = cfg.get("rev_thresh", 200)
    if gap < -G and net15m > G * 0.6:  return "CE", "GAP_DOWN_FILL"
    if gap < -G and net15m < -G:       return "PE", "GAP_DOWN_CONT"
    if gap > G  and net15m < -R:       return "PE", "GAP_UP_REVERSAL"
    if gap > G  and net15m > G:        return "CE", "GAP_UP_CONT"
    if abs(gap) <= G and net15m < -M:  return "PE", "FLAT_MOM_DOWN"
    if abs(gap) <= G and net15m > M:   return "CE", "FLAT_MOM_UP"
    return None, "NO_SIGNAL"

SYM_CFG = {
    "NIFTY":     {"gap_thresh": 50,  "mom_thresh": 75,  "rev_thresh": 200},
    "BANKNIFTY": {"gap_thresh": 100, "mom_thresh": 150, "rev_thresh": 400},
}

# ── State per symbol ──────────────────────────────────────────────────────────
state = {}
for sym in SYMBOLS:
    state[sym["name"]] = {
        "expiry": None, "tokens": [], "last_atm": None,
        "prev_close": None, "open_p": None, "net15m": None,
        "signal_dir": None, "signal_type": None, "signal_logged": False,
        "snap_file": None, "writer": None, "file_handle": None,
    }

# ── Load previous close for each symbol ──────────────────────────────────────
import pandas as pd
for sym in SYMBOLS:
    name = sym["name"]
    cache = BASE / "backtest_cache" / f"{name}_5m_180d.csv"
    if cache.exists():
        n5 = pd.read_csv(cache, index_col=0, parse_dates=True)
        n5.index   = pd.to_datetime(n5.index, utc=True).tz_localize(None)
        n5.columns = [c.capitalize() for c in n5.columns]
        n5["_date"] = n5.index.date
        daily_close = {dt: float(grp["Close"].iloc[-1]) for dt, grp in n5.groupby("_date")}
        prev_days = sorted(d for d in daily_close if d < date.today())
        if prev_days:
            state[name]["prev_close"] = daily_close[prev_days[-1]]
            log(f"{name} prev close: {state[name]['prev_close']:.0f}")

# ── Open snapshot files ───────────────────────────────────────────────────────
today_str = date.today().isoformat()
for sym in SYMBOLS:
    name = sym["name"]
    fpath = SNAP_DIR / f"{today_str}_{name}.csv"
    fh = open(fpath, "a", newline="", encoding="utf-8")
    writer = csv.writer(fh)
    if fpath.stat().st_size == 0:
        writer.writerow(["timestamp", "symbol", "expiry", "strike", "option_type",
                         "ltp", "bid", "ask", "volume", "oi", "spot"])
    state[name]["snap_file"] = fpath
    state[name]["writer"]    = writer
    state[name]["file_handle"] = fh

# ── Strategy log header ───────────────────────────────────────────────────────
write_log_hdr = not LOG_FILE.exists()
log_fh = open(LOG_FILE, "a", newline="", encoding="utf-8")
log_writer = csv.writer(log_fh)
if write_log_hdr:
    log_writer.writerow([
        "date", "symbol", "weekday", "strategy", "signal_type",
        "direction", "strike", "entry_ltp", "gap", "net15m",
        "is_expiry_day", "expiry"
    ])

# ── Main collection loop ──────────────────────────────────────────────────────
MARKET_OPEN  = dtime(9, 10)
SIGNAL_TIME  = dtime(9, 25)
ENTRY_TIME   = dtime(9, 30)
MARKET_CLOSE = dtime(15, 35)

if not args.dry_run and not is_trading_day():
    log("Today is a weekend. Nothing to do.")
    sys.exit(0)

from core.ipc import is_market_holiday
_is_hol, _hol_label = is_market_holiday(date.today().isoformat())
if not args.dry_run and _is_hol:
    log(f"Today is a market holiday: {_hol_label}. NSE is closed. Exiting.")
    sys.exit(0)

log(f"Starting daily runner for {today_str}")
log(f"Waiting for market open (09:10)...")

try:
    while True:
        now   = now_ist()
        t_now = now.time()

        if not args.dry_run and t_now < MARKET_OPEN:
            time.sleep(30)
            continue

        if t_now > MARKET_CLOSE:
            log("Market closed. Stopping collection.")
            break

        # ── Per-symbol snapshot ───────────────────────────────────────────────
        for sym in SYMBOLS:
            name = sym["name"]
            step = sym["step"]
            st   = state[name]

            # Get live spot
            spot = af.get_index_ltp(name)
            if not spot or spot <= 0:
                continue

            # First bar = opening price
            if st["open_p"] is None:
                st["open_p"] = spot
                log(f"{name} open: {spot:.0f}  (prev_close: {st['prev_close'] or 'unknown'})")

            atm = int(round(spot / step)) * step

            # Expiry on first run
            if st["expiry"] is None:
                st["expiry"] = nearest_expiry(name, date.today())
                log(f"{name} expiry: {st['expiry']}")

            # Refresh tokens if ATM moved
            if st["last_atm"] is None or abs(atm - st["last_atm"]) >= step:
                st["tokens"] = get_tokens(name, st["expiry"], atm, step, args.strikes)
                st["last_atm"] = atm

            # Fetch quotes
            quotes = fetch_quotes(st["tokens"])
            ts_str = now.strftime("%Y-%m-%d %H:%M:%S")
            rows_written = 0
            for tok_info in st["tokens"]:
                q = quotes.get(str(tok_info["token"]), {})
                if not q or q.get("ltp", 0) <= 0:
                    continue
                st["writer"].writerow([
                    ts_str, name, st["expiry"],
                    tok_info["strike"], tok_info["option_type"],
                    q["ltp"], q["bid"], q["ask"],
                    q["volume"], q["oi"], round(spot, 2)
                ])
                rows_written += 1
            if rows_written:
                st["file_handle"].flush()

            # ── 9:25 signal check ─────────────────────────────────────────────
            if t_now >= SIGNAL_TIME and st["net15m"] is None and st["open_p"]:
                st["net15m"] = spot - st["open_p"]
                prev = st["prev_close"] or spot
                gap  = min(max(st["open_p"] - prev, -600), 600)
                st["gap"] = gap
                direction, sig_type = gap_signal(gap, st["net15m"], SYM_CFG[name])
                st["signal_dir"]  = direction
                st["signal_type"] = sig_type
                is_expiry = (date.today().weekday() == sym["expiry_day"])
                log(f"{name} SIGNAL @ 9:25: {sig_type} | dir={direction} "
                    f"| gap={gap:+.0f} | net15m={st['net15m']:+.0f} "
                    f"| {'EXPIRY DAY' if is_expiry else 'non-expiry'}")

            # ── 9:30 entry price ──────────────────────────────────────────────
            if (t_now >= ENTRY_TIME and not st["signal_logged"]
                    and st["signal_dir"] and st["open_p"]):
                strike   = int(round(spot / step)) * step
                direction = st["signal_dir"]
                # Get real LTP from current quote
                match_tok = next((
                    tok for tok in st["tokens"]
                    if tok["strike"] == strike and tok["option_type"] == direction
                ), None)
                entry_ltp = 0.0
                if match_tok:
                    q = quotes.get(str(match_tok["token"]), {})
                    entry_ltp = q.get("ltp", 0.0)

                prev = st["prev_close"] or spot
                gap  = st.get("gap", 0)
                is_expiry = (date.today().weekday() == sym["expiry_day"])
                strategy  = "EXPIRY_GAP" if is_expiry else "ANY_DAY_GAP"

                log_writer.writerow([
                    today_str, name, now.strftime("%A"), strategy,
                    st["signal_type"], direction, strike,
                    round(entry_ltp, 1), round(gap, 0), round(st["net15m"], 0),
                    is_expiry, st["expiry"]
                ])
                log_fh.flush()
                st["signal_logged"] = True

                log(f"{name} ENTRY LOG @ 9:30: {direction} {strike} "
                    f"LTP=Rs{entry_ltp:.0f} | strategy={strategy}")

        # ── Status line every 5 min ───────────────────────────────────────────
        nifty_spot = af.get_index_ltp("NIFTY") or 0
        bn_spot    = af.get_index_ltp("BANKNIFTY") or 0
        log(f"NIFTY={nifty_spot:.0f}  BANKNIFTY={bn_spot:.0f}  "
            f"next snapshot in {args.interval}m")

        time.sleep(args.interval * 60)

except KeyboardInterrupt:
    log("Stopped by user.")

finally:
    for sym in SYMBOLS:
        name = sym["name"]
        fh = state[name].get("file_handle")
        if fh: fh.close()
    log_fh.close()

# ── Post-market: real-price backtest ─────────────────────────────────────────
log("Market closed. Running real-price backtest on today's data...")
import subprocess
result = subprocess.run(
    [sys.executable, str(BASE / "scripts" / "backtest_real_prices.py"),
     "--date", today_str, "--lots", "1"],
    capture_output=True, text=True, cwd=str(BASE)
)
if result.stdout:
    print(result.stdout)
if result.stderr and "Error" in result.stderr:
    print(result.stderr[:500])

# ── Daily report ─────────────────────────────────────────────────────────────
print(f"\n{'='*64}")
print(f"  DAILY REPORT — {today_str}")
print(f"{'='*64}")

if LOG_FILE.exists():
    log_df = pd.read_csv(LOG_FILE)
    today_log = log_df[log_df["date"] == today_str]
    if today_log.empty:
        print(f"  No signals fired today.")
    else:
        for _, row in today_log.iterrows():
            print(f"  {row['symbol']:<12} {row['strategy']:<15} {row['signal_type']:<18}"
                  f" dir={row['direction']}  strike={row['strike']}"
                  f"  entry=Rs{row['entry_ltp']:.0f}"
                  f"  {'EXPIRY' if row['is_expiry_day'] else 'non-expiry'}")

    print(f"\n  All-time strategy log: {LOG_FILE}")
    print(f"  Total signals logged: {len(log_df)}")
    if len(log_df) > 0:
        by_type = log_df.groupby("signal_type").size()
        print(f"\n  Signal frequency (all time):")
        for sig, cnt in by_type.items():
            print(f"    {sig:<22} : {cnt}")

print(f"\n  Snapshot files saved to: {SNAP_DIR}")
for sym in SYMBOLS:
    f = SNAP_DIR / f"{today_str}_{sym['name']}.csv"
    if f.exists():
        rows = sum(1 for _ in open(f)) - 1  # minus header
        print(f"    {f.name}: {rows} rows")

print(f"\n  Tomorrow: run again at 08:45")
print(f"  Or automate: python scripts/run_today.py --setup-task")
print(f"{'='*64}\n")
