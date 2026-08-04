# Quantitative Finance 101 — Course Notes

Source: YouTube playlist "Quantitative Finance 101" (FEBS, IIT Bhubaneswar),
lecturer Ajit Kulkarni (quant risk researcher; ex-Deloitte model-risk validator,
ex-Bank of America risk optimization, ex-Wells Fargo derivatives-pricing quant).
Guest session by Mehul Mehta (US brokerage risk modeler).

Notes distilled from auto-generated transcripts of 11 of 12 sessions.
Session 7 has no subtitles on YouTube and is not covered.

---

# Session 1: Quantitative Finance 101 — Opening Session (Ajit Kulkarni, FEBS IIT Bhubaneswar)

## Lecturer background
Model-risk validator at Deloitte → risk-optimization initiatives at Bank of America → senior derivatives-pricing quant at Wells Fargo → currently quant risk researcher at "CM group" (transcription; almost certainly CME Group). Teaching style: deliberately example-driven and non-technical first, rigor layered on later ("quant finance is a marathon, not a sprint").

## 1. Topics covered (in order)
1. Motivation: a shared paper on Brownian motion in stock markets opens with σ-algebra/filtration definitions — used to show why quant papers feel impenetrable without foundations; the course builds exactly those foundations.
2. Probability space as a triple **(Ω, ℱ, P)**.
3. Sample space Ω (coin toss, die roll, three-coin-toss experiment).
4. Filtration as *information content* over time, built explicitly on a 3-coin-toss tree.
5. σ-algebra axioms.
6. Probability measure P: ℱ → [0,1], with elementary probabilities on the coin tree.
7. Multiple measures on the same experiment — the "Ajit vs Roger Federer" tennis-match example (frequentist vs information-conditioned measure).
8. Natural (physical/real-world) measure **P** vs risk-neutral measure **Q**.
9. Insurance pricing under P: mortality tables, actuarially fair premium.
10. Stock-price expectation under P vs Q; why derivatives pricing lives in Q and needs no historical data.
11. Preview of next session: Radon–Nikodym derivative (transcribed "redon neodium") + numerical example, as the basis for Girsanov's theorem.

## 2. Key definitions & statements
- **Sample space Ω**: set of all possible outcomes of an experiment, known *before* the experiment is run. Three coin tosses: Ω = {HHH, HHT, HTH, HTT, THH, THT, TTH, TTT}.
- **Filtration {ℱₜ}**: an increasing sequence of σ-algebras, ℱ₀ ⊂ ℱ₁ ⊂ ℱ₂ ⊂ …, interpreted as accumulated information. Past information is never lost; realized events partition the tree — once the first toss is H you can never reach branches starting with T.
  - ℱ₀ = {Ω, ∅} (toss or don't toss — no resolution yet).
  - ℱ₁ = {Ω, ∅, A_H, A_T}, where A_H = {HHH, HHT, HTH, HTT} (starts with H), A_T = complement.
  - ℱ₂ adds the four two-toss atoms A_HH, A_HT, A_TH, A_TT **plus all their unions, intersections and complements** with the earlier sets.
- **σ-algebra ℱ**: Ω, ∅ ∈ ℱ; closed under complements; closed under countable unions and intersections.
- **Probability measure**: a function P: ℱ → [0,1] with P(Ω)=1, P(∅)=0. On the coin tree: P(A_H)=1/2, P(A_HH)=1/4, etc. A valid measure only requires Σᵢ p(ωᵢ) = 1 over disjoint outcomes and 0 ≤ p(ωᵢ) ≤ 1 — *any* assignment satisfying this is a legitimate measure Q on the same Ω.
- **Natural (physical) measure P**: objective/frequentist probabilities of a natural process, estimated from historical data.
- **Risk-neutral measure Q**: an alternative measure under which derivatives are priced; introduced by name only, formal construction deferred.

## 3. Formulas & reasoning
- Expectation of a stock price (discrete; he explicitly avoids integrals): **E[S] = Σᵢ Sᵢ · p(Sᵢ)**.
- Actuarially fair premium: set **E[payoff] = 0** for the insurer. Example: ₹1 Cr death benefit, fair premium p\* ≈ ₹2,500 (his number). If premium > p\* → insurer profits on average, but competitors undercut until premium converges to p\*; if premium < p\* → expected claims exceed premiums collected (selling to 1 Cr people at ₹2,000 vs paying out ₹1 Cr per death when more than 2,000 die) → insolvency. Market forces pin price to the zero-expectation value — a deliberate preview of no-arbitrage pricing logic.
- Under P, stock-up probability is genuinely p and 1−p (unknown, must be estimated). Under Q, up/down are equally likely (½, ½); he asserts both "it is ½–½" and "it is not ½–½" are true, depending on the measure. Justification given verbally: if *all* information affecting the stock is already incorporated in its price, there is no reason to prefer up over down going forward (an efficient-market-flavored statement).

## 4. Intuition & examples
- **Federer match**: Ω = {Ajit wins, loses, tie}. Naive answer ⅓–⅓–⅓ (frequentist, no information); knowing the opponent is Federer shifts it to roughly (0, 1, 0). Same experiment, different measure — information changes the measure.
- **Term insurance**: age-30 buyer, ₹1 Cr payout if dead by 60. Premium comes from the **mortality table** (P(death) tabulated by age; transcript numbers "0.89 / 0.91" are garbled) — a frequentist, historical-data object, hence a P-measure computation. ML/data science adds value by conditioning on more factors (habits, family history, comorbidities) beyond the single factor (age), shrinking estimation error of P(death) and thus of p\*.
- Fun aside: Sheldon's line — **"possibilities are not probabilities"**; possibilities generate Ω, probabilities generate the measure.

## 5. Trading-relevant takeaways
- **P vs Q is the fault line between quant finance and data science.** Derivatives pricing/hedging happens entirely under Q and requires *no* historical data or probability estimation; estimating P needs history and ML and is where biases/estimation error live.
- Pre-1970s, finance was stuck trying to find P for option pricing. The Black–Scholes-era result (1970s, stated loosely): you can price correctly assuming ½–½-type dynamics under Q, proved *rigorously*, not as an assumption — and even the true P is **redundant for pricing**.
- **P is for making money** (directional views, alpha); **derivatives are for perfect hedging**, not making money. Finding P across thousands of listed stocks is infeasible — Q sidesteps it entirely.
- The insurance premium-competition argument is the actuarial mirror of arbitrage forcing prices to risk-neutral expectations: misprice and you either bleed to competitors or go bust.

## 6. Lecturer's asides & advice
- Course logistics: math/finance in sessions; coding via assignments (plain Python — loops, if/else, a little pandas for data pulls; the goal is writing algorithms, not libraries); Wednesday doubt-clearing calls; Python is effectively a prerequisite.
- Career realism: four weeks alone won't get you a quant job ("you can't cook biryani in Maggi time"), but the foundation + his promised week-4 list of books/courses/materials + doing all exercises can.
- Where quants are used: Deloitte-style "risk advisory" consulting verticals (not strategy consulting like BCG/Bain), banks, insurers, hedge funds — and any internationally exposed corporate: BP, Shell, big agriculture, Tata Steel, Tata Motors all run derivatives/prop desks for hedging (motivation deferred to later sessions).
- Student question on ML bias in estimating P parked for Saturday; he emphasized the course will spend *no* effort finding P — only Q matters for pricing.
- Next session: Radon–Nikodym derivative with a numerical example, as the foundation for Girsanov's theorem, to be simplified rather than presented in its usual technically heavy form.

**Fidelity note:** auto-generated transcript; "fub1/FS2" = ℱ₁/ℱ₂, "five/fi" = ∅, "ah/at" = A_H/A_T, "Eto/Ito lemma", "martingale", "Gambler's Ruin", and "Brownian motion" (beyond the paper mention) do **not** appear in this session — they were not covered yet. Small interjections at a few point are garbled but no substantive content appears lost.

---

Read the full transcript (59 KB, single line — paged through in 4 chunks). Summary below.

---

# Session 02 — Introductory Session by Mehul Mehta (Guest Speaker)

**Nature of the session:** This is a **career-orientation / Q&A session**, not a technical lecture. Mehul Mehta is an industry quant (ex-Regions Bank quantitative modeler, AVP; currently risk modeler / manager at a brokerage firm, US-based). He explains what quantitative finance is, who can enter it, required skills, roles, certifications, and answers student questions. **No mathematical theorems, derivations, or formulas are presented** (no σ-algebras, martingales, Itô, Girsanov, etc. — those presumably appear in later sessions).

## 1. Topics covered (in order)

1. Speaker's career path: engineering undergrad → discovered quant finance via a professor and a US-based mentor (~6th semester) → PwC internship → Masters in the US → Regions Bank (quantitative modeler, AVP) building **deposit-balance forecasting models** for a commercial bank → current brokerage risk-modeling role (models + automation).
2. Who can enter quant finance: math/statistics, computer science, finance/economics, or engineering backgrounds.
3. Definition of quantitative finance (= financial engineering = financial mathematics = computational finance): a combination of **statistics + mathematics + economics + programming + finance**; "quantitative" because math/stats/economics are applied to finance problems.
4. Educational pathways: Masters in Quant Finance / Financial Engineering / Computational Finance; typical coursework (below); professional certifications as an alternative (FRM strongly recommended over CFA for quant roles).
5. Typical masters coursework: **Risk management** (credit risk, market risk, counterparty credit risk, liquidity risk — motivated by the 2007–08 US housing crisis); **options & derivatives**; **fixed income** (largest US market, bigger than equities/derivatives); **financial mathematics & stochastic calculus** (interest-rate models, volatility models); **statistics** (normal, binomial, Student-t, Poisson and other distributions); **econometrics/time-series** (ARMA, ARIMA, ARIMAX-style models — transcribed as "a ma ARA arax"); **programming**.
6. Skill set employers want: math & stats, financial-market knowledge (equity, bond/fixed-income, derivatives markets), programming (Python/R for modeling; C++/Java + DSA for quant-dev), algorithm/strategy development, risk-management literacy, machine learning (decision trees, random forest, XGBoost, neural networks).
7. Quant roles taxonomy (details below).
8. Programming language guidance: **Python** (preferred over R) for modelers/researchers; **C++** for quant developers because speed matters (buy/sell in milliseconds).
9. Extended student Q&A (see §6).

## 2. Key definitions stated (informal, no formal theorems)

- **Market risk** — risk associated with a position taken in the financial market ("investments are subject to market risk").
- **Credit risk** — risk that a borrower defaults on a loan from a bank. **PD (probability of default) model**: essentially a logistic-regression-style 0/1 classifier predicting default vs. no-default.
- **Operational risk** — risk tied to the bank's operations/reputation (stated loosely).
- **Liquidity risk, counterparty credit risk** — named as risk categories, not defined.
- **Alpha** — strategies/trading strategies that generate money for a financial institution; "generating alpha" = building profitable trading strategies.
- **Quant validator** — checks model assumptions (example: linear-regression assumptions; if violated and unaddressed, flag the model as unusable), data accuracy, process, and end results.
- Roles: **quant researcher** (deep math/stats; builds or improves pricing/market-prediction models from scratch), **quant developer** (CS-heavy; optimized low-latency C++/Java execution code), **quant risk analyst**, **quant validator**, **quant data scientist** (ML/NN, incl. neural nets in algo trading), **quant trader** (executes strategies, works with portfolio managers and researchers).

## 3. Formulas / quantitative content

None derived. Quantitative items mentioned in passing:

- **Log returns**: first step after downloading price data is computing (log) returns, on the assumption that returns follow a **normal distribution**.
- **Black–Scholes model and the financial Greeks** — cited as a good resume project (build a system implementing Black–Scholes pricing + Greeks).
- **Value at Risk (VaR) and Expected Shortfall** — cited as market-risk model projects.
- **PD / logistic regression default models** (credit risk).
- Time-series models (ARMA/ARIMA family) under econometrics.
- Linear regression emphasized as a centuries-old yet still heavily used bank modeling tool.

## 4. Intuition & examples

- Deposit forecasting at a commercial bank: models project how deposit balances grow/shrink; not directly market-linked but indirectly affects a listed bank.
- Trading firms work with data arriving every few seconds; 10 years of such data is far too large for Excel → Python/R needed.
- Economics background helps because models consume economic parameters (housing price index, interest rates, inflation).

## 5. Trading-relevant takeaways

- Backtesting strategies in Python is the standard industry workflow for alpha research; C++ for execution latency.
- Fixed income is the biggest US market — worth learning, not just equities/options.
- Recommended portfolio projects: credit-risk PD models (Kaggle), VaR / Expected Shortfall market-risk models, Black–Scholes + Greeks implementation, algo-trading backtests.
- FRM Part I is the recommended certification for quant/risk-track entrants (CFA suits equity research/valuation); FRM takes ~4–5 months prep per level, exam costs roughly $1,000–1,500.
- Trading-desk hiring: firms may give simulated capital/games (e.g., Futures First-style) to test alpha generation; interviews feature brain teasers, probability, and game theory.

## 6. Lecturer's asides & advice

- Quant finance is very new in India; few institutions teach it; entry salaries quoted ~₹20–25 lakh, with "massive" growth potential; in the US quants earn on par with or above software engineers.
- LinkedIn cold outreach: ~1 in 10 replies; persistence got him a mentor.
- Employers he's seen hire from: commercial banks, investment banks, trading firms, consulting firms.
- On AI replacing quants: "AI will not replace jobs; it makes jobs easier/better."
- DSA prep: high bar only for quant-dev roles (LeetCode/HackerRank; mentioned "Love Babbar" videos); risk/validation roles need only basic DSA.
- Advises the 4-week QF101 course covers core concepts from his masters; urges students to learn the models covered even if coverage is brief.
- Offered to mentor students via LinkedIn; agreed to share his slide deck.

**Fidelity note:** the transcript is heavily garbled in places (names like "Char shop", "RadonNiko"-style artifacts absent here; "arax" ≈ ARIMAX). No technical content appeared inaudible, but all numbers (salaries, FRM cost) are the speaker's off-the-cuff recollections.

---

I've read the entire transcript (84 KB, single-line file paged through in full). Here is the structured summary.

# Session 2: Random Walk, Radon–Nikodym Derivative, Gambler's Ruin

**Course:** Quantitative Finance 101 (FEBS, IIT Bhubaneswar) — Lecturer: Ajit Kulkarni

## 1. Topics covered (in order)

1. Recap / deferred questions from Session 1: relevance of **bias in the natural probability measure P**, and the **branching of quant finance from ML/data science (P vs Q)**.
2. **Radon–Nikodym derivative** (discrete case) — changing expectation from one probability measure to another.
3. Assignment Q1: construct a payoff-likelihood measure Q and verify the Radon–Nikodym identity numerically (Monte Carlo, first principles, no libraries).
4. **Stochastic process** definition; **random walk** construction from coin tosses; sample-space structure after n tosses; hitting probabilities via the **binomial distribution**.
5. **Expectation of a random walk** — when it is zero; existence of a **unique measure making it zero** → link to the **Fundamental Theorem of Asset Pricing** and unique derivative prices.
6. **Chapman–Kolmogorov-type forward relation** for transition probabilities (lecturer calls it the discrete analogue of the **Fokker–Planck equation**). Assignment Q2.
7. **Gambler's Ruin** — full analytic derivation via difference equations and geometric series. Assignment Q3 (simulation).
8. Casino/roulette interpretation; probability-of-winning surface vs initial wealth and win probability.
9. Answers to the two deferred questions: bias in P (regression analogy) and P-world (hedge funds) vs Q-world (investment banks / market makers).
10. Preview of Session 3: Brownian motion, quadratic variation, PDEs, Taylor series, Itô's lemma, stochastic calculus; Markov process vs martingale formalization deferred.

## 2. Key definitions & theorems

- **Random variable:** a function X : Ω → ℝ. Example: Ω = {H, T} mapped to {1, 2} (heads pays ₹1, tails ₹2).
- **Expectation (discrete):** E_P[X] = Σᵢ pᵢ·xᵢ over the sample space.
- **Radon–Nikodym derivative (discrete):** define the random variable Z(ωᵢ) = qᵢ/pᵢ. Then
  **E_Q[X] = E_P[(qᵢ/pᵢ)·X]** — "we are scaling our random variable with respect to the ratio of probabilities for the respective outcomes." The lecturer stresses this measure-change is *the* central concept of quant finance. The continuous version is announced but only the discrete statement is derived.
- **Stochastic process:** a time-indexed collection of random variables {X(ωᵢ, t)}.
- **Random walk:** S_T = Σᵢ₌₁^T Xᵢ, where Xᵢ = ±1 with probability ½ each (fair coin), starting at S₀ = 0.
- **Binomial connection:** with k up-steps in n tosses, position X = k·(+1) + (n−k)·(−1) = **2k − n**, and P(reaching x in n tosses) follows a binomial law, e.g. P(X₃ = −1) = C(3,2)·(½)²·(½).
- **Zero-mean random walk condition:** for steps +L with prob p and −M with prob 1−p, E = pL − (1−p)M; setting it to zero gives **L/M = p/(1−p)**, a *linear* equation in p with a **unique solution**. Hence: for any step sizes there exists a **unique probability measure** under which the random walk has zero expectation. The lecturer explicitly identifies this uniqueness with the **Fundamental Theorem of Asset Pricing**: it "ensures no arbitrage" and gives rise to unique derivative pricing (the condition for derivatives in general comes later).
- **Forward transition relation (discrete Fokker–Planck / Chapman–Kolmogorov):** P(X_{n+1} = x) = Σ over reachable y of P(X_n = y)·P(y → x). Worked example: P(X₄ = 0) = P(X₃ = −1)·½ + P(X₃ = +1)·½.
- **Gambler's Ruin setup:** gambler starts with wealth i (0 < i < N, N = 100), wins ₹1 with prob p, loses ₹1 with prob q = 1−p; ruined at 0, wins at N. pᵢ = probability of eventually winning from wealth i. Boundary conditions: **p₀ = 0** (already ruined), **p_N = 1**.

## 3. Formulas & derivations

- **Radon–Nikodym numerically:** P = (½, ½) on payoffs (1, 2): E_P[X] = ½·1 + ½·2 = 3/2. With Q = (⅓, ⅔): E_Q[X] = ⅓·1 + ⅔·2 = 5/3. Check via change of measure: E_P[(q/p)·X] = ½·[(⅓)/(½)]·1 + ½·[(⅔)/(½)]·2 = ⅓ + 4/3 = 5/3. ✓
- **Assignment Q1:** Ω = {₹10, ₹5, ₹1}, P uniform (⅓ each). Find a valid Q "as per likeliness of payoff" — the bettor's intuition: the counterparty "is not a fool; he pays proportionately more for less likely outcomes," so higher payoff ⟹ lower probability under Q (suggested shape e.g. q = (0.2, 0.3, 0.5)). Then verify E_Q[X] = E_P[(qᵢ/pᵢ)X] both analytically and by Monte Carlo (uniform RNG → bucket into intervals → average over n trials → convergence as n → ∞; only a uniform-random library allowed).
- **Random walk sample space:** after n tosses, positions are {k(+1) + (n−k)(−1) : k = 0…n} = {2k − n}; spacing of 2 between adjacent outcomes.
- **Unique martingale measure:** pL = (1−p)M ⟹ p = M/(L + M) (transcript algebra is garbled — "p = −n/(l−n)" — but the intent and the L/M = p/(1−p) relation are clear). Uniqueness because the equation is linear in p.
- **Gambler's Ruin derivation:**
  - Recursion: pᵢ = p·pᵢ₊₁ + q·pᵢ₋₁.
  - Using p + q = 1: (p+q)pᵢ = p·pᵢ₊₁ + q·pᵢ₋₁ ⟹ q(pᵢ − pᵢ₋₁) = p(pᵢ₊₁ − pᵢ) ⟹ **pᵢ₊₁ − pᵢ = (q/p)(pᵢ − pᵢ₋₁)**.
  - Iterating with p₀ = 0: p₂ − p₁ = (q/p)p₁, p₃ − p₂ = (q/p)²p₁, …, **pᵢ₊₁ − pᵢ = (q/p)ⁱ·p₁**.
  - Telescoping: p_{i+1} − p₁ = Σₖ₌₁ⁱ (q/p)ᵏ·p₁ — a **geometric series**. Applying p_N = 1 (N = 100) pins down p₁, yielding the closed form (the standard result pᵢ = [1 − (q/p)ⁱ]/[1 − (q/p)^N], and pᵢ = i/N when p = q = ½ — the final formula itself is not written out in the audible transcript, only the method).
  - **Two-sided variant:** with lower absorbing barrier b (quit at ₹300) and upper target a+b (win ₹10,000), replace N by a+b and i accordingly. A stock example is quoted: "stock at $10, up-move probability 0.45, probability of reaching $15 before $5."
- **Assignment Q2:** verify P(X_{n+1} = x) = Σ_y P(X_n = y)·P(y→x) generically by simulation (e.g. from y = 3 at t = 3 to x = 251 at n = 97 — intractable by hand, hence code).
- **Assignment Q3:** simulate Gambler's Ruin (start ₹10, target ₹100, N parameterized, p = q = ½ then asymmetric ⅓/⅔); estimate win probability as wins/trials for 1,000 then 10,000 trials and check convergence to the analytic answer.

## 4. Intuition & examples

- **Coin toss as the universal experiment:** reused for stochastic processes, random walks, Markov processes, martingales, and later binomial option pricing — "so our concepts stay relatable."
- **Payoff–probability inversion:** a counterparty paying ₹10/₹5/₹1 for three outcomes reveals Q: bigger promised payoff ⟹ less likely event.
- **Casino economics:** the casino has effectively infinite wealth relative to the player, so even in a *fair* game (e.g. roulette without the 0/00, betting even numbers, p = ½) the house wins almost surely by Gambler's Ruin. Probability of winning rises with initial wealth — mapped to the Hindi proverb "wealth attracts wealth." A 2-D surface of win probability vs (initial wealth, p) is sketched: higher p steepens the curve; at p = ½ with ₹10 vs ₹100 target, chances are slim. Casino only risks a fair fight against a bankroll comparable to its own. Roulette's 0/00 is how casinos *additionally* rig the odds.
- **Monte Carlo philosophy:** "Not all functions can be integrated and not all PDEs can be solved, but everything can be simulated." Simulation is the fallback when no analytic solution exists — but then you can't verify correctness, so problems with both solutions (like Gambler's Ruin) are used for cross-checking.

## 5. Trading-relevant takeaways

- **Unique risk-neutral measure ⟺ no arbitrage ⟺ unique derivative price.** The zero-expectation random walk is the toy model of this; it recurs in binomial pricing and Black–Scholes.
- **P vs Q division of labor:**
  - *Hedge funds / prop (P-world):* profit from directional positions; need the best estimate of the *natural* measure P. Bias in P̂ matters most near decision-flip boundaries (true up-prob 0.55 estimated as 0.45 → exactly wrong action → "doomed for bust"; 0.90 estimated as 0.85 → lose little).
  - *Investment banks / derivative desks (Q-world):* sell exotics, hedge with vanillas; if the book is perfectly hedged (risk-neutral), they earn the spread/commission. Worked example: sell a bet paying +1 (heads) / +2 (tails) — fair value 3/2, charge ₹1.51; sell the opposite bet paying fair −1.5 for ₹1.49; bank pockets ₹0.02 regardless of outcome. They don't care whether the underlying is mispriced — only that the net position is hedged.
- **Bias formalization:** like regression (y = ax + b vs ŷ = ax + b + ε), even with all data you get P̂, never P; P − P̂ is the bias/error, shrinking with model quality and statistical confidence.
- **Risk-of-ruin framing:** survival probability depends on capital relative to target and edge (p). Practical trading analogue: position sizing and capital adequacy dominate even in a fair game; quitting thresholds (stop at ₹300, goal ₹10,000) map to the two-barrier formula.

## 6. Lecturer's asides

- **On study materials:** no single book/source presents these topics in this sequence with uniform rigor; he deliberately avoided recommending one so far, but will share the reference PDFs/notes used for the random-walk and Gambler's Ruin derivations. Advice for reading white papers: when a paper jumps straight to the general symbolic result, redo it with concrete numbers (L = 1, p = q = ½) step by step first.
- **Feynman technique, repurposed:** "The dumbest person you can explain something to is a PC." If you can explain a concept to a computer (working code from first principles) and it matches the analytics, you've understood it. Hence the insistence on no libraries beyond a uniform RNG.
- **Assignment logistics:** 3 questions in Assignment 1 (RN derivative, transition-probability relation, Gambler's Ruin), attempt before Wednesday's doubt session, working code expected by the weekend; submit scripts in a shared drive folder (Git push restrictions), optionally anonymized; he'll review random scripts. Write specific-case code first, then parameterize/generalize. Colab/Jupyter — whatever is comfortable.
- **Personal:** he used to trade in Indian markets, has stopped, now invests long-term; promises a "quant lens on investing" segment (fundamental vs technical vs quant analysis) around week four.
- **Next session:** Markov process and martingale formal definitions (with the four combinations — Markov/not, martingale/not), then Brownian motion, quadratic variation and its need, PDEs, Taylor expansion, Itô's lemma — motivation: "class-12/engineering calculus is insufficient for stochastic processes; we need stochastic calculus," as background for the Black–Scholes derivation. Three more assignments coming.

*Fidelity note: a few symbolic lines are garbled in the auto-generated subtitles (the closed-form Gambler's Ruin solution and the p = M/(L+M) rearrangement are described verbally but not cleanly transcribed); the reconstructions above are flagged where inferred. The continuous-statement of Radon–Nikodym was announced but not covered — only the discrete case was derived.*

---

I've read the full transcript. Here is the technical summary.

---

# QF101 — Session 3 Summary (Lecturer: Ajit Kulkarni, FEBS IIT Bhubaneswar)

**Session theme:** Markov processes, martingales, no-arbitrage and the Fundamental Theorem of Asset Pricing, introduction to Brownian motion (Wiener process), quadratic variation, and a refresher on time-value-of-money and partial derivatives — all as groundwork for Itô calculus (Session 4+).

## 1. Topics covered (in order)

1. Recap: multiple probability measures per stochastic process; why a different measure (Q) is needed for derivatives pricing.
2. Markov processes (with coin-toss / random-walk example; counter-example).
3. Martingales — definition, verification on a random walk, counter-examples.
4. No-arbitrage pricing game; one-period stock example; non-existence of a martingale measure ⇒ arbitrage.
5. Fundamental Theorem of Asset Pricing (existence of unique martingale measure ⇔ no arbitrage).
6. Career/roles digression: Quant researcher vs developer vs trader vs validation; QF vs FE vs FRM/CQF/CFA; risk management taxonomy.
7. Brownian motion / Wiener process — definition, 4 properties.
8. Moments of the Wiener process: E[Wₜ] = 0, Var(Wₜ) = t, E[WₛWₜ] = min(s, t).
9. Quadratic variation of Brownian motion: (dW)² = dt.
10. Time value of money: FV/PV, annuity formula (loan/EMI example).
11. Total differential / partial derivatives refresher on the annuity function; numerical verification assignment.

## 2. Key definitions & theorems

- **Markov process:** P(Xₜ₊₁ | ℱₜ) = P(Xₜ₊₁ | Xₜ). Next state depends only on the current state, not the path. Shown on the symmetric random walk: e.g. P(X₂=2 | X₁=1) = ½, P(X₂=0 | X₁=1) = ½, same as P(X₂=0 | X₁=−1) = ½.
- **Non-Markov counter-example:** path-dependent transition probabilities — after +1 go up with p (to 2) and down with 1−p; after −1 go down with p (to −2) and up with 1−p. Then P(X₂=0 | X₁=1) = 1−p but P(X₂=0 | X₁=−1) = p ≠ 1−p. It becomes Markov iff p = ½ (which recovers the random walk).
- **Martingale:** E[Xₜ₊₁ | ℱₜ] = Xₜ — must hold for **all** nodes reachable at time t (all filtration outcomes), not just one branch. Verified on the symmetric random walk: E[X₃ | X₂=2] = ½·3 + ½·1 = 2; E[X₃ | X₂=0] = 0; E[X₃ | X₂=−2] = −2.
- **Markov but not martingale example:** set P(2→3) = 1/3, P(2→1) = 2/3 ⇒ E[X₃ | X₂=2] = 3·⅓ + 1·⅔ = 5/3 ≠ 2. Breaking it at one node is sufficient to break the martingale property.
- **Key insight:** for the fixed outcome set {+1, −1} per step, the *only* probabilities making the process a martingale are p = ½. Probabilities compatible with the martingale property are determined by the **outcomes**, not by the physical coin — this motivates why pricing measures ignore real-world probabilities.
- **Fundamental Theorem of Asset Pricing (as taught):** there exists a (unique) probability measure Q under which the (discounted) asset price process is a martingale ⇔ no arbitrage. If no such measure exists, there is arbitrage. Q is called the martingale / risk-neutral measure; the natural/physical measure is P. Expectations under P and Q are related via the Radon–Nikodym derivative dQ/dP, but the measures themselves are "not connected" in the sense that Q need not resemble P.
- **Brownian motion / Wiener process (Wₜ or Bₜ), 4 defining properties:**
  1. W₀ = 0 (starts at zero).
  2. Wₜ is (almost surely) continuous. Continuity refresher: lim_{x→k⁺} f(x) = lim_{x→k⁻} f(x) = f(k); counter-example = step/greatest-integer function.
  3. Independent increments — Wₜ − Wₛ is independent of the path up to s (Markov-like). Note Wₜ is *not* independent of Wₛ; only increments are independent. Also stated: for independent X, Y, E[XY] = E[X]E[Y].
  4. Wₜ − Wₛ (s < t) ~ N(0, t−s) — normally distributed increments with variance equal to the time length (variance, not std dev).
  Brownian motion = limiting case of a random walk as Δt → 0 (hence continuity). The Central Limit Theorem is the "connecting link" between random walk and Brownian motion.

## 3. Formulas & derivations

- **Moments of Wₜ:**
  - E[Wₜ] = 0, since Wₜ = Wₜ − W₀ ~ N(0, t).
  - Var(Wₜ) = E[Wₜ²] − (E[Wₜ])² = E[Wₜ²] = t.
  - Covariance: for s < t, write Wₜ = Wₛ + (Wₜ − Wₛ). Then E[WₜWₛ] = E[Wₛ²] + E[Wₛ(Wₜ − Wₛ)] = s + E[Wₛ]·E[Wₜ − Wₛ] = s + 0 (independent increments). Generically **E[WₛWₜ] = min(s, t)**.
- **Quadratic variation:** for a partition Π = {0 = t₀, t₁, …, tₙ = T} define Q_Π = Σ_{j=0}^{n−1} (W_{t_{j+1}} − W_{t_j})². Then
  E[Q_Π] = Σ E[(W_{t_{j+1}} − W_{t_j})²] = Σ [t_{j+1} − 2·min(t_{j+1}, t_j) + t_j] = Σ (t_{j+1} − t_j) = tₙ − t₀ = T.
  Since Var(Q_Π) → 0 as the mesh → 0 (proof deferred — reference given: Shreve, *Stochastic Calculus for Finance*), the sum of squared increments converges to T, i.e. **(dW)² = dt**. This is the key departure from ordinary calculus: for a smooth deterministic function (e.g. 2t²) the same sum-of-squared-differences tends to 0 as the mesh refines; for Brownian motion it tends to T. This result is the foundation for Itô calculus.
- **Time value of money (r = interest rate, simple setup):**
  - FV = P(1 + rT); with T = 1, PV = FV / (1 + r).
  - PV of level cash flows C at t = 1…n: PV = C/(1+r) + C/(1+r)² + … + C/(1+r)ⁿ.
  - Geometric-series sum → **annuity formula:** a = C·[1 − (1+r)^(−n)] / r.
  - Rule: money at different times cannot be added — all cash flows must be brought to one time instant (not necessarily t = 0) before adding. EMI/loan example: sum of discounted EMIs = loan principal.
- **Total differential:** a = a(r, n, C) ⇒ da = (∂a/∂r)dr + (∂a/∂n)dn + (∂a/∂C)dC. Supporting differentiation facts: quotient rule d(u/v) = (v du − u dv)/v²; d(aˣ)/dx = aˣ ln a (derived via y = aˣ ⇒ ln y = x ln a).

## 4. Intuition & examples

- **Probability measures depend on objective:** "me vs Roger Federer" — extra information changes the probabilities you'd assign to the same process; likewise different objective functions call for different measures (P vs Q).
- **Pricing game:** outcomes {₹5, ₹2} with probabilities {2/3, 1/3} ⇒ fair price = (2/3)·5 + (1/3)·2 = 4. Any other price ⇒ arbitrage.
- **One-period stock, no-arbitrage ↔ martingale measure:** stock at 100 moves to 105 or 102 (both up). Buying at 100 is a sure win ⇒ price rises until entry is indifferent; viable prices lie strictly between 102 and 105. Solving p·105 + (1−p)·102 = 100 gives 3p = −2 ⇒ p = −2/3 — impossible since probabilities must satisfy 0 ≤ P(ωᵢ) ≤ 1 and ΣP = 1. Non-existence of a martingale measure is mathematically equivalent to the arbitrage already found by logic. Interest rate assumed 0 for now.
- **Radon–Nikodym:** E^Q[(dQ/dP)·X] = E^P[X] — expectations are related through the derivative even though measures aren't.
- **Continuity intuition:** a random walk keeps a value at every zoom level, so its limit (Δt → 0) is continuous.

## 5. Trading-relevant takeaways

- **P vs Q split of the industry:** risk management (banks: market/credit/operational/regulatory risk) operates largely under the physical measure P; derivatives pricing/trading operates under Q. Volume of jobs in P-world (low risk, low pay); value/high pay in small Q-teams (hedge funds), higher career risk.
- No-arbitrage is the foundational pricing principle: without a martingale measure, multiple prices for the same payoff → arbitrage, and derivatives cannot be priced consistently.
- The whole mathematical stack (quadratic variation → Itô calculus → PDEs) exists to support **hedging**: "these concepts are built on the belief that you can hedge well."
- Fundamental, technical, and quantitative analysis are complementary: combining methods improves the odds of reaching the "global minimum" risk portfolio (better Sharpe for the same return); a strategy found fundamentally can be cross-checked quantitatively. The course will dedicate a week to designing pure-Quant strategies.
- Mutual funds ≈ fundamental-only (no derivatives/shorting); hedge funds (Medallion, Citadel, Jane Street) use quant strategies.

## 6. Lecturer's asides & advice

- **Books:** Steven Shreve, *Stochastic Calculus for Finance* (for the rigorous Var(Q_Π) → 0 proof); Jim Simons biography *The Man Who Solved the Markets*.
- He deliberately avoids standard FTAP literature (state-price densities, Arrow–Debreu securities, complete markets) because it drowns intuition in notation; MFE courses go the rigorous route.
- **Certifications:** FRM = risk management only, no stochastic calculus; CQF = closest certification covering both QF and FE; CFA = fundamental/equity research (Warren Buffett's category). QF/FE are academically near-synonymous in foreign master's programs. QF is offered only at master's level worldwide (his view).
- **Quant economics vs QF:** QE = empirical macro/micro analysis under P (e.g. Fed CCAR stress tests with baseline/adverse/severely-adverse scenarios); QF = pricing/hedging individual securities (MBS, Asian/digital options, option strategies). QF niche but far more job-rich; on average QF pays more.
- **Career advice:** spread wide in undergrad, go deep/niche in master's; Bachelor's in maths recommended over economics; MBA for business-facing roles, MS/MFE for research/quant-dev. Switching fields means re-entering as a fresher. Credentials matter for the first 3–5 years only. His own path: mechanical engineering → MBA (IFMR Chennai) → FRM (part-time) → 6 years quant experience.
- **Quant developer reality:** coding difficulty isn't syntax — it's understanding the math behind discretization/simulation; researchers hand over an algorithm + one Excel check case, the rest is the developer's problem.
- Learning method: never skip symbols you don't understand; master each concept fully before moving on; build your own examples (e.g. try stock 100 → {99, 98}).
- **Assignments given (Assignment 2):** (Q1) simulate and verify the Central Limit Theorem — 40 draws from mixed distributions per trial, ~1000 trials, plot distribution of sample means and of the standardized means; (Q2) Python code verifying quadratic variation of Brownian motion → T as Δt → 0 (meshes 10/100/1000 over [0,1]); (Q3) numerically verify da = (∂a/∂r)dr + (∂a/∂n)dn + (∂a/∂C)dC for the annuity formula using finite differences. Also a typo noted in the previous assignment (Q2: probability "1/5, 1/5" should be 0.5/0.5).
- QF was "invented by PhD mathematicians" — Black–Scholes authors were economists; literature assumes all basics, which is why self-study is hard.

**Note on transcript quality:** fully legible; mishearings are benign ("Coos" = coin toss, "redon neum/Ron neodium" = Radon–Nikodym, "EO calculus" = Itô calculus, "winner process" = Wiener process). One passage near the annuity differentiation shows garbled derivative sketches on a whiteboard ("do a by do R will be this") where the actual displayed formulas are not recoverable from audio alone.

---

# QF101 — Session 4 Summary (Doubt-Clearing / Q&A Session)

This session is primarily a student Q&A and review session led by Ajit Kulkarni. It revisits concepts from Sessions 1–3 (probability measures, martingales, random walks, Brownian motion, filtration, arbitrage) rather than introducing new theorems. Key content below.

## 1. Topics covered (in order)

1. Natural (physical, P) vs risk-neutral (Q) probability measure.
2. Equivalence of the "no-arbitrage" expectation condition and the martingale condition in the random-walk example.
3. Assignment guidance: simulating the law of total probability / one-step transition equation for random walks.
4. What a stochastic process is (student-exam-marks example).
5. Random walk → Brownian motion as a scaling limit; why stock prices motivate this.
6. Empirical justification: log-returns of stock prices are approximately normal (CLT connection).
7. Meaning and notational role of filtration (Markov property, martingale definitions).
8. Arbitrage explained with a non-probabilistic example (idli–vada combo pricing) and the "cost of playing the game = expected reward" martingale/no-arbitrage condition.
9. Why risk-neutral pricing works without knowing real-world probabilities (existence of derivatives markets / riskless portfolios).
10. Risk vs reward nuance; P-measure expected return = risk-free + risk premium (CAPM preview) vs Q-measure expected return = risk-free rate.
11. Live spreadsheet simulation of a Wiener process and a random walk; quadratic variation; Gambler's Ruin as a random walk starting at 10.

## 2. Key definitions & statements

- **Natural (physical) probability measure**: the probabilities obtained by actually performing the experiment — e.g., tossing a real coin 10,000 times and finding P(H) = 0.6, P(T) = 0.4. These are "natural to" the process; applies to any stochastic process with measurable outcomes (all outcomes known before the experiment is run).
- **Multiple measures on one process**: the same stochastic process (and the same filtration) can carry more than one probability measure; the natural measure P and the risk-neutral measure Q are just two different mappings of the same events into [0, 1]. *Why* we switch to Q is deferred to Week 3 (Black–Scholes derivation).
- **Martingale (as stated)**: a process with E^P[S_{t+1} | F_t] = S_t. In the random-walk example (start 0, steps l and m with probabilities p, 1−p), the martingale condition reduces to p·l + (1−p)·m = 0; zero was the specific case, the general condition is "equals its current value S_t". The unique p solving this is the same p that appeared in the no-arbitrage discussion — the two statements are the same condition in this example.
- **Filtration**: notational shorthand for "all information available so far". The Markov property P(X_{n+1} | X_0,…,X_n) = P(X_{n+1} | X_n) is written compactly as P(X_{n+1} | F_n) = P(X_{n+1} | X_n); white papers write martingales as E[S_{t+1} | F_t] = S_t. Filtration is a property of the process, not the measure: the *same* filtration underlies both P and Q; only the mapping of events to numbers in [0,1] differs.
- **Stochastic process**: a collection of random variables evolving over time, e.g. X_1, X_2, X_3, … = a student's marks (0–100, integers) across repeated maths exams. Realizations denoted by small letters; the process can have behavioral properties (scores expected to improve with study, like drift).
- **Wiener process / Brownian motion properties** (enumerated in the demo): (i) starts at 0 (W_0 = 0); (ii) increments W_{t+1} − W_t are normally distributed with mean 0 and variance equal to the time step (unit step ⇒ N(0,1)); (iii) increments are independent of each other; (iv) paths are continuous. Cross-section at time T is normal with mean 0 and variance T. Brownian motion is a special case of a stochastic process where increments are normal; not every stochastic process is Brownian (e.g., Poisson processes, used for bond pricing and credit derivatives; Lévy distributions also exist).
- **Quadratic variation**: Σ (W_{i+1} − W_i)² = T — the squared increments of a Wiener process sum to total time T (demonstrated numerically in the spreadsheet; exact in the limit Δt → 0).

## 3. Formulas & derivations

- **One-step martingale/no-arbitrage condition (random walk)**: p·l + (1−p)·m = 0, where l, m are the up/down step values (earlier session used +1/−1). Solving for p gives the unique measure making the process a martingale.
- **Law of total probability (assignment 1, Q3)**: P(X, n+1) = Σ_y P(X, n+1 | Y, n)·P(Y, n). For a symmetric random walk the transition probabilities are ½; students must simulate both sides independently (generate the LHS distribution empirically, compute the RHS from simulated transition probabilities), and write generic code so p can be changed (e.g., ⅓, ⅔) and the check iterated over different states.
- **Log returns**: log(S_t / S_{t−1}) (e.g., log of day-2 price over day-1 price); the frequency distribution of these closely resembles a normal distribution — the empirical motivation for Brownian motion models of prices (strictly geometric BM; plain BM used for simplicity for now).
- **No-arbitrage pricing identity (martingale pricing)**: today's price (cost of playing the game) = expectation of discounted/future payoff under some measure Q: E^Q[S_1] = S_0. In the example: stock at 100 today, going to 105 or 103; cash flow is −100 at t=0 and +105/+103 at t=1.
- **P vs Q expected returns** (preview, Week 4): under Q, expected stock return = risk-free rate; under P, expected return = risk-free rate + risk premium (CAPM territory). Both are "correct in their own places."

## 4. Intuition & examples

- **Idli–vada arbitrage example**: one vendor sells 1 idli for ₹2, 1 vada for ₹5, and a combo of 2 idlis + 3 vadas for ₹13; the shop both buys and sells at quoted rates. Arbitrage: buy the combo for ₹13, sell back the components for 3×5 + 2×2 = ₹19, pocket ₹6 riskless. The only arbitrage-free combo price is ₹19 — exactly one price point is consistent; any other invites arbitrage. Market pressure corrects mispricing: repeated buying of the cheap combo raises its price and selling the components lowers theirs, until the combo equals ₹19 (and if it overshoots to ₹20, the reverse trade kicks in).
- **Cost of playing the game**: any probabilistic game must charge an entry cost equal to the probability-weighted expected reward for the game to be arbitrage-free (coin-toss game paying ₹4 on heads, ₹2 on tails cannot be free — players would bet on both outcomes). Setting cost = expected payoff *is* the martingale condition.
- **Why real probabilities aren't needed**: unlike a coin-toss operator (who must know the true odds to price the game), with stocks you can set up a **riskless portfolio using derivatives** — something impossible with coins. Hence it suffices that *some* measure Q exists under which E^Q[payoff] = today's price; the actual P-measure odds are irrelevant. This is contingent on derivatives markets existing.
- **Brownian motion demo**: generate independent N(0,1) increments (via norm-inverse of a uniform random number) per unit step, cumulatively sum → simulated Wiener paths all starting at 0; cross-section at any time is N(0, T). Random walk (±1 via coin flip on random > 0.5, cumulatively summed) resembles Brownian motion more and more as Δt shrinks — the limiting case Δt → 0 of a random walk *is* Brownian motion (backed by CLT, the assignment's content).
- **Gambler's Ruin**: take the same random-walk simulation but start at 10 instead of 0 — that is the Gambler's Ruin problem; with enough trials the simulated absorption statistics match theory.

## 5. Trading-relevant takeaways

- **Arbitrage is self-correcting**: mispriced packages vs their components invite two-way flow that pushes prices to the single consistent level — the logic behind relative-value and basis trading.
- **Risk-neutral vs real-world returns**: derivative pricing lives under Q (expected return = risk-free); investment/expected-return questions (mutual funds, CAPM) live under P (risk-free + premium). Confusing the two measures is a category error. "Hedge funds exist because people believe arbitrage (P ≠ Q discrepancies) is possible."
- **High returns *require* high risk, but high risk does not imply high returns** — there is good risk and bad risk (diversifiable vs not); the lecturer explicitly corrects the converse fallacy.
- **Model choice by asset class**: most equity/FX/commodity/interest-rate derivatives can use a Wiener (normal) process; credit derivatives and bond pricing often need Poisson/jump processes. Normality of log returns is an empirical fact to verify on historical data, not an axiom.

## 6. Lecturer's asides & practical notes

- Assignment coding is deliberately minimal: 5–8 lines, from first principles, no custom libraries (with libraries, 2–3 lines).
- Code should be written **generically** (parameterized probabilities, reusable across states) so experiments can be iterated.
- Roadmap teased: why we move from P to Q and the Black–Scholes derivation in **Week 3**; CAPM / expected-returns-under-P material in **Week 4**.
- Brownian motion named after Robert Brown; the lecturer jokes that if processes were named by distribution it would be the "normal process."
- Poisson process mis-transcribed as "Pon/pron process" in places; "Wiener" appears as "Wier/Winer" — all clearly refer to the Wiener process.

*Faithfulness note: the transcript is fully audible/coherent throughout; no significant garbled sections beyond the minor transcription artifacts noted above.*

---

I have the full transcript. Here is the summary.

---

# Session 5 — Quantitative Finance 101 (Ajit Kulkarni, FEBS IIT Bhubaneswar)

**Note on fidelity:** The transcript begins mid-sentence (the opening minutes are missing) and cuts off mid-explanation at the end. Despite the course's later math topics, **this session contains no σ-algebras, martingales, Itô, or Girsanov** — it is a derivatives-fundamentals lecture: assets, forwards/futures, cost of carry, and no-arbitrage pricing.

## 1. Topics covered (in order)

1. What is an asset (context-dependent definition).
2. Classification: real vs. financial assets; overview of derivative underlyings (commodities, stocks, bonds, FX).
3. Motivation for derivatives — movie-ticket contract example (replacing the conventional farmer/wheat story); derivatives as a zero-sum game.
4. Futures contracts: definition, payoff, forwards vs. futures.
5. No-arbitrage futures pricing and cost of carry; live NSE Nifty futures example with implied interest rates.
6. Calendar-spread arbitrage between futures expiries.
7. Dividends and dividend-adjusted futures pricing.
8. Commodity futures: storage cost / convenience yield; cash vs. physical settlement.
9. Contango, backwardation, and spot–futures convergence at expiry.
10. Market participants (hedgers, speculators, arbitrageurs) — introduced, deferred to next session.

## 2. Key definitions & concepts

- **Asset:** anything that generates future cash flow; classification is context-dependent. A laptop used for freelance income is an asset; the same laptop used to watch movies is an expenditure.
- **Real assets:** tangible, subject to depreciation (machines, gold, oil). **Financial assets:** non-tangible (stocks, bonds, FX).
- **Forward contract:** bilateral (OTC) agreement between two parties A and B to exchange the difference S_T − F at expiry; no cash flows until T.
- **Futures contract:** same economic structure as a forward but **exchange-traded** — orders are routed to an exchange that matches counterparties; positions can be traded thereafter.
- **Payoff:** buying stock at S_t yields P&L = S_T − S_t; a long futures position entered at price F yields S_T − F.
- **Zero-sum game:** derivatives are time-bound contracts where one party's gain is exactly the other's loss — unlike stocks, where buyer and seller can both profit over time.
- **Cost of carry:** the financing cost of holding the underlying (interest on borrowed capital), which the futures buyer avoids paying upfront and therefore pays via a higher futures price.
- **Settlement:** *cash settlement* (loser pays the difference in cash) vs. *physical settlement* (delivery of the underlying, e.g. crude oil: pay the agreed $80, receive a barrel worth $100). **Contract specifications** define grade/quality; delivery happens via a vendor, and quality inspection adds cost.
- **Contango:** F > S (positive net carry). **Backwardation:** F < S (e.g. when dividend/convenience terms exceed r).
- **Convergence:** at t = T, F = S exactly (F = S·e⁰ = S); futures and spot must converge at expiry.

## 3. Formulas & derivations

- **Base no-arbitrage futures price:** F = S·e^{r(T−t)}, with simple-interest variant F = S(1 + r(T−t)). Derivation: buying stock requires upfront capital; if you must borrow S at rate r you pay interest S·r·(T−t). The futures position has identical payoff with no upfront capital, so it must trade at a premium equal to this financing cost, else you short the expensive leg and buy the cheap one for riskless profit.
- **Implied interest rate from futures:** r = ln(F/S) · (365/days), or (F/S − 1) · (365/days).
- **With continuous dividend yield q:** F = S·e^{(r−q)(T−t)}. Since dividends mechanically reduce the stock price by the dividend amount, q enters negatively. Q is defined as the *annual* dividend as a % of spot (example: ₹100 stock paying ₹2/quarter → ₹8/yr → q = 8%).
- **Commodities with storage/convenience yield c:** F = S·e^{(r−c)(T−t)} (lecturer first wrote +c, then corrected: "it is r minus c"). Storage, insurance, delivery, and quality-check costs modify the carry.
- **Heuristic term structure:** F_1m = S + carry(1m); F_2m = S + carry(2m); so F_2m − F_1m = S·r·(2m − 1m)/12 when the implied rate is consistent. If implied rates across expiries are inconsistent, that spread is an arbitrage.

## 4. Intuition & numerical examples

- **Movie-ticket forward:** On 1 Jan 2024 a moviegoer and a theater fix every ticket that year at ₹400. Ω = {200, 400, …, 2000} with probabilities p_i ∈ (0,1), Σp_i = 1 (tying back to earlier probability lectures). A blockbuster year (tickets ₹600–2000) → consumer wins at the theater's expense; a flop year (₹100–400) → theater wins. Derivatives = zero-sum.
- **Nifty futures (live NSE data, 29 Dec 2023):** spot S = 21,731; near month (25 Jan 2024) F = 21,861; mid month (29 Feb) F = 21,999; far month (28 Mar) F = 22,135. With T−t ≈ 26/365 for the near month, implied annualized rates come out ≈ **8.4%, 8.04%, 7.8%** respectively (transcript garbles "8.4" as "88.4" once).
- **Mispricing arbitrage (calendar spread):** if the Feb contract traded at 21,864 (implied ~4%) instead of its fair 21,999: **buy the cheap Feb future, short the fairly-priced March future**, close both on 29 Feb. Numerically: if Nifty ends at 22,000, the Feb long gains 22,000 − 21,864 = +136, and the March short (fair value then ≈ 22,000 plus one month's carry) gains the small residual — the transcript's arithmetic here is approximate. The intuition: you're *paying* 4% carry on the long and *receiving* ~8% carry on the short; the rate differential is locked in today regardless of where Nifty goes. Terminology: **near month / mid month / far month**; long one expiry + short another = **calendar spread**.
- **Dividend mechanics:** profit ₹1,000 over 10 shares → ₹100/share; a ₹10 dividend is paid out of the same profit pool, so the share price drops to ₹90 *purely due to the dividend*. Observed prices can still rise (100 → 110) if expected future profits jumped (to 120) before the ₹10 was deducted. Consequently, when a large dividend falls between two expiries, the far-month future can trade **below** the near-month, inverting normal carry order (example shown numerically: raising q from 0 → 2% → 5% → 8% pushes F from ~21,861 down through spot into backwardation).
- **Share-price fundamentals:** revenue − costs = profit; profit per share is the fundamental driver of price; the market disagrees only about the present value of that profit stream into the future.

## 5. Trading-relevant takeaways

- Futures implied rates are directly observable: ln(F/S)·365/days gives the market's implied cost of capital — a tradeable signal when it deviates from your funding rate or from neighboring expiries.
- Calendar spreads monetize carry mispricing with market-direction risk (mostly) cancelled — the P&L is the differential of implied rates, not of spot.
- Exchanges mandate physical settlement so futures markets can't decouple from and rig the spot market; **a derivative is only priceable if its underlying is deliverable** — a made-up, non-deliverable underlying admits no credible market.
- Watch dividend announcements around expiry boundaries: large dividends invert the futures term structure and create apparent (but not real) arbitrage if you price with q = 0.
- Margin exists in practice but is separate from the contract's no-arbitrage price (deferred to a later session).

## 6. Lecturer's asides

- Book reference: "standard books, John Hull" (transcript's "John Cal" = John C. Hull, *Options, Futures, and Other Derivatives*).
- Interview advice: asked whether cash-settled contracts have convenience yield, answer **no** — quote F = S·e^{rT}; convenience yield belongs to physically-settled commodity markets.
- Deliberately replaced the textbook farmer/wheat hedging example with the movie-ticket example "for our class."
- Frequent invitations to interrupt with questions; interactive Socratic derivation of the no-arbitrage price with students.

---

# Session 6 Summary — Quantitative Finance 101 (FEBS, IIT Bhubaneswar; Ajit Kulkarni)

**Content note:** This session is entirely on derivatives market mechanics (futures margins, leverage, FX futures, option payoff basics). No stochastic-calculus topics (σ-algebras, martingales, Girsanov, Itô) appear here. The transcript is clean and complete; nothing material was garbled.

## 1. Topics covered (in order)

1. Recap of Session 5: derivative definition, futures long/short (bullish/bearish views), cost-of-carry pricing (F ≈ S + carry), market participants (hedgers, speculators, arbitrageurs), futures spread strategies, index arbitrage, convenience yield & physical delivery, contango (F > S) vs backwardation (F < S).
2. Credit risk in futures and the exchange/clearing mechanism.
3. Margins: initial margin vs variation margin; mark-to-market daily settlement.
4. Leverage — definition, numerical examples (stock on borrowed money, home loan analogy), leverage in Nifty futures with full worked numbers; margin calls.
5. Payoff diagrams: long futures, short futures; combined long+short = 0.
6. Basis = F_t − S_t; basis → 0 at expiry.
7. FX futures pricing via covered interest rate parity (round-trip arbitrage argument).
8. Motivation for options (asymmetric payoff game); premium; European vs American exercise.
9. Call/put payoff functions and diagrams (C⁺, C⁻, P⁺, P⁻); payoff vs profit (PnL) diagrams; squaring off vs exercising; x-axis convention caveat (S_T vs K).
10. Assignment 3: Python code for option payoff diagrams and strategy combos.

## 2. Key definitions

- **Credit risk:** the losing party lacks the ability or willingness to pay the winner (analogy: a home-loan defaulter). Mitigated in futures because both parties trade against the **exchange**, which guarantees settlement.
- **Initial margin:** refundable security deposit collected up front from both sides to cover credit risk — not a premium or cost.
- **Variation margin:** funds topping up daily mark-to-market losses.
- **Mark-to-market (MTM):** daily settlement of accrued P&L rather than waiting to expiry. Example: A long, B short at S=1000 on a 30-day contract. Day 1: S→1200, B pays A 200; from then on P&L is measured relative to 1200. By day 29 most of the cumulative P&L is already settled, so residual default exposure is only the last day's move (lecturer's numbers: 500 total move, 450 already settled, risk ≈ 50).
- **Lot size:** futures trade only in multiples (Nifty lot = 50 units at the time of the example).
- **Basis:** basis = F_t − S_t at the same time t; equals zero at expiry since F_T = S_T.
- **Option premium:** amount paid up front by buyer to seller for the right (choice); stays with the seller regardless of outcome.
- **European option:** exercisable only at expiry; **American:** any time before expiry (lecturer notes names have nothing to do with geography; Indian equity options are European-style, but positions can be exited early by selling — "squaring off" — which is distinct from exercising).
- **Payoff diagram:** cash flows **at expiry only**; **profit/PnL diagram:** payoff minus premium paid (long call breaks even at K + p, not K).

## 3. Formulas & derivations

- **Cost of carry / futures pricing:** F = S·e^(rτ), τ = residual time to expiry.
- **Futures delta:** ∂F/∂S = e^(rτ) ≈ 1 (r ≈ 5–6%, τ = 1/12 ⇒ e^(rτ) ≈ 1), hence ΔF ≈ ΔS — one-rupee stock move ≈ one-rupee futures move per unit (× lot size per lot).
- **Payoffs:** long futures: S_T − K (slope 1, 45° line, y-intercept −K); short futures: K − S_T (mirror image). Long + short (same underlying, expiry) nets to zero.
- **Leverage ratios:** asset/capital and debt/equity. Worked stock example: ₹100 own + ₹400 borrowed buys ₹500 stock; stock → 550 yields ₹50 profit on ₹100 equity (vs ₹10 unlevered) — leverage 5:1 (asset:equity), 4:1 (debt:equity); interest owed regardless of outcome.
- **Nifty futures leverage example (Jan-2024 expiry):** spot 21,731; buying 50 units physically costs ₹10,86,550; exit at 22,000 ⇒ profit ₹13,450 ≈ 1.2%/month (~14% annualized; ~1% net of 12%-p.a. financing). Same exposure via futures at 21,861 requires margin ≈ ₹1,23,879; P&L = (22,000 − 21,861) × 50 = ₹6,950 ⇒ ~5.6% return on margin; leverage = 10,93,050 / 1,23,879 ≈ **8.8×** (₹1 own + ₹7.8 effective loan). Per-unit margin ≈ ₹2,478 vs spot 21,731 — i.e., a ₹500 stock can be "held" via futures for ~₹57 of margin. Margins are set by the **exchange**, not the broker; no money is actually procured — by definition a futures contract requires zero up-front payment, margin is only default protection.
- **Covered interest rate parity (FX futures):** F = S·e^((r_d − r_f)τ) (he initially wrote foreign−domestic, then corrected to domestic−foreign). Derivation by round-trip arbitrage: with USD/INR = 80, India risk-free 8%, US 4%, a US investor converting $1 → ₹80 → ₹86.4 after 1 year must be able to lock a forward rate making the round trip equal 1.04 USD; iterating candidate forward rates (90 → 0.96, 85 → ~1.01, 84 → ~1.02, 83 ≈ 1.04) converges to the parity-implied forward ≈ 83. If the market forward deviates, arbitrage exists: short the expensive leg, long the cheap one (spot-vs-futures spread eliminates FX risk, leaving only the parity violation).
- **Option payoffs:** long call C⁺ = max(S_T − K, 0); short call C⁻ = −max(S_T − K, 0); long put P⁺ = max(K − S_T, 0); short put P⁻ = −max(K − S_T, 0). Long + short of the same option = 0.
- **Fair-price intuition game:** symmetric coin-flip (+1/−1 with p = ½ each) has zero expectation ⇒ free to enter. Truncating the downside (heads: +1, tails: 0) has expectation +½ ⇒ fair entry price is a ½-rupee up-front payment — the prototype of an option premium.

## 4. Intuition & examples

- Leverage as a **double-edged sword**; Warren Buffett's 2007–08 remark calling derivatives "weapons of mass destruction" (transcribed as "nuclear weapons"); derivatives are zero-sum, making leverage riskier than in real assets.
- Why retail flocks to derivatives: near-infinite notional exposure for minimal capital and low funding cost (interest paid only on the margin-funded principal, not full notional).
- Payoff-diagram discipline: always fix the x-axis variable, fix the other constant (K or S_T = 100), build a payoff **table** first, then plot. Plotting a call payoff against K (S_T fixed) produces a shape resembling a put — a common source of confusion in white papers/interviews.
- Premium trading before expiry: option LTPs are premiums; exiting early means selling the contract to a new buyer at the *current* premium (squaring off), not settling S−K with the original seller.

## 5. Trading-relevant takeaways

- MTM + margin calls mean futures losses are funded daily; margin shortfalls must be topped up same-day or positions are cut.
- Exchange-set margins ⇒ leverage (~6–9× on index futures) is identical across brokers.
- ΔF ≈ ΔS makes futures a near-1:1 delta instrument — leverage multiplies P&L per rupee of capital, not per unit of underlying.
- FX forward/futures mispricing vs interest-rate parity is an arbitrage signal: compare S·e^((r_d−r_f)τ) to the quoted forward and trade the spread (ignoring transaction costs and retail/institutional FX rate differences).
- Derivatives' advantage over cash: no up-front capital, lower funding cost, amplified gains *and* losses.

## 6. Lecturer's asides & assignments

- **Assignment 3 (two questions; Q2 stated here):** write Python functions `C+`, `C−`, `P+`, `P−` returning payoffs as functions of S and K, and build strategy payoffs **by composing those functions**, not by hardcoding diagrams: long/short straddle (C⁺ + P⁺), strangle, covered call, protective put (noted ≈ married put), all four bull/bear call/put spreads, iron condor, butterfly, synthetic long/short call and put.
- Practical advice: if stuck in an interview, rebuild payoff diagrams from the payoff function and a table of values; pay attention to x-axis conventions in books/white papers.
- Logistics: notes and assignment to be shared; doubt-clearing session Wednesday; session closed early with New Year wishes (recorded end of Dec 2023, referencing Jan-2024 Nifty contracts).

---

Read the full transcript (single-line file, ~61.5 KB, paged in three slices). Here is the session summary.

# Session 8 — Binomial Option Pricing, Implied Volatility & Monte Carlo

## 1. Topics covered (in order)
1. The option-pricing problem statement and the course's three solution methods: **BOPM** (binomial option pricing model), **MCS** (Monte Carlo simulation), **BS-OPM** (Black–Scholes).
2. One-step binomial model via replication: sell option, buy Δ shares, invest residual in bonds; solving two equations for Δ₀ and V₀.
3. Risk-neutral probabilities and worked numerical examples (call and put).
4. The no-arbitrage condition on u, d, r — detected when a computed "probability" exceeds 1.
5. CRR parametrization of u and d from volatility; multi-step trees and backward induction.
6. Implied volatility: models used in reverse; historical vol vs. market-consensus vol; the volatility surface, skew/smile, and interpolation for OTC pricing.
7. Bachelier's (1905) arithmetic Brownian motion stock model and its Euler discretization.
8. Monte Carlo option pricing demo in Excel; effect of drift and volatility on simulated paths.
9. The five Greeks and sign conventions for long vs. short calls.

## 2. Key definitions & results
- **Replication setup (t = 0):** sell option for V₀ (received), buy Δ₀ shares at S₀ (paid), invest residual V₀ − Δ₀S₀ in a bond growing at (1+r). If V₀ < Δ₀S₀ the residual is borrowed.
- At expiry the stock goes to u·S₀ ("heads"/good state) or d·S₀ ("tails"/bad state); u = (up price)/S₀, d = (down price)/S₀ — pure price ratios.
- **Two equations, two unknowns** (Δ₀, V₀): matching the portfolio value to the option payoff in each state, where payoffs are known: max(uS₀ − K, 0) and max(dS₀ − K, 0) for a call.
- **Risk-neutral probabilities:** p* = (1 + r − d)/(u − d), 1 − p* = (u − (1+r))/(u − d). They satisfy 0 < p* < 1 and sum to 1, but **have nothing to do with real-world probabilities** (real up-probability could be 99% and you'd still price with p*).
- **No-arbitrage condition:** d < 1 + r < u. If violated, the computed p* falls outside [0,1] — the risk-neutral measure does not exist and an arbitrage is present.
- **CRR inputs (continuous compounding):** u = e^(σ√(T/n)), d = e^(−σ√(T/n)), p̂ = (e^(rT/n) − d)/(u − d), where T = time to expiry in years, n = number of steps. p̂ is constant across the tree once u, d are fixed.
- **Implied volatility (IV):** the volatility input that makes a model price (Black–Scholes, binomial, …) match the observed traded option price. "The real use of Black–Scholes/binomial is not to compute the option price — it is to compute implied volatility."
- **Volatility surface / skew:** IV plotted against moneyness is lowest at-the-money and higher ITM/OTM (smile/skew); Black–Scholes assumes it flat. The skew flattens as time to expiry increases.
- **Bachelier model (1905):** dS = μ dt + σ dW_t (arithmetic Brownian motion). μ = drift = expected annual stock return; from quadratic variation, dW_t ~ √dt (i.e., (dW)² = dt).
- **Greeks:** price is a function of five variables — σ, time to expiry, r, spot, strike. Vega = ∂P/∂σ; Rho = ∂P/∂r; Theta = ∂P/∂(time to expiry); Delta = ∂P/∂S; Gamma = ∂²P/∂S². Since one long call + one short call = zero portfolio, any Greek of the long leg equals minus that of the short leg (Δ_long = −Δ_short; numerically equal, opposite sign).

## 3. Formulas & derivations
- **One-step binomial price:** V₀ = 1/(1+r) · [p*·V₁(H) + (1−p*)·V₁(T)] — a discounted risk-neutral expectation.
- Example 1: S₀=100, u=1.01, d=0.99, r=0 → p* = (1−0.99)/(1.01−0.99) = ½. Call K=100 pays 1 (up) / 0 (down) → V₀ = 0.5·1 + 0.5·0 = 0.5.
- Class exercise (put): S₀=100 → {103, 96}, r=5%, K=100. p̂ = (1.05−0.96)/(1.03−0.96) = 9/7 > 1 → **invalid; numbers admit arbitrage** (borrow/short the stock, earn a riskless 105 vs. max outcome 103). Corrected tree {109, 96}: p̂ = (1.05−0.96)/(1.09−0.96) = 9/13; put payoffs 0 and 4 → V₀ = (1/1.05)·(4/13)·4 ≈ **1.17**.
- **Monte Carlo discretization:** S_{t+Δt} = S_t + μ·Δt + σ·√Δt·N(0,1). Excel demo: μ=10%, σ=15%, Δt=1/365, S₀=100, 30 days, 50 paths (NORMINV(RAND())). Option price = e^(−rT) · average of max(S_T − K, 0), with r=5%, T=1/12.
- IV by bisection: market call price 358; model gives 345 at σ=11% and 364 at σ=12% → IV = 11.28%.

## 4. Intuition & examples
- **Drift in ABM:** μ=0 gives a symmetric random walk (paths above 100.1 ≈ paths below 99.9); positive μ tilts paths upward, negative downward.
- **Volatility:** doubling σ from 15% to 30% spreads the paths (~twice the max deviation); since option price = average of payoffs, higher σ → higher price → Vega positive for a long call.
- **Cricket-ball analogy (historical vs. implied vol):** a ball bowled at 140 km/h measured at 110 km/h mid-pitch — averaging past speeds misestimates the speed at the batsman, and the ball may skid (speed up) unpredictably. Historical vol is a *bad* predictor of future vol; vol itself follows a process and has "vol of vol." Asking 100 people for estimates and averaging them = market consensus = implied vol; like a question paper solved collectively by the whole class, the crowd is more likely right.
- **OTC vs. ETO:** exchange-traded options have standard strikes (Nifty in multiples of 50) and standard expiries; pricing an OTC option (e.g., strike 21,637.5, expiry 31 Jan between the 25 Jan and 1 Feb expiries) requires interpolating the IV surface in both strike and expiry, then feeding that IV into the binomial/BS model.
- **Long vs. short call prices:** the *price* is the same for buyer and seller, but P&L differs — if price goes 10→15 the buyer gains 5 and the seller's obligation grows by 5; hence Greeks flip sign.

## 5. Trading-relevant takeaways
- A binomial tree is only as good as its u, d assumptions; in practice these come from IV, which is backed out of traded prices — so models calibrate to the market rather than predict it.
- Watch the no-arbitrage band d < 1+r < u; model outputs outside [0,1] "probabilities" flag arbitrage or bad inputs.
- Vol surfaces are skewed, not flat — single-σ Black–Scholes misprices away from ATM; interpolate IV across strike and expiry for non-standard contracts.
- Historical/realized vol is a poor forecast of future vol; treat IV as the market's forward-looking consensus.
- Greeks are sensitivities to the five pricing inputs; hedging long/short legs cancel exactly.

## 6. Lecturer's asides
- Historical note: Louis Bachelier's 1905 stock-price model (transcript garbles it as "bashier"/"bachar"; "Brownian" appears as "Donan motion").
- Promised to share a Python ("spider") script on the group that plots Delta, Gamma, Vega surfaces vs. spot and strike (he couldn't locate it live).
- No book recommendations in this session. Next session: the same content in continuous time — derivation of the Black–Scholes PDE and BS pricing.

*Transcript quality note: a few segments are obscured by `[Music]` tags (screen-sharing pauses while the lecturer hunts for files/websites); no technical content appears lost there. Numbers occasionally surface mid-sentence (e.g., "275" for the 21,700 call, "10.31%" IV) consistent with an on-screen NSE option chain; figures above are as stated.*

---

I've read the entire transcript (single-line file, paged through all ~38.5 KB in chunks). Here is the technical summary.

---

# Session 9 — Black–Scholes PDE Derivation, Exotic Options & Course Project (Finite Differences)

## 1. Topics covered (in order)

1. **Leftover Greeks recap** — near expiry **Gamma dominates**, far from expiry **Vega dominates**; Delta of a short call is entirely negative; how to study Greeks graphs: always examine **levels, slopes, and curvatures**.
2. **Arithmetic vs Geometric Brownian motion** and their distributional implications.
3. **Derivation of the Black–Scholes PDE** via a hedged (riskless) portfolio and Itô/Taylor expansion with quadratic variation.
4. **Black–Scholes closed-form solution** for a European call (stated, not derived — "out of scope").
5. **Exotic options** — American, digital, barrier, Asian, compound, chooser.
6. **Course project briefing** — numerically solving the Black–Scholes PDE by finite differences.

## 2. Key definitions & results

- **ABM:** dS = μ dt + σ dW → the *change in stock price* is normally distributed (μ, σ, dt are constants; only dW ~ N(0, dt) is random).
- **GBM:** dS/S = μ dt + σ dW → *returns* are normally distributed; after integrating, log S is normal, hence **S is lognormally distributed**. This is why GBM is used to simulate stock prices. (Class example: S_t = 100 → S_{t+Δt} = 110 makes dS/S = 10% return "click".)
- **Wiener-process algebra:** dW = √dt · ε with ε ~ N(0,1); dt² → 0; dt·dW ~ dt^1.5 → 0; **dW² = dt** (from the quadratic-variation assignment — this is the term that survives in stochastic calculus but would vanish in ordinary calculus).
- **Hedged portfolio:** Π = f − Δ·S (long one option f, short Δ shares), so dΠ = df − Δ·dS.
- **Black–Scholes PDE:** ∂f/∂t + ½σ²S² ∂²f/∂S² + rS ∂f/∂S − rf = 0.
- **Closed-form call price:** C = S·N(d₁) − K e^(−rτ) N(d₂), with d₁ = [ln(S/K) + (r + σ²/2)τ] / (σ√τ), d₂ = d₁ − σ√τ (N = standard normal CDF; τ = time to expiry).

## 3. Derivation (as presented)

- Option price f = f(S, t); Taylor-expand df:
  df = (∂f/∂t)dt + (∂f/∂S)dS + ½(∂²f/∂S²)dS² (+ higher-order terms ≈ 0).
- Compute dS² = (μS dt + σS dW)² = μ²S²dt² + σ²S²dW² + 2μσS² dt dW → only **σ²S² dt** survives.
- Substitute into dΠ = df − Δ dS and expand. The **only source of uncertainty is dW** (dt is the fixed discretization step, e.g. 1/365; μ and σ are constants — σ constant being a Black–Scholes assumption).
- **Kill the risk:** set the dW coefficient to zero: σS(∂f/∂S) − ΔσS = 0 → **Δ = ∂f/∂S**. (Lecturer's note: many texts *start* with Δ = ∂f/∂S, which hides *why*; deriving it shows the Wiener term is deliberately weeded out.)
- With this Δ the μ-terms also cancel automatically — **the drift μ disappears**. Implication stressed heavily: no stock-specific growth-rate information is needed to value a perfectly hedged portfolio; the riskless portfolio must grow at the risk-free rate, dΠ = rΠ dt, which yields the PDE above.
- Black & Scholes' bigger achievement (Nobel) was **solving** the PDE analytically, not just deriving it; the analytic solution is on Wikipedia and out of course scope.
- Recap: **three ways to price an option** — (1) binomial tree, (2) discounted expectation of payoff (Monte Carlo), (3) Black–Scholes closed form.

## 4. Exotic options (differ from vanilla by payoff)

- **American** — exercisable any time up to expiry.
- **Digital** — cash-or-nothing (pay fixed amount M if S > K, else 0) and asset-or-nothing (deliver S_T if S > K, else 0); payoff jumps discontinuously at K vs. the vanilla hockey-stick.
- **Barrier** — four subtypes: up-and-in, up-and-out, down-and-in, down-and-out (call/put variants). Up-and-out: worthless if price ever crosses barrier B. Monitoring can be **continuous** (one-second touch kills it), **daily** (checked end-of-day only; intraday breach that recovers doesn't count), or **at expiry only**.
- **Asian** — payoff on an average: max(S̄ − K, 0) or floating-strike max(S − S̄, 0); average can be arithmetic or geometric.
- **Compound** — option on an option (at first expiry you choose whether to enter the underlying option).
- **Chooser** — choose whether the underlying becomes a call or a put.
- **Pricing:** Black–Scholes formula works only for vanilla; exotics need simulation (Monte Carlo or binomial tree) — evolve the price, compute payoff, discount to present value.

## 5. Course project — finite-difference solution of the BS PDE

- Discretize: ∂V/∂t ≈ (V_{i,j+1} − V_{i,j})/Δt, ∂V/∂S ≈ (V_{i+1,j} − V_{i,j})/ΔS. Build an S×T grid: time 0→T in ~10 steps; spot 0→S_max with S_max ≈ 2× spot.
- **Boundary conditions:** at expiry, V = max(S − K, 0); at S = 0, V = 0; at S = S_max, V ≈ S − K (time stops mattering). Recurse **backwards in time** using neighboring grid points until t = 0.
- Schemes: **forward difference, backward difference, Crank–Nicolson** (extra credit for Crank–Nicolson; it's more computationally intensive).
- Deliverables: (1) Word doc deriving the difference equation for V(i,j); (2) neatly labeled/color-coded Excel implementation; (3) Python code — functions, meaningful variable names, comments; (4) comparison graphs of finite-difference prices vs Black–Scholes across discretization steps, spots, and times to expiry (convergence study). European options only — American is too hard. Groups split call vs put (transcript contradicts itself on odd/even assignment — clarify with instructor). A reference paper was shared; no regular assignment this week.

## 6. Trading-relevant takeaways

- **Delta hedging is the foundation of option pricing:** holding Δ = ∂f/∂S short shares against a long option removes all Wiener risk, and the hedged book must earn r — the entire risk-neutral-pricing worldview in one argument.
- **Drift μ is irrelevant to option value** — you cannot arbitrage on growth expectations; only σ, r, S, K, τ matter.
- **Lognormal-price / normal-return assumption** is testable: compute historical log-returns of any stock and inspect the bell curve.
- Near-dated options are Gamma-plays; long-dated are Vega-plays — relevant for structuring straddles/hedges around events.
- Exotic structures (barriers especially) behave discontinuously and depend on monitoring conventions — cannot be priced with vanilla formulas.

## 7. Lecturer's asides

- The PDE derivation is standard — "you will find it even on Wikipedia"; a reference paper on the finite-difference method was shared for the project.
- Pedagogical advice: when learning, plug in actual numbers (100 → 110) until concepts "click"; study graphs via levels/slopes/curvatures.
- Coding advice: reuse the sample code, write functions not free-flowing scripts, no single-letter variables, comment each block.
- Project completion is critical for the course certificate; a Google form on time availability/skills was circulated for group formation. Next meeting is a doubt session.

*Note: the transcript is one continuous unpunctuated line with no timestamps; the Greeks-graph segments refer to an on-screen script/plots that are not visible in text, so details of those plots (e.g., exact Delta-of-short-call values) cannot be reported beyond what was said.*

---

I've read the full transcript (single-line file, paged through all of it). Here is the study summary.

---

# Session 10 — Modern Portfolio Theory, Sharpe Ratio, Beta & Volatility-Managed Portfolios (Intuition-First)

**Note on format:** The lecturer deliberately takes a non-mathematical, intuition-first approach (math is "publicly available on Wikipedia/YouTube"); the session is discussion-driven with a live Excel demo. No stochastic-calculus content (no σ-algebras, martingales, Itô, Girsanov) appears in this session.

## 1. Topics covered (in order)

1. Portfolio basics: weights, degrees of freedom in portfolio construction.
2. Three ways to select securities: fundamental analysis, technical analysis, quant strategies.
3. Risk–reward framework of MPT: risk = standard deviation, reward = expected return.
4. Sharpe ratio — derived from first principles as incremental return per incremental risk vs a risk-free baseline.
5. Two-asset portfolio expected return and variance; role of covariance/correlation.
6. Live Excel experiment: 4 NSE stocks (Infosys, TCS, Reliance, HDFC Bank), 1-year daily data — multiple weight combinations giving identical returns at different risk levels.
7. MPT's flipped optimization: fix expected return as a constraint, minimize variance.
8. Efficient vs inefficient risk; "calculated risk"; cricket analogy.
9. Beta as a risk measure: regression of stock/portfolio returns on index returns.
10. Case study: Nifty 100 Low Volatility 30 index vs Nifty 50 fact sheets — the low-volatility anomaly.
11. Constructing a market-neutral long–short arbitrage from the low-vol anomaly.
12. The quant ecosystem: quant research → quant dev → traders → middle office/risk.
13. Volatility-managed portfolios (Moreira & Muir paper): scaling exposure inversely to variance; India VIX example (March 2020 vs October 2023).

## 2. Key definitions & concepts

- **Portfolio (2 stocks):** invest weight w in stock 1, weight (1 − w) in stock 2. The lecturer counts **3 degrees of freedom**: choosing stock 1, choosing stock 2, and choosing w (then 1 − w is determined).
- **Risk–reward framework (MPT):** risk is defined as the **standard deviation** of the security's returns; reward is the **expected return**.
- **Fundamental analysis:** balance sheet, income statement, cash-flow statement, annual reports, MD&A; models like DCF and free-cash-flow; output is a fair value, compared to market price to judge under/overvaluation. Career path: CA, CFA.
- **Technical analysis:** price-based methods — moving averages, Fibonacci, Darvas box, GMMA (Guppy multiple moving average), support/resistance levels.
- **Quant strategies:** logically developed frameworks not derived from firm fundamentals or chart patterns; inputs are mostly **historical prices plus statistics**; MPT is a major quant framework. A quant either trades because a pattern works (with some probability) or trades against it on new evidence it doesn't.
- **Beta:** slope of the regression of stock/portfolio returns against benchmark (index) returns; β = 1 means risk equal to the index; β > 1 means riskier than the index (demand higher return); β < 1 means lower risk (accept lower return).
- **Efficient vs inefficient risk:** risk that translates into (incremental) return is efficient; extra risk taken while return stays fixed is inefficient risk. "Calculated risk" = taking only risk that pays.

## 3. Formulas & derivations

**Sharpe ratio, built up as pairwise comparison.** Example: Stock 1: 20% return, 15% vol; FD (risk-free): 7%, 0% vol; Stock 2: 11% return, 14% vol. Compare two at a time ("sort three numbers by comparing two at a time"):

- Stock 1 vs FD: incremental return / incremental risk = (20% − 7%)/(15% − 0%) = 13/15
- Stock 2 vs FD: (11% − 7%)/(14% − 0%) = 4/14 (transcript shows the lecturer writing "7/11" — either garbled numbers or a slip; the ranking conclusion is 13/15 > 4/14, so Stock 1 dominates Stock 2)

Since σ(FD) = 0, the general incremental ratio reduces to:

**Sharpe ratio = (μ − r_f) / σ**

**Two-asset portfolio:**
- Expected return: E[R_p] = w₁·E[R₁] + w₂·E[R₂], with w₂ = 1 − w₁
- Variance: Var(R_p) = w₁²σ₁² + w₂²σ₂² + 2·w₁·w₂·ρ·σ₁·σ₂ (transcript says "2·W1·W2·correlation·variance of first plus variance of second" — clearly meant ρσ₁σ₂, i.e. 2w₁w₂·Cov(X,Y), building on Var(X+Y) = Var(X) + Var(Y) + 2Cov(X,Y))
- Without the covariance term, weights would be trivially decided — you'd put 100% in one stock; the correlation term (−1 ≤ ρ ≤ 1) "makes it interesting."

**Matrix form (n assets):** portfolio variance = **wᵀΣw** (weight vector × variance–covariance matrix × weight transpose); Σ is symmetric and independent of weights (a property of the data). Excel mechanics: Analysis ToolPak → covariance; MMULT with Ctrl+Shift+Enter.

**Annualization in the demo:** 247 daily observations; annual vol = daily vol × √247. Demo result: equal-ish weights (25/25/25/25) gave ~10.58% return with ~15.5% annual vol; alternative weights (TCS 0%, more Reliance/HDFC) gave ~the same return with ~14.9% vol — same return for ~1.5% less risk. Portfolio vol (15%) came out **below every individual stock's vol** — the covariance/diversification effect.

**MPT optimization statement:** fix E[R_p] = target (constraint), minimize σ_p². Rationale: any return above r_f requires risk (no free lunch), but the converse fails — not every risk earns return.

**Volatility-managed portfolio (Moreira & Muir):** weight at t+1 = c / Var_t (constant divided by lagged realized variance) — exposure inversely proportional to variance; shown to improve Sharpe ratio because vol spikes are not offset by proportional increases in expected returns (if vol triples, expected return may only rise 1.25–1.5×).

## 4. Intuition & examples

- **Cricket analogy:** "maximize expected runs" is the naive objective; hitting a six every ball takes necessary *plus* unnecessary risk. To score 300 in 50 overs you can take singles (low risk) or attempt 50 sixes (very high risk — high wicket-loss probability). Correct framing: fix the run target, minimize the risk needed. Cricketers *can* hit sixes; they don't because the risk doesn't pay.
- **Low-vol anomaly case study (fact sheets, Dec 2023):** Nifty 100 Low Volatility 30 index — selects the 30 lowest-volatility stocks (by 1-yr std dev of daily log returns) from the Nifty 100 F&O universe, weights inversely to volatility, rebalanced semi-annually. Since inception: **17.42% p.a. with β = 0.76** vs Nifty 50's **11.55% p.a. with β = 1**. Lower risk, ~50% higher return — the Nifty itself is "inefficient risk."
- **Long–short arbitrage construction:** of Nifty 100's β ≈ 1, if 30 stocks average β = 0.76, the other 70 must average β > 1; and if 30 stocks return 17.45% while 100 average 11.55%, the 70 must return **less** than 11.55%. Strategy: **short the 70 high-β stocks, use proceeds to buy the 30 low-vol stocks, park the remainder in the bank.** Worked example: short ₹1,000 of the 70 → buy ₹700 of the 30 → ₹300 left earning interest. Worst-case spread: 17.45% − 11.55% ≈ 5.5% plus ~3–4% interest ≈ ~9–10% on **zero capital and zero net market risk** — "technically infinite return," scalable to any size (₹10 cr or ₹100 cr).
- **India VIX / vol timing:** India VIX = 30-day forward-looking volatility from option prices. Peaked ~70% on 27 March 2020 (→ minimum 10% exposure); ~10.3% on 6 Oct 2023, below the ~15–16% five-year average (→ maximum exposure). Nifty then rose from 19,653 to ~21,894 (+11%) over the following 5–6 months — the low-vol entry paid off.

## 5. Trading-relevant takeaways

- Rank-ordering stocks by Sharpe tells you *which* stocks are good, but never put everything in the top stock — diversification via the covariance term is the whole game.
- Many weight combinations achieve the same expected return; always pick the minimum-variance one. Same return at lower risk means the discarded risk was inefficient.
- Beta (vs a benchmark) and volatility are complementary risk measures; low-β/low-vol portfolios have historically *outperformed* — a directly tradeable anomaly via long low-vol / short high-vol market-neutral books, with self-funded financing (short proceeds fund the long leg; residual cash earns interest).
- Scale exposure inversely to volatility/variance (target-vol or variance-managed sizing): cut to minimum exposure when VIX spikes, max out when vol is below average; this improves Sharpe because vol rises faster than expected returns.
- Strategies decay: your own trading moves prices and erodes the edge ("you yourself change the risk-reward in the market"), so research is a continuous search for the next anomaly. Rebalance the low/high-vol baskets more frequently than the index provider (monthly vs semi-annual) as an enhancement.

## 6. Lecturer's asides

- **Quant ecosystem / career map:** quant **research** (read white papers, reams of data, find and justify the anomaly — the most time-consuming step) → **quant dev** (Python implementation, backtests over 1/2/3 years) → **traders** (execution, periodic rebalancing of the baskets) → **middle office/risk** (daily monitoring of β spread ≥ ~0.25 and return spread ≥ ~5.5%, escalating to traders/quants when limits compress). A typical quant day: read papers, replicate results on your own universe/time period, statistically prove them to management.
- **Recommended paper:** *Volatility-Managed Portfolios* by **Alan Moreira and Tyler Muir** (Yale; transcript mangles it as "Ellen morera and Tyler mu" from "Meale University") — PhD-thesis-grade work with YouTube talks available; covers transaction costs and leverage; lecturer shared the link and assigned replication (paper → Excel → Python) as a project.
- Practical pointers: NSE fact sheets (published monthly, e.g. 29 Dec 2023 edition) are a free research source; NSE historical data downloads and Excel's Analysis ToolPak suffice for replication; copy date+close columns and check date alignment when merging series.
- Advice on session style: as an experienced professional he adds value via intuition and live demos, not by repeating math available online.

**Garbled/uncertain spots:** the Stock-2-vs-FD arithmetic ("7/11") is inconsistent with the stated numbers (should be 4/14); the two-asset variance formula is verbally mangled (ρσ₁σ₂, not "variance of first plus variance of second"); author/university names in the Moreira–Muir reference are mis-transcribed.

---

# Session 11 (Final Session): Course Wrap-up, Career Guidance & Statistics for Trading

## Overview

This is the final session of QF 101 and is deliberately **non-technical in the stochastic-calculus sense** — no new theorems, SDEs, or pricing derivations. Instead, the lecturer (Ajit Kulkarni) spends the entire session on: (a) student feedback, (b) how to continue learning quant finance (books, white papers, podcasts, courses), (c) career guidance, and (d) an extended worked discussion of **how statistics/data science connects to quant trading** via hypothesis testing, pattern mining, and backtesting — with important warnings about confirmation bias and alpha decay. A Garbled section exists around the "30% stock / 70%" strategy mentioned by a student (from the previous session); its exact construction is not restated here.

## Topics Covered (in order)

1. Student feedback round; the lecturer's meta-point on **rigor vs. intuition**.
2. **Recommended books** for continuing beyond the intro course.
3. **White papers from regulators/institutions** as a learning resource (BIS, ISDA, Fed) — including complete/incomplete markets.
4. **Podcasts** — Quantcast by Risk.net.
5. **Certifications and degree programs** (CQF, WorldQuant University, MFE/EPFL, IFMR, Madras School of Economics, IIQF; QuantInsti/QuantInsti-style algo-trading courses discussed and scoped).
6. Career Q&A: freelancing (none exists in QF), internships (risk mgmt → investment banks; trading/investing → hedge funds/AMCs), post-60 careers (consulting/regulator consultation papers vs. trading one's own capital), quant developer job profiles (JP Morgan, Millennium examples; ~80% coding / 20–30% QF; Python/C++ central; CFA/FRM/CQF valued), Kalman filters (use only when the problem demands it), India-based quant jobs growing.
7. **Data science × quant finance: hypothesis testing** — the Fisher "lady tasting tea" example; ANOVA for day-of-week effects; conditional-pattern trading; low-volatility pattern from the previous session.
8. NIFTY 50 index composition and rule-based backtesting (free-float market-cap rule is the constant, not the constituents).
9. Combining fundamental/technical/quant backtests — Venn-diagram overlap and **confirmation bias**.
10. Quantum computing in finance — JP Morgan + IBM white paper on option pricing.
11. Resources for fundamental analysis (CBSE class 11–12 accountancy texts; **Aswath Damodaran**), technical analysis (no specific endorsement), Python libraries (TA-Lib, Zipline, QuantLib, portfolio/alpha-vantage, yfinance, BeautifulSoup, Quandl).
12. **Why published strategies don't make money** — alpha exclusivity, **alpha decay** accelerating with compute/AI, LTCM anecdote (Scholes, Merton, Mullins; founded by John Meriwether of Salomon Brothers), efficient market hypothesis, probabilistic vs. deterministic market knowledge.
13. Close: WhatsApp quant community group for serious participants, LinkedIn.

## Key Ideas & Precise Statements

### Rigor vs. intuition (lecturer's framing)
- 90–95% of the rigorous math (measure changes, Black–Scholes derivations, PDEs) will **not** be used directly on the job; its purpose is to sharpen intuition. Example given: PDE thinking as "if something changes by a small amount, what is the impact on other things."
- Derived value > actual value: option concepts transfer directly to new products (e.g., a **swaption** inherits all optionality concepts from options), so you never start from zero.
- End goal of reading anything: extract intuition, state it in 1–2 sentences.
- Scope humility: this course is the "preface/chapter zero" of quant finance — less than 5–10% of a full QF curriculum.

### Complete markets (referenced, not derived)
- Recommended reading: a **1992 Federal Reserve white paper on complete markets**, praised as a rigorous linear-algebra treatment: arbitrage, Arrow–Debreu securities ("arody deu" in the transcript = Arrow–Debreu), bets, consistent bets, redundant bets, state claims, scalar multipliers, efficient allocation, general equilibrium, hedging. Many papers assume complete markets; this paper defines what that assumption means.

### Hypothesis testing for trading (the core technical content)
- **Fisher's tea-tasting setup (retold as physicist husband/wife):** wife claims she can tell whether milk or tea-water was poured first. If she guesses randomly, P(correct) = 1/2 per trial; over ~100 trials, a hit rate well above 50% (e.g., 70–90%) indicates genuine discrimination rather than luck. This is the template for testing any market observation.
- **Day-of-week effect via ANOVA:** hypothesis "market falls on Mondays" → compare **between-day return variances vs. intraday return variances**; if between-day variance is significantly higher, the day identity matters. Then act on the underlying or its derivatives (e.g., buy puts if a fall is expected).
- **Conditional-pattern template:** estimate P(stock move | event X) — e.g., "if crude oil moves > 3%, the Indian market falls ≥ 2% within a month" — then test whether the pattern is luck or trend before taking a position. Election-cycle patterns (every 4 years in India) given as another candidate.
- **Volatility pattern (from prior session):** when volatility is very low (~10%), stocks tend to rise over the next 1–2 months. Proper validation: take 30–40 years of data, collect every instance of vol ≈ 10%, average the forward returns, plot the distribution, and conclude only from that.
- **Data science → insight; markets → monetization:** patterns in financial data are monetizable via stock/derivatives markets; analogous patterns (e.g., social media analytics) are useless to an outsider without a monetization platform.

### Index backtesting
- NIFTY 50 is a **rule-based set** (top free-float market-cap companies), not a fixed basket. Backtests should condition on the *rule* being constant; constituent turnover is irrelevant. A 1-year beta of 0.5 computed on today's NIFTY is valid as-is — asking whether it would have been 0.5 ten years ago with different constituents is the wrong question.

### Multi-method backtesting & confirmation bias
- Run fundamental, technical, and quant analyses **independently**, then trust only the intersection (Venn-diagram overlap) — higher confirmation probability.
- Warning: a single analyst doing all three sequentially suffers **confirmation bias** — after method 1 yields conclusion C, methods 2 and 3 unconsciously try to prove C ("if you torture the data enough, it will confess to anything"), collapsing three circles into one. Mitigation: independent analysts, or conscious discipline.

### Alpha, alpha decay, and efficient markets
- A published money-making strategy is either one that **never worked** or one that **stopped working** (author now monetizes the book instead). Strategies that work are never shared; they exist but not in the public domain.
- **Alpha decay:** any discovered edge has a finite life (~2–6 months in his telling) before others find it and arbitrage it away; then the search restarts. Bitcoin-mining analogy: everyone races to solve the same problem; once solved, everyone moves to the next one.
- With faster compute and ChatGPT-class tooling, discovery cost rises (9 months → 12 → 15–18 months per new strategy, year over year) while decay accelerates (2 months → 1 month → 15 days of exploitable life). This is why ~95% of traders lose money.
- **LTCM anecdote:** the hedge fund co-founded by John Meriwether (Salomon Brothers) with Myron Scholes and Robert Merton on the board — plus ex-Fed vice-chairman David Mullins — still failed. The people whose formulas underpin a trillion-dollar industry could not make money trading. Cited as a "test case" worth reading in full.
- **Efficient Market Hypothesis:** derivatives pricing rests on it — all available information is already priced in; further moves come only from new information. Hence stock picking/entry/exit timing is knowable only **probabilistically** (e.g., a probability distribution over where the bottom is), never deterministically.
- Anyone claiming to *know* which stock to buy, when to buy, and when to sell — and offering to teach it — is faking; real holders of such knowledge spend their time exploiting it.

## Book & Resource Recommendations (lecturer's asides)

- **John Hull** ("John Cal" in transcript) — *Options, Futures, and Other Derivatives*: the **first** book; do all exercises honestly, struggle 3–4 days per problem before looking anything up.
- **Steven Shreve** ("Steve Shri"), *Stochastic Calculus for Finance*, Vol. I (discrete) then Vol. II (continuous) — his top recommendation; Vol. I: binomial pricing, finite/general probability spaces, independence, σ-algebras, Lebesgue measure; Vol. II: Brownian motion, stochastic calculus, risk-neutral pricing, exotic options, change of numéraire, term-structure models, jump processes. Praised for its exercises — "the finance equivalent of Irodov"; solving both volumes covers 60–70% of any QF course.
- **Frank Fabozzi** ("Frank fauzi/abuzzi"), *Fixed Income Securities* — bonds, treasuries, ABS, MBS, CDOs, agency pass-throughs (but not OTC interest-rate swaps; Hull covers those).
- White papers: **BIS** (incl. quarterly reviews), **ISDA**, **Federal Reserve** (the 1992 complete-markets paper singled out).
- Podcast: **Quantcast by Risk.net** (on SoundCloud/app stores) — practitioner quants explaining their white papers; expect a week of re-listening and glossary-building per episode initially.
- Courses/certifications: **CQF** (expensive; often employer-reimbursed after joining an investment bank), **WorldQuant University** MSc Financial Engineering (free), MFE in the US, **EPFL** (Switzerland), a Paris program (name forgotten), IIT Kharagpur/Kanpur (unsure), **IFMR** PGDM (lecturer's alma mater; QF depth in year 2), **Madras School of Economics** (2-year QF), **IIQF** (friends with industry experience found it elementary), **QuantInsti**-style algo-trading courses (since 2017; trading-oriented, only ~1/4 of a real QF curriculum — QF is ~3/4 derivatives/hedging, which are risk-management instruments, not money-making assets).
- Fundamental analysis: CBSE class 11–12 accountancy textbooks for balance-sheet/income-statement basics, then **Aswath Damodaran** ("Musings on Markets" blog, videos, valuation classes).
- Quantum computing: JP Morgan + IBM (Zurich) joint white paper *"Option Pricing Using Quantum Computers."*
- Python: TA-Lib (technical), Zipline (Quantopian), QuantLib, pyfolio ("pfolio"), Alpha Vantage, yfinance, Quandl, BeautifulSoup; ChatGPT can write integrated backtesting scripts.

## Trading-Relevant Takeaways

- Derivatives are **hedging/risk-management instruments, not assets** — this frames why QF curricula are ~3/4 derivatives.
- Every algo-trading signal is a **conditional statement** tested against history; never trade an untested observation. Use long windows (30–40 years), collect all matching instances, and examine the full distribution of outcomes, not one anecdote.
- Independence of analyses and awareness of confirmation bias are practical necessities when combining fundamental/technical/quant signals.
- Treat any edge as perishable: plan for alpha decay, continuous re-research, and the reality that monetizable knowledge is never published.
- Probabilistic framing only: build probability models (macro, rates, crude, firm-specific inputs) for tops/bottoms rather than seeking deterministic signals.

## Practical / Career Asides

- Lecturer prepared for this course partly with ChatGPT and endorses it as a study aid.
- He sees PDEs "in every job" he's done — others doing the same job don't; training determines what you perceive.
- Freelancing doesn't exist in QF (projects are long-term, interdependent commitments); internships are the entry route — use them to A/B test risk management vs. trading careers.
- Career-shape analogy: risk management ≈ Brownian motion **drift** (steady compounding); trading/hedge-fund careers ≈ high-variance paths with higher possible mean — choose by your volatility tolerance.
- Post-60: risk managers → consulting/regulator consultation-paper contribution (e.g., BIS); investors → trading own capital after financial independence.
- Quant developer roles (JP Morgan, Millennium cited) ≈ 80% coding + 20–30% QF; Python/C++ dominant; India-based quant jobs growing, less need to relocate.
- Kalman filters (and any technique): start from the problem, not the tool — only bring in a method if the problem genuinely requires it.
- Lecturer runs a WhatsApp quant community (job posts, coding questions, HFT subgroups; India + US practitioners) — link shared with project participants.
