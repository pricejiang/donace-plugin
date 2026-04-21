# Donace — Project Contract

Generator-Evaluator agent harness for Claude Code. This file is the stable
contract for anyone (Claude or human) working on the codebase — commands,
layout, invariants. Dynamic knowledge lives in `.ai/` (gitignored) and in
recent commit history.

## Layout

```
sdk/                 Python orchestrator (CLI + dispatch + commands)
  orchestrator.py      argparse CLI entry point
  commands.py          cmd_* subcommand bodies
  agent_dispatch.py    AgentDispatcher, hooks, codex integration
  job_runner.py        per-stage execute pipeline (implement → verify → fix)
  events.py            EventBus, Stage, JobResult, event dataclasses
  run_validator.py     post-run validation (hook_deny_volume, token_anomaly, etc.)
  dashboard.py         live WebSocket dashboard + HTML
  tests/               stdlib unittest suite
agents/              agent frontmatter + system prompts
  team-lead.md         execution coordinator
  planner.md           plan writer
  implementer.md       code writer
  test-engineer.md     test writer/runner
  {ts,ios}-reviewer.md deep reviewers
  runtime-verifier.md  black-box verifier
  documenter.md        doc updater
skills/              user-invokable Claude Code skills
  plan/ execute/ chat/
```

## Commands

**Run tests** (stdlib unittest — no pytest, no new deps):
```bash
python3 -m unittest discover -s sdk/tests
```

**Invoke the orchestrator** (always absolute path — `python3 -m sdk.orchestrator`
requires `cwd=plugin-root` which doesn't match team-lead's cwd):
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" <subcommand> [args]
```

Available subcommands: `run_start`, `run_complete`, `list_runs`, `mark`,
`write_plan`, `plan`, `plan_status`, `run_job`, `verify`, `review`,
`document`, `health`.

## Status conventions

**Plan job status** (`jobs_completed["plan"]`):
- `PASS` — codex_review terminal, no major issues → execute can dispatch stages
- `REVIEW` — `codex_review.has_major_issues: true` → user must re-run `/donace:plan`
- `PENDING` — `codex_review.status in {queued, running}` → team-lead runs `plan_status` to poll
- `ERROR` — something broke; read job result JSON

The gate lives in `_classify_run_state` via `not plan_review_in_progress`.
A stale on-disk `PASS` plan job is held back if `plan.json.codex_review.status`
flips to `queued`/`running` (e.g. after a revision kicks off a new review).

**run_job status**:
- `PASS` — stage succeeded; per-stage commit created (files in scope only)
- `BLOCKED` — stage failed; counts against 3-strike `repeated_stage_failure`
- `INTERRUPTED` — external pause (rate-limit, user cancel). Does NOT count
  against retry budgets. `interrupted_at` tells you which phase: implement / verify / fix_loop

**Run state** (`_classify_run_state`):
`empty` | `not_started` | `in_progress` | `incomplete` | `completed`

## Codex integration

Codex plugin lives at `~/.claude/plugins/cache/openai-codex/codex/<version>/`.
donace calls `scripts/codex-companion.mjs`:

| Donace call | Codex subcommand | Mode | Rate-limit aware |
|---|---|---|---|
| `run_codex_review` (per stage) | `review` | foreground, 180s cap, via Haiku wrapper | yes |
| `run_codex_plan_review` | `task --background --json` | direct subprocess, 600s cap, poll-then-fetch | no (bypasses wrapper) |
| `fetch_codex_plan_review_result` | `status <job-id>` then `result <job-id>` | direct subprocess | no |
| `_codex_task_resume_candidate_thread_id` | `task-resume-candidate` | direct subprocess | no |

**Single test seam**: monkey-patch `AgentDispatcher._run_codex_json_subcommand`
to return canned per-subcommand payloads. Do not spawn real `node` subprocesses
in tests.

Plan-review thread reuse: `run_codex_plan_review(..., resume_thread_id=X)`
only passes `--resume-last` when `task-resume-candidate` confirms X is still
codex's latest task for the repo — otherwise falls back to fresh to avoid
resuming an unrelated thread.

## Hook guardrails

`AgentDispatcher` installs SDK hooks on every agent dispatch:
- Path boundary (Write/Edit confined to cwd)
- `file_scope` restriction (stage-specific writes only) — scope is
  normalized at construction via `_normalize_file_scope` (strips
  markdown backticks, whitespace, trailing `/`)
- Bash write detection (redirects, sed -i, tee, cp/mv) — `_blank_quoted_regions`
  neutralizes content inside `"..."` / `'...'` before `_REDIR_RE` runs, so
  JS / Perl / HTML embedded in `node -e "..."` doesn't register as writes
- Quoted redirect targets are re-extracted from the original command so
  `cmd > "apps/x.ts"` still scope-checks
- Command blocklist (`rm -rf /`, `DROP TABLE`, force-push, etc.)
- Mass mutators banned under `file_scope` (prettier --write, black, etc.)

**If a valid-looking operation is getting denied**: check
`run_validator.py::hook_deny_volume` output first — the normalization
code is where false positives have historically lived.

## When to touch what

| Change | Files |
|---|---|
| New orchestrator subcommand | `cmd_*` in `commands.py` + argparse/dispatch in `orchestrator.py` |
| Change agent behavior / tools | `agents/<name>.md` frontmatter + system prompt |
| Add a codex-companion call | Use `_run_codex_json_subcommand` seam for testability |
| Change plan job status logic | `_plan_status_from_codex_review` AND `_classify_run_state` AND team-lead.md decision tables |
| New event type | `events.py` + register + consume in `dashboard.py`/`run_validator.py` |

## Testing

- Pattern: stdlib `unittest`. One module per feature area.
- Async tests use a `_run(coro)` helper that creates a fresh event loop
  per call (`asyncio.new_event_loop()` + `finally: loop.close()`). Do
  not rely on the default loop.
- Mock codex-companion via `dispatcher._run_codex_json_subcommand = fake`
  where `fake(companion_script, subcommand, args, **_kw) -> dict | None`.
- Mock agent dispatch via `dispatcher.query = fake_async_fn`.
- Don't introduce pytest / new test deps. `requirements.txt` stays
  minimal.

## Invariants worth preserving

- **Plan review findings are a gate, not advisory**. `REVIEW` and `PENDING`
  both block stage dispatch. A late `needs-attention` verdict arriving via
  `plan_status` must still block execute — see the `plan_review_in_progress`
  bypass check in `_classify_run_state`.
- **Stages run serially**. Per-stage commit + codex review share git
  state; parallel `run_job` calls can contaminate review scope or cause
  `auto_commit` to skip because HEAD moved mid-dispatch.
- **Rate-limits are infra, not plan errors**. `RateLimitError` →
  `INTERRUPTED` at every call site. The run can resume; don't count it
  against retry budgets.
- **`.ai/runs/<id>/plan.md` is read-only during execute**. Team-lead
  never rewrites the plan. If the plan is broken, abort and redirect
  the user to `/donace:plan <run-id>`.
- **Hook false positives waste more tokens than they save**. Scope
  checks are load-bearing; if they reject in-scope operations, implementer
  burns tokens retrying. When in doubt, err toward letting a write through
  and catching the real damage at commit-scope level.

## Non-contracts

`.ai/sessions/`, `.ai/cards/`, and `.ai/runs/` are gitignored. Session
notes, knowledge cards, and run artifacts are local-only — do not expect
them on other machines or checkouts. Commit history is the shared record.
