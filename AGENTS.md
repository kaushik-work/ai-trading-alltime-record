# Agent Guide — AI Trading All-Time Record

> **State as of 2026-07-04:** crypto-only live trading on Delta Exchange India.
> Legacy NSE/NIFTY trading code has been retired. NSE option-chain collectors
> are still active for research data but do not touch the trading API.

This guide is written for AI coding agents who need to understand, modify, or
extend the project. Read this file, `README.md`, and `core/risk_management.py`
first — together they form the project contract.

---

## 1. Project overview

This is a production crypto-futures trading bot that trades **ETHUSD**
perpetual contracts on **Delta Exchange India** (BTCUSD is disabled in the
current config because the vol filter degraded BTC backtest performance). It uses a pure
price-action strategy called **Price-action S/R retest**, decoded from a Hindi
livestream:

1. Daily trend filter (24h moving average) — only trade in the higher-timeframe
   direction.
2. Enter only at 4-hour S/R range edges; skip mid-range setups.
3. Require the candle wick to actually touch or pierce the S/R level
   (`wick_touch` retest), plus a strong reversal body (close in the top/bottom
   30% of the range).
4. Wider SL, big target (asset-specific: BTC 0.6% / 1:7, ETH 0.7% / 1:7).
5. Trail stop to breakeven at +1R.

A separate Next.js dashboard visualizes live signals, positions, charts, and a
manual kill switch. NSE option-chain collectors run only as data harvesters for
research/backtest data; they do not trade.

Backtest (Apr–Jun 2026, BTC + ETH, 1m data, wick_touch + block-after-loss 180 min):
- **BTCUSD** SL 0.6% / 1:7: **+17.28%**, PF 1.79, 124 trades, 57.3% WR, MaxDD 2.52%.
- **ETHUSD** SL 0.7% / 1:7: **+18.10%**, PF 2.01, 83 trades, 56.6% WR, MaxDD 2.33%.

---

## 2. Technology stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.12 (Docker base `python:3.12-slim`) |
| API / bot | FastAPI, Uvicorn, APScheduler, `websocket-client` |
| Frontend | Next.js 14.2.3 (App Router), React 18, TypeScript 5, Tailwind CSS 3.4 |
| Charts | `lightweight-charts` (dashboard); `recharts` is installed but unused |
| Data stores | MongoDB Atlas (mirror), local CSV (`db/oi_snapshots/`), local logs |
| Exchange | Delta Exchange India (HMAC REST + persistent WebSocket) |
| NSE data | Angel One SmartAPI (`data/angel_fetcher.py`) |
| Infra | Docker Compose on a DigitalOcean droplet; nginx reverse proxy with Let's Encrypt; Next.js dashboard hosted on Vercel |

Key Python dependencies are in `requirements.txt`:
`fastapi`, `uvicorn[standard]`, `apscheduler`, `websocket-client`, `pymongo`,
`pandas`, `numpy`, `scipy`, `requests`, `python-jose[cryptography]`,
`python-multipart`, `python-dotenv`, `cryptography`, `smartapi-python`,
`logzero`, `pyotp`, `anthropic`, `streamlit`, `mplfinance`.

---

## 3. Repository layout

```
.
├── api/                          FastAPI application
│   ├── server.py                 App lifespan, CORS, JWT login, /ws/crypto
│   ├── routes_crypto.py          /api/crypto/* REST routes
│   ├── auth.py                   JWT helpers (DASHBOARD_SECRET / DASHBOARD_PASS)
│   └── broadcaster.py            Generic WS connection manager (mostly unused)
├── core/                         Trading engine
│   ├── bot_runner.py             APScheduler host
│   ├── brokers/delta_crypto.py   Delta REST broker + WS-first read caches
│   ├── execution/crypto_runner.py Tick loop, entry/exit, kill switch, shadow trades
│   ├── ws/delta_stream.py        Persistent Delta WebSocket client
│   ├── risk_management.py        Production risk dials (single source of truth)
│   ├── mongo.py                  MongoDB connection + collection names
│   ├── ipc.py                    NSE market-holiday helper (collectors only)
│   ├── utils.py                  now_ist() / today_ist()
│   └── sr_levels.py              Supply/demand zone math for chart overlays
├── strategies/                   Signal generation
│   ├── crypto_base.py            CryptoStrategy abstract base + decision dataclass
│   └── price_action_sr.py        Price-action S/R retest strategy (live)
├── data/                         NSE data helpers
│   └── angel_fetcher.py          Angel One SmartAPI client
├── scripts/                      Stand-alone utilities
│   └── collect_option_snapshots.py  NSE 5-min option-chain collector
├── delta_exchange/               Backtest sandbox (own .venv + CSV data)
│   ├── backtest_engine.py
│   ├── backtest_price_action_sweep.py
│   └── ...                       Other backtest / diagnostic scripts
├── frontend/                     Next.js dashboard
│   ├── app/page.tsx              Main dashboard
│   ├── app/CryptoChart.tsx       Lightweight-charts wrapper
│   ├── app/login/page.tsx        JWT login
│   ├── app/components/Header.tsx Logo + logout
│   └── package.json
├── config.py                     NSE collector credentials + market holidays
├── config/assets/                Stale/legacy YAML files (NOT loaded by crypto bot)
├── docker-compose.yml            api + optional NSE collector services
├── Dockerfile                    Python 3.12 slim image
├── nginx/nginx.conf              Production reverse proxy
├── deploy.sh                     Manual droplet deploy script
├── .github/workflows/deploy.yml  GitHub Actions → droplet deploy
└── docs/                         STRATEGY.md / PDF builder
```

Note: `core/memory.py` is referenced in older docs but does **not** exist in the
current codebase. `config/assets/*.yml` is also stale and not loaded by the live
bot.

---

## 4. Build, run, and test commands

### Production crypto bot

```bash
# Start only the API / trading bot
docker compose up -d

# View logs
docker compose logs -f api
```

### With NSE data collectors

```bash
docker compose --profile nse up -d
```

Collectors run only between **03:30–10:05 UTC** (09:00–15:35 IST) and sleep
otherwise. They restart daily via `unless-stopped`.

### Manual droplet deploy

```bash
./deploy.sh
```

This pulls, prunes old images/containers, rebuilds, and recreates containers.

### Local backend (not recommended for live trading)

```bash
python -m venv .venv
pip install -r requirements.txt
uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload
```

`core/bot_runner.py` refuses to start APScheduler on non-Linux unless you set
`ALLOW_LOCAL_SCHEDULER=1`.

### Frontend

```bash
cd frontend
npm install
npm run dev      # localhost:3000
npm run build    # production build
npm run start
```

`NEXT_PUBLIC_API_URL` defaults to `http://localhost:8000`. On Vercel it points
at `https://thegaintcompany.com`.

### Backtests

```bash
cd delta_exchange
# Use the sandbox venv
./.venv/Scripts/python.exe backtest_price_action_sweep.py
# or
python backtest_engine.py 2026-06-10
```

### Testing

There is **no automated test suite** in this repository. Validation is done
through backtests in `delta_exchange/` and live shadow/paper trading. If you add
logic, add backtest scripts or dry-run checks rather than unit tests unless the
team agrees on a testing framework.

---

## 5. Runtime architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Vercel (Next.js dashboard)                                          │
│  NEXT_PUBLIC_API_URL → https://thegaintcompany.com/api/*            │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │
                          nginx (443 ssl, /ws upgrade)
                                   │
┌──────────────────────────────────▼───────────────────────────────────┐
│  Docker host (DigitalOcean droplet)                                  │
│  ┌─────────────┐  ┌───────────────────────────────────────────────┐  │
│  │ nginx       │  │ api container (FastAPI + crypto bot)          │  │
│  │ :80 → :443  │  │  • core/ws/delta_stream ← 224 symbols WS      │  │
│  │ /api → :8000│  │  • strategies/price_action_sr.py (live signal) │  │
│  │ /ws  → :8000│  │  • core/execution/crypto_runner.py (orders)   │  │
│  └─────────────┘  │  • core/risk_management.py (dials)            │  │
│                   └───────────────────────────────────────────────┘  │
│  Optional NSE collectors (profile `nse`) ×4                            │
└──────────────────────────────────────────────────────────────────────┘
```

### Startup flow (`api/server.py` lifespan)

1. `BotRunner.start()` initializes APScheduler (Linux/cloud only).
2. `start_stream()` launches `DeltaStream` in a background thread.
3. If `ENABLE_CRYPTO_RUNNER` is true, four scheduled jobs are added.

### Scheduler jobs (`core/execution/crypto_runner.py`)

| Job | Cadence | Purpose |
|---|---|---|
| `tick_position_management` | every 2s | SL, TP, trail, max-hold, shadow positions |
| `tick_signal_sample` | every 5m | Record raw pred into `_sig_history` for persistence gate |
| `tick_entry_decisions` | every 1 min UTC (`*:05`) | New entry decisions only |
| `_wallet_heartbeat` | every 5m | Log Delta wallet breakdown |

`max_instances=1, coalesce=True` so ticks never overlap.

### WebSocket data flow

- `DeltaStream` subscribes to `MARKET:ETHUSD` for the ETH perp + ~15
  near-money ETH option strikes per expiry. BTC is not subscribed.
- Broker read methods prefer stream marks, falling back to REST caches.
- `/ws/crypto` pushes `_build_crypto_snapshot()` every second to authenticated
  dashboard clients.
- REST `/api/crypto/snapshot` returns the same snapshot builder.

---

## 6. Strategy details

### Price-action S/R retest (`strategies/price_action_sr.py`)

This is the live crypto strategy. It needs only perp OHLC data, so the bot no
longer subscribes to option-chain marks for signal generation.

Decoded rules from the Hindi livestream:

1. Daily trend filter (24h moving average) — only buy dips in an uptrend, sell
   rallies in a downtrend.
2. Trade at the 4-hour S/R range edges only; skip mid-range setups.
3. Wait for a strong reversal candle (body ≥ 1.3× average, wick ≤ 45%).
4. Wider SL (0.6% BTC / 0.7% ETH), big target (1:7 R:R).
5. Pure SL/TP bracket exit (no trail) under the current `pure_sltp` regime.

The strategy builds its own 1-minute candles from live perp mark updates. The
2-second position-management tick feeds marks into the candle buffer; the
1-minute entry tick evaluates the signal on each completed 1m candle.

### Production dials

| Dial | Value | Location |
|---|---|---|
| S/R lookback | 4h (240 candles) | `strategies/price_action_sr.py` |
| Trend lookback | 24h (1440 candles) | `strategies/price_action_sr.py` |
| Range width max | 1.5% | `strategies/price_action_sr.py` |
| Level zone | ±0.4% of range high/low | `strategies/price_action_sr.py` |
| Retest mode | `wick_touch` | `strategies/price_action_sr.py` |
| Wick touch tolerance | 7 bps vs S/R level | `strategies/price_action_sr.py` |
| Body position threshold | 0.70 (close in top/bottom 30%) | `strategies/price_action_sr.py` |
| Body multiplier | 1.3× | `strategies/price_action_sr.py` |
| Wick ratio max | 45% | `strategies/price_action_sr.py` |
| Stop loss | 0.6% BTC / 0.7% ETH | `strategies/price_action_sr.py` |
| Target | 4.2% BTC / 4.9% ETH | `strategies/price_action_sr.py` |
| Cooldown | 60 min | `strategies/price_action_sr.py` |
| Block after loss | 180 min | `strategies/price_action_sr.py` |
| 24h realized vol filter | ≤ 34% ETH / off BTC | `strategies/price_action_sr.py` |
| Optional WR filters | volume, RSI, trend slope, range min, hours, HTF align, engulfing, pin bar | `strategies/price_action_sr.py` |
| Leverage | 30× | `core/risk_management.py` |
| Capital mode | Fixed ₹50k INR per trade, compounding disabled | `core/risk_management.py` |
| Max hold | 4h | `strategies/price_action_sr.py` |
| Cooldown | 1h between signals | `strategies/price_action_sr.py` |
| Daily kill | -5% of base equity | `core/risk_management.py` |
| Max live contracts | 50 BTC / 300 ETH | `core/risk_management.py` |

### Exit regime

`EXIT_REGIME` should remain `pure_sltp` for this strategy. The price-action
strategy sets `partial_tp_pct` to the asset-specific target and `stop_loss_pct`
to the asset-specific SL, so the position manager executes a clean bracket
order.

Backtest results on Delta 1m data:

**April–June 2026 (~80 days, last 3 months)**

| Asset | Config | Trades | WR | P&L | PF | MaxDD | MaxCL |
|---|---|---:|---:|---:|---:|---:|---:|
| BTCUSD | SL 0.6% / 1:7 | 124 | 57.3% | +17.28% | 1.79 | 2.52% | 5 |
| ETHUSD | SL 0.7% / 1:7 | 83 | 56.6% | +18.10% | 2.01 | 2.33% | 3 |

Walk-forward (40% / 60% split) is healthy with the `wick_touch` retest filter
plus 180-min block-after-loss: BTC PF 1.45 → 1.71, ETH PF 2.18 → 1.78. Both
assets remain profitable in both halves and MaxCL stays at 3–5.

Additional WR-boost filters (RSI, volume, trend slope, range min, time-of-day,
15m HTF align, engulfing, pin bar) are exposed as dials in the backtest harness.
Individually they mostly reduce trade count; the safest global improvement is
`BLOCK_AFTER_LOSS_MINUTES = 180`.

### Leverage and liquidation risk

A liquidation-aware sweep on Apr–Jun 2026 1m data (`delta_exchange/backtest_leverage_liquidation.py`) models margin call if any 1m wick touches the isolated liquidation price during an open trade. At 50% pool per cycle, effective exposure is `LEVERAGE × 0.50`:

| Leverage | Effective exposure | BTC Ret/mo | ETH Ret/mo | In-sample liquidations |
|---|---:|---:|---:|---:|
| 10× | 5× | ~41% | ~46% | 0 |
| 20× | 10× | ~111% | ~124% | 0 |
| 30× | 15× | ~215% | ~238% | 0 |
| 40× | 20× | ~355% | ~382% | 0 |
| 50× | 25× | ~518% | ~539% | 0 |
| 100× | 50× | ~576% | ~484% | 1 |

Live default is **30×** as a safer-aggressive compromise (~200%/mo BTC, ~238%/mo ETH in-sample). A ~3.3% adverse wick against the position wipes the allocated margin. The daily −5% kill switch and 50% per-cycle cap are the only live guardrails. Consider 10×–20× if you are not comfortable with single-wick liquidation risk.

---

## 7. Configuration

### Hardcoded production dials (preferred)

- `core/risk_management.py` — risk, capital, leverage, exit regime, kill
  thresholds.
- `strategies/price_action_sr.py` — S/R lookback, trend filter, SL/TP, candle
  aggression filters.

Changes to these files should go through PR review. Do **not** put production
dials in `.env`.

### Environment variables (secrets + optional overrides)

Create a `.env` file in the project root. It is git-ignored and bind-mounted
into containers.

| Variable | Purpose |
|---|---|
| `DELTA_API_KEY` / `DELTA_API_SECRET` | Delta India REST/WS HMAC auth (Read + Trade scopes, IP-whitelisted) |
| `DELTA_BASE_URL` / `DELTA_WS_URL` | Delta REST/WS endpoints (defaults usually fine) |
| `MONGODB_URL` | MongoDB Atlas connection string |
| `MONGODB_DB_NAME` | Database name |
| `DASHBOARD_SECRET` | JWT signing secret (`api/auth.py`) |
| `DASHBOARD_USER` | Dashboard login username |
| `DASHBOARD_PASS` | Dashboard login password |
| `DASHBOARD_ORIGINS` | Extra CORS allowlist, comma-separated |
| `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PASSWORD`, `ANGEL_TOTP_TOKEN` | Angel One SmartAPI (NSE collectors only) |
| `ANGEL_JWT_TOKEN`, `ANGEL_REFRESH_TOKEN`, `ANGEL_FEED_TOKEN` | Runtime Angel tokens (written by collector) |
| `ENABLE_CRYPTO_RUNNER` | `true`/`false` to start the bot |
| `CRYPTO_TRADING_MODE` | `live` (default) or `paper` |
| `CRYPTO_TICK_SECONDS` | Tick interval, default 2 |
| `CRYPTO_EQUITY_USD` | Paper-mode equity floor, default $1,000 |
| `CRYPTO_CAPITAL_USE_PCT` | Per-cycle capital fraction, default 0.50 |
| `CRYPTO_BTC_CAPITAL_PCT` / `CRYPTO_ETH_CAPITAL_PCT` | Per-asset split, default 0.50 each |
| `CRYPTO_DAILY_LOSS_KILL_PCT` | Default 0.05 |
| `CRYPTO_MAX_LIVE_CONTRACTS` | Default 50 |
| `CRYPTO_EXIT_REGIME` | `pure_sltp` (recommended for price-action) or `trail_partial` |
| `USD_INR_RATE` | INR→USD conversion for wallet valuation, default 86 |

`api/auth.py` fails closed with HTTP 500 if `DASHBOARD_SECRET` or
`DASHBOARD_PASS` still use the placeholder defaults.

### Stale/legacy config

`config/assets/btc_usd.yml` and `eth_usd.yml` exist but are **not loaded** by
the running code. Do not update them expecting the bot to pick up changes.

---

## 8. Data stores

| Store | Purpose | Critical? |
|---|---|---|
| **MongoDB Atlas** | Mirror: `crypto_trades`, `crypto_signal_log`, `crypto_signal_history`, `option_snapshots` | No — failures are swallowed |
| **Local CSV** | `db/oi_snapshots/YYYY-MM-DD_SYMBOL.csv` from NSE collectors | Yes for collector research |
| **Local logs** | `logs/YYYY-MM-DD/*.log` | Diagnostic |
| **`db/flags/`** | File-based IPC for holidays/event blocks/settings | Yes for NSE collector scheduling |

### Mongo collections

| Collection | Owner | Purpose |
|---|---|---|
| `crypto_trades` | crypto bot | entry/exit events |
| `crypto_signal_log` | crypto bot | gated signal observations |
| `crypto_signal_history` | crypto bot | every raw pred sample (persistence gate recovery) |
| `option_snapshots` | NSE collectors | 5-min option chain dumps |

Old NSE collections (`shadow_trades`, `daily_journals`, `trades`, `records`)
still exist but are **not written to**. Always prefix new crypto collections
with `crypto_`.

---

## 9. Code style and conventions

- **Deterministic signals only.** No LLM/RL/ML in signal generation.
- **Risk dials are code, not config.** They live in `core/risk_management.py`
  and `strategies/price_action_sr.py`.
- **Crypto collections use the `crypto_` prefix.** Do not mix crypto data into
  legacy NSE collections.
- **Stream-first reads.** Broker methods prefer WebSocket marks before REST
  fallback.
- **Linux-only scheduler.** `core/bot_runner.py` refuses to start APScheduler
  on non-Linux unless `ALLOW_LOCAL_SCHEDULER=1`.
- **Paper vs live mode.** `CRYPTO_TRADING_MODE=paper` journals orders but does
  not hit Delta.
- **INR is tradeable capital.** Delta auto-converts INR→USD at trade time, so
  wallet pool = USD stablecoins + INR / `USD_INR_RATE`.
- **Position management is split from entry decisions.** This matches backtest
  semantics and reduces noise from real-time WS mark jitter.
- **1-minute entry grid.** Entries are evaluated at `*:05 UTC` on each completed
  1m candle, matching the 1m candle basis of the price-action strategy. The old
  15-minute grid was shown to miss ~88% of setups in the corrected backtest.
- **No trailing commas or wildcard imports** by convention; follow the existing
  file style.

---

## 10. Security considerations

- **Live money is on the line.** The bot places real orders in `live` mode.
  Verify changes in `paper` mode or `delta_exchange/` backtests first.
- **Placeholder auth is rejected.** `api/auth.py` raises HTTP 500 if the
  default `DASHBOARD_SECRET` or `DASHBOARD_PASS` is still in place.
- **Delta API key is IP-whitelisted.** Droplet resize or IP change will break
  trading until the whitelist is updated on Delta.
- **`.env` is git-ignored and never committed.** It contains API secrets and
  tokens.
- **No NSE trading endpoints.** Do not reintroduce NSE trading in `api/`.
- **Kill switches:**
  - Daily-loss kill at -5% of base equity halts new entries.
  - Manual kill via dashboard closes positions and sets `_KILLED`.
- **Bounded order size.** `MAX_LIVE_CONTRACTS=50` caps any single order.
- **JWT tokens expire in 24h.** Dashboard clients must re-login daily.
- **CORS is configured** in `api/server.py` for known origins including
  `localhost:3000` and `*.vercel.app`; extra origins come from
  `DASHBOARD_ORIGINS`.

---

## 11. Deployment

### GitHub Actions (`.github/workflows/deploy.yml`)

- Trigger: push to `main`.
- Runner: `ubuntu-latest`.
- Steps: SSH as `root` into the droplet, pull, prune Docker system to free disk,
  `docker compose down`, `docker compose build`, `docker compose up -d
  --force-recreate`.

### `deploy.sh`

- Pulls, force-removes lingering containers, builds `--no-cache`, recreates,
  and tails logs.

### nginx (`nginx/nginx.conf`)

- Redirects HTTP → HTTPS on `thegaintcompany.com`.
- SSL certs from `/etc/letsencrypt/live/thegaintcompany.com/`.
- Proxies `/` and `/api` to `api:8000`.
- Upgrades `/ws` to WebSocket with a 1-hour read timeout.

### Frontend hosting

The Next.js dashboard is deployed to **Vercel**. API calls go directly to
`NEXT_PUBLIC_API_URL`. The production domain is `https://thegaintcompany.com`.

---

## 12. Known risks and open gaps

| Risk | Mitigation today | Open gap |
|---|---|---|
| Delta IP whitelist drift after droplet resize | Manual fix on Delta API key | — |
| Network blip during exit order | Retries on next 2s tick | No bounded retry counter or alert |
| Fill price drift from displayed mark | Recorded as mark (small bias) | Real fill not fetched post-order |
| Backtest ≠ live PnL | Backtest assumes 5bps slippage | Live slippage not measured |
| Position state drift | Manual KILL button | No 30s reconciliation engine |
| Wide-spread strike contaminates signal | None | Liquidity filter deferred per user policy |

---

## 13. How a new agent should orient

1. Read this file + `README.md` + `core/risk_management.py`.
2. Crypto bot logic: `core/execution/crypto_runner.py` +
   `strategies/price_action_sr.py`.
3. Delta WebSocket plumbing: `core/ws/delta_stream.py`.
4. Broker/orders: `core/brokers/delta_crypto.py`.
5. Backtests: `delta_exchange/backtest_price_action_sweep.py`.
6. Dashboard: `frontend/app/page.tsx` and `frontend/app/CryptoChart.tsx`.
7. NSE collectors: `scripts/collect_option_snapshots.py`; enable with
   `docker compose --profile nse up -d`.

---

## 14. Don't do

- Don't write to old NSE Mongo collections from crypto code.
- Don't put production dials in `.env`. They live in `core/risk_management.py`
  and `strategies/price_action_sr.py`.
- Don't add LLM/RL/ML to signal generation. The strategy is deterministic by
  design.
- Don't reintroduce NSE trading endpoints in `api/server.py` or elsewhere.
- Don't rely on `config/assets/*.yml` for live crypto parameters; the bot does
  not load them.
