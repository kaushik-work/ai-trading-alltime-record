# Alternative Strategy Research Pipeline

This document tracks quant/ICT/synthetic and higher-frequency strategy experiments
that are **separate from the live ETH price-action S/R bot**. The goal is to find
a return profile closer to 300–400% annualised without blowing up under realistic
costs.

> **Status:** work-in-progress. Results are recorded here; nothing in this file is
> wired to the live trading engine unless explicitly promoted.
> **Critical correction (2026-07-07):** Delta India ETH options have a contract
> size of **0.01 ETH per contract**, not 1 ETH. All earlier backtest P&L numbers
> in this file have been updated to reflect the correct sizing.
> **Critical correction (2026-07-14):** The option fetcher selected ATM strikes
> using spot at expiry (look-ahead bias) and the backtest understated fees and
> compounded capital. Corrected entry-time-ATM results are shown below; the
> short ATM straddle is now disabled.

---

## Strategy map

| # | Theme | Script | Status | Result (ETH, Apr–Jul 2026, realistic costs) |
|---|-------|--------|--------|---------------------------------------------|
| 1 | Higher-frequency microstructure | `backtest_hf_microstructure.py` | Failed | 2,631 trades, 24.1% WR, −₹2.46M, MaxDD 4,927% |
| 2a | Options / synthetic parity | `backtest_options_parity.py` | Rejected | 33 trades, 24.2% WR, −₹75k, MaxDD 208% |
| 2b | Options / short straddle (ATM) | `backtest_short_straddle_inr50k.py` | Disabled (2026-07-14) | ETH: 72 trades, 30.6% WR, −183.2%, MaxDD 183.2%. BTC: 62 trades, 38.7% WR, −72.1%, MaxDD 72.1%. Earlier positive results were invalidated by look-ahead strike selection. |
| 2c | Options / short strangle / iron condor | TBD | Research | Need OTM strikes and Greeks |
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

## 2. Options high-probability strategies

### 2a. Synthetic parity — **rejected**

**Script:** `delta_exchange/backtest_options_parity.py`

Result: 33 trades, 24.2% WR, **−$75k**, MaxDD 208%. Deviations are persistent
and do not mean-revert quickly enough for perp-only fading.

### 2b. Short ATM straddle — **disabled after corrected backtest**

**Scripts:**
- `delta_exchange/backtest_eth_short_straddle.py` (fixed-risk sizing)
- `delta_exchange/backtest_eth_short_straddle_sweep.py` (parameter sweep)
- `delta_exchange/backtest_eth_short_straddle_realistic.py` (1-contract sizing)
- `delta_exchange/backtest_short_straddle_inr50k.py` (generic ₹50k pool backtest)
- `delta_exchange/fetch_options_for_parity.py` (generic option data fetcher)

Corrected result (entry-time ATM selection, fixed ₹50k INR pool, no look-ahead,
contract size 0.01 ETH / 0.001 BTC, 15% margin/leg, 50 bps slippage, 25 bps/leg
fee, 4 fills per round-trip):

**ETH (Apr–Jul 2026, 1m marks):** 72 trades, 30.6% WR, **−183.2% return**,
MaxDD 183.2% (final capital −$483.91, i.e. the fixed pool is wiped out).

**BTC (Apr–Jul 2026, 1m marks):** 62 trades, 38.7% WR, **−72.1% return**,
MaxDD 72.1%.

Earlier reported positive numbers (+298% ETH, +127% BTC) were invalidated by
look-ahead bias: the old fetcher selected ATM strikes using spot at expiry
instead of spot at entry time, and the backtest used compounded capital and an
incorrect fee multiplier. The corrected assumptions show the short ATM straddle
is unprofitable over the Apr–Jul 2026 window.

Live wiring:
- `strategies/eth_short_straddle.py` — signal generation (ETH + BTC classes).
- `core/execution/options_runner.py` — position management, exits.
- `core/risk_management.py` — options capital dials.
- `api/server.py` — starts the options runner alongside the perp runner.

Status (2026-07-14): **Options runner disabled and both ETH/BTC short straddles
turned off** in `core/risk_management.py` and `core/strategy_toggles.py`. Do not
re-enable until a profitable variant is found in walk-forward backtests.

Next steps:
1. Investigate whether the losses are driven by the short available history
   (actual DTE ~3 instead of target 5) or by the 50%/200% exit rules.
2. Test OTM strangles / iron condors and IV-rank filters.
3. Run a walk-forward / out-of-sample test before any live re-enable.

### 2c. Strangle / iron condor / credit spreads — **research phase**

These are the classic HPS (high-probability short option) structures. Need:
- OTM call and put marks for each expiry.
- IV rank / percentile filter.
- Greeks-aware strike selection (target deltas).

**Reference literature:**
1. *Options as a Strategic Investment* — Lawrence McMillan (comprehensive strategy bible)
2. *Option Volatility and Pricing* — Sheldon Natenberg (volatility, Greeks, skew)
3. *The Option Trader’s Hedge Fund* — Dennis Chen & Mark Sebastian (short-option business model)
4. *The High Probability Options Trader* — Marcel Link

**Empirical rules from equity/index literature (to test on ETH):**
- Sell 16-delta OTM puts/calls, 30–45 DTE.
- Close at 50% of max profit.
- Stop at 200% of credit received.
- IV rank > 30–50 before entry.
- Expected: 65–75% win rate, ~15–25% annualised, 15–25% MaxDD.

Note: equity-index results cannot be blindly ported to crypto. Crypto has fatter
tails, wider bid-ask, and different margin rules.

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
| 2a | Options / synthetic parity | 33 | 24.2% | −₹75,168 | 208.4% | Fails — deviations do not mean-revert quickly enough |
| 2b | Options / short ATM straddle | ETH: 72 / 30.6%; BTC: 62 / 38.7% | ETH: −183.2%; BTC: −72.1% | ETH: ₹50k pool −183.2%; BTC: ₹50k pool −72.1% | ETH: 183.2%; BTC: 72.1% | Disabled 2026-07-14; earlier results were look-ahead biased |
| 3 | Cross-exchange spread | — | — | — | — | Blocked: no second-venue CSVs |
| 4 | BTC-leads-ETH momentum | 68 | 41.2% | +₹15,322 | 55.8% | Positive but drawdown too high |
| 5 | Market-making grid | 3,148 | 14.7% | −₹20,208,471 | 40,445% | Catastrophic — inventory stops dominate |

**Takeaway:** The short ATM straddle was promoted to the live runner after the
contract-size correction, then **disabled on 2026-07-14** once a corrected
entry-time-ATM backtest showed it is unprofitable (ETH −183.2%, BTC −72.1% over
Apr–Jul 2026). Earlier positive results were driven by look-ahead strike
selection, compounded capital, and an understated fee model. The other four
directions remain rejected or blocked.

## Decision log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-13 | Created pipeline file | Live ETH S/R edge is too thin for 300–400% target; need parallel research track. |
| 2026-07-13 | Committed alternative backtest scripts | Keep experiments reproducible and separate from live code. |
| 2026-07-13 | Initial prototypes all rejected or blocked | 1m perp-only data is insufficient for the target return/risk ratio at 15× leverage. |
| 2026-07-13 | Fetched ETH option ATM 1h marks | `fetch_eth_options_for_parity.py` downloaded 83 ATM expiry pairs from Delta. |
| 2026-07-13 | Options parity backtest completed | Fades synthetic-forward deviation with perp-only trades; losses dominated by persistent bias + SL hits. |
| 2026-07-07 | Short straddle promoted to live runner | Added `strategies/eth_short_straddle.py`, `core/execution/options_runner.py`, `core/risk_management.py` options dials, and `backtest_eth_short_straddle_inr50k.py`. Hardcoded live and enabled; all options dials are code, not env. |
| 2026-07-07 | Contract size corrected | Delta India ETH options = 0.01 ETH/contract. Prior backtest P&L numbers were 100× too large. |
| 2026-07-07 | BTC short straddle backtested | Fetched BTC option 1m marks and ran fixed-pool ₹50k backtest. Original report claimed 69 trades, 73.9% WR, +127.4%, MaxDD 0.0%; later corrected to a loss. Verified Delta India BTC option contract size = 0.001 BTC. |
| 2026-07-14 | Options runner disabled | Corrected entry-time-ATM backtest shows ETH short straddle −183.2% and BTC −72.1%. Set `ENABLE_OPTIONS_RUNNER = False` and disabled both ETH/BTC short straddle toggles until a profitable variant is revalidated. |

---

## Promotion criteria

Before any strategy here is promoted to the live bot:

1. Positive net P&L over **at least 3 months** of out-of-sample or walk-forward data.
2. MaxDD under **30%** of allocated capital at target leverage.
3. Profit/Risk ratio **> 2.0**.
4. Robust across parameter perturbations (±20% on key dials).
5. Realistic cost model confirmed (fees + slippage included).
6. Data required for live execution is available and reliable.

No strategy currently meets the promotion criteria. The short ATM straddle was
promoted and then rolled back on 2026-07-14 after corrected backtests showed
severe losses. Re-promotion requires a profitable walk-forward variant and
real-margin validation on Delta India.
