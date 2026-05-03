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

5. **Hand off.** Tell the user:

   ```
   Plan written. Edit .ai/runs/<id>/plan.md if needed (especially `implementer:` tags), then /donace:execute <id>
   ```

## What `/donace:plan` is NOT

- Not a plan reviewer — no codex auto-review of the plan, no AWAIT_APPROVAL state. The user reads plan.md and edits it. If they want a second opinion, they ask the main LLM directly.
- Not interactive after dispatch — the planner runs in isolation. If the user wants to clarify mid-plan, they cancel the agent (KillShell) and re-invoke with a refined spec.
- Not retryable per stage — the planner writes the whole plan in one shot.
