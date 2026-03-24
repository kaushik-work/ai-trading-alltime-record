# AI Trading Bot — All-Time Record

Fully automated intraday trading bot for **NIFTY & BANKNIFTY** using Claude AI (Anthropic) + jugaad-trader (Zerodha, no paid API needed).

Strategy inspired by **AishDoc** — price action, ATR-based sizing, VWAP, ORB breakout.

---

## Roadmap

| Phase | Budget | Style | Goal | Status |
|-------|--------|-------|------|--------|
| **A** | ₹20,000 | Intraday NIFTY + BANKNIFTY | Compound to ₹1.5L | ✅ Active |
| **B** | ₹1.5L | Intraday + Short-term swing | Compound to ₹15L | 🔒 Locked |
| **C** | ₹15L+ | Options Selling (Straddle / Iron Condor) | Monthly income | 🔒 Locked |

---

## How It Works

```
Every 5 minutes (9:45 AM – 3:10 PM IST):

  Real OHLCV data (yfinance)
        ↓
  Intraday indicators: VWAP, ORB, PDH/PDL, ATR(14), 15-min RSI
        ↓
  Signal scoring (-10 to +10):
    • Price vs SMA50/SMA20/EMA9   • ATR volatility filter
    • RSI pullback zone           • VWAP above/below
    • MACD crossover              • ORB breakout
    • Volume ratio                • 15-min trend
    • Bollinger Bands             • PDH/PDL levels
    • Candlestick patterns (12 patterns detected)
        ↓
  Score ≥ 5 → Claude AI (claude-sonnet-4-6) reviews and confirms
        ↓
  Order placed via jugaad-trader (Zerodha)
        ↓
  Trade logged → All-time records checked

  3:10 PM IST: Mandatory square-off of all positions (never hold overnight)
  3:35 PM IST: Claude AI end-of-day review
```

---

## AishDoc Strategy Rules (enforced in code)

| Rule | Implementation |
|------|----------------|
| Wait for market to settle | No trades before **9:45 AM** |
| Trade with the 15-min trend | SMA9 vs SMA20 on 15-min chart |
| VWAP = primary intraday level | +2 score if above, -2 if below |
| ORB breakout = entry trigger | +3 score on ORB break (first 15 min range) |
| ATR-based stop-loss | SL placed 1× ATR below entry — adapts to volatility |
| ATR-based position sizing | Qty = Risk ÷ ATR (not fixed lot) |
| 1:2 Risk:Reward minimum | SL = 1× ATR, TP = 2× ATR |
| Respect PDH / PDL | Previous day high/low scored as S/R |
| Never hold intraday overnight | Hard square-off job at **3:10 PM** |
| Risk ≤ 5% per trade | ₹1,000 max risk on ₹20k budget |

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure `.env`
```env
# Zerodha credentials
ZERODHA_USER_ID=AB1234
ZERODHA_PASSWORD=your_password
ZERODHA_TOTP_SECRET=your_totp_base32_secret

# Claude AI
ANTHROPIC_API_KEY=sk-ant-...

# Mode
TRADING_MODE=paper        # paper = simulation | live = real money

# Risk (Phase A defaults)
STARTING_BUDGET=20000
MAX_TRADE_AMOUNT=10000
MAX_DAILY_LOSS=2000
STOP_LOSS_PCT=1.5
TAKE_PROFIT_PCT=3.0
RISK_PER_TRADE_PCT=5.0
MIN_SIGNAL_SCORE=5
```

### 3. Get TOTP secret
1. Login to [kite.zerodha.com](https://kite.zerodha.com) → My Profile → Security
2. Enable Two-Factor Auth → click **"Can't scan? Get the key instead"**
3. Copy the 32-character base32 key → paste as `ZERODHA_TOTP_SECRET`

---

## Run Commands

### Start the trading bot
```bash
python main.py
```

### Start the dashboard
```bash
python -m streamlit run dashboard/app.py --server.headless true
```

### Run both (two terminals)
```bash
# Terminal 1 — Bot
python main.py

# Terminal 2 — Dashboard
python -m streamlit run dashboard/app.py --server.headless true
```

Dashboard opens at: **http://localhost:8501**

---

## Dashboard Features

| Tab | Features |
|-----|----------|
| **Dashboard** | KPI metrics, all-time records, recent trades, P&L equity curve, live NIFTY/BANKNIFTY prices |
| **Manual Trade** | Force BUY/SELL any symbol, view open positions with unrealized P&L |
| **Setup & Config** | Credential status, test Zerodha connection, risk parameters, step-by-step live guide |

**Sidebar controls:**
- **Pause Bot** — signals the bot to stop trading at next cycle
- **Resume Bot** — signals the bot to resume
- **Refresh** — refresh dashboard data

---

## Project Structure

```
ai-trading-alltime-record/
├── main.py                     # Entry point — scheduler, signal handling
├── config.py                   # All settings (loaded from .env)
├── requirements.txt
│
├── core/
│   ├── brain.py                # Claude AI — BUY/SELL/HOLD decisions
│   ├── broker.py               # MockBroker (paper) + JugaadBroker (live)
│   ├── memory.py               # SQLite trade history
│   ├── records.py              # All-time records tracker
│   ├── security.py             # Fernet encryption, credential masking in logs
│   └── ipc.py                  # Flag-file IPC between bot and dashboard
│
├── data/
│   ├── market.py               # yfinance data + indicators (daily + intraday)
│   └── oi_data.py              # NSE PCR / OI data (for Phase C)
│
├── strategies/
│   ├── trend_strategy.py       # Main strategy engine (AishDoc intraday)
│   ├── signal_scorer.py        # Signal scoring -10 to +10
│   ├── patterns.py             # 12 candlestick pattern detectors
│   └── base_strategy.py        # Base class (reference)
│
├── dashboard/
│   └── app.py                  # Streamlit dashboard (3 tabs)
│
└── db/
    ├── trading.db              # SQLite — trades, summaries, records
    └── flags/                  # IPC flag files (pause, resume, force_trade)
```

---

## Security

- All credentials in `.env` (never committed — in `.gitignore`)
- Trade data encrypted at rest using Fernet AES-128
- Logs automatically mask API keys, passwords, tokens
- `.env` → never share, never push to git

---

## Paper vs Live

| | Paper Mode | Live Mode |
|-|-----------|-----------|
| `TRADING_MODE` | `paper` | `live` |
| Real money | No | Yes |
| Orders | Simulated (MockBroker) | Real (jugaad-trader → Zerodha) |
| Market data | yfinance (real prices) | yfinance + Zerodha live feed |
| Recommended | Start here, run for days | Only after paper testing |

**Before switching to live:** Test connection in the Setup tab → verify credentials → run paper for at least a few sessions → then change `TRADING_MODE=live` and restart.
