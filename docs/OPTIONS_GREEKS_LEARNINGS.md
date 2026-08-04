# Options, Greeks, expiry and volatility — what we verified and what was wrong

Written 2026-08-04. Companion to `RESEARCH_LEARNINGS.md`, which covers
backtesting method. This one covers **options mathematics**: Greeks, expiry
mechanics, and the volatility numbers we size positions with.

Everything below is measured against our own 1,253 sessions of NIFTY 1-minute
option data (2021-01-01 → 2026-05-21). Where a widely repeated rule turned out
to be right, that is stated too — the point is to know which is which.

Reproduce with:

    python -m nse.quant.expiry_calendar
    python -m nse.quant.greeks_taylor_check
    python -m nse.quant.test_vix_coverage

---

## 1. The expiry weekday changed mid-dataset

This is the single most consequential fact in this file, because **every Greek
is a function of T**. Get the expiry date wrong and delta, gamma, theta and IV
are all wrong together, silently, and in the same direction.

Measured from the data (`nse/quant/expiry_calendar.py`), no calendar assumed:

| year | expiries | Mon | Tue | Wed | Thu |
|---|---|---|---|---|---|
| 2021 | 52 | 0 | 0 | 4 | 48 |
| 2022 | 48 | 0 | 0 | 1 | 47 |
| 2023 | 48 | 0 | 0 | 3 | 45 |
| 2024 | 48 | 0 | 0 | 2 | 46 |
| 2025 | 48 | 1 | **13** | 2 | 32 |
| 2026 | 20 | 3 | **17** | 0 | 0 |

**Changeover, measured: last Thursday expiry 2025-08-28 → first Tuesday expiry
2025-09-02.** The stray Mondays and Wednesdays are holiday-shifted expiries.

This is the **same class of error as the hardcoded lot size** (`RESEARCH_LEARNINGS`
§1.8): a constant that was true when written and silently wrong later. Any code
with `weekday == 3` in it is wrong for the last ~9 months of our data. Use
`dte_for(date)`, which is measured per session.

### How the calendar is derived, and the mistake made deriving it

On expiry day an ATM straddle has no time value left, so it collapses near the
close. First attempt used a level threshold, `straddle/spot < 0.50%`, and found
**318 expiries — 1.27 per week, which is impossible.**

The extras were **1-DTE Wednesdays**. In a low-IV regime a Wednesday straddle
falls to ~0.5% and lands on top of the expiry population. The two populations
genuinely overlap in *level*, so no threshold separates them.

The fix was to detect the **shape** instead. The straddle series is a sawtooth
and expiry is its local minimum:

    expiry  <=>  straddle/spot < 60% of BOTH neighbouring sessions

Scale-free, so it survives IV regimes and the 14k → 26k move in spot, and it
catches holiday shifts for free because it never mentions a weekday.

**What made the fix trustworthy** was not the reasoning, it was two checks:

- the rate became 1.05 expiries/week, which is physically possible;
- an unrelated absolute rule (`< 0.30%`) selected **exactly the same 264
  sessions** — zero disagreement.

A real gap only appeared *after* the fix: max detected 0.299% vs min rejected
0.324%. Before it, the threshold was doing the work rather than the data.

> **Rule:** when a detector needs a tuned constant, prefer one that keys on
> shape. And always print a sanity rate — "1.27 expiries per week" is a bug
> report that needs no domain knowledge to read.

---

## 2. Greeks do not add up the way the note said

A quant note supplied this worked example: NIFTY 24000, 24200 CE, 1 DTE, VIX
15.87%, premium ₹60, delta 0.30, gamma 0.005, theta −15/day, vega 8. Nifty
moves to 24100 and IV drops 1 point. It concludes the option is worth ₹69.50.

Checked against Black-Scholes, there are three errors.

### 2.1 The Greeks do not belong to the contract

| quantity | as stated | Black-Scholes | |
|---|---|---|---|
| premium | 60.00 | **17.45** | 244% off |
| delta | 0.300 | **0.166** | 81% off |
| gamma | 0.0050 | **0.00125** | 300% off |
| theta | −15.00 | **−25.56** | 41% off |
| vega | 8.00 | **3.13** | 156% off |

No single (S, K, T, σ) produces that set. Greeks are **not free parameters** —
all five come out of the same five inputs, so they must be generated together.
Hand-assembled Greeks describe a contract that does not exist.

### 2.2 The gamma term is mis-multiplied

    0.5 × gamma × dS²  =  0.5 × 0.005 × 100²  =  25.00

The note carries **2.50**. A factor of ten. Corrected total: ₹92.00, not ₹69.50.

### 2.3 The step takes T to zero — this is the expensive one

The contract has **1 day left** and the example advances **one day**. So the
option expires. At 24100, a 24200 call is out of the money.

    Greek-sum says      ₹92.00
    true expiry value   ₹0.00
    error               ₹92.00  — the entire position

Taylor expansions describe a **local** slope. Expiry is not local: gamma and
theta are singular at T→0, so no number of correction terms rescues it. **At
expiry you do not approximate, you settle.**

---

## 3. Where the Greek-sum shortcut is actually valid

`new = old + Δ·dS + ½Γ·dS² + Θ·dt + ν·dIV` is a second-order Taylor expansion.
Error vs exact reprice, ATM NIFTY call, one session elapsing, IV unchanged:

| DTE | premium | +10 | +25 | +50 | +100 | +200 | +400 |
|---|---|---|---|---|---|---|---|
| 30 | 483.04 | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0.1% |
| 15 | 326.49 | 0.1% | 0.1% | 0.1% | 0.1% | 0.1% | 0.3% |
| 7 | 215.27 | 0.3% | 0.3% | 0.3% | 0.2% | 0.1% | 0.4% |
| 3 | 137.18 | 1.9% | 1.8% | 1.5% | 0.8% | −0.4% | 0.5% |
| 2 | 110.96 | 5.5% | 4.9% | 3.8% | 1.6% | −1.3% | 0.6% |
| 1 | 77.49 | **328.0%** | **104.1%** | 31.6% | −0.7% | −8.9% | 2.9% |

**The pattern is not "bigger move = worse".** Read the DTE-1 row: +25 gives
+104%, +100 gives −0.7%, +400 gives +2.9%. Error is driven by how much of the
premium is **optionality**, not by move size. Far from the strike an option is
nearly linear (delta pinned at 0 or 1) and the expansion is exact again. The
blow-up is **near the strike with little time left**, where true value collapses
to intrinsic but the Greek-sum keeps paying for time value that is gone.

**Nor is the error one-signed** — +104% in one cell, −0.7% in the next. So
"add a safety margin" does not fix it.

| DTE | verdict |
|---|---|
| ≥ 7 | error < 0.4% even on a 400-point move — Greek-sum is fine |
| 2–3 | a few % near the money — usable for risk, not for P&L |
| ≤ 1 | up to 100%+ near the money — **always reprice** |

**Our NIFTY trading sits at 0–2 DTE, entirely inside the region where the
shortcut fails.** Any live Greeks display must come from a repricer, not a
stored Greek vector.

---

## 4. The VIX → daily move rule is correct (with one condition)

The widely repeated `daily σ = VIX / √252` is **right**, and the derivation
matters because the two conventions look incompatible:

    India VIX 16%, annualised over 30 CALENDAR days
      over 30 calendar days   16 × √(30/365)      = 4.587%
      trading days in those   30 × 252/365        = 20.7
      per trading day         4.587 / √20.7       = 1.008%
      shortcut                16 / √252           = 1.008%

They agree exactly, because `√(30/365) / √(30·252/365) ≡ 1/√252` — the calendar
and trading-day factors cancel.

**The condition:** the answer is a move per **trading day**. Applying it to a
calendar day, or comparing it against a realised vol computed on calendar days,
double-counts weekends. That is not hypothetical — §5 is exactly that bug.

---

## 5. The 68.2% band is wrong on our data, and the direction matters

The claim "with 68.2% confidence NIFTY stays inside ±1σ" assumes **normal**
returns. Measured on 1,250 sessions using ATM IV observed at 09:30:

| band | normal says | actual | TRAIN | VALID | TEST |
|---|---|---|---|---|---|
| ±1 sd | 68.3% | **90.4%** | 91.9% | 89.9% | 87.6% |
| ±2 sd | 95.4% | 99.5% | 99.7% | 98.7% | 99.7% |
| ±3 sd | 99.7% | 100.0% | 100.0% | 100.0% | 100.0% |

Coverage far **above** normal means the band is too **wide** — the option market
charged for more movement than the index delivered. That is the variance risk
premium, arriving by a second and completely independent route from §3.3 of
`RESEARCH_LEARNINGS`.

---

## 6. The overnight gap — closing open item §4, and it changes the answer

`RESEARCH_LEARNINGS` §3.3 caveated that our realised vol was intraday-only
while implied prices the whole calendar. That caveat was worth **41% of the
measured edge**. Variance decomposition over 1,249 sessions:

| component | std dev | share of variance |
|---|---|---|
| overnight (close→open) | 0.608% | **42.3%** |
| intraday (open→close) | 0.700% | 56.1% |
| cross term | — | 1.6% |
| **close-to-close total** | **0.934%** | 100% |

**Intraday-only realised vol understates true volatility by 1.335×.**

### Corrected variance premium

| year | predicted σ | intraday RV | true c2c RV | old ratio | **true ratio** |
|---|---|---|---|---|---|
| 2021 | 1.303% | 0.791% | 0.995% | 0.61 | **0.76** |
| 2022 | 1.472% | 0.854% | 1.134% | 0.58 | **0.77** |
| 2023 | 0.901% | 0.486% | 0.618% | 0.54 | **0.69** |
| 2024 | 1.158% | 0.720% | 0.978% | 0.62 | **0.84** |
| 2025 | 0.964% | 0.575% | 0.760% | 0.60 | **0.79** |
| 2026 | 1.143% | 0.711% | 1.131% | 0.62 | **0.99** |

Two things follow, and the second is a warning:

1. The premium **is still real** — the ratio is below 1.00 in every year. But
   it is roughly **35% smaller** than we published. Applied to §3.3's TRAIN
   figures, the premium falls from **+8.64 vol points to about +5.1**.
2. **In 2026 the ratio is 0.99 — the premium has essentially vanished.** On
   94 sessions that is not conclusive, but it is the most recent regime and it
   is the one a live book would be trading. Any short-vol deployment must
   monitor this ratio continuously, not assume the historical average.

### Where the fat tail actually lives

| measure | kurtosis | worst day | beyond 3sd |
|---|---|---|---|
| intraday only | **0.98** | −2.69 sd | 0.00% |
| close-to-close | **13.75** | −3.73 sd | 0.40% |

Normalised by its own IV, NIFTY's **intraday** distribution is close to normal —
kurtosis 0.98, and it never once reached 3σ in 1,249 sessions. The famous fat
tail is **entirely in the overnight gap**.

This has a direct consequence: **an intraday-only backtest is structurally blind
to the risk that kills short-vol books.** Ours were. Every "sell premium and go
home flat" result in this repo measured the 0.98-kurtosis world and never saw
the 13.75-kurtosis one.

### A timing bug found while measuring this

The first version predicted the close-to-close move using **today's 09:30 IV**.
But close-to-close starts *last evening*, so that IV already knew the gap had
happened — it is elevated precisely on large-move days. Lookahead.

Fixing it (use the **previous** session's IV) barely moved the ratios but
**doubled the measured kurtosis, 6.67 → 13.75**, and pushed the worst day from
−3.00 to −3.73 sd.

> **Rule:** a forecast must be built only from information that existed before
> the window opened. Same-day IV hid more than half the tail — and the tail was
> the thing being measured.

---

## 7. What this means for strategy

- **Delta-hedged short gamma is not a separate strategy from the variance
  premium — it is the mechanism.** Rebalancing a short-gamma book always buys
  high and sells low; the realised cost of that hedging is what the premium
  pays for. The edge is `implied² − realised²`, which is exactly the ratio
  table in §6.
- Sizing a short-vol book off the intraday ratio (0.58) instead of the true one
  (0.79) would **over-lever it by roughly a third**, in a strategy whose failure
  mode is a single gap.
- **Never naked.** Kurtosis 13.75 with a −3.73σ day on record. Defined-risk
  structures only, and the defined risk must span the overnight session.
- The 2026 ratio of 0.99 says the premium may currently be gone. Measure before
  deploying.

---

## 8. Applying this to the "5K → 50K" breakout-retest

The headline for that strategy was **VALID −₹80,769**, computed treating every
session alike. But a long option pays theta, and theta accelerates into expiry:
on expiry day an ATM option is almost pure time value burning to zero within
hours, so the same index move must be far larger to pay for the decay it fights.

That suggests a specific, falsifiable question: **does it lose on direction, or
on theta?** If the losses concentrate near expiry, the entry may be sound and
the instrument wrong — a fixable problem. Split by *measured* sessions-to-expiry
(`--by-dte`, SL 20, 3R, ₹180–200 band, ₹50k budget):

| DTE | trades | WR | avg P&L | total | TRAIN | VALID | TEST |
|---|---|---|---|---|---|---|---|
| 0 | 27 | 26% | −854 | −23,046 | −7,394 | −23,361 | +7,709 |
| 1 | 28 | 29% | −1,927 | **−53,970** | −120 | −42,708 | −11,141 |
| 2 | 44 | 30% | −219 | −9,630 | −17,378 | +20,582 | −12,834 |
| 3 | 58 | 40% | +1,345 | **+78,007** | +34,535 | −7,820 | +51,292 |
| 4 | 32 | 28% | −237 | −7,569 | −13,764 | +13,429 | −7,235 |
| **all** | **192** | **32%** | **−27** | **−5,249** | −4,121 | −39,878 | +38,750 |

**The theta hypothesis is wrong, and the filter does not save it.**

- Losses are **not** concentrated at 0 DTE — **1 DTE is twice as bad**. So this
  is not a decay artifact that an expiry filter removes.
- The one profitable bucket, 3 DTE at +₹78,007, **fails the hold-out**
  (VALID −₹7,820). With eight buckets tested, one looking good is exactly what
  chance produces — the same trap as `MR rsi 15/85` in `RESEARCH_LEARNINGS` §2.1.
- Dropping expiry day entirely still leaves **VALID −₹16,517**.
- The 5/8/9-DTE rows have n=1 (holiday gaps) and mean nothing.

**Verdict: the entry signal has no edge; the instrument was not the problem.**
This is a negative result and it is worth as much as a positive one, because it
closes off "just avoid expiry day" as a rescue and stops further tuning here.

---

## 9. The variance premium traded — the first thing here that survives

`nse/backtest/test_delta_hedged_vol.py`. Short ATM straddle on **recorded**
prices, 1,251 sessions, index points per unit (×65 = rupees per lot), gross.

| variant | n | win | mean | sd | worst | kurt | TRAIN | VALID | TEST | BE/leg |
|---|---|---|---|---|---|---|---|---|---|---|
| naked, unhedged | 1243 | 64% | 4.49 | 56.3 | −386 | 6.5 | 3.08 | 3.66 | 8.17 | 2.25 |
| naked, delta-hedged | 1243 | 65% | **7.14** | 43.7 | −399 | 13.3 | 7.24 | 6.93 | 7.06 | 3.57 |
| iron fly ±200, unhedged | 1212 | 61% | 1.70 | 30.3 | −131 | 4.6 | 0.02 | 3.13 | 4.47 | 0.42 |
| overnight naked | 973 | 70% | 4.21 | 35.5 | −475 | **42.5** | 2.45 | 3.15 | 8.85 | 2.11 |

**Every variant is positive in TRAIN, VALID and TEST** — the first time anything
in this repo has done that. The delta-hedged straddle is also remarkably stable
across splits (7.24 / 6.93 / 7.06), which is what a real structural edge looks
like as opposed to a fitted one.

Confirmation it is the mechanism we think it is: P&L by |move| decile is
positive in deciles 0–8 and −18.7 in decile 9. That is textbook short gamma —
it earns on quiet days and pays on violent ones.

### The mistake I made inside this test

The two worst delta-hedged sessions:

    2024-02-29  move +0.39%   opt +114.0   hedge -512.8   net -398.8
    2025-10-20  move -0.07%   opt +115.8   hedge -303.5   net -187.7

**The options made money. The hedge lost 3–5× the credit, on days the index
barely moved.** Both are **0 DTE** — and this file computes hedge deltas from
Black-Scholes at T→0, which §3 of this very document says is unusable. I
documented the rule and then broke it one file later.

The mechanism is short-gamma whipsaw: at 0 DTE gamma is enormous, delta flips
violently around the strike, and every rebalance buys high and sells low.

### What survives once that is fixed and hedging is charged for

| variant | n | mean | sd | worst | kurt | TRAIN | VALID | TEST | BE/leg |
|---|---|---|---|---|---|---|---|---|---|
| naked unhedged, **ex-0DTE** | 981 | 3.38 | 44.6 | −299 | 7.9 | 3.02 | 3.87 | 3.81 | **1.69** |
| naked hedged, ex-0DTE | 981 | 4.53 | 28.8 | −184 | 7.0 | 4.72 | 5.96 | 3.11 | 2.26 |
| naked hedged, ex-0DTE, **1pt/rebalance** | 981 | 1.64 | 29.3 | −186 | 6.7 | 1.75 | 3.26 | **0.25** | 0.82 |
| iron fly, ex-0DTE | 957 | 0.88 | 12.1 | −67 | 4.8 | 0.01 | 2.39 | 1.75 | 0.22 |

- Dropping 0 DTE **halves the tail** (worst −399 → −184, kurtosis 13.3 → 7.0).
- Hedging averages only 3.2 rebalances/session, but at 1 point each the edge
  falls from 4.53 to 1.64 and **TEST collapses to 0.25**. The hedged version is
  far more cost-sensitive than its headline suggests.
- The **simplest** variant is the most robust: unhedged, skip expiry day,
  3.02 / 3.87 / 3.81 across splits with BE/leg 1.69 points — above our
  estimated 0.5–1.35 point spread, though not by a comfortable margin.

### The catch, and it is a big one

**The edge lives in the naked structure, which we cannot margin.** A −299 point
day is **−₹19,400 per lot**; on 4 lots that exceeds the entire ₹50k budget.
Short straddles also carry SPAN margin far above the premium collected — this
must be checked against Angel's margin API before any of this is deployable,
but the direction is not in doubt.

The structure we *can* afford — any iron fly — is **negative before costs**
(see the corrected wing sweep above). So this is worse than the crypto finding:
there, a real edge sat under its cost floor. Here the edge exists only in the
structure we cannot margin, and every affordable version of it loses money.

### The wing sweep, and a selection bias that manufactured an edge

Looking for a structure between "naked, unaffordable" and "±200 fly, no edge",
I swept wing width. The first run looked spectacular — and was entirely false:

| wings | n | mean | worst | verdict |
|---|---|---|---|---|
| naked | 981 | 3.38 | −298.6 | |
| ±200 | 957 | 0.88 | −67.1 | 24 sessions dropped |
| ±400 | 704 | **10.98** | −48.5 | **277 dropped** |
| ±500 | 169 | **19.67** | −34.8 | **812 dropped** |

Mean rising while worst-case *falls* is impossible for a wider fly — a wider
wing means more risk, not less. The `n` column was the tell.

**The wing strike goes missing precisely on the days the index moves**, because
the recorded ladder re-centres intraday: the strike is quoted at 09:30 and gone
by 15:20 on a big move. Dropping those sessions silently discards the losing
days.

    wings +/-400   dropped sessions  naked P&L -36.60,  |move| 1.006%
                   kept sessions     naked P&L +19.11,  |move| 0.285%
    wings +/-500   kept sessions     |move| 0.085%  -- only dead-flat days

Two attempts were needed. Clamping to strikes available **at entry** changed
nothing (`n` identical) — the strikes were all present at 09:30. The diagnostic
that found it counted the failure reasons: `strike_absent 0, not_traded 0,
zero_px 0, exit_absent 279`. Clamping to the **intersection of entry and exit**
strikes fixed it.

Corrected, with all 981 sessions retained:

| wings | n | mean | worst | kurt | TRAIN | VALID | TEST |
|---|---|---|---|---|---|---|---|
| naked | 981 | **+3.38** | −298.6 | 7.9 | 3.02 | 3.87 | 3.81 |
| ±100 | 981 | −0.08 | −51.8 | 22.0 | −0.30 | 0.26 | 0.14 |
| ±200 | 981 | −0.33 | −103.4 | 8.6 | −0.64 | −0.04 | 0.15 |
| ±300 | 981 | −1.07 | −174.2 | 8.2 | −1.19 | −1.49 | −0.52 |
| ±400 | 981 | −2.66 | −236.9 | 8.0 | −2.09 | −4.19 | −2.81 |
| ±500 | 981 | −4.85 | −289.3 | 8.5 | −3.53 | −7.38 | −5.93 |

**Every defined-risk variant loses money before costs**, monotonically worse
with wing width — and that monotonicity is itself evidence the result is real.

The economics are now clear and they are not a coincidence. Far-OTM wings are
expensive *because* of the fat tail measured in §6: the market charges for gap
risk, so you **pay** the variance premium on the wings while **collecting** it
on the body. Buying protection hands back more than it saves.

> **Rule:** whenever a variant sweep changes the sample size, the sweep is
> measuring the sample, not the variant. Put `n` next to every result.

### Which expiry, and why ATM rather than ITM

Both verified rather than assumed:

- **Expiry: the nearest weekly.** Calendar days-to-expiry across the traded
  sessions is 1–6 for 972 of 981, never the next weekly or the monthly.
- **ATM is correct by construction for a seller.** Extrinsic value — the only
  part a premium seller harvests — peaks hard at the money:

| strike | CE price | CE **extrinsic** | PE price | PE **extrinsic** |
|---|---|---|---|---|
| ITM −300 | 344.0 | 43.8 | 35.6 | 35.6 |
| ITM −100 | 185.8 | 85.6 | 77.1 | 77.1 |
| **ATM** | 124.0 | **117.3** | 115.3 | **108.8** |
| OTM +100 | 78.1 | 78.1 | 169.5 | 69.7 |
| OTM +300 | 29.0 | 29.0 | 319.9 | 20.1 |

  Selling an ITM call collects ₹344 but only **₹43.8** of it is time value; the
  other ₹300 is intrinsic, which is a pure directional bet carrying no
  volatility edge. Liquidity does not decide it — every strike above traded in
  100% of the sampled minutes. ATM harvests **2.7× more** sellable premium than
  ITM −300. (Note this is the opposite conclusion to the ₹180–200 *buying*
  band, which selected ITM at delta 0.80 — buyers want delta, sellers want
  extrinsic.)

**Status: the most promising thing measured here, and not yet tradeable.** The
next step is neither more tuning nor more architecture — it is measuring the
actual bid/ask on ATM weeklies (collection was fixed 2026-08-04) and the real
SPAN margin, because those two numbers decide it and both are now collectable.

---

## 10. Corrections to make elsewhere in the repo

1. `RESEARCH_LEARNINGS.md` §3.3 — premium ~+5.1 vol points, not +8.64. **Done.**
2. Any DTE/expiry logic assuming Thursday — use `nse.quant.expiry_calendar`.
3. Live Greeks must reprice at ≤ 2 DTE rather than reuse a stored vector.
4. Journal feature selection still spent TEST (`RESEARCH_LEARNINGS` §1.9) — open.
5. Date-indexed NIFTY lot-size table — still open, same class as §1.
