# Cross-Instrument Backtest Report — Price-Action S/R Strategy

Date: 2026-08-01 · Research only, no live config changed.

Scope: live price-action S/R retest strategy + QF101-inspired variants across
BTC/ETH futures, BTC/ETH options (Delta), and NIFTY/BANKNIFTY/SENSEX options
(NSE). Scripts: `backtest_pa_variants.py`, `check_option_coverage.py`,
`backtest_pa_options.py` (delta_exchange/), `nse/backtest/pa_options_nse.py`.

---

## 1. Master results table

### Crypto futures variants (Apr 1–Jun 21 2026 window, gross of fees — see §3A)

| Asset | Variant | Trades | WR | PF | Net % | MaxDD | EV/tr | WF (40/60) |
|---|---|---:|---:|---:|---:|---:|---:|---|
| BTC | V0 baseline (live) | 124 | 60.5% | 1.71 | +16.0% | 1.86% | +0.129% | 1.46 / 1.88 |
| BTC | V1 +BE trail @1R | 124 | 57.3% | 1.79 | +16.2% | 2.46% | +0.130% | 1.51 / 1.98 |
| BTC | V2 vol-managed size | 124 | 60.5% | 1.58 | +10.4% | 2.31% | +0.084% | 1.72 / 1.53 |
| BTC | V3 1% fractional | 124 | 60.5% | 1.71 | +29.7% | 3.45% | +0.215% | 1.46 / 1.88 |
| ETH | V0 baseline (live) | 83 | 59.0% | 2.03 | +18.0% | 1.98% | +0.216% | 1.84 / 2.17 |
| ETH | V1 +BE trail @1R | 83 | 56.6% | 2.01 | +17.0% | 2.25% | +0.205% | 1.87 / 2.11 |
| ETH | V2 vol-managed size | 83 | 59.0% | 1.85 | +12.2% | 2.03% | +0.146% | 2.49 / 1.46 |
| ETH | V3 1% fractional | 83 | 59.0% | 2.03 | +28.3% | 3.03% | +0.309% | 1.84 / 2.17 |

Liquidation study at 30× (50% margin/trade): V0 and V2 both show **0 in-sample
liquidations**; V2 worsens return/DD badly (17.5 → 5.8 BTC). Vol-scaling sizes
up in quiet periods before range-expansion losses and down in the high-vol
directional moves this strategy profits from — wrong direction for this edge.

### Crypto options (BS model-priced; 0% real-mark coverage at needed strikes)

BTC, 164 signals (IV anchored from nearby strikes 80%, fallback rv7×1.15 20%):

| Structure | WR | PF | PnL $ | MaxDD $ | Avg premium $ | RoP |
|---|---:|---:|---:|---:|---:|---:|
| perp V0 (1u notional) | 53.0% | 1.55 | +11,278 | 1,401 | — | — |
| debit buy ATM | 46.3% | 1.30 | +3,526 | 1,032 | 906 | +2.4% |
| debit buy 1-OTM | 45.7% | 1.28 | +3,048 | 1,000 | 810 | +2.3% |
| debit spread ATM/+2 | 36.6% | 0.78 | −488 | 644 | 186 | −1.6% |

ETH live config (20 signals) / unfiltered (106 signals):

| Structure | WR | PF | PnL $ | RoP | | unfiltered PF | PnL $ |
|---|---:|---:|---:|---:|---|---:|---:|
| perp V0 | 75.0% | 3.01 | +102.4 | — | | 1.85 | +422.5 |
| debit buy ATM | 45.0% | 2.29 | +41.6 | +4.5% | | 1.51 | +149.2 |
| debit buy 1-OTM | 45.0% | 2.18 | +33.9 | +4.6% | | 1.49 | +120.2 |
| debit spread | 45.0% | 1.64 | +7.3 | +2.2% | | 1.09 | +10.6 |

IV sensitivity ±10 vol pts: rankings stable (atm > otm1 > spread); BTC spread
flips sign at +10 vols. Theta measured ~1.6% of premium over a 4h hold —
irrelevant at this horizon.

### NSE index options (real collected marks, 5-min, intraday only)

| Symbol | Structure | Trades | WR | PF net | Net ₹ | Sample |
|---|---|---:|---:|---:|---:|---|
| NIFTY | debit buy | 13 | 38.5% | 0.52 | −6,766 | 27 days |
| NIFTY | debit spread | 13 | 23.1% | 0.13 | −8,024 | 27 days |
| BANKNIFTY | debit buy | 3 | 66.7% | 1.40 | +1,965 | 13 days |
| BANKNIFTY | debit spread | 3 | 33.3% | 0.12 | −631 | 13 days |
| SENSEX | debit buy | 5 | 60.0% | 2.14 | +3,099 | 14 days |
| SENSEX | debit spread | 5 | 60.0% | 4.29 | +1,829 | 14 days |

## 2. Verdicts

1. **Keep the live futures config as-is (V0, pure SL/TP, no trail).** Best
   risk-adjusted variant. BE trail (V1) is neutral-to-negative (scratches at
   pnl=0, worse MaxDD, flat PnL). Vol-managed sizing (V2) is *wrong-direction*
   for this strategy — it sizes up before range-expansion losses. V3
   fractional is only a compounding choice, not an edge.
2. **ATM/1-OTM debit buys are a viable defined-risk alternative to the 30×
   perp**: positive EV on all passes, ~30% lower MaxDD, worst case capped
   well below premium, no liquidation tail. They sacrifice ~2/3 of raw
   perp PnL per signal. On return-per-capital-at-risk they're comparable.
   **Debit spreads are the wrong tool** — the sold leg amputates the
   right-tail drift that carries this strategy (TP never fires in-sample;
   all profit is hold-exit drift), while still paying theta.
3. **NSE index options: no edge, and mostly structural.** The crypto signal
   relies on wick information that doesn't exist in 5-min spot prints; 1:7 RR
   is unreachable intraday on indices (TP never hit once); spread costs
   (₹420/trade) exceed average gross wins. NIFTY — the only adequate sample —
   is net negative (PF 0.52). Don't pursue in current form.

## 3. Data/doc integrity findings (from Phase A/B/D — action-worthy)

- **A. The documented sweep numbers are gross of fees.** `PERP_FEE_BPS` is
  defined but never applied in `backtest_price_action_sweep.py`. Estimated
  drag: −12.4% of equity (BTC, 124 trades) / −8.3% (ETH) — fees eat 60–75%
  of the documented gross PnL. The AGENTS.md performance table overstates
  the edge accordingly.
- **B. Documented numbers were generated with `trail_be=True`**, not the live
  `pure_sltp` regime (V1 exactly reproduces BTC 124/57.3%/1.79). Deltas are
  small but the docs describe a different exit regime than what runs live.
- **C. The documented ETH 34% vol filter was not active** in the documented
  run (enabling it leaves 17–20 ETH trades on this data range).
- **D. Data dirs are inconsistent**: `data/perp/` reproduces documented BTC
  results; `data/btc/` gives 141 trades/PF 1.60 and runs to Jul 7 while
  `data/eth/` stops Jun 20. Standardize before the next research round.
- **E. TP-before-SL same-candle ordering** in the harness resolves ambiguous
  candles optimistically; live results will be slightly worse.
- **F. Delta options data cannot support real-mark backtests of ATM
  structures** (1 strike per expiry, ATM only 72h before expiry). Any serious
  options-execution evaluation needs a live shadow run against executable
  quotes, or a proper option-chain collector for Delta.
- **G. NSE snapshot quirks fixed in pa_options_nse.py**: expiry-roll
  contamination on exits, 1-second duplicate sub-snapshots in the July
  window, overnight-hold leak on partial days. BANKNIFTY has only monthly
  expiries in the data (NSE discontinued its weeklies).

## 4. Candidates for next step

1. **Paper/shadow the ATM debit-buy options execution** on Delta for ETH
   (the only options result with adequate signal count and positive EV) —
   compare BS-modeled entries to actual fills before any live consideration.
2. **Correct the backtest harness** (fees, trail regime documentation) and
   re-baseline the live expectancy net of costs; update AGENTS.md to match.
3. Revisit NSE only with real 5-min OHLC spot (Angel historical candles) and
   an intraday-appropriate RR (1:1.5–1:2) — a different strategy, not this one.
4. Do **not** adopt vol-managed sizing or BE trail for the live crypto bot.
