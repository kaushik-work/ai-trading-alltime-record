# Claude Agent Notes

> Project state: **no live strategy on either venue.** Every strategy was
> deleted in `62f89c9` (2026-08-04) after measurement killed it — see
> `docs/RESEARCH_LEARNINGS.md`. `_get_strategies()` returns `{}` and
> `ENABLE_NSE_RUNNER=false`. The execution layer is intact and working:
> order placement, brackets, reconciliation, kill switch, dashboard.
> NSE option-chain collectors run for research data behind
> `docker compose --profile nse up -d`. See `AGENTS.md` for architecture.
>
> In progress: NIFTY options multi-lens system (see the architecture brief).

## Project-specific rules
- **Crypto Mongo collections use `crypto_` prefix.** Don't write to legacy
  NSE collections from crypto code.
- **Risk dials live in `core/risk_management.py`**, not `.env`. PR review,
  not silent edits.
- **No LLM / RL in signal generation.** The deterministic core decides every
  trade. The NIFTY vision lens is the one deliberate exception and is pinned
  at weight 0 (commentary only) until live attribution earns it a vote.
- **Signal seam:** a strategy emits `CryptoSignalDecision`
  (`core/execution/signal_types.py`) and registers in
  `crypto_runner._get_strategies()`. Nothing else in the execution path changes.
- **Execution:** `core/execution/crypto_runner.py` (Delta),
  `nse/execution/nse_runner.py` (Angel). Brokers: `core/brokers/delta_crypto.py`,
  `nse/broker/angel_broker.py`. WS stream: `core/ws/delta_stream.py`.
- **Angel order placement requires a whitelisted static IP**; market data does
  not. Order placement belongs on the VPS sentinel only — never localhost.

---

## 1. Plan First
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don't keep pushing
- Write detailed specs upfront to reduce ambiguity

## 2. Subagent Strategy
- Use subagents to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- One task per subagent for focused execution

## 3. Verification Before Done
- Never mark a task complete without proving it works
- Run scripts, check logs, demonstrate correctness
- Ask yourself: "Would a staff engineer approve this?"

## 4. Demand Elegance
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky — implement the elegant solution instead
- Skip this for simple, obvious fixes — don't over-engineer

## 5. Autonomous Bug Fixing
- When given a bug report: just fix it, don't ask for hand-holding
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user

## 6. Core Principles
- **Simplicity First** — make every change as simple as possible, impact minimal code
- **No Laziness** — find root causes, no temporary fixes, senior developer standards
- **No Extras** — don't add features, comments, or refactors beyond what was asked

---

## MCP Tools: code-review-graph

**ALWAYS use graph tools BEFORE Grep/Glob/Read.** Faster, cheaper, gives structural context.

| Tool | Use when |
|------|----------|
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `query_graph` | Tracing callers, callees, imports, tests |
| `detect_changes` | Risk-scored review of code changes |
| `get_impact_radius` | Blast radius of a change |
| `get_review_context` | Token-efficient source snippets for review |
| `get_architecture_overview` | High-level structure |

Fall back to Grep/Glob/Read only when the graph doesn't cover it.
