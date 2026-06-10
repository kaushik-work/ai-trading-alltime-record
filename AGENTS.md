# Agent Guide — AI Trading All-Time Record

> **State as of 2026-06-10:** crypto-only live trading. Legacy NSE/NIFTY
> trading code has been retired. NSE option-chain collectors are still
> active for research data but they don't touch the trading API.

## Architecture

Production crypto-futures bot on **Delta Exchange India** + NSE option-chain
**data collectors** running on a single droplet via Docker Compose.

**Stack:** Python 3.12, FastAPI, Next.js (Vercel), MongoDB Atlas, APScheduler,
Delta India WebSocket + REST.

## Live strategy: Synthetic Forward v5.5

```
synthetic_forward = call_price − put_price + strike
dislocation       = (synthetic_forward − spot) / spot
```

Take the median dislocation across ≥3 near-money strikes (±5% of spot) on
expiries 6–72h out. If `|median| ≥ 0.6%` for 1 continuous hour, fire a
perp trade in that direction.

| Dial | Value | File |
|---|---|---|
| Entry gate | 0.6% | `strategies/synth_forward.py:33` |
| Persistence | 1h | `synth_forward.py:34` |
| Min strikes | 3 | `synth_forward.py:35` |
| TTE window | 6h–72h | `synth_forward.py:36-37` |
| Moneyness | ±5% | `synth_forward.py:38` |
| Size mult | 0.5×–3× by signal strength | `synth_forward.py:39-41` |
| Stop loss | 1.5% | `crypto_base.py` `CryptoSignalDecision` defaults |
| Partial TP | half at +1% | same |
| Trail | give back ≤0.25% after +0.5% peak | same |
| Max hold | 72h | `risk_management.py:77` |
| Leverage | 3× | `risk_management.py:66` |
| Capital per cycle | 50% of pool × size_mult | `risk_management.py:60-62` |
| Daily kill | -5% of base equity | `risk_management.py:69` |

Validated June 2026 (9 days, ₹40k start, shared pool): **+28.9% net**, WR ~94%.

## Trading surface

| Surface | Status |
|---|---|
| `BTCUSD` perpetual on Delta India | LIVE |
| `ETHUSD` perpetual on Delta India | LIVE |
| `XAUTUSD` | architecture supports, not registered |
| NSE NIFTY / BANKNIFTY / SENSEX trading | RETIRED |

## File map (post-cleanup)

```
api/
  server.py                Auth, health, /ws/crypto, lifespan startup
  routes_crypto.py         /api/crypto/* (signals, snapshot, kill, candles)
  auth.py                  JWT helpers
core/
  bot_runner.py            APScheduler host (minimal stub)
  brokers/delta_crypto.py  HMAC REST + stream-first reads + wallet
  execution/crypto_runner.py  Tick loop, position mgmt, shadow trades, kill
  ws/delta_stream.py       Persistent Delta WS client
  risk_management.py       Production dials (single source of truth)
  memory.py                SQLite trade memory
  mongo.py                 Mongo connection + collections
  ipc.py                   NSE market-holiday helper (collectors use it)
  utils.py                 now_ist() etc
strategies/
  crypto_base.py           CryptoStrategy abstract class + sig_history
  synth_forward.py         v5.5 production dials, _compute_signal
scripts/
  collect_option_snapshots.py   NSE 5-min OI collector (NIFTY/BANKNIFTY/etc)
data/
  angel_fetcher.py         Angel One SmartAPI helper (collectors only)
delta_exchange/            Backtest sandbox — historical CSVs + scripts
frontend/app/
  page.tsx                 Crypto dashboard (lives at /)
  CryptoChart.tsx          Lightweight-charts wrapper
  components/Header.tsx    Logo + Logout only
  login/page.tsx           Sign-in → / on success
```

## Mongo collections

| Collection | Owner | Purpose |
|---|---|---|
| `crypto_trades` | crypto bot | entry/exit events |
| `crypto_signal_log` | crypto bot | gated signals (renamed from `signal_log`) |
| `option_snapshots` | NSE collectors | 5-min OI dumps |

Old NSE collections (`shadow_trades`, `daily_journals`, `trades`, `records`)
still exist on the cluster but are no longer written to. Don't add new
crypto data there — always prefix `crypto_`.

## Operational notes

- **Tick interval:** 2s. Driven by APScheduler. `max_instances=1, coalesce=True`
  so ticks never overlap.
- **WS stream:** 224 symbols subscribed (BTC + ETH near-money options + 2 perps).
  Filtered to 7 strikes below + 7 above + ATM per expiry to keep load on the
  1vCPU droplet manageable.
- **Wallet:** Delta auto-converts INR → USD at trade time. The broker reads
  `available_balance` on USD-stablecoin assets + INR balance, sums to a single
  USD-equivalent pool. `CRYPTO_BTC_CAPITAL_PCT` / `CRYPTO_ETH_CAPITAL_PCT`
  determine per-cycle deployment.
- **Wallet heartbeat:** Every 5 min the bot logs the full breakdown to docker
  logs. Useful for SSH diagnostics.
- **Kill switches:** `_KILLED` flag. Set by daily-loss kill (-5%) or the
  manual KILL button on the dashboard. No auto consecutive-loss kill or
  drawdown kill (user policy).

## Risks worth knowing

| Risk | Mitigation today | Open gap |
|---|---|---|
| Delta IP whitelist drift after droplet resize | Manual fix on Delta API key | — |
| Network blip during exit order | Retries on next 2s tick | No bounded retry counter or alert |
| Fill price drift from displayed mark | Recorded as mark (small bias) | Real fill not fetched post-order |
| Backtest ≠ live PnL | Backtest assumes 5bps slippage | Live slippage not measured |
| Position state drift | Manual KILL button | No 30s reconciliation engine |
| Wide-spread strike contaminates signal | None | Liquidity filter deferred per user |

## How a new agent should orient

1. Read this file + `README.md` + `core/risk_management.py` — that's the contract.
2. Crypto bot lives in `core/execution/crypto_runner.py` + `strategies/synth_forward.py`.
3. Delta WS plumbing in `core/ws/delta_stream.py`.
4. Backtests in `delta_exchange/`. Use `backtest_synth_forward_v5_5_sweep.py` for sweeps.
5. Dashboard in `frontend/app/page.tsx`. Single-page.
6. NSE collectors in `scripts/collect_option_snapshots.py`. Gated behind
   `docker compose --profile nse up -d`.

## Don't do

- Don't write to old NSE Mongo collections from crypto code.
- Don't put production dials in `.env`. They live in `core/risk_management.py`.
- Don't add LLM/RL/ML to signal generation. Strategy is deterministic by design.
- Don't reintroduce NSE trading endpoints in `api/server.py`.
