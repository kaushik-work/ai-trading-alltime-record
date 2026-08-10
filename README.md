# AI Trading All-Time Record

Research and execution platform for **NSE index options** (Angel One) and
**crypto perpetual futures** (Delta Exchange India).

> **No strategy is live on either venue.** `_get_strategies()` returns `{}`,
> `ENABLE_CRYPTO_RUNNER=false`, `ENABLE_NSE_RUNNER=false`. Every strategy was
> deleted in `62f89c9` after measurement killed it. The execution layer, the
> data collectors and the research harness are intact and working.
>
> That is a deliberate state, not an outage. What this repo is good at is
> **killing bad strategies cheaply**, and it has killed several.

## The one idea

Every strategy this project has run was profitable on paper and lost money
when measured properly. The response was to build the measuring apparatus
first and hold everything to it:

| Rule | Why |
|---|---|
| Fill at the bar's **close**, never its label | A 5-minute label/close mismatch was worth **+₹384,000** of imaginary profit |
| Apply every declared cost | A `PERP_FEE_BPS` that existed but was never referenced turned **+23.89% into −8.21%** |
| Measure the **entry** before tuning an exit | No exit rule harvests an edge the entry does not have |
| Measure a quantity's **distribution** before gating on it | A 0.60% entry gate sat above the 0.404% maximum ever observed and fired **0 times in 1,869 observations** |
| Score on **expectancy**, never win rate | A 50%-WR strategy with small wins loses to a 29%-WR one with big ones |
| **TRAIN / VALIDATE / TEST**, and TEST is spent once | With ~23 hypotheses, one clears p<0.05 by chance |
| A missing data point is a **counted miss**, not a dropped row | Silently dropping sessions where a strike vanished manufactured a **+19.67pt edge from nothing** |

Full working, with the numbers, in [`docs/RESEARCH_LEARNINGS.md`](docs/RESEARCH_LEARNINGS.md).

## What has been measured

| Strategy | Verdict |
|---|---|
| Crypto price-action S/R | Real 9–12 bps edge, structurally **below** its ~14 bps cost floor |
| NSE synthetic forward | Gate unreachable — never fired once in production |
| Breakout-retest (index) | +165 / +259 / +320 pts — positive in all three splits |
| Breakout-retest (options) | VALIDATE **−₹80,769** — the index edge did not survive the instrument |
| Variance risk premium | Survives every split, but only in a **naked** structure that cannot be margined |
| Liquidity sweeps (BTC/ETH 5m) | No edge — negative in all four cells, gross, before costs |

### The lens roster: 8 built, 1 with a measured edge

390 NIFTY sessions, identical snapshots for every lens, TRAIN 2021–23 /
VALIDATE 2024. TEST (2025–26) is **unspent**.

| lens | reads | TRAIN | VALIDATE | state |
|---|---|---|---|---|
| **`volume_oi`** | OI walls, volume profile, OI build | **+1.66** p=0.0012 | **+1.49** p=0.0527 | **PROBATION 0.50** |
| `vwap` | session VWAP z-score | −2.31 p=0.0014 | −1.06 | SHADOW — and a −0.769 echo of `volume_oi` |
| `ict_smc` | order blocks, FVG, sweeps | −4.25 p=0.0000 | −0.68 | SHADOW |
| `greeks` | 25δ risk reversal (tilt) | −0.54 | −1.08 | SHADOW |
| `smile` | IV curvature (butterfly) | +0.53 | +0.18 | SHADOW |
| `momentum` | ATR range breakout | +1.38 | −1.21 | SHADOW |
| `liquidity` | `no_trade` density, volume HHI | context lens | splits contradict | SHADOW |
| `vision` | renders the chart, asks a model | unmeasurable by replay | — | SHADOW, pinned 0 |

Adding a lens is meant to be cheap — a new one votes at weight **0** until
attribution promotes it, so a bad idea costs a journal entry rather than money.
Seven lenses have cost exactly that.

**What did *not* work, each properly tested:**

- **Combining them.** Equal weighting scored VALIDATE +0.22 bps against
  `volume_oi`'s +1.49 alone — three negative lenses outvote one positive one.
- **A rejected lens as a *filter*.** `ict_smc`, the worst voter, was the best
  gate on TRAIN (+4.82, bootstrap p=0.0010) and collapsed to +0.55 (p=0.75) on
  VALIDATE.
- **Deliberation and the journal.** Both looked like they paid (+4.14, +5.01 bps)
  until controlled against a random subset of the same size — each arm also
  trades *fewer bars*. Only the conviction gate survived that control (p=0.0203).

**What did:** trade only `volume_oi`'s top confidence tercile. VALIDATE +3.70 bps
on n=503. One parameter, the lens's own confidence, no fitted interaction.

## Architecture

Two venues, one research spine. Everything a lens reads is a `MarketSnapshot`,
produced identically by the live path and the replay path — so a lens cannot
tell which built it, and every lens is backtestable by construction.

```
  Angel One (NFO/BFO)                    Delta India
  SmartWebSocketV2 ticks                 WS marks + REST
  option chain, VIX, futures             perps: BTC / ETH / XAUT
          │                                     │
          └──────────────┬──────────────────────┘
                         ▼
                 MarketSnapshot                    ← one shared observation
                         │
                         │
      ROUND 0 ── every lens reads it ALONE ──────────────────────────┐
        ┌──────┬──────┬──────┬───────┬────────┬─────────┬────────┐   │  attribution
        ▼      ▼      ▼      ▼       ▼        ▼         ▼        ▼   │  scores THIS
    volume_oi vwap ict_smc greeks  smile  momentum liquidity vision  │  round only
        └──────┴──────┴──────┴───────┴────────┴─────────┴────────┘   │
                         │                                           │
      ROUND 1 ── now they hear each other ─────────────────────────  ┘
                         │      hold · revise · defer
                         │      (vwap defers to volume_oi: at −0.769
                         │       correlation it is an echo, not a second vote)
                         ▼
      ROUND 2 ── the council resolves ONE call
                         │      lead lens · objections · stand aside
                         │      peers may OBJECT, never CONFIRM
                         ▼
              journal EVERY decision      including the ones it declined —
                         │                attribution needs the control group
                         ▼
                 sentinel_client          the brain tier's ONLY route to an order
                         │  signed intent
                         ▼
        ┌────────────────────────────────┐
        │  SENTINEL  (VPS, static IP)    │  the only process holding order
        │  AngelBroker + GTT OCO         │  credentials. Dead-man's switch
        │  dead-man's switch             │  flattens if the brain goes dark.
        └────────────────────────────────┘
```

**The tier split is structural, not a convention.** Angel requires a whitelisted
static IP for order placement and for nothing else — market data reads from
anywhere. So the machine doing the thinking physically cannot place an order,
holds no order credentials, and there is a test asserting it imports no
order-placing symbol.

### Lens mortality

A lens is not a strategy; it is one perspective on a shared snapshot. Lenses
have a measured lifecycle:

```
SHADOW ──> PROBATION ──> ACTIVE
             ^              │
             └── SUSPENDED <┘ ──> RETIRED
```

Scored on `E[pnl | voted FOR] − E[pnl | voted AGAINST]` in rupees. Minimum 30
closed trades before a weight moves, rolling-window refit, frozen during market
hours, and demotion deliberately faster than promotion. A lens that cannot read
the snapshot is *absent*, not neutral — ten consecutive errors bench it even
while profitable.

**Round 0 stays independent so this remains computable.** Once a lens has heard
its peers, "what is that lens worth?" has no answer, and the whole promote /
suspend / retire machinery runs on exactly that number. A purely deliberative
council is one whose members can never be fired.

### Two mechanisms that run but cannot yet trade

Both are built, journaled, and visible on the dashboard — and both are pinned
off, because measurement did not clear them:

- `COUNCIL_DELIBERATION_BINDING = False` — round 1 annotates the decision but
  the traded call comes from round 0 plus the conviction gate.
- The **adaptive quorum** (demand more agreement when the tape is hard) measured
  consistently positive in both splits, +0.67 and +1.27 bps, and reached
  significance in neither.

This is the SHADOW rule that governs lenses, applied to mechanisms. Present and
auditable from day one; load-bearing only once live attribution earns it.

### Lenses that re-tune themselves

`nse/selftune.py`. A lens learns a **percentile** — "trade the top third" — never
a value; the value is recomputed from its own recent distribution. That is what
makes a threshold survive a regime change, and its absence is why an ATR gate
fitted on TRAIN kept 11% of TRAIN and 6% of VALIDATE.

Guards, because this is an overfitting engine wearing the costume of
adaptiveness: causal window only (asserted), 200-observation minimum, shrinkage
toward the measured prior, and a hard `[0.5×, 2×]` band that logs a **warning**
when hit — "the data wants more than allowed" is a finding for a human, not
something to absorb. Self-tuning may never flip a direction convention.

## Repo layout

```
core/
  chart/            venue-neutral price-action: ATR-relative levels, liquidity
                    sweeps, setups, risk sizing, OHLC cache, chart rendering
  brokers/          delta_crypto.py — HMAC REST + WS-first reads
  execution/        crypto_runner.py, sentinel_client.py
  ws/               delta_stream.py
  risk_management.py, sr_levels.py, mongo.py
nse/
  snapshot.py       the shared observation every lens reads
  lenses/           volume_oi · vwap · ict_smc · greeks · smile · momentum ·
                    liquidity · vision  (+ base contract, + bootstrap.py which
                    records what each one measured and the weight it earned)
  council.py        two-round deliberation, then one resolved call
  journal.py        end-of-day record; strict $lt so a session cannot read itself
  brain.py          per-lens attribution, weights, lifecycle FSM
  selftune.py       percentile-targeted recalibration, banded and causal
  execution/        options_runner.py (brain tier) · sentinel_client.py
  ws/               angel_stream.py — SmartWebSocketV2 feed
  broker/           angel_broker.py — orders + GTT OCO brackets (SENTINEL ONLY)
  backtest/         replay · lens_harness · options_harness · costs · loaders
  quant/            black_scholes · expiry_calendar · volume_profile ·
                    spread_study
sentinel/           the VPS execution service + signed intent protocol
api/                FastAPI: REST, /ws/crypto, /ws/nse/chain
frontend/           Next.js dashboard (Vercel)
scripts/            option-chain collectors
docs/               RESEARCH_LEARNINGS.md — read this first
```

## Running

```bash
docker compose up -d                  # api only
docker compose --profile nse up -d    # + the four option-chain collectors
./deploy.sh                           # deploy
```

The sentinel runs separately, on the static-IP VPS:

```bash
uvicorn sentinel.main:app --host 0.0.0.0 --port 8090
```

Order placement is **off by default** (`SENTINEL_LIVE_ORDERS=0`) so a fresh
deploy cannot trade on a config nobody has reviewed.

## Environment

`.env` holds **secrets only**. Production dials (leverage, capital, stops, lens
lifecycle thresholds) are hardcoded in `core/risk_management.py` and
`nse/config.py` so every change to risk lands in a diff.

| Variable | Purpose |
|---|---|
| `ANGEL_API_KEY` / `ANGEL_CLIENT_ID` / `ANGEL_PASSWORD` / `ANGEL_TOTP_TOKEN` | Angel One SmartAPI. Login is fully scriptable — no manual morning step |
| `DELTA_API_KEY` / `DELTA_API_SECRET` | Delta India |
| `MONGODB_URL` / `MONGODB_DB_NAME` | Atlas |
| `DASHBOARD_SECRET` / `DASHBOARD_USER` / `DASHBOARD_PASS` | Dashboard JWT |
| `SENTINEL_SECRET` | ≥32 chars, shared brain↔sentinel. Not the Angel key |
| `SENTINEL_LIVE_ORDERS` | `1` to arm order placement |
| `ENABLE_CRYPTO_RUNNER` / `ENABLE_NSE_RUNNER` | Both `false` |

`api/auth.py` reads `DASHBOARD_SECRET` at **import** time — the app needs `.env`
in its environment before import, or it silently validates tokens against the
placeholder secret and every WebSocket closes with a 1008.

## Dashboard

Live option chain in the classic calls / strike / puts layout — ITM shading,
mirrored OI bars, PCR, max pain, VIX — pushed over a diffed WebSocket at a
**median 191 ms**, against the 3 s poll it replaced. Greeks are computed from
the current mark, never read from storage, and grey out inside 2 DTE where
analytic values run up to 100% wrong.

## Safety

- Nothing trades. Both runners are disabled and no strategy is registered.
- The brain tier cannot place an order — enforced by a test, not a convention.
- Exchange-side GTT OCO brackets survive the process dying; the dead-man's
  switch covers the case they cannot, and latches until cleared by hand.
- Daily-loss kill switch, plus a manual kill on the dashboard.
- `.env` is git-ignored.
