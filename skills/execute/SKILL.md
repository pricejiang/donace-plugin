---
name: execute
description: Execute a donace plan end-to-end. Use when the user invokes `/donace:execute [run-id]` or asks to run a validated donace plan. Resolves a run-id, validates `codex_review.status == PASS`, then dispatches the team-lead agent to drive run_jobs → verify → review → document → run_complete. Aborts if the plan is missing or needs revision.
---

# Donace Execute

Dispatch the `team-lead` agent to execute a validated plan to completion. This skill is a thin launcher — team-lead does all coordination. Use this when `/donace:plan` has produced a PASS plan and the user is ready to build.

## Rules

- Invoke orchestrator commands by **absolute path**: `python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" <cmd>`.
- Do NOT run any stage yourself. All execution goes through the `team-lead` dispatch in Step 3.
- The user is in control: abort with a clear message if any precondition fails instead of guessing.

## Step 1: Resolve run-id

**If the user provided a run-id argument**, use it directly. Continue to Step 2.

**Otherwise**, auto-detect the most recent PASS run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" list_runs \
  --cwd "<project>" --state not_started,incomplete
```

`not_started` = plan done, execute never dispatched (the common case after `/donace:plan`).
`incomplete` = execute partially ran — we can resume if the plan is still PASS.

Parse the output. Filter entries where both:
- `plan_done == true`, AND
- `jobs_completed.plan == "PASS"`

Pick the newest by `started_at` (the orchestrator already sorts newest-first).

If **no** such run exists, abort with:

> "No executable plan found. Run `/donace:plan <your-task>` first to create one."

## Step 2: Validate plan status

Read `.ai/runs/<id>/plan.json`.

Then fetch the run entry:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" list_runs \
  --cwd "<project>"
```

Filter to this `run_id` and treat `jobs_completed.plan` as the authoritative execute gate. **Do not use `codex_review.status` as the PASS/REVIEW verdict** — its affirmative terminal value is `completed`; the user-facing gate is the plan job status.

First handle run-level states:

- If the run entry is missing: abort — the run-id is invalid or the run directory is gone.
- If `state == "in_progress"`: abort and tell the user another process is already executing this run.
- If `state == "completed"`: report that the run is already finished; do not dispatch `team-lead` again.

Branch on `jobs_completed.plan`:

- If `"PASS"`: continue to Step 3.
- If `"AWAIT_APPROVAL"`: abort with:

  > "Plan at `.ai/runs/<id>/plan.md` is waiting on approve/reject for codex's auto-fix. Run `/donace:plan <id>` to land that verdict before executing."

- If `"REVIEW"`: abort with:

  > "Plan at `.ai/runs/<id>/plan.md` needs revision. Run `/donace:plan <id>` on this run to revise, then try executing again."

- If `"PENDING"`, or if `plan.json.codex_review.status == "running"`: abort with:

  > "Plan review for run-`<id>` is still in progress. Wait for it to finish or resume `/donace:plan <id>` before executing."

- If `"ERROR"`: abort with:

  > "Plan review for run-`<id>` failed before producing an executable verdict. Inspect `plan.json`'s `codex_review.reason`, fix that issue, then re-run `/donace:plan <id>`."

- If missing or any other value: abort and tell the user the plan gate is not in a runnable PASS state.

Also confirm `plan.json.stages` is a non-empty list. If empty, abort — no stages to run.

## Step 3: Dispatch team-lead

Use the `Agent` tool with `subagent_type: "team-lead"`. The prompt must be self-contained (team-lead starts with zero context from this conversation):

```
Agent(
  subagent_type: "team-lead",
  description: "Execute donace run <id>",
  name: "team-lead-<run-id-short>",
  run_in_background: true,
  prompt: """
Execute donace run-id=<id> to completion.

Plan: .ai/runs/<id>/plan.md (codex status: PASS)
Plan sidecar: .ai/runs/<id>/plan.json — read this for stages, files, dependencies.
Project cwd: <project>

Pipeline:
  1. For each stage in plan.json, in dependency order:
       run_job --stage-id <sid> --plan .ai/runs/<id>/plan.json \
         --run-id <id> --cwd <project>
     Use run_in_background: true. Run stages SERIALLY (per-stage commit + codex review scope depend on shared git state — never parallelize).
     On BLOCKED: read the job JSON's `unresolved` field. Retry with --max-fix-attempts 3 if the failure is mechanical (missing import, typo). Escalate to the user via AskUserQuestion if structural.
  2. After all stages PASS, run the wrap phase in background:
       verify --agents "test,codex"
       review (only if ≥5 files changed total, or security-sensitive code touched)
       document (only if any stage has user-facing changes)
  3. run_complete --run-id <id> --cwd <project>

Rules:
- All long commands: run_in_background: true.
- AskUserQuestion ONLY for genuine blockers (BLOCKED stage with structural unresolved, ambiguous direction). Do not ask about classification or scope — the plan is already validated.
- Do NOT re-plan. If codex flags new issues mid-execution, report and stop — revision is the plan skill's job.

Return a 3-line summary:
  stages: <passed>/<total> PASS, <blocked> BLOCKED
  wrap:   verify=<status> review=<status> document=<status>
  final:  <run_complete state>
"""
)
```

Use `run_in_background: true` so main LLM stays unblocked while team-lead runs — the user can ask other questions or stop the run mid-flight. Save the returned `task_id` (you'll need it for Step 5). Team-lead's internal long commands each run in background inside the subagent — that hasn't changed.

After dispatch, give the user a one-line confirmation:

> "Team-lead is now executing run-`<id>` in the background (task `<task_id-short>`). Ask me anything; say `stop` to interrupt the active run cleanly."

Then continue handling other user messages normally. **Do NOT poll, sleep, or proactively check progress** — the harness will notify you when team-lead completes.

## Step 4: Handle team-lead task notifications

When you receive the `<task-notification>` for the team-lead task, branch on the task outcome before reporting anything to the user.

### Success path

If the task completed successfully **and** its result contains the promised 3-line summary, print that summary verbatim, plus a final `list_runs` snapshot for the run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" list_runs \
  --cwd "<project>" --state not_started,incomplete,completed
```

Filter the result to just this run's entry (by `run_id`) and show its final state.

If the user is mid-conversation on something unrelated when the notification arrives, insert the report briefly ("by the way, run-`<id>` finished: ...") without derailing the current topic.

If any stage is BLOCKED or wrap phase has errors, tell the user where to look:
- BLOCKED job JSON: `.ai/runs/<id>/jobs/job-run_job-<stage-id>-*.json` (look at `unresolved`)
- Verify failures: `.ai/runs/<id>/jobs/job-verify-*.json`

Suggest next actions — retry the specific stage, revise the plan via `/donace:plan`, or dig into the job JSON manually.

### Failure / cancellation path

If the task notification reflects a **cleanup stop that you intentionally triggered in Step 5 after confirming there was no active orchestrator job**, report it plainly as a launcher stop, show the current run entry, and do not call it an execution failure.

Otherwise, if the task notification says the background `team-lead` task errored, was cancelled, was stopped unexpectedly, or returned no 3-line summary, **do NOT pretend execute finished cleanly**. First pull the current run state:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" list_runs \
  --cwd "<project>" --state not_started,incomplete,completed
```

Filter to this run's entry and show it. Then tell the user plainly:

> "Background execute for run-`<id>` ended unexpectedly before team-lead produced a final summary."

Include the task error / cancellation reason verbatim if one exists. Point the user at the relevant artifacts:
- Stage jobs: `.ai/runs/<id>/jobs/job-run_job-*.json`
- Verify jobs: `.ai/runs/<id>/jobs/job-verify-*.json`
- Final aggregate (if present): `.ai/runs/<id>/result.json`

Suggest the next step based on the run state:
- `incomplete` → resume with `/donace:execute <id>`
- `completed` but failing / blocked → inspect the job JSONs and retry the specific stage or revise the plan

## Step 5: Stopping a running execute

If at any time after Step 3 the user says "stop", "kill team-lead", "停掉", "终止" or similar, **do not `TaskStop` team-lead first**. The goal is to stop the active orchestrator job cleanly so it returns `INTERRUPTED` and team-lead can report the partial state.

First, inspect active jobs:

```bash
curl -s localhost:8741/api/jobs/active
```

If there are active jobs for this run, interrupt each matching job through the dashboard API:

```
curl -s -X POST localhost:8741/api/interrupt \
  -H "Content-Type: application/json" \
  -d '{"job_id":"<active-job-id>","reason":"user requested stop from /donace:execute"}'
```

Then tell the user:

> "Stop requested for run-`<id>`. The active job will finish its current agent call, then return `INTERRUPTED`. I'll report the final partial state when team-lead confirms the run is quiescent."

Leave the background `team-lead` task running so it can observe the interrupt, gather the job result, and exit through the normal Step 4 notification path.

If `jobs/active` shows **no** active job for this run, but the saved background `team-lead` task is still hanging around (for example, it is only polling or waiting to report), then use `TaskStop` as cleanup:

```
TaskStop({task_id: <saved-from-step-3>})
```

Then confirm:

> "No active orchestrator job was running for run-`<id>`, so I stopped the background team-lead task itself. `/donace:execute <id>` can resume from the current run state if needed."

If the dashboard interrupt API is unavailable and the user still wants a hard stop, tell them that `TaskStop` is only a launcher kill and may leave an in-flight `run_job` / `verify` bash running. Only use it as a fallback after that warning; do not say the run is fully stopped until the run state or active-jobs view shows nothing still executing.

## Abort conditions

- No run-id and no PASS run found (Step 1).
- Plan not PASS or empty stages (Step 2).
- Team-lead dispatch itself errors before producing a task_id (report the Agent tool error verbatim).
