# Month-on-Month Backtest Report — Price-Action S/R Strategy

Date: 2026-08-01 · Research only.
Scripts: `delta_exchange/backtest_pa_monthly.py` (crypto),
`nse/backtest/pa_options_nifty_1m.py` (NIFTY 1m).
Data: Delta 1m perp marks (BTC `data/perp/`, ETH `data/eth/perp/`),
`C:\Users\anura\Downloads\Nifty_option_historical\Week_1min` (2021-01→2026-05-21),
Mongo `option_snapshots` tail (2026-05-22→06-09, 07-29→07-31, 5-min, flagged).

---

## 1. Crypto (Delta futures, V0 live config, 2026-04→06)

### BTCUSD — LIVE config (pure SL/TP, no trail, no vol filter)

| Month | Trades | WR | PF gross | PF net | Gross % | Net % | MaxDD% |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-04 | 48 | 58.3% | 1.40 | 0.81 | +3.50% | **−2.26%** | 3.78% |
| 2026-05 | 53 | 64.2% | 2.03 | 1.19 | +8.33% | +1.97% | 2.20% |
| 2026-06* | 23 | 56.5% | 1.75 | 1.21 | +4.19% | +1.43% | 3.31% |
| **TOTAL** | 124 | 60.5% | 1.71 | **1.04** | +16.03% | **+1.15%** | 3.78% |

### BTCUSD — DOCUMENTED config (trail_be=True — the AGENTS.md numbers)

| Month | Trades | WR | PF gross | PF net | Gross % | Net % |
|---|---:|---:|---:|---:|---:|---:|
| 2026-04 | 48 | 54.2% | 1.44 | 0.79 | +3.52% | **−2.24%** |
| 2026-05 | 53 | 60.4% | 2.05 | 1.15 | +7.86% | +1.50% |
| 2026-06* | 23 | 56.5% | 1.96 | 1.32 | +4.79% | +2.03% |
| **TOTAL** | 124 | 57.3% | 1.79 | **1.05** | +16.18% | **+1.30%** |

### ETHUSD — LIVE config (pure SL/TP, 24h vol ≤34%)

| Month | Trades | WR | PF gross | PF net | Gross % | Net % |
|---|---:|---:|---:|---:|---:|---:|
| 2026-04 | 4 | 100% | inf | 34.09 | +3.58% | +3.10% |
| 2026-05 | 11 | 81.8% | 2.72 | 1.10 | +1.46% | +0.14% |
| 2026-06* | 2 | 50.0% | 0.57 | 0.00 | −0.06% | **−0.30%** |
| **TOTAL** | 17 | 82.4% | 6.00 | **2.66** | +4.97% | **+2.93%** |

### ETHUSD — DOCUMENTED config (trail_be, no vol filter)

| Month | Trades | WR | PF gross | PF net | Gross % | Net % |
|---|---:|---:|---:|---:|---:|---:|
| 2026-04 | 31 | 54.8% | 1.80 | 1.19 | +5.35% | +1.63% |
| 2026-05 | 40 | 57.5% | 1.14 | 0.67 | +1.22% | **−3.58%** |
| 2026-06* | 12 | 58.3% | 7.72 | 5.07 | +10.41% | +8.97% |
| **TOTAL** | 83 | 56.6% | 2.01 | **1.32** | +16.97% | **+7.01%** |

*June partial (data ends 2026-06-20/21). Net = gross − 12bps notional round
trip (5bps×2 fees + 2bps exit slippage; entry slippage already in gross).
Cost drag: BTC 124 round trips ≈ 14.9% of equity.

**Crypto takeaways:** BTC net of realistic fees is essentially breakeven
(PF 1.04–1.05) — the documented gross PF 1.79 overstates the deployable edge.
ETH holds up net (+7.01% doc config) because per-trade edge is larger. The
ETH live vol filter cuts 83→17 trades (WR 82%) — too few for monthly
conclusions. Net-negative months: BTC Apr (both configs), ETH doc May,
ETH live Jun (2-trade month).

## 2. NIFTY options (1m data 2021→2026-05 + Mongo tail, chosen config SL 0.35% / RR 1:3)

2025 in-sample sweep: **all 9 configs (SL 0.2/0.35/0.5% × RR 1.5/2/3) lose
money** (PF 0.82–0.88). Chosen = least-bad (0.35%/1:3, PF 0.88).

### Yearly subtotals (net ₹, 1 lot, period lot-size map)

| Year | Trades | WR | PF | Net ₹ |
|---|---:|---:|---:|---:|
| 2021 | 131 | 35.1% | 0.61 | −54,274 |
| 2022 | 118 | 34.7% | 0.75 | −25,433 |
| 2023 | 131 | 36.6% | 0.91 | −8,223 |
| 2024 | 120 | 30.8% | 0.54 | −39,585 |
| 2025 | 129 | 37.2% | 0.88 | −20,006 |
| 2026 (→May) | 56 | 32.1% | 0.61 | −34,201 |
| **GRAND** | **685** | **34.7%** | **0.73** | **−181,721** |

Reference crypto-equivalent config (0.7%/1:7): grand PF 0.73, −₹191,204;
**spot TP never fired once in 682 trades over 5.4 years.**

Full month-by-month table: `db/nse_backtest/NIFTY_pa_1m_monthly.csv`
(67 months; best: 2025-04 +₹30,355 tariff-crash rebound; worst:
2025-01 −₹22,488; 2026-05*–07* rows are 5-min Mongo granularity, flagged).
Per-trade log: `db/nse_backtest/NIFTY_pa_1m_trades.csv` (685 trades).
Expiry-day bucket: 131 trades, PF 0.84 — no hidden edge there either.
Exit anatomy (chosen config): max_hold exits PF 2.03 (+₹104k), square_off
PF 1.23 (+₹35k), spot_tp 13 trades (+₹96k) — all erased 2× over by 199
stop-outs (−₹417k).

**NIFTY takeaway:** consistently negative across every regime (2021 bull,
2022 bear, 2024 election vol, 2025) — no period exists that a filter could
isolate. Structural causes: 4h lookback spans the overnight gap (stale
levels), intraday ATM debit buys bleed theta, costs eat the forced-down RR.
Do not pursue this signal family on NIFTY options in this form. If
revisited: short-premium structures (flip side of the bleed) or
session-local lookbacks reset daily.

## 3. Data integrity notes

- Crypto net figures now count the full 14bps round trip exactly once
  (harness previously ignored fees entirely — AGENTS.md numbers are gross).
- NIFTY dataset: Dec 2022–2025 folders end Dec 1–2 (December effectively
  missing except 2021); 2026-05-21 partial (ends 11:55); wild IV outliers
  exist in the `iv` column (unused by strategy); expiry weekday verified
  Thursday → Tuesday from 2025-09-02.
- 685-trade NIFTY run: 1 illiquid entry dropped, 0 exit fallbacks — chain
  data quality is solid.
