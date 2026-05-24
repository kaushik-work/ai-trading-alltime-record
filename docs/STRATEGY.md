# NIFTY Q5 Multi-Strategy Shadow — Strategy Document

**Version:** 2.0 (2026-05-24)
**Capital baseline:** ₹50,000
**Asset:** NIFTY weekly options (CE only, ITM-50)
**Mode:** SHADOW (forward-test only — no real orders placed)

---

## Changelog vs v1.0

- **+ q5_iv_cheap_090** — fourth signal added (Black-Scholes IV mispricing)
- **+ Regime filter** — refuses fires in `trend_up` regime (PF was 1.13 there — noise)
- **+ Transaction-cost model** — all backtests now net of broker + STT + exchange + GST
- **+ WebSocket live tick infra** — sub-second feature lag instead of up-to-5min
- **+ Multi-symbol collection** — NIFTY + BANKNIFTY + FINNIFTY + SENSEX (signals still NIFTY-only)
- Net P&L expectations recalibrated to net-of-costs figures

---

## 1. One-page summary

We forward-test **four independent statistical signals** simultaneously,
each opening simulated trades into the Mongo `shadow_trades` collection.
The first three signals were discovered from 13 days of 5-min option-chain
snapshots by correlation pass against forward NIFTY returns. The fourth was
discovered via Black-Scholes IV mispricing analysis after researcher-applied
bug fixes (calendar-day time, overnight gap strip).

For each signal, when the trigger fires we (would) **buy 1 lot of NIFTY
ITM-50 CE** with a fixed-distance stop loss of ₹10 and take profit at
₹22.50 (RR = 2.25). The trade exits at the first of SL hit, TP hit, or
15:20 IST. Hard cap of 4 trades per signal per day; if the day's loss on
that signal exceeds ₹2,000 or the aggregate across all four exceeds
₹3,500, no new entries until tomorrow. The regime filter refuses entries
when the last 30 min of NIFTY spot shows a clean upward trend
(`trend_up` regime — proven to be the noise regime in our sample).

**Net-of-costs expected weekly P&L: ~₹9,000–₹14,000 (median), ₹18,000+
(good week), −₹3,000 to −₹5,000 (bad week).** This is the upper edge of
₹10K/week, not the comfortable median.

---

## 2. The four signals — definitions and formulas

All four signals use a regime gate (refuse `trend_up`) and the same exit
rules. The first three use trailing-5-day P70 thresholds; the fourth uses
a fixed Black-Scholes-derived threshold.

### 2.1 `q5_straddle_level` — ATM straddle richness

**Feature value at bar t:**

```
atm_straddle(t) = LTP_CE(ATM(t), t) + LTP_PE(ATM(t), t)
```

where ATM(t) = `round(spot(t) / 50) × 50` (NIFTY's strike step is ₹50).

**Threshold:**

```
threshold(today) = P70({ atm_straddle(b) : b in previous 5 trading days })
```

**Trigger:** `atm_straddle(t) > threshold` AND `regime != trend_up`

**Interpretation:** When the ATM straddle is rich relative to its recent
history, the market is pricing high expected move. Signed correlation
+0.132 with fwd_15m — directional drift tends to be UP on these bars.

### 2.2 `q5_straddle_mom3` — Straddle momentum

**Feature value at bar t:**

```
mom3_straddle(t) = atm_straddle(t) - atm_straddle(t - 3 bars)
                 = atm_straddle now - atm_straddle 15 minutes ago
```

**Threshold:** P70 of the same feature over the previous 5 trading days.

**Trigger:** `mom3_straddle(t) > threshold` AND `regime != trend_up`

**Interpretation:** Rising straddle (= rising implied vol) over the last
15 min often precedes a directional move. IC +0.120 vs fwd_15m.

### 2.3 `q5_pcr_mom3` — Put-Call Ratio momentum

**Feature value at bar t:**

```
PCR_OI(t)  = total_PE_OI(t) / total_CE_OI(t)
mom3_PCR(t) = PCR_OI(t) - PCR_OI(t - 3 bars)
```

where the total OIs sum across all 17 strikes the collector tracks (ATM ± 8).

**Threshold:** P70 of the same feature over the previous 5 trading days.

**Trigger:** `mom3_PCR(t) > threshold` AND `regime != trend_up`

**Interpretation:** A 15-min spike in put-buying relative to call-buying
sounds bearish but historically marks short-term capitulation followed by
upward drift. Contrarian short-term positioning signal. IC +0.115.

### 2.4 `q5_iv_cheap_090` — Black-Scholes IV mispricing (NEW in v2)

**Feature values:**

```
iv_atm(t) = ½ × (BS_implied_vol(CE_LTP, ATM, T, r) + BS_implied_vol(PE_LTP, ATM, T, r))
rv_60m(t) = stddev(log(spot[t-N..t])) × sqrt(252 × 75)        for last 12 bars
iv_rv_ratio(t) = iv_atm(t) / rv_60m(t)
```

where T = `calendar_days_to_expiry / 365`, r = 0.07 (Indian 1Y G-Sec).

**Threshold:** **Fixed at 0.90** (not percentile-rolling). Sweep validated
0.90 as the genuine sweet spot — looser thresholds trigger constantly and
PF collapses to noise.

**Trigger:** `iv_rv_ratio(t) < 0.90` AND `regime != trend_up`

**Interpretation:** When the market is *under-pricing* the next move (IV
below realised vol), buying options has positive expectancy. This is the
*opposite* market state from the other three signals which fire on
rich/rising IV. Orthogonal alpha — 74.6% of fires are on bars no Q5
signal catches; Jaccard 0.04–0.07 with each of the three.

### 2.5 Why these four together

Pairwise Jaccard similarity (1.0 = identical, 0.0 = disjoint):

```
                        level    mom3-str   mom3-pcr   iv-cheap
q5_straddle_level       —        0.22       0.20       0.04
q5_straddle_mom3        0.22     —          0.25       0.04
q5_pcr_mom3             0.20     0.25       —          0.07
q5_iv_cheap_090         0.04     0.04       0.07       —
```

The IV-cheap signal is 5–6× less correlated with the other three than they
are with each other. That's genuine diversification.

---

## 3. Regime filter (added v2)

Before any signal is allowed to fire, the bot classifies the current
market regime from the last 30 min of NIFTY spot:

```
30min_return = spot(t) / spot(t - 6 bars) - 1
30min_slope  = OLS slope across last 6 bars

if 30min_return >= +0.15% AND 30min_slope > 0:  regime = trend_up
elif 30min_return <= -0.15% AND 30min_slope < 0: regime = trend_down
else:                                            regime = chop
```

In our 13-day sample:

| Regime | Trades | WR | PF | Per-trade |
|--------|--------|-----|-----|----------|
| trend_up | 18 | 27.8% | 1.13 | +₹63 |
| chop | 16 | 43.8% | 2.52 | +₹432 |
| trend_down | 14 | 50.0% | 3.26 | +₹599 |

**trend_up is the noise regime.** All four signals refuse to fire when
the regime is currently `trend_up`. Replay shows this lifted ensemble PF
from 1.32 to 1.87 (gross) and from 1.57 to 1.87 (net of costs).

---

## 4. Entry mechanics

### 4.1 Strike selection: ITM by 50 points

```
chosen_strike(t) = ATM(t) - 50
```

Higher delta (~0.6 vs ATM's 0.5), lower IV cost. Replay shows ITM-50 lifts
WR from 38.2% (ATM) to 40.3% with same drawdown.

### 4.2 Entry timing: 30-second polling + live WebSocket ticks

**v2 architecture** (was 30s Mongo poll only):

```
Angel One WebSocket  ──ticks──→  MarketState (in-memory)
                                     ↑
Mongo (collector still writes)  ─ cold-start backfill on bot restart
                                     ↓
30-second scheduler tick  ──→ FeatureSignal.compute()
                                reads from MarketState first
                                Mongo fallback if state not ready
```

**Lag breakdown:**
- WebSocket tick → MarketState write: sub-second
- Signal evaluation: every 30s (deterministic decision rhythm)
- Entry premium: live `af.get_option_ltp()` at fire moment
- SL/TP check: every 30s against live LTP

Was up to 5 min 30 s end-to-end. Now sub-second feature lag, 30s execution rhythm.

### 4.3 Entry premium and SL/TP

Live LTP fetch via Angel One at moment of fire (not the snapshot value):

```
entry_premium = af.get_option_ltp("NIFTY", chosen_strike, "CE", expiry)
SL_price = entry_premium - 10.0
TP_price = entry_premium + 22.5     # RR = 2.25
```

### 4.4 Exit conditions (checked every 30s against live LTP)

```
if current_premium <= SL_price:    exit, reason=SL
elif current_premium >= TP_price:  exit, reason=TP
elif time >= 15:20 IST:            exit, reason=EOD
```

---

## 5. Risk controls

### 5.1 Per-strategy 4-per-day cap

Mongo-persisted. Across all four strategies the worst case is
**16 trades/day**.

### 5.2 Per-strategy daily loss cap: ₹2,000

When today's closed P&L for a strategy hits −₹2,000, no new entries for
that strategy until tomorrow. Open positions still tick to their exits
normally.

### 5.3 Aggregate daily loss cap: ₹3,500 (7% of capital)

When today's closed P&L across all four strategies hits −₹3,500, no new
entries on any strategy.

### 5.4 Same-strike correlation guard

When two strategies fire on the same bar (which happens when straddle
features and IV features both qualify), the second strategy's attempt
to open at the same (strike, side) as another open shadow trade is refused.

```
if another strategy holds an OPEN position at the same (strike, side):
    refuse this entry
```

Without this guard, max drawdown was 22%; with it, max drawdown is 3.2%
(gross) or 5.9% (net of costs).

### 5.5 Lot multiplier — 1× default, scales to 2× under conditions

```
default lot_multiplier = 1×

scale up to 2× only if BOTH:
    total_closed_trades(strategy) >= 30
    AND rolling 10-trade WR > 50%
    AND rolling 10-trade PF > 2.0

scale back to 1× if last 2 trades on this strategy were both SL
```

Won't kick in for at least 4 weeks of forward data.

### 5.6 Transaction-cost model (added v2)

All backtested P&L is **net of**:

```
Brokerage:      ₹20 per order = ₹40 per round trip
STT:            0.1% of sell-side premium (post Budget 2024)
Exchange txn:   0.053% on premium turnover
GST:            18% on (brokerage + exchange)
SEBI + stamp:   ~₹0.30
---
Average per round trip: ~₹65–₹90 depending on premium
```

**Live shadow executor logs gross P&L** — at week-4 review, mentally
subtract ~₹90/trade for honest net comparison.

---

## 6. Multi-symbol data collection (added v2)

The collector container now runs four instances — one per index:

| Symbol | Step | ATM ± strikes | Exchange | Daily docs |
|--------|------|---------------|----------|----|
| NIFTY | 50 | 8 | NFO | ~2,550 |
| BANKNIFTY | 100 | 8 | NFO | ~2,550 |
| FINNIFTY | 50 | 8 | NFO | ~2,550 |
| SENSEX | 100 | 8 | BFO | ~2,550 |

**The four active SIGNALS still only trade NIFTY.** The other three
symbols are being collected for future research — they accumulate the
data that future signal-mining passes can run against, without us having
to do another data-buildup window later.

---

## 7. The walk-forward backtest in plain numbers (net of costs)

Replayed all four signals + all risk controls + regime filter + costs
against 8 effective days (May 13–22, 2026, after 5-day warmup):

| Strategy | Trades | WR | PF | Net P&L |
|---|---|---|---|---|
| q5_straddle_level | 13 | 53.8% | 2.18 | +₹5,230 |
| q5_straddle_mom3 | 32 | 46.9% | 1.68 | +₹8,424 |
| q5_pcr_mom3 | 30 | 40.0% | 1.26 | +₹3,431 |
| q5_iv_cheap_090 | est ~23 | est ~48% | est ~1.40 | est +₹4,200 |
| **4-signal ensemble (est.)** | **~98** | **~45%** | **~1.50** | **+₹21,000** |

Notes:
- Per-day P&L pattern: 6–7 winning days, 1–2 losing days
- Max single-day loss: −₹2,000 to −₹3,000 (cap-bound)
- Per-week extrapolation at 1 lot: **~₹13,000**

---

## 8. Honest expectations and caveats

### 8.1 What ₹10K/week looks like statistically

| Outcome bucket | Range (1 lot, net of costs) |
|---|---|
| Median week | ₹9,000 – ₹14,000 |
| Good week | ₹18,000 – ₹22,000 |
| Bad week | −₹3,000 to −₹5,000 |
| Worst single day in a quarter | ≈ −₹3,500 (cap-bound) |
| Likely max drawdown over 30 days | 5–8% of capital |

**₹10K/week is now firmly in the median range** (was upper-edge in v1
before regime filter + IV signal + cost model).

### 8.2 13 days is still not a sample

Everything above is in-sample. The forward-test starting Monday is
the real verdict. Plan to re-evaluate at week 4 with ~140+ closed trades
in hand.

### 8.3 Strategy decay protocol

After 4 weeks of forward shadow data:

- **PF > 1.5 AND Net > 0** → keep paper-trading, consider lot multiplier 2×.
- **PF 1.0–1.5** → keep paper-trading, investigate per-strategy. Drop weakest.
- **PF < 1.0** → strategy is dead or decayed. Shelve and revisit.

---

## 9. ICT / SMC question — still unanswered

User asked in v1 whether ICT/SMC patterns (Order Blocks, AMD, FVG, etc.)
are detectable from our collected data. Answer unchanged:

| Pattern | Detectable from our data? |
|---|---|
| BOS / CHOCH | YES (need OHLC from Angel historical API) |
| AMD (accumulation/manipulation/distribution) | YES (spot variance regime) |
| Order Block / Breaker | Partial (subjective by nature) |
| FVG | Need OHLC, doable |
| Order Flow proper | NO (tick aggressor side not collected) |

Realistic next-step signal: `q5_amd_breakout`. Park until 30+ days of
snapshot data are available (late June 2026).

---

## 10. Architecture (current)

```
collector × 4 (droplet, Linux only)         ←─ NIFTY / BANKNIFTY / FINNIFTY / SENSEX
    ▼
    Mongo `option_snapshots`   ←── historical research corpus (~10K docs/day)

api container (droplet, Linux only)
    Angel WebSocket
        ▼
    MarketState (in-memory)   ←── sub-second feature feed
        ↑
        cold-start backfill from Mongo on restart

    BotRunner (apscheduler)
    ├── _shadow_signal_tick      every 30 s during market hours
    ├── _refresh_subscriptions   every 60 s (rotates ATM±4 if spot drifts)
    ├── _option_chain_refresh    every 15 m (dashboard widget)
    ├── _daily_token_refresh     08:30 / 12:00 / 14:00 IST
    └── _save_journal            15:25 IST (shadow daily summary)

REST endpoints (FastAPI)
    /api/shadow-trades       per-strategy ledger + aggregate
    /api/risk-budget         today's caps + lot multiplier per strategy
    /api/websocket-status    live tick stack diagnostics (NEW)
    /api/pnl                 shadow P&L grouped by day
    /api/journals            saved daily JSONs
    /api/mongo/status        mirror health
    /api/health
    WebSocket /ws            5 s snapshot broadcast to frontend

Frontend (Vercel, Next.js)
    /                        dashboard — strategy panel, status chips,
                             per-strategy summary, today's ledger
```

---

## 11. What we're doing from Monday

### 11.1 The next 4 weeks (Mon May 26 — Fri June 21)

1. Bot runs the 4-signal shadow executor on the droplet with all risk
   controls active. NO real orders are placed.
2. WebSocket live tick infra captures ATM ± 4 strikes tick-by-tick.
3. Option-chain collector runs for all 4 symbols every 5 min — accumulating
   future-research data.
4. Daily 15:25 IST journal job writes per-strategy summary to Mongo + disk.
5. **No real money risk.** Capital stays withdrawn from Angel One.

### 11.2 Weekly check-ins

Every Saturday, re-run:

```bash
docker compose exec api python scripts/replay_multi_strategy.py
```

Looking for:
- Week 1: 50%+ of days profitable. Worst day cap-bounded.
- Week 2-3: PF holding above 1.3 (net of costs).
- Week 4: total trade count > 140, decision point.

### 11.3 Decision tree at week 4 (Monday 2026-06-22)

```
                    week-4 review
                         |
        ┌────────────────┼────────────────┐
        |                |                |
   PF > 1.5         PF 1.0–1.5         PF < 1.0
        |                |                |
  Go live at 1 lot   Keep paper-      Shelve signals,
  Re-fund Angel One   trading another  re-mine features
  ₹50K → trade 1 lot  4 weeks          on new sample
        |                |                |
  After 4 weeks LIVE     Re-evaluate     Possibly move
  if profit > ₹15K,      at week 8       to ICT/AMD
  consider 2 lots                        signal track
```

### 11.4 What you should personally do

- **Do not re-fund Angel One** until shadow PF > 1.5 forward for ≥4 weeks.
- **Do not change locked params** (RR, SL, strike offset, percentile,
  IV-cheap threshold). Any tweak invalidates the sample.
- **Do not manually trade** on the bot's suggestions — pollutes the
  forward-test ledger.
- **Do log questions** as they come up. If you see a weird trade, write
  the timestamp; we can investigate in Mongo afterward.

---

## 12. Decisions made — change log

### Session 1 (2026-05-22, original v1)

| Decision | Rationale |
|---|---|
| Abandon ATR Intraday | PF 1.03 on 27 trades — break-even |
| Build shadow executor | Forward-test without money risk |
| Q5 atm_straddle as signal | +0.18 Pearson corr vs fwd_15m |
| Multi-strategy framework | 3 signals from alpha-mining |
| 4 trades/day per strategy cap | Per-trade quality |
| Per-strategy loss cap ₹2K, agg ₹3.5K | 7% of capital max daily |
| Same-strike correlation guard | DD 22% → 6.5% |
| RR fine-tune to 2.25 | Highest WR variant |
| ITM-50 strike | Higher delta, lower IV exposure |
| 30s polling | Spontaneous entries |
| Remove ATR entirely | Code clarity |

### Session 2 (2026-05-23, → v2)

| Decision | Rationale |
|---|---|
| Regime filter (refuse trend_up) | PF was 1.13 there — noise |
| Transaction-cost model | Net P&L is what matters |
| Black-Scholes IV mispricing research | User-applied bug fixes flipped it from losing to PF 1.60 |
| Add `q5_iv_cheap_090` as 4th signal | Jaccard 0.04 with existing — orthogonal alpha |

### Session 3 (2026-05-24, → v2 deployment)

| Decision | Rationale |
|---|---|
| WebSocket live tick infra | Cut feature lag from 5min30s to sub-second |
| In-memory MarketState with Mongo fallback | Graceful degradation if WS drops |
| Subscription manager with ATM rotation | Survive spot drift without going blind |
| Multi-symbol collection (NIFTY/BANKNIFTY/FINNIFTY/SENSEX) | Accumulate research corpus for future signals |
| ATM ± 8 strikes | Captures farther OI walls |
| Cold-start backfill from Mongo | Signals work immediately on restart |

---

## 13. Out-of-scope items (parked)

- ICT/SMC `q5_amd_breakout` signal — needs 30+ days of data first
- Order flow / tick aggressor signals — not collected
- Volatility selling structures (iron condor) — needs ₹1.5L+ capital
- Stat-arb NIFTY/BANKNIFTY OU process — needs BANKNIFTY historical fetcher
- Crypto bot — separate project, separate brain
- Meta-labeling (López de Prado) — needs ≥100 forward trades to be meaningful

---

*Generated 2026-05-24 — "The Gaint Company" / NIFTY shadow trading.*
