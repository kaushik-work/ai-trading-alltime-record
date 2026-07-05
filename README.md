# AI Trading All-Time Record

Production crypto-futures trading bot on **Delta Exchange India** + NSE
option-chain **data collectors** for research.

## What this is

**Live trading surface:** crypto perpetual futures only (BTCUSD, ETHUSD).
The strategy is **Price-action S/R retest** — a pure perp price-action strategy
decoded from a Hindi livestream. It trades at 4h S/R levels in the direction of
the 24h trend, using tiny stops and asymmetric targets.

**Data surface:** NIFTY / BANKNIFTY / FINNIFTY / SENSEX 5-min option-chain
snapshots into MongoDB. Pure data collection. No NSE trading, no NSE bot,
no NSE strategies.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                           Delta India                               │
│   WebSocket (perp + option marks)        REST (wallet, orders)      │
└──────────────────┬──────────────────────────────┬───────────────────┘
                   │                              │
                   ▼                              ▼
       ┌───────────────────────┐      ┌──────────────────────┐
       │  core/ws/delta_stream │      │  core/brokers/       │
       │  (993 symbols / 2s)   │      │  delta_crypto.py     │
       └───────────┬───────────┘      └──────┬───────────────┘
                   │                          │
                   └────────────┬─────────────┘
                                ▼
       ┌─────────────────────────────────────────┐
       │  strategies/price_action_sr.py          │
       │  S/R retest · tiny SL · 1:7 R:R         │
       └─────────────┬───────────────────────────┘
                     │
                     ▼
       ┌─────────────────────────────────────────┐
       │  core/execution/crypto_runner.py        │
       │  tick 2s · stop 1.5% · trail 0.25%      │
       │  partial TP @ 1% · max hold 72h         │
       └─────────────┬───────────────────────────┘
                     │
                     ▼
       ┌─────────────────────────────────────────┐
       │  Dashboard (Next.js, /)                 │
       │  Live signal radar + chart + KILL btn   │
       └─────────────────────────────────────────┘
```

## Strategy: Price-action S/R retest

1. **Trend filter**: only trade in the direction of the 24h moving average.
2. **Levels**: enter only near the 4h range high/low; skip mid-range.
3. **Aggression**: require a strong reversal candle (body ≥ 1.3× average, wick ≤ 45%).
4. **Risk**: wider SL, big target (BTC 0.6% / 1:7, ETH 0.7% / 1:7).
5. **Trail**: move stop to breakeven after +1R.

| Parameter | Value |
|---|---|
| S/R lookback | 4h |
| Trend lookback | 24h |
| Stop loss BTC | -0.6% |
| Stop loss ETH | -0.7% |
| Target BTC | +4.2% |
| Target ETH | +4.9% |
| Leverage | 3× |
| Capital per cycle | 50% of pool |
| Max hold | 4h |
| Daily kill | -5% of base equity |

Backtest validation (April–June 2026, ~80 days, $10k per asset,
`wick_touch` retest + 180-min block-after-loss):
- BTCUSD SL 0.6% / 1:7: **+17.28%**, PF 1.79, 124 trades, 57.3% WR, MaxDD 2.52%
- ETHUSD SL 0.7% / 1:7: **+18.10%**, PF 2.01, 83 trades, 56.6% WR, MaxDD 2.33%

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
│   │   └── delta_stream.py    Persistent Delta WebSocket client
│   ├── risk_management.py     Production dials (LEVERAGE, gate, kill thresholds)
│   ├── memory.py              SQLite trade memory
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

**Plus the NSE data collectors:**
```bash
docker compose --profile nse up -d
```

**Local backtest:**
```bash
cd delta_exchange
./.venv/Scripts/python.exe backtest_price_action_sweep.py
```

## Required env (in `.env`, not committed)

```
DELTA_API_KEY=...               # with Read + Trade scopes + IP whitelist
DELTA_API_SECRET=...
MONGODB_URL=...
MONGODB_DB_NAME=...
JWT_SECRET=...                  # dashboard auth
ANGEL_*=...                     # only for NSE collectors
DASHBOARD_ORIGINS=...           # CORS allowlist
```

Risk dials live in `core/risk_management.py`, not `.env` — change via PR review.

## Mongo collections

| Collection | Owner | Purpose |
|---|---|---|
| `crypto_trades` | crypto bot | every entry/exit event |
| `crypto_signal_log` | crypto bot | gated signal observations |
| `option_snapshots` | NSE collectors | 5-min option chain dumps (NIFTY/BANKNIFTY/FINNIFTY/SENSEX) |

NSE-side legacy collections (`shadow_trades`, `daily_journals`, etc) are untouched
but no longer written to.

## License

Private.
