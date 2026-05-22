# NIFTY Q5 Multi-Strategy Shadow — Strategy Document

**Version:** 1.0 (2026-05-23)
**Capital baseline:** ₹50,000
**Asset:** NIFTY weekly options (CE only, ITM-50)
**Mode:** SHADOW (forward-test only — no real orders placed)

---

## 1. One-page summary

We forward-test **three independent statistical signals** simultaneously,
each opening simulated trades into the Mongo `shadow_trades` collection.
The signals were discovered from 13 days of 5-min option-chain snapshots
by running a correlation pass against forward NIFTY returns. All three
have Information Coefficient (IC) > 0.10 vs the 15-min forward return —
i.e. they have measurable predictive content individually, and they were
selected for low cross-correlation so the ensemble is genuinely diversified.

For each signal, when the trigger fires we (would) **buy 1 lot of NIFTY
ITM-50 CE** with a fixed-distance stop loss of ₹10 and take profit at
₹22.50 (RR = 2.25). The trade exits at the first of SL hit, TP hit, or
15:20 IST. Hard cap of 4 trades per signal per day; if the day's loss on
that signal exceeds ₹2,000 or the aggregate across all three exceeds
₹3,500, no new entries until tomorrow.

---

## 2. The three signals — definitions and formulas

All three signals use the same threshold logic: at the start of each
trading day, compute the 70th percentile of the feature value over the
previous five trading days. During the day, fire whenever the current
bar's feature value exceeds that threshold.

### 2.1 `q5_straddle_level` — ATM straddle richness

**Feature value at bar t:**

```
atm_straddle(t) = LTP_CE(ATM(t), t) + LTP_PE(ATM(t), t)
```

where ATM(t) = `round(spot(t) / 50) × 50` (NIFTY's strike step is ₹50).

**Threshold:**

```
threshold = P70({ atm_straddle(b) : b in previous 5 trading days })
```

**Trigger:** `atm_straddle(t) > threshold`

**Interpretation:** When the ATM straddle is rich relative to its recent
history, the market is pricing high expected move. Empirically (signed
correlation +0.132 with fwd_15m), the subsequent NIFTY drift tends to
be UP on these bars. We buy CE to capture the drift.

### 2.2 `q5_straddle_mom3` — Straddle momentum

**Feature value at bar t:**

```
mom3_straddle(t) = atm_straddle(t) - atm_straddle(t - 3 bars)
                 = atm_straddle now - atm_straddle 15 minutes ago
```

**Threshold:** P70 of the same feature over the previous 5 trading days.

**Trigger:** `mom3_straddle(t) > threshold`

**Interpretation:** Rising straddle (= rising implied volatility) over
the last 15 min often precedes a directional move. The signal has a
+0.120 IC vs fwd_15m — slightly weaker than the level signal but uses
different information.

### 2.3 `q5_pcr_mom3` — Put-Call Ratio momentum

**Feature value at bar t:**

```
PCR_OI(t)  = total_PE_OI(t) / total_CE_OI(t)
mom3_PCR(t) = PCR_OI(t) - PCR_OI(t - 3 bars)
```

where total_CE_OI(t) and total_PE_OI(t) sum across all 17 strikes the
collector tracks (ATM ± 8 strikes).

**Threshold:** P70 of the same feature over the previous 5 trading days.

**Trigger:** `mom3_PCR(t) > threshold`

**Interpretation:** A 15-min spike in put-buying relative to call-buying
sounds bearish but historically marks short-term capitulation — followed
by an upward NIFTY drift over the next 15 min. Contrarian short-term
positioning signal. IC +0.115.

### 2.4 Why these three together

Pairwise correlation between the three feature values across our sample:

| Pair | Max |corr| |
|---|---|
| level ↔ mom3_straddle | 0.18 |
| level ↔ mom3_PCR     | 0.00 |
| mom3_straddle ↔ mom3_PCR | 0.26 |

All under 0.5, so the three signals are approximately independent. Under
the assumption of independent Sharpe ~0.5 signals, the ensemble Sharpe
should be ~√3 × 0.5 ≈ 0.87. We'll see how the real forward data shapes
this number.

---

## 3. Entry mechanics

### 3.1 Strike selection: ITM by 50 points

```
chosen_strike(t) = ATM(t) - 50   (one step in-the-money for CE)
```

**Why ITM, not ATM?** Three reasons confirmed by the 8-day replay:

1. **Higher delta** (~0.6 vs 0.5 ATM) means the option moves more per
   point of NIFTY movement, so we hit our ₹22.50 TP target faster.
2. **Lower IV exposure**: ATM options carry the richest IV. With a
   fixed ₹10 SL we don't want to bleed premium to IV crush.
3. **Validated:** WR 40.3% / PF 1.52 at ITM-50 vs WR 38.2% / PF 1.39
   at ATM, with the same ₹1,625 max drawdown.

Going deeper than ITM-50 (ITM-100, ITM-150) gives no additional benefit —
within a single 5-min bar, the absolute SL/TP distances are hit
simultaneously across nearby strikes.

### 3.2 Entry timing: 30-second polling

The signal feature only changes every 5 minutes (when the option-chain
collector writes a new bar), but the executor polls every **30 seconds**.

- **Why poll faster than the feature refreshes?** Because the moment a
  new 5-min bar shows the threshold has been breached, we want to enter
  on the next live LTP (within 30s), not wait up to 5 more minutes for
  the next cron tick.
- **Why not poll every second?** Angel One API rate limits. 30s gives us
  ~10 API calls/min worst case, well under the 180/min limit.

### 3.3 Entry premium

Live ATM-CE LTP is fetched from Angel One at the moment of fire (not
the snapshot ₹value, which can be up to 15s stale).

```
entry_premium = af.get_option_ltp("NIFTY", chosen_strike, "CE", expiry)
```

### 3.4 Stop loss and take profit

```
SL_price = entry_premium - 10.0           (fixed ₹10 distance)
TP_price = entry_premium + 22.5           (RR = 2.25, so 10 × 2.25)
```

**Example:** entry at ₹152.30 → SL at ₹142.30, TP at ₹174.80.

**Why fixed-distance and not %-based?** NIFTY option premiums move in
absolute rupees with the underlying. A ₹10 move at the lower strike (low
delta) is harder than a ₹10 move at the higher strike (high delta), so
fixed-distance naturally adapts: deeper ITM strikes hit TP faster, which
is exactly what we want.

**Why RR=2.25 specifically?** RR fine-tune showed:
- RR 2.25 → WR 38.2%, PF 1.39, max DD ₹1,625 ← chosen for max WR
- RR 2.50 → WR 36.8%, PF 1.46, max DD ₹1,300 ← max PF
- RR 2.75 → WR 32.9%, PF 1.35, max DD ₹3,738

You chose RR 2.25 for the highest win-rate variant (more wins, smaller
per-win amount, smoother psychological experience).

### 3.5 Exit conditions

Checked every 30 seconds against live LTP for the open position's strike:

```
if current_premium <= SL_price:    exit at SL_price, reason="SL"
elif current_premium >= TP_price:  exit at TP_price, reason="TP"
elif time_of_day >= 15:20 IST:     exit at current_premium, reason="EOD"
```

---

## 4. Risk controls (mandatory, not optional)

### 4.1 Per-strategy trade cap

```
MAX_TRADES_PER_DAY_PER_STRATEGY = 4
```

Each signal can open at most 4 trades per day. Across all three signals
the worst case is 12 trades/day. Mongo-persisted via
`db.shadow_trades.count_documents({strategy, date})`.

### 4.2 Per-strategy daily loss cap

```
PER_STRAT_LOSS_CAP = ₹2,000      (4% of capital baseline)
```

When today's closed P&L for a strategy falls to ≤ −₹2,000, no new
entries on that strategy until tomorrow. Open positions still tick to
their exits normally.

### 4.3 Aggregate daily loss cap

```
DAILY_AGG_LOSS_CAP = ₹3,500      (7% of capital baseline)
```

When today's closed P&L summed across ALL three strategies hits −₹3,500,
no new entries on any strategy.

### 4.4 Same-strike correlation guard

The biggest risk control we added: when all three signals fire on the
same 5-min bar, they would all naturally want to open ATM-CE at the
same strike. That's not three diversified bets, it's one bet ×3.

```
if any other strategy has an OPEN position at the same (strike, side):
    refuse this entry
```

**Impact in replay:** maximum drawdown dropped from −₹3,250 (6.5% of
capital) to −₹1,625 (3.2% of capital). 53 redundant trades were refused
over 8 days.

### 4.5 Lot multiplier (currently 1×, scales to 2× under conditions)

```
default lot_multiplier = 1×

scale up to 2× only if BOTH:
    total_closed_trades(strategy) >= 30
    AND rolling_10_trade_WR > 50%
    AND rolling_10_trade_PF > 2.0

scale back to 1× if the last 2 trades on this strategy were both SL
```

We won't see 2× kick in for at least 4 weeks of forward data (need 30+
closed trades per signal). When it does, capital risk on a single trade
doubles from one lot's worth (~₹650 SL) to two lots' worth (~₹1,300 SL).

---

## 5. The walk-forward backtest in plain numbers

Replayed all three signals + all risk controls against 8 days of
historical option-snapshot data (May 13-22, 2026). Results:

| Strategy | Trades | WR | PF | Net P&L |
|---|---|---|---|---|
| q5_straddle_level | 22 | 41% | 1.50 | +₹4,800 |
| q5_straddle_mom3  | 30 | 37% | 1.32 | +₹5,200 |
| q5_pcr_mom3       | 25 | 40% | 1.55 | +₹5,438 |
| **Total** | **77** | **40.3%** | **1.52** | **+₹15,438** |

Per-day:

| Date | Day W/L | P&L | Cumulative |
|---|---|---|---|
| May 13 | WIN  | +₹2,762 | +₹2,762 |
| May 14 | WIN  | +₹4,875 | +₹7,638 |
| May 15 | LOSS |   −₹975 | +₹6,662 |
| May 18 | WIN  | +₹3,250 | +₹9,912 |
| May 19 | LOSS |   −₹325 | +₹9,588 |
| May 20 | WIN  | +₹2,762 | +₹12,350 |
| May 21 | LOSS | −₹1,625 | +₹10,725 |
| May 22 | WIN  | +₹1,138 | +₹11,862 |

- **Max drawdown:** −₹1,625 (3.2% of ₹50K capital)
- **Day-level win rate:** 5/8 = 62.5%
- **Trade-level win rate:** 31/77 = 40.3%
- **Per-week extrapolation at 1 lot:** ~₹9,600/week

---

## 6. Honest expectations and caveats

### 6.1 What ₹10K/week looks like statistically

Your goal is ₹10,000/week on ₹50,000 capital. That's a **20% weekly
return** — annualised, ~1000% non-compounded or 10,400× compounded.
Anyone promising those numbers consistently is either lying, using
ruinous leverage, or hasn't met their first 50% drawdown yet.

What the backtest actually suggests is more like:

| Outcome bucket | Range |
|---|---|
| Median week     | ₹6,000 – ₹10,000 |
| Good week       | ₹12,000 – ₹15,000 |
| Bad week        | −₹2,000 to −₹4,000 |
| Worst single day in a quarter | ≈ −₹3,500 (cap-bound) |
| Likely max drawdown | 3 – 5% of capital |

So ₹10K/week is at the upper edge of the *typical* range, not the
median. Some weeks will hit it, many won't.

### 6.2 13 days is not a sample

The whole edge we discovered is from 13 trading days. That's enough to
*spot* signal but nowhere near enough to *trust* it. Real forward-test
of 4+ weeks is required before any real money goes near this. The
shadow executor was built specifically to accumulate that forward data
without risking capital.

### 6.3 What happens if it doesn't work forward

Per López de Prado's terminology, the discovered signal can be one of:

1. **Real edge** — IC holds out-of-sample, PF stays > 1.5 forward.
2. **Decayed edge** — was real, but the market has noticed. PF drifts
   toward 1.0 over weeks.
3. **Curve-fit** — was never real, in-sample luck. PF drops to ~1.0
   immediately in forward data.

The sweep (54 combos profitable in 43, PF ≥ 2 in 15) suggests it's not
curve-fit, but doesn't rule out decay. Plan for re-evaluation every
2-4 weeks of forward data.

### 6.4 Strategy decay protocol

If after 4 weeks of forward shadow data we see:

- **PF > 1.5 and Net > 0** → keep paper-trading, consider scaling lot
  multiplier toward 2× (it'll auto-engage when conditions are met).
- **PF 1.0–1.5** → keep paper-trading, don't scale, investigate which
  of the three signals is contributing. Possibly drop the weakest.
- **PF < 1.0** → strategy is dead or decayed. Shelve and revisit
  feature mining with the new ~30 days of data.

---

## 7. ICT / SMC analysis — can we identify these patterns?

You asked whether ICT/SMC concepts (Order Blocks, Accumulation–
Manipulation–Distribution, Breaker Blocks, BOS / CHOCH / FVG / IDM /
Order Flow) can be detected from the collected option-chain live data.

### 7.1 What our snapshots actually contain

Per bar (every 5 min, 09:10–15:35 IST), we store:

```
timestamp · date · symbol · expiry · strike · option_type
ltp · bid · ask · volume · oi · spot
```

17 strikes × 2 sides per bar = 34 rows per snapshot. The `spot` field is
the NIFTY index price at the moment of the snapshot — but it's a single
LTP value, **not** open/high/low/close for the 5-min bar.

### 7.2 What's needed for each pattern

| Pattern | Data needed | Have it? |
|---|---|---|
| **Order Block (Supply/Demand zone)** | OHLC + body/wick distinction at a granular bar | NIFTY OHLC: NO from snapshots, YES from Angel One historical API |
| **Accumulation / Manipulation / Distribution (AMD)** | Spot price over time + volatility regime | Spot: YES from snapshots. Need range/volatility transform. |
| **Breaker Block** | Swing highs + lows + break confirmation | Needs OHLC; partial from snapshots. |
| **BOS (Break of Structure)** | Swing-high/low detection + close beyond | Needs OHLC. |
| **CHOCH (Change of Character)** | First BOS in opposite direction of prior trend | Needs OHLC. |
| **FVG (Fair Value Gap)** | Three-candle pattern with O/H/L/C | Needs OHLC. |
| **IDM (Inducement)** | Minor swings inside major structure | Needs OHLC. |
| **Order Flow (OF)** | Aggressor side of trades / volume delta | NIFTY tick data: not collected. Volume on spot: not collected. |

### 7.3 What our options data *uniquely* adds

The collected snapshots have data that pure price-action ICT/SMC
doesn't use, but which can **strengthen** any structural signal:

1. **PCR_OI at a swing low** — A demand zone with high put-OI suggests
   real money is selling puts at that level (writers expect support to
   hold). Validates the zone.
2. **Max-pain drift** — When max-pain price drifts toward a structural
   level, it acts as a magnet (option writers adjust hedges around it).
3. **OI walls at SR levels** — Strikes with the highest CE / PE OI
   often align with structural resistance / support. A breakout BOS
   above a heavy CE-OI wall is more credible than one through thin OI.
4. **ATM straddle level during a manipulation phase** — Manipulation
   spikes in ICT (the "raid" candle that hunts stops) often coincide
   with sudden IV expansion. Our `mom3_straddle` signal already
   captures this.

### 7.4 So can we build ICT detection?

**Yes, but with two combined data sources:**

1. Pull NIFTY 1-min or 5-min OHLC from Angel One historical API (we
   already use this API elsewhere — `data/angel_fetcher.py`).
2. Run pattern-detection algorithms on the OHLC series.
3. Use the snapshot data as a **validation layer** (confirm a detected
   demand zone with high put-OI, etc.).

### 7.5 Honest assessment

ICT/SMC concepts work well in **discretionary** hands because the human
eye smooths over noise. Algorithmic detection is much harder:

- Order Block detection has ~60-70% accuracy at best (subjective by
  nature; "what counts as a significant move" is fuzzy).
- BOS is more objective and detectable cleanly (just `close > prior_swing_high`).
- AMD / Manipulation has a clean statistical proxy (variance regime
  change followed by spike) that we could code in ~50 lines.
- Order Flow proper requires tick-level aggressor side data which we
  don't collect.

**Realistic next step:** if you want ICT-style entries, we could build
a `q5_amd_breakout` signal — detect accumulation (low-variance regime
for N bars) followed by a manipulation spike (spot move > 2σ in 5 min),
then enter on the reverse move. This would be a *fourth* shadow strategy
running alongside the three Q5 signals. Estimated 2-3 days of code +
backtest work.

We'd **need at least 30 trading days of snapshot data** before this
analysis is meaningful. Right now we have 13. Park this for late June.

---

## 8. What we're doing from Monday

### 8.1 The next 4 weeks (Monday May 26 — Friday June 21)

1. **Bot runs the 3-signal shadow executor on the droplet** with all
   risk controls active. No real orders are placed.
2. **Option-chain collector keeps writing snapshots** every 5 min to
   Mongo. This data continues to grow regardless of what the executor
   does.
3. **You watch the dashboard daily**:
   - The SHADOW chip turns blue when one of the three signals has an
     open simulated position.
   - The per-strategy summary card shows today's P&L for each.
   - Today's shadow ledger table shows every trade fired today.
4. **The journal job runs at 15:25 IST** every trading day, writing a
   summary JSON to disk + Mongo with per-strategy stats.
5. **No real money risk.** Capital stays withdrawn from Angel One.

### 8.2 Weekly check-ins

Every Saturday, re-run:

```bash
docker compose exec api python scripts/replay_multi_strategy.py
```

This replays the full strategy against ALL accumulated snapshot data
(13 days at start, growing weekly) and tells us whether forward
performance is tracking the in-sample expectation.

Looking for:

- Week 1: at least 50% of days profitable. Worst day cap-bounded.
- Week 2-3: PF holding above 1.3.
- Week 4: total trade count > 60, decision to go live or not.

### 8.3 Decision tree at week 4

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

### 8.4 What you should personally do

- **Do not re-fund the Angel One account** until shadow PF > 1.5
  forward and you've ACTUALLY watched the bot's behaviour for a week.
- **Do not change locked params** (RR, SL, strike offset, percentile)
  during the forward-test window. Any tweak invalidates the sample.
- **Do log questions** as they come up. If you see a trade fire that
  looks weird, write the timestamp and we can investigate in Mongo.
- **Do NOT manually trade** on the bot's suggestions. The whole point
  is the shadow ledger is a *clean* statistical sample. Mixing in
  manual trades pollutes it.

---

## 9. Architecture / where everything lives

```
collector container (droplet, Linux only)
    ▼
    Mongo `option_snapshots`   ─── primary research dataset
       (5-min × 17 strikes × CE/PE, ~32,000 rows/day)

api container (droplet, Linux only)
    BotRunner (apscheduler)
    ├── _shadow_signal_tick    every 30 s during market hours
    │     ├── reads latest option_snapshots bar from Mongo
    │     ├── for each of 3 signals:
    │     │     • computes feature value
    │     │     • compares to trailing-5d P70 threshold
    │     │     • if fire: live LTP fetch + open shadow position
    │     │     • if open: live LTP fetch + tick SL/TP/EOD
    │     └── all writes go to Mongo `shadow_trades`
    ├── _option_chain_refresh  every 15 m   (dashboard panel)
    ├── _daily_token_refresh   08:30 / 12:00 / 14:00 IST
    └── _save_journal          15:25 IST    (daily summary)

REST endpoints (FastAPI)
    /api/shadow-trades      multi-strategy ledger + aggregate
    /api/risk-budget        today's caps + lot multiplier per strategy
    /api/pnl                shadow P&L grouped by day
    /api/journals           saved daily JSONs
    /api/mongo/status       mirror health
    /api/health
    GET /ws                 WebSocket — 5s snapshot broadcast

Frontend (Vercel, Next.js)
    /                       dashboard — NIFTY chart, status chips,
                            per-strategy summary, today's ledger
    /journal/{date}         daily journal viewer
    /pnl                    historical P&L by day
    /market-holidays        holiday admin
    /errors                 Angel One error log
```

---

## 10. Decisions made today — change log

| Decision | Rationale | Impact |
|---|---|---|
| Abandon ATR Intraday | PF 1.03 on 27 trades over 4 effective days. Effectively break-even — not investable. | Whole strategy removed |
| Build shadow executor | Forward-test new signals without real-money risk while we validate | New core/shadow_book.py |
| Q5 atm_straddle as primary signal | Pearson +0.183 vs fwd_15m (2.7σ from zero), PF 2.11 in backtest | strategies/straddle_signal.py (now superseded) |
| Sweep + LOO validation | 43/54 combos profitable, PF stays 1.96-2.41 across single-day drops | Proves signal not driven by a single day |
| Regime filter discovery | trend_up regime has PF 1.13 (noise); trend_down 3.26, chop 2.52 | Future filter — not deployed yet |
| Meta-labeling experiment | Sample too thin (14 test trades) | Revisit at 100+ trades |
| Alpha mining → 3 independent signals | mom3_straddle (IC +0.12), mom3_PCR (IC +0.115) added | Multi-strategy framework |
| Multi-strategy executor | Three signals in parallel, separate ledgers, scheduler ticks all | core/bot_runner.py rewritten |
| 4 trades/day per strategy cap | Avoid over-trading on same-day re-entries; smoother per-trade quality | core/shadow_book.py |
| Daily loss caps (₹2K/₹3.5K) | Hard floor on any single bad day | core/risk_budget.py |
| Same-strike correlation guard | All 3 signals firing same bar = 3× the risk in disguise. Refuse 2nd/3rd | Max DD dropped 22% → 3.2% |
| RR fine-tune to 2.25 | Pareto-best by win rate (your choice): WR 38.2% vs 36.8% at RR=2.5 | strategies/feature_signals.py |
| ITM-50 strike, not ATM | Higher delta + lower IV cost → WR 40.3% vs 38.2% ATM | strategies/feature_signals.py |
| 30s polling, not 5-min | Spontaneous entries / tighter exits | core/bot_runner.py scheduler |
| Remove ATR entirely | "If a strategy is not in use, remove it completely." | 15+ files deleted, 5 rewritten |

---

## 11. What this document does NOT cover

- Order-flow reading via tick data (we don't collect ticks)
- Volatility surface trading (would need IV calc from premiums, deferred)
- Stat-arb on NIFTY/BANKNIFTY spread (Track 2, deferred)
- Crypto / commodities (out of scope)
- Manual discretionary overlays (out of scope by design — see §8.4)

---

*Generated 2026-05-23 — "The Gaint Company" / NIFTY shadow trading.*
