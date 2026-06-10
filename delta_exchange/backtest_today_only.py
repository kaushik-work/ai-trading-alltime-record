"""Filter June 10 trades from the shared-pool, 3x-leverage v5.5 backtest.
Calls run_combined() from backtest_user_capital_june.py and prints only
trades whose entry_t falls within today (UTC)."""
from __future__ import annotations
import sys
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
from backtest_user_capital_june import run_combined, USD_INR_RATE, START_INR, fmt_inr

TODAY = pd.Timestamp("2026-06-10", tz="UTC")
TOMORROW = TODAY + pd.Timedelta(days=1)

start_usd = START_INR / USD_INR_RATE

print("=" * 100)
print(f"  v5.5 LIVE-config Backtest — June 10, 2026 (today, ₹40k shared BTC+ETH pool, 3x leverage)")
print(f"  Filter: entry_t in [{TODAY.date()} 00:00, {TOMORROW.date()} 00:00) UTC")
print("=" * 100)

r = run_combined(start_usd, leverage=3.0)
all_trades = r["trades"]

# Equity at start of today = equity_after of last trade entered before today,
# or start_usd if no prior trade.
prior = [t for t in all_trades if t["entry_t"] < TODAY]
equity_at_midnight = prior[-1]["equity_after"] if prior else start_usd

today_trades = [t for t in all_trades if TODAY <= t["entry_t"] < TOMORROW]

print(f"\n  Equity at midnight UTC: ${equity_at_midnight:.2f}  ({fmt_inr(equity_at_midnight)})")
print(f"  Trades fired today: {len(today_trades)}")
print(f"  Final equity now:   ${r['equity_final']:.2f}  ({fmt_inr(r['equity_final'])})")

if not today_trades:
    print("\n  No trades fired today yet.")
    sys.exit(0)

print()
print(f"  {'entry':<13} {'asset':<5} {'side':<5} {'pred':>7} {'notional':>10} "
      f"{'PnL USD':>9} {'PnL INR':>9} {'equity USD':>11} {'equity INR':>11} {'reason':<12}")
print("  " + "─" * 110)

today_pnl_usd = 0.0
for tr in today_trades:
    today_pnl_usd += tr["pnl_usd"]
    side_str = "LONG" if tr["side"] == 1 else "SHORT"
    print(f"  {tr['entry_t'].strftime('%m-%d %H:%M UTC'):<13} {tr['asset']:<5} {side_str:<5} "
          f"{tr.get('pred', 0)*100:>+6.3f}% "
          f"${tr['notional']:>8.2f} ${tr['pnl_usd']:>+8.2f} "
          f"{fmt_inr(tr['pnl_usd']):>9} "
          f"${tr['equity_after']:>9.2f} {fmt_inr(tr['equity_after']):>11} {tr['exit_reason']:<12}")

today_pnl_inr = today_pnl_usd * USD_INR_RATE
print()
print(f"  Today's net PnL (combined 3x): ${today_pnl_usd:+.2f}  ({'+' if today_pnl_inr>=0 else ''}{today_pnl_inr:,.0f} INR)")
print(f"  Pool change today: {fmt_inr(equity_at_midnight)} → {fmt_inr(r['equity_final'])}  "
      f"({(r['equity_final']/equity_at_midnight - 1)*100:+.2f}%)")
