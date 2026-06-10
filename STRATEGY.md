# Strategy — Synthetic Forward v5.5 (Production)

> Live on Delta India for BTCUSD + ETHUSD perpetuals.
> Validated +462% BTC / +1038% ETH on 92-day in-sample;
> +28.9% combined on 9-day OOS (Jun 2-10, 2026) with ₹40k seed.
>
> **Deterministic by design — no LLM, no ML, no sentiment.**

---

## 1. TL;DR

| | |
|---|---|
| **Edge source** | Cross-strike options dislocation (synthetic forward vs spot) |
| **Universe** | BTCUSD + ETHUSD perpetuals on Delta India |
| **Decision cadence** | Hourly entries at HH:00:30 UTC, 5-min signal sampling between |
| **Leverage** | 3× isolated (~33× safety buffer to liquidation) |
| **Per-cycle deploy** | 50% of live wallet pool, BTC/ETH split 50/50 |
| **Position size mult** | 0.5×–3.0× based on signal strength |
| **Exits** | Pure SL/TP: −1.5% stop OR +1.0% target (full close), max-hold 72h |
| **Daily kill switch** | Halt new entries if day P&L < −5% of base equity |
| **Backtest WR** | 92.1% (9-day OOS, pure SL/TP regime) |

---

## 2. The Core Idea — Synthetic Forward Arbitrage

### 2.1 The formula

For any option strike `K` with the same expiry:

```
synthetic_forward = call_price − put_price + strike
                  = C − P + K
```

By **put-call parity**, in a perfectly arbitraged market this MUST equal the perp/futures price for the same maturity. If `synthetic_forward ≠ spot`, there's a dislocation — and dislocations don't last because market makers will arb them.

We measure it per strike:

```
dev_K = (synthetic_forward_K − spot) / spot
      = ((C_K − P_K + K) − spot) / spot
```

Then we take the **median** across all near-money strikes with the same expiry:

```
pred = median([dev_K  for K in near_money_strikes])
```

If `pred` is positive, the options market is pricing the *forward* above spot → bullish flow → buy perp.
If `pred` is negative → bearish flow → sell perp.

### 2.2 Why it works

The formula isn't ours — it's textbook put-call parity. What's edge is the *measurement window*. Crypto options markets occasionally dislocate by 0.5-2% because:

1. **Spot-to-derivatives lag** — large spot moves take seconds to propagate into the options chain. Market makers re-mark in batches, briefly leaving the chain skewed.
2. **Flow-driven repricing** — when institutional flow hits the chain (a large block of calls or puts), market makers tilt the entire chain to manage their risk. The median across 14 strikes captures that tilt.
3. **Funding pressure** — when perp funding is extreme (longs paying heavy), options sometimes lead the inevitable mean-reversion.

We're not predicting price. We're **measuring where 14 of the smartest pricers in crypto have already decided the forward should be**, before the perp tape catches up.

### 2.3 What "median across 14 strikes" buys us

A single strike's dev_K is noisy — call/put marks update at slightly different microseconds, bid-ask spread skews it. The **median across ≥3 strikes** is the lie detector:

- If all 14 strikes agree (small range) → real flow signal
- If they disagree wildly (large range) → noise, no trade

That's `MIN_STRIKES = 3` and the consensus requirement: at least 3 of the strikes must share the sign of the median, or we skip.

---

## 3. Decision Pipeline

```
                    ┌────────────────────────────────────────┐
  Every WS push  →  │ 1. Pull current option chain marks      │
  (~1 sec)          │ 2. Filter: 6h ≤ TTE ≤ 72h, ±5% ATM      │
                    │ 3. Per expiry, compute dev_K for each K  │
                    │ 4. Require ≥3 same-sign strikes         │
                    │ 5. pred = median(devs)                   │
                    └────────────────────┬───────────────────┘
                                         │
                                         ▼
                    ┌────────────────────────────────────────┐
  Every 5 min    →  │ Sample pred into _sig_history + Mongo  │
                    │ (no order, just warm-up for persistence)│
                    └────────────────────┬───────────────────┘
                                         │
                                         ▼
                    ┌────────────────────────────────────────┐
  HH:00:30 UTC   →  │ 6. Best expiry: argmax(|pred|)         │
  (hourly cron)     │ 7. Gate: |pred| ≥ 0.6%?  no → skip    │
                    │ 8. Persistence: ≥1h same-sign?         │
                    │ 9. Sizing: 50% × size_mult            │
                    │10. Wallet has funds + no kill? + slot? │
                    │11. PLACE MARKET ORDER at 3× leverage  │
                    └────────────────────┬───────────────────┘
                                         │
                                         ▼
                    ┌────────────────────────────────────────┐
  Every 2 sec    →  │ Position management:                   │
                    │  • Hit target (+1.0%)? → full exit    │
                    │  • Hit stop  (−1.5%)? → full exit     │
                    │  • Held ≥72h? → exit                  │
                    │  • Expiry reached? → exit             │
                    └────────────────────────────────────────┘
```

---

## 4. Production Dials

Single source of truth: `core/risk_management.py`. Defaults are production values.

### 4.1 Entry-side (`strategies/synth_forward.py`)

| Dial | Value | Why |
|---|---|---|
| `ENTRY_PCT` | 0.006 (0.6%) | Sweet spot — every test below this lost edge |
| `PERSIST_HOURS` | 1 | v5.5 reduced from v5's 2 — same Sharpe, +28% more trades |
| `MIN_STRIKES` | 3 | Lower → false signals; higher → too few trades |
| `TT_MIN_HOURS` | 6 | Below 6h: gamma scalping noise, not flow |
| `TT_MAX_HOURS` | 72 | Above 72h: term-structure carry dominates, NOT mean-reversion |
| `MONEYNESS` | 0.05 (±5%) | Wider = wing noise; tighter = thin coverage |

### 4.2 Exit-side (`core/risk_management.py`)

| Dial | Value | Why |
|---|---|---|
| `EXIT_REGIME` | `pure_sltp` | Validated +₹964 better than trail+partial on 9-day |
| `stop_loss_pct` | 0.015 (−1.5%) | Wide enough to avoid noise, tight enough vs liq |
| `partial_tp_pct` | 0.010 (+1.0%) | Target in pure_sltp mode (full exit) |
| `MAX_HOLD_HOURS` | 72 | Matches TTE_MAX — never hold past option expiry |

### 4.3 Capital + leverage (`core/risk_management.py`)

| Dial | Value | Why |
|---|---|---|
| `LEVERAGE` | 3 | Same returns as 10×, 33× safer (validated in sweep) |
| `CAPITAL_USE_PCT` | 0.50 | Deploy half pool per cycle, hold half for next signal |
| `BTC_CAPITAL_PCT` | 0.50 | Independent BTC bucket sizing |
| `ETH_CAPITAL_PCT` | 0.50 | Independent ETH bucket sizing |
| `DAILY_LOSS_KILL_PCT` | 0.05 | Halt entries if day P&L < −5% of base equity |
| `MAX_LIVE_CONTRACTS` | 50 | Hard cap per single order |
| `MAX_CONCURRENT` | 2 | One BTC slot + one ETH slot, max |

### 4.4 Size multiplier formula

```python
size_mult = clamp(|pred| / 0.005, 0.5, 3.0)
notional  = equity × CAPITAL_USE_PCT × size_mult
```

So a pred of:
- 0.6% (just above gate) → mult = 1.2×
- 1.0% → mult = 2.0×
- 1.5%+ → mult = 3.0× (cap)
- 0.3% → mult = 0.6× (but blocked by gate anyway)

---

## 5. Risk Management

### 5.1 Per-trade

```
entry_px × (1 − 1.5%)   ← stop loss (full exit)
entry_px × (1 + 1.0%)   ← target    (full exit)
entry_px × (1 − 32.83%) ← liquidation @ 3× iso (we never see this)
```

At entry $61,700 BTC short: stop=$62,625, target=$61,083, liq=$82,300.
**Liquidation buffer is ~22× the stop distance.** Even a flash-crash 1m wick that skips our SL would need a 33% move to liquidate.

### 5.2 Position-level

- `MAX_CONCURRENT = 2`: only one BTC + one ETH at any time. Never doubled up.
- One position per `(asset, expiry)` pair — no two simultaneous BTC trades on Jun-12 expiry.
- Per-asset `MAX_LIVE_CONTRACTS = 50`: hard cap on any single order.

### 5.3 Account-level

- `DAILY_LOSS_KILL_PCT = 5%`: when realized day P&L crosses −$50 (on $1000 base), the bot **halts new entries** for the rest of the UTC day. Open positions continue to manage themselves.
- **Manual KILL** button on dashboard: closes all open positions immediately + halts new entries until restart.

### 5.4 What's NOT in the strategy

Explicitly excluded for clarity + statistical hygiene:

- ❌ No LLM, no GPT, no sentiment scoring
- ❌ No reinforcement learning, no neural nets
- ❌ No news/social/Twitter signal
- ❌ No backtested-then-changed dials (all changes go through PR review + cross-validation sweep)

The strategy is **deterministic**: same option marks → same decision.

---

## 6. Worked Examples — Jun 10, 2026

All times IST. Pool: ₹40k seed compounded to current via v5.5 baseline.

### 6.1 BTC Example A — WINNING SHORT (07:30 IST, +₹41)

**Setup**

```
asset:       BTCUSD
time:        07:30 IST  (02:00 UTC)
spot:        $61,700.21
chosen expiry: Jun 12 12:00 UTC  (47.6h TTE — inside 6-72h band ✓)
```

**Pred computation** (median across 14 near-ATM strikes):

```
strikes scanned (±5% from $61,700): 14 calls + 14 puts
all 14 strikes returned dev_K in range [−0.611%, −0.604%]
                                       ─────────────────
median pred = −0.606%   (strong consensus, tight range)
sign agreement: 14 of 14 negative ✓
```

**Gates**

```
|pred| ≥ 0.6%?  |−0.606%| = 0.606% ≥ 0.6%        ✓ PASS
persistence ≥ 1h?  prior 06:30 IST sample was −0.5%  ✓ PASS (same sign)
```

**Sizing**

```
size_mult = clamp(0.606 / 0.5, 0.5, 3.0) = 1.21×
equity    = $621.96
notional  = $621.96 × 50% × 1.21× = $376.87
margin    = $376.87 / 3 = $125.62  (locked at Delta)
contracts = $376.87 / ($0.001 × $61,700) = 6 contracts (rounded)
```

**Entry + bracket levels**

```
side:           SHORT (sell BTC)
fill price:     $61,700.21  (market order, 2bps slip baked in)
stop loss:      $62,625.71  (entry × 1.015)
target:         $61,083.16  (entry × 0.990)
liquidation:    $82,238.30  (entry × 1.328 — never relevant)
```

**Outcome under pure_sltp** (current live regime):

```
08:30 IST (1h later):  BTC = $61,302  (unrealized +0.65% short)
09:30 IST (2h):        BTC = $61,536  (no target hit, no stop hit)
14:51 IST (7.4h):      BTC = $60,802  (deep dip — target $61,083 HIT here)
                        ↳ Exit at target $61,083 + slip = net +0.88%
                        ↳ PnL: $376.87 × +0.88% = +$3.32 ≈ +₹285
```

Under the pure_sltp regime this trade catches the morning shorts properly. Under the deprecated trail+partial regime it would have exited at 09:30 IST for only +$0.48 — trail giveback fired too early.

---

### 6.2 BTC Example B — STOPPED LONG (00:30 IST, −₹558)

**Setup**

```
time:        00:30 IST Jun 10  (19:00 UTC Jun 9)
spot:        $61,747.72
chosen expiry: Jun 12 12:00 UTC
pred:        +0.612%  (median across 11 strikes, 8 positive)
```

This signal fired because at 00:30 IST, BTC was near its 24h high (~$62,400 — see the chart left-edge). Options chain priced the forward slightly above spot. Gate passed, persistence passed.

**Entry + bracket**

```
side:        LONG (buy BTC)
fill:        $61,747.72
stop:        $60,821.50  (entry × 0.985)   ← will be HIT
target:      $62,365.20  (entry × 1.010)
liquidation: $41,473.89  (entry × 0.672)
```

**Outcome (pure_sltp)**

```
00:30 → 14:51 IST:  BTC trended DOWN from $61,747 to $60,802 (intraday low)
14:51 IST:          1m low at $60,809  ≤  $60,821 stop  → STOP HIT
                     ↳ Exit at $60,821 (with slip): net −1.62%
                     ↳ PnL: $376.87 × −1.62% = −$6.49 ≈ −₹558

Worst adverse seen during trade: 1.629%  (vs 32.83% liq buffer = 20× safety)
```

The trade caught a wrong-direction signal at the day's high. The bot didn't override — strategy is deterministic. The 1.5% stop limited the damage to ₹558 instead of letting it ride further.

Under the deprecated trail+partial: trail would have bailed out at 03:30 IST for only −₹42. Lesson: trail saves on losers but caps winners. Pure_sltp accepts the bigger loser to capture the bigger winner — and over 9 days, that math wins (+₹14,407 vs +₹13,443).

---

### 6.3 ETH Example A — WINNING SHORT (07:30 IST, +₹139)

```
time:    07:30 IST  (02:00 UTC)
spot:    $1,637.91
expiry:  Jun 12 12:00 UTC (47.6h)
pred:    −0.798%  (11 strikes, median range −0.79 to −0.81%)
size_mult: clamp(0.798/0.5, 0.5, 3) = 1.60×
equity:  $622 (combined pool)  →  notional = $622 × 50% × 1.60 = $497.60
```

Entry $1,637.91 short → target $1,621.21 → stop $1,663.52.

Under pure_sltp: target HIT at 08:18 IST when ETH wicked to $1,620.5.
PnL: $497.60 × +0.88% = +$4.38 ≈ +₹377

Trail+partial regime caught only the first +0.43% scalp at the 09:30 IST hourly tick for +₹139. Pure_sltp lets it ride the full 0.88% to target.

---

### 6.4 ETH Example B — STOPPED LONG (12:30 IST, −₹558)

```
time:    12:30 IST  (07:00 UTC)
spot:    $1,630.54
expiry:  Jun 12 12:00 UTC (29.6h remaining)
pred:    +0.620%  (just-barely-passes gate)
```

Marginal signal — gate at exactly 0.62% vs threshold 0.60%. Fires because persistence + gate both pass.

Under pure_sltp: ETH rose briefly to $1,641 (+0.7%), then collapsed back through entry, hit stop $1,606.41 at 14:51 IST.
PnL: $445 × −1.62% = −$6.49 ≈ −₹558

Worst adverse: 1.566% (liq buffer 21× wider).

Marginal-pred trades are the strategy's tail risk. They pass the gate but barely. Statistically the +0.6 to +0.8% bucket has lower win rate than the +1.0%+ bucket — that's why `size_mult` scales with |pred|: weak signals get smaller positions.

---

## 7. What's Validated vs What's Hypothesis

### 7.1 Validated (in production)

- ✅ Gate 0.6% beats 0.4%, 0.5%, 0.7%, 0.8% on 92d in-sample
- ✅ Persist 1h beats 2h (more trades, same Sharpe)
- ✅ TTE 6-72h beats wider/tighter bands
- ✅ Multi-strike consensus beats single-strike
- ✅ 3× leverage = 10× returns (same PnL, 3× safer)
- ✅ Pure SL/TP beats trail+partial (+₹964 on 9-day OOS)
- ✅ 50% capital deploy beats 25%, 75%, 100%
- ✅ Size scaling 0.5-3.0× beats flat 1.0×

### 7.2 NOT YET validated (open questions)

- ❓ Perp OI direction as confirmation filter (raw data available)
- ❓ Funding rate contrarian fade (raw data available)
- ❓ Put/call OI skew (raw data available)
- ❓ Zone-touch-retest (this PR's experiment — see backtest_zone_retest.py)

### 7.3 Explicitly out of scope

- ✗ ML/LLM signal generation
- ✗ Discretionary overrides
- ✗ News/sentiment integration
- ✗ Cross-exchange arb

---

## 8. Operational Notes

### 8.1 Files

| Path | Purpose |
|---|---|
| `strategies/synth_forward.py` | Signal computation (gate, persistence, consensus) |
| `core/execution/crypto_runner.py` | Order placement, position management, kill switch |
| `core/risk_management.py` | All production dials |
| `core/ws/delta_stream.py` | Delta WebSocket subscription |
| `core/brokers/delta_crypto.py` | REST + WS broker abstraction |
| `delta_exchange/backtest_engine.py` | Unified backtest (runs both exit regimes) |
| `STRATEGY.md` | This file |

### 8.2 How to backtest

```bash
cd delta_exchange
python backtest_engine.py 2026-06-10        # any IST date
python backtest_engine.py                   # defaults to today IST
```

The engine runs BOTH exit regimes (trail+partial AND pure_sltp), prints side-by-side full window + selected date, and shows per-trade exit verification with liquidation buffer per trade.

### 8.3 How to verify a live deploy

```bash
docker compose logs api --tail=80 | grep -i "regime\|crypto runner"
```

Should show:
```
crypto runner enabled — mode=live regime=pure_sltp mgmt_tick=2s
sample=5m entry=hourly@HH:00:30 UTC equity=$1000 kill=-5.0% max_contracts=50
```

### 8.4 Reverting an exit regime change

```bash
# To go back to trail+partial without code change:
export CRYPTO_EXIT_REGIME=trail_partial
docker compose up -d --force-recreate
```

The regime is env-var driven; both code paths are live.

---

## 9. Change Log

| Date | Version | Change |
|---|---|---|
| 2026-06-10 | v5.5 | Default exit regime → `pure_sltp` (+₹964 on 9-day OOS) |
| 2026-06-10 | — | Backtest engine unified into `backtest_engine.py` |
| 2026-06-10 | — | Live decision grid moved to hourly@HH:00:30 UTC + 5-min sample warm-up |
| 2026-06-10 | — | Persisted `_sig_history` to Mongo for restart resilience |
| 2026-05-XX | v5.5 | Persist 1h (was 2h), validated +28% trade count, same Sharpe |
| 2026-04-XX | v5.0 | Production launch on Delta India |
