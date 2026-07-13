# AI Trading All-Time Record

Production crypto-futures trading bot on **Delta Exchange India**.

## What this is

**Live trading surface:** **ETHUSD perpetual futures only**. BTCUSD is disabled.

The strategy is **Price-action S/R retest** — a pure perp price-action strategy
decoded from a Hindi livestream. It trades at 4h S/R levels in the direction of
the 24h trend, using tight stops and asymmetric targets, filtered by 24h
realized volatility.

**Data surface:** NIFTY / BANKNIFTY / FINNIFTY / SENSEX 5-min option-chain
snapshots into MongoDB. Pure data collection. No NSE trading, no NSE bot,
no NSE strategies.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                           Delta India                               │
│   WebSocket (ETH perp + ETH option marks)    REST (wallet, orders)  │
└──────────────────┬──────────────────────────────┬───────────────────┘
                   │                              │
                   ▼                              ▼
       ┌───────────────────────┐      ┌──────────────────────┐
       │  core/ws/delta_stream │      │  core/brokers/       │
       │  (ETH-only stream)    │      │  delta_crypto.py     │
       └───────────┬───────────┘      └──────┬───────────────┘
                   │                          │
                   └────────────┬─────────────┘
                                ▼
       ┌─────────────────────────────────────────┐
       │  strategies/price_action_sr.py          │
       │  ETH S/R retest · 0.7% SL · 4.9% TP     │
       │  24h vol filter ≤ 34%                   │
       └─────────────┬───────────────────────────┘
                     │
                     ▼
       ┌─────────────────────────────────────────┐
       │  core/execution/crypto_runner.py        │
       │  tick 2s · entry every 1m · 1:7 R:R     │
       │  fixed Rs 50k capital per trade         │
       └─────────────┬───────────────────────────┘
                     │
                     ▼
       ┌─────────────────────────────────────────┐
       │  Dashboard (Next.js, /)                 │
       │  ETH signal radar + chart + KILL btn    │
       └─────────────────────────────────────────┘
```

## Strategy: Price-action S/R retest (ETH-only)

1. **Trend filter**: only trade in the direction of the 24h moving average.
2. **Levels**: enter only near the 4h range high/low; skip mid-range.
3. **Aggression**: require a strong reversal candle (body ≥ 1.3× average, wick ≤ 45%).
4. **Volatility filter**: skip if 24h realized vol > 34%.
5. **Risk**: ETH 0.7% SL / 4.9% TP (1:7 R:R).
6. **Exit regime**: pure SL/TP bracket — full close on stop or target (no trail).

| Parameter | Value |
|---|---|
| Asset | ETHUSD |
| S/R lookback | 4h |
| Trend lookback | 24h |
| Stop loss ETH | -0.7% |
| Target ETH | +4.9% |
| Leverage | 15× |
| Capital per trade | Fixed Rs 50,000 INR |
| Vol filter | 24h realized vol ≤ 34% |
| Max hold | 4h |
| Daily kill | -5% of base equity |

Backtest validation (`delta_exchange/backtest_eth_live_config.py`,
April–July 2026, fixed Rs 50k notional per trade, 15× leverage,
vol filter ≤ 34%, `wick_touch` retest, **5 bps/side fee, 2 bps slippage**):
- **ETHUSD**: 17 trades, **47.1% WR**, **+Rs 21,995 net**, **MaxDD Rs 8,656 (17.3%)**.

> ⚠️ The previous 82.4% / +Rs 39,708 number was from an optimistic backtest
> that ignored fees, ignored exit slippage, and allowed entries on every minute.
> The live bot now evaluates entries every minute (the strategy is 1m-based),
> so the corrected numbers above are the realistic projection.

Production dials are hardcoded in `core/risk_management.py` and
`strategies/price_action_sr.py` (not `.env`) so every change is tracked in git.

## Repo layout

```
├── api/                       FastAPI app
│   ├── server.py              Auth, health, /ws/crypto, lifespan
│   └── routes_crypto.py       /api/crypto/* surfaces (signals, snapshot, kill)
├── core/
│   ├── bot_runner.py          APScheduler host (minimal stub)
│   ├── brokers/
│   │   └── delta_crypto.py    HMAC-signed REST + WS-first reads
│   ├── execution/
│   │   └── crypto_runner.py   Tick loop, entry/exit, kill switch, shadow trades
│   ├── ws/
│   │   └── delta_stream.py    Persistent Delta WebSocket client (ETH-only)
│   ├── risk_management.py     Production dials (LEVERAGE, fixed capital, gates)
│   ├── mongo.py               Mongo connection + collections
│   ├── ipc.py                 NSE market-holiday helper (used by collectors)
│   └── utils.py               Date/timezone helpers
├── strategies/
│   ├── crypto_base.py         CryptoStrategy base class
│   └── price_action_sr.py     price-action S/R production dials
├── scripts/
│   └── collect_option_snapshots.py    NSE 5-min OI snapshot collector
├── data/
│   └── angel_fetcher.py       Angel One SmartAPI helper (used by collectors)
├── delta_exchange/            Backtesting playground (CSVs + scripts)
├── frontend/                  Next.js dashboard (Vercel)
└── docker-compose.yml         api + 4 NSE collectors (gated behind 'nse' profile)
```

## Running

**Trade live (production):**
```bash
docker compose up -d              # crypto api only
```

**With NSE option-chain collectors:**
```bash
docker compose --profile nse up -d
```

**Deploy manually:**
```bash
./deploy.sh
```

## Environment variables

Create a `.env` file in the project root. Required:

| Variable | Purpose |
|---|---|
| `DELTA_API_KEY` / `DELTA_API_SECRET` | Delta India REST/WS HMAC auth |
| `MONGODB_URL` / `MONGODB_DB_NAME` | MongoDB Atlas mirror |
| `DASHBOARD_SECRET` / `DASHBOARD_USER` / `DASHBOARD_PASS` | JWT auth |
| `ENABLE_CRYPTO_RUNNER` | `true` to start the bot |
| `CRYPTO_TRADING_MODE` | `live` or `paper` |

Production dials (leverage, capital, SL/TP, vol filter) are **hardcoded in code**,
not in `.env`.

## Dashboard

Login at `/login`. The dashboard shows:
- ETHUSD live signal radar (spot, 4h range, trend, state, SL/TP, 24h vol)
- Warmup progress bar until 1,440 one-minute candles are collected
- Live ETH chart with S/R zones
- Portfolio: fixed Rs 50k budget badge, day P&L, open positions
- Manual kill switch

## Security

- Live money is on the line in `live` mode.
- `.env` is git-ignored and never committed.
- Manual kill switch closes positions and halts new entries.
- Daily-loss kill at -5% of base equity.
