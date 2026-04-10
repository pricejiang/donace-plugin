# Parallel Implementation Design

Current orchestrator runs one implementer per stage, stages run sequentially. This doc explores three approaches to parallelism.

## Current Flow (Serial)

```
Stage 1: implementer → verify → fix
Stage 2: implementer → verify → fix    ← waits for Stage 1
Stage 3: implementer → verify → fix    ← waits for Stage 2
```

Total time = sum of all stage times. A 5-stage plan where each stage takes 5 minutes = 25 minutes of implementation alone.

## Problem

Single-threaded implementation is the bottleneck. Verification already runs in parallel (test-engineer + codex + runtime-evaluator). But the implementer — the longest step — always runs alone.

Two independent dimensions of parallelism:

| Dimension | What runs in parallel | Who decides | Example |
|---|---|---|---|
| **Stage-level** | Multiple stages at once | Orchestrator (Python) | Stage 1 (auth guard) + Stage 2 (API routes) run simultaneously |
| **File-level** | Multiple files within a stage | Implementer (LLM) | Within Stage 3: tests/auth.test.ts + tests/routes.test.ts written simultaneously |

## Approach A: File-Level Parallelism (Implementer Self-Organizes)

The implementer agent spawns sub-agents for independent file changes within a single stage.

### How It Works

```
orchestrator
  └─ implementer (lead) — receives stage task
       ├─ analyzes: "3 files to change, auth-guard.ts and api/routes.ts are independent, tests depend on both"
       ├─ spawn sub-implementer-1: "Implement auth-guard.ts changes"
       ├─ spawn sub-implementer-2: "Implement api/routes.ts changes"
       ├─ wait for both
       ├─ spawn sub-implementer-3: "Write tests for auth-guard + routes" (depends on 1, 2)
       └─ return combined result
```

### Changes Required

Uses Claude Code's native [subagent](https://code.claude.com/docs/en/sub-agents) mechanism. The implementer session discovers agent definitions from the `agents/` directory automatically — no Python code changes.

**1. `agents/sub-implementer.md`** (new file):

```markdown
---
name: sub-implementer
description: Implement a specific, self-contained file change within a larger stage. Receives exact file paths and clear success criteria from the lead implementer.
tools: ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]
model: sonnet
---

# Sub-Implementer

Focused developer handling one piece of a larger implementation stage.
The lead implementer has analyzed the full stage and delegated this specific slice.

## Rules
- Only modify the files specified in your brief — do not touch other files
- Match existing code style exactly
- If you discover a dependency on a file being changed in parallel, report it back instead of modifying that file
- Do not commit — the lead handles commits after integrating all sub-agent work
- Do not run the full test suite — the lead handles that after integration
```

**2. `agents/implementer.md`** (update):

- Add `Agent` to the `tools` frontmatter list
- Add a "Parallel Execution" section to the prompt body (see below)

**3. No changes to `orchestrator.py`, `sprint_loop.py`, or `agent_dispatch.py`.**

The Agent SDK dispatches implementer as a full Claude Code session. That session inherits the project's `agents/` directory and can spawn `sub-implementer` subagents via the `Agent` tool natively. The orchestrator doesn't know or care whether the implementer parallelizes internally.

### Trade-offs

| Pro | Con |
|---|---|
| Zero orchestrator changes | LLM decides what's independent — may get it wrong |
| Implementer has full context to judge dependencies | Sub-agent coordination overhead (context passing) |
| Works within existing stage/verify/fix loop | Merge conflicts possible if sub-agents touch overlapping code |
| Incremental — can enable/disable per agent config | Token cost increases (multiple agent sessions) |

### Risk: Conflicting Edits

Two sub-implementers editing the same file or interdependent files. Mitigations:
- Implementer lead analyzes dependencies before spawning
- Sub-implementers operate on disjoint file sets
- Lead implementer does a final integration pass after sub-agents finish
- If conflict detected, fall back to serial execution

### When It Helps

- Stages with 3+ independent files to modify
- Large stages (e.g., "update all API route handlers")
- Test writing (different test files are naturally independent)

### When It Doesn't Help

- Single-file changes
- Tightly coupled changes (new type + all usages)
- Small stages where parallelism overhead exceeds time saved

### Dashboard Visibility

Subagents run inside the implementer's Claude Code session. Our EventBus hooks are registered on the implementer session only — subagent internal activity (file edits, bash commands) is invisible to the dashboard by default.

**What the official SDK provides:**

| Mechanism | Available to us? | What it gives |
|---|---|---|
| `/agents` Running tab — live subagent status | No — CLI UI only, not available via SDK | |
| `color` frontmatter — color in task list | No — CLI UI only | |
| `SubagentStart` hook — fires when subagent begins | **Yes** — fields: `agent_id`, `agent_type`, `cwd` | |
| `SubagentStop` hook — fires when subagent completes | **Yes** — fields: `agent_id`, `agent_type`, `agent_transcript_path` | |
| `agent_transcript_path` — full subagent transcript file | **Yes** — readable after subagent stops | |

There is no official external dashboard for subagent visualization. The SDK gives us lifecycle hooks and transcript files.

**Implementation: two-phase visibility**

**Phase 1: Lifecycle events** — add `SubagentStart`/`SubagentStop` hooks to the implementer's hook config:

```python
# In _make_hooks(), add:
"SubagentStart": [HookMatcher(matcher=".*", hooks=[subagent_start_hook])],
"SubagentStop": [HookMatcher(matcher=".*", hooks=[subagent_stop_hook])],
```

New event types in `events.py`:

```python
@dataclass
class SubagentStarted(Event):
    parent_agent: str = ""     # "implementer"
    subagent_type: str = ""    # "sub-implementer"
    subagent_id: str = ""

@dataclass
class SubagentCompleted(Event):
    parent_agent: str = ""
    subagent_type: str = ""
    subagent_id: str = ""
    transcript_path: str = ""
```

Dashboard shows:

```
implementer          running 45s
  ├─ sub-implementer    running 20s
  ├─ sub-implementer    running 18s
  └─ sub-implementer    waiting
```

**Phase 2: Transcript backfill** — when `SubagentStop` fires, read `agent_transcript_path` and extract key events (file edits, bash commands). Emit them as backfilled events to the dashboard:

```python
async def subagent_stop_hook(hook_input, tool_use_id, context):
    transcript = Path(hook_input.agent_transcript_path).read_text()
    # Parse transcript for tool_use entries
    for tool_call in parse_transcript_tools(transcript):
        await bus.emit(AgentToolUse(
            agent=f"{agent_name}/sub-implementer",
            tool=tool_call.tool,
            target=tool_call.target,
        ))
```

Dashboard shows (after subagent completes):

```
implementer               running 45s
  ├─ sub-implementer (1)    completed 20s
  │    ├─ Edit src/auth-guard.ts
  │    └─ Edit src/auth.test.ts
  ├─ sub-implementer (2)    completed 18s
  │    └─ Edit src/routes.ts
  └─ sub-implementer (3)    running 12s
```

Events arrive with a delay (only after each subagent completes), but information is complete. This is acceptable because the primary purpose is observability, not real-time control — subagents don't have checkpoints.

## Approach B: Stage-Level Parallelism (Orchestrator Schedules)

Orchestrator identifies independent stages and runs them concurrently.

### How It Works

Architect annotates stage dependencies in the plan:

```markdown
## Stage 1: Auth Guard
**Dependencies**: None

## Stage 2: API Route Handlers  
**Dependencies**: None

## Stage 3: Integration Tests
**Dependencies**: Stage 1, Stage 2

## Stage 4: Frontend Error Handling
**Dependencies**: None
```

Orchestrator builds a dependency graph and executes:

```
Time ──────────────────────────────────────────►

Wave 1 (parallel):
  ├─ Stage 1: Auth Guard          ████████░░░░ verify ✓
  ├─ Stage 2: API Route Handlers  ██████████░░ verify ✓
  └─ Stage 4: Frontend Errors     ██████░░░░░░ verify ✓

Wave 2 (after wave 1 completes):
  └─ Stage 3: Integration Tests   ████████░░░░ verify ✓
```

### Changes Required

**events.py** — extend Stage dataclass:

```python
@dataclass
class Stage:
    name: str
    has_user_facing_changes: bool
    depends_on: list[str] = field(default_factory=list)  # stage names
```

**orchestrator.py** — update plan parser:

```python
def _parse_plan_stages(content: str) -> list[Stage]:
    # Also parse: **Dependencies**: Stage 1, Stage 2
    deps_pattern = re.compile(
        r"\*\*Dependencies?\*\*:\s*(.+)", re.IGNORECASE
    )
    # "None" → [], "Stage 1, Stage 2" → ["Stage 1", "Stage 2"]
```

**sprint_loop.py** — replace sequential loop with wave-based execution:

```python
async def run_sprint_loop(stages, cwd, bus, ...):
    completed: set[str] = set()
    results: list[StageResult] = []
    remaining = list(stages)

    while remaining:
        # Find stages whose dependencies are all completed
        ready = [s for s in remaining if all(d in completed for d in s.depends_on)]

        if not ready:
            # Circular dependency or missing stage — break
            for s in remaining:
                results.append(StageResult(name=s.name, status="BLOCKED", ...))
            break

        # Run all ready stages in parallel
        wave_tasks = [
            run_single_stage(stage, cwd, bus, query, ...)
            for stage in ready
        ]
        wave_results = await asyncio.gather(*wave_tasks, return_exceptions=True)

        for stage, result in zip(ready, wave_results):
            if isinstance(result, Exception):
                results.append(StageResult(name=stage.name, status="BLOCKED", ...))
            else:
                results.append(result)
                if result.status == "PASS":
                    completed.add(stage.name)
            remaining.remove(stage)

    return SprintResult(stages=results, ...)
```

**agents/architect.md** — update plan format to include dependencies:

```markdown
## Stage N: [Name]
**Goal**: ...
**Dependencies**: None | Stage X, Stage Y
**Has user-facing changes**: Yes/No
...
```

### Trade-offs

| Pro | Con |
|---|---|
| Significant speedup — independent stages don't wait | Requires architect to correctly identify dependencies |
| Deterministic — Python controls the graph, not LLM | More complex sprint_loop (wave scheduling) |
| Dashboard can show parallel execution visually | Architect must correctly assign disjoint files per stage |
| MUST_STOP on one stage doesn't block independent stages | Unified verify sees combined diff — harder to attribute failures to a specific stage |

### Risk: Git Conflicts

Multiple implementers writing to the same repo simultaneously. Mitigation:
- **Architect ensures disjoint files**: Stage Sizing rules require different stages to touch different files. No technical enforcement — relies on architect quality.

~~Git worktrees~~ — not needed. The actual implementation uses **parallel implement → unified verify**: all implementers finish before any verification runs, so there's no concurrent read/write on the same codebase state. Worktrees would add complexity (create, merge, conflict resolution, cleanup) for no benefit.

### Risk: Resource Exhaustion

The actual implementation runs N parallel implementers + 1 unified verify (not N × full pipeline). Resource usage:
- N implementer sessions (parallel)
- 1 test-engineer + 1 codex review + 0-1 runtime-evaluator (sequential after all implements)
- 1 fix loop implementer (if needed)
- Token budget tracking per run

### When It Helps

- Plans with 4+ stages where at least 2 are independent
- Feature work that touches multiple unrelated subsystems
- Frontend + backend changes that don't share interfaces

### When It Doesn't Help

- Linear dependency chains (each stage depends on the previous)
- Small plans (2-3 stages, serial is fast enough)
- Tightly coupled codebase where every change affects everything

## Approach C: Both (Recommended End State)

Stage-level parallelism (B) at the orchestrator layer, file-level parallelism (A) at the implementer layer. Independent concerns, composed naturally.

```
orchestrator (wave scheduler)
  │
  ├─ Wave 1 (parallel stages):
  │   ├─ Stage 1 implementer (lead)
  │   │    ├─ sub-implementer: auth-guard.ts
  │   │    └─ sub-implementer: auth.test.ts
  │   └─ Stage 4 implementer (lead)
  │        └─ (single file, no sub-agents needed)
  │
  └─ Wave 2 (depends on Wave 1):
      └─ Stage 3 implementer (lead)
           ├─ sub-implementer: integration-auth.test.ts
           └─ sub-implementer: integration-routes.test.ts
```

### Implementation Order

1. **Phase 1: Approach A only** — enable sub-agents in implementer. No orchestrator changes. Immediate benefit for large stages.

2. **Phase 2: Approach B** — add dependency tracking to Stage, wave scheduler to sprint_loop. Requires architect prompt update and plan parser changes.

3. ~~**Phase 3: Git worktrees**~~ — not needed. Parallel implement → unified verify means no concurrent writes.

4. **Phase 3: Resource management** — token budget tracking, max parallelism caps.

### Status

Both A and B are implemented. The wave scheduler uses parallel implement → unified verify, eliminating the need for worktrees and reducing token cost compared to full-pipeline-per-stage parallelism.

## Open Questions

1. **Checkpoint behavior with parallel stages** — if the dashboard shows 3 stages running, what do checkpoint buttons control? Per-stage or per-wave?

2. **MUST_STOP propagation** — if Stage 1 blocks, should parallel Stage 2 continue or be cancelled? Depends on whether Stage 2's output is useful without Stage 1.

3. **Token budget** — parallel execution multiplies token usage. Should there be a max-concurrent-agents or max-cost-per-run limit?

4. **Dashboard visualization** — parallel stages need a different visual layout. Timeline/Gantt view instead of linear list?

5. **Verification resource contention** — multiple stages hitting codex review simultaneously. Queue or parallel?
