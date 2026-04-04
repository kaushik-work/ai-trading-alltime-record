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
