# Implementation Plan: Donace Skills — Plan & Execute Entry Points

## Overview

Add two user-invokable skills (`/donace:plan`, `/donace:execute`) that serve as the primary entry points to the donace orchestrator. The plan skill runs in the main conversation to support multi-turn Q&A with the user and writes `.ai/runs/<id>/plan.md` via the existing `write_plan` + `plan` commands. The execute skill dispatches the team-lead agent to drive the full run_job → verify → review → document → run_complete pipeline. Team-lead is refactored to focus on execution coordination only; classification and planning decisions move out of its responsibility.

## Stage 1: Plugin Skill Registration
**Goal**: Plugin manifest declares two new skills and the `skills/` directory exists as a scaffold, so that `/donace:plan` and `/donace:execute` are resolvable slash commands.
**Files to modify**: `.claude-plugin/plugin.json` (modify), `skills/plan/SKILL.md` (new, placeholder), `skills/execute/SKILL.md` (new, placeholder)
**Dependencies**: None
**Has user-facing changes**: Yes (new slash commands)
**Estimated turns**: 3
**Success Criteria**:
- `plugin.json` contains a top-level `"skills"` array listing `"skills/plan/SKILL.md"` and `"skills/execute/SKILL.md"`.
- Both `SKILL.md` files exist with valid YAML frontmatter (`name`, `description`).
- `python3 -c 'import json; json.load(open(".claude-plugin/plugin.json"))'` succeeds.
- After plugin reload, `/donace:plan` and `/donace:execute` appear in the skill list (manual verification — user reloads Claude Code or runs `/plugin reload`).
**Tests**:
- JSON validity of `plugin.json` (script above).
- Each SKILL.md has frontmatter parseable as YAML with required keys.
**Status**: Not Started

## Stage 2: `/donace:plan` Skill Content
**Goal**: The plan skill drives a multi-turn planning session that (1) checks for resumable prior runs, (2) gathers requirements via Q&A, (3) runs `run_start` + `write_plan` + `plan`, (4) presents plan status to the user, and (5) ends without executing.
**Files to modify**: `skills/plan/SKILL.md` (modify — replace placeholder with full content)
**Dependencies**: Stage 1
**Has user-facing changes**: Yes
**Estimated turns**: 8
**Success Criteria**:
- Skill frontmatter `description` includes the trigger phrase `/donace:plan` and mentions "planning session for donace".
- Skill body has 7 numbered steps matching the flow in "Skill body outline" below.
- Steps invoke orchestrator commands using the **absolute path** form (`python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" <cmd>`), matching team-lead's convention.
- Step 1 instructs the skill to run `list_runs --state incomplete` and offer resume if a prior run has `plan_status=REVIEW` or `plan_done=false`.
- Step 2 uses `AskUserQuestion` for up to 3 clarifying questions when the user brief is vague, with a checklist of what must be concrete before proceeding (target files, expected behavior, success criteria).
- Step 5 reads `.ai/runs/<id>/plan.json` and branches on `codex_review.status`: PASS → handoff message; REVIEW → show findings and loop back to Step 4 with a revision brief.
- Step 7 explicitly does NOT execute the plan — tells the user to invoke `/donace:execute <run-id>` separately.
**Tests**:
- Manual end-to-end: invoke `/donace:plan`, provide a vague task ("add a feature"), verify skill asks clarifying questions before proceeding.
- Manual revision: after a PASS plan, invoke `/donace:plan` again with the same run-id context — verify it detects the existing run and offers revision path.
- grep skill body for required markers: `list_runs`, `AskUserQuestion`, `write_plan`, `plan.json`, `codex_review`, `/donace:execute`.
**Status**: Not Started

### Skill body outline (Step 2 implementation target)

```
Step 1: Pre-flight
  - Run: python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" health --cwd <project>
  - Run: python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" list_runs --cwd <project> --state incomplete
  - If an incomplete run has plan_done=false OR plan_status=REVIEW, ASK user: "Resume run <id> or start fresh?"
  - If resume: skip to Step 4 with existing run-id.

Step 2: Gather requirements (multi-turn Q&A in main conversation)
  - Read the user's task brief.
  - If brief is vague (no target files, no success criteria, no constraints), use AskUserQuestion to ask up to 3 questions from:
      * "Which files/modules should this touch?"
      * "What is the observable success condition?"
      * "Any constraints — perf, security, compat?"
  - Synthesize a one-paragraph brief. Show it to the user: "Here's what I'll plan: <brief>. Proceed?"
  - Do NOT go to Step 3 until user confirms.

Step 3: Start run
  - Generate run-id (8-char hex).
  - Start dashboard if not running (background):
      python3 "${CLAUDE_PLUGIN_ROOT}/sdk/dashboard.py" --port 8741 &
  - Run: python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" run_start --run-id <id> --cwd <project>

Step 4: Dispatch planner (background)
  - Run in background: python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" write_plan \
      --run-id <id> --cwd <project> --task "<brief>"
  - Wait for completion notification.
  - Read .ai/runs/<id>/plan.md — show user the stage list for awareness.

Step 5: Codex plan review (background)
  - Run in background: python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" plan \
      --run-id <id> --cwd <project>
  - Wait for completion notification.
  - Read .ai/runs/<id>/plan.json — check codex_review.status.

Step 6: Branch on review status
  - If status == "PASS": print plan path + "Run /donace:execute <run-id> when ready."
  - If status == "REVIEW":
      * Show codex_review.findings to user.
      * Ask: "Address these findings — what changes?"
      * Loop to Step 4 with revised brief (write_plan auto-detects existing plan.md and treats new --task as revision).

Step 7: Handoff
  - Skill ends. Do not dispatch run_job. User must explicitly call /donace:execute.
```

## Stage 3: `/donace:execute` Skill Content
**Goal**: The execute skill resolves a run-id, validates the plan is PASS, and dispatches team-lead with a minimal execution directive. Team-lead drives the pipeline to `run_complete`.
**Files to modify**: `skills/execute/SKILL.md` (modify — replace placeholder with full content)
**Dependencies**: Stage 1
**Has user-facing changes**: Yes
**Estimated turns**: 6
**Success Criteria**:
- Skill frontmatter `description` includes the trigger phrase `/donace:execute` and mentions "execute a donace plan end-to-end".
- Skill body has 4 numbered steps (resolve run-id → validate → dispatch → report).
- Step 1 accepts an optional positional run-id argument; if absent, runs `list_runs --state incomplete` and picks the newest run where `plan_done=true` and `jobs_completed.plan == "PASS"`. If no such run exists, aborts with message: "No executable plan found — run /donace:plan first."
- Step 2 reads `.ai/runs/<id>/plan.json` and aborts if `codex_review.status != "PASS"` with message "Plan needs revision — run /donace:plan again."
- Step 3 uses the Agent tool with `subagent_type: "team-lead"` and a prompt containing: run-id, plan.md absolute path, directive to run `run_job → verify → review → document → run_complete` serially with `run_in_background: true` for each long command, and instruction to use `AskUserQuestion` only for blockers.
- Step 4 summarizes team-lead's return value: stages passed/blocked, final run status.
**Tests**:
- Manual: after a successful `/donace:plan`, invoke `/donace:execute` (no arg), verify auto-detection picks the right run.
- Manual: `/donace:execute <bad-run-id>` aborts cleanly with the no-plan message.
- Manual: `/donace:execute <run-id-with-REVIEW-plan>` aborts with the needs-revision message.
- grep skill body for required markers: `list_runs`, `plan.json`, `codex_review.status`, `subagent_type: "team-lead"`, `run_complete`.
**Status**: Not Started

### Execute skill body outline

```
Step 1: Resolve run-id
  - If $1 provided, use it.
  - Else: python3 "${CLAUDE_PLUGIN_ROOT}/sdk/orchestrator.py" list_runs --cwd <project> --state incomplete
    Parse output, filter where plan_done=true AND jobs_completed.plan == "PASS".
    Pick newest by run-id (hex-sorted or ctime). If none, abort.

Step 2: Validate plan status
  - Read .ai/runs/<id>/plan.json.
  - Assert codex_review.status == "PASS". If not, abort with revision message.

Step 3: Dispatch team-lead
  - Use Agent tool:
      subagent_type: team-lead
      description: "Execute donace run <id>"
      prompt: |
        Execute run-id=<id> to completion. Plan: .ai/runs/<id>/plan.md (status PASS).

        Pipeline: run_job (serial, respect dependencies) → verify (test,codex)
        → review (if ≥5 files changed) → document (if user-facing) → run_complete.

        Use run_in_background: true for all long commands.
        Use AskUserQuestion ONLY for blockers (BLOCKED stage, unresolved codex findings, ambiguous fix direction).
        Return: summary of stages_passed/stages_blocked and final run state.

Step 4: Report
  - When team-lead returns, print its summary verbatim + final run state from list_runs.
```

## Stage 4: Refactor `agents/team-lead.md` to Execution-Only
**Goal**: Team-lead's instructions describe execution coordination (resume + run_job loop + wrap phase + run_complete) without the classification/planning decision tree, since skills now own the plan phase and the entry point.
**Files to modify**: `agents/team-lead.md` (modify)
**Dependencies**: Stage 2, Stage 3
**Has user-facing changes**: No (internal agent prompt)
**Estimated turns**: 6
**Success Criteria**:
- Header paragraph rewritten to state team-lead is "invoked by `/donace:execute` with a validated plan to drive run_jobs → verify → review → document → run_complete".
- "Step 2: Classify the Request" section deleted (classification now belongs to the skills).
- "Step 3: Execute" → "Complex Task Flow" steps 1–3 (write plan, validate plan, present to user) deleted — these are the plan skill's job.
- Remaining kept: Step 0 resume logic, run_job loop (step 4), failure handling (step 5), verify/review/document (step 6), run_complete (step 7).
- New opening note: "If dispatched without a run-id, fall back to Step 0 resume logic to find the right run. If dispatched with a run-id and plan is PASS, skip Step 0 and start at Step 4."
- "Direct edit vs orchestrator" section deleted (trivial-change path doesn't apply when dispatched from execute skill).
- Tools list in frontmatter unchanged.
**Tests**:
- grep `agents/team-lead.md` for forbidden phrases (should NOT appear): `"Classify the Request"`, `"Trivial change"`, `"Direct edit vs orchestrator"`, `"Step 1: Start Dashboard"` as standalone.
- grep for required phrases (should appear): `"/donace:execute"`, `"validated plan"`, `"run_complete"`, `"AskUserQuestion"`.
- Manual: dispatch team-lead with a test run-id, verify it skips to run_job loop without re-asking about classification.
**Status**: Not Started

## Stage 5: Update README Entry Points
**Goal**: Top-level docs describe the new user-facing flow (`/donace:plan` → review → `/donace:execute`) and team-lead's revised role, so a new user sees the skill entry points first.
**Files to modify**: `README.md` (modify)
**Dependencies**: Stage 4
**Has user-facing changes**: Yes (docs)
**Estimated turns**: 4
**Success Criteria**:
- `README.md` "Usage" (or equivalent top section) shows a 3-step flow: `/donace:plan <task>` → user reviews `.ai/runs/<id>/plan.md` → `/donace:execute <run-id>`.
- A short "Revision" subsection in README explains: re-invoke `/donace:plan` on the same run to revise (write_plan auto-detects).
- The agents table in README reflects team-lead's new role (execution coordinator dispatched by `/donace:execute`), not the old "orchestrator — coordinates the full workflow" wording.
- Any old instructions that tell the user to invoke team-lead directly, or that describe the full pipeline as team-lead's single responsibility, are removed or redirected to the skills.
- **REFERENCE.md intentionally not modified** — it is landscape research comparing donace to similar projects (mission-control, LangGraph, etc.), not an API/skills reference. An earlier draft of this plan incorrectly scoped a "Skills section" edit into REFERENCE.md; removed.
**Tests**:
- grep `README.md` for `/donace:plan` and `/donace:execute` (must exist).
- grep `README.md` for outdated phrases like "team-lead is the entry point" or "Orchestrator — coordinates the full workflow" (must NOT exist, unless qualified as legacy).
- Read-through sanity check: does the flow make sense to a first-time user?
**Status**: Not Started

---

## Out of Scope (Explicit)

- **Judges / scoring layer** — Tomacco's post-commit LLM judges (qa/security/architect scoring) are *not* part of this plan. They would be a future additive stage on top of run_job, independent of the skill refactor.
- **Separate `/donace:revise` skill** — revision is handled by re-invoking `/donace:plan` on the same run-id, leveraging `write_plan`'s existing revision-brief detection. No new skill needed.
- **PRD-style schema validation** — Tomacco uses `prd.schema.json` to constrain plan structure. The donace `plan` command's regex parser already enforces the plan.md template; no schema file is added.
- **Orchestrator command changes** — `run_start`, `write_plan`, `plan`, `run_job`, etc. are unchanged. Skills are pure composition over the existing command surface.

## Sequencing & Bisect Discipline

- Stage 1 (scaffold) separate from Stage 2/3 (content) so manifest changes can be reverted independently.
- Stage 4 (team-lead refactor) depends on Stages 2 & 3 so that if we revert the skills, team-lead still works standalone.
- Stage 5 (docs) last — docs describe final shape of the system.
- Each stage is one self-contained commit with message `[stage-N] <stage-name>`.
