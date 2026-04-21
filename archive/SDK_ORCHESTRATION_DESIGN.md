# SDK Orchestration Design

## Problem

`team-lead` agent's sprint loop is controlled by LLM reading markdown instructions. Steps like codex review, test-engineer, and runtime-evaluator are frequently skipped because LLM "decides" they aren't needed. This is a reliability problem — prompt-based flow control is inherently "advisory", not "mandatory".

## Solution

Move Phase 2 (Sprint Loop) from LLM orchestration to Python code using Claude Agent SDK. Agent definitions stay as markdown. Only the scheduling logic becomes code.

## Architecture

```
team-lead.md (LLM)                    sprint_loop.py (Agent SDK)
┌──────────────────────┐              ┌──────────────────────────────┐
│ Phase 0: Boot        │              │                              │
│ - restore context    │              │  for stage in plan:          │
│ - load cards         │              │    ① runtime-evaluator       │
│ - check resume       │              │       → sprint contract      │
│                      │              │    ② implementer             │
│ Phase 1: Plan        │              │       → write code           │
│ - classify task      │              │    ③ verify (parallel):      │
│ - planner (optional) │  Bash call   │       test-engineer    ✓     │
│ - architect          │ ───────────→ │       codex review     ✓     │
│ - codex plan review  │              │       runtime-eval     ?     │
│                      │              │    ④ fix loop (max 3)        │
│ Phase 2: Sprint Loop │              │    ⑤ gate check              │
│ (delegates to SDK)   │              │                              │
│                      │ ←─────────── │  returns JSON result         │
│ Phase 3: Wrap        │              │                              │
│ - final review       │              └──────────────────────────────┘
│ - session log        │
│ - knowledge cards    │
│ - report             │
└──────────────────────┘
```

## Phase Ownership

| Phase | Owner | Why |
|-------|-------|-----|
| Phase 0: Boot | LLM | Reading session logs, loading knowledge cards, judging whether to resume — all require understanding and judgment |
| Phase 1: Plan | LLM | Task classification, deciding whether to skip planner, invoking architect — needs flexible judgment. Codex plan review added as mandatory step |
| Phase 2: Sprint Loop | **SDK (Python)** | Fixed workflow, every step must run, parallel dispatch, retry control — needs enforced execution |
| Phase 3: Wrap | LLM | Final review, session log, knowledge card extraction — all summarization and creative work |

## File Structure

```
donace/
  agents/
    team-lead.md          # Modified: Phase 2 calls sprint_loop.py
    ...other agents unchanged...
  sdk/
    sprint_loop.py        # Agent SDK orchestration
    requirements.txt      # claude-agent-sdk
```

## Interface: team-lead.md → sprint_loop.py

**Invocation:**

```bash
python sdk/sprint_loop.py \
  --plan .ai/plans/current-plan.md \
  --cwd /path/to/project
```

**Input:** Reads the plan file to extract stages.

**Output:** JSON to stdout, structured as:

```json
{
  "stages": [
    {
      "name": "Stage 1: WebSocket Server",
      "status": "PASS",
      "contract": "...",
      "test_result": { "passed": 12, "failed": 0 },
      "codex_result": { "status": "completed", "p1_findings": 0, "findings": [] },
      "runtime_result": { "status": "PASS", "score": "5/5" },
      "fix_attempts": 0
    },
    {
      "name": "Stage 2: Broadcasting",
      "status": "BLOCKED",
      "contract": "...",
      "test_result": { "passed": 8, "failed": 2 },
      "codex_result": { "status": "completed", "p1_findings": 1, "findings": ["..."] },
      "runtime_result": { "status": "FAIL", "score": "3/5" },
      "fix_attempts": 3,
      "unresolved": ["test: broadcast to disconnected client throws unhandled error"],
      "recommendation": "MUST_STOP"
    }
  ],
  "warnings": ["Codex review SKIPPED for Stage 3 — CLI not available"],
  "summary": { "passed": 1, "blocked": 1, "total": 2 }
}
```

team-lead LLM reads this JSON and produces the Phase 3 report.

## Sprint Loop Logic (sprint_loop.py)

### Per-stage flow

```python
for stage in plan.stages:
    # 1. Sprint contract (mandatory)
    contract = await query(
        agent="runtime-evaluator",
        prompt=f"Write sprint contract for: {stage}"
    )

    # 2. Implement (mandatory)
    impl_result = await query(
        agent="implementer",
        prompt=f"Implement: {stage}\nContract: {contract}"
    )

    # 3. Verify (parallel, all mandatory)
    test_result, codex_result = await asyncio.gather(
        run_test_engineer(stage),
        run_codex_review()
    )

    # runtime-evaluator: only if stage has user-facing changes
    runtime_result = None
    if stage.has_user_facing_changes:
        runtime_result = await run_runtime_evaluator(contract)

    # 4. Fix loop (max 3 attempts)
    failures = collect_failures(test_result, codex_result, runtime_result)
    for attempt in range(3):
        if not failures:
            break
        fix_result = await query(
            agent="implementer",
            prompt=f"Fix: {failures}"
        )
        test_result, codex_result = await asyncio.gather(
            run_test_engineer(stage),
            run_codex_review()
        )
        failures = collect_failures(test_result, codex_result, runtime_result)

    # 5. Gate check
    if failures:
        if all(f.severity == "warning" for f in failures):
            stage_result.recommendation = "SKIP_ALLOWED"
        else:
            stage_result.recommendation = "MUST_STOP"
            break  # Stop sprint loop, return to team-lead
```

### Codex review handling

Codex review is mandatory, but the CLI may be unavailable. Handle gracefully:

| Situation | Action |
|-----------|--------|
| CLI not installed / auth failed | Pre-check at startup. If unavailable, warn but continue. Record `SKIPPED` in output |
| Timeout / network error | Retry once. Still fails → record `SKIPPED` with reason |
| Returns empty | Treat as PASS (no findings) |
| Returns P1 findings | Add to failure list for fix loop |

The key guarantee: **skipped steps are never silent**. They always appear in the JSON output, and team-lead LLM must include them in the report.

### BLOCKED handling

When fix loop exhausts 3 attempts:

| Failure severity | Recommendation | What team-lead does |
|-----------------|----------------|-------------------|
| All warnings | `SKIP_ALLOWED` | LLM may skip to next stage, but must note in report |
| Any critical/error | `MUST_STOP` | sprint_loop.py stops. LLM reports to user with failure details |

`MUST_STOP` means the Python script stops iterating stages and returns. team-lead LLM cannot pretend it passed — the JSON output makes the failure explicit.

## Phase 1 Addition: Codex Plan Review

Added as a mandatory step in Phase 1, before handing off to Phase 2:

```
Phase 1 flow:
  ① planner → spec (skipped for bug fixes)
  ② architect → plan
  ③ codex review on the plan text (NEW — mandatory)
  ④ if major issues → architect revises plan → re-review
  ⑤ plan approved → hand off to sprint_loop.py
```

This stays in LLM because:
- Plan review feedback requires understanding and judgment to act on
- It runs once per task, not per-stage (low risk of being skipped repeatedly)
- The revision loop (architect adjusting based on feedback) needs LLM flexibility

team-lead.md will be updated to make this step explicitly mandatory and require logging the review result in the session log.

## Agent Model Assignments

Each agent in sprint_loop.py uses its own model, matching the markdown frontmatter:

| Agent | Model | Rationale |
|-------|-------|-----------|
| runtime-evaluator | opus | Needs judgment for contract negotiation and verification |
| implementer | sonnet | Code writing — fast and capable enough |
| test-engineer | sonnet | Test writing — fast and capable enough |
| codex review | codex (external) | Independent cross-model review |

## What Changes vs What Stays

| Component | Changes? | Details |
|-----------|----------|---------|
| agents/team-lead.md | **Yes** | Phase 2 replaced with `Bash: python sdk/sprint_loop.py ...` |
| agents/architect.md | No | |
| agents/planner.md | No | |
| agents/implementer.md | No | |
| agents/runtime-evaluator.md | No | |
| agents/test-engineer.md | No | |
| agents/typescript-reviewer.md | No | |
| agents/ios-reviewer.md | No | |
| sdk/sprint_loop.py | **New** | Agent SDK orchestration script |
| sdk/requirements.txt | **New** | `claude-agent-sdk` |

## Open Questions

1. **Agent definition loading**: Should sprint_loop.py read the markdown agent files to use as system prompts, or define agents inline? Reading markdown keeps a single source of truth but adds file parsing.

2. **Codex CLI path**: How to locate the codex binary reliably across environments? Need to detect or make configurable.

3. **Plan parsing**: How to extract stages from `.ai/plans/current-plan.md`? Regex on markdown headers, or require a structured frontmatter section?

4. **Cost tracking**: Should sprint_loop.py track and report token usage per agent per stage?
