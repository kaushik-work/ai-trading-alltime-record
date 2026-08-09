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

**Same class, found later:** the NIFTY **expiry weekday** also changed
mid-dataset — Thursday until **2025-08-28**, Tuesday from **2025-09-02**. Any
`weekday == 3` is wrong for the last nine months of data, and expiry date sets
T, which sets every Greek. Derived empirically in `nse/quant/expiry_calendar.py`.
Assume nothing about a calendar constant across a five-year window; measure it.

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

Present in every year 2021–2025.

**CORRECTED 2026-08-04 — the caveat below was worth 41% of the edge.** RV above
is intraday-only while implied prices the whole calendar. Measured properly
(`nse/quant/test_vix_coverage.py`), the overnight session is **42.3% of total
variance**, so intraday-only RV understates true volatility by **1.335×**. The
TRAIN premium is therefore about **+5.1 vol points, not +8.64**. The premium is
still real — realised/implied is below 1.00 in every year — but it is a third
smaller than published here, and **in 2026 the ratio is 0.99, i.e. gone**.

**Tails live overnight, not intraday.** Normalised by its own IV, the intraday
move has kurtosis **0.98** and never reached 3σ in 1,249 sessions. Close-to-close
kurtosis is **13.75** with a −3.73σ day. An intraday-only backtest is
structurally blind to the risk that kills short-vol books. Defined-risk spreads
only, never a naked short. Full working in `docs/OPTIONS_GREEKS_LEARNINGS.md`.

### 3.4 Strategy verdicts
| Strategy | Verdict |
|---|---|
| Crypto price-action S/R | Edge below cost floor; 1:7 target unreachable |
| NSE synthetic forward | Gate unreachable — has never fired |
| Breakout-retest (index) | +165/+259/+320 pts — positive in all three |
| Breakout-retest (options) | VALID −₹80,769 — period-specific, not viable |
| Variance risk premium | Survives hold-out at p<0.0001 |
| Greeks lens (25d risk reversal) | No directional edge; negative in TRAIN and VALID |
| Volume/OI lens (walls + profile + build) | +1.80/+1.62 bps, positive and significant in TRAIN and VALID. TEST unspent. |

### 3.5 The 25-delta risk reversal does not predict NIFTY direction

First lens measured through `nse/backtest/lens_harness.py`. 390 sessions,
30-minute grid, 60-minute horizon, signed forward return against a mix-matched
baseline.

    split      n     long%   edge       t       p
    TRAIN    1600    56.4%   −0.54bps   −0.76   0.4485
    VALIDATE  784    86.6%   −1.08bps   −1.48   0.1405

Negative in both, significant in neither, break-even spread NONE. Left in
SHADOW at weight 0.

**Two things worth keeping from it.**

*Index skew is structurally negative, so raw sign is not a signal.* Measured on
TRAIN: rr_norm median −0.2098, **negative in 98.4% of 1,092 observations**. A
lens reading raw sign votes SHORT essentially always — a permanent bearish tilt
wearing a signal's clothes. Centring on the measured median made the vote 50/50
by construction, which is the only way anything surviving is timing rather than
tilt. This is §1.3's lesson again: measure the distribution before setting the
threshold.

*The calibration does not transport across years.* Long fraction was 56.4% on
TRAIN by construction but **86.6% on VALIDATE** — the neutral skew level drifted
between 2021-23 and 2024, and the lens read the flatter 2024 surface as
persistently bullish. Any constant fitted to a volatility surface needs either
a trailing reference or an explicit recalibration cadence; a fixed number is the
wrong shape for the quantity.

### 3.6 Volume/OI is the first entry that survives a hold-out

OI walls + volume profile + OI build direction, blended. Same protocol as §3.5:
390 sessions, 30-minute grid, 60-minute horizon, mix-matched baseline.

    split      n     long%   edge       t       p
    TRAIN    2693    54.4%   +1.80bps   +3.53   0.0004
    VALIDATE 1381    55.7%   +1.62bps   +2.10   0.0360

Positive and significant in both, signs agree, long/short balanced, and only 30
abstentions in 4,854 observations. Break-even half-spread **0.70%** on a ₹150
premium at 1 lot with the measured delta 0.80.

**This is promising, not proven.** Four things stand between it and capital:

1. **TEST is unspent and must stay that way** until one final candidate is
   ready. Spending it now to satisfy curiosity converts the only clean hold-out
   into another training set (§2.1).
2. **The estimated half-spread band is 0.03%–0.90% and break-even is 0.70%.**
   The edge clears most of that band but not its top. Where the truth sits
   inside the band decides whether this is tradeable at 1 lot (§2.3).
3. **1.80 bps is an INDEX forward return, not an option P&L.** Turning it into
   money still needs a structure, a strike rule and real fills.
4. Three components were each scale-calibrated on TRAIN. That is centring, not
   return-fitting, but it is not free either.

### 3.8 Traded as an option, Volume/OI lands exactly on the spread boundary

The index edge (§3.6) run through `nse/backtest/options_harness.py` — real
recorded premiums, date-aware costs, exit at the same 60-minute horizon the
entry was measured at, no new degrees of freedom.

**Gross is positive everywhere.** The direction call is real and it does convert
into option P&L. What happens next is entirely about costs.

    variant                       TRAIN gross   VALID gross   verdict @ tick floor
    ATM, 1 lot                       +18,015       +41,018     both NEGATIVE
    Rs150 band (ITM), 1 lot          +90,727       +82,725     TRAIN negative
    Rs150 band (ITM), 5 lots        +453,635      +413,627     both POSITIVE

Two things move it from dead to alive, and neither is a signal change:

*Strike selection.* The ₹150 premium band picks ITM strikes at delta ~0.80 and
captures **5x the gross** of ATM for the same signal. ATM has the tightest
spread but the least delta per rupee of premium.

*Position size.* At 1 lot, flat ₹20-per-order brokerage is **59% of total
cost**. At 5 lots it is 27%. Gross scales with size; flat brokerage does not.
The ₹50,000 per-trade budget allows exactly 5 lots at a ₹150 premium — so the
1-lot runs were testing a size the capital rule never specified.

**Where it actually lands:**

    half-spread   TRAIN net    VALIDATE net
    0.03%          +223,457       +298,376
    0.10%          +118,005       +245,387
    0.20%           -32,640       +169,690     <- TRAIN flips
    VERDICT: profitable in BOTH splits up to a half-spread of 0.10%

**CORRECTED 2026-08-08 — the "0.10% survival threshold" above was a GRID
ARTEFACT, not a break-even.** The sweep ran on `(0.03, 0.10, 0.20, …)`, TRAIN
turned negative somewhere between 0.10 and 0.20, and the coarse grid reported
the last surviving gridpoint as though it were the boundary. Re-running on the
measured percentiles instead put the true break-even **above 0.16%**:

    half-spread          TRAIN net    VALIDATE net
    0.1028%  (p25)        +113,787       +243,268
    0.1230%  (p50)         +83,357       +227,977
    0.1409%  (p75)         +56,391       +214,427
    0.1573%  (p90)         +31,686       +202,013
    VERDICT: profitable in BOTH splits up to a half-spread of 0.16%

**Profitable in both splits at every measured percentile, including p90.** The
whole measured distribution (§3.10) sits below break-even. Declaring it dead off
a gridpoint would have been the mirror image of §3.7 — there a units bug made a
live signal look worthless; here grid granularity nearly did the same.

Remaining caveats, none of them small:
- **TEST is unspent.** This is a TRAIN/VALIDATE result.
- The spread sample is **four sessions in August 2026**. The backtest spans
  2021-2026 and spreads were plausibly wider for most of it.
- **VALIDATE is 3-6x stronger than TRAIN** at every spread level. A hold-out
  outperforming the training set that consistently is a regime signal, not a
  bonus — treat the TRAIN column as the honest one.
- Expectancy at p90 on TRAIN is **+₹20/trade**. Thin.
- Worst single trade is **−₹46,150**, roughly 92% of a ₹50,000 position.
- Slippage beyond the quoted touch is not modelled.

### 3.10 The measured half-spread distribution

Collector depth, 14,204 NIFTY observations with a genuine two-sided book across
four sessions from 2026-08-04. Half-spread as a percentage of mid.

    bucket          n      mean     p25     p50     p75     p90
    deep ITM    1,676    0.2214  0.1321  0.1577  0.2236  0.2998
    ITM         3,352    0.1590  0.1197  0.1369  0.1637  0.2077
    near ATM    3,346    0.1371  0.1025  0.1234  0.1418  0.1604
    OTM         3,334    0.2815  0.1198  0.1439  0.1735  0.5405
    deep OTM    2,496    0.5558  0.1418  0.1792  0.2295  2.1277

**Deep OTM has a p90 of 2.13% against a 0.18% median** — the same "cheap OTM is
a cost trap" finding as §1.4's brokerage arithmetic, now visible in the spread.

By time of day, the close is a different market:

    open 09:15-09:30    p50 0.1490  p75 0.1943  p90 0.2916
    midday 10:30-14:00  p50 0.1399  p75 0.1720  p90 0.2385
    close 15:00+        p50 0.1592  p75 0.3829  p90 1.2048

p90 at the close is **5x midday**. A session-wide average hides that entirely.

Across symbols, ₹120-190 premium band: NIFTY p50 **0.1230%**, SENSEX **0.1423%**,
FINNIFTY **1.4760%**. FINNIFTY is an order of magnitude wider and effectively
untradeable at these premiums.

### 3.9 Liquidity sweeps have no edge on BTC/ETH 5m

Wick pierces a prior swing extreme, closes back inside, stop beyond the wick,
target at the next opposing level. 399 days of true OHLC per symbol, 5-minute
bars, per-bar ATR, no lookahead.

**Unconditional base rate — negative in all four cells, GROSS:**

    symbol    split       n    hit%   need%    edge
    ETHUSD    TRAIN    6,108   29.5%   31.1%  −0.016
    ETHUSD    VALIDATE 2,243   31.0%   31.9%  −0.009
    BTCUSD    TRAIN    5,889   29.0%   30.2%  −0.013
    BTCUSD    VALIDATE 2,276   30.0%   30.5%  −0.005

`need%` is 1/(1+medianRR). The sweep resolves in its favour slightly less often
than its own reward ratio requires, before a single rupee of cost.

**Conditional — no feature survives out of sample.** Quintiled seven entry-time
features on TRAIN and checked each on VALIDATE: pierce depth, rejection
strength, level age, stop width, target touches, volatility regime, R:R. TRAIN
spreads of 0.04–0.13 R across quintiles look promising and then do not hold —
the VALIDATE columns are noise. Four of thirty-five cells came back positive in
both splits, which is what chance delivers at that many comparisons (§2.1).

**The best cell still loses.** Tight stops (`stop_atr` q1) was the one candidate
with a monotonic story: +0.085 R on TRAIN, +0.084 on VALIDATE. Against Delta's
5 bps/side fee and 2 bps/side slippage — 14 bps round trip — the cost in R units
is 0.0014 / stop_fraction:

    stop 0.3%  cost 0.467 R      stop 1.0%  cost 0.140 R
    stop 0.5%  cost 0.280 R      stop 1.5%  cost 0.093 R

Every one exceeds the 0.085 R gross edge. The setup loses at every stop width,
and that is being generous — 0.085 R is the best of thirty-five cells.

This is the crypto price-action finding again in a harsher form. There, BTC had
a real 9–12 bps edge sitting under a ~14 bps cost floor (§2.2). Here there is no
real edge to begin with.

**What this does and does not rule out.** It rules out *this* sweep definition,
on 5-minute BTC/ETH, with next-opposing-level targets. It does not rule out
chart-based trading in general, other timeframes, or NSE. The value is that the
infrastructure to test the next variant now exists and answers in hours.

### 3.7 An index-bps edge is not an option-premium bps edge

`breakeven_spread()` multiplied an index-move edge straight into the option's
premium notional (`premium × qty`). That understated gross by roughly **80×**
and reported "costs eat the edge at the tick floor" for a signal that in fact
clears 0.70%. The conversion takes three steps, not one:

    index move   = spot × edge_bps / 10,000     index points
    premium move = delta × index move           option points
    gross        = premium move × qty           rupees

Caught because a signal significant at p=0.0004 was being declared unviable,
which was too convenient to accept. §2.3 warns that a wrong-but-conservative
cost figure kills viable strategies exactly as unscientifically as an
optimistic one flatters dead ones — this was that, in the conservative
direction, inside our own harness.

Delta defaults to the **measured 0.80**, not 0.5 (§1.7). Premium and delta must
describe the same contract: (₹2, 0.80) is not a contract that exists.

---

### 3.11 Four lenses measured on identical snapshots: one survived

390 NIFTY sessions (260 TRAIN, 130 VALIDATE), 30-minute grid, 60-minute
horizon, **the same `MarketSnapshot` handed to every lens** so the comparison is
between perspectives and not between datasets.

| lens | TRAIN | VALIDATE | signs agree | verdict |
|---|---|---|---|---|
| `volume_oi` | **+1.66** p=0.0012 | **+1.49** p=0.0527 | **yes** | survives, PROBATION |
| `vwap` | −2.31 p=0.0014 | −1.06 p=0.2585 | no | no edge |
| `ict_smc` | −4.25 p=0.0000 | −0.68 p=0.5609 | no | no edge |
| `greeks` | −0.54 p=0.4485 | −1.08 p=0.1405 | no | no edge |

Building a lens is cheap; earning a weight is not. Four of five got built, one
gets to move capital, and it does so capped at PROBATION weight.

**`volume_oi` moved +1.80 → +1.66 bps on a change of bar construction alone**
(per-minute → 5-minute OHLC), and VALIDATE crossed p=0.036 → p=0.053 with it.
Nothing about the signal changed — only how the bars were sliced. Real, but not
robust; that fragility is why it sits on PROBATION rather than ACTIVE.

`greeks` went **86.6% long on VALIDATE against 56.4% on TRAIN**: `SKEW_NEUTRAL`
was calibrated on TRAIN and does not hold in 2024, so the lens is measuring its
own constant rather than the skew.

### 3.12 A correlated lens is not a second opinion

|            | volume_oi | vwap | ict_smc | greeks |
|---|---|---|---|---|
| volume_oi  | 1.000 | **−0.769** | −0.285 | −0.178 |
| vwap       | −0.769 | 1.000 | 0.394 | 0.230 |

`vwap` agrees with `volume_oi` on **18.4%** of decisions — it is substantially
volume_oi's volume-profile component read with the opposite sign convention, not
an independent reading. Its negative result is therefore *not* separate evidence
against the champion, and weighting it would double-count one opinion.

Measure pairwise correlation **before** assigning weights. A vote cannot detect
this on its own: it sees N opinions and has no way to know it is being handed
the same one twice.

### 3.13 The combined vote did not beat the best single lens

| scheme | VALIDATE | vs champion |
|---|---|---|
| `volume_oi` alone | +1.49 bps p=0.0527 | — |
| `equal` weights | +0.22 bps p=0.7656 | −1.27 |
| `positive_only` | +1.49 bps p=0.0527 | +0.00 (collapses to the champion) |
| `train_signed` | +1.90 bps p=0.0139 | +0.40 |

Equal weighting **destroys** the edge — three negative lenses outvote one
positive one. `positive_only` is identical to the champion by construction.

`train_signed` (each lens's TRAIN sign as its convention, weight |edge|) looks
like the combination finally working. It is not, and the three checks that killed
it are the reusable part:

1. **It agreed with `volume_oi` alone on 82.5% of decisions.** Sign-flipping a
   −0.77-correlated lens makes it a +0.77-correlated copy, so the "combination"
   is the champion levered up.
2. **It gave 48.5% of its weight to `ict_smc`, the worst lens**, because
   weighting by |edge| rewards whichever lens was most *wrong*.
3. **With `volume_oi` removed, the three flipped lenses scored VALIDATE +0.91
   bps at p=0.2118** — nothing.

By that point VALIDATE had been looked at ~10 times, which puts p=0.0139 inside
what multiple comparisons produce by chance. **Do not flip a lens's sign after
seeing its result** — §2.3's discipline applies to conventions, not just costs.
The convention is stated before the measurement or the measurement is worthless.

### 3.14 The roster measurement left the aggregator unable to trade

`MIN_VOTING_LENSES = 2`, and exactly one lens now carries weight. Verified on
10 VALIDATE sessions: **129 decisions, 0 executed**, every one rejected with
`only 1 lens(es) with weight could read this snapshot, need 2`.

This is the guard working, not a bug — but it means the multi-lens system is
structurally a no-op until either a second lens earns weight or the quorum is
deliberately lowered to 1, which converts it into a single-lens system that
happens to journal four opinions. That is a capital decision, not a config
tidy-up, and it is left to explicit review.

### 3.15 A rejected lens is not secretly a good filter

The obvious way to give a measured-negative lens a job is to stop asking it
*which way* and start asking it *whether* — let it gate the good lens instead of
voting alongside it. Averaging cannot express that, so it was worth a real test:
**18 gates**, each scored on the subset it admits, against two nulls — the
ungated baseline, and **a random subset of the same size**. The second null is
the one that matters. Any filter admitting fewer, higher-conviction bars shows a
higher mean; the question is whether it beats coin-flipping its way to the same
sample size.

| gate | TRAIN | boot p | VALIDATE | boot p |
|---|---|---|---|---|
| `ict_smc` flipped-confirms | **+4.82** | **0.0010** | +0.55 | 0.7478 |
| `vwap` flipped-confirms | +2.93 | 0.0168 | +1.01 | 0.7097 |
| no lens contradicts | +2.36 | 0.0395 | +2.47 | 0.0703 |
| **own confidence, top third** | **+2.42** | 0.1472 | **+3.87** | **0.0125** |

`ict_smc` — the *worst* lens as a voter — looked like the best gate in the set at
bootstrap p=0.0010, and **evaporated to +0.55 bps (p=0.75) on VALIDATE**. So did
`vwap`. A lens with no measured edge does not secretly know when the good lens is
wrong; it only looked that way on the split the rule was chosen from.

The one rule that survived is the least interesting and the most robust: **trade
only `volume_oi`'s top confidence tercile** (cut 0.414). Positive in both splits,
signs agreeing, on 888 and 513 observations, and it is the lens's *own*
confidence — no foreign lens, no fitted interaction, one parameter. It does not
clear Bonferroni across 18 gates (p<0.00278), so it ships as a PROBATION-grade
rule to be re-measured live, not as an established fact.

**Selection criterion, disclosed:** picking by raw edge chose `ATR high tercile`
(+8.76 bps) whose VALIDATE sample was **n=81** — the TRAIN-fitted ATR threshold
kept 11% of TRAIN but only 6% of VALIDATE. Rather than quietly re-pick by a
better criterion and present one clean shot, all 18 gates were then scored on
VALIDATE and the whole table published. A criterion changed after seeing results
is not a criterion.

### 3.16 Deliberation and the journal did not beat trading fewer bars

The council was built in four arms, each adding exactly one mechanism, all four
driven from byte-identical inputs:

| arm | TRAIN | VALIDATE | n (VALID) | vs its parent |
|---|---|---|---|---|
| A lead only | +1.66 | +1.49 | 1380 | — |
| B + conviction gate | +2.60 | **+3.70** | 503 | **p=0.0203 SELECTS** |
| C + deliberation | +2.43 | +4.14 | 450 | p=0.2125 |
| D + journal | +2.61 | **+5.01** | 332 | p=0.1970 |

Read the VALIDATE column alone and every mechanism pays, ending at +5.01 bps —
**3.4× the ungated baseline**. That reading is wrong, and the way it is wrong is
the point of this entry: *each arm also trades fewer bars.* Every arm is a strict
subset of the one above it, so the parent is the exact null, and against a random
subset of its parent of the same size **only the conviction gate clears**.
Deliberation's +0.44 bps is what dropping 53 trades at random gives you; the
journal's +0.86 is what dropping 118 gives you.

Neither is *harmful* — and with n falling to 332, this is low power, so absence
of evidence is weak evidence of absence. But neither has earned the right to move
capital.

**Disposition:** `COUNCIL_DELIBERATION_BINDING = False`. Round 1 still runs, is
still journaled, and still renders the transcript the operator reads on the left
panel — the lenses visibly argue — but the traded decision comes from the
independent round plus the conviction gate. This is the SHADOW rule that governs
*lenses* (§3.11) applied to a *mechanism*: present and auditable from day one,
load-bearing only once live attribution earns it. Flip the flag when
deliberation beats its parent on live closed trades.

The end-of-day journal enforces its own no-lookahead rule in the loader
(`for_session` is a strict `$lt`, and `ReplayJournals` mirrors it), because a
session that can read its own summary is §1.2 wearing a different hat.

### 3.17 Roster expanded to 8 lenses: diversity achieved, edge not

Three lenses were added on information no existing lens read — `smile` (IV
curvature/butterfly), `momentum` (ATR range breakout), `liquidity` (`no_trade`
density and volume concentration).

| lens | TRAIN | VALIDATE | verdict |
|---|---|---|---|
| `smile` | +0.53 p=0.64 | +0.18 p=0.87 | no edge |
| `momentum` | +1.38 p=0.48 | −1.21 p=0.77 | no edge |
| `liquidity` | context lens | splits contradict | no signal |

**The diversity is real.** `smile` and `momentum` correlate with the entire
existing roster at |r| ≤ 0.29, and with each other at exactly 0.000 — genuinely
independent readings, unlike `vwap`, which was a −0.769 echo of `volume_oi`.
The architecture produces distinct opinions. Distinct opinions are not edge.

**`momentum` is a valuable negative.** It is adjacent to inverting `vwap`'s
significantly-negative mean-reversion result, and it does not show positive even
in-sample. That closes the "trend was the right convention" hypothesis instead
of leaving it as a standing temptation.

#### The same calibration bug, twice, in two different lenses

`smile`'s first measurement returned **n=34 directional verdicts across three
years** and was not a measurement at all. `BUTTERFLY_NEUTRAL` was hardcoded to
0.0 on the assumption that cheap wings means wings below ATM — but a normal
equity smile has wings *above* ATM, and butterfly was positive in **97.7%** of
TRAIN observations. The lens returned NEUTRAL almost always.

That is precisely the bug `greeks` already hit and fixed with
`SKEW_NEUTRAL = -0.2098` (rr_norm negative in 98.4% of observations, §3.5), in a
file sitting next to it, with the fix documented. It was rebuilt anyway.

`liquidity` failed the same way from the other direction: an absolute
`CONCENTRATED_HHI = 0.25` against an archive whose p95 is 0.113, so breadth was
pinned at 1.0 on every snapshot and 99.7% of the tape rated "liquid".

**The rule, stated so a third lens does not pay for it: neutral is wherever the
market actually sits, never zero, and it must be MEASURED before the pivot is
chosen.** A normalised quantity being centred on zero is an assumption about the
market, not a property of normalisation.

Even after recalibrating `liquidity` to TRAIN percentiles the splits contradict
each other — TRAIN favours the middle half (+2.05), VALIDATE the top quartile
(+3.27) — and the lens's own score distribution drifted (TRAIN top quartile
>0.28 vs VALIDATE >0.51). A percentile fitted on one period is still a constant
in the next.

#### Standing count

**8 lenses built, 1 with a measured edge.** Adding lenses is cheap and the
roster is designed to make it cheap — a new lens votes at weight 0 until
attribution promotes it, so a bad idea costs a journal entry rather than money.
But the cost of a *badly calibrated* lens is worse than zero: it produces a
number that looks like a measurement and is not, which is how `smile` nearly
entered the record as "tested, no edge" on n=34.

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
4. ~~Re-measure the variance premium including overnight gaps~~ **DONE** — see
   §3.3 and `docs/OPTIONS_GREEKS_LEARNINGS.md` §6. Premium ~+5.1, not +8.64.
5. Design the defined-risk structure for the variance premium — now known to
   need overnight-spanning defined risk (close-to-close kurtosis 13.75)
6. Test the breakout-retest on BTC/ETH/XAUT
7. Surface the journal on the frontend
8. `ENABLE_NSE_RUNNER=false` until the synthetic forward is replaced
9. Re-run any backtest that used an assumed Thursday expiry (§1.8)
10. Live Greeks must reprice at ≤2 DTE — stored Greek vectors are up to 100%
    wrong there (`OPTIONS_GREEKS_LEARNINGS.md` §3)
