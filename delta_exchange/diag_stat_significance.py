"""
v5 Statistical Reality Check — is the edge real or noise?
==========================================================
Applies the standard tools quant funds use to distinguish genuine alpha
from data-mined luck. Operates on the trade log saved by
backtest_synth_forward_v5.py (data/v5_trades.csv).

Tests:
  1. Welch t-test       — H0: mean per-trade PnL = 0
  2. Bootstrap CI       — 10,000 IID resamples of trade PnL
  3. Block bootstrap    — preserves serial correlation (block size = 5 trades)
  4. Walk-forward       — does edge persist in chronological thirds?
  5. Probabilistic SR   — Bailey & López de Prado (2012)
  6. Bonferroni-adj p   — penalty for trying many strategies
"""

import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
from pathlib import Path
import numpy as np
import pandas as pd

UNDERLYING = os.environ.get("UNDERLYING", "BTC").upper()
DATA = (Path(__file__).parent / "data") if UNDERLYING == "BTC" \
       else (Path(__file__).parent / "data" / UNDERLYING.lower())
TRADE_LOG  = DATA / "v5_trades.csv"

BOOT_N           = 10_000
BLOCK_SIZE       = 5
PSR_BENCHMARK_SR = 1.0       # "is true Sharpe above 1.0?"
N_STRATEGIES_TRIED = 10       # rough count of variants we tested
ANNUALIZATION_FACTOR = 365    # daily-equivalent for Sharpe scaling


def t_stat_p(x: np.ndarray) -> tuple[float, float]:
    """One-sample t-stat and two-sided p-value (no scipy)."""
    n = len(x)
    if n < 2: return float("nan"), float("nan")
    mean = x.mean(); sd = x.std(ddof=1)
    se = sd / math.sqrt(n)
    if se == 0: return float("inf"), 0.0
    t = mean / se
    # two-sided p-value via standard-normal approximation (n is large enough)
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
    return t, p


def sharpe(returns: np.ndarray, factor: float = 1.0) -> float:
    if len(returns) < 2: return 0.0
    sd = returns.std(ddof=1)
    if sd == 0: return 0.0
    return returns.mean() / sd * math.sqrt(factor)


def bootstrap_ci(x: np.ndarray, stat_fn, n_boot: int, ci: float = 0.95):
    rng = np.random.default_rng(42)
    idx = rng.integers(0, len(x), size=(n_boot, len(x)))
    samples = np.fromiter(
        (stat_fn(x[idx[i]]) for i in range(n_boot)),
        dtype=np.float64, count=n_boot,
    )
    lo, hi = np.quantile(samples, [(1 - ci) / 2, 1 - (1 - ci) / 2])
    return float(lo), float(hi), samples


def block_bootstrap(x: np.ndarray, block_size: int, stat_fn, n_boot: int):
    rng = np.random.default_rng(43)
    n = len(x)
    n_blocks = math.ceil(n / block_size)
    samples = np.empty(n_boot)
    for b in range(n_boot):
        starts = rng.integers(0, n - block_size + 1, size=n_blocks)
        chunks = [x[s:s + block_size] for s in starts]
        cat = np.concatenate(chunks)[:n]
        samples[b] = stat_fn(cat)
    lo, hi = np.quantile(samples, [0.025, 0.975])
    return float(lo), float(hi), samples


def probabilistic_sharpe(sr_obs: float, sr_bench: float, n: int,
                         skew_v: float, kurt_v: float) -> float:
    """Bailey & López de Prado 2012. Returns P(true Sharpe > sr_bench)."""
    if n < 3: return float("nan")
    denom = math.sqrt(1 - skew_v * sr_obs + (kurt_v - 1) / 4 * sr_obs**2)
    if denom <= 0: return float("nan")
    z = (sr_obs - sr_bench) * math.sqrt(n - 1) / denom
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def bonferroni_adj(p_obs: float, m: int) -> float:
    """Worst-case familywise-error adjustment if we tried m strategies."""
    return min(1.0, p_obs * m)


def main():
    if not TRADE_LOG.exists():
        print(f"trade log not found at {TRADE_LOG}; run backtest_synth_forward_v5.py first")
        return
    df = pd.read_csv(TRADE_LOG, parse_dates=["entry_t", "exit_t"])
    df = df.sort_values("exit_t").reset_index(drop=True)
    print(f"Loaded {len(df):,} trade legs for {UNDERLYING}   "
          f"({df['exit_t'].min().date()} → {df['exit_t'].max().date()})")
    print()

    pnl = df["pnl_usd"].to_numpy()
    pct = df["pnl_pct"].to_numpy()
    n   = len(pnl)

    # ── basic stats ─────────────────────────────────────────────────────────
    mean_pnl = pnl.mean()
    std_pnl  = pnl.std(ddof=1)
    skew_v   = float(pd.Series(pnl).skew())
    kurt_v   = float(pd.Series(pnl).kurt() + 3)   # pandas reports excess kurt; PSR wants raw
    sr_obs   = sharpe(pct, factor=n)   # per-trade-Sharpe (treat each trade as one period)
    sr_ann   = sharpe(pct, factor=ANNUALIZATION_FACTOR)
    win_rate = (pnl > 0).mean()

    print("=" * 80)
    print("  v5 STATISTICAL REALITY CHECK")
    print("=" * 80)
    print(f"  trade-leg count       : {n}")
    print(f"  mean PnL/trade        : ${mean_pnl:+.2f}")
    print(f"  std PnL/trade         : ${std_pnl:.2f}")
    print(f"  skew                  : {skew_v:+.3f}")
    print(f"  excess kurtosis       : {kurt_v - 3:+.3f}")
    print(f"  win rate              : {win_rate*100:.1f}%")
    print(f"  per-trade Sharpe      : {sr_obs:.3f}")
    print(f"  annualized Sharpe     : {sr_ann:.2f}")
    print()

    # ── 1. t-test ───────────────────────────────────────────────────────────
    t, p_raw = t_stat_p(pnl)
    print("─" * 80)
    print("  [1] One-sample t-test  H0: mean PnL = 0")
    print(f"      t-stat: {t:+.3f}      p-value (two-sided): {p_raw:.6f}")
    verdict = ("STRONG evidence edge ≠ 0" if p_raw < 0.001
               else "evidence edge ≠ 0" if p_raw < 0.05
               else "cannot reject null at 5%")
    print(f"      verdict: {verdict}")
    print()

    # ── 2. Bootstrap CI (IID) ───────────────────────────────────────────────
    lo_m, hi_m, _ = bootstrap_ci(pnl, lambda x: x.mean(), BOOT_N)
    lo_s, hi_s, _ = bootstrap_ci(pct, lambda x: sharpe(x, ANNUALIZATION_FACTOR), BOOT_N)
    lo_t, hi_t, _ = bootstrap_ci(pnl, lambda x: x.sum(), BOOT_N)
    print("─" * 80)
    print(f"  [2] Bootstrap 95% CIs ({BOOT_N:,} IID resamples)")
    print(f"      mean PnL/trade     : [${lo_m:+.2f}, ${hi_m:+.2f}]")
    print(f"      annualized Sharpe  : [{lo_s:.2f}, {hi_s:.2f}]")
    print(f"      total PnL          : [${lo_t:+,.0f}, ${hi_t:+,.0f}]")
    print(f"      observed mean / SR / total: ${mean_pnl:+.2f} / {sr_ann:.2f} / ${pnl.sum():+,.0f}")
    print()

    # ── 3. Block bootstrap ──────────────────────────────────────────────────
    lo_bm, hi_bm, _ = block_bootstrap(pnl, BLOCK_SIZE, lambda x: x.mean(), BOOT_N)
    lo_bs, hi_bs, _ = block_bootstrap(pct, BLOCK_SIZE, lambda x: sharpe(x, ANNUALIZATION_FACTOR), BOOT_N)
    print("─" * 80)
    print(f"  [3] Block bootstrap 95% CIs (block size {BLOCK_SIZE}, preserves autocorr)")
    print(f"      mean PnL/trade     : [${lo_bm:+.2f}, ${hi_bm:+.2f}]")
    print(f"      annualized Sharpe  : [{lo_bs:.2f}, {hi_bs:.2f}]")
    print()

    # ── 4. Walk-forward stability ───────────────────────────────────────────
    third = n // 3
    parts = [pnl[:third], pnl[third:2*third], pnl[2*third:]]
    print("─" * 80)
    print("  [4] Walk-forward stability — chronological thirds")
    for i, p in enumerate(parts, 1):
        m = p.mean(); sd = p.std(ddof=1)
        wr = (p > 0).mean()
        sr = m / sd * math.sqrt(ANNUALIZATION_FACTOR) if sd > 0 else 0
        print(f"      tercile {i}  n={len(p):>4}  "
              f"mean=${m:+.2f}  win={wr*100:.1f}%  Sharpe={sr:.2f}  total=${p.sum():+,.0f}")
    means = [pp.mean() for pp in parts]
    range_ratio = (max(means) - min(means)) / abs(np.mean(means)) if np.mean(means) != 0 else float("inf")
    print(f"      tercile-mean spread / overall mean: {range_ratio:.2f}× "
          f"({'STABLE' if range_ratio < 1 else 'UNSTABLE'})")
    print()

    # ── 5. Probabilistic Sharpe Ratio ───────────────────────────────────────
    psr = probabilistic_sharpe(sr_obs, PSR_BENCHMARK_SR / math.sqrt(ANNUALIZATION_FACTOR),
                                n, skew_v, kurt_v)
    print("─" * 80)
    print(f"  [5] Probabilistic Sharpe Ratio (Bailey & López de Prado 2012)")
    print(f"      P(true annualized Sharpe > {PSR_BENCHMARK_SR:.1f}) = {psr*100:.2f}%")
    interp = ("very high confidence true edge > benchmark" if psr > 0.95
              else "decent confidence" if psr > 0.80
              else "weak confidence" if psr > 0.50
              else "cannot establish edge above benchmark")
    print(f"      interpretation: {interp}")
    print()

    # ── 6. Multiple-testing penalty ─────────────────────────────────────────
    p_bonf = bonferroni_adj(p_raw, N_STRATEGIES_TRIED)
    print("─" * 80)
    print(f"  [6] Multiple-testing correction (Bonferroni, m = {N_STRATEGIES_TRIED})")
    print(f"      raw p-value     : {p_raw:.6f}")
    print(f"      adjusted p-value: {p_bonf:.6f}")
    print(f"      survives 5% after penalty: {'YES' if p_bonf < 0.05 else 'NO'}")
    print()

    # ── Verdict ─────────────────────────────────────────────────────────────
    checks = {
        "t-test p < 0.001"          : p_raw < 0.001,
        "bootstrap mean CI excludes 0": lo_m > 0,
        "block-bootstrap CI excludes 0": lo_bm > 0,
        "walk-forward stable (spread < 1×)": range_ratio < 1.0,
        "PSR > 80% above SR=1.0"   : psr > 0.80,
        "Bonferroni-adjusted p < 0.05": p_bonf < 0.05,
    }
    print("=" * 80)
    print("  VERDICT")
    print("=" * 80)
    for k, v in checks.items():
        print(f"    [{'PASS' if v else 'FAIL'}]  {k}")
    n_pass = sum(checks.values())
    print()
    if n_pass == 6:
        print(f"  → {n_pass}/6 checks passed. Strong statistical evidence of real edge.")
    elif n_pass >= 4:
        print(f"  → {n_pass}/6 checks passed. Edge likely real but some weak spots — investigate failures.")
    else:
        print(f"  → {n_pass}/6 checks passed. Edge may be illusory — likely data-mined.")
    print()


if __name__ == "__main__":
    main()
