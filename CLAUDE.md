# Claude Agent Notes

> Project state: **nothing trades live on either venue.** Every crypto strategy
> was deleted in `62f89c9` (2026-08-04) after measurement killed it.
> `_get_strategies()` returns `{}`, `ENABLE_NSE_RUNNER=false`, and
> `ENABLE_OPTIONS_RUNNER` defaults false with `OPTIONS_PAPER_MODE=true`.
> The execution layer is intact: order placement, brackets, reconciliation,
> kill switch, dashboard.
>
> **NIFTY multi-lens council is built and wired** — 8 lenses, per-lens brains,
> two-round deliberation, end-of-day journal, `options_runner` → sentinel.
> **1 of 8 lenses has a measured edge** (`volume_oi`, PROBATION 0.50); the other
> seven sit at SHADOW weight 0. Read `docs/RESEARCH_LEARNINGS.md` §3.11–3.17
> before touching any of it — most of the obvious next ideas have already been
> measured and rejected there.
>
> Still open: TEST (2025–26) unspent · vision unmeasured · Angel static IP and
> Algo-ID not yet registered (external, blocks live regardless of code).

## Project-specific rules
- **Crypto Mongo collections use `crypto_` prefix.** Don't write to legacy
  NSE collections from crypto code.
- **Risk dials live in `core/risk_management.py`**, not `.env`. PR review,
  not silent edits.
- **No LLM / RL in signal generation.** The deterministic core decides every
  trade. The NIFTY vision lens is the one deliberate exception and is pinned
  at weight 0 (commentary only) until live attribution earns it a vote.
- **A lens earns weight by measurement, never by argument.** New lenses join
  `ROSTER` at SHADOW 0 and are recorded in `nse/lenses/bootstrap.py` with the
  run that produced their number. Changing a weight without a measurement is
  the one thing this whole apparatus exists to prevent.
- **Neutral is measured, never assumed to be zero.** Two lenses have now been
  broken by pivoting a normalised quantity on 0.0 when the market sat elsewhere
  (`greeks`: `SKEW_NEUTRAL=-0.2098`; `smile`: butterfly positive 97.7% of the
  time, producing n=34 across three years). Measure the median first.
- **No absolute price/level constants.** Use ATR-relative or percentile-relative
  thresholds. An absolute ATR cut fitted on TRAIN kept 11% of TRAIN and 6% of
  VALIDATE and stopped meaning the same thing.
- **Never flip a convention after seeing its result.** A negative measurement is
  one bit of information, not a licence to invert and re-run — that is what made
  `train_signed` look like it beat the champion (§3.13).
- **Signal seam:** a strategy emits `CryptoSignalDecision`
  (`core/execution/signal_types.py`) and registers in
  `crypto_runner._get_strategies()`. Nothing else in the execution path changes.
- **NSE decision seam:** a lens implements `evaluate(snapshot) -> LensVerdict`
  (round 0, independent — attribution scores this) and optionally
  `_deliberate(...)` (round 1, sees peers). `nse/council.py` resolves. There is
  no aggregator; the weighted vote was deleted after measuring as a dead end.
- **Execution:** `core/execution/crypto_runner.py` (Delta),
  `nse/execution/options_runner.py` (NIFTY council, brain tier),
  `nse/execution/nse_runner.py` (legacy Angel). Brokers:
  `core/brokers/delta_crypto.py`, `nse/broker/angel_broker.py`.
- **Angel order placement requires a whitelisted static IP**; market data does
  not. Order placement belongs on the VPS sentinel only — never localhost.
  `options_runner.test_no_order_imports()` asserts the brain tier imports no
  broker code; keep it passing.
- **A module that reads env vars loads them.** `core/mongo.py` calls
  `load_dotenv()` itself. Relying on someone having imported root `config.py`
  first silently disabled persistence for every `nse/` entry point.

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
