# AI Trading Bot — Agent Guide

## Architecture (updated 2026-05-06)

Intraday NIFTY options trading bot. Phase A capital ₹1.25L → ₹15L target.
Stack: Python 3.12, FastAPI, Next.js, SQLite, Angel One SmartAPI, Claude Sonnet 4.6.

### One Live Strategy: ATR Intraday

| Strategy | Score mode | Sections | Threshold | Scheduler |
|---|---|---|---|---|
| ATR Intraday | `atr_only` | 1–11 (SMA / EMA / RSI / MACD / VWAP / ORB / PDH/PDL / patterns / PCR / OI / herd / ATR-vol) + 12 (S/R + structure gate) | live ≥ ±8 (config ±6) | `*/5 * * * *` :05 |

A second `_atr_fast_check` job fires at :30 of each odd-numbered minute when the
previous bar's score was 6–7 (near miss) — catches moves 3 minutes early.

C-ICT was active previously and will be **rebuilt from scratch later**. The
prior C-ICT scorer (`order_flow.py`, `ict_only` mode) and the dead Musashi /
Raijin / SMC / Expiry-Day Gap research code paths have all been removed.

### Key files

| File | Role |
|---|---|
| `strategies/signal_scorer.py` | `score_symbol(..., mode="atr_only")` — 12-section scorer, returns `{score, action, threshold, signals, breakdown, confidence}` |
| `strategies/trend_strategy.py` | `TrendStrategy(strategy_name, score_mode)` — order execution, SL/TP, zone gating |
| `core/bot_runner.py` | `_atr_cycle`, `_atr_fast_check`, `_position_guardian`, `_zone_briefing`, `_eod_squareoff`, `_save_journal` |
| `core/zone_briefing.py` | Pre-market watch zones (PDH/PDL/ORB/weekly) computed at 09:00 IST |
| `core/sr_levels.py` | RBD/DBR institutional zones, structure detection |
| `core/ipc.py` | Flag files: pause/resume, force_trade, day_bias, sl_orders, tp_orders, watch_zones, settings, event_blocks, runtime_holidays |
| `core/journal.py` | Daily journal JSON + Claude AI review (haiku for daily, sonnet for weekly) |
| `api/server.py` | FastAPI: snapshot, signal-log, paper-comparison, chart-data, holidays, event-blocks, WebSocket |
| `config.py` | All financial params hardcoded (capital, SL/TP, premium target, lot size, holidays) — not env vars |
| `scripts/backtest_live_atr.py` | Realistic ATR backtest with `--slippage` modeling |
| `scripts/collect_option_snapshots.py` | 5-min option chain snapshot writer (builds historical premium dataset) |

### Daily routine — fully automated on the droplet

All scheduling lives in `core/bot_runner.py` (APScheduler in the api container)
and the shell-loop wrapper in the collector container. There is no laptop
dependency, no cron, no Windows Task Scheduler.

1. **Container start (any time)** — Angel One TOTP login + warm-up.
2. **09:00 IST** — `_zone_briefing` writes today's watch zones.
3. **09:00–15:35 IST** — collector container scrapes 5-min option chain
   snapshots → CSV in `db/oi_snapshots/` + Mongo `option_snapshots`.
4. **09:30 IST** — `_vix_auto_lots_set` reads India VIX, sets `min_lots`.
5. **09:30–15:20 IST** — every 5 min: ATR cycle + position guardian (only
   when a position is open) + Angel One trade book sync.
6. **15:20 IST** — `_eod_squareoff` closes any open position, then
   `_save_journal` writes JSON + Claude AI review (mirrored to Mongo).
7. **20:00 IST** — day bias resets to NEUTRAL, zone-entry-fired flag clears.
8. **Saturday 08:00 IST** — Claude weekly review (mirrored to Mongo).

**No daily token script run is required.** TOTP auth is lazy and self-healing.

`scripts/get_token.py` is a manual sanity check only:
```bash
docker compose exec api python scripts/get_token.py
```

### When changing strategy logic — update ALL these places

1. `strategies/signal_scorer.py` — section logic / thresholds
2. `strategies/trend_strategy.py` — execution, SL/TP, live threshold floor
3. `core/bot_runner.py` — cycle jobs, `last_scores` shape
4. `scripts/backtest_live_atr.py` — keep parity with live logic
5. `frontend/app/debug/page.tsx` — Signal Radar display
6. `frontend/app/strategies/page.tsx` — Strategy Playbook copy

---

## MCP Tools: code-review-graph

**ALWAYS use graph tools BEFORE Grep/Glob/Read** for codebase exploration.
The graph is faster, cheaper, and gives structural context (callers, dependents,
test coverage) that file scanning cannot.

| Tool | Use when |
|------|----------|
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `query_graph` | Tracing callers/callees/imports/tests |
| `detect_changes` | Risk-scored review of code changes |
| `get_impact_radius` | Blast radius of a change |
| `get_review_context` | Token-efficient source snippets for review |
| `get_architecture_overview` | High-level structure |

Fall back to Grep/Glob/Read only when the graph doesn't cover what you need.
