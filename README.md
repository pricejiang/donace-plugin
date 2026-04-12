# Donace Plugin

Generator-Evaluator agent harness for Claude Code. Sprint-based development workflow with cross-model review (Claude + Codex) and knowledge persistence.

## Agents

| Agent | Model | Role |
|---|---|---|
| **team-lead** | opus | Orchestrator — coordinates the full workflow |
| **planner** | opus | Expands brief into product spec |
| **architect** | opus | Designs staged implementation plan |
| **implementer** | sonnet | Writes production code following the plan |
| **runtime-evaluator** | opus | Runtime verification via Playwright / Xcode Simulator / curl |
| **test-engineer** | sonnet | Writes and runs unit tests |
| **ios-reviewer** | opus | Deep iOS/Swift code review |
| **typescript-reviewer** | opus | Deep TypeScript/React code review |
| **ui-designer** | opus | On-demand UI/UX design specs |

## Workflow

```
Phase 0: Boot (restore context from .ai/sessions/ and .ai/cards/)
Phase 1: Planning (planner → architect)
Phase 2: Sprint Loop
  ├── Sprint contract (runtime-evaluator)
  ├── Implement (implementer)
  ├── Verify (test-engineer + Codex review + runtime-evaluator)
  └── Fix loop (max 3 cycles)
Phase 3: Completion (final Claude review + session log + knowledge cards)
```

## Install as Plugin

```bash
/plugin marketplace add pricejiang/donace-plugin
/plugin install donace@pricejiang-donace-plugin
```

Then use agents with the `donace:` prefix:

```bash
claude --agent donace:team-lead
```

## Install via Symlink (for contributors)

```bash
git clone https://github.com/pricejiang/donace-plugin.git
ln -sf $(pwd)/donace-plugin/agents/*.md ~/.claude/agents/
```

Then use agents directly:

```bash
claude --agent team-lead
```

## Token Optimization

The harness includes several mechanisms to reduce LLM token consumption while preserving correctness. Three complementary strategies work together: shadow context auditing, fast-path agent shortcuts, and CLI tooling for inspection.

### Shadow Compact-Context

When an agent compacts its context, the harness runs a *shadow audit* in parallel — a lightweight re-read of the same context window that measures how many tokens were used, how many were trimmed, and what sections were kept vs. dropped. This audit never blocks the main agent path; it records `context.audit` events that `validate_run` picks up in post-run analysis.

- Implementation: `sdk/context_budget.py`
- Event type emitted: `context.audit` (fields: `stage`, `consumer`, `full_tokens`, `compact_tokens`, `reduction_tokens`, `reduction_pct`, `kept_sections`, `dropped_sections`)
- Validator check: `sdk/run_validator.py` Check 11 (`context_shadow_audit`) aggregates all audit events into the `context_audits` list in `RunReport` and surfaces per-stage reduction metrics as INFO items

### Fast-Path Agents

Certain agents — **documenter** and **test-engineer** — can skip the Claude API entirely when their work is purely mechanical (e.g., writing docs from a fixed template, or regenerating a test suite from a diff that is already in context). The harness signals this by emitting `agent.started` with `model="local-fast-path"`. No LLM call is made; the agent runs a local code path instead.

Detection happens at two levels:

1. **Explicit signal** — `agent.started` event carries `model="local-fast-path"` in its payload.
2. **Zero-token heuristic** — `agent.completed` arrives with `tokens_used == 0` for an agent type that normally consumes tokens; validator treats this as an implicit fast-path.

The validator (`sdk/run_validator.py`) accumulates these events in **Check 12** (`fast_path_savings`) and emits an INFO-level report item. The resulting `RunReport.to_dict()` includes:

```json
"fast_path_summary": {
  "total_invocations": 2,
  "agents": [
    {"agent": "documenter", "stage": "docs", "est_tokens_saved": 2000},
    {"agent": "test-engineer", "stage": "unit-tests", "est_tokens_saved": 2000}
  ],
  "total_est_tokens_saved": 4000
}
```

`total_est_tokens_saved` is a conservative estimate based on average prompt sizes for each agent type; actual savings depend on stage complexity.

### CLI Tools

**Token audit** — models the token budget for every stage in a plan before the run starts:

```bash
python -m sdk.token_audit path/to/plan.md
python -m sdk.token_audit --doc-only-stages Stage1,Stage2 path/to/plan.md
```

Prints a table of per-stage token estimates (implementer prompt, test-engineer prompt, doc overhead) and a `fast_path_savings` section showing which stages qualify for local fast-path execution and the projected savings. Pass `--doc-only-stages stage1,stage2` to mark stages explicitly as documentation-only. The `fast_path_savings` field in `AuditReport.to_dict()` contains keys `stages` (list), `total_est_tokens_saved` (int), and `stage_count` (int).

**Performance benchmark** — measures Python-side overhead of prompt context assembly:

```bash
python -m sdk.perf_validate
python -m sdk.perf_validate --stages 10 --iterations 200
```

Reports mean/p95/max assembly latency and estimated token counts for full-context, compact-stage, and compact-wrap strategies. Use this to verify that token optimizations do not add measurable Python-side overhead. For per-run diagnostics, call `validate_run` from `sdk/run_validator.py` directly with the run's event stream.

## Key Design Decisions

- **Generator-Evaluator pattern** — inspired by [Anthropic's harness design blog](https://www.anthropic.com/engineering/harness-design-long-running-apps)
- **Cross-model review** — Codex reviews Claude's code every sprint via `/codex:review`
- **Claude reviewer at ship time** — deep stack-specific review runs once at the end, not every sprint (saves tokens)
- **Knowledge persistence** — `.ai/cards/` for reusable insights, `.ai/sessions/` for session logs
- **Ralph Loop compatible** — resumption check skips re-planning on restart

## License

MIT
