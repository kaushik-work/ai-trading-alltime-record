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
| Greeks lens (25δ risk reversal) | No directional edge; negative in TRAIN and VALIDATE |
| **Volume/OI lens** | **+1.80 / +1.62 bps, significant in both splits.** The first entry to survive a hold-out |
| Liquidity sweeps (BTC/ETH 5m) | No edge — negative in all four cells, gross, before costs |

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
        ┌────────────┬───┴────┬─────────────┬────────────┐
        ▼            ▼        ▼             ▼            ▼
     greeks      volume_oi   vwap        ict/smc      vision       ← lenses
   (rejected)   (survives)  (unmeasured)  (todo)   (weight 0)        vote, never
        └────────────┴────────┴─────────────┴────────────┘          read each other
                         ▼
                    Aggregator            weighted consensus; SHADOW lenses are
                         │                heard and journaled but move no capital
                         ▼
              journal EVERY decision      including the ones voted down —
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
  lenses/           greeks · volume_oi · vwap · vision  (+ base contract)
  aggregator.py     weighted-consensus vote and the decision journal
  brain.py          per-lens attribution, weights, lifecycle FSM
  ws/               angel_stream.py — SmartWebSocketV2 feed
  broker/           angel_broker.py — orders + GTT OCO brackets
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
