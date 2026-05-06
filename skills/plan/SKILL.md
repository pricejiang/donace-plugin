---
name: donace-plan
description: Decompose a spec.md into an implementation plan.md by dispatching the planner subagent. Use this when the user has a spec from /donace:chat (or written one by hand) and wants stages with files, success criteria, tests, and per-stage implementer tags.
---

# /donace:plan

Dispatch the planner subagent to convert `.ai/runs/<id>/spec.md` into `.ai/runs/<id>/plan.md`.

## Inputs

- `<run-id>`: required positional argument. Must correspond to an existing run directory under `.ai/runs/`.

## Flow

1. **Sanity-check.** Verify `.ai/runs/<id>/spec.md` exists. If not, tell the user to run `/donace:chat` first (or check the run-id).

2. **Dispatch planner subagent in background.**

   ```
   Agent(
     subagent_type=planner,
     prompt=<see below>,
     run_in_background=true,
   )
   ```

   Prompt template:

   ```
   run-id: <id>
   cwd: <absolute project root>

   spec.md contents:
   ---
   <verbatim contents of .ai/runs/<id>/spec.md>
   ---

   Write the implementation plan to .ai/runs/<id>/plan.md. When done, reply
   with a short confirmation summary like "Plan written: 5 stages, 3 claude
   / 2 codex".
   ```

3. **Stay interactive.** While the planner runs, the main LLM is free. The user can chat, clarify, or interrupt. When the Agent completes, you'll be notified.

4. **Verify output.** Once the agent reports done, check that `.ai/runs/<id>/plan.md` exists and is non-empty:

   ```bash
   test -s .ai/runs/<id>/plan.md
   ```

   If the file is missing or empty, surface the planner's text reply (likely contains the failure reason) and stop.

5. **Dispatch codex plan review (background, advisory).** Planner is Claude, so the reviewer is codex (opposite-model independence — same policy as per-stage review).

   ```
   Bash(
     "python3 sdk/codex_call.py review-plan --run-id <id>",
     run_in_background=true,
   )
   ```

   While codex reviews, the main LLM stays interactive — same pattern as background planner dispatch. When the Bash job completes, parse stdout JSON `{status, summary, ...}` and persist `summary` to `.ai/runs/<id>/plan-review.md`. On non-zero exit, surface the `error_class` + `message` from stdout JSON to the user but do NOT block hand-off (this review is advisory; a flaky codex run shouldn't stop planning).

6. **Hand off.** Tell the user:

   ```
   Plan written: .ai/runs/<id>/plan.md
   Plan review:  .ai/runs/<id>/plan-review.md  (advisory; read before /donace:execute)

   Edit plan.md if needed (especially `implementer:` tags), then /donace:execute <id>
   ```

   If the plan review surfaced any `[P0]` findings, surface them inline so the user sees them without having to open the file.

## What `/donace:plan` is NOT

- Not a plan-review **gate** — codex's findings are advisory; the orchestrator does not block `/donace:execute` on them. The user reads `plan-review.md` and decides whether to edit `plan.md`.
- Not interactive after dispatch — the planner runs in isolation. If the user wants to clarify mid-plan, they cancel the agent (KillShell) and re-invoke with a refined spec.
- Not retryable per stage — the planner writes the whole plan in one shot.
