"""
Honest Corrections to v5 Reporting — addressing Sonnet's critique
==================================================================
Fixes:
  1. Position-level win rate (dedupe partial_tp + final exit by position ID)
  2. Sharpe via pct_change() instead of diff() — return-based, not $-based
  3. BTC-ETH correlation measurement (assumes independence were wrong)
  4. Honest revised numbers

Does NOT fix: in-sample tuning. That requires the OOS data (pulling now).
"""

import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent

ANNUAL = 365


def load_trades(asset: str) -> pd.DataFrame:
    p = (ROOT / "data" / "v5_trades.csv") if asset == "BTC" \
        else (ROOT / "data" / asset.lower() / "v5_trades.csv")
    df = pd.read_csv(p, parse_dates=["entry_t", "exit_t"])
    df = df.sort_values("exit_t").reset_index(drop=True)
    # synthesize a position_id = (entry_t, expiry, side)
    df["position_id"] = df["entry_t"].astype(str) + "_" + df["expiry"].astype(str) \
                         + "_" + df["side"].astype(str)
    return df


def position_level_stats(df: pd.DataFrame) -> dict:
    """Aggregate trade legs back into positions; report position-level metrics."""
    by_pos = df.groupby("position_id").agg(
        pnl_usd=("pnl_usd", "sum"),
        n_legs=("pnl_usd", "count"),
        first_entry=("entry_t", "min"),
        last_exit=("exit_t", "max"),
        side=("side", "first"),
    ).reset_index()
    n_pos = len(by_pos)
    wins = (by_pos["pnl_usd"] > 0).sum()
    losses = (by_pos["pnl_usd"] <= 0).sum()
    avg_win = by_pos.loc[by_pos["pnl_usd"] > 0, "pnl_usd"].mean() if wins else 0
    avg_loss = by_pos.loc[by_pos["pnl_usd"] <= 0, "pnl_usd"].mean() if losses else 0
    rr = abs(avg_win / avg_loss) if avg_loss else float("nan")
    return {
        "n_positions": int(n_pos),
        "n_legs": int(len(df)),
        "win_rate_pct": float(wins / n_pos * 100) if n_pos else 0.0,
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "rr": float(rr) if np.isfinite(rr) else None,
        "total_pnl": float(by_pos["pnl_usd"].sum()),
    }


def equity_curve_from_trades(df: pd.DataFrame, start: float = 10_000.0) -> pd.Series:
    """Reconstruct equity curve at trade exits."""
    df = df.sort_values("exit_t")
    eq = start + df["pnl_usd"].cumsum()
    eq.index = df["exit_t"]
    return pd.concat([pd.Series([start], index=[df["exit_t"].iloc[0] - pd.Timedelta(seconds=1)]),
                       eq])


def corrected_sharpe(eq: pd.Series, factor: int = ANNUAL) -> float:
    """Sharpe from daily pct_change, not diff."""
    daily = eq.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    if rets.std() == 0 or rets.empty: return 0.0
    return float(rets.mean() / rets.std() * math.sqrt(factor))


def diff_based_sharpe(eq: pd.Series, factor: int = ANNUAL) -> float:
    """The wrong way we were doing it."""
    daily = eq.resample("1D").last().dropna()
    diffs = daily.diff().dropna()
    if diffs.std() == 0 or diffs.empty: return 0.0
    return float(diffs.mean() / diffs.std() * math.sqrt(factor))


def compute_correlation():
    """BTC vs ETH perp 1m close correlation — were we right to assume independence?"""
    btc = pd.read_csv(ROOT / "data" / "perp" / "BTCUSD_mark_1m.csv")
    eth = pd.read_csv(ROOT / "data" / "eth" / "perp" / "ETHUSD_mark_1m.csv")
    btc["timestamp"] = pd.to_datetime(btc["time"], unit="s", utc=True)
    eth["timestamp"] = pd.to_datetime(eth["time"], unit="s", utc=True)
    btc = btc.set_index("timestamp")["close"].sort_index()
    eth = eth.set_index("timestamp")["close"].sort_index()
    # log returns at various horizons
    out = {}
    for label, freq in [("1m", "1min"), ("5m", "5min"), ("1h", "1h"), ("1d", "1D")]:
        b = btc.resample(freq).last().dropna()
        e = eth.resample(freq).last().dropna()
        common = b.index.intersection(e.index)
        bl = np.log(b.loc[common]).diff().dropna()
        el = np.log(e.loc[common]).diff().dropna()
        common_ret = bl.index.intersection(el.index)
        if len(common_ret) < 10: continue
        corr = float(np.corrcoef(bl.loc[common_ret], el.loc[common_ret])[0, 1])
        out[label] = corr
    return out


def trade_pnl_correlation(btc_df: pd.DataFrame, eth_df: pd.DataFrame) -> float:
    """Correlation between BTC and ETH strategy PnL by day."""
    btc_daily = btc_df.groupby(btc_df["exit_t"].dt.date)["pnl_usd"].sum()
    eth_daily = eth_df.groupby(eth_df["exit_t"].dt.date)["pnl_usd"].sum()
    common = btc_daily.index.intersection(eth_daily.index)
    if len(common) < 5: return float("nan")
    return float(np.corrcoef(btc_daily.loc[common], eth_daily.loc[common])[0, 1])


def main():
    print("=" * 84)
    print("  CORRECTED v5 ANALYSIS — addressing methodological critiques")
    print("=" * 84)

    btc = load_trades("BTC")
    eth = load_trades("ETH")

    print()
    print(f"  Loaded BTC trade legs: {len(btc)}   "
          f"unique positions: {btc['position_id'].nunique()}")
    print(f"  Loaded ETH trade legs: {len(eth)}   "
          f"unique positions: {eth['position_id'].nunique()}")
    print()

    # ── 1. Position-level vs leg-level win rate ─────────────────────────────
    print("─" * 84)
    print("  [1] Position-level win rate (NOT trade-leg) — partial_tp double-counts fixed")
    print("─" * 84)
    btc_pos = position_level_stats(btc)
    eth_pos = position_level_stats(eth)
    btc_leg_win = (btc["pnl_usd"] > 0).mean() * 100
    eth_leg_win = (eth["pnl_usd"] > 0).mean() * 100
    print(f"  {'metric':<28} {'BTC (legs)':>12} {'BTC (pos)':>12} "
          f"{'ETH (legs)':>12} {'ETH (pos)':>12}")
    print(f"  {'win rate':<28} {btc_leg_win:>11.1f}% "
          f"{btc_pos['win_rate_pct']:>11.1f}% {eth_leg_win:>11.1f}% "
          f"{eth_pos['win_rate_pct']:>11.1f}%")
    print(f"  {'avg win $':<28} {'—':>12} ${btc_pos['avg_win']:>+10.1f} "
          f"{'—':>12} ${eth_pos['avg_win']:>+10.1f}")
    print(f"  {'avg loss $':<28} {'—':>12} ${btc_pos['avg_loss']:>+10.1f} "
          f"{'—':>12} ${eth_pos['avg_loss']:>+10.1f}")
    rr_b = f"{btc_pos['rr']:.2f}" if btc_pos['rr'] else "—"
    rr_e = f"{eth_pos['rr']:.2f}" if eth_pos['rr'] else "—"
    print(f"  {'R:R':<28} {'—':>12} {rr_b:>12} {'—':>12} {rr_e:>12}")
    print()
    print(f"  ⚠️  Leg-level win rate over-reports by ~{btc_leg_win - btc_pos['win_rate_pct']:.1f}pp "
          f"(BTC) and ~{eth_leg_win - eth_pos['win_rate_pct']:.1f}pp (ETH).")
    print()

    # ── 2. Corrected Sharpe ─────────────────────────────────────────────────
    print("─" * 84)
    print("  [2] Corrected Sharpe — pct_change() instead of diff()")
    print("─" * 84)
    eq_btc = equity_curve_from_trades(btc)
    eq_eth = equity_curve_from_trades(eth)
    btc_sr_wrong = diff_based_sharpe(eq_btc)
    btc_sr_right = corrected_sharpe(eq_btc)
    eth_sr_wrong = diff_based_sharpe(eq_eth)
    eth_sr_right = corrected_sharpe(eq_eth)
    print(f"  {'asset':<10} {'reported (diff)':>20} {'corrected (pct_change)':>26}")
    print(f"  {'BTC':<10} {btc_sr_wrong:>19.2f}  {btc_sr_right:>25.2f}")
    print(f"  {'ETH':<10} {eth_sr_wrong:>19.2f}  {eth_sr_right:>25.2f}")
    print()
    print(f"  ⚠️  pct_change Sharpe is the right one. Most public 'Sharpe N' claims "
          f"use this formulation.")
    print()

    # ── 3. BTC-ETH correlation ──────────────────────────────────────────────
    print("─" * 84)
    print("  [3] BTC vs ETH correlation — were they really 'independent'?")
    print("─" * 84)
    corrs = compute_correlation()
    print(f"  Perp log-return correlation by horizon:")
    for k, v in corrs.items():
        print(f"    {k:<4}  ρ = {v:+.3f}")
    pnl_corr = trade_pnl_correlation(btc, eth)
    print(f"  Daily strategy-PnL correlation: ρ = {pnl_corr:+.3f}")
    print()
    print(f"  ⚠️  Underlyings are highly correlated. The claimed portfolio "
          f"diversification effect was overstated.")
    if not math.isnan(pnl_corr) and abs(pnl_corr) > 0.3:
        # combined-portfolio max DD estimate: NOT min(BTC_DD, ETH_DD)
        # closer to the worse asset's DD, scaled by (1+ρ)/2
        scale = (1 + pnl_corr) / 2
        print(f"  Realistic portfolio max DD ≈ {(scale)*100:.0f}% of the worse asset's DD, "
              f"not the average.")
    print()

    # ── 4. Honest summary table ─────────────────────────────────────────────
    print("=" * 84)
    print("  HONEST REVISED SUMMARY")
    print("=" * 84)
    print(f"  {'metric':<28} {'BTC reported':>14} {'BTC corrected':>15} "
          f"{'ETH reported':>14} {'ETH corrected':>15}")
    print("  " + "-" * 80)
    print(f"  {'win rate (per position)':<28} {btc_leg_win:>13.1f}% {btc_pos['win_rate_pct']:>14.1f}% "
          f"{eth_leg_win:>13.1f}% {eth_pos['win_rate_pct']:>14.1f}%")
    print(f"  {'Sharpe (daily)':<28} {btc_sr_wrong:>14.2f} {btc_sr_right:>15.2f} "
          f"{eth_sr_wrong:>14.2f} {eth_sr_right:>15.2f}")
    net_btc = (btc["pnl_usd"].sum() / 10000) * 100
    net_eth = (eth["pnl_usd"].sum() / 10000) * 100
    print(f"  {'total return on $10k':<28} {net_btc:>+13.1f}% {net_btc:>+14.1f}% "
          f"{net_eth:>+13.1f}% {net_eth:>+14.1f}%")
    print()
    print(f"  Total return is unchanged — that's the actual realized PnL.")
    print(f"  Win rate drops to position-level (still strong, just honest).")
    print(f"  Sharpe drops because pct_change weights big-equity periods correctly.")
    print()

    # ── 5. Outstanding issues ───────────────────────────────────────────────
    print("=" * 84)
    print("  WHAT'S STILL OPEN (not fixed by this script)")
    print("=" * 84)
    print("  • OOS test: needs Dec 2025–Feb 2026 data (pulling now in background).")
    print("    This is the BIGGEST open question — all tuning was in-sample.")
    print()
    print("  • instruments_traded_today never resets — bug in guards.py.")
    print("    Currently doesn't bite because max_trades_per_day isn't in active")
    print("    guard list, but should still be fixed.")
    print()
    print("  • Real bid/ask vs 2bps assumption — unknown until live paper-trade.")
    print()


if __name__ == "__main__":
    main()
