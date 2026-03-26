# AI Trading Bot — All-Time Record

Fully automated intraday trading bot for **NIFTY** options using Claude AI + jugaad-trader (Zerodha).

**Backend:** FastAPI on Railway | **Frontend:** Vercel | **Data:** Zerodha → NSE India API

---

## Roadmap

| Phase | Budget | Style | Goal | Status |
|-------|--------|-------|------|--------|
| **A** | ₹20,000 | Intraday NIFTY | Compound to ₹1.5L | ✅ Active |
| **B** | ₹1.5L | Intraday + Swing | Compound to ₹15L | 🔒 Locked |
| **C** | ₹15L+ | Options Selling (Straddle / Iron Condor) | Monthly income | 🔒 Locked |

---

## Strategies

Three independent strategies run simultaneously. All trade NIFTY CE/PE options.

### 1. Musashi — 15-min Trend Rider
- **Signal:** EMA8/21 stack + VWAP bias + pullback to EMA21 + Heikin-Ashi + RSI + volume
- **Timeframe:** 15-minute bars
- **Entry window:** 9:45–11:30 AM + 1:30–2:30 PM IST
- **R:R:** 1:2.5 (SL = 1.25× ATR, TP = 3.125× ATR)
- **Max trades:** 2 per day
- **Threshold:** 7.5 / 10

### 2. Raijin — 5-min VWAP Mean-Reversion Scalp
- **Signal:** Price at VWAP ±2σ band + Heikin-Ashi flip + RSI extreme + volume spike
- **Timeframe:** 5-minute bars
- **Entry window:** 9:45–10:45 AM + 2:15–2:45 PM IST
- **R:R:** 1:2.0 (SL = 0.6× ATR, TP = 1.2× ATR)
- **Max trades:** 3 per day
- **Threshold:** 8.5 / 10

### 3. ATR Intraday — AishDoc Multi-Signal
- **Signal:** SMA50 trend + VWAP + ORB breakout + RSI + MACD + PDH/PDL + candlestick patterns
- **Timeframe:** 15-minute bars, score −10 to +10
- **Entry window:** 9:45 AM – 3:10 PM IST
- **Threshold:** ±5 / 10, Claude AI confirms before order
- **Square-off:** 3:10 PM hard close

---

## Data Pipeline

```
Zerodha (jugaad-trader)   ←  primary: real NSE bars, real volume
        ↓ if unavailable
NSE India public API      ←  fallback: official OHLCV, no login needed
        ↓ if unavailable
Cycle skipped             ←  never trade on bad data
```

No yfinance anywhere. All three strategies and the backtest engine use the same pipeline.

---

## Architecture

```
Railway (FastAPI)
├── api/server.py          # REST + WebSocket endpoints, bot lifecycle
├── core/bot_runner.py     # APScheduler — runs all 3 strategies every 5/15 min
│
├── strategies/
│   ├── nifty_intraday.py  # Musashi scorer (0–10)
│   ├── nifty_scalp.py     # Raijin scorer (0–10)
│   ├── trend_strategy.py  # ATR Intraday (AishDoc, −10 to +10)
│   ├── signal_scorer.py   # ATR Intraday scoring engine
│   ├── indicators.py      # Custom TA: EMA, RSI, ATR, VWAP, HA, swing structure
│   └── patterns.py        # 12 candlestick pattern detectors
│
├── data/
│   ├── zerodha_fetcher.py # jugaad-trader singleton — intraday + daily + historical DFs
│   ├── nse_fetcher.py     # NSE India API singleton — OHLCV fallback
│   └── market.py          # RealMarketData — indicators for ATR Intraday
│
├── core/
│   ├── brain.py           # Claude AI — trade remarks (entry + exit)
│   ├── broker.py          # MockBroker (paper) + JugaadBroker (live)
│   ├── memory.py          # SQLite trade history
│   ├── records.py         # All-time records tracker
│   ├── journal.py         # Daily journal — saved at 3:20 PM
│   └── security.py        # Credential masking in logs
│
├── backtesting/
│   ├── engine.py          # Bar-by-bar replay — Musashi, Raijin, ATR Intraday
│   ├── metrics.py         # Win rate, Sharpe, max drawdown, profit factor
│   └── charges.py         # Real brokerage + STT + exchange fees
│
└── db/
    └── trading.db         # SQLite — trades, records, journals

Vercel (Next.js frontend)
└── frontend/              # Dashboard — live P&L, signal scores, backtest, journals
```

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure `.env`
```env
# Zerodha
ZERODHA_USER_ID=AB1234
ZERODHA_PASSWORD=your_password
ZERODHA_TOTP_SECRET=your_totp_base32_secret   # 32-char base32 key from Kite 2FA settings

# Claude AI
ANTHROPIC_API_KEY=sk-ant-...

# Mode
TRADING_MODE=paper        # paper = simulation | live = real money (ATR Intraday only)

# Risk (Phase A defaults)
STARTING_BUDGET=20000
MAX_TRADE_AMOUNT=10000
MAX_DAILY_LOSS=2000
RISK_PER_TRADE_PCT=5.0
MIN_SIGNAL_SCORE=5
```

### 3. Get TOTP secret
1. Login to [kite.zerodha.com](https://kite.zerodha.com) → My Profile → Security
2. Enable 2FA → click **"Can't scan? Get the key instead"**
3. Copy the 32-character base32 key → paste as `ZERODHA_TOTP_SECRET`

---

## Run Locally

```bash
# Start FastAPI backend
python -m uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload
```

API available at `http://localhost:8000`

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/auth/token` | POST | Login (returns JWT) |
| `/api/snapshot` | GET | Full dashboard data — P&L, trades, positions, prices |
| `/api/bot/debug` | GET | Live signal scores for all 3 strategies |
| `/api/bot/pause` | POST | Pause trading |
| `/api/bot/resume` | POST | Resume trading |
| `/api/backtest` | POST | Run backtest (Musashi / Raijin / ATR Intraday) |
| `/api/journals` | GET | List saved daily journals |
| `/api/journals/{date}` | GET | Get a specific day's journal |
| `/ws` | WebSocket | Live snapshot every 5 seconds |

---

## Backtesting

```json
POST /api/backtest
{
  "strategy": "Musashi",
  "symbol": "NIFTY",
  "period": "60d",
  "interval": "15m",
  "capital": 20000,
  "risk_pct": 4.0,
  "rr_ratio": 2.5,
  "daily_loss_limit_pct": 8.0
}
```

Strategies: `"Musashi"` | `"Raijin"` | `"ATR Intraday"`

Backtest uses real Zerodha historical bars — same data as live trading.

---

## Paper vs Live

| | Paper Mode | Live Mode |
|-|-----------|-----------|
| `TRADING_MODE` | `paper` | `live` |
| Real money | No | Yes |
| Orders placed | Simulated (MockBroker) | Real Zerodha orders via jugaad-trader |
| Which strategies go live | None | ATR Intraday only (Musashi + Raijin log to DB only) |
| Market data | Zerodha → NSE India | Same |

> **Note:** Musashi and Raijin do not place real orders even in live mode — they log simulated trades to the database. Only ATR Intraday calls `broker.place_order()`.

**Before switching to live:** Run paper for at least 1–2 weeks after the data pipeline is stable → verify signal scores in Railway logs → check trade quality in journals → then set `TRADING_MODE=live`.

---

## Security

- All credentials in `.env` — never committed (`.gitignore`)
- Logs automatically mask API keys, passwords, tokens
- Never push `.env` to git
