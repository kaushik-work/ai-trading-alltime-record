"""
ETH S/R retest with optimized dials from the parameter sweep.

Compared to the live config (SL 0.7%, R:R 1:7), the sweep found that
SL 1.0% with the same R:R and vol filter improves profit/risk on Apr-Jul 2026.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
from backtest_price_action_sweep import load_perp, prepare

BUDGET_INR = 50_000.0
LEVERAGE = 15.0
PERP_FEE_BPS = 5.0
SLIPPAGE_BPS = 2.0
SYMBOL = "ETHUSD"

# Optimized dials
SL_PCT = 0.010
RR = 7.0
TP_PCT = SL_PCT * RR
VOL_FILTER_MAX = 0.34
RETEST_MODE = "wick_touch"
BODY_POS_THRESHOLD = 0.70
WICK_TOUCH_TOL = 0.0007

COOLDOWN_CANDLES = 60
BLOCK_AFTER_LOSS_CANDLES = 180
MAX_HOLD_CANDLES = 240


def load_eth_df():
    dfs = []
    for subdir in ["eth", "july_eth"]:
        dfs.append(load_perp(subdir, SYMBOL))
    df = pd.concat(dfs).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[df.index >= pd.Timestamp("2026-04-01", tz="UTC")]
    return df


def main():
    df = load_eth_df()
    s = prepare(df, use_trend=True, retest_mode=RETEST_MODE,
                body_pos_threshold=BODY_POS_THRESHOLD, wick_touch_tol=WICK_TOUCH_TOL,
                vol_filter_max=VOL_FILTER_MAX)
    o, h, l, c = s["o"], s["h"], s["l"], s["c"]
    ts = df.index
    n = len(df)
    long_sig, short_sig = s["retest_long"], s["retest_short"]

    trades = []
    pos = None
    cooldown = -1
    block_long_until = -1
    block_short_until = -1
    start_i = max(240, 1440) + 10

    for i in range(start_i, n - 1):
        if pos is not None:
            sign = 1 if pos["side"] == "long" else -1
            reason = None
            exit_px = None
            if (sign > 0 and h[i] >= pos["tp"]) or (sign < 0 and l[i] <= pos["tp"]):
                reason = "tp"
                exit_px = pos["tp"] * (1 - sign * SLIPPAGE_BPS / 1e4)
            elif (sign > 0 and l[i] <= pos["sl"]) or (sign < 0 and h[i] >= pos["sl"]):
                reason = "sl"
                exit_px = pos["sl"] * (1 - sign * SLIPPAGE_BPS / 1e4)
            elif i - pos["entry_idx"] >= MAX_HOLD_CANDLES:
                reason = "hold"
                exit_px = c[i] * (1 - sign * SLIPPAGE_BPS / 1e4)

            if reason is not None:
                gross = sign * (exit_px - pos["entry"]) / pos["entry"]
                net = gross - 2 * PERP_FEE_BPS / 1e4
                trades.append({**pos, "exit_px": exit_px, "exit_reason": reason,
                               "net_pnl_pct": net, "exit_time": ts[i]})
                pos = None
                cooldown = i + COOLDOWN_CANDLES
                if net <= 0:
                    if sign > 0:
                        block_long_until = i + BLOCK_AFTER_LOSS_CANDLES
                    else:
                        block_short_until = i + BLOCK_AFTER_LOSS_CANDLES
            continue

        if i < cooldown:
            continue

        next_open = o[i + 1]
        t_entry = ts[i + 1]

        if long_sig[i] and i >= block_long_until:
            entry = next_open * (1 + SLIPPAGE_BPS / 1e4)
            stop_level = l[i] * (1 - SLIPPAGE_BPS / 1e4)
            sl_dist = max(SL_PCT, (entry - stop_level) / entry)
            pos = {
                "side": "long", "entry": entry,
                "sl": entry * (1 - sl_dist),
                "tp": entry * (1 + TP_PCT),
                "entry_idx": i + 1, "entry_time": t_entry,
                "decision_time": ts[i], "sl_dist": sl_dist,
            }
            continue

        if short_sig[i] and i >= block_short_until:
            entry = next_open * (1 - SLIPPAGE_BPS / 1e4)
            stop_level = h[i] * (1 + SLIPPAGE_BPS / 1e4)
            sl_dist = max(SL_PCT, (stop_level - entry) / entry)
            pos = {
                "side": "short", "entry": entry,
                "sl": entry * (1 + sl_dist),
                "tp": entry * (1 - TP_PCT),
                "entry_idx": i + 1, "entry_time": t_entry,
                "decision_time": ts[i], "sl_dist": sl_dist,
            }

    equity = BUDGET_INR
    peak = equity
    gross = 0.0
    wins = 0
    max_dd = 0.0
    for t in trades:
        pnl = BUDGET_INR * LEVERAGE * t["net_pnl_pct"]
        gross += pnl
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if pnl > 0:
            wins += 1

    print("=" * 80)
    print("ETH S/R retest — OPTIMIZED dials")
    print(f"SL {SL_PCT*100:.1f}% | R:R 1:{RR:.0f} | vol≤{VOL_FILTER_MAX*100:.0f}% | {RETEST_MODE}")
    print(f"Fixed ₹{BUDGET_INR:,.0f} | {LEVERAGE:.0f}x | {PERP_FEE_BPS}bps fee + {SLIPPAGE_BPS}bps slippage")
    print("=" * 80)
    print(f"  Trades: {len(trades)}")
    print(f"  Wins: {wins} ({100*wins/len(trades):.1f}%)")
    print(f"  Gross P&L: ₹{gross:,.0f}")
    print(f"  Final equity: ₹{equity:,.0f}")
    print(f"  Return: {100*(equity-BUDGET_INR)/BUDGET_INR:+.1f}%")
    print(f"  MaxDD: ₹{max_dd:,.0f} ({100*max_dd/BUDGET_INR:.1f}% of budget)")
    print(f"  Profit/Risk: {gross/max(max_dd,1):.2f}x")

    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    print(f"  Exit reasons: {reasons}")

    monthly = {}
    for t in trades:
        m = t["entry_time"].strftime("%Y-%m")
        monthly.setdefault(m, []).append(BUDGET_INR * LEVERAGE * t["net_pnl_pct"])
    print("\n  Monthly P&L:")
    for m in sorted(monthly):
        print(f"    {m}: {len(monthly[m]):2d} trades, ₹{sum(monthly[m]):>+10,.0f}")

    print("\n  First/last 5 trades:")
    for t in trades[:5] + trades[-5:]:
        pnl = BUDGET_INR * LEVERAGE * t["net_pnl_pct"]
        print(f"    {t['side']:5} entry {t['entry_time']} → exit {t['exit_time']} "
              f"reason={t['exit_reason']:<8} pnl=₹{pnl:>+8,.0f}")


if __name__ == "__main__":
    main()
