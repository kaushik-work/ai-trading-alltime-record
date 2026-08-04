# Research learnings — mistakes, methods, and measured facts

Written 2026-08-04. The point of this file is that we do not repeat the errors
below. Several of them cost real money or produced results that were confidently
wrong. Read this before designing or trusting any backtest in this repo.

---

## 1. Mistakes made, and how each was caught

### 1.1 A backtest that omitted its largest cost
`backtest_price_action_sweep.py` defined `PERP_FEE_BPS = 5.0` and **never used
it**. Every published number from that file — including the +17.28% BTC /
+18.10% ETH that justified the live crypto strategy — was gross of fees.

With fees and exit slippage applied to the same data: **BTC +23.89% → −8.21%,
ETH +16.27% → −3.58%.** PF fell below 1.0 for both.

**Rule:** a cost constant that exists but is never referenced is worse than no
constant, because it looks handled. Grep for every declared cost and confirm it
is applied.

### 1.2 Five minutes of lookahead worth ₹384,000
`pandas.resample` labels a bar by its **start**. A bar labelled 09:30 covers
09:30:00–09:34:59, but a signal is only confirmed at the bar's **close**.
Reading the option price at the bar label gave five minutes of lookahead.

    with lookahead   203 trades   52.2% WR   +₹377,749
    corrected        193 trades   32.6% WR   −₹  6,110

**What exposed it:** 138 stops but 106 winners. Trades showing
`PE 182.55 → 186.40, stop, +866` — the index hit its stop while the option
gained. That is impossible, and impossible results are the cheapest bug
detector available.

**Rule:** always fill at bar CLOSE, never at the bar label. And when a result
looks good, look for the impossible row before celebrating.

### 1.3 A live gate above the physical maximum of what it measures
The NSE synthetic-forward strategy fires when the synthetic forward
(`K + CE − PE`) deviates from spot by more than `ENTRY_PCT = 0.60%`.

Measured across 1,869 strike observations: mean deviation **+0.028%**, max ever
observed **0.404%**. The 0.60% gate was reached **0 times**.

Put-call parity is an arbitrage identity — the deviation is bounded by carry
plus transaction costs, i.e. single-digit basis points. The gate demanded 60bps.
It could never trigger, which is why `nse_signals` is empty in Mongo. The
strategy was live, enabled, and structurally inert for its entire deployment.

**Rule:** before tuning a threshold, measure the distribution of the quantity it
gates. A threshold outside the observed range is not conservative, it is broken.

### 1.4 A 1:7 target the market almost never reaches
The crypto strategy paired a 0.6–0.7% stop with a 1:7 target and a 4-hour max
hold. How often is that target even reachable in 4 hours?

    BTC  4.2% target   0.46% of windows
    ETH  4.9% target   1.15% of windows
    XAUT 3.5% target   0.00% of windows

The target was hit **once in 515 trades**. The real exit distribution was
stop-or-max-hold; the stated 1:7 R:R did not exist in practice.

**Rule:** check that a target is reachable within the hold time before assuming
the R:R is real.

### 1.5 Reasoning from evidence that had been deleted
Concluded twice that "no log lines means it never happened". But
`docker compose logs` only covers the **current** container, and a deploy had
destroyed the previous one. The conclusion happened to survive; the reasoning
did not.

**Rule:** for anything that must survive a restart, check Mongo, not logs.
`nse_signals` / `nse_trades` / `crypto_trades` are the restart-proof record.

### 1.6 Guessing at a CPU cause twice
Blamed the 30s reconciliation tick, then swap. `docker stats` showed every
container under 1%; `free` showed no swap configured; `vmstat` showed zero
iowait. The real signal was `load average 5.05, 16.03, 18.13` — **decaying**,
i.e. a finished spike, which was the deploy's own `docker build`.

**Rule:** measure before hypothesising. `docker stats`, `free`, `vmstat`, `top`
answer this in thirty seconds.

### 1.7 An assumed delta instead of the recorded price
Estimated option P&L as `index_points × 0.5`. Measured against actual recorded
premiums, realised delta was **median 0.80, mean 0.84** — because the ₹180–200
premium band selects **in-the-money** strikes, not ATM.

**Rule:** when the actual traded price is in the dataset, never model it.

### 1.8 A hardcoded lot size contradicting config
Used `LOT = 75` while `nse/config.py` says 65. Every rupee figure was ~15% off.
Worse, **NIFTY lot size changed during 2021–2026**, so a single constant is
wrong for a five-year backtest — it needs a date-indexed table like STT has.

### 1.9 Selecting features while looking at the hold-out
Chose four journal features because their sign was consistent across TRAIN,
VALIDATE **and TEST**. Thresholds came from TRAIN only, which is correct, but
the feature *selection* saw TEST. Still uncorrected.

**Rule:** select on TRAIN+VALID. Spend TEST once, at the end, on one candidate.

---

## 2. Methodology that works

### 2.1 Three-way split, and TEST is spent once
    TRAIN     2021-2023   look, iterate, tune freely
    VALIDATE  2024        check a shortlist
    TEST      2025-2026   touched once, never informs any choice

**Proof it earns its keep:** `MR rsi 15/85` measured **+12.4 bps in TRAIN**
(p=0.004). VALIDATE +1.1. TEST **−2.1**. Chosen on TRAIN alone it looks
excellent. With ~23 hypotheses, roughly one clears p<0.05 by chance alone.

### 2.2 Measure the ENTRY before tuning the exit
Signed forward return against a baseline matched on the signal's own long/short
mix. No exit rule can harvest an edge the entry does not have, and this test is
cheap enough to run across dozens of hypotheses.

Result: BTC has a real edge (9–12 bps, p<0.01) that is **structurally below its
~14 bps cost floor**. No filter setting lifted it. That is not a tuning problem.

### 2.3 Report break-even, not a single P&L, when an input is unknown
No bid/ask exists in any source we hold. Estimation left a band from 0.03%
(one tick) to 0.9% (Corwin-Schultz) — a 30× range. Rather than pick a number,
sweep spread as a parameter and report *"profitable while half-spread < X%"*.

A wrong-but-conservative figure kills viable strategies just as unscientifically
as a wrong-but-optimistic one flatters dead ones.

### 2.4 Reject an estimator when the data contradicts it
Roll (1984) implied a ₹6–8 spread on ATM weeklies with ₹70–116 premiums. Those
contracts print in **100% of minutes** and the median 1-minute price *move* is
₹1.20–2.20. A spread wider than the whole minute's range is impossible, so Roll
was measuring volatility, not spread. Discarded with evidence, not preference.

### 2.5 Import production dials, never restate them
`backtest_live_config.py` imports every dial from `core.risk_management` and
`strategies.price_action_sr`. `backtest_price_action_sweep.py` restated them and
drifted. `gtt_levels_for_leg()` lives in `nse/config.py` so broker and backtest
cannot diverge.

---

## 3. Measured facts

### 3.1 Win rate is not profit
`Expectancy = WR × avg_win − (1−WR) × |avg_loss|`. Booking half at 1R **pins**
win rate at 50% for every R:R from 1:2 up — but caps average win at ~35 points
against ~87 for a single-stage exit.

    1-stage  1:9   WR 29%   avg win 87.3   expectancy 7.86   total 1,864
    3-stage  1:9   WR 50%   avg win 35.5   expectancy 5.08   total 1,209

The 3-stage buys **stability**, not profit: the 1-stage TRAIN column flips sign
between 1:6 and 1:9 because so few trades reach a 9R target.

### 3.2 Buying options penalises being right slowly
Measured on recorded premiums:

    losers    realised delta 0.86   avg hold  62 min
    winners   realised delta 0.73   avg hold 195 min

Winners are held 3× longer and pay theta throughout. The stop is fast and
full-priced; the target is slow and decayed. Theta at 1 DTE is **−32/day**.

### 3.3 The variance risk premium is the only edge that survived
    TRAIN 21-23  n=235  IV 19.18  RV 10.54  premium +8.64  t=16.79  IV>RV  98%
    VALID 24     n= 75  IV 17.29  RV  9.92  premium +7.38  t=12.27  IV>RV  96%
    TEST  25-26  n= 39  IV 18.55  RV 10.10  premium +8.45  t= 9.62  IV>RV 100%

Present in every year 2021–2025. **Caveat:** RV is intraday-only (overnight gaps
excluded from realised but priced into implied), so the premium is overstated
for anything held overnight. **Tails are real:** 2023-06-26 IV 10.83 vs RV
67.78. Defined-risk spreads only, never a naked short.

### 3.4 Strategy verdicts
| Strategy | Verdict |
|---|---|
| Crypto price-action S/R | Edge below cost floor; 1:7 target unreachable |
| NSE synthetic forward | Gate unreachable — has never fired |
| Breakout-retest (index) | +165/+259/+320 pts — positive in all three |
| Breakout-retest (options) | VALID −₹80,769 — period-specific, not viable |
| Variance risk premium | Survives hold-out at p<0.0001 |

---

## 4. Data facts

- **NIFTY 1m option data:** 1,255 sessions, 2021-01-01 → 2026-05-21, true OHLC
  per contract with volume/OI/IV. **Only NIFTY** — no BANKNIFTY/FINNIFTY/SENSEX.
- **Contamination:** `NIFTY_2021-08-30` holds 16 rows at BANKNIFTY levels.
  `clean_day()` drops rows >10% from the session median.
- **No-trade bars:** O=H=L=C with volume 0 means no trade that minute. 12.9% of
  bars in 2021 falling to 1.1% in 2026. Never treat as fillable.
- **`spot` is a CLOSE, not OHLC.** Resampled highs/lows are extremes of closes,
  so wick-touch rules under-fire versus live.
- **Mongo bid/ask were all zero.** The collector read `bidPrice`/`askPrice`,
  which Angel's FULL mode does not return — it exposes `depth.buy[]`/`sell[]`.
  Fixed 2026-08-04; real spreads accumulate from that date.
- **Lot sizes (2026):** NIFTY 65, BANKNIFTY 30, FINNIFTY 60, SENSEX 20.
  1 lot = 65 units, so premium ₹150 costs 65 × 150 = ₹9,750.

---

## 5. Live-system facts

- **Delta ETHUSD `contract_value` is 0.01, not 0.001.** The wrong constant made
  every ETH order 10× the intended notional and understated realized P&L (and
  therefore the daily kill switch) by the same factor.
- **Delta signatures expire in 5 seconds.** Re-sign on every retry attempt.
- **Delta bracket legs are limit-only** — place the limit *through* the trigger
  or it will not fill in the move that fired it.
- **Angel GTT:** one OCO rule per **leg**. A two-leg combo needs **two** rules.
  Never two separate rules for target and stop — the survivor stays armed after
  the first fires and later opens an unwanted position.
- **Never arm a GTT on an unconfirmed fill.** If the entry did not fill, the
  exit rule *opens* a naked position instead of closing one.
- **Leverage ceiling is set by liquidation, not appetite.** Delta BTC/ETH carry
  0.25% maintenance margin, so liquidation sits at `(1/LEV − 0.0025)`. At 200×
  that is 0.25% — inside a 0.70% stop, so the stop can never fire.

---

## 6. Open items

1. Redo journal feature selection on TRAIN+VALID only, spend TEST once (§1.9)
2. Date-indexed NIFTY lot-size table (§1.8)
3. ATR-normalised stops — a fixed 25-point stop is 2× tighter in 2026 than 2021
4. Re-measure the variance premium including overnight gaps (§3.3)
5. Design the defined-risk structure for the variance premium
6. Test the breakout-retest on BTC/ETH/XAUT
7. Surface the journal on the frontend
8. `ENABLE_NSE_RUNNER=false` until the synthetic forward is replaced
