# AI Trading Bot — Agent Guide

## Architecture Overview (updated 2026-04-07)

This is an **intraday NIFTY options trading bot** targeting ₹1.25L → ₹15L.
Stack: Python 3.12, FastAPI, Next.js, SQLite, Zerodha Kite Connect, Claude claude-sonnet-4-6.

### Two Independent Bots (NOT combined)

| Strategy | Score mode | Sections used | Threshold | Scheduler |
|---|---|---|---|---|
| ATR Intraday | `atr_only` | 1–11 (SMA/EMA/RSI/MACD/VWAP/ORB/PDH) | ±6 | Every 5 min |
| C-ICT | `ict_only` | 12 only (ICT OB + liquidity sweep) | ±2 | Every 5 min (+2m30s offset) |

Each has its own `TrendStrategy` instance → own position tracker → own VIX gate.

### Key Files

| File | Role |
|---|---|
| `strategies/signal_scorer.py` | Scoring engine. `mode` param gates which sections run. |
| `strategies/order_flow.py` | ICT logic: `find_ict_signals()`, `analyse()` |
| `strategies/trend_strategy.py` | Entry/exit, `TrendStrategy(strategy_name, score_mode)`, `self.last_score` |
| `core/bot_runner.py` | `_atr_cycle()` + `_ict_cycle()` jobs, `_is_vix_blocked(strategy)` |
| `core/ipc.py` | Flag files: pause, vix_override, vix_override_atr, vix_override_ict, day_bias |
| `core/journal.py` | Daily journal: trades + VIX context + day bias review + auto learning notes |
| `api/server.py` | FastAPI: snapshot, debug, VIX override endpoints, WebSocket |
| `config.py` | All financial params hardcoded here — NOT env vars (except TRADING_MODE) |
| `scripts/backtest_full.py` | Month-on-month backtest for both strategies |

### VIX Gate System

Three flags (persist across restarts, excluded from `clear_all_flags()`):
- `vix_override` — global bypass (both strategies)
- `vix_override_atr` — bypass ATR Intraday only
- `vix_override_ict` — bypass C-ICT only

API endpoints: `POST /api/bot/vix-override`, `/api/bot/vix-override/atr`, `/api/bot/vix-override/ict`

### When Changing Strategy Logic — Update ALL 6 Places
1. `strategies/signal_scorer.py` — mode logic + section thresholds
2. `strategies/order_flow.py` — ICT signals implementation
3. `strategies/trend_strategy.py` — execution + `score_mode` param usage
4. `core/bot_runner.py` — cycle jobs, VIX checks, `last_scores` population
5. `scripts/backtest_full.py` — keep in sync with live logic
6. `frontend/app/debug/page.tsx` — Signal Radar display for both strategies

### Daily Routine
1. Run `python scripts/get_token.py` before 9:15 AM IST
2. Bot auto-starts ATR (9:45) + C-ICT (9:45 +2m30s), skips lunch 12:30–13:30
3. Mandatory EOD squareoff at 15:10, journal saved at 15:20
4. Day bias resets to NEUTRAL at 20:00 IST

---

## MCP Tools: code-review-graph

**ALWAYS use graph tools BEFORE Grep/Glob/Read to explore the codebase.**
The graph is faster, cheaper (fewer tokens), and gives structural context.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.
