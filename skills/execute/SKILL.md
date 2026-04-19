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
  --cwd "<project>" --state incomplete
```

Parse the output. Filter entries where both:
- `plan_done == true`, AND
- `jobs_completed.plan == "PASS"`

Pick the newest by `run-id` (uuid hex — either ctime-sorted via the output order, or the entry the orchestrator lists first).

If **no** such run exists, abort with:

> "No executable plan found. Run `/donace:plan <your-task>` first to create one."

## Step 2: Validate plan status

Read `.ai/runs/<id>/plan.json`. Check `codex_review.status`:

- If `"PASS"`: continue to Step 3.
- If `"REVIEW"` (or missing): abort with:

  > "Plan at `.ai/runs/<id>/plan.md` needs revision (codex status: `<status>`). Run `/donace:plan` on this run to revise, then try executing again."

Also confirm `plan.json.stages` is a non-empty list. If empty, abort — no stages to run.

## Step 3: Dispatch team-lead

Use the `Agent` tool with `subagent_type: "team-lead"`. The prompt must be self-contained (team-lead starts with zero context from this conversation):

```
Agent(
  subagent_type: "team-lead",
  description: "Execute donace run <id>",
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

Use `run_in_background: false` for the Agent dispatch itself — you want to wait for the full pipeline summary, which can take many minutes. Team-lead's internal long commands each run in background inside the subagent.

## Step 4: Report

When team-lead returns, print its 3-line summary verbatim, plus a final `list_runs` snapshot for the run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" list_runs \
  --cwd "<project>" --state incomplete
```

If the run has moved to `completed` state (no longer in `--state incomplete`), run without the filter to show its final row.

If any stage is BLOCKED or wrap phase has errors, tell the user where to look:
- BLOCKED job JSON: `.ai/runs/<id>/jobs/job-run_job-<stage-id>-*.json` (look at `unresolved`)
- Verify failures: `.ai/runs/<id>/jobs/job-verify-*.json`

Suggest next actions — retry the specific stage, revise the plan via `/donace:plan`, or dig into the job JSON manually.

## Abort conditions

- No run-id and no PASS run found (Step 1).
- Plan not PASS or empty stages (Step 2).
- Team-lead dispatch itself errors before producing a summary (report the Agent tool error verbatim).
