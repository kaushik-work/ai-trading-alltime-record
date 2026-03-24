# OPTIONS TRADING: COMPREHENSIVE KNOWLEDGE BASE
### For AI Trading Brain — Compiled from Zerodha Varsity, Investopedia, Groww & Standard Options Theory

---

## PART 1: OPTIONS FUNDAMENTALS

### 1.1 What Is an Option?

An **option** is a financial derivative contract that gives the buyer the **right, but NOT the obligation**, to buy or sell an underlying asset at a predetermined price (the **strike price**) on or before a specified date (**expiry date**). The buyer pays a fee called the **premium** to obtain this right. The seller (writer) of the option receives this premium and takes on the obligation.

Key distinction from futures: In a futures contract, both parties are obligated. In options, only the seller is obligated; the buyer has a choice.

---

### 1.2 Call Option (CE) — The Right to BUY

A **Call option (CE = Call European)** gives the buyer the right to **buy** the underlying asset at the strike price.

**When to buy a Call:** You are **bullish** — you believe the underlying price will rise above the strike price before expiry.

**Payoff for Call Buyer:**
- If Spot > Strike at expiry → Profit = (Spot − Strike) − Premium Paid
- If Spot ≤ Strike at expiry → Loss = Premium Paid (maximum loss)
- **Breakeven = Strike Price + Premium Paid**

Example (NIFTY spot = 22,000, buy 22,200 CE at ₹80, lot = 75):

| NIFTY at Expiry | P&L per unit | Total P&L |
|-----------------|--------------|-----------|
| 21,800 | -80 | -₹6,000 |
| 22,200 | -80 | -₹6,000 |
| 22,280 | 0 | ₹0 (breakeven) |
| 22,400 | +120 | +₹9,000 |
| 22,600 | +320 | +₹24,000 |

---

### 1.3 Put Option (PE) — The Right to SELL

A **Put option (PE = Put European)** gives the buyer the right to **sell** the underlying asset at the strike price.

**When to buy a Put:** You are **bearish** — you believe the underlying price will fall below the strike price before expiry.

**Payoff for Put Buyer:**
- If Spot < Strike at expiry → Profit = (Strike − Spot) − Premium Paid
- If Spot ≥ Strike at expiry → Loss = Premium Paid (maximum loss)
- **Breakeven = Strike Price − Premium Paid**

---

### 1.4 Buyer vs Seller — Who Makes Money When

| Position | View | Max Profit | Max Loss | Margin Required |
|----------|------|------------|----------|-----------------|
| Call Buyer | Bullish | Unlimited | Premium Paid | No |
| Call Seller | Bearish/Neutral | Premium Received | Unlimited | YES (large) |
| Put Buyer | Bearish | Strike − Premium | Premium Paid | No |
| Put Seller | Bullish/Neutral | Premium Received | Strike − Premium | YES (large) |

**~85% of options expire worthless** — sellers win more often, but when buyers win they win BIG.

---

### 1.5 Moneyness

**For Call Options (CE) — NIFTY @ 22,000:**
- ITM: Strike < 22,000 (e.g., 21,700 CE, 21,900 CE)
- ATM: Strike ≈ 22,000
- OTM: Strike > 22,000 (e.g., 22,200 CE, 22,500 CE)

**For Put Options (PE) — NIFTY @ 22,000:**
- ITM: Strike > 22,000 (e.g., 22,300 PE, 22,500 PE)
- ATM: Strike ≈ 22,000
- OTM: Strike < 22,000 (e.g., 21,700 PE, 21,500 PE)

---

### 1.6 Intrinsic Value vs Time Value

**Premium = Intrinsic Value + Time Value**

- Call Intrinsic = MAX(0, Spot − Strike)
- Put Intrinsic = MAX(0, Strike − Spot)
- OTM options have ZERO intrinsic value — 100% time value that erodes to zero if underlying doesn't move

---

### 1.7 Black-Scholes Model

```
C = S × N(d1) − K × e^(−rT) × N(d2)
P = K × e^(−rT) × N(−d2) − S × N(−d1)

d1 = [ln(S/K) + (r + σ²/2) × T] / (σ × √T)
d2 = d1 − σ × √T
```

Variables: S=spot, K=strike, r=risk-free rate (~7% India), T=time in years, σ=implied volatility, N=cumulative normal CDF.

**Implied Volatility (IV):** Back-solved from market price. High IV = expensive options. India VIX is the fear gauge:
- VIX > 20: High fear, expensive premiums — avoid buying
- VIX 12–18: Normal range
- VIX < 12: Cheap options — good time to buy

---

## PART 2: THE GREEKS

### 2.1 Delta (Δ) — Directional Sensitivity

Rate of change of premium per ₹1 move in underlying.

| Moneyness | Call Delta | Put Delta |
|-----------|-----------|-----------|
| Deep ITM | ~0.90–1.0 | ~−0.90 to −1.0 |
| ATM | ~0.50 | ~−0.50 |
| OTM | ~0.20–0.40 | ~−0.20 to −0.40 |
| Deep OTM | ~0.05–0.15 | ~−0.05 to −0.15 |

**Delta as probability proxy:** Delta ≈ probability option expires ITM.
- ATM (δ=0.5) → ~50% chance ITM at expiry
- OTM (δ=0.2) → ~20% chance ITM at expiry
- Deep OTM (δ=0.05) → ~5% chance — lottery ticket

**For our strategy:** ATM delta ≈ 0.45. NIFTY moves 100pts → our option gains ~₹45/unit.

---

### 2.2 Gamma (Γ) — Rate of Change of Delta

- Always positive for buyers
- Highest at ATM options
- **Explodes near expiry** — the gamma trap

**Gamma near expiry is dangerous:**
- Monday (7 DTE): Gamma = 0.002, predictable
- Thursday (expiry): Gamma = 0.05 (25× higher)
- Small moves cause massive delta swings → binary "hero or zero" outcomes
- This is exactly why we enforce DTE > 3 rule

---

### 2.3 Theta (Θ) — Time Decay (THE ENEMY OF OPTION BUYERS)

Rate of premium loss per day (always negative for buyers).

**Theta is NOT linear — it accelerates near expiry:**

| DTE | Daily Theta (ATM NIFTY ~₹150 premium) |
|-----|---------------------------------------|
| 20 | −₹5 to −₹8/day |
| 10 | −₹10 to −₹15/day |
| 5 | −₹18 to −₹25/day |
| 3 | −₹30 to −₹40/day |
| 1 | −₹50 to −₹80/day |

**The theta race:** You buy OTM at ₹120 with 10 DTE, NIFTY flat:
- Day 3: ₹85 (lost ₹35)
- Day 7: ₹40 (lost ₹80)
- Day 10 expiry: ₹0 (lost ₹120) — 100% loss on flat market

**You can be directionally correct and still lose money to theta.**

**Theta as % of ATM premium (rule of thumb):**
```
Daily theta % ≈ 0.5% × (30/DTE)
At 30 DTE: ~0.5%/day
At 15 DTE: ~1%/day
At 7 DTE: ~2%/day
At 3 DTE: ~5%/day
At 1 DTE: ~15–30%/day
```

---

### 2.4 Vega (ν) — Implied Volatility Sensitivity

Rate of change of premium per 1% change in IV.
- Always positive for buyers
- Highest for ATM options and longer DTE
- Buyers are long vega — they gain when IV rises

**IV Crush:** After known events (budget, RBI, elections), IV collapses even if direction is correct.
- Pre-budget: IV = 25%, Premium = ₹200
- Post-budget (market moves your way): IV = 14%, Premium = ₹100 — you still lose!
- **Never buy options the day before RBI/budget/elections.**

---

### 2.5 Summary of Greeks

| Greek | What It Measures | Impact on Buyers | When Largest |
|-------|-----------------|------------------|--------------|
| Delta | Price sensitivity | +0 to +1 (calls) | ITM/ATM |
| Gamma | Delta change rate | Always positive | ATM, near expiry |
| Theta | Time decay | Always negative (hurts buyers) | Accelerates near expiry |
| Vega | IV sensitivity | Always positive | ATM, longer DTE |
| Rho | Interest rate | Minimal for weekly options | Ignore for intraday/weekly |

---

## PART 3: RISK MANAGEMENT FOR OPTION BUYERS

### 3.1 Why You Need Direction + Magnitude + Timing

Three things must be right simultaneously:
1. **Direction** — which way the underlying moves
2. **Magnitude** — far enough to cover premium + theta
3. **Timing** — before expiry, ideally early

Being right on 2 of 3 is NOT enough.

---

### 3.2 Why the ₹180–200 Premium Target Was Chosen (NIFTY)

| Premium Range | Delta | Prob ITM | Risk/lot | Issue |
|---------------|-------|----------|----------|-------|
| ₹20–50 (Deep OTM) | 0.05–0.15 | 5–15% | ₹1,500–3,750 | Lottery ticket, wide spreads |
| ₹80–150 (OTM) | 0.20–0.35 | 20–35% | ₹6,000–11,250 | Marginal probability |
| **₹180–200 (near-ATM)** | **0.35–0.45** | **35–45%** | **₹13,500–15,000** | **Sweet spot** |
| ₹300–400 (ITM) | 0.60–0.80 | 60–80% | ₹22,500–30,000 | Acts like futures, less leverage |

**₹180–200 rationale:**
- Delta 0.35–0.45: meaningful move sensitivity
- ~35–45% probability of being ITM at expiry
- Sufficient time value that theta isn't immediately devastating
- Good liquidity, tight bid-ask spread (near-ATM strikes)
- ₹13,500 per lot = ~2% of ₹6.75L account (fits 2% risk rule)
- 1:2+ R:R achievable: 100pt NIFTY move → premium doubles

---

### 3.3 Why DTE > 3 Rule Exists

Inside the last 3 calendar days before expiry:
1. **Theta explosion** — loses 5–30% of premium value PER DAY
2. **Gamma explosion** — small adverse moves wipe out all value
3. **Binary outcome** — "hero or zero," no room for error
4. **Wide bid-ask spreads** — harder to exit at fair price
5. **Psychological pressure** — watching ₹150 become ₹10 in 2 days

**Switching to next week's expiry when DTE ≤ 3:**
- Gives 7–10 days for trade to work
- Theta manageable (₹10–15/day not ₹40–80/day)
- Allows proper stop-loss implementation

---

### 3.4 Position Sizing — The 2% Rule

```
Max loss per trade = Account Size × 0.02
Max lots = Max Loss / (Premium × Lot Size)
```

Example: Account = ₹5,00,000 → Max risk = ₹10,000
- NIFTY CE at ₹180: ₹180 × 75 = ₹13,500 per lot
- Trade 1 lot only if account ≥ ₹6,75,000

**With 50% premium stop-loss:** Actual risk = ₹90 × 75 = ₹6,750/lot → 2× more capital-efficient

**Survival math of 2% rule:**
- 10 consecutive losses: 0.98^10 = 81.7% capital remaining (survivable)
- With 5% rule: 0.95^10 = 59.9% remaining (dangerous)

---

### 3.5 The 50% Premium Stop-Loss

- Entry: buy at ₹180
- Stop: exit if premium falls to ₹90
- Never hold to zero hoping for recovery — time decay compounds the loss
- Set price alert at stop level, exit without hesitation

**Take-profit approach:**
- At 80% gain (premium ₹324): raise stop to breakeven
- At 100% gain (premium ₹360): raise stop to ₹200 (lock profit)
- Let trade run until stop hit or target reached

---

### 3.6 Daily Loss Limits

- Hard daily stop: lose > 3–5% of account → no more trades today
- Consecutive loss stop: 3 losing trades in a row → stop for the day
- Monthly drawdown limit: if account down 15%, halve position sizes

---

### 3.7 No-Trade Days (IV Crush Risk)

Never buy options on:
- Union Budget day (typically Feb 1)
- RBI MPC announcement days (6 times/year)
- Election results days

On these days: IV spikes before the event, collapses after — option buyers get destroyed even when direction is correct.

---

## PART 4: TRADING PSYCHOLOGY & MINDSET

### 4.1 Why Most Option Buyers Lose (SEBI Data)

- ~85–90% of retail F&O traders lose money
- ~85% of individual OTM options expire worthless
- Average retail option buyer loses ₹50,000–₹1,50,000 per year

**10 reasons retail buyers lose:**
1. Buying deep OTM (cheap but near-zero probability)
2. No stop-losses — holding hope positions to expiry
3. Overtrading — every day, every move
4. FOMO entries — buying after a big move (premium already inflated)
5. Not understanding theta — not calculating daily cost
6. Holding through events — IV crush destroys buyers
7. Position sizes too large — one bad trade wipes out weeks of gains
8. Averaging down — "it's only ₹50, buy more" — fatal
9. Random trades — tips, gut feel, WhatsApp groups
10. Ignoring transaction costs — charges can be 5–10% of small premiums

---

### 4.2 Expected Value — The Math That Proves 37% Win Rate Works

```
EV = (Win Rate × Average Win) − (Loss Rate × Average Loss)
```

**With 37% win rate and 1:2.5 R:R:**
```
EV = (0.37 × 2.5R) − (0.63 × 1R) = 0.925R − 0.63R = +0.295R per trade
```

Over 100 trades at ₹10,000 risk each:
- 37 wins × ₹25,000 = ₹9,25,000
- 63 losses × ₹10,000 = −₹6,30,000
- **Net: +₹2,95,000 profit**

**Minimum breakeven win rate** = 1R / (2.5R + 1R) = **28.6%**

This is why R:R matters MORE than win rate. Focus on cutting losses fast and letting winners run.

---

### 4.3 Patience — The #1 Edge

The best traders do NOTHING most of the time. They wait for the perfect setup.

**Signs of a high-quality setup:**
1. Clear directional bias (VWAP aligned, trend confirmed)
2. Score above threshold (≥8.5 NIFTY, ≥9.0 BANKNIFTY)
3. Options priced reasonably (VIX not extreme)
4. DTE > 3 calendar days
5. Near-ATM strike (₹180–200 NIFTY, ₹400–450 BANKNIFTY)
6. Not entering into major resistance/support
7. Volume confirming the move

**"I didn't take a bad trade today" = a win, even if P&L = 0.**

---

### 4.4 The 4 Behavioral Traps

1. **FOMO** — "I missed the move, I'll buy now even though premium is high."
   → Don't. The premium is now expensive and the easy move is done.

2. **Loss aversion** — "I'll hold this losing option — it might come back."
   → It won't. Theta is destroying it every minute. Cut at 50% loss.

3. **Early profit taking** — "I'm up 30%, let me take profits and be safe."
   → 30% gain with 1:2 target still open means you're leaving money. Trail your stop.

4. **Revenge trading** — "I lost ₹15,000, let me make it back with one big trade."
   → This is how accounts blow up. Stop for the day after 3 consecutive losses.

---

### 4.5 Consecutive Losses — Psychological Survival

With 37% win rate, you WILL lose multiple times in a row regularly:
- P(5 consecutive losses) = 0.63^5 = **9.9% per sequence** — very common
- P(4 consecutive losses) = 0.63^4 = **15.7% per sequence** — happens often

**How to survive:**
1. Trust the backtest data over 200+ trades, not 5-trade streaks
2. Never deviate from rules during drawdown
3. Journal every trade — forced discipline
4. Separate "trade quality" from "trade outcome" — a good setup that loses is still good
5. Think in 30–50 trade sample sizes, not individual outcomes
6. Reduce position size to 50% after 5 consecutive losses

---

## PART 5: NSE-SPECIFIC KNOWLEDGE

### 5.1 NIFTY vs BANKNIFTY

| Factor | NIFTY | BANKNIFTY |
|--------|-------|-----------|
| Lot Size | 75 | 15 |
| Strike Gap | 50 pts | 100 pts |
| Weekly Expiry | Thursday | Wednesday |
| Typical Daily Range | 50–200 pts (0.25–1%) | 200–600 pts (0.5–1.5%) |
| IV (typical) | 10–20% | 15–25% |
| Premium target | ₹180–200 | ₹400–450 |
| Score threshold | ≥8.5 | ≥9.0 |
| Capital per lot | ₹13,500 | ₹6,000 |
| Suitable for | Trend following | Volatile momentum |

BANKNIFTY is more volatile — use higher score threshold and wider SL multiplier (1.5×).

---

### 5.2 Weekly vs Monthly Expiry

Our strategy uses **weekly options with 7–10 DTE at entry:**
- Enter Monday–Tuesday of the PREVIOUS week (not current expiry week)
- This gives 7–10 days for the trade to work
- Theta manageable but still allows good leverage
- Never buy current-week options on Wednesday/Thursday (DTE < 2)

---

### 5.3 Options Liquidity by Strike

| Distance from ATM | Bid-Ask Spread | Tradeable? |
|-------------------|----------------|------------|
| ATM | ₹0.50–₹1 | Excellent |
| 100 pts OTM | ₹1–₹2 | Very Good |
| 200 pts OTM | ₹1–₹3 | Good |
| 300 pts OTM | ₹2–₹5 | Acceptable |
| 500+ pts OTM | ₹5–₹20 | Risky |

**Never buy options with premiums below ₹50** — bid-ask spread becomes too large a percentage.

---

### 5.4 The "Hero or Zero" Phenomenon Near Expiry

OTM options near expiry become binary:
- Hero: NIFTY gaps massively your way → option 3×–10× in 1 day
- Zero: NIFTY flat or moves against → 100% loss

**The math of chasing heroes (10 attempts):**
- 1 Hero (3×): +₹120 gain
- 9 Zeros (100% loss): −₹360 loss
- **Net: −₹240 even on a "lucky" hero**

Our ₹180–200 + DTE > 3 rules avoid this trap.

---

### 5.5 NSE Settlement Rules

1. **European-style**: Index options can only be exercised AT expiry, not before
2. **Cash settlement**: No physical delivery — settled at intrinsic value
3. **STT trap for exercising ITM options**: STT = 0.125% of NOTIONAL VALUE (not premium) — much higher than the 0.0625% for selling. Always SELL ITM options before expiry, never let them exercise.
4. **Auto-exercise**: ITM options at expiry auto-exercise — but STT trap applies!
5. **Trading hours**: 9:15 AM – 3:30 PM IST, Monday to Friday

---

### 5.6 India VIX — The Fear Gauge

India VIX measures expected 30-day volatility of NIFTY options:
- **VIX > 20**: High fear → premiums expensive → require larger spot move to profit → reduce lot size 50%
- **VIX 14–20**: Elevated → normal caution
- **VIX 10–14**: Normal → standard sizing
- **VIX < 10**: Low fear → cheap options → best time to buy

---

## KEY FORMULAS QUICK REFERENCE

```
# Breakeven
Call breakeven = Strike + Premium
Put breakeven  = Strike − Premium

# Intrinsic Value
Call intrinsic = MAX(0, Spot − Strike)
Put intrinsic  = MAX(0, Strike − Spot)

# Time Value
Time Value = Premium − Intrinsic Value

# Max Risk (Buyer)
Max Loss = Premium × Lot Size

# Expected Value
EV = (Win% × Avg Win) − (Loss% × Avg Loss)

# Position Size
Lots = (Account × 0.02) / (Premium × Lot Size)

# Stop Loss
Exit premium = Entry Premium × 0.50

# Theta Rule of Thumb
Daily theta % = 0.5% × (30 / DTE)   [of ATM premium]
```

---

## THE 10 COMMANDMENTS OF OPTION BUYING

1. **Never buy options with DTE ≤ 3** — gamma/theta trap, binary outcomes
2. **Only buy near-ATM (₹180–200 NIFTY, ₹400–450 BANKNIFTY)** — avoid lotteries
3. **Always have a stop-loss before entry** — exit if premium drops 50%
4. **Never risk more than 2% of account per trade** — position sizing is survival
5. **Calculate daily theta before entering** — know what flat market costs you
6. **Check India VIX** — don't buy when VIX is very high (expensive premiums)
7. **Trade only with the trend** — VWAP direction, higher timeframe alignment
8. **Patience** — no setup = no trade (being flat is a valid, profitable position)
9. **Think in R:R, not win rate** — 35% win rate with 1:3 R:R is excellent
10. **The exit matters more than the entry** — know when you're wrong and act immediately

---

*Compiled from: Zerodha Varsity Options Theory, Investopedia Options Guide, Groww Options Primer,*
*NSE India official specifications, SEBI F&O data, Black-Scholes theory.*
*Purpose: AI Trading Brain knowledge base for NIFTY/BANKNIFTY weekly options backtesting system.*
