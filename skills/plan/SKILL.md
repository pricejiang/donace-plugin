---
name: plan
description: Start a planning session for donace. Use when the user invokes `/donace:plan` or asks to plan a task using donace. Runs multi-turn Q&A to gather requirements, dispatches the planner to write `.ai/runs/<id>/plan.md`, runs codex plan review, and hands off to `/donace:execute`. Does NOT execute the plan.
---

# Donace Plan

Drive a planning session that produces a codex-reviewed `.ai/runs/<id>/plan.md`. This skill runs in the main conversation so you can have natural multi-turn dialogue with the user. It ends at handoff — executing the plan is a separate skill (`/donace:execute`).

## Rules

- Invoke orchestrator commands by **absolute path**: `python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" <cmd>`. Never use `python3 -m sdk.orchestrator`.
- All long commands (`write_plan`, `plan`) MUST use `run_in_background: true`. You'll be notified when they complete.
- Do NOT dispatch `run_job` or any execution command. This skill ends at handoff.
- Use the project cwd (the user's working directory), not the plugin root, for `--cwd`.

## Step 1: Pre-flight

Run health check and scan for runs that need plan attention:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" health --cwd "<project>"
python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" list_runs --cwd "<project>" --state incomplete,not_started
```

The `--state` flag accepts comma-separated values — both `not_started` (PASS plan ready, awaiting execute) and `incomplete` (plan/execute started but unfinished) are surfaced in one pass.

**State meanings** (parsed from `list_runs` output):
- `not_started` = plan has PASSed codex review and is waiting for `/donace:execute`. **Not a plan-skill concern** — do NOT offer to resume these. The user should run `/donace:execute <run-id>` instead.
- `incomplete` with `jobs_completed.plan == "REVIEW"` = codex flagged the plan; this IS plan-skill territory (revision).
- `incomplete` with `jobs_completed.write_plan == "ERROR"` = write_plan crashed before producing a plan.md; also plan-skill territory (retry).
- `incomplete` with `jobs_completed.write_plan == "PASS"` and no `jobs_completed.plan` = planner wrote `plan.md` but codex plan review did not finish; resume at Step 5.

For each `incomplete` run matching the REVIEW or ERROR cases above, use `AskUserQuestion` to offer:
- **Resume** that run (skip to Step 4 with its run-id — `write_plan` auto-detects the existing plan.md and treats the new brief as a revision directive)
- **Start fresh** (continue to Step 2 with a new run-id)

If an `incomplete` run has `write_plan == "PASS"` but no plan verdict yet, offer to resume review and skip directly to Step 5 with that run-id.

If no resumable run matches, continue to Step 2. `not_started` runs that you find should be surfaced to the user as **"you have an unstarted plan at `.ai/runs/<id>/plan.md` — run `/donace:execute <id>` to execute it, or proceed to write a new plan."** Do not auto-resume them from this skill.

## Step 2: Gather requirements

Read the user's task brief. Evaluate whether you have enough to write **testable success criteria** for each planned stage.

**If the brief is vague** (no target files, no observable success condition, no constraints), use `AskUserQuestion` for up to 3 questions. Pick from:

- "Which files or modules should this change touch?"
- "What is the observable success condition — what does 'done' look like?"
- "Any constraints — performance budgets, security, backwards compatibility, specific frameworks?"
- "What's explicitly out of scope?"

**Do NOT proceed** to Step 3 until the brief answers, at minimum:
1. What is being built (target surface)
2. Observable done condition (what passes/returns/renders)
3. Any hard constraint the planner must respect

Synthesize a one-paragraph brief. Show it to the user:

> "Here's the brief I'll send to the planner: *<synthesized brief>*. Proceed?"

Wait for the user's confirmation before continuing.

## Step 3: Start run

Generate a run-id (12-char hex — matches `sdk/events.py:new_run_id()` format):

```bash
python3 -c 'import uuid; print(uuid.uuid4().hex[:12])'
```

Start the dashboard if not already running, then start the run:

```bash
# Start dashboard in background if not already listening on 8741
(lsof -i :8741 -sTCP:LISTEN >/dev/null 2>&1) || \
  python3 "${CLAUDE_PLUGIN_ROOT}/sdk/dashboard.py" --port 8741 &

python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" run_start \
  --run-id <id> --cwd "<project>" \
  --dashboard-url ws://localhost:8741
```

Record the run-id — you'll reference it in every subsequent step and in the handoff message.

## Step 4: Dispatch planner (background)

Call `write_plan` in background with the synthesized brief. If this is a resume from Step 1, pass the revision brief (describe what codex findings to address):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" write_plan \
  --run-id <id> --cwd "<project>" \
  --task "<one-paragraph brief or revision instructions>"
```

Use `run_in_background: true`. Wait for the completion notification.

After completion, read `.ai/runs/<id>/plan.md` and show the user the stage list (stage names + dependencies) so they have visibility before codex review runs.

## Step 5: Codex plan review (background)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" plan \
  --run-id <id> --cwd "<project>"
```

Use `run_in_background: true`. Wait for completion.

Read `.ai/runs/<id>/plan.json`. Look at `codex_review.status` (PASS or REVIEW) and `codex_review.findings` (list of issues).

## Step 6: Branch on review status

### If `codex_review.status == "PASS"`:

Print the plan path and hand off:

> "Plan validated. Review `.ai/runs/<id>/plan.md` — when you're ready to build, run:
> 
> `/donace:execute <run-id>`"

Then **end the skill**. Do not proceed further.

### If `codex_review.status == "REVIEW"`:

Show the findings to the user and ask how to address them. Example:

> "Codex flagged these issues with the plan:
> 
> - *<finding 1>*
> - *<finding 2>*
> 
> How should I revise the plan?"

Synthesize the user's response into a revision brief, then **loop back to Step 4** with that brief as `--task`. The `write_plan` command auto-detects the existing `plan.md` and treats the new brief as a revision directive.

Keep looping Steps 4-6 until the plan passes or the user decides to abandon the run.

## Step 7: Handoff

Once you print the PASS handoff message, **do not** invoke `run_job`, `verify`, `review`, or any execution command. The user must explicitly run `/donace:execute` to trigger execution.

If the user says "run it" or similar during Step 6, politely remind them that execution is `/donace:execute <run-id>` — keep the plan/execute phases separated so they retain a review checkpoint.

## Abort conditions

Stop the skill and report to the user when:
- `health` reports a broken orchestrator (missing deps, bad Python version)
- `run_start` returns non-zero
- `write_plan` errors and returns a job JSON with `unresolved` set
- The user declines to clarify a vague brief after 3 `AskUserQuestion` attempts
- Codex review has looped 3+ times without the plan reaching PASS (escalate for a human-driven edit of `plan.md`)
