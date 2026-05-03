# Donace — Project Contract

Plan / execute / review pipeline orchestrating Claude Code and Codex with per-stage worker selection. Main LLM in the user's Claude Code session is the orchestrator; three subagents (planner, implementer, reviewer) do focused work.

This file is the stable contract for anyone working on donace. Dynamic knowledge lives in `.ai/runs/<id>/` (gitignored, local-only) and in recent commit history.

## Layout

```
skills/
  chat/SKILL.md       /donace:chat — brainstorm spec.md with the user (inline; main LLM)
  plan/SKILL.md       /donace:plan <id> — dispatch planner subagent → plan.md
  execute/SKILL.md    /donace:execute <id> — 15-step loop, stages run serially in background
agents/
  planner.md          spec.md → plan.md (Read/Grep/Glob/Bash/Write)
  implementer.md      one stage at a time (Read/Grep/Glob/Bash/Write/Edit)
  reviewer.md         diff + test-results → [P0]/[P1]/[P2] markdown (Read/Grep/Glob/Bash; no Write)
  references/         review-checklist-{python,typescript,ios,general}.md
  prompts/            codex-{implementer,reviewer}.md (used by codex_call.py)
  qa.md, ui-designer.md   standalone ad-hoc tools (not part of the pipeline)
sdk/
  codex_call.py       wraps codex-companion.mjs: task → status → result
  cli.py              donace run_start | list_runs | parse_plan | stage_status | mark_completed
  tests/              stdlib unittest
```

## Commands

**Run tests** (stdlib unittest — no pytest):

```bash
python3 -m unittest discover -s sdk/tests
```

**Use the CLI directly** (also invoked from skill bodies):

```bash
python3 sdk/cli.py run_start
python3 sdk/cli.py list_runs
python3 sdk/cli.py parse_plan --run-id <id>
python3 sdk/cli.py stage_status <id>
python3 sdk/cli.py mark_completed --run-id <id>
```

## Status conventions

**Per-stage `status.json`**:

- `running` — a worker or orchestrator step is in flight.
- `passed` — committed; skipped on resume.
- `interrupted` — recoverable stop; partial work preserved. Reasons: `rate_limited`, `user_interrupted`, session ended mid-stage. **Resume does NOT consume a retry.**
- `blocked` — hard stop; needs user intervention. Reasons: `tests failed` (after retry budget exhausted), `P0 unresolved`, `resume baseline mismatch`.

**Per-run `meta.json` `status`** (one of): `spec`, `planned`, `running`, `interrupted`, `completed`, `blocked`.

**P0 retry budget**: 3 total tries per stage (initial + 2 retries). Worker errors, test failures, and reviewer P0 findings all consume from the same budget. Rate-limits do NOT.

## Worker dispatch

| Implementer | Reviewer | Implementer dispatch | Reviewer dispatch |
|---|---|---|---|
| `claude` | `codex` | `Agent(subagent_type=implementer, run_in_background=true)` | `Bash("python3 sdk/codex_call.py review --diff-file ... --test-results-file ... --stack ...", run_in_background=true)` |
| `codex` | `claude` | `Bash("python3 sdk/codex_call.py implement ...", run_in_background=true)` | `Agent(subagent_type=reviewer, run_in_background=true)` |

## codex_call.py exit codes

- `0` — success; stdout JSON `{status, summary, raw_output}`.
- `1` — retry-eligible error (timeout, plugin missing, worker_failed, parse_fail). Stdout JSON `{status: "error", error_class, message}`. Consumes a retry.
- `2` — `rate_limited`. Drives stage to `interrupted`. Does NOT consume a retry.

## Worktree contract

`/donace:execute` requires a clean worktree on fresh start (`git status --porcelain` empty). On resume of an `interrupted` stage, dirty worktree is allowed because that's the partial work; the orchestrator verifies HEAD still matches the saved `pre_stage_sha`. Mismatch → `blocked: resume baseline mismatch`.

The shared worktree is reserved while a run is active. User makes unrelated edits at their own risk; `git add -A` at PASS time will pick them up.

## Invariants worth preserving

- **Plan-time worker selection is the planner's job.** Plan files have `implementer: claude|codex`; the orchestrator does NOT auto-route. Reviewer is implicit (opposite model).
- **Orchestrator runs `tests:` itself, not the implementer or reviewer.** Test results are evidence written to `test-results.md` and passed in the reviewer payload. Reviewer must NOT re-run tests.
- **Reviewer is text-only.** It returns markdown findings; main LLM persists the document and parses [P0] count for retry decisions.
- **The diff is the source of truth.** Implementer's text reply is failure-surface context only. `git diff <pre_stage_sha>` (persisted to `diff.patch`) is what gets reviewed and committed.
- **No NEEDS_CONTEXT escalation protocol.** Implementer is self-directed; only fails on a true hard stop.

## Non-goals (v0)

Parallel stages via worktrees (v1), unattended overnight runs / detach (v2), `/donace:chat` continuity (v1), web dashboard (v2), `--from-stage`/`--watch`/`--json` flags on `stage_status` (v1+).

## Spec source of truth

[docs/superpowers/specs/2026-05-02-donace-simplify-design.md](docs/superpowers/specs/2026-05-02-donace-simplify-design.md)

## Non-contracts

`.ai/runs/<id>/` is gitignored. Run artifacts (spec.md, plan.md, stages/) are local-only.
