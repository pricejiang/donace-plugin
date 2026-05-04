# Spec: donace simplify

**Status**: draft
**Branch**: `simplify` (branched from `main`; complexity reduction is measured against `orchestration`)
**Date**: 2026-05-02

## Why

donace has accumulated layers of complexity where each layer was added to patch a symptom of the previous: team-lead subagent → SIGKILL on idle → DETACH plan; pre-hoc file_scope hooks → false positives → repeated normalization fixes; codex review failures → new "warning" severity; plan revisions → AWAIT_APPROVAL state → auto-fix → re-review. Recent run history: not a single end-to-end clean run.

Strip back to actual user need: **one tool that orchestrates Claude and Codex through plan / execute / review, with each stage's worker selected per-stage.**

## Goals (v0)

1. Three skills: `/donace:chat`, `/donace:plan`, `/donace:execute`.
2. Per-stage `implementer:` tag in plan; planner picks based on stage character.
3. Reviewer = the OPPOSITE model from implementer (claude impl → codex review; codex impl → claude review).
4. Reviewer outputs `[P0]` / `[P1]` / `[P2]` markers; P0 gates with up to 2 retries; P1/P2 advisory in report.
5. Main LLM (user's Claude Code session) IS the orchestrator — no team-lead subagent.
6. All long-running calls dispatch as background, main LLM stays interactive throughout — user can chat / clarify / interrupt while stages run.

## Non-goals (v0)

- Parallel stage execution (worktree per stage) — v1.
- Unattended overnight runs (survive Claude Code session close) — v2.
- Hook-enforced file scope, mass-mutator ban, subagent type restrictions — only cwd boundary + command blocklist remain.
- `documenter` agent / `verify` wrap phase / `runtime-verifier` / `code-reviewer` (replaced by simpler `reviewer`).
- Plan revision auto-fix via codex (`run_codex_plan_fix`) and the AWAIT_APPROVAL / REVIEW state machine.
- Idle-heartbeat watchdog, three-strike repeat-failure detection.
- Dashboard / web UI / SQLite history / WebSocket events — terminal only.
- Multi-state plan-job machine — plans are just files; if you don't like one, edit it.
- Resume after Claude Code session close.

## Architecture

```
Main LLM (user's Claude Code terminal) IS the team-lead.

  /donace:chat                  ← user types this
      │
      └─ brainstorm with user, write spec.md to .ai/runs/<id>/

  /donace:plan <run-id>         ← user types this
      │
      └─ dispatch planner subagent to read spec.md and write plan.md

  /donace:execute <run-id>      ← user types this
      │
      └─ for each stage in plan.md, in order:
          ├─ dispatch implementer (background)
          │   ├─ if claude: Agent(subagent=implementer, run_in_background=true)
          │   └─ if codex:  Bash(python sdk/codex_call.py implement ..., run_in_background=true)
          ├─ wait for completion (notification or BashOutput poll)
          ├─ run listed test commands
          │   └─ if any fail: feed failures to implementer + retry
          ├─ dispatch reviewer (opposite model, same background pattern)
          ├─ parse review for [P0] / [P1] / [P2]:
          │   ├─ has P0:   feed P0 to implementer + retry (≤2 total retries)
          │   └─ no P0:    git commit reviewed stage diff
          └─ next stage
```

Critical properties:
- **Main LLM never blocks on a worker.** Both Agent and Bash use `run_in_background=true`, so the user can still ask "where are we?", "kill stage 3", or "show me the latest review" while work is running.
- **`/donace:execute` is re-entrant.** If the current Claude Code session stays alive, main LLM can poll and continue the loop. If the session ends or the user interrupts, re-running `/donace:execute <id>` resumes from the first non-passed stage.
- **v0 uses one shared worktree.** To keep stage diffs and auto-commits trustworthy without reintroducing hook machinery or worktrees, execute requires a clean worktree at start and treats that worktree as reserved for the run until stopped.
- **`interrupted` and `blocked` are different states.** `interrupted` means the stage stopped but its current worktree is intentionally preserved for resume (for example: rate limit, user-requested stop). `blocked` means the run cannot safely continue without user action or a new attempt strategy.

## Components

| File | Type | Lines (est) | Purpose |
|---|---|---|---|
| `skills/chat/SKILL.md` | markdown | 80 | Brainstorm spec.md from user |
| `skills/plan/SKILL.md` | markdown | 100 | spec.md → plan.md with implementer tags |
| `skills/execute/SKILL.md` | markdown | 200 | The execute loop |
| `agents/planner.md` | markdown | 100 | Claude subagent; reads spec.md + emits plan.md with stages tagged `implementer:` per heuristic |
| `agents/implementer.md` | markdown | 80 | Claude implementer; self-directed, codebase-first execution with TDD encouraged |
| `agents/reviewer.md` | markdown | 60 | Claude reviewer; receives an injected stack checklist + outputs [P0]/[P1]/[P2] markers |
| `agents/references/review-checklist-python.md` | markdown | 60 | Stack-specific items reviewer looks for in Python diffs |
| `agents/references/review-checklist-typescript.md` | markdown | 60 | TypeScript / JS items |
| `agents/references/review-checklist-ios.md` | markdown | 60 | Swift / Objective-C / iOS items |
| `agents/references/review-checklist-general.md` | markdown | 40 | Stack-agnostic catchall |
| `agents/prompts/codex-implementer.md` | markdown | 50 | Prompt prefix for codex-as-implementer |
| `agents/prompts/codex-reviewer.md` | markdown | 50 | Prompt prefix for codex-as-reviewer |
| `sdk/codex_call.py` | python | ~200 | Subprocess wrapper around codex-companion (`task --background --json` + status + result) |
| `sdk/cli.py` | python | ~150 | CLI: `donace run_start`, `donace list_runs`, `donace stage_status` (with ASCII pipeline view per the section below) |
| `sdk/tests/test_codex_call.py` | python | ~150 | Unit tests with mocked subprocess |
| `sdk/tests/test_plan_parser.py` | python | ~80 | Plan-format parser tests (in `cli.py`) |
| `sdk/tests/test_run_start.py` | python | ~60 | Run-id creation and `.ai/runs/<id>/` skeleton tests |
| `sdk/tests/test_execute_test_commands.py` | python | ~100 | Stage `tests:` command parsing, skipping, persistence, and failure handling tests |
| `sdk/tests/test_stage_status_render.py` | python | ~80 | ASCII pipeline view rendering: header line, glyph mapping, state-summary phrasing, tail extraction, terminal-width handling |
| `.claude-plugin/plugin.json` | json | ~30 | Plugin metadata paired with skill wiring and README so `/donace:chat`, `/donace:plan`, `/donace:execute` are the public interface |
| `CLAUDE.md` | markdown | ~100 | Project contract (create or rewrite; this branch may not currently have one) |

Total new code: ~1730 lines. ~660 of that is Python (codex_call + cli + tests); rest is markdown (skills, agents, references, contract).

## Public interface / packaging

The public user interface for donace v0 is:
- `/donace:chat`
- `/donace:plan`
- `/donace:execute`

Packaging implications:
- `skills/chat/`, `skills/plan/`, and `skills/execute/` are the primary entrypoints a user sees after install.
- `.claude-plugin/plugin.json`, README install docs, and contributor symlink instructions must all expose those three skills as the canonical interface.
- Agent files remain internal building blocks for the skills. Direct `claude --agent ...` usage is contributor/debugging surface, not the product surface.

## Concrete file actions on top of `main`

The implementation branch starts from `main`, which ships 11 agent files. The simplify pipeline needs 3 core pipeline agents (`planner`, `implementer`, `reviewer`); the rest split between "ad-hoc tools we leave alone" and "deleted because they're vestigial." The simplification claim itself is relative to `orchestration`, the last full harness branch.

| File on `main` | Action | Reason |
|---|---|---|
| `agents/architect.md` | DELETE | Its plan-writing role is replaced by the `/donace:plan` skill dispatching the rewritten `planner` subagent |
| `agents/implementer.md` | REWRITE | Per spec: self-directed implementation with TDD encouraged; keep frontmatter, replace body |
| `agents/ios-reviewer.md` | DELETE | Replaced by single `reviewer.md` + `references/review-checklist-ios.md` (orchestration's `code-reviewer + references/` pattern) |
| `agents/planner.md` | REWRITE | Subagent that writes the implementation plan (stages with `files:`, `success criteria:`, `tests:`, `implementer:` tag) from spec.md. Brainstorm/spec-writing stays between user and main LLM in `/donace:chat`; planner runs only in `/donace:plan`. Both frontmatter and body are rewritten because the current planner targets product-spec writing and lacks the required tool set |
| `agents/qa.md` | KEEP UNCHANGED | Standalone ad-hoc tool, not part of donace pipeline |
| `agents/runtime-evaluator.md` | DELETE | No runtime verification in v0 |
| `agents/team-lead.md` | DELETE | Main LLM IS team-lead in new design |
| `agents/templates/card.md` | DELETE | No card system in v0 |
| `agents/test-engineer.md` | DELETE | TDD is implementer's responsibility (prompt-only) |
| `agents/typescript-reviewer.md` | DELETE | Replaced by single `reviewer.md` + `references/review-checklist-typescript.md` |
| `agents/ui-designer.md` | KEEP UNCHANGED | Standalone ad-hoc tool, not part of donace pipeline |

New files added by simplify:

| File | Purpose |
|---|---|
| `agents/reviewer.md` | Single reviewer agent. Receives a stack-specific checklist via injected payload + outputs `[P0]` / `[P1]` / `[P2]` markers per the severity contract below |
| `agents/references/review-checklist-{python,typescript,ios,general}.md` | Stack-specific checklist items. Reviewer reads the matching one for the stage's stack |
| `agents/prompts/codex-implementer.md` | Prompt prefix used when implementer is `codex` |
| `agents/prompts/codex-reviewer.md` | Prompt prefix used when reviewer is `codex` |

## Plan format

```markdown
# Plan: <feature>

## Stage 1: <name>
- implementer: claude
- goal: <one sentence>
- files: src/foo.py, src/foo_test.py
- success criteria:
  - <criterion>
- tests:
  - <repo-root shell command to run>

## Stage 2: <name>
- implementer: codex
...
```

Required per stage: `implementer`, `goal`, `files`, `success criteria`, `tests`.

`tests:` is a list of concrete repo-root shell commands. If no meaningful automated test exists, the planner must still write an explicit note such as `- none: docs-only stage` or `- none: mechanical rename; existing suite already covers behavior`. The parser treats each bullet as opaque text.

`implementer:` accepts only `claude` or `codex`. Anything else → parse error.

Reviewer is implicit (opposite model). Plan does not write reviewer.

## /donace:chat skill

Invoked with no args. v0 has no chat continuity — every invocation is a fresh brainstorm with a new run-id.

Flow:
1. Mint run-id via `donace run_start`.
2. Open-ended dialogue with user about what they want to build.
3. When user signals readiness ("ok let's plan", "looks good"), write `spec.md` to `.ai/runs/<id>/`.
4. Tell user: "Spec at `.ai/runs/<id>/spec.md`. Next: `/donace:plan <id>`."

`spec.md` format: free-form prose. No required sections. Whatever the conversation converged on. The /plan skill must be flexible enough to parse this freeform prose into stages.

## /donace:plan skill

Invoked with `<run-id>`.

Flow (main LLM follows this):
1. Sanity-check: `.ai/runs/<id>/spec.md` exists.
2. Dispatch planner subagent: `Agent(subagent_type=planner, prompt="run-id: <id>; cwd: <abs>; spec.md contents below; write the implementation plan to .ai/runs/<id>/plan.md and reply with a short confirmation.", run_in_background=true)`.
3. While planner runs, main LLM is free — user can chat / clarify. If the same session is still active, main LLM can poll and continue; if not, the user can re-run `/donace:plan <id>` to check whether `plan.md` was written.
4. On completion, verify `.ai/runs/<id>/plan.md` exists and is non-empty. If not, surface the planner's text reply to the user (likely contains the failure reason) and stop.
5. Tell user: "Plan written. Edit `.ai/runs/<id>/plan.md` if needed (especially `implementer:` tags), then `/donace:execute <id>`."

Why subagent (not inline like `/donace:chat`): plan writing is a focused structured-output task that benefits from fresh context and a specialized prompt. Brainstorm needs main LLM's conversation context with the user (inline); plan writing needs isolation from that context (subagent). Two-level plan: spec.md (informal, conversational, written with user) → plan.md (structured, mechanical, written by planner).

No automatic plan review (no codex plan review gate). User reads it, edits if needed, runs.

User can hand-edit plan.md to override implementer tags or any other field.

### Planner subagent contract (`agents/planner.md`)

- **Tools**: `Read`, `Grep`, `Glob`, `Bash`, `Write`. Bash for context-gathering during planning (e.g., `git log` to see recent activity in target files, `wc -l` to size existing files, `npm test --listTests` to inventory tests, `find` with complex predicates). Write for emitting plan.md to disk directly.
- **Input** (passed in prompt): run-id, absolute cwd, spec.md contents.
- **Output**: writes `.ai/runs/<id>/plan.md` directly. Returns a short confirmation summary as the agent's text reply (e.g., "Plan written: 5 stages, 3 claude / 2 codex"). Main LLM uses the reply only for surfacing failure context if the file wasn't written.
- **Stage decomposition**: sequential, file-bounded. Each stage should land as one bisect-friendly commit.
- **`tests:` field**: planner writes runnable shell commands when possible. If no meaningful automated command exists, planner writes an explicit `none: <reason>` marker rather than leaving the field vague.
- **`implementer:` tag heuristic**:

   | Stage character | Implementer |
   |---|---|
   | Mechanical refactor / batch rename / typed transforms | codex |
   | Algorithmic / dense logic / single-file dense impl | codex |
   | Cross-file judgment / needs Claude skills / context-heavy | claude |
   | Default when uncertain | claude |

## /donace:execute skill

Invoked with `<run-id>`.

Flow:

1. Preflight:
   - **Fresh run** (no stage has started yet): require `git status --porcelain` to be empty. If the worktree is dirty, stop and tell the user to commit/stash first. v0 deliberately uses a shared worktree; clean start is the safety boundary.
   - **Resume of an `interrupted` stage**: allow the dirty worktree to remain, because those edits are presumed to be the partial progress of that interrupted stage. Before resuming, verify that the current branch/HEAD still match the interrupted stage's recorded `pre_stage_sha` baseline (that is: no new commit, branch switch, or unrelated git move happened after interruption). If that check fails, stop and mark the stage `blocked` with a reason like `resume baseline mismatch`; do not auto-reset away partial work.
   - **Resume with a stale `running` stage**: if a previous session died mid-stage and left `status.json.status == "running"`, first rewrite it to `{"status": "interrupted", "reason": "session_ended", ...}` and then follow the interrupted-resume path. Do not treat stale `running` as a fresh entry.
   - **Resume with a `blocked` stage**: do not auto-rerun it. Stop, show the blocked reason, and have the user choose whether to keep the partial work for manual fixes or explicitly discard it and restart the stage.
2. Read `.ai/runs/<id>/plan.md`, parse stages.
3. For each stage in order:
   1. Stage-entry gate:
      - `status.json.status == "passed"` → skip, advance to next stage.
      - `status.json.status == "interrupted"` → resume that stage from the current worktree; preserve `pre_stage_sha` and `retry_count`.
      - `status.json.status == "running"` → treat as stale in-flight state from a previous session; rewrite to `interrupted: session_ended`, then resume using the same `pre_stage_sha` and `retry_count`.
      - `status.json.status == "blocked"` → stop immediately and tell the user why the stage is blocked. Do not re-enter automatically.
      - No `status.json` yet → fresh entry.
   2. Establish `pre_stage_sha`: on first entry to the stage, capture `git rev-parse HEAD`. On resume from `interrupted`, read the value already in `status.json` — the preflight has already verified HEAD still matches it.
   3. Write `status.json: {status: "running", retry_count: 0, pre_stage_sha}` on first entry to the stage. On resume from `interrupted`, preserve the original `pre_stage_sha` and `retry_count`; only flip `status` back to `running`.
   4. Build implementer payload: stage block + TDD reminder.
   5. Dispatch implementer (background):
      - claude: `Agent(subagent_type=implementer, prompt=payload, run_in_background=true)`
      - codex:  `Bash("python sdk/codex_call.py implement --run-id <id> --stage-id <sid>", run_in_background=true)`
        On retries, append `--retry-context-file .ai/runs/<id>/stages/<sid>/retry-context.md`.
   6. Wait for completion (notification or BashOutput drain). If the worker reports rate-limit (codex_call.py exit 2 OR Agent surfaces a `RateLimitError`), write `status.json: {status: "interrupted", reason: "rate_limited"}`, stop the run, report to user; resume preserves partial work and picks up the same stage **without consuming a retry**. Otherwise proceed to step 7.
   7. Run the stage's `tests:` commands from repo root, skipping only bullets that begin with `none:`. Save command, exit code, and stdout/stderr snapshot to `.ai/runs/<id>/stages/<sid>/test-results.md`.
   8. If any runnable test command failed:
       - retry_count++
       - Persist the failure surface to `.ai/runs/<id>/stages/<sid>/retry-context.md`.
       - If retry_count > 2: write `status.json: {status: "blocked", reason: "tests failed"}`, stop run, report.
       - Else: feed the failing test output back to implementer and GOTO step 5.
   9. Capture diff: `git diff <pre_stage_sha>`.
      - Persist that diff to `.ai/runs/<id>/stages/<sid>/diff.patch` before review dispatch. This file is the reviewer/codex-review input artifact for the current attempt.
   10. Detect stack from diff file extensions (`.py` → python, `.ts`/`.tsx`/`.js`/`.jsx` → typescript, `.swift`/`.m`/`.mm` → ios, else → general). Read `agents/references/review-checklist-<stack>.md`.
   11. Build reviewer payload: stage block + diff + test-results + stack checklist contents + success criteria.
   12. Dispatch reviewer (OPPOSITE model, background, same pattern). Codex path: `codex_call.py review --diff-file .ai/runs/<id>/stages/<sid>/diff.patch --test-results-file .ai/runs/<id>/stages/<sid>/test-results.md --stack <name>` so the helper loads the same checklist and the same collected evidence on its side. On successful completion, save the returned markdown immediately to `.ai/runs/<id>/stages/<sid>/review.md`.
   13. Parse reviewer output for `[P0]` / `[P1]` / `[P2]` markers from `review.md`.
   14. If implementer worker errored at step 6 OR reviewer worker errored at step 12 OR review parsed at least one [P0]:
       - retry_count++
       - Persist the failure surface to `.ai/runs/<id>/stages/<sid>/retry-context.md`.
       - If retry_count > 2: write `status.json: {status: "blocked", reason}`, stop run, report.
       - Else: feed retry context (worker error message OR failing test output OR P0 findings) into implementer payload; for codex implementer, include `--retry-context-file .ai/runs/<id>/stages/<sid>/retry-context.md`; GOTO step 5.
   15. No P0, all runnable tests passed, and both workers succeeded:
       - `git add -A && git commit` of the reviewed working-tree changes from this stage.
       - Write `status.json: {status: "passed"}`.
       - The reviewer's latest output already lives at `.ai/runs/<id>/stages/<sid>/review.md` from the review step. By the time the stage passes, it contains only P1/P2 findings by definition — any P0 caused an earlier retry.
       - Next stage.
4. All stages PASS: write `meta.json: {status: "completed"}`, report to user.

**Test enforcement**: end-state, not process.
- The actual gate is `/donace:execute` step 7 (orchestrator runs the stage's `tests:` commands itself and persists `test-results.md`) plus step 8 (failed runnable command → retry).
- Implementer's prompt still asks for TDD ("write the failing test first, run it, then write minimal code to pass") as a quality recommendation — but no part of the loop relies on it. A model that writes code first and tests second is fine as long as the listed `tests:` pass at end of stage.
- The reviewer judges from `test-results.md` evidence (passed via the payload at step 11), not from inferring test quality off the diff alone.
- Stage `tests:` bullets that begin with `none:` are skipped at step 7. The reviewer is still expected to flag a `none:` placeholder as [P0] if the diff obviously introduces behavior that needed automated coverage.

**Shared-worktree contract**:
- Once `/donace:execute` starts, that repo worktree is reserved for the run.
- The user can chat, inspect status files, or interrupt the run, but making unrelated code edits in the same worktree during a running stage is unsupported in v0.
- If the user wants to edit code manually, they stop the run first, make the change, and then re-run `/donace:execute <id>`.

**Stage status semantics**:
- `running`: a worker or orchestrator step is actively in flight for the stage.
- `passed`: stage completed, committed, and will be skipped on resume.
- `interrupted`: stage stopped without exhausting its retry budget, and its current worktree should be preserved for resume. Typical causes: `rate_limited`, `user_interrupted`, session ended mid-stage.
- `blocked`: stage cannot continue safely without user intervention or a changed strategy. Typical causes: retry budget exhausted, resume-baseline mismatch, true external blocker.

### Implementer subagent contract (`agents/implementer.md`)

- **Tools**: `Read`, `Grep`, `Glob`, `Bash`, `Write`, `Edit`. Bash for running tests during impl, build/typecheck, inspecting git state. Write/Edit for code changes.
- **Input** (passed in prompt): run-id, stage-id, absolute cwd, the stage block (goal, files, success criteria, tests), an optional retry context (P0 findings or failing test output from a previous attempt).
- **Output**: writes code/tests directly to the working tree. Returns a short text summary (what was changed, which tests now pass) used by main LLM only as failure-surface context. The diff captured via `git diff <pre_stage_sha>` at step 9 is the source of truth.
- **Assumption discipline**: implementer is expected to figure things out from the codebase. Read adjacent files, inspect call sites, and choose the most boring local-consistent interpretation rather than escalating ordinary ambiguity. If multiple implementations are plausible, prefer the one that best matches existing patterns and the stage's listed files / success criteria, then state the assumption briefly in the summary.
- **Hard rules**: no re-planning (don't rewrite `plan.md`), no editing other stages' artifacts, no committing (orchestrator commits at step 15). Only treat something as a blocker when it is a genuine hard stop that cannot be inferred safely from the repo and spec (for example: missing required credential, referenced file truly absent, or an external product decision with no local precedent). In that case, fail normally and explain the blocker in the text reply; do not rely on a special protocol token.

### Reviewer subagent contract (`agents/reviewer.md`)

- **Tools**: `Read`, `Grep`, `Glob`, `Bash`. Bash is for ad-hoc spot-checks only (e.g., verify a file path cited in the diff actually exists, look at neighboring code the diff touches). **No** `Write` / `Edit`: reviewer's output goes back to main LLM as text; main LLM persists it.
- **Input** (passed in prompt): run-id, stage-id, the stage block, the diff, the `test-results.md` contents (already collected by orchestrator at step 7), the stack checklist contents, and the universal severity rubric echoed inline.
- **Output**: returns the markdown review document per the "Reviewer output format" subsection below. Main LLM parses for `### [P0]` / `### [P1]` / `### [P2]` headers at step 13 and writes the document to `.ai/runs/<id>/stages/<sid>/review.md` at step 15.
- **Hard rules**: do not re-run the stage's `tests:` commands — the orchestrator already did that at step 7 and the result is in the payload. Do not propose or write code changes; that's the implementer's job on the next retry.

**User interruption during execution**:
- "what's stage 3 doing" → main LLM reads `status.json` and `BashOutput`, reports.
- "kill stage 3" → main LLM calls KillShell on the background process; writes `status.json: {status: "interrupted", reason: "user_interrupted"}` and preserves the current worktree for resume.
- "edit plan and restart" → main LLM stops current dispatch. If the user wants to change the current stage's intended work, they decide first:
  - **keep partial work**: do nothing; resume picks it up.
  - **discard partial work**: only on explicit user choice, run `git reset --hard <pre_stage_sha>` and `git clean -fd`, then remove the stage's old evidence files (`status.json`, `retry-context.md`, `diff.patch`, `review.md`, `test-results.md`) so the next `/donace:execute <id>` starts that stage fresh.
  Then user edits plan.md and runs `/donace:execute <id>` again.

## Reviewer severity contract: P0 / P1 / P2

The reviewer agent (Claude `reviewer.md` or codex via `codex-reviewer.md` prompt prefix) must tag every finding with `[P0]`, `[P1]`, or `[P2]`. Severity drives execute-loop behavior in `/donace:execute` step 13 (parse) and step 14 (decision):

- **[P0]** gates the stage. Even one P0 sends the stage into retry (see `/donace:execute` step 14).
- **[P1]** and **[P2]** are advisory — saved to `review.md`, run continues.

### What goes where

**[P0] — must fix before stage passes**
- Stage's `success criteria` not met by the diff.
- A runnable command listed in the stage's `tests:` field fails.
- The diff changes behavior that obviously needs automated coverage, but the stage provides only `none:` placeholders or omits the necessary test updates.
- Diff introduces a bug that produces incorrect behavior in normal use.
- Security regression: secret leak, command injection, SQL injection, XSS, auth bypass, sandbox escape.
- Data corruption or data-loss path.
- Breaking change to a public API contract not specified in the spec.
- Stage `files:` listed a file but the diff doesn't actually modify it (implementer skipped a listed file).

**[P1] — advisory; should fix soon, not now**
- Code quality issue in the diff: deep nesting, unclear naming, duplication, dead code.
- Missing error handling for a plausible failure mode (not a clear bug, but worth hardening).
- Coverage gap: a code path in the diff isn't exercised by tests, even though the listed tests pass.
- Clearly suboptimal complexity (e.g., O(n²) where O(n) is the obvious choice).
- Project-convention deviation in the diff (a clear pattern in adjacent files not followed).

**[P2] — nit; flag for awareness, no obligation**
- Naming preferences.
- Comment phrasing.
- Cosmetic refactor opportunities ("you could use a list comprehension here").
- Minor convention inconsistencies in non-load-bearing places.

### Scope rule

The reviewer flags things **on the diff only**. Pre-existing issues in untouched code are out of scope for the review. If a [P0] in the diff is symptomatic of a deeper architectural issue elsewhere, the reviewer flags the symptom in the diff as P0 and notes the architectural concern separately as P1 — it does NOT escalate pre-existing code to P0.

### Reviewer output format

The reviewer must produce a markdown document with this skeleton (parseable by `/donace:execute`):

````markdown
# Review: stage-<sid>

## Summary
<one-paragraph verdict>

## Findings

### [P0] <short title>
<detail; cite file:line where possible>

### [P1] <short title>
<detail>

### [P2] <short title>
<detail>

## Tests
<note about whether listed tests passed, missing tests, parsing notes>
````

If no findings of a given severity: omit the corresponding `### [Px]` blocks. If no findings at all, `## Findings` reads `(none)`.

The execute skill scans for `### [P0]` / `### [P1]` / `### [P2]` headers (anchored at start of line) to count findings per severity. Anything else in the document is human-facing prose.

### Stack-specific items via `references/`

The severity rubric above is universal. Stack-specific anti-patterns and idiomatic concerns live in `agents/references/review-checklist-<stack>.md`. Execute detects the stage's stack from the diff and injects the matching checklist into the reviewer payload.

**Stack detection** (`/donace:execute` step 10, between capture-diff and build-reviewer-payload):
1. Look at file extensions in the diff:
   - `.py` → `python`
   - `.ts` / `.tsx` / `.js` / `.jsx` → `typescript`
   - `.swift` / `.m` / `.mm` (or `.h` colocated with `.swift`/`.m`) → `ios`
   - Mixed / no match / can't tell → `general`
2. Read `agents/references/review-checklist-<stack>.md`.
3. Include its contents verbatim in the reviewer payload, between the diff and the "produce a [P0]/[P1]/[P2] review" instruction.

For codex reviewer, `codex_call.py review --diff-file <path> --test-results-file <path> --stack <name>` loads the same diff, test evidence, and checklist before composing the codex prompt.

**Checklist file format** — each `references/review-checklist-<stack>.md`:
- Markdown bullets organized under `## P0 (block stage)` / `## P1 (advisory)` / `## P2 (nit)` headings.
- Each bullet: a stack-specific anti-pattern with a one-line "why" and a code example.
- Length target: 40-80 lines.
- Severity buckets are advisory to the reviewer — the universal contract above takes precedence if they conflict (e.g., a checklist item listed under P1 that becomes P0 in a particular case because it's a security regression).

Sample items to seed each file:
- **python**: bare `except:` → P1; `subprocess(..., shell=True)` with user input → P0; mutable default arg → P1; `# type: ignore` without comment → P2.
- **typescript**: `any` type when concrete is feasible → P1; non-null assertion `!` on user-provided value → P0; `console.log` left in non-debug code → P2; `as` casts of network responses without validation → P0.
- **ios**: force-unwrap of optionals from user input or network → P0; force-cast `as!` → P0 if user-controlled; implicitly-unwrapped optional on lazy property → P1; `print()` left in shipped code → P2.
- **general**: function > 100 lines → P1; magic numbers in business logic → P2; commented-out code in the diff → P2.

## Codex shell helper (`sdk/codex_call.py`)

Single Python script with two CLI modes:

```
codex_call.py implement --run-id <id> --stage-id <sid> [--retry-context-file <path>]
codex_call.py review    --run-id <id> --stage-id <sid> --diff-file <path> --test-results-file <path> --stack <python|typescript|ios|general>
```

Behavior (mirrors orchestration commit `2317a3f`):
1. Locate codex companion: `~/.claude/plugins/cache/openai-codex/codex/<version>/scripts/codex-companion.mjs`.
2. Build prompt:
   - For `implement`: `codex-implementer.md` prefix + current stage block + optional retry context.
   - For `review`: `codex-reviewer.md` prefix + current stage block + diff + `test-results.md` contents + `references/review-checklist-<stack>.md` contents + the universal severity rubric (echoed inline so codex doesn't have to remember it).
3. Persist prompt to `.ai/runs/<id>/stages/<sid>/codex-<mode>-prompt.txt`, then run `node codex-companion.mjs task --background --json [--write] --prompt-file <path> --cwd <cwd>` → capture `jobId` from JSON. `--write` is added for `implement` only (review is read-only).
4. Poll: `node codex-companion.mjs status <jobId> --json` every 5s, max 600s; read terminal status from `job.status`.
5. Fetch: `node codex-companion.mjs result <jobId> --json` → read final message from `storedJob.result.rawOutput` (with `storedJob.result.codex.stdout` / `storedJob.rendered` as fallbacks).
6. Print result to stdout (JSON: `{status, summary, raw_output}` on success; `{status: "error", error_class, message}` on failure where `error_class ∈ {rate_limited, timeout, plugin_missing, worker_failed, parse_fail}`).
7. Exit codes:
   - `0` — success; main LLM parses stdout for the review/impl artifact.
   - `1` — retry-eligible error (timeout, plugin missing, worker failed, parse fail). Main LLM treats it the same as a P0 reviewer finding — consumes one retry from the stage's 3-try budget.
   - `2` — rate-limited. Drives the stage to `interrupted` per `/donace:execute` step 6. **Does not consume a retry.**

No "warning severity" advisory like current donace.

## State on disk

```
.ai/runs/<id>/
  meta.json              # {created_at, status: "spec"|"planned"|"running"|"interrupted"|"completed"|"blocked"}
  spec.md                # from /chat
  plan.md                # from /plan
  stages/
    <sid>/
      status.json        # {status, retry_count, pre_stage_sha, reason?}
      retry-context.md   # latest failure surface fed into the next retry (tests / worker error / P0 block)
      test-results.md    # listed test commands + exit codes + output snapshots
      review.md          # latest reviewer output; saved immediately after each review attempt
      diff.patch         # current-attempt diff used for review; after PASS it also serves as the final stage diff snapshot
```

`.ai/` is gitignored.

`<id>` format: `run-<8-hex>` (e.g., `run-a1b2c3d4`).
`<sid>` format: `stage-<n>` (e.g., `stage-1`, `stage-2`).

## `donace stage_status` output

`donace stage_status <run-id>` renders a one-screen ASCII pipeline view of the run from the on-disk artifacts. **No daemon, no event bus, no WebSocket** — purely a function from on-disk state to a rendered string. This is v0's lightweight equivalent of a dashboard: when a user wants to glance at run state, they invoke this; the conversation with main LLM remains the live-interactive surface.

**Inputs read** (all already specified in `## State on disk`):
- `meta.json` — overall run state, `created_at`.
- `plan.md` — stage names and `implementer:` tags. Reuses the same parser the planner subagent and `/donace:execute` use; no duplicate parser.
- `stages/<sid>/status.json` — per-stage state, `retry_count`, `reason?`.
- `stages/<sid>/test-results.md` — tail extracted on demand for the active or blocked stage.
- `stages/<sid>/review.md` — tail used when the active or blocked stage has a recorded P0 finding.

**Output layout**:

```
Run <run-id>  •  <overall-status>  •  N/M stages passed  •  started <relative-time>

  <glyph>  <sid>   <stage-name>             <implementer> → <reviewer>   <state-summary>
  ...

▼ <active-or-blocked-sid> latest <test-failure | P0-finding> (retry <n>):
  <tail block>
```

**Glyphs** (single-character UTF-8, fallback to ASCII when `LANG` is plain):
- `✓` (`*`) — passed
- `⟳` (`>`) — running
- `·` (`.`) — pending (not yet started)
- `✗` (`!`) — blocked
- `‖` (`|`) — interrupted

**State-summary phrasing**:
- `passed (1 try)` / `passed (3 tries)`
- `running, retry 1/2`
- `blocked: tests failed` / `blocked: P0 unresolved` / `blocked: resume baseline mismatch`
- `interrupted: rate_limited` / `interrupted: user_interrupted`
- `pending` (no qualifier)

**Reviewer column** is derived from the implementer tag using the "opposite model" rule (claude impl → codex review; codex impl → claude review). Stage_status does not need `reviewer:` written into plan.md.

**Tail extraction** (only for the single active or blocked stage, to keep output one-screen):
- Status `running` or `blocked: tests failed` → last failing command line + last ~8 lines of its stderr/stdout from `test-results.md`.
- Status `running` retrying after a [P0] (i.e., previous review.md had a P0) → first `### [P0]` block from `review.md`, truncated to ~8 lines.
- Status `passed` / `pending` / `interrupted` / no clear evidence → omit the tail block.

**Width**:
- Default 80 columns. Uses `shutil.get_terminal_size()` to expand on wider terminals; never wraps stage rows mid-line.
- Stage-name column truncates with `…` when the name is longer than the available column width.

**No flags in v0**:
- `donace stage_status <run-id>` is the only invocation. No `--json`, no `--watch`, no `--stage <sid>` filter, no `--no-color`. Add later if real usage demands it.

## What we delete from current donace

(`simplify` branches from `main`, so most of these files are already absent on the implementation branch. This section is still useful because the simplification claim is relative to `orchestration`, where these layers exist today.)

- All of `sdk/` except a slim `codex_call.py` and `cli.py` (vs current `agent_dispatch.py`, `commands.py`, `events.py`, `job_runner.py`, `dashboard.py`, `run_validator.py`, `orchestrator.py`, `detach.py`).
- All hook code (file_scope, blocklist edge cases, mass-mutator ban, subagent type restrictions).
- All plan-job state machine logic (`PASS`/`AWAIT_APPROVAL`/`REVIEW`/`PENDING`/`ERROR`).
- The complex run-state classifier (`empty`/`not_started`/`in_progress`/`incomplete`/`completed`) that derived a state by inspecting multiple on-disk artifacts. v0 carries a flat `status` field on `meta.json` written at known transition points instead — much narrower in scope.
- Lock files / `_register_job` / `_unregister_job` / stale-lock cleanup.
- DETACH plan + `sdk/detach.py` + `cmd_job_status`.
- Dashboard (HTML, WebSocket, SQLite history).
- `run_validator` + post-run validation.
- `agents/team-lead.md`, `agents/test-engineer.md`, `agents/runtime-verifier.md`, `agents/documenter.md`, `agents/code-reviewer.md`, `agents/references/`.
- `cmd_verify`, `cmd_review`, `cmd_document` (the wrap phase).
- Plan auto-fix via codex (`run_codex_plan_fix`).
- Two-pass codex plan review + missed-finding audit.
- Three-strike repeat-failure detection.
- Idle-heartbeat watchdog.
- Token budget hints / SharedContext levels / routing hints (EMA stats).
- `archive/` legacy plans.

## What we port from current donace

Patterns we recreate in `simplify`, referencing the orchestration commit as source:

| Pattern | Source commit | Why keep |
|---|---|---|
| Codex stage call via `task --background --json` + status poll + result fetch | `2317a3f`, `85358af` | Survives Claude Code idle teardown; plan-review proven this works |
| `pre_stage_sha` capture before stage + per-stage auto-commit (after PASS) | `c2da399` | Bounds reviewer diff scope; recovery anchor. We still depart from c2da399's temporary-index "scope to listed files" pattern, but v0 compensates with a clean-worktree preflight and "reserved worktree while execute is running" contract. |
| `CLAUDE_PLUGIN_ROOT` for portability in skill files | `50347dd` | Skills run from various cwds |
| `RateLimitError` → INTERRUPTED status (not BLOCKED) | `cb4f747`, `a383e66` | Rate-limit is infra, not failure; preserve partial work and resume cleanly |

## Out of scope (v1+)

| Feature | When | Sketch |
|---|---|---|
| Parallel stage execution via worktrees | v1 | Each stage runs in own worktree; merge after all PASS; conflict detection on merge. |
| Unattended overnight runs | v2 | Add `donace execute --detach`; reuses pattern from orchestration commits `ce2c865..6fdde6f`. |
| /chat session continuity | v1 | Resume conversation via `/donace:chat <run-id>`; persist transcript. |
| Plan revision flow | v1 | If user wants to revise mid-execute, `/donace:plan <id> --revise` re-runs planner with current spec + retained passed stages. |
| Dashboard | v2 | Re-add if needed; not now. |

## Testing strategy

Test framework: stdlib `unittest`. No pytest, no new deps.

```
python3 -m unittest discover -s sdk/tests
```

Unit tests:
- `test_codex_call.py` — mock subprocess, verify task → status → result flow + error handling (timeout, status=failed, plugin missing, parse failure).
- `test_plan_parser.py` — parse plan.md, extract stages, validate `implementer:` tag values, reject malformed.
- `test_run_start.py` — mints run-id, creates `.ai/runs/<id>/` skeleton.
- `test_execute_test_commands.py` — parse `tests:` bullets, skip `none:` markers, persist `test-results.md`, and surface failures as retry context.
- `test_stage_status_render.py` — feed canned `meta.json` + `plan.md` + per-stage status/test-results/review fixtures; assert the exact ASCII output (header line, per-stage rows with glyphs and state-summary phrasing, tail extraction for the active or blocked stage). Cover: all 5 status glyphs, retry counter formatting, `none:` test bullets, missing review.md, narrow vs wide terminal width, and the ASCII fallback when `LANG` is plain.

No tests for skills (prompt files, not code).
No integration test for `/donace:execute` end-to-end — Claude Code session is the runtime, can't be unit-tested.

Manual smoke test: a tiny "add a function" run through chat → plan → execute on a scratch repo. Acceptance: 1 claude stage and 1 codex stage both pass review, produce per-stage commits, and leave behind `test-results.md` for each stage.

## Migration / bootstrap order

The new donace is bootstrapped on `simplify` branch. Since donace doesn't yet exist there, we manually drive the bootstrap (no chicken-and-egg).

1. **Branch from main as `simplify`** ✅ done.
2. **Write spec doc** (this) — in progress.
3. **Write implementation plan** — `.ai/runs/<bootstrap-id>/plan.md` by hand. Stages:
   - Stage 1: skill scaffolding (`skills/chat/SKILL.md`, `skills/plan/SKILL.md`, `skills/execute/SKILL.md`) + package wiring (`plugin.json`, README install docs, contributor skill symlink instructions).
   - Stage 2: agent files (`planner.md`, `implementer.md`, `reviewer.md`) + 4 stack checklists in `agents/references/` + codex prompt prefixes.
   - Stage 3: `sdk/codex_call.py` + tests.
   - Stage 4: `sdk/cli.py` — three subcommands (`run_start`, `list_runs`, `stage_status`) plus the ASCII pipeline view renderer that `stage_status` calls. Tests cover plan parsing, execute test-command handling, and the renderer's glyph/state-summary/tail-extraction outputs.
   - Stage 5: create or rewrite `CLAUDE.md` to reflect the new contract.
4. **Manually execute the plan** using main LLM + Agent tool. Two bootstrap-specific constraints: (a) all bootstrap stages must use `implementer: claude` because `codex_call.py` doesn't exist until stage 3 lands; (b) reviewer is also `claude` (i.e., subagent dispatch of `reviewer.md` once it lands in stage 2; before that, reviewer is the main LLM doing a manual diff read). Bootstrap is a one-shot dogfood, not a representative run.
5. **First real run** (post-bootstrap): use `/donace:chat` + `/donace:plan` + `/donace:execute` on a small toy task to verify the full loop end-to-end with both implementer models actually exercised.
6. **Smoke acceptance** (criteria for "v0 ships"):
   - Successful run with at least one `implementer: claude` stage and one `implementer: codex` stage, both passing review with no manual intervention.
   - Reviewer output parsed into [P0]/[P1]/[P2] correctly (assert by reading `review.md`, not by deliberately engineering a P0).
   - User confirmed mid-run interactivity: chat with main LLM during a long stage, get a response, and either the same session or a resumed `/donace:execute <id>` continues the run correctly.

## Decisions locked

(For things that came up during brainstorming and were resolved.)

| Decision | Pick | Reason |
|---|---|---|
| Reviewer behavior | Mixed: P0 gates, P1/P2 advisory | Pure gate is too rigid; pure advisory removes the point of having a reviewer |
| Reviewer model | Opposite of implementer | Independence is review's value |
| Worker selection | Planner-tagged at `/plan` time | Planner reads stage anyway; user can override by editing |
| Plan vs spec separation | Two levels: spec.md (main LLM + user, conversational) → plan.md (planner subagent, structured) | Brainstorm needs main LLM's chat context with user; plan writing benefits from a focused subagent with a specialized prompt and fresh context window |
| Worktree policy | Clean-tree preflight + shared worktree reserved during `/execute` | Keeps stage diffs and `git add -A` commits trustworthy without reintroducing worktrees or hook-heavy file scoping |
| Stop states | `interrupted` preserves resumable partial work; `blocked` means user action required | User-requested stop and rate-limit are not failures; retry-budget exhaustion and baseline mismatch are |
| Test evidence | Execute runs stage `tests:` commands and passes results to reviewer | Reviewer should judge from evidence, not infer test failures from the diff alone |
| Test enforcement | Orchestrator runs listed `tests:` commands; reviewer judges the recorded evidence | End-state verification is reliable; strict TDD-order enforcement is not |
| P0 retry budget | 2 retries (3 total tries) | Tighter than current donace's 3-strike; covers hallucination correction without indefinite loops |
| /chat output | Free-form prose | Templating restricts brainstorm; planner is flexible |
| codex_call.py poll interval | 5s | Matches plan-review pattern; not chatty |
| Migration approach | In-place rewrite from `main` | Less to delete than from orchestration; proven small baseline |

## Decisions deferred

These do not block implementation; revisit if real usage surfaces issues:

- Whether `/donace:execute` should support `--from-stage <sid>` to skip ahead manually.
- Whether codex prompt prefixes live in `agents/prompts/` vs inline in `codex_call.py`.
- Whether reviewer dispatch can be skipped per-stage via `review: skip` plan tag (escape hatch for trivial mechanical stages).
- Whether stage `files:` field should accept globs (`src/**/*.py`).

---

## Acceptance for "spec done"

- [ ] User reviews spec for accuracy.
- [ ] No `TBD` / `TODO` / `?` placeholders.
- [ ] No internal contradictions between planner contract, execute flow, and packaging/install surface.
- [ ] All "decisions locked" come from this conversation; nothing invented.
- [ ] All "decisions deferred" are non-blocking.
- [ ] Migration order is executable by main LLM without donace itself existing.
