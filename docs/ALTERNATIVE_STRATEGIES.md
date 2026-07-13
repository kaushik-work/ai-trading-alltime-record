# Alternative Strategy Research Pipeline

This document tracks quant/ICT/synthetic and higher-frequency strategy experiments
that are **separate from the live ETH price-action S/R bot**. The goal is to find
a return profile closer to 300–400% annualised without blowing up under realistic
costs.

> **Status:** work-in-progress. Results are recorded here; nothing in this file is
> wired to the live trading engine unless explicitly promoted.

---

## Strategy map

| # | Theme | Script | Status | Result (ETH, Apr–Jul 2026, realistic costs) |
|---|-------|--------|--------|---------------------------------------------|
| 1 | Higher-frequency microstructure | `backtest_hf_microstructure.py` | Failed | 2,631 trades, 24.1% WR, −₹2.46M, MaxDD 4,927% |
| 2 | Options / synthetic parity | `backtest_options_parity.py` | Blocked | No local ETH options data |
| 3 | Cross-exchange spread | `backtest_cross_exchange.py` | Blocked | No second-venue 1m data |
| 4 | Multi-asset / inter-market | `backtest_multi_asset_momentum.py` | Marginal | 68 trades, 41.2% WR, +₹15,322, MaxDD ₹27,903 (55.8%) |
| 5 | Market-making grid | `backtest_market_making_grid.py` | Failed | 3,148 trades, 14.7% WR, −₹20.2M, MaxDD 40,445% |

---

## Common assumptions

All prototypes use the same realistic cost model so results are comparable:

- Fixed capital per trade: **₹50,000**
- Leverage: **15×**
- Perp taker fee: **5 bps / side**
- Entry + exit slippage: **2 bps each side**
- 1-minute decision grid (continuous) unless the strategy explicitly needs a coarser grid
- Max hold and cooldown rules documented per strategy

If a strategy needs data we do not currently have (options, tick, second venue),
the script fails loudly and prints the required file layout.

---

## 1. Higher-frequency microstructure

**Concept:** Trade short-term mean reversion around an anchored VWAP or fair
value, exiting quickly on small edge. This uses 1m candles as a proxy for tick
data until real tick/order-book feeds are available.

**Script:** `delta_exchange/backtest_hf_microstructure.py`

**Signal:**
- Anchor VWAP over last N minutes.
- Enter long when price pierces VWAP − k×std and shows reversal close.
- Enter short on VWAP + k×std.
- Tight SL/TP (e.g., 0.3% / 0.6%) and max hold 15–30 minutes.

**Why it might help:** Higher frequency = more trades, smaller per-trade risk,
potentially smoother equity curve. Costs dominate, so edge must be clean.

---

## 2. Options / synthetic parity

**Concept:** Reconstruct synthetic forward from call − put + strike and trade
perp against mispriced options, or buy long straddles when parity deviations are
large.

**Script:** `delta_exchange/backtest_options_parity.py`

**Signal:**
- For each expiry/strike: `synthetic_F = C − P + K`.
- Median deviation across near-money strikes.
- If |deviation| > threshold, trade perp in the direction that profits when the
deviation compresses, or buy ATM straddle for volatility expansion.

**Blocker:** No local ETH option-chain CSVs. BTC option files referenced in older
scripts are also missing. Need to either:
- Download Delta option marks, or
- Request/restore the `data/options/` directory.

---

## 3. Cross-exchange spread

**Concept:** Trade the BTC or ETH perp spread between Delta India and a liquid
international venue (Binance/Bybit). When the spread widens beyond funding +
slippage, buy the cheap leg and sell the rich leg.

**Script:** `delta_exchange/backtest_cross_exchange.py`

**Signal:**
- `spread_bps = (price_venue_A − price_venue_B) / mid × 10,000`
- Enter when spread > entry_threshold; exit when spread < exit_threshold.
- Account for double fees, slippage, and funding differential.

**Blocker:** Need second-venue 1m data. The script will describe the expected
file format.

---

## 4. Multi-asset / inter-market momentum

**Concept:** Use BTC as a leading signal for ETH, or run the existing S/R retest
engine across BTC, ETH, and XAUT with asset-specific dials and a portfolio-level
risk budget.

**Script:** `delta_exchange/backtest_multi_asset_momentum.py`

**Signal options:**
- BTC 15m trend breakout → ETH entry in same direction after a small lag.
- Equal-risk allocation across BTC/ETH/XAUT S/R setups.
- Correlation-adjusted position sizing so a single risk-off move does not hit
all legs.

**Notes:**
- BTC and ETH are correlated; XAUT (gold) is less correlated but showed poor fit
with the S/R engine in early tests.
- Data windows differ: ETH has Apr–Jul 2026; BTC starts Jun 2026; XAUT starts
Jun 2026.

---

## 5. Market-making grid

**Concept:** Place a ladder of buy/sell orders around a reference price (e.g.,
VWAP or EMA) and collect the spread. Each filled leg is hedged by an opposite
leg at the next grid level.

**Script:** `delta_exchange/backtest_market_making_grid.py`

**Signal:**
- Reference = EMA(N) or VWAP(N).
- Grid levels every `grid_pct` above/below reference.
- When price touches a buy level, open long; when it touches a sell level, open
short. Close at the next grid level in profit.
- Inventory limit and stop-out rule to avoid one-directional runaway.

**Why it might help:** Purely short-term, mean-reverting, many small wins. The
enemy is trending markets and inventory build-up.

---

## Initial results (2026-07-13)

All five prototypes were run against locally available 1m perp data with the
standard cost model (5 bps fee, 2 bps slippage, ₹50k fixed, 15× leverage).

| # | Strategy | Trades | Win % | Gross P&L | MaxDD | Verdict |
|---|----------|--------|-------|-----------|-------|---------|
| 1 | HF VWAP mean reversion | 2,631 | 24.1% | −₹2,463,490 | 4,927% | Catastrophic — no mean-reversion edge at 1m |
| 2 | Options / synthetic parity | — | — | — | — | Blocked: no option-chain CSVs |
| 3 | Cross-exchange spread | — | — | — | — | Blocked: no second-venue CSVs |
| 4 | BTC-leads-ETH momentum | 68 | 41.2% | +₹15,322 | 55.8% | Positive but drawdown too high |
| 5 | Market-making grid | 3,148 | 14.7% | −₹20,208,471 | 40,445% | Catastrophic — inventory stops dominate |

**Takeaway:** None of the five directions currently beats or even matches the
live ETH S/R retest strategy (+₹21,995, MaxDD ~17%). Two directions are blocked
by missing data; the three that ran all have fatal flaws under 15× leverage.

## Decision log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-13 | Created pipeline file | Live ETH S/R edge is too thin for 300–400% target; need parallel research track. |
| 2026-07-13 | Committed alternative backtest scripts | Keep experiments reproducible and separate from live code. |
| 2026-07-13 | Initial prototypes all rejected or blocked | 1m perp-only data is insufficient for the target return/risk ratio at 15× leverage. |

---

## Promotion criteria

Before any strategy here is promoted to the live bot:

1. Positive net P&L over **at least 3 months** of out-of-sample or walk-forward data.
2. MaxDD under **30%** of allocated capital at target leverage.
3. Profit/Risk ratio **> 2.0**.
4. Robust across parameter perturbations (±20% on key dials).
5. Realistic cost model confirmed (fees + slippage included).
6. Data required for live execution is available and reliable.

No promotion is currently planned for any of these prototypes.
