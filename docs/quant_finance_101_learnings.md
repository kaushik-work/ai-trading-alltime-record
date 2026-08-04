# Quantitative Finance 101 — Key Learnings (Distilled)

Distilled from the YouTube playlist *Quantitative Finance 101* (FEBS, IIT
Bhubaneswar; lecturer Ajit Kulkarni, quant risk researcher). 11 of 12 sessions
covered via transcripts (Session 7 has no subtitles). Per-session detail:
`docs/quant_finance_101_notes.md`.

---

## 1. The one big idea: two probability measures

The same market process carries two measures:

| | **P — physical measure** | **Q — risk-neutral measure** |
|---|---|---|
| What it is | Real-world odds, estimated from history | Pricing measure implied by no-arbitrage |
| Expected stock return | Risk-free + risk premium (CAPM) | Risk-free rate only |
| Used for | Directional bets, alpha, risk management | Derivatives pricing & hedging |
| Needs historical data? | Yes — and all estimation bias lives here | **No** |
| Who works in it | Hedge funds, prop, mutual funds | Investment banks, derivative desks, market makers |

Pre-1970s finance tried to estimate P to price options and failed. The
Black–Scholes-era result: pricing under Q is provably correct and the true P
is **redundant for pricing**. "P is for making money; derivatives are for
hedging."

The industry splits along this line: risk-management jobs (banks) live in P —
high volume, lower pay; derivative pricing/quant teams live in Q — small
teams, high pay, higher career risk.

## 2. The theory chain (as built across sessions)

1. **Probability space (Ω, ℱ, P)** — sample space, σ-algebra axioms, and
   **filtration** ℱ₀ ⊂ ℱ₁ ⊂ … as accumulated information, built on a
   3-coin-toss tree. Same filtration underlies both P and Q; only the event→
   [0,1] mapping differs.
2. **Radon–Nikodym derivative** — Z(ωᵢ) = qᵢ/pᵢ, so
   **E_Q[X] = E_P[(qᵢ/pᵢ)·X]**. Change of measure; foundation of Girsanov.
   "The central concept of quant finance."
3. **Random walk & Gambler's Ruin** — S_T = ΣXᵢ, Xᵢ = ±1. Ruin/win
   probabilities from the recursion pᵢ = p·pᵢ₊₁ + q·pᵢ₋₁ → geometric series →
   **pᵢ = [1 − (q/p)ⁱ] / [1 − (q/p)^N]** (fair game: pᵢ = i/N).
   Lesson: even in a *fair* game, the bigger bankroll wins almost surely —
   capital adequacy and sizing dominate edge.
4. **Markov vs martingale** — Markov: next state depends only on current
   state. Martingale: E[X_{t+1}|ℱₜ] = Xₜ at **every** node. For fixed step
   outcomes {+L, −M} there is exactly **one** probability making the walk a
   martingale (pL = (1−p)M). → **Fundamental Theorem of Asset Pricing**:
   unique martingale measure ⇔ no arbitrage ⇔ unique derivative price.
5. **Brownian motion** — W₀ = 0; continuous paths; independent increments;
   Wₜ − Wₛ ~ N(0, t−s). Moments: E[Wₜ] = 0, Var = t, E[WₛWₜ] = min(s,t).
   Limit of a random walk as Δt → 0 (via CLT).
   **Quadratic variation: (dW)² = dt** — the term that survives in stochastic
   calculus but vanishes in ordinary calculus; the reason Itô's lemma exists.
6. **Binomial pricing** — replicate: sell option, buy Δ shares, bond for the
   residual; match payoffs in both states →
   V₀ = (1+r)⁻¹[p*·V(H) + (1−p*)·V(T)], with risk-neutral
   **p* = (1 + r − d)/(u − d)** and no-arbitrage band **d < 1+r < u**
   (a computed p* outside [0,1] flags arbitrage or bad inputs).
   CRR: u = e^(σ√(T/n)), d = e^(−σ√(T/n)).
7. **Black–Scholes PDE** — hedged portfolio Π = f − ΔS; Taylor-expand df,
   keep dW² = dt, **kill the dW term with Δ = ∂f/∂S**; the drift μ cancels:
   **∂f/∂t + ½σ²S²∂²f/∂S² + rS∂f/∂S − rf = 0**.
   Growth expectations are irrelevant to option value; only σ, r, S, K, τ
   matter. Closed form: C = S·N(d₁) − Ke^(−rτ)N(d₂),
   d₁ = [ln(S/K) + (r+σ²/2)τ]/σ√τ, d₂ = d₁ − σ√τ.
8. **Implied volatility** — "the real use of Black–Scholes is not to compute
   the option price — it is to compute IV." Vol surface is skewed (cheapest
   at ATM), flattens with expiry; historical vol is a poor forecast of future
   vol; IV is the market's forward-looking consensus. OTC pricing =
   interpolate the IV surface in strike and expiry, feed into model.
9. **Futures & cost of carry** — F = S·e^((r−q)τ); implied rate
   r = ln(F/S)·365/days is directly observable; inconsistent implied rates
   across expiries = calendar-spread arbitrage. FX: covered interest parity
   F = S·e^((r_d − r_f)τ). Contango/backwardation; F → S at expiry.
10. **Portfolio theory** — Sharpe = (μ − r_f)/σ built as "incremental return
    per incremental risk"; portfolio variance wᵀΣw — the covariance term is
    the whole game (many weight sets give the same return; pick minimum
    variance). MPT's real optimization: **fix target return, minimize risk**
    (cricket: score 300 with singles, not 50 attempted sixes).

## 3. Tradeable anomalies & strategy patterns from the course

- **Low-volatility anomaly** — Nifty 100 Low Vol 30 (Dec 2023 fact sheet):
  17.42% p.a. at β = 0.76 vs Nifty 50's 11.55% at β = 1. Lower risk, ~50%
  higher return. Market-neutral construction: short the 70 high-β names, buy
  the 30 low-vol names with proceeds, park residual cash → spread + interest
  on ~zero net market risk. Rebalance more often than the index provider.
- **Volatility-managed sizing** (Moreira & Muir, Yale) — exposure at t+1 =
  c / Var_t. Cut exposure when vol spikes (India VIX ~70 in Mar 2020), max it
  when vol is below average (VIX ~10 in Oct 2023 preceded Nifty +11% in 5
  months). Works because vol rises faster than expected returns compensate.
- **Futures term-structure signals** — implied rates across expiries must be
  consistent; large dividends invert the term structure (apparent, not real,
  arbitrage if priced with q = 0).

## 4. The discipline of quant trading (final-session warnings)

- **Every signal is a hypothesis test.** Fisher's tea-tasting template:
  random guessing gives 50%; a pattern must beat luck on *many* instances.
  Validate over 30–40 years, collect every matching instance, study the full
  distribution of outcomes — never trade one anecdote.
- **Confirmation bias** — running fundamental → technical → quant analyses
  sequentially, one analyst unconsciously bends methods 2 and 3 to confirm
  method 1. "If you torture the data enough, it will confess to anything."
  Keep analyses independent; trust only the intersection.
- **Alpha decay** — published strategies either never worked or stopped
  working (the author now monetizes the book). Real edges are never shared;
  their exploitable life is ~2–6 months and shrinking with compute/AI.
  Research is a continuous search, not a one-time find.
- **LTCM** — Scholes and Merton themselves, plus an ex-Fed vice-chairman,
  blew up. The people whose formulas run the industry could not trade
  profitably. Formulas ≠ edge.
- **EMH framing** — all available information is priced in; entries/exits are
  knowable only probabilistically. Anyone selling certainty is faking it.
- **Index backtests** — condition on the *rule* (free-float market-cap), not
  the constituents; constituent turnover is irrelevant.
- Derivatives are **risk-management instruments, not assets** — which is why
  ~3/4 of a real QF curriculum is derivatives/hedging.

## 5. Relevance to this repository

- Our deterministic-signals-only rule matches the course's P-world
  discipline: hypothesis-test every pattern on long history, expect decay,
  treat ruin math/position sizing as primary.
- The returnswealth.com analysis (`nse/backtest/alpha_replication.py`) is a
  textbook case of the course's warnings: marketed "2-yr backtested" returns
  on ATM weekly structures that ignore vol skew and realized-move magnitude;
  our backtest on real collected marks showed both structures gross-negative.
- Candidate strategy ideas to backtest in `nse/`: low-vol long–short basket,
  vol-managed sizing overlay (c/Var) on the existing crypto runner,
  futures-basis/calendar-spread monitor on NIFTY expiries.
- Greeks intuition worth keeping near the option code: near expiry Gamma
  dominates, far from expiry Vega dominates; study any payoff surface by
  **levels, slopes, curvatures**.

## 6. Books & resources recommended by the lecturer

- **John Hull**, *Options, Futures, and Other Derivatives* — the first book;
  do all exercises honestly (struggle 3–4 days per problem).
- **Steven Shreve**, *Stochastic Calculus for Finance* Vol. I (discrete) then
  Vol. II (continuous) — his top pick; "the finance equivalent of Irodov";
  solving both ≈ 60–70% of any QF course.
- **Frank Fabozzi**, *Fixed Income Securities* — bonds, ABS/MBS/CDOs.
- White papers: BIS (incl. quarterly reviews), ISDA, the 1992 Federal Reserve
  paper on complete markets (rigorous Arrow–Debreu treatment).
- Podcast: **Quantcast by Risk.net**.
- Paper to replicate: Moreira & Muir, *Volatility-Managed Portfolios*.
- Fundamental analysis: CBSE class 11–12 accountancy texts, then Aswath
  Damodaran (blog + valuation classes).
- Python stack: TA-Lib, Zipline, QuantLib, pyfolio, yfinance, Alpha Vantage,
  Quandl, BeautifulSoup.
- Certifications: FRM (risk track), CQF (closest to QF+FE), CFA (equity
  research). WorldQuant University MFE is free.
