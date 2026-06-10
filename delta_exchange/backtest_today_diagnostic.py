"""June 10 (today, IST) diagnostic — shows EVERY hourly decision point
so we can see why the strategy fired (or didn't) at each hour.

All times in IST (Asia/Kolkata, UTC+5:30).
"""
from __future__ import annotations
import sys
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
from backtest_user_capital_june import (
    load_data, compute_pred, ENTRY_PCT, PERSIST_HOURS, MIN_STRIKES,
    USD_INR_RATE, START_INR,
)

# IST window for "today"
IST = "Asia/Kolkata"
TODAY_IST_START = pd.Timestamp("2026-06-10 00:00:00", tz=IST)
TODAY_IST_END   = pd.Timestamp("2026-06-11 00:00:00", tz=IST)
# Convert to UTC for matching against perp index (which is in UTC)
TODAY_UTC_START = TODAY_IST_START.tz_convert("UTC")
TODAY_UTC_END   = TODAY_IST_END.tz_convert("UTC")

print("=" * 110)
print(f"  v5.5 hourly diagnostic — JUNE 10 IST (Today)")
print(f"  Window: {TODAY_IST_START.strftime('%Y-%m-%d %H:%M IST')} → {TODAY_IST_END.strftime('%Y-%m-%d %H:%M IST')}")
print(f"  Equiv UTC:  {TODAY_UTC_START.strftime('%Y-%m-%d %H:%M UTC')} → {TODAY_UTC_END.strftime('%Y-%m-%d %H:%M UTC')}")
print(f"  Gate: pred ≥ {ENTRY_PCT*100}%  |  Persist: {PERSIST_HOURS}h same-sign  |  Min strikes: {MIN_STRIKES}")
print("=" * 110)

# ── BTC + ETH spot 5m range today (what the eye sees on the chart) ────────────
def report_intraday_range(asset: str, subdir: str):
    perp, marks, cat = load_data(subdir, f"{asset}USD")
    today_mask = (perp.index >= TODAY_UTC_START) & (perp.index < TODAY_UTC_END)
    today_perp = perp[today_mask]
    if today_perp.empty:
        print(f"\n  {asset}USD — no data in today's window")
        return None, None, None, None
    hi = today_perp.max(); lo = today_perp.min()
    open_px = today_perp.iloc[0]; last_px = today_perp.iloc[-1]
    print(f"\n  {asset}USD intraday (5m bars, all of today IST so far):")
    print(f"    open   ₹/$  {open_px:>10,.2f}   @ {today_perp.index[0].tz_convert(IST).strftime('%H:%M IST')}")
    print(f"    high   ₹/$  {hi:>10,.2f}")
    print(f"    low    ₹/$  {lo:>10,.2f}")
    print(f"    last   ₹/$  {last_px:>10,.2f}   @ {today_perp.index[-1].tz_convert(IST).strftime('%H:%M IST')}")
    print(f"    range %     {(hi-lo)/lo*100:>9.2f}%   |  open→last  {(last_px/open_px-1)*100:>+6.2f}%")
    return perp, marks, cat, today_perp


def hourly_table(asset: str, perp, marks, cat):
    print(f"\n  {asset}USD hourly decision points (IST):")
    print(f"  {'IST':<13} {'spot':>10} {'best pred':>10} {'n':>3} {'expiry IST':<18} {'gate ok':<8} {'persist':<8} {'action':<20}")
    print("  " + "─" * 96)

    sig_history = {}
    fired = []

    # Walk hourly bars only — strategy ticks each hour
    hours_all = perp.index[(perp.index.minute == 0) & (perp.index.second == 0)]
    # Also need history BEFORE today for persistence — include yesterday's hours
    yday_start = TODAY_UTC_START - pd.Timedelta(hours=6)
    hours_with_history = hours_all[hours_all >= yday_start]

    for t in hours_with_history:
        if t not in perp.index:
            continue
        spot = float(perp.loc[t])
        preds = compute_pred(t, spot, cat, marks)
        for p in preds:
            sig_history.setdefault(p["expiry"], []).append((t, p["pred"]))
        # trim history to 6h
        for exp in list(sig_history.keys()):
            sig_history[exp] = [(ti, pi) for ti, pi in sig_history[exp]
                                 if (t - ti).total_seconds() <= 6 * 3600]

        # Only PRINT rows for today IST window
        if t < TODAY_UTC_START or t >= TODAY_UTC_END:
            continue

        ist_t = t.tz_convert(IST).strftime("%m-%d %H:%M")
        if not preds:
            print(f"  {ist_t:<13} {spot:>10,.2f} {'—':>10} {'—':>3} {'—':<18} {'—':<8} {'—':<8} no eligible options")
            continue

        # Best by |pred|
        best = max(preds, key=lambda p: abs(p["pred"]))
        exp_ist = best["expiry"].tz_convert(IST).strftime("%m-%d %H:%M IST")
        gate_ok = abs(best["pred"]) >= ENTRY_PCT
        hist = sig_history.get(best["expiry"], [])
        recent = [pi for ti, pi in hist if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
        same_sign = sum(1 for pi in recent if np.sign(pi) == np.sign(best["pred"]))
        persist_ok = len(recent) >= PERSIST_HOURS and same_sign >= PERSIST_HOURS

        action = "—"
        if gate_ok and persist_ok:
            action = f"FIRE {'LONG' if best['pred']>0 else 'SHORT'}"
            fired.append((ist_t, best))
        elif not gate_ok:
            action = f"skip: |pred|<{ENTRY_PCT*100}%"
        elif not persist_ok:
            action = f"skip: persistence {same_sign}/{PERSIST_HOURS}h"

        print(f"  {ist_t:<13} {spot:>10,.2f} {best['pred']*100:>+9.3f}% {best['n_strikes']:>3} "
              f"{exp_ist:<18} {'YES' if gate_ok else 'no':<8} {'YES' if persist_ok else 'no':<8} {action:<20}")

    print(f"\n  → {len(fired)} entry signals fired today on {asset}USD")
    return fired


for asset, sub in [("BTC", "june_btc"), ("ETH", "june_eth")]:
    perp, marks, cat, _ = report_intraday_range(asset, sub)
    if perp is not None:
        hourly_table(asset, perp, marks, cat)

print("\n" + "=" * 110)
print("  Why the result can look 'flat' even when intraday range is large:")
print("  - Strategy ticks on HOURLY bars (top of hour), not 5m wicks")
print("  - Gate uses options dislocation (Call − Put + Strike) vs spot — NOT raw price")
print("  - When options are well-arb'd, dislocation stays under 0.6% even in choppy ranges")
print("  - When pred fires marginally (~0.6%), trail exit takes ~0.25-0.5% before giveback closes")
print(f"  - Expiry used: weekly (Fri) + month-end only. Today that = Jun 12 17:30 IST (Friday)")
print(f"    Daily Wed Jun 10 / Thu Jun 11 expiries are EXCLUDED by WEEKLY_PLUS_ONLY filter")
print("=" * 110)
