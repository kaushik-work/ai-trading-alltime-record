"""
Phase 6 — Backtest Performance Metrics

Computes all key statistics from a list of closed trades and a daily equity curve.
"""

import math
from typing import List


def compute_metrics(trades: list, equity_curve: list, initial_capital: float) -> dict:
    """
    Args:
        trades        : list of trade dicts (each has 'pnl', 'side', 'exit_reason')
        equity_curve  : list of {'date': str, 'equity': float}
        initial_capital: float

    Returns:
        dict of performance stats
    """
    if not trades:
        return {
            "error": "No trades executed in this period.",
            "total_trades": 0,
        }

    pnls         = [t["pnl"] for t in trades]
    total_charges = round(sum(t.get("charges", 0) for t in trades), 2)
    total_gross   = round(sum(t.get("pnl_gross", t["pnl"]) for t in trades), 2)
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_trades  = len(trades)
    win_trades    = len(wins)
    loss_trades   = len(losses)
    win_rate      = win_trades / total_trades * 100 if total_trades else 0

    avg_win  = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    rr_ratio = abs(avg_win / avg_loss)   if avg_loss != 0 else 0.0

    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss else 0.0

    total_pnl        = sum(pnls)
    total_return_pct = (total_pnl / initial_capital) * 100

    # ── Max Drawdown (on cumulative equity of trades) ──────────────────────────
    equities = [initial_capital] + [t["equity"] for t in trades]
    peak     = equities[0]
    max_dd   = 0.0
    for e in equities:
        if e > peak:
            peak = e
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # ── Sharpe Ratio (annualised, based on daily equity curve) ────────────────
    sharpe = 0.0
    if len(equity_curve) >= 5:
        eq_vals = [row["equity"] for row in equity_curve]
        daily_returns = []
        for j in range(1, len(eq_vals)):
            prev = eq_vals[j - 1]
            if prev > 0:
                daily_returns.append((eq_vals[j] - prev) / prev)
        if daily_returns:
            mean_r = sum(daily_returns) / len(daily_returns)
            var    = sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns)
            std_r  = math.sqrt(var) if var > 0 else 1e-9
            sharpe = round((mean_r / std_r) * math.sqrt(252), 2)

    # ── Streak analysis ────────────────────────────────────────────────────────
    max_win_streak = max_loss_streak = cur_win = cur_loss = 0
    for p in pnls:
        if p > 0:
            cur_win  += 1
            cur_loss  = 0
        else:
            cur_loss += 1
            cur_win   = 0
        max_win_streak  = max(max_win_streak,  cur_win)
        max_loss_streak = max(max_loss_streak, cur_loss)

    # ── Exit breakdown ─────────────────────────────────────────────────────────
    exit_counts = {}
    for t in trades:
        reason = t.get("exit_reason", "?")
        exit_counts[reason] = exit_counts.get(reason, 0) + 1

    # ── Best / worst trade ─────────────────────────────────────────────────────
    best_trade  = max(pnls)
    worst_trade = min(pnls)

    return {
        "total_trades":      total_trades,
        "win_trades":        win_trades,
        "loss_trades":       loss_trades,
        "win_rate":          round(win_rate, 1),
        "avg_win":           round(avg_win, 2),
        "avg_loss":          round(avg_loss, 2),
        "rr_ratio":          round(rr_ratio, 2),
        "profit_factor":     profit_factor,
        "total_pnl":         round(total_pnl, 2),
        "total_pnl_gross":   total_gross,
        "total_charges":     total_charges,
        "total_return_pct":  round(total_return_pct, 2),
        "max_drawdown_pct":  round(max_dd, 2),
        "sharpe_ratio":      sharpe,
        "max_win_streak":    max_win_streak,
        "max_loss_streak":   max_loss_streak,
        "best_trade":        round(best_trade, 2),
        "worst_trade":       round(worst_trade, 2),
        "exit_breakdown":    exit_counts,
    }
