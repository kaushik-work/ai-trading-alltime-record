# AI Trading Bot — All-Time Record

Fully automated intraday trading bot for **NIFTY** options on Angel One SmartAPI,
with Claude AI for trade narrative + post-trade review.

**Backend:** FastAPI on a DigitalOcean droplet (Docker) | **Frontend:** Next.js on Vercel | **Data:** Angel One SmartAPI

---

## Roadmap

| Phase | Budget | Style | Goal | Status |
|-------|--------|-------|------|--------|
| **A** | ₹1.25L | Intraday NIFTY | Compound to ₹1.5L | ✅ Active |
| **B** | ₹1.5L  | Intraday + Swing | Compound to ₹15L | 🔒 Locked |
| **C** | ₹15L+  | Options Selling (Straddle / Iron Condor) | Monthly income | 🔒 Locked |

---

## Strategy

The bot runs a **single live strategy**: **ATR Intraday**.

### ATR Intraday
- **Signal:** SMA50 + SMA20 + EMA9 + RSI + MACD + Bollinger + Volume + ATR-vol filter
  + VWAP + ORB + 15m trend/RSI + PDH/PDL + 12 candlestick patterns
  + PCR + OI walls + herd-gate + S/R zones (RBD/DBR + structure)
- **Timeframe:** 5-minute bars
- **Score range:** −10 to +10. Live entry threshold: ≥ ±8 (config default ±6,
  hard-floor of 8 enforced in `strategies/trend_strategy.py` to skip 6–7 traps).
- **R:R:** 1:3.0 — SL = ½ × min(5m ATR, 50)pts (≥ 5pts floor); TP = SL × 3.0
- **Entry window:** 09:30–15:20 IST. Mandatory square-off at 15:20.
- **Strike selection:** Walk ATM ± 10 strikes in ₹50 steps until premium falls
  in ₹155–₹165 range. Closest match used as fallback.
- **Zone gating:** Pre-market watch zones (PDH/PDL/ORB/weekly) computed at 09:00.
  Trade rejected if signal fights an active zone within 30pts.
- **Zone-reversal early entry:** Independent of score — at most one entry/day.
  When price taps a watch zone and the last 5m candle confirms rejection
  (bearish at resistance / bullish at support), force-entry CE/PE.

C-ICT was active previously and will be **rebuilt from scratch later**.
Musashi / Raijin / SMC / Expiry-Day Gap research are not in the live path.

---

## Data Pipeline

```
Angel One SmartAPI   ←  primary: real NSE bars + option chain + VIX
        ↓ if unavailable
Cycle skipped        ←  never trade on bad data
```

5-minute option chain snapshots are persisted to build a real historical
premium dataset over time (mitigates the BS-vs-live premium mismatch that
hurts naive backtests).

---

## Architecture

```
DigitalOcean droplet (Docker)
└── api  (FastAPI + APScheduler in one process)
    ├── api/server.py           # REST + WebSocket, snapshot every 5s
    ├── core/bot_runner.py      # AsyncIOScheduler — schedules every cycle
    │
    ├── strategies/
    │   ├── trend_strategy.py   # Order execution, SL/TP, zone gating
    │   ├── signal_scorer.py    # ATR Intraday scoring engine (atr_only)
    │   ├── indicators.py       # EMA, RSI, ATR, VWAP, swing structure
    │   ├── patterns.py         # 12 candlestick pattern detectors
    │   └── vix_filter.py       # India VIX regime helper
    │
    ├── data/
    │   ├── angel_fetcher.py    # Angel One singleton — TOTP, bars, option LTP, VIX
    │   ├── market.py           # RealMarketData — daily + intraday indicators
    │   ├── option_chain.py     # OI walls, PCR, max-pain, herd-gate
    │   └── oi_data.py          # OI helpers
    │
    ├── core/
    │   ├── brain.py            # Claude AI trade remarks
    │   ├── broker.py           # MockBroker (paper) + AngelOneBroker (live)
    │   ├── memory.py           # SQLite — trades, signal_log, daily_summaries
    │   ├── records.py          # All-time records tracker
    │   ├── journal.py          # Daily journal JSON + Claude review (haiku)
    │   ├── ipc.py              # Flag-file IPC: pause, force-trade, day-bias, settings
    │   ├── paper_seller.py     # Buyer-vs-seller paper P&L comparison
    │   ├── sr_levels.py        # RBD/DBR institutional zones, structure detection
    │   ├── zone_briefing.py    # Pre-market PDH/PDL/ORB/weekly watch zones (9 AM)
    │   ├── greeks.py           # Black-Scholes delta/gamma/theta/vega
    │   ├── trade_analyst.py    # Post-trade Claude analysis
    │   └── angel_error_log.py  # Append-only error log
    │
    ├── backtesting/
    │   ├── engine.py           # ATR Intraday bar-by-bar replay
    │   ├── metrics.py          # Win rate, Sharpe, drawdown, profit factor
    │   └── charges.py          # NSE brokerage + STT + exchange + GST
    │
    └── db/
        └── trading.db          # SQLite

Vercel (Next.js frontend)
└── frontend/                   # Dashboard, signal radar, P&L, backtest, journal
```

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure `.env`
```env
# Angel One SmartAPI
ANGEL_API_KEY=...
ANGEL_CLIENT_ID=...
ANGEL_PASSWORD=...
ANGEL_TOTP_TOKEN=...        # base32 secret from Angel One 2FA setup

# Claude AI
ANTHROPIC_API_KEY=sk-ant-...

# Mode
TRADING_MODE=paper          # paper = simulation | live = real money
```

All financial parameters (capital, lot size, SL/TP, premium target) live in
`config.py` — not env vars. See `feedback_deploy_habits.md` for the rationale.

### 3. Daily token routine — automatic on droplet

Nothing to do daily. Angel One TOTP login is automatic — `data/angel_fetcher.py`
reads `ANGEL_API_KEY / ANGEL_CLIENT_ID / ANGEL_PASSWORD / ANGEL_TOTP_TOKEN`
from `.env` and generates a fresh session on first authenticated call. The bot
runs 24/7 on the DigitalOcean droplet so this happens automatically every
container start and as needed during the session.

`scripts/get_token.py` remains as a **manual sanity check only**:

```bash
# Run inside the api container if you ever need to verify creds work
docker compose exec api python scripts/get_token.py
```

It does not need to be scheduled on a laptop or as a cron — the droplet
handles its own auth.

---

## Run Locally

```bash
# Start FastAPI backend (Bot runs inside the same process via APScheduler lifespan)
python -m uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload
```

API at `http://localhost:8000`, WebSocket at `ws://localhost:8000/ws`.

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/auth/token`        | POST      | Login (returns JWT) |
| `/api/snapshot`          | GET       | Full dashboard snapshot |
| `/api/bot/debug`         | GET       | Live ATR signal score |
| `/api/bot/pause`         | POST      | Pause trading |
| `/api/bot/resume`        | POST      | Resume trading |
| `/api/bot/bias`          | POST      | Set day bias (BULLISH / NEUTRAL / BEARISH) |
| `/api/trade/force`       | POST      | Queue a manual trade |
| `/api/live/preflight`    | POST      | Validate token, contract, LTP, margin |
| `/api/backtest`          | POST      | Run ATR Intraday backtest |
| `/api/journals`          | GET       | List daily journals |
| `/api/journals/{date}`   | GET       | Get a specific day's journal |
| `/api/signal-log`        | GET       | Every 5-min evaluation (trade or no-trade) |
| `/api/paper-comparison`  | GET       | Buyer-vs-seller paper P&L per signal |
| `/api/chart-data`        | GET       | NIFTY 5m candles + S/R + EMA + POC |
| `/api/event-blocks`      | GET/POST  | Event-blocked dates (Budget, RBI MPC) |
| `/api/market-holidays`   | GET/POST  | NSE market holidays |
| `/api/angel/session`     | POST      | Force fresh Angel One TOTP login |
| `/ws`                    | WebSocket | Live snapshot every 5s |

---

## Backtesting

```json
POST /api/backtest
{
  "strategy": "ATR Intraday",
  "symbol": "NIFTY",
  "period": "60d",
  "interval": "5m",
  "capital": 125000,
  "risk_pct": 2.0,
  "rr_ratio": 3.0,
  "daily_loss_limit_pct": 5.0
}
```

Backtest replays real Angel One historical bars. For realistic results
(bar-level SL + slippage modeling), use `scripts/backtest_live_atr.py`.

---

## Paper vs Live

| | Paper Mode | Live Mode |
|-|-----------|-----------|
| `TRADING_MODE` | `paper` | `live` |
| Real money | No | Yes |
| Orders placed | Simulated (MockBroker) | Real Angel One orders |
| Market data | Angel One | Same |

> Always run paper for at least 1–2 weeks after material strategy changes.
> Verify signal scores in the Signal Radar, then flip `TRADING_MODE=live`.

---

## Deploy

```bash
git pull
docker compose build --no-cache
docker compose up -d --force-recreate
```

Or use `deploy.sh`. The GitHub Actions workflow (`.github/workflows/deploy.yml`)
SSHs into the droplet on push to `main` and runs the same sequence.

---

## Security

- All credentials in `.env` — never committed (`.gitignore`)
- Logs automatically mask API keys, passwords, tokens (`core/security.py`)
- Never push `.env` to git
