# AI Trading Bot — Shadow

Forward-test platform for **NIFTY** options on Angel One SmartAPI. The bot
**never places real orders** — every decision is logged to a Mongo
`shadow_trades` collection for offline evaluation.

**Backend:** FastAPI on a DigitalOcean droplet (Docker)
**Frontend:** Next.js on Vercel
**Data:** Angel One SmartAPI + persistent option-chain snapshots

---

## Active strategy: Q5 multi-signal shadow

Three independent signals run in parallel, each with its own ledger:

| Signal | Trigger | Discovered via |
|--------|---------|----------------|
| `q5_straddle_level` | ATM straddle > trailing-5d P70 | analyze_option_chain.py (IC +0.132) |
| `q5_straddle_mom3`  | 3-bar change in ATM straddle > P70 | alpha_mining.py (IC +0.120) |
| `q5_pcr_mom3`       | 3-bar change in PCR_OI > P70 | alpha_mining.py (IC +0.115) |

**Locked parameters** (from sweep + multi-strategy replay):

| Param | Value | Notes |
|-------|-------|-------|
| SL distance | ₹10 | premium points |
| RR ratio | 2.25 | WR-max from RR fine-tune (38–40%) |
| Strike | ITM by 50 (CE) | higher delta, lower IV cost |
| Side | CE only | signed corr was bullish |
| Trades/day/strategy | 4 | hard cap |
| Per-strategy daily loss cap | ₹2,000 | 4% of ₹50K |
| Aggregate daily loss cap | ₹3,500 | 7% of ₹50K |
| Same-strike correlation guard | ON | prevents triple-betting on a bar |
| Lot multiplier | 1× default, 2× after 30+ closed trades | Kelly-capped |

**Backtested expectation** (8 days replay):

- Net +₹14,300, WR ~40%, PF 1.52
- Max DD 2.6% of capital
- 5 winning days out of 8, worst day −₹1,300

---

## Architecture

```
collector container       ──→  Mongo option_snapshots (5-min bars, 17 strikes × 2 sides)
        │
api container (FastAPI)
  ├── BotRunner (apscheduler)
  │   ├── _shadow_signal_tick    every 30 s
  │   ├── _option_chain_refresh  every 15 m
  │   ├── _daily_token_refresh   08:30 / 12:00 / 14:00 IST
  │   └── _save_journal          15:25 IST
  ├── REST endpoints
  │   ├── /api/shadow-trades  per-strategy ledger
  │   ├── /api/risk-budget    today's caps + lot multipliers
  │   ├── /api/pnl            daily shadow P&L grouping
  │   ├── /api/journals       saved daily JSONs
  │   ├── /api/mongo/status   mirror health
  │   └── /api/health
  └── WebSocket /ws           5 s snapshot broadcast
frontend container (Next.js)
  └── Dashboard               chart + status chips + per-strategy summary + today's trades
```

---

## Research scripts (scripts/)

| Script | Purpose |
|--------|---------|
| `analyze_option_chain.py`     | Feature → forward-return correlation pass |
| `backtest_straddle_signal.py` | Single-signal backtest with caps |
| `sweep_straddle.py`           | 54-combo parameter sweep + LOO |
| `regime_filter.py`            | Trend-regime classifier (`trend_up` = noise) |
| `meta_label_q5.py`            | López-de-Prado meta-labeling (needs more data) |
| `alpha_mining.py`             | WorldQuant-style alpha discovery |
| `replay_multi_strategy.py`    | Full forward-test simulation |
| `verify_shadow_signal.py`     | Sanity check (passes) |
| `collect_option_snapshots.py` | The 5-min option-chain writer |

---

## Deploy

```bash
# Droplet
git pull
docker compose build --no-cache api frontend
docker compose up -d --force-recreate api frontend
docker compose logs -f api | grep -E "shadow|ShadowBook"
```

You should see, every 30 s during market hours:

```
shadow[q5_straddle_level]: val=312.5 thr=349.4 fire=False
shadow[q5_straddle_mom3]:  val=-4.3  thr=0.65  fire=False
shadow[q5_pcr_mom3]:       val=0.0234 thr=0.0175 fire=True
ShadowBook[q5_pcr_mom3] OPEN ... strike=23750 prem=Rs 152.30 SL=142.30 TP=174.80
```

---

## What was retired

- **ATR Intraday strategy** — at-break-even (PF 1.03, 25.9% WR). Removed
  Nov 2026 along with its scorer, scheduler jobs, day-bias panel, force-trade
  IPC, signal_log, weekly Claude reviews, backtest engine, and dashboard pages.
- **Paper trading / MockBroker** — removed earlier. The bot is live-only.
- **VIX-based logic** — never validated, removed.
- **Real money trading** — currently disabled. Capital is withdrawn from
  Angel One. Bot can fetch quotes but cannot place orders. Re-fund only after
  shadow PF stays > 1.5 across 4+ weeks of forward data.
