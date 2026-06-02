"""
XAUTUSD Funding-Carry Mean Reversion — Step D
==============================================
XAUTUSD has no options on Delta India → v5's synth-forward signal can't be
applied. Instead, use the FUNDING RATE itself as the signal.

Hypothesis (from QF101 transcript on R vs Q measures):
  Funding rate is the perp's analog of "cost of carry" (Q − R in BS terms).
  When funding diverges far from its rolling median, the perp price is
  away from its no-arbitrage fair value relative to spot/index. The
  deviation tends to mean-revert as arbs close the gap.

Signal:
  fund_z = (funding_now − rolling_median(7d)) / rolling_std(7d)
  fund_z > +k → SHORT perp (longs are paying too much; expect reversion)
  fund_z < −k → LONG perp  (shorts are paying too much; expect reversion)
  exit when |fund_z| < EXIT_K or time stop

Risk: price moves against the funding signal can dominate the carry capture.
Use tight stops + trail like v5.
"""

import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
from pathlib import Path
import numpy as np
import pandas as pd

from guards import (
    TradeIntent, PortfolioState, pipeline,
    max_concurrent_positions, cooldown_after_consecutive_losses,
    underlying_whitelist,
)

UNDERLYING  = "XAUT"
PERP_SYMBOL = "XAUTUSD"
DATA = Path(__file__).parent / "data" / UNDERLYING.lower()

# ── Signal & execution dials ──────────────────────────────────────────────────
LOOKBACK_HOURS    = 7 * 24    # 7d rolling stats for funding z-score
ENTRY_Z           = 1.2       # |z| ≥ 2.0 to enter
EXIT_Z            = 0.5       # |z| ≤ 0.5 to exit on signal collapse
PERSIST_HOURS     = 1         # signal must hold for this many hours
PERP_FEE_BPS      = 5.0
SLIPPAGE_BPS      = 2.0
STOP_LOSS_PCT     = 0.010     # 1% — tighter than v5 because XAU is less volatile
TRAIL_PEAK_PCT    = 0.004
TRAIL_GIVEBACK    = 0.0020
MAX_HOLD_HOURS    = 48
SIZE_PCT          = 1.0       # 1× equity per trade (XAU vol low → safer to size larger)
MAX_CONCURRENT    = 1


# ── Data plumbing ─────────────────────────────────────────────────────────────
def load_data():
    mark = pd.read_csv(DATA / "perp" / f"{PERP_SYMBOL}_mark_1m.csv")
    mark["timestamp"] = pd.to_datetime(mark["time"], unit="s", utc=True)
    mark = mark.set_index("timestamp")["close"].sort_index()

    fund = pd.read_csv(DATA / "perp" / f"{PERP_SYMBOL}_funding_1h.csv")
    fund["timestamp"] = pd.to_datetime(fund["time"], unit="s", utc=True)
    fund = fund.set_index("timestamp")["close"].sort_index()
    return mark, fund


def compute_signal(fund: pd.Series) -> pd.DataFrame:
    rolling_med = fund.rolling(LOOKBACK_HOURS).median()
    rolling_std = fund.rolling(LOOKBACK_HOURS).std()
    z = (fund - rolling_med) / rolling_std.replace(0, np.nan)
    sig = pd.DataFrame({
        "fund": fund, "median": rolling_med, "std": rolling_std, "z": z
    })
    return sig.dropna()


def run():
    print("Loading XAUTUSD data...")
    mark, fund = load_data()
    print(f"  perp mark 1m: {len(mark):,} bars  ({mark.index[0]} → {mark.index[-1]})")
    print(f"  funding 1h  : {len(fund):,} bars")
    print()

    sig_df = compute_signal(fund)
    print(f"Funding signal stats:")
    print(f"  funding rate mean: {fund.mean()*100:+.4f}% annualized")
    print(f"  funding rate min : {fund.min()*100:+.4f}%   max: {fund.max()*100:+.4f}%")
    print(f"  z-score | mean   : {sig_df['z'].abs().mean():.3f}   "
          f"p95: {sig_df['z'].abs().quantile(0.95):.3f}   "
          f"p99: {sig_df['z'].abs().quantile(0.99):.3f}")
    print()

    equity_usd = 10_000.0
    state = PortfolioState(equity_usd=equity_usd)
    guards = [
        underlying_whitelist({UNDERLYING}),
        max_concurrent_positions(MAX_CONCURRENT),
        cooldown_after_consecutive_losses(3, cooldown_hours=24),
    ]

    open_position = None
    trades = []
    equity_curve = []
    sig_history = []
    rejections = {"no_signal": 0, "no_persist": 0, "guard": 0, "max_conc": 0}

    print("Walking hourly decision points...")
    hours = sig_df.index
    for i, t in enumerate(hours):
        # snap perp price to nearest minute
        if t in mark.index:
            spot = float(mark.loc[t])
        else:
            ix = mark.index.get_indexer([t], method="nearest")[0]
            spot = float(mark.iloc[ix])
        equity_curve.append((t, equity_usd))
        row = sig_df.loc[t]
        z = row["z"]
        sig_history.append((t, z))
        sig_history = sig_history[-PERSIST_HOURS * 2:]

        # ── manage open position ────────────────────────────────────────────
        if open_position is not None:
            held_h = (t - open_position["entry_t"]).total_seconds() / 3600
            side = open_position["side"]
            entry_px = open_position["entry_px"]
            unreal_ret = side * (spot - entry_px) / entry_px
            open_position["peak_ret"] = max(open_position.get("peak_ret", 0.0), unreal_ret)

            exit_now, reason = False, ""
            if abs(z) <= EXIT_Z:
                exit_now, reason = True, "signal_collapse"
            elif held_h >= MAX_HOLD_HOURS:
                exit_now, reason = True, "max_hold"
            elif unreal_ret < -STOP_LOSS_PCT:
                exit_now, reason = True, "stop_loss"
            elif open_position["peak_ret"] >= TRAIL_PEAK_PCT and \
                 (open_position["peak_ret"] - unreal_ret) > TRAIL_GIVEBACK:
                exit_now, reason = True, "trail"

            if exit_now:
                fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
                ret = side * (fill_px - entry_px) / entry_px
                pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
                pnl_usd = open_position["notional"] * pnl_pct
                equity_usd += pnl_usd
                state.equity_usd = equity_usd
                state.last_n_pnls.append(pnl_usd)
                trades.append({**open_position, "exit_t": t, "exit_px": fill_px,
                               "z_exit": z, "ret": ret, "pnl_pct": pnl_pct,
                               "pnl_usd": pnl_usd, "exit_reason": reason,
                               "equity_after": equity_usd})
                open_position = None

        # ── entry consideration ────────────────────────────────────────────
        if open_position is not None: continue
        if abs(z) < ENTRY_Z:
            rejections["no_signal"] += 1; continue
        # persistence check — z must have been above gate for PERSIST_HOURS
        recent_z = [zi for ti, zi in sig_history if (t - ti).total_seconds() <= PERSIST_HOURS * 3600]
        if len(recent_z) < PERSIST_HOURS:
            rejections["no_persist"] += 1; continue
        if sum(1 for zi in recent_z if np.sign(zi) == np.sign(z) and abs(zi) >= ENTRY_Z) < PERSIST_HOURS:
            rejections["no_persist"] += 1; continue

        # signal: positive z → funding too high → SHORT perp (contrarian)
        side = -1 if z > 0 else 1
        intent = TradeIntent(timestamp=t, structure="xau_funding_carry",
                              underlying=UNDERLYING, risk_usd=equity_usd * 0.02,
                              notional_usd=equity_usd, iv_rv_gap_pp=abs(z))
        reason = pipeline(intent, state, guards)
        if reason is not None:
            rejections["guard"] += 1; continue

        fill_px = spot * (1 + side * SLIPPAGE_BPS / 1e4)
        notional = equity_usd * SIZE_PCT
        open_position = {
            "entry_t": t, "entry_px": fill_px, "side": side,
            "z_entry": z, "funding_entry": row["fund"],
            "notional": notional, "peak_ret": 0.0,
        }
        state.open_positions = 1

    # close residual
    if open_position is not None:
        side = open_position["side"]; entry_px = open_position["entry_px"]
        t_end = mark.index[-1]; spot = float(mark.iloc[-1])
        fill_px = spot * (1 - side * SLIPPAGE_BPS / 1e4)
        ret = side * (fill_px - entry_px) / entry_px
        pnl_pct = ret - 2 * PERP_FEE_BPS / 1e4
        pnl_usd = open_position["notional"] * pnl_pct
        equity_usd += pnl_usd
        trades.append({**open_position, "exit_t": t_end, "exit_px": fill_px,
                       "z_exit": 0, "ret": ret, "pnl_pct": pnl_pct,
                       "pnl_usd": pnl_usd, "exit_reason": "data_end",
                       "equity_after": equity_usd})

    if not trades:
        print("No trades produced.")
        print(f"  rejections: {rejections}")
        return

    df = pd.DataFrame(trades)
    df["entry_t"] = pd.to_datetime(df["entry_t"], utc=True)
    df["exit_t"]  = pd.to_datetime(df["exit_t"], utc=True)
    n = len(df); wins = (df["pnl_usd"] > 0).sum()
    avg_win = df.loc[df["pnl_usd"] > 0, "pnl_usd"].mean() if wins else 0
    avg_loss = df.loc[df["pnl_usd"] <= 0, "pnl_usd"].mean() if (n - wins) else 0
    rr = abs(avg_win / avg_loss) if avg_loss else float("nan")
    eq = pd.Series([e for _, e in equity_curve], index=[t for t, _ in equity_curve])
    daily = eq.resample("1D").last().dropna()
    daily_ret = daily.pct_change().dropna()
    sharpe = daily_ret.mean() / daily_ret.std() * math.sqrt(365) if daily_ret.std() > 0 else 0
    dd = (eq - eq.cummax()).min()

    print()
    print("=" * 88)
    print("  XAUTUSD FUNDING-CARRY MEAN REVERSION (D)")
    print(f"  Entry z: ±{ENTRY_Z}  exit z: ±{EXIT_Z}  persist≥{PERSIST_HOURS}h  "
          f"stop {STOP_LOSS_PCT*100:.1f}%  trail {TRAIL_GIVEBACK*100:.2f}%")
    print("=" * 88)
    print(f"  trades   : {n}     wins {wins}   win rate {wins/n*100:.1f}%   R:R {rr:.2f}")
    print(f"  avg win  : ${avg_win:+,.0f}   avg loss ${avg_loss:+,.0f}")
    print(f"  total PnL: ${df['pnl_usd'].sum():+,.0f}   equity ${equity_usd:,.0f}   "
          f"({(equity_usd-10_000)/10_000*100:+.1f}% on $10k)")
    print(f"  Sharpe   : {sharpe:.2f}     max DD ${dd:+,.0f}  ({dd/10_000*100:.1f}%)")
    print(f"  rejections: {rejections}")
    print()

    # exit reason summary
    print("  Exits by reason:")
    print(df.groupby("exit_reason")["pnl_usd"].agg(["count", "sum", "mean"]).to_string())
    print()

    print("  Last 10 trades:")
    cols = ["entry_t", "side", "z_entry", "z_exit", "entry_px", "exit_px",
            "pnl_usd", "exit_reason"]
    print(df[cols].tail(10).to_string(index=False))
    print()

    out = DATA / "xau_funding_trades.csv"
    df.to_csv(out, index=False)
    print(f"  trade log → {out.relative_to(DATA.parent.parent)}")


if __name__ == "__main__":
    run()
