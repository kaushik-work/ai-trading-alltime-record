# Strategy — Price-action S/R Retest (Production)

> Live on Delta India for BTCUSD + ETHUSD perpetuals.
> Pure perp price-action S/R retest strategy.
>
> **Deterministic by design — no LLM, no ML, no sentiment.**

---

## 1. TL;DR

| | |
|---|---|
| **Edge source** | S/R retest + buyer/seller aggression at 4h levels, filtered by 24h trend |
| **Universe** | BTCUSD + ETHUSD perpetuals on Delta India |
| **Decision cadence** | 15-minute entry tick at :00/:15/:30/:45:30 UTC; 2s bar updates |
| **Leverage** | 40× isolated |
| **Per-cycle deploy** | 50% of live wallet pool, BTC/ETH split 50/50 |
| **Exits** | Pure SL/TP bracket: BTC −0.6% / +4.2%, ETH −0.7% / +4.9% |
| **Daily kill switch** | Halt new entries if day P&L < −5% of base equity |
| **Backtest** | Apr–Jun 2026: BTC +17.28% (WR 57.3%), ETH +18.10% (WR 56.6%) |

---

## 2. The Core Idea

Decode the Hindi livestream in three rules:

1. **Higher-timeframe bias first.** Only buy dips in an uptrend, only sell
   rallies in a downtrend. We use a 24h simple moving average as the bias.
2. **Trade at levels, never mid-range.** The 4-hour range high/low are the
   only valid entry zones. Price must be within ±0.4% of a level.
3. **Wait for aggression.** A strong reversal candle (body ≥ 1.3× the 4h
   average body, wick ≤ 45%) confirms that buyers (at support) or sellers
   (at resistance) have stepped in.

Then: wider stop, big target, trail to breakeven.

We are not predicting moves. We are entering where the last defence of a level
happened, with a stop just beyond that defence and a target at the opposite
edge of the range.

---

## 3. Decision Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│  Live perp mark updates every 2s                                │
│  → build 1m OHLC candles internally                             │
└─────────────────────────┬───────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  4h range high / low  +  24h trend MA                           │
│  → is price near a level AND with the trend?                    │
└─────────────────────────┬───────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  Closed 1m candle: body ≥ 1.3× avg, wick ≤ 45%                  │
│  → long at support, short at resistance                         │
└─────────────────────────┬───────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  Bracket order: SL below/above reversal candle, TP = RR target  │
│  Trail SL to entry after +1R                                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. Production Dials

| Dial | Value | File |
|---|---|---|
| S/R lookback | 4h (240 × 1m) | `strategies/price_action_sr.py` |
| Trend lookback | 24h (1440 × 1m) | `strategies/price_action_sr.py` |
| Range width max | 1.5% | `strategies/price_action_sr.py` |
| Level zone | ±0.4% of range high/low | `strategies/price_action_sr.py` |
| Retest mode | `wick_touch` | `strategies/price_action_sr.py` |
| Wick touch tolerance | 7 bps vs S/R level | `strategies/price_action_sr.py` |
| Body position threshold | 0.70 (close in top/bottom 30%) | `strategies/price_action_sr.py` |
| Body multiplier | 1.3× | `strategies/price_action_sr.py` |
| Wick ratio max | 45% | `strategies/price_action_sr.py` |
| BTC SL / target | 0.6% / 4.2% (1:7) | `strategies/price_action_sr.py` |
| ETH SL / target | 0.7% / 4.9% (1:7) | `strategies/price_action_sr.py` |
| Max hold | 4h | `strategies/price_action_sr.py` |
| Signal cooldown | 1h | `strategies/price_action_sr.py` |
| Block after loss | 180 min | `strategies/price_action_sr.py` |
| Optional WR filters | volume, RSI, trend slope, range min, hours, HTF align, engulfing, pin bar | `strategies/price_action_sr.py` |
| Leverage | 40× | `core/risk_management.py` |
| Capital per cycle | 50% of pool | `core/risk_management.py` |
| Daily kill | −5% of base equity | `core/risk_management.py` |
| Exit regime | `pure_sltp` | `core/risk_management.py` |

---

## 5. Backtest Evidence

Run:

```bash
cd delta_exchange
UNDERLYING=BTC START_DT=2026-03-01 END_DT=2026-06-20 STAGE=discover \
  .venv/Scripts/python fetch_delta_history.py
UNDERLYING=ETH START_DT=2026-03-01 END_DT=2026-06-20 STAGE=discover \
  .venv/Scripts/python fetch_delta_history.py
.venv/Scripts/python backtest_price_action_sweep.py \
  --btc-subdir . --eth-subdir eth --date-start 2026-04-01 --date-end 2026-06-20
```

### April–June 2026 (~80 days)

| Asset | SL / R:R | Trades | WR | P&L | PF | MaxDD | MaxCL |
|---|---|---:|---:|---:|---:|---:|---:|
| BTCUSD | 0.6% / 1:7 | 124 | 57.3% | +17.28% | 1.79 | 2.52% | 5 |
| ETHUSD | 0.7% / 1:7 | 83 | 56.6% | +18.10% | 2.01 | 2.33% | 3 |

These figures are unlevered. Live leverage is **40× isolated** (effective ~20×
pool exposure at 50% per-cycle capital). A liquidation-aware sweep on the same
1m data shows ~355%/mo BTC and ~382%/mo ETH with zero in-sample liquidations,
but a ~2.5% adverse wick against an open position wipes the allocated margin.
Run `delta_exchange/backtest_leverage_liquidation.py` to reproduce.

### Walk-forward (40% / 60% split)

| Asset | First 40% PF | Last 60% PF |
|---|---|---|
| BTCUSD | 1.45 | 1.71 |
| ETHUSD | 2.18 | 1.78 |

Both assets remain profitable in both halves. The `wick_touch` retest filter
plus 180-min block-after-loss lifts WR to 57.3% / 56.6%, keeps MaxDD under 2.6%,
and holds Max consecutive losses at 3–5.

Additional WR-boost filters are exposed in `strategies/price_action_sr.py` and
`delta_exchange/backtest_price_action_sweep.py` for experimentation. Individually
they mostly reduce trade count; the safest global improvement is the
block-after-loss rule.

---

## 6. Risks & Mitigations

| Risk | Mitigation today | Open gap |
|---|---|---|
| Range-bound chop | Trend filter + `wick_touch` retest | No volatility regime filter |
| Long losing streaks | 1:7 R:R + stricter retest | 3–5 consecutive losses possible |
| Late-period degradation | `wick_touch` retest filter | Need more OOS months |
| Slippage on market entry | Next-tick execution in backtest | Live fill may differ |
| Breakeven trail not in live code | Fixed bracket still profitable | Add BE trail to position manager |

---

## 7. Operational Notes

- The bot no longer needs option-chain marks for crypto signal generation.
  WebSocket subscriptions can be reduced to BTC/ETH perps only, lowering load.
- The 2-second position-management tick feeds perp marks into the strategy's
  internal 1m candle builder. The 15-minute entry tick evaluates the signal.
- `CRYPTO_TRADING_MODE=paper` is strongly recommended for the first 2–4 weeks
  of live validation.
