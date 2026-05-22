# AI Trading Bot — Agent Guide

## Architecture (updated 2026-05-23 — shadow-only)

Forward-test NIFTY options bot. **Places NO real orders.** Every decision is
logged to Mongo `shadow_trades` for offline evaluation. ATR Intraday was
retired in this revision.

Stack: Python 3.12, FastAPI, Next.js, MongoDB Atlas, SQLite (vestigial),
Angel One SmartAPI.

### Active strategies — three independent shadow signals

| Strategy | Trigger | IC |
|---|---|---|
| `q5_straddle_level` | ATM straddle > trailing-5d P70 | +0.132 |
| `q5_straddle_mom3`  | 3-bar change in ATM straddle > P70 | +0.120 |
| `q5_pcr_mom3`       | 3-bar change in PCR_OI > P70 | +0.115 |

Locked params: SL=₹10, RR=2.25, side=CE, strike=ITM by 50, 4 trades/day/strategy,
loss caps ₹2K/strategy and ₹3.5K aggregate, same-strike correlation guard ON.

### Key files

| File | Role |
|---|---|
| `strategies/feature_signals.py` | Three signal classes + threshold cache, ITM-50 strike, locked params |
| `core/shadow_book.py` | Per-strategy book (one open trade max), Mongo-persisted, restart-safe |
| `core/risk_budget.py` | Daily loss caps, lot multiplier, same-strike guard |
| `core/bot_runner.py` | `_shadow_signal_tick` (30s), `_option_chain_refresh` (15m), `_daily_token_refresh` (08:30/12:00/14:00), `_save_journal` (15:25) |
| `core/mongo.py` | Mirror helpers for shadow_trades, indexes |
| `core/journal.py` | Daily shadow summary writer |
| `core/ipc.py` | pause/resume flags, event blocks, market holidays, settings |
| `api/server.py` | `/api/shadow-trades`, `/api/risk-budget`, `/api/pnl`, `/api/journals`, WebSocket |
| `scripts/collect_option_snapshots.py` | 5-min option chain writer (builds the dataset signals depend on) |
| `scripts/replay_multi_strategy.py` | Full forward-test simulation |
| `config.py` | Capital baseline + lot sizes + watchlist only |

### Daily routine — fully automated on the droplet

All scheduling lives in `core/bot_runner.py` (APScheduler in the api container).
No laptop dependency, no cron, no Windows Task Scheduler.

1. **Container start** — Angel One TOTP login + warm-up.
2. **09:15–15:30 IST** — `_shadow_signal_tick` fires every 30 s.
3. **09:00–15:35 IST** — collector container writes option_snapshots
   (5-min × 17 strikes × CE/PE) to CSV + Mongo.
4. **08:30 / 12:00 / 14:00 IST** — daily Angel JWT refresh.
5. **15:25 IST** — `_save_journal` writes shadow summary to disk + Mongo.

### When changing signal logic — update these places

1. `strategies/feature_signals.py` — signal class + locked params
2. `core/bot_runner.py` — `_shadow_signal_tick` if interfaces change
3. `scripts/replay_multi_strategy.py` — keep parity with live logic
4. `scripts/verify_shadow_signal.py` — must keep passing
5. `frontend/app/components/ShadowBadge.tsx` — chip rendering
6. `README.md` — locked param table

---

## MCP Tools: code-review-graph

**ALWAYS use graph tools BEFORE Grep/Glob/Read** for codebase exploration.
The graph is faster, cheaper, and gives structural context.

| Tool | Use when |
|------|----------|
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `query_graph` | Tracing callers/callees/imports/tests |
| `detect_changes` | Risk-scored review of code changes |
| `get_impact_radius` | Blast radius of a change |
| `get_review_context` | Token-efficient source snippets for review |
| `get_architecture_overview` | High-level structure |

Fall back to Grep/Glob/Read only when the graph doesn't cover what you need.
