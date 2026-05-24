"""
WebSocket smoke test — runs for 60 s, confirms ticks arrive and bars build.

Run on the droplet during market hours:
    docker compose exec api python scripts/verify_websocket.py

Expected output:
    Subscribed N tokens, X ticks/sec observed, M bars seeded from Mongo,
    spot=23,XXX.YY received, sample strikes have latest_ltp populated.

Fails fast if:
  • Angel One auth missing
  • WebSocket can't connect within 10 s
  • Zero ticks in 30 s of market hours
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: F401

IST = ZoneInfo("Asia/Kolkata")
RUN_SECONDS = 60


def _is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    from datetime import time as t
    return t(9, 15) <= now.time() <= t(15, 30)


def main():
    if not _is_market_hours():
        print("WARN: Not during market hours. Live WS ticks may be sparse or zero.")

    from data.angel_websocket import get_client
    from core.market_state import get_state
    from core.subscription_manager import get_manager

    ws = get_client()
    state = get_state()
    mgr = get_manager(ws, state)

    print(f"[{datetime.now(IST).isoformat()}] Wiring tick callback ...")
    ws.on_tick(state.on_tick)

    print(f"[{datetime.now(IST).isoformat()}] Backfilling from Mongo ...")
    backfill = state.cold_start_from_mongo(today_only=True)
    print(f"  seeded {backfill.get('bars_seeded')} bars across "
          f"{backfill.get('tokens_seeded')} tokens")

    print(f"[{datetime.now(IST).isoformat()}] Starting WebSocket ...")
    ws.start()
    mgr.start()

    # Wait up to 10 s for connection
    for _ in range(20):
        if ws.is_connected():
            print(f"[{datetime.now(IST).isoformat()}] Connected!")
            break
        time.sleep(0.5)
    else:
        print("FAIL: WebSocket did not connect within 10 s")
        ws.stop()
        sys.exit(1)

    # Refresh subscriptions (will resolve ATM tokens once spot arrives)
    print(f"[{datetime.now(IST).isoformat()}] Refreshing subscriptions ...")
    for attempt in range(10):
        time.sleep(2)
        mgr.refresh()
        diag = mgr.diagnostics()
        if diag.get("current_center"):
            print(f"  ATM center={diag['current_center']} after {(attempt+1)*2}s")
            break
    else:
        print("WARN: SubscriptionManager never found ATM (spot didn't arrive in 20 s)")

    print(f"\n[{datetime.now(IST).isoformat()}] Listening for ticks for {RUN_SECONDS} s ...")
    start_ticks = ws.diagnostics()["ticks_received"]
    start_time  = time.time()
    while time.time() - start_time < RUN_SECONDS:
        time.sleep(5)
        d = ws.diagnostics()
        elapsed = time.time() - start_time
        rate = (d["ticks_received"] - start_ticks) / max(1, elapsed)
        print(f"  +{elapsed:5.1f}s  ticks={d['ticks_received']}  "
              f"rate={rate:5.1f}/s  subs={d['active_subs_count']}")

    # Final state
    print(f"\n[{datetime.now(IST).isoformat()}] Final state:")
    ws_diag = ws.diagnostics()
    state_diag = state.diagnostics()
    print(f"  WebSocket:        {ws_diag}")
    print(f"  MarketState:      {state_diag}")
    print(f"  SubscriptionMgr:  {mgr.diagnostics()}")

    # Sample a few strikes
    print(f"\n  Sample option snapshots (first 4):")
    for s in state.all_option_snapshots()[:4]:
        print(f"    {s.get('strike')}{s.get('option_type'):2}  "
              f"ltp={s.get('latest_ltp')}  oi={s.get('latest_oi')}  "
              f"bars={len(s.get('bars', []))}")

    ws.stop()

    if ws_diag["ticks_received"] - start_ticks == 0 and _is_market_hours():
        print("\nFAIL: zero ticks received during market hours")
        sys.exit(1)
    print("\nOK")


if __name__ == "__main__":
    main()
