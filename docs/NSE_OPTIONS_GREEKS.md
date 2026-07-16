# NSE Options: Greeks, Data Collection & Naked Directional Buying

## 1. Why this matters

Angel One SmartAPI gives us **price, bid/ask, volume and OI** — but it does **not**
give us implied volatility or Greeks.  For naked long-option buying (just CE or
just PE) we absolutely need:

| Field | Why it matters for naked buying |
|---|---|
| **Implied Volatility (IV)** | Tells us if the option is cheap or expensive.  Buying into high IV is a headwind. |
| **Delta** | Measures directional exposure.  We want ~0.50 delta (ATM) for the best risk/reward. |
| **Theta** | Daily time-decay cost.  Theta is the enemy; we want it as low as possible relative to expected move. |
| **Vega** | Sensitivity to IV changes.  Long options are long vega — we profit if IV rises. |
| **Gamma** | Acceleration near expiry.  High gamma is a double-edged sword. |
| **Days to expiry (DTE)** | Less DTE = more gamma/theta risk.  Naked buyers usually want 3–10 DTE for momentum, or monthly for lower theta. |
| **India VIX** | Market-wide fear gauge.  Spiking VIX means expensive options — avoid fresh longs. |
| **Underlying spot + futures** | Spot for strike selection; futures for fair-value / cost-of-carry checks. |

## 2. What we collect now

`scripts/collect_option_snapshots.py` now supports:

```bash
python scripts/collect_option_snapshots.py --symbol NIFTY --interval 5 --strikes 8 --expiries 2 --greeks --vix
```

- `--expiries 2` — nearest **two** weekly expiries (doubles data, lets the bot pick the better Greek profile).
- `--greeks` — computes and stores **iv, delta, gamma, theta, vega, rho** using Black-Scholes.
- `--vix` — stores India VIX alongside each snapshot.
- Output: `db/oi_snapshots/YYYY-MM-DD_SYMBOL.csv` + MongoDB `option_snapshots`.

### Schema

Base columns: `timestamp, symbol, expiry, strike, option_type, ltp, bid, ask, volume, oi, spot`

With `--greeks`: adds `iv, delta, gamma, theta, vega, rho`

With `--vix`: adds `vix`

## 3. Greek computation

`nse/data/greeks.py` — scalar version for live ticks.

`nse/data/greeks_vectorized.py` — array version for backfilling / loaders.

Model: Black-Scholes for European index options, continuous dividend yield.

Defaults:
- Risk-free rate = 6.5% p.a.
- Dividend yield = 0.0% (close enough for intraday index work).
- IV solved by Brent's method.

## 4. Greek-aware naked strategy

`nse/strategies/greek_naked_options.py`

Same core signal as the naked strategy (synthetic-forward divergence), but adds
filters and strike selection:

| Filter | Default | Purpose |
|---|---|---|
| `max_iv_rank` | 0.70 | Don't buy when IV is in top 30% of its 60-day range. |
| `min_days_to_expiry` | 1.0 | Avoid the final 24h gamma/theta explosion. |
| `min_vega_theta_ratio` | 2.0 | Want at least INR 2 of vega exposure for every INR 1 of daily theta. |
| `max_theta_pct` | 0.20 | Daily theta should be <= 20% of premium paid. |
| `max_vix_1d_change_pct` | 20% | Skip if VIX spiked >20% from yesterday's close. |
| `target_delta_abs` | 0.50 | Pick strike closest to 50-delta. |
| `delta_band` | 0.15 | Accept strikes between 35- and 65-delta. |

The strategy evaluates **all expiries** present in the snapshot and picks the
one with the best divergence × Greek score.

## 5. Backtest usage

```bash
# Base naked strategy (ATM, no Greek filters)
PYTHONPATH=. python nse/backtest/naked_options.py --symbol NIFTY --source mongo --capital 300000

# Greek-aware strategy
PYTHONPATH=. python nse/backtest/naked_options.py --symbol NIFTY --source mongo --greek --capital 300000
```

The loader computes Greeks on the fly for old snapshots that lack them, so the
existing ~1 month of Mongo data is immediately usable.

## 6. Restarting collectors on the server

The collectors have been offline since **2026-06-09**.  On the droplet:

```bash
cd /root/ai-trading-alltime-record

# Remove stale containers (fixes the "name already in use" error)
docker compose --profile collectors down --remove-orphans
docker rm -f ai-trading-alltime-record-collector-1 \
             ai-trading-alltime-record-collector-banknifty-1 \
             ai-trading-alltime-record-collector-finnifty-1 \
             ai-trading-alltime-record-collector-sensex-1

# Pull latest code and rebuild
git pull origin main
docker compose build

# Start collectors only (NOT the synthetic-forward live runner)
docker compose --profile collectors up -d --force-recreate

# Watch logs
docker compose logs -f collector
```

`deploy.sh` and `.github/workflows/deploy.yml` have been updated to
auto-restart collectors on every deploy without starting the legacy
synthetic-forward runner.

## 7. Backfilling Greeks on old Mongo snapshots

Optional — only needed if you want Greeks persisted in Mongo for faster loads:

```bash
PYTHONPATH=. python scripts/enrich_option_snapshots_greeks.py --dry-run
PYTHONPATH=. python scripts/enrich_option_snapshots_greeks.py
```

Warning: this touches every document and will take time.  The backtest loader
currently computes Greeks on the fly, so this is not strictly required.

## 8. What is still missing / next steps

1. **More historical data** — only ~1 month in Mongo.  Need at least 3–6 months
   to judge Greek filters.
2. **Futures data** — synthetic-forward signal currently uses spot; futures
   fair value would be cleaner.
3. **Multiple expiry backtests** — `--expiries 2` was added to the collector;
   need a few weeks of data before the Greek strategy can pick between expiries.
4. **IV term structure** — compare front-month vs back-month IV.
5. **Skew analysis** — IV smile/skew can hint at directional bias.
6. **Live VIX close** — backtests use snapshot VIX; live runner should fetch
   prior-day VIX close from broker.
7. **NSE/BSE instrument coverage** — currently NIFTY, BANKNIFTY, FINNIFTY (NFO)
   and SENSEX (BFO).  MIDCPNIFTY could be added.

## 9. Quick Greek cheat sheet for naked buying

| You want | Greek sign | What to check |
|---|---|---|
| Directional move up | Delta > 0 (CE) | Buy CE, delta 0.45–0.55 |
| Directional move down | Delta < 0 (PE) | Buy PE, delta -0.45 to -0.55 |
| Cheap options | Low IV / low IV rank | IV rank < 50% ideal |
| Low time decay | Theta small negative | theta/premium < 15–20% per day |
| Volatility upside | Vega positive | vega/theta > 2 |
| Avoid expiry blow-up | DTE > 1–2 days | gamma manageable |

Remember: naked long options are **gamma + vega + delta** trades.  You need the
move, you need it before theta kills you, and you ideally want to buy when vol
is cheap.
