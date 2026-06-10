# AI Trading All-Time Record

Production crypto-futures trading bot on **Delta Exchange India** + NSE
option-chain **data collectors** for research.

## What this is

**Live trading surface:** crypto perpetual futures only (BTCUSD, ETHUSD).
The strategy is **Synthetic Forward v5.5** — exploits dislocations between
the options-implied forward price and the perp mark.

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
       │  strategies/synth_forward.py (v5.5)     │
       │  gate 0.6% · persist 1h · ±5% strikes  │
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

## Strategy: Synthetic Forward v5.5

For each near-money strike on a given expiry:

```
synthetic_forward = call_price − put_price + strike
dislocation       = (synthetic_forward − spot) / spot
```

Then the bot:

| Step | Rule |
|---|---|
| **Aggregate** | median dislocation across ≥3 strikes within ±5% of spot |
| **Eligible expiry** | 6h ≤ TTE ≤ 72h |
| **Entry gate** | \|dislocation\| ≥ 0.6% |
| **Persistence** | signal must hold same direction for ≥1h |
| **Direction** | positive dislocation → LONG perp · negative → SHORT |
| **Sizing** | `equity × 50% × (0.5–3× by signal strength)` |
| **Leverage** | 3× (validated equal returns vs 10× with 3× more liquidation buffer) |
| **Stop loss** | -1.5% |
| **Partial TP** | half-close at +1% |
| **Trail** | give back max 0.25% after reaching +0.5% peak |
| **Max hold** | 72h |
| **Daily kill** | -5% of base equity → halt new entries |

Backtest validation (June 2026, 9 days, ₹40k starting):
- BTC + ETH shared pool: **₹40,000 → ₹51,564 (+28.9%)**
- Win rate ~94%, only 2 small losers out of 32 trades

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
│   └── synth_forward.py       v5.5 production dials
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
./.venv/Scripts/python.exe backtest_synth_forward_v5_5_sweep.py
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
