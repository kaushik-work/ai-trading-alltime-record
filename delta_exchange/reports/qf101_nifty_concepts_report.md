# QF101 concepts on NIFTY 1m data (2021-01 .. 2026-05)

Research-only backtest of three quant-finance course concepts. Spot bars: median spot per 1m bar from the Week_1min option dataset (cache from `pa_options_nifty_1m.py`). ATM IV: winsorized [2%, 80%] median of ATM CALL/PUT IV at the last bar at/before 15:15. rf = 6% annual. December 2022–2025 is missing from the dataset (folders end Dec 1–2); forward-return windows spanning those gaps are excluded from primary tests.

## Study 1 — Volatility-managed NIFTY (Moreira & Muir)

| variant | series | CAGR | ann ret | ann vol | Sharpe | Sortino | MaxDD | final eq |
|---|---|---|---|---|---|---|---|---|
| daily-close RV | buy&hold | 9.15% | 9.82% | 14.57% | 0.26 | 0.25 | -18.08% | 1.53 |
| daily-close RV | volmgmt gross (daily-close RV) | 8.81% | 9.35% | 13.56% | 0.25 | 0.25 | -17.89% | 1.51 |
| daily-close RV | volmgmt net (daily-close RV) | 8.67% | 9.23% | 13.57% | 0.24 | 0.24 | -17.93% | 1.50 |
| 1m intraday RV | buy&hold | 9.65% | 10.28% | 14.60% | 0.29 | 0.29 | -18.08% | 1.57 |
| 1m intraday RV | volmgmt gross (1m intraday RV) | 8.72% | 9.26% | 13.44% | 0.24 | 0.24 | -16.58% | 1.51 |
| 1m intraday RV | volmgmt net (1m intraday RV) | 8.43% | 8.99% | 13.44% | 0.22 | 0.22 | -16.64% | 1.49 |
| 15m-resampled RV | buy&hold | 9.65% | 10.28% | 14.60% | 0.29 | 0.29 | -18.08% | 1.57 |
| 15m-resampled RV | volmgmt gross (15m-resampled RV) | 9.35% | 9.82% | 13.36% | 0.29 | 0.29 | -14.99% | 1.55 |
| 15m-resampled RV | volmgmt net (15m-resampled RV) | 9.08% | 9.58% | 13.36% | 0.27 | 0.27 | -15.00% | 1.53 |

| variant | c | mean w | ann turnover | ann cost drag | Sharpe uplift (net vs B&H) |
|---|---|---|---|---|---|
| daily-close RV | 6.19368e-05 | 1.000 | 9.43x | 0.09% | -0.02 |
| 1m intraday RV | 3.58686e-05 | 1.000 | 5.61x | 0.06% | -0.07 |
| 15m-resampled RV | 3.34431e-05 | 1.000 | 6.59x | 0.07% | -0.03 |

Buy-and-hold sanity: CAGR = **9.15%** (expected ~13-15%).

Baseline exposure distribution: mean 1.00, p5 0.24, median 1.11, p95 1.50; 35.4% of days at the 1.5x cap, 22.4% of days below 0.5x.

## Study 2 — Low-IV conditional forward returns

Forward window **21 trading days**: 1234 instances; **84 excluded** because the window spans a >10-calendar-day gap (missing Decembers 2022–2025). Windows are defined in trading sessions.

### 21-trading-day forward log returns by IV decile (gap-spanning excluded)

| decile | n | median IV % | mean fwd | median fwd | p25 | p75 | P(fwd>0) | std |
|---|---|---|---|---|---|---|---|---|
| 0 | 115 | 5.0 | 0.51% | 0.98% | -1.31% | 2.95% | 60.9% | 3.53% |
| 1 | 115 | 9.0 | 1.02% | 1.20% | -0.55% | 3.25% | 71.3% | 3.46% |
| 2 | 115 | 10.6 | 1.25% | 1.87% | -0.88% | 3.65% | 69.6% | 3.44% |
| 3 | 115 | 11.9 | 0.22% | 0.38% | -1.76% | 2.96% | 55.7% | 3.91% |
| 4 | 115 | 13.2 | 0.39% | 0.29% | -1.59% | 2.59% | 58.3% | 3.47% |
| 5 | 115 | 14.8 | 0.50% | 0.73% | -1.17% | 2.83% | 62.6% | 3.49% |
| 6 | 115 | 16.5 | 1.20% | 1.26% | -1.61% | 3.73% | 64.3% | 4.06% |
| 7 | 115 | 18.5 | 0.72% | 0.89% | -2.75% | 4.24% | 59.1% | 4.20% |
| 8 | 115 | 22.3 | 1.31% | 0.96% | -1.92% | 4.50% | 60.9% | 4.15% |
| 9 | 115 | 27.2 | 1.65% | 1.47% | -1.41% | 5.00% | 61.7% | 4.36% |

**LOWEST (D0) decile vs rest** (21d forward, primary): mean 0.51% vs 0.92% (diff -0.41%), P(>0) 60.9% vs 62.6%. Welch t=-1.17 (p=0.2422); Newey-West lag=21 t=-0.92 (p=0.3597); non-overlapping Welch t=-0.17 (p=0.8732); 10k permutation bootstrap p=0.2775.

**HIGHEST (D9) decile vs rest** (21d forward, primary): mean 1.65% vs 0.79% (diff 0.86%), P(>0) 61.7% vs 62.5%. Welch t=+2.04 (p=0.0429); Newey-West lag=21 t=+1.21 (p=0.2276); non-overlapping Welch t=-0.48 (p=0.6495); 10k permutation bootstrap p=0.0210.

Robustness with gap-spanning instances included: lowest-decile mean 0.60% vs rest 0.91% (Welch p=0.3854).

Forward window **42 trading days**: 1213 instances; **168 excluded** because the window spans a >10-calendar-day gap (missing Decembers 2022–2025). Windows are defined in trading sessions.

### 42-trading-day forward log returns by IV decile (gap-spanning excluded)

| decile | n | median IV % | mean fwd | median fwd | p25 | p75 | P(fwd>0) | std |
|---|---|---|---|---|---|---|---|---|
| 0 | 105 | 5.6 | 1.00% | 2.49% | -2.09% | 4.02% | 65.7% | 4.88% |
| 1 | 104 | 9.3 | 1.72% | 2.88% | -0.68% | 4.95% | 68.3% | 4.73% |
| 2 | 105 | 10.8 | 2.85% | 3.40% | -0.52% | 6.15% | 71.4% | 4.90% |
| 3 | 104 | 12.0 | 0.96% | 0.73% | -2.84% | 4.51% | 56.7% | 5.34% |
| 4 | 105 | 13.4 | 1.77% | 1.96% | -1.41% | 5.18% | 61.9% | 4.92% |
| 5 | 104 | 15.0 | 1.02% | 1.28% | -2.33% | 4.75% | 59.6% | 5.37% |
| 6 | 104 | 16.6 | 1.48% | 1.62% | -1.68% | 5.45% | 66.3% | 5.41% |
| 7 | 105 | 18.6 | 1.62% | 2.65% | -0.47% | 5.43% | 71.4% | 5.35% |
| 8 | 104 | 22.4 | 2.28% | 1.87% | -1.21% | 5.53% | 68.3% | 5.14% |
| 9 | 105 | 27.4 | 2.61% | 1.91% | -0.46% | 6.15% | 68.6% | 5.19% |

**LOWEST (D0) decile vs rest** (42d forward, primary): mean 1.00% vs 1.81% (diff -0.81%), P(>0) 65.7% vs 65.9%. Welch t=-1.60 (p=0.1120); Newey-West lag=42 t=-1.04 (p=0.3001); non-overlapping Welch t=+0.53 (p=0.6120); 10k permutation bootstrap p=0.1228.

**HIGHEST (D9) decile vs rest** (42d forward, primary): mean 2.61% vs 1.63% (diff 0.98%), P(>0) 68.6% vs 65.5%. Welch t=+1.84 (p=0.0686); Newey-West lag=42 t=+1.01 (p=0.3120); non-overlapping Welch t=-1.13 (p=0.2850); 10k permutation bootstrap p=0.0691.

Robustness with gap-spanning instances included: lowest-decile mean 0.92% vs rest 1.75% (Welch p=0.0962).

## Study 3 — Day-of-week effect (ANOVA)

One-way ANOVA: F=1.223, p=0.2992. Kruskal-Wallis: H=5.274, p=0.2603.

| weekday | n | mean | median | std | 95% CI |
|---|---|---|---|---|---|
| Mon | 253 | 0.050% | 0.180% | 1.16% | [-0.093%, 0.193%] |
| Tue | 250 | 0.125% | 0.053% | 0.89% | [0.014%, 0.235%] |
| Wed | 248 | 0.080% | 0.082% | 0.81% | [-0.021%, 0.181%] |
| Thu | 251 | 0.007% | 0.057% | 0.85% | [-0.099%, 0.113%] |
| Fri | 246 | -0.045% | -0.042% | 0.92% | [-0.160%, 0.070%] |

**Verdict rule** (p<0.05 on both tests AND a per-day CI excluding 0): **NO tradeable day-of-week effect**.

