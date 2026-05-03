---
name: donace-chat
description: Brainstorm a feature with the user and produce a free-form spec.md under .ai/runs/<id>/. Use this when the user wants to start a new donace run from an idea (not from an existing plan). The skill is conversational — main LLM stays inline with the user.
---

# /donace:chat

Brainstorm a feature with the user and write a free-form spec to `.ai/runs/<id>/spec.md`. This is the entry point for a new run.

## Inputs

None. (v0 has no chat continuity — every invocation starts a fresh run.)

## Flow

1. **Mint a run-id.** Run from project root:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/sdk/cli.py" run_start
   ```

   Stdout returns the new run-id (e.g. `run-a1b2c3d4`) plus the absolute path of the run directory. Show this to the user.

2. **Brainstorm.** Have an open-ended dialogue with the user. Ask questions one at a time. Refine the idea — purpose, constraints, success criteria, scope. Don't gate; let the user signal readiness.

3. **Watch for closure signals.** When the user says something like "ok let's plan", "looks good", "go ahead", "that's it", treat it as the cue to write the spec.

4. **Write the spec.** Save to `.ai/runs/<id>/spec.md`. Free-form prose; no required sections. Capture WHAT they want and WHY (not HOW — that's plan.md's job). Aim for 30–200 lines depending on scope.

5. **Hand off.** Tell the user:

   ```
   Spec at .ai/runs/<id>/spec.md. Next: /donace:plan <id>
   ```

## What `/donace:chat` is NOT

- Not a planner — don't decompose into stages here.
- Not a reviewer — don't critique their idea unless they ask.
- Not a continuation — v0 has no resume. Each invocation is a fresh run-id.
- Not a code-reader — don't dive into the repo unless the user is asking specifically about it; the planner subagent does codebase exploration during /donace:plan.
