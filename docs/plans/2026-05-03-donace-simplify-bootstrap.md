# donace simplify — bootstrap implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land donace v0 on the `simplify` branch — three skills (`/donace:chat`, `/donace:plan`, `/donace:execute`), three subagents (planner, implementer, reviewer) with stack-specific review checklists, codex shell helper, CLI with ASCII pipeline view, and a rewritten project contract.

**Architecture:** Main LLM (in the user's Claude Code session) orchestrates the run; three subagents do focused work via Agent dispatch; codex-as-impl/reviewer goes through `sdk/codex_call.py` which wraps `codex-companion.mjs` task → status → result. Per-stage auto-commit with `pre_stage_sha` capture, clean-worktree preflight, `interrupted`/`blocked` stop-state distinction.

**Tech Stack:** Python 3 stdlib only (no pytest, no new deps), markdown for skills + agents, JSON for state, codex-companion.mjs (existing openai-codex plugin), Claude Code Task/Bash background dispatch primitives.

**Source of truth:** [docs/superpowers/specs/2026-05-02-donace-simplify-design.md](../specs/2026-05-02-donace-simplify-design.md). When this plan says "per spec", look up the section there for the full contract.

**Bootstrap constraint:** This plan is executed manually (main LLM + Task tool) because donace itself doesn't yet exist on `simplify`. Per spec migration order: all bootstrap stages use `implementer: claude` (no codex_call.py until Task 9 lands); reviewer is main LLM (manual diff read) until Task 7 lands the reviewer subagent, after which subsequent tasks can dispatch it.

---

## File structure (locked decisions)

**Created (new):**
- `skills/chat/SKILL.md` (Task 2)
- `skills/plan/SKILL.md` (Task 3)
- `skills/execute/SKILL.md` (Task 4)
- `agents/reviewer.md` (Task 7)
- `agents/references/review-checklist-{python,typescript,ios,general}.md` (Task 7, 4 files)
- `agents/prompts/codex-implementer.md` (Task 8)
- `agents/prompts/codex-reviewer.md` (Task 8)
- `sdk/__init__.py`, `sdk/tests/__init__.py` (Task 9, package markers)
- `sdk/codex_call.py` (Task 9–10)
- `sdk/cli.py` (Task 11–13)
- `sdk/tests/test_codex_call.py` (Task 9–10)
- `sdk/tests/test_run_start.py` (Task 11)
- `sdk/tests/test_plan_parser.py` (Task 12)
- `sdk/tests/test_execute_test_commands.py` (Task 12)
- `sdk/tests/test_stage_status_render.py` (Task 13)
- `CLAUDE.md` (Task 14)

**Modified:**
- `.claude-plugin/plugin.json` (Task 1)
- `agents/planner.md` (Task 5, full rewrite)
- `agents/implementer.md` (Task 6, full rewrite)
- `README.md` (Task 14)

**Deleted (Task 1):**
- `agents/architect.md`
- `agents/ios-reviewer.md`
- `agents/runtime-evaluator.md`
- `agents/team-lead.md`
- `agents/templates/` (whole directory)
- `agents/test-engineer.md`
- `agents/typescript-reviewer.md`

**Untouched (kept from main):**
- `agents/qa.md` (standalone ad-hoc tool)
- `agents/ui-designer.md` (standalone ad-hoc tool)
- `LICENSE`

---

## Task 1: Plugin scaffolding + remove unused agents

**Files:**
- Modify: `.claude-plugin/plugin.json`
- Delete: 6 agent files + `agents/templates/`

- [ ] **Step 1: Verify clean baseline**

```bash
cd /Users/minghaojiang/Developer/donace
git status --short
```

Expected: empty output (no uncommitted changes). If anything is dirty, stop and resolve.

- [ ] **Step 2: Delete unused agents**

```bash
rm agents/architect.md agents/ios-reviewer.md agents/runtime-evaluator.md \
   agents/team-lead.md agents/test-engineer.md agents/typescript-reviewer.md
rm -rf agents/templates
ls agents/
```

Expected output:
```
implementer.md  planner.md  qa.md  ui-designer.md
```

- [ ] **Step 3: Rewrite plugin.json**

Write `.claude-plugin/plugin.json`:

```json
{
  "name": "donace",
  "version": "0.2.0",
  "description": "Plan / execute / review pipeline orchestrating Claude and Codex; per-stage worker selection, P0 review gating with retries, ASCII pipeline view.",
  "author": {
    "name": "Minghao Jiang"
  },
  "skills": [
    "skills/chat",
    "skills/plan",
    "skills/execute"
  ],
  "agents": [
    "agents/planner.md",
    "agents/implementer.md",
    "agents/reviewer.md",
    "agents/qa.md",
    "agents/ui-designer.md"
  ]
}
```

Note: `agents/reviewer.md` is referenced here even though it doesn't exist yet. Task 7 creates it. Plugin parser will warn during Tasks 1–6; that's expected and intentional — the manifest is the contract, file lands later.

- [ ] **Step 4: Commit**

```bash
git add .claude-plugin/plugin.json agents/
git commit -m "Cut unused agents; switch plugin manifest to skills + 5 retained agents"
```

---

## Task 2: `/donace:chat` skill

**Files:**
- Create: `skills/chat/SKILL.md`

- [ ] **Step 1: Write the skill file**

Write `skills/chat/SKILL.md`:

```markdown
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
```

- [ ] **Step 2: Verify file exists with expected sections**

```bash
test -f skills/chat/SKILL.md && grep -c "^## " skills/chat/SKILL.md
```

Expected: outputs `3` (Inputs / Flow / What `/donace:chat` is NOT — counting `##` headers only).

- [ ] **Step 3: Commit**

```bash
git add skills/chat/SKILL.md
git commit -m "Add /donace:chat skill: brainstorm → spec.md"
```

---

## Task 3: `/donace:plan` skill

**Files:**
- Create: `skills/plan/SKILL.md`

- [ ] **Step 1: Write the skill file**

Write `skills/plan/SKILL.md`:

```markdown
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

   Write the implementation plan to .ai/runs/<id>/plan.md per the planner contract
   in docs/superpowers/specs/2026-05-02-donace-simplify-design.md (section
   "Planner subagent contract"). When done, reply with a short confirmation
   summary like "Plan written: 5 stages, 3 claude / 2 codex".
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
```

- [ ] **Step 2: Verify file**

```bash
test -f skills/plan/SKILL.md && grep -c "^## " skills/plan/SKILL.md
```

Expected: `3`.

- [ ] **Step 3: Commit**

```bash
git add skills/plan/SKILL.md
git commit -m "Add /donace:plan skill: dispatch planner subagent → plan.md"
```

---

## Task 4: `/donace:execute` skill

**Files:**
- Create: `skills/execute/SKILL.md`

This is the longest skill. It encodes the 15-step execute flow plus the rate-limit, interrupted-vs-blocked, and resume semantics from spec.

- [ ] **Step 1: Write the skill file**

Write `skills/execute/SKILL.md`:

```markdown
---
name: donace-execute
description: Run the execute loop on a plan.md — for each stage, dispatch implementer (Claude or Codex), run tests, dispatch reviewer (the opposite model), retry on P0 or test failure, commit on PASS. Stays interactive: long-running workers run in background, the main LLM responds to the user between dispatches.
---

# /donace:execute

Iterate the stages of `.ai/runs/<id>/plan.md`, dispatching implementer + reviewer per stage with the contracts in [the spec](../../docs/superpowers/specs/2026-05-02-donace-simplify-design.md) sections "/donace:execute skill" and "Reviewer severity contract".

## Inputs

- `<run-id>`: required.

## Preflight

1. **Worktree state.** Decide path:
   - **Fresh run** (no `stages/<sid>/status.json` files yet): `git status --porcelain` MUST be empty. If dirty, stop and tell the user to commit/stash first.
   - **Resume** (some stage has `status.json` with status `running` / `interrupted` / `blocked`): allow dirty worktree IF it's the partial work of an `interrupted` stage. Verify HEAD still matches that stage's saved `pre_stage_sha`. If HEAD diverged (unrelated commit, branch switch), mark the stage `blocked` with reason `resume baseline mismatch` and stop.

2. **Read** `.ai/runs/<id>/plan.md`. Use:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/sdk/cli.py" parse_plan --run-id <id>
   ```

   This emits stages as JSON to stdout. If parse fails, surface error to user; they edit plan.md and retry.

## Per-stage loop

For each stage in plan order:

### 1. Skip-or-resume gate

- `status.json.status == "passed"` → skip, advance to next stage.
- `status.json.status == "interrupted"` → resume in-place: keep `pre_stage_sha` and `retry_count` from disk; flip `status` back to `running`. Do NOT recapture `pre_stage_sha`.
- Otherwise (no status.json, or status `running`/`blocked`) → fresh entry: capture `pre_stage_sha = git rev-parse HEAD`, write `status.json: {status: "running", retry_count: 0, pre_stage_sha: <sha>}`.

### 2. Dispatch implementer (background)

Build payload:

```
run-id: <id>
stage-id: <sid>
cwd: <abs project root>

stage block:
---
<the stage's plan.md section verbatim>
---

retry context (if retry_count > 0):
---
<previous failure: failing test output, or P0 findings, depending on which gate triggered the retry>
---
```

If `stage.implementer == "claude"`:

```
Agent(
  subagent_type=implementer,
  prompt=payload,
  run_in_background=true,
)
```

If `stage.implementer == "codex"`:

```bash
Bash(
  command="python3 \"${CLAUDE_PLUGIN_ROOT}/sdk/codex_call.py\" implement --run-id <id> --stage-id <sid>",
  run_in_background=true,
)
```

### 3. Wait for implementer + rate-limit dispatch

When the worker completes:

- **Codex exit 2** OR **Agent surfaces RateLimitError**: write `status.json: {status: "interrupted", reason: "rate_limited"}`, stop the run, report to user. Resume preserves partial work; **does not consume a retry**.
- **Codex exit 1** OR **Agent reports error**: this counts as a retry. Goto step 7 (retry handling).
- **Codex exit 0** OR **Agent reports success**: proceed to step 4.

### 4. Run stage's `tests:` commands

For each bullet in `stage.tests`:

- If bullet starts with `none:` (e.g. `none: docs-only stage`), skip and record the marker.
- Otherwise, run from repo root:

  ```bash
  Bash(command="<bullet contents>", run_in_background=false, timeout=120000)
  ```

Capture exit code, stdout, stderr (truncated to last 2000 chars each). Append to `.ai/runs/<id>/stages/<sid>/test-results.md` in this format:

```markdown
# Test results: <sid> (attempt <retry_count + 1>)

## Command 1
$ <bullet command>
exit: <code>
stdout (last 2000):
<...>
stderr (last 2000):
<...>

## Command 2
...
```

### 5. Tests-failed gate

If any runnable bullet returned non-zero exit:

- Build retry context from the first failing bullet's command + last 30 lines of stderr/stdout.
- Goto step 7 (retry handling).

Else proceed to step 6.

### 6. Diff capture + persist

```bash
git diff <pre_stage_sha> > .ai/runs/<id>/stages/<sid>/diff.patch
```

If the diff is empty, treat as P0: implementer made no changes despite a passing test (probably a no-op stage that should have been `none:` only). Goto step 7 with `reason="empty diff"`.

### 7. Stack detection

Read `.ai/runs/<id>/stages/<sid>/diff.patch`. Look at file extensions:
- Any `.py` → `python`
- Else any `.ts`/`.tsx`/`.js`/`.jsx` → `typescript`
- Else any `.swift`/`.m`/`.mm` (or `.h` colocated with `.swift`/`.m`) → `ios`
- Else → `general`

Read `agents/references/review-checklist-<stack>.md` from `${CLAUDE_PLUGIN_ROOT}`.

### 8. Dispatch reviewer (opposite model, background)

Reviewer = opposite of `stage.implementer`. Build payload:

```
run-id: <id>
stage-id: <sid>

stage block: <verbatim>
diff: <contents of diff.patch>
test-results: <contents of test-results.md>
stack checklist: <contents of references/review-checklist-<stack>.md>

severity rubric: <inline echo of P0/P1/P2 contract from spec>

Produce a review document per the spec's "Reviewer output format" section.
```

If reviewer is `claude` (implementer was codex):

```
Agent(subagent_type=reviewer, prompt=payload, run_in_background=true)
```

If reviewer is `codex` (implementer was claude):

```bash
Bash(
  command="python3 \"${CLAUDE_PLUGIN_ROOT}/sdk/codex_call.py\" review --run-id <id> --stage-id <sid> --diff-file .ai/runs/<id>/stages/<sid>/diff.patch --test-results-file .ai/runs/<id>/stages/<sid>/test-results.md --stack <stack>",
  run_in_background=true,
)
```

### 9. Wait for reviewer; rate-limit and error dispatch

Same model as step 3:
- Rate-limit → `interrupted: rate_limited`, stop, no retry consumed.
- Error (exit 1, parse fail, etc.) → counts as retry. Goto step 7.
- Success → got a review document. Save it to `.ai/runs/<id>/stages/<sid>/review.md` immediately (so it's visible to the user even if the stage later retries).

### 10. Parse [P0]/[P1]/[P2] markers

Scan review.md for headers `### [P0] ...` / `### [P1] ...` / `### [P2] ...` (start of line). Count each.

If P0 count >= 1: build retry context from the [P0] block contents. Goto step 7.

Else: proceed to step 11.

### 7'. Retry handling (referenced by steps 3, 5, 6, 9, 10)

- Increment `retry_count` in `status.json`.
- If `retry_count > 2`: write `status.json: {status: "blocked", reason: <one of: "tests failed", "P0 unresolved", "worker errored", "empty diff", ...>}`. Stop the run. Tell the user.
- Else: feed retry context (the failure detail) into the next implementer dispatch; goto step 2.

### 11. Stage PASS

- `git add -A && git commit -m "[<sid>] <stage name>"` — commit ALL working-tree changes from this stage. (`stage.files` is a hint, not a commit boundary.)
- Write `status.json: {status: "passed"}`. Preserve `retry_count` for the record.
- The reviewer's final document already lives at `review.md` (saved in step 9); it contains only [P1]/[P2] by definition since any [P0] would have triggered retry.
- Advance to the next stage.

## Run completion

After all stages reach `passed`:

```bash
# write meta.json status: completed
python3 "${CLAUDE_PLUGIN_ROOT}/sdk/cli.py" mark_completed --run-id <id>
```

Tell the user:

```
Run <id> complete: <N> stages passed in <M> total tries.
View pipeline: donace stage_status <id>
```

## User interruption during execution

The main LLM never blocks while a worker runs. Common interactions:

- **"what's stage 3 doing"** → Read `.ai/runs/<id>/stages/stage-3/status.json` and tail BashOutput for the active subprocess. Report inline.
- **"kill stage 3"** → KillShell on the active background process; write `status.json: {status: "interrupted", reason: "user_interrupted"}`; preserve worktree for resume. Stop the run.
- **"edit plan and restart"** → Stop the current dispatch. Tell the user to decide first whether to **keep partial work** (do nothing; resume picks it up) OR **discard partial work** (`git checkout -- .` to reset tracked files to `pre_stage_sha`, then `rm -f .ai/runs/<id>/stages/<sid>/status.json` so resume restarts the stage fresh). Then they edit `plan.md` and re-invoke `/donace:execute <id>`.

## What `/donace:execute` is NOT

- Not a one-shot — it's re-entrant. If the session ends or the user interrupts, re-running picks up at the first non-`passed` stage.
- Not parallel — stages run serially in v0 (worktree parallelism is v1).
- Not auto-recovering from `blocked` — `blocked` requires user action (edit plan, fix env, etc.) before resume.
- Not running in its own process group — workers are children of the main LLM's session and die if the user closes Claude Code (this is the v2 detach scope, deliberately out of v0).
```

- [ ] **Step 2: Verify file**

```bash
test -f skills/execute/SKILL.md && grep -c "^## " skills/execute/SKILL.md
```

Expected: `6` (Inputs, Preflight, Per-stage loop, Run completion, User interruption during execution, What `/donace:execute` is NOT).

- [ ] **Step 3: Commit**

```bash
git add skills/execute/SKILL.md
git commit -m "Add /donace:execute skill: 15-step loop with interrupted/blocked semantics"
```

---

## Task 5: Planner subagent rewrite

**Files:**
- Modify: `agents/planner.md` (full replacement)

- [ ] **Step 1: Verify current state**

```bash
head -10 agents/planner.md
```

Expected: shows the OLD product-spec-writing planner. We replace.

- [ ] **Step 2: Write new agents/planner.md**

```markdown
---
name: planner
description: Convert spec.md into an implementation plan.md with sequential file-bounded stages, each tagged with files, success criteria, runnable tests, and per-stage implementer (claude or codex). Used only by /donace:plan.
tools: ["Read", "Grep", "Glob", "Bash", "Write"]
model: opus
---

# Planner

You take a free-form `spec.md` and produce a structured `plan.md` for `/donace:execute` to run. You are dispatched by `/donace:plan` once per run; your output IS the plan.

## Your context (passed in the dispatching prompt)

- `run-id`: e.g. `run-a1b2c3d4`
- `cwd`: absolute path of the project root
- `spec.md` contents

## Your output

Write the plan to `<cwd>/.ai/runs/<run-id>/plan.md` using the Write tool. After writing, reply with a short confirmation summary like:

> Plan written: 5 stages, 3 claude / 2 codex. Stack: python.

## Plan format

```markdown
# Plan: <feature name from spec>

## Stage 1: <short name>
- implementer: claude
- goal: <one-sentence intent>
- files: src/foo.py, src/foo_test.py
- success criteria:
  - <criterion 1>
  - <criterion 2>
- tests:
  - python3 -m unittest src.foo_test -v

## Stage 2: <short name>
- implementer: codex
- goal: ...
- files: ...
- success criteria:
  - ...
- tests:
  - none: mechanical rename; existing suite covers behavior
```

Required per-stage fields: `implementer`, `goal`, `files`, `success criteria`, `tests`.

`implementer:` MUST be exactly `claude` or `codex`. Anything else fails parsing.

`tests:` is a list of concrete shell commands run from the repo root. If no meaningful automated test exists for a stage, write an explicit `none: <reason>` marker. Don't leave the field empty or vague.

## How to decompose

1. **Read the codebase first.** Use `Read`, `Grep`, `Glob`, and `Bash` (e.g. `git log -- <path>`, `wc -l <path>`, `find ... -name ...`) to understand existing structure. Don't propose stages that fight the grain of the repo.

2. **Stages are sequential.** Each stage is a single bisect-friendly commit. No parallelism in v0.

3. **Stages are file-bounded.** Each stage's `files:` lists the files it touches. Reviewer uses this to spot scope drift; the orchestrator does NOT enforce it (`git add -A` commits whatever the implementer wrote, and the reviewer flags out-of-scope writes as P0/P1).

4. **Pick `implementer:` per stage character.**

   | Stage character | Implementer |
   |---|---|
   | Mechanical refactor / batch rename / typed transforms | codex |
   | Algorithmic / dense logic / single-file dense impl | codex |
   | Cross-file judgment / needs Claude tools / context-heavy | claude |
   | Default when uncertain | claude |

5. **Tests must be runnable.** When you write a `tests:` bullet, it must be a real command that returns exit 0 on success. If you don't know the project's test command, look it up via Grep / Read on `package.json` / `pyproject.toml` / `Makefile` / etc. before writing the plan.

6. **Number of stages.** Aim for 3–8 stages for a typical feature. More than 10 is a smell — fold related stages or revisit decomposition. Fewer than 3 usually means you're not breaking it down enough for bisect-friendly commits.

## Hard rules

- Do NOT brainstorm or rewrite the spec. The user already did that with the main LLM. Your input is the spec; treat it as fixed.
- Do NOT include reviewer in the plan. Reviewer is implicit (opposite model from the implementer tag); the orchestrator derives it.
- Do NOT include `dependencies:`, `runtime verification:`, `estimated turns:` — those fields don't exist in v0.
- Do NOT execute any stage. You only produce the plan; `/donace:execute` runs it.
- Do NOT modify any file other than the plan.md you're writing.
```

- [ ] **Step 3: Verify file**

```bash
grep -c "^## " agents/planner.md && head -1 agents/planner.md
```

Expected: a count `≥ 5` (sections: Your context / Your output / Plan format / How to decompose / Hard rules) and the first line is `---` (frontmatter).

- [ ] **Step 4: Commit**

```bash
git add agents/planner.md
git commit -m "Rewrite planner: spec.md → structured plan.md with implementer tags"
```

---

## Task 6: Implementer subagent rewrite

**Files:**
- Modify: `agents/implementer.md` (full replacement)

- [ ] **Step 1: Write new agents/implementer.md**

```markdown
---
name: implementer
description: Implement a single stage of a donace plan. Self-directed, codebase-first; reads adjacent code to resolve ordinary ambiguity rather than asking. Writes code + tests directly to the working tree. Dispatched by /donace:execute, one stage at a time.
tools: ["Read", "Grep", "Glob", "Bash", "Write", "Edit"]
model: opus
---

# Implementer

You implement one stage at a time. The orchestrator gives you the stage block + an optional retry context, and you change the working tree to satisfy the stage's `success criteria` and `tests`.

## Your context (passed in the dispatching prompt)

- `run-id`, `stage-id`, absolute `cwd`
- The stage block from `plan.md` (goal, files, success criteria, tests)
- Optional retry context: failing test output OR a P0 finding from a previous attempt

## Your job

1. **Understand the stage.** Read the listed files and adjacent ones. Run `git log --oneline -- <path>` if you want recent history.

2. **Use test-driven development as a quality recommendation.** Write a failing test, run it, then write the minimal code to make it pass, then refactor. Strict TDD ordering is NOT enforced — the orchestrator runs the stage's `tests:` after you finish, and as long as those pass, you're fine. But: TDD usually produces better code with fewer regressions, so the default ask is to do it.

3. **Stay in scope.** Touch the files listed in `files:` first. If you discover you need to touch adjacent files, do it — `files:` is a hint, not a hard boundary, and the reviewer flags genuinely-out-of-scope writes as P0. Don't fight ordinary scope creep that's required to land the stage.

4. **Run tests as you go** (Bash: `python3 -m unittest ...`, `npm test`, etc.). The orchestrator will re-run them after, but you should not hand back a stage that fails its own listed tests.

5. **When done, reply** with a short text summary: what you changed, what tests now pass, any assumptions you made (briefly). The orchestrator uses this only as failure-surface context if your worktree changes don't actually satisfy the stage.

## Assumption discipline (replaces NEEDS_CONTEXT escalation)

When the stage is ambiguous, **don't escalate**. Do this instead:

- Read adjacent files for established patterns. Match them.
- Inspect call sites. Pick the interpretation that makes the most call sites work.
- Pick the most boring local-consistent interpretation. Document the assumption in your reply.
- Only treat something as a hard blocker (and fail) when it's a genuine stop:
  - A required credential is not in the environment and the stage assumes it
  - A file referenced by the stage is genuinely absent and there's no precedent for creating it
  - An external product decision is required (e.g. "should this respect feature-flag X?") and there's no local precedent

When you do hit a true blocker, explain it clearly in your text reply. There is no special protocol token (no `NEEDS_CONTEXT:`); just describe the situation. The orchestrator will treat it as a stage failure (consumes a retry; user intervenes after retry exhaustion).

## Hard rules

- Do NOT rewrite `plan.md`. If the stage is wrong, fail and explain in your reply; user will edit the plan and resume.
- Do NOT touch other stages' artifacts under `.ai/runs/<id>/stages/`.
- Do NOT commit. The orchestrator commits at PASS time.
- Do NOT skip the listed `tests:` — if a bullet command fails, your stage isn't done.
- Do NOT mass-rewrite unrelated parts of the codebase ("while I'm here..."). Surgical changes only.
```

- [ ] **Step 2: Verify file**

```bash
grep -c "^## " agents/implementer.md
```

Expected: `≥ 4` (Your context / Your job / Assumption discipline / Hard rules).

- [ ] **Step 3: Commit**

```bash
git add agents/implementer.md
git commit -m "Rewrite implementer: self-directed assumption discipline, no NEEDS_CONTEXT protocol"
```

---

## Task 7: Reviewer subagent + 4 stack checklists

**Files:**
- Create: `agents/reviewer.md`
- Create: `agents/references/review-checklist-python.md`
- Create: `agents/references/review-checklist-typescript.md`
- Create: `agents/references/review-checklist-ios.md`
- Create: `agents/references/review-checklist-general.md`

- [ ] **Step 1: Create references directory**

```bash
mkdir -p agents/references
```

- [ ] **Step 2: Write `agents/reviewer.md`**

```markdown
---
name: reviewer
description: Review a stage's diff + test results, output [P0]/[P1]/[P2] findings per the universal severity rubric and the injected stack-specific checklist. P0 gates the stage; P1/P2 are advisory. Dispatched by /donace:execute.
tools: ["Read", "Grep", "Glob", "Bash"]
model: opus
---

# Reviewer

You review one stage's diff + test results and produce a markdown review document.

## Your context (passed in the dispatching prompt)

- `run-id`, `stage-id`
- The stage block (goal, files, success criteria, tests)
- The diff (verbatim contents of `diff.patch`)
- The test-results (verbatim contents of `test-results.md`) — already collected by the orchestrator
- The stack-specific checklist (verbatim contents of `references/review-checklist-<stack>.md`)
- The universal severity rubric (echoed inline)

## Your output

Reply with a markdown document in this exact skeleton:

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

If no findings of a given severity, omit the corresponding `### [Px]` block. If no findings at all, `## Findings` reads `(none)`.

The orchestrator scans for `### [P0]` / `### [P1]` / `### [P2]` headers (start of line) to count findings per severity.

## Severity rubric (universal, takes precedence over checklist when they conflict)

**[P0] — must fix before stage passes**
- Stage's `success criteria` not met by the diff.
- A runnable command listed in the stage's `tests:` field fails (visible in the test-results you were given).
- The diff changes behavior that obviously needs automated coverage, but the stage provides only `none:` placeholders or omits the necessary test updates.
- Diff introduces a bug that produces incorrect behavior in normal use.
- Security regression: secret leak, command injection, SQL injection, XSS, auth bypass, sandbox escape.
- Data corruption or data-loss path.
- Breaking change to a public API contract not specified in the spec.
- Stage `files:` listed a file but the diff doesn't actually modify it.

**[P1] — advisory; should fix soon, not now**
- Code quality issue in the diff: deep nesting, unclear naming, duplication, dead code.
- Missing error handling for a plausible failure mode.
- Coverage gap: a code path in the diff isn't exercised by tests, even though listed tests pass.
- Clearly suboptimal complexity (e.g., O(n²) where O(n) is the obvious choice).
- Project-convention deviation in the diff (a clear pattern in adjacent files not followed).

**[P2] — nit; flag for awareness, no obligation**
- Naming preferences.
- Comment phrasing.
- Cosmetic refactor opportunities.
- Minor convention inconsistencies in non-load-bearing places.

## Scope rule

You flag things **on the diff only**. Pre-existing issues in untouched code are out of scope. If a P0 in the diff is symptomatic of a deeper architectural issue elsewhere, flag the symptom in the diff as P0 and note the architectural concern separately as P1 — do NOT escalate pre-existing code to P0.

## Hard rules

- Do NOT re-run the stage's `tests:` commands. The orchestrator already did that and the results are in your payload. Trust them.
- Do NOT propose or write code changes. Your output is findings; the implementer's job (on the next retry) is to address them.
- Do NOT modify any files. Your tools are Read/Grep/Glob/Bash for spot-checking only (verifying a file exists, looking at neighboring code).
```

- [ ] **Step 3: Write `agents/references/review-checklist-python.md`**

```markdown
# Python review checklist

Stack-specific items the reviewer should look for in Python diffs. Severity buckets here are advisory — the universal rubric in `reviewer.md` always takes precedence.

## P0 (block stage)
- `subprocess(..., shell=True)` with user-controlled input — command injection risk.
- `eval(...)` / `exec(...)` on data crossing a trust boundary.
- SQL string concatenation with user input (e.g., `f"SELECT * FROM x WHERE id = {user_id}"`); use parameterized queries instead.
- Missing or wrong `__init__.py` for a package that expects to be importable.
- Bare `assert` used as a runtime guard in production code (Python strips asserts under `-O`).

## P1 (advisory)
- Bare `except:` clauses (catches `KeyboardInterrupt`, `SystemExit`); use `except Exception:` or narrower.
- Mutable default arguments (`def f(x=[])`).
- Modifying a list while iterating over it.
- Returning `None` implicitly from a function whose other branches return values; either be explicit or restructure.
- Reaching into `_private` or `__dunder` attributes of another module.

## P2 (nit)
- `# type: ignore` without a comment explaining why.
- f-strings used for non-trivial logic that would be clearer as a helper.
- Trailing whitespace, missing newline at EOF (most repos auto-fix; flag only if the project doesn't).
- Commented-out code in the diff.
```

- [ ] **Step 4: Write `agents/references/review-checklist-typescript.md`**

```markdown
# TypeScript / JS review checklist

## P0 (block stage)
- Non-null assertion `!` on user-provided or network-derived value (e.g., `req.body.user!.id`).
- `as` cast of a network response or user input without runtime validation.
- `eval()` / `Function(...)` on data crossing a trust boundary.
- Direct DOM injection of user-supplied HTML without sanitization (XSS risk).
- Missing await on a promise whose rejection would surface as an unhandled rejection in production.

## P1 (advisory)
- `any` type used where a concrete type is feasible (especially function parameters and public API surfaces).
- `console.log` left in non-debug code paths.
- `// @ts-ignore` / `// @ts-expect-error` without a comment.
- Catch block that swallows the error silently.
- Use of `==` where `===` is the project default.

## P2 (nit)
- `let` used where `const` would suffice.
- Long functional chains where intermediate `const` would help readability.
- Comments stating what the code does instead of why.
```

- [ ] **Step 5: Write `agents/references/review-checklist-ios.md`**

```markdown
# iOS (Swift / Objective-C) review checklist

## P0 (block stage)
- Force-unwrap `!` of an Optional sourced from user input or network.
- Force-cast `as!` of a value that could be user-controlled or untrusted.
- Hardcoded API key / secret in source.
- Storing PII in `UserDefaults` (use Keychain for sensitive data).
- Networking off the main thread without back-pressure or cancellation.

## P1 (advisory)
- Implicitly Unwrapped Optional (`Type!`) on a stored property that isn't lazy.
- Long completion-handler chains that should be `async/await`.
- `print(...)` / `NSLog(...)` left in shipped code (use a logger).
- Strong reference cycle risk in closure captures (`self` not weakly captured).
- Force-unwrap of `Bundle.main.path(forResource:)` when the resource is optional in practice.

## P2 (nit)
- View controller code mixed with model logic that should be in a ViewModel.
- Magic numbers in layout constraints.
- Method names that don't match Swift API design guidelines.
```

- [ ] **Step 6: Write `agents/references/review-checklist-general.md`**

```markdown
# General review checklist (stack-agnostic)

Use this when the diff doesn't match a more specific stack (Python / TypeScript / iOS) or contains a mix.

## P0 (block stage)
- Hardcoded credential / secret in source.
- Logic that silently swallows errors that should propagate.
- Off-by-one in a loop bound that handles user-controlled length.
- Dropping a database column / breaking a public API contract not in the spec.

## P1 (advisory)
- Function longer than ~100 lines without internal structure.
- Duplication of a logic block that already exists nearby.
- Missing error handling for a plausible failure mode (e.g., the network call you just added has no timeout).
- Comment that states what the code does (the code already does that) but doesn't explain why.

## P2 (nit)
- Magic numbers in business logic.
- Commented-out code in the diff.
- Variable names that abbreviate beyond clarity (`u` for user, `cfg` for config — judge by neighbors).
- Inconsistency with project conventions in non-load-bearing places.
```

- [ ] **Step 7: Verify all 5 files**

```bash
ls -la agents/reviewer.md agents/references/
```

Expected: `reviewer.md` exists; `references/` contains 4 files.

- [ ] **Step 8: Commit**

```bash
git add agents/reviewer.md agents/references/
git commit -m "Add reviewer subagent + 4 stack-specific checklists (python/ts/ios/general)"
```

---

## Task 8: Codex prompt prefixes

**Files:**
- Create: `agents/prompts/codex-implementer.md`
- Create: `agents/prompts/codex-reviewer.md`

- [ ] **Step 1: Create prompts directory**

```bash
mkdir -p agents/prompts
```

- [ ] **Step 2: Write `agents/prompts/codex-implementer.md`**

```markdown
# Codex implementer prompt prefix

This text is prepended to the stage payload by `sdk/codex_call.py implement` before sending to codex via `codex-companion.mjs task --background --json`.

---

You are an implementer for the donace pipeline. You implement one stage at a time and stop.

Your context will follow this prefix and contains:
- `run-id`, `stage-id`, absolute `cwd`
- The stage block (goal, files, success criteria, tests)
- An optional retry context (failing test output or [P0] findings from a previous attempt)

Your job:

1. Understand the stage. Read the listed files and adjacent ones to match existing patterns.
2. Use test-driven development as a quality recommendation; the orchestrator runs the listed tests after you finish, and as long as those pass, your stage is done.
3. Stay in scope. Touch the files listed in `files:` first; treat `files:` as a hint, not a hard boundary. The reviewer flags out-of-scope writes as P0/P1.
4. Run tests as you go to catch issues before handing back.
5. When done, output a short summary at the end: what you changed, which tests pass, any assumptions you made.

Assumption discipline: when ambiguous, read adjacent code, match patterns, pick the most boring local-consistent interpretation. Don't ask the orchestrator — there is no escalation channel. Only fail when there's a true hard stop (missing credential, file truly absent, external product decision required).

Hard rules:
- Do NOT rewrite `plan.md`.
- Do NOT touch other stages' artifacts under `.ai/runs/<id>/stages/`.
- Do NOT commit; the orchestrator commits on PASS.
- Do NOT skip the listed `tests:`.
- Do NOT mass-rewrite unrelated code.

---
```

- [ ] **Step 3: Write `agents/prompts/codex-reviewer.md`**

```markdown
# Codex reviewer prompt prefix

This text is prepended to the review payload by `sdk/codex_call.py review` before sending to codex via `codex-companion.mjs task --background --json`.

---

You are a reviewer for the donace pipeline. You review one stage's diff + test results and produce a markdown review document.

Your context will follow this prefix and contains:
- `run-id`, `stage-id`
- The stage block (goal, files, success criteria, tests)
- The diff (`diff.patch` contents)
- The test results (`test-results.md` contents) — already collected by the orchestrator
- The stack-specific checklist (`references/review-checklist-<stack>.md` contents)
- The universal severity rubric (echoed inline below)

Output skeleton:

````markdown
# Review: stage-<sid>

## Summary
<one paragraph verdict>

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

If no findings of a given severity, omit that `### [Px]` block. If no findings at all, `## Findings` reads `(none)`.

The orchestrator scans for `### [P0]` / `### [P1]` / `### [P2]` headers (start of line) to count findings per severity.

Universal severity rubric (takes precedence over checklist when they conflict):

[P0] — stage's success criteria unmet, listed test fails, security regression, data-loss path, breaking unspecified API change, listed file not actually modified, behavior obviously needs tests but `tests:` is `none:`-only.

[P1] — code quality (deep nesting, unclear naming, duplication, dead code in the diff), missing error handling for plausible failure, coverage gap on diff path, clearly suboptimal complexity, project-convention deviation.

[P2] — naming preferences, comment phrasing, cosmetic refactor opportunities, minor convention nits.

Scope rule: flag things **on the diff only**. Pre-existing issues are out of scope; if a P0 symptom in the diff hints at deeper architecture issues elsewhere, flag the symptom P0 and the architecture concern as separate P1 — never escalate pre-existing code to P0.

Hard rules:
- Do NOT re-run the stage's `tests:` commands; the orchestrator already did and results are in your payload.
- Do NOT propose or write code changes.
- Do NOT modify any files.

---
```

- [ ] **Step 4: Verify**

```bash
ls -la agents/prompts/
```

Expected: 2 files (`codex-implementer.md`, `codex-reviewer.md`).

- [ ] **Step 5: Commit**

```bash
git add agents/prompts/
git commit -m "Add codex prompt prefixes for implementer + reviewer"
```

---

## Task 9: codex_call.py — package + happy path + tests

**Files:**
- Create: `sdk/__init__.py`
- Create: `sdk/tests/__init__.py`
- Create: `sdk/codex_call.py`
- Create: `sdk/tests/test_codex_call.py`

- [ ] **Step 1: Create package markers**

```bash
mkdir -p sdk/tests
: > sdk/__init__.py
: > sdk/tests/__init__.py
```

(Both files empty; they exist solely to make `sdk` and `sdk.tests` importable.)

- [ ] **Step 2: Write the failing happy-path test**

Write `sdk/tests/test_codex_call.py`:

```python
"""Tests for sdk.codex_call.

Mock subprocess invocations of codex-companion.mjs; verify the JSON output
contract on stdout and the exit-code contract.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk import codex_call


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class HappyPathTest(unittest.TestCase):
    def test_implement_completes_with_summary(self):
        # Sequence: task → status (running x1, completed x1) → result.
        responses = iter([
            _FakeCompletedProcess(stdout=json.dumps({"jobId": "j-123"})),
            _FakeCompletedProcess(stdout=json.dumps({"status": "running"})),
            _FakeCompletedProcess(stdout=json.dumps({"status": "completed"})),
            _FakeCompletedProcess(stdout=json.dumps({
                "finalMessage": "stage implemented; tests pass"
            })),
        ])

        def fake_run(*args, **kwargs):
            return next(responses)

        with patch.object(subprocess, "run", side_effect=fake_run):
            with patch.object(codex_call, "_locate_companion", return_value=Path("/fake/companion.mjs")):
                with patch.object(codex_call, "_sleep", lambda _: None):  # skip the 5s waits
                    out = codex_call.run(
                        mode="implement",
                        run_id="run-test",
                        stage_id="stage-1",
                        cwd=Path("/fake/project"),
                        diff_file=None,
                        test_results_file=None,
                        stack=None,
                    )

        self.assertEqual(out["status"], "completed")
        self.assertIn("stage implemented", out["summary"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the test, verify it fails**

```bash
python3 -m unittest sdk.tests.test_codex_call -v
```

Expected: ImportError or ModuleNotFoundError for `from sdk import codex_call` (file doesn't exist yet) — that's the "failing" state.

- [ ] **Step 4: Write `sdk/codex_call.py` with happy-path implementation**

```python
"""Subprocess wrapper around the openai-codex `codex-companion.mjs` script.

Two CLI modes (mirrors the spec section "Codex shell helper"):

    codex_call.py implement --run-id <id> --stage-id <sid>
    codex_call.py review    --run-id <id> --stage-id <sid> \
                            --diff-file <path> \
                            --test-results-file <path> \
                            --stack <python|typescript|ios|general>

Stdout JSON contract:
    {"status": "completed", "summary": "...", "raw_output": "..."}    # success
    {"status": "error", "error_class": "rate_limited|...", "message": "..."}

Exit codes:
    0 — success
    1 — retry-eligible error
    2 — rate_limited
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

_PLUGIN_CACHE = Path.home() / ".claude" / "plugins" / "cache" / "openai-codex" / "codex"
_POLL_INTERVAL_SEC = 5
_POLL_MAX_SEC = 600

# Sentinel substrings codex-companion's status response may surface for rate limit.
_RATE_LIMIT_HINTS = (
    "rate limit",
    "rate-limit",
    "429",
    "RateLimitError",
)


def _locate_companion() -> Path:
    """Find the most recent codex-companion.mjs under ~/.claude/plugins/cache/openai-codex."""
    if not _PLUGIN_CACHE.exists():
        raise FileNotFoundError(
            f"openai-codex plugin not found at {_PLUGIN_CACHE}. "
            "Install the plugin via /plugin install openai-codex/codex first."
        )
    candidates = sorted(_PLUGIN_CACHE.glob("*/scripts/codex-companion.mjs"))
    if not candidates:
        raise FileNotFoundError(
            f"No codex-companion.mjs under {_PLUGIN_CACHE}. Plugin may be partially installed."
        )
    return candidates[-1]  # most recent version


def _sleep(seconds: int) -> None:
    """Indirection so tests can stub it out."""
    time.sleep(seconds)


def _run_companion(companion: Path, args: list[str], cwd: Path) -> dict:
    """Invoke `node <companion> <args>` and parse stdout as JSON.

    Raises FileNotFoundError if node is missing, ValueError on parse failure.
    """
    proc = subprocess.run(
        ["node", str(companion), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"codex-companion exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"codex-companion stdout was not JSON: {proc.stdout[:500]}") from exc


def _build_prompt(
    mode: str,
    run_id: str,
    stage_id: str,
    cwd: Path,
    diff_file: Optional[Path],
    test_results_file: Optional[Path],
    stack: Optional[str],
) -> str:
    """Compose the prompt: prefix file + payload."""
    plugin_root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", cwd))

    if mode == "implement":
        prefix_path = plugin_root / "agents" / "prompts" / "codex-implementer.md"
    elif mode == "review":
        prefix_path = plugin_root / "agents" / "prompts" / "codex-reviewer.md"
    else:
        raise ValueError(f"Unknown mode: {mode}")

    prefix = prefix_path.read_text() if prefix_path.exists() else ""

    parts = [prefix, "", f"run-id: {run_id}", f"stage-id: {stage_id}", f"cwd: {cwd}"]

    plan_path = cwd / ".ai" / "runs" / run_id / "plan.md"
    if plan_path.exists():
        parts.extend(["", "plan.md (find your stage):", "---", plan_path.read_text(), "---"])

    if mode == "review":
        if diff_file and diff_file.exists():
            parts.extend(["", "diff:", "---", diff_file.read_text(), "---"])
        if test_results_file and test_results_file.exists():
            parts.extend(["", "test results:", "---", test_results_file.read_text(), "---"])
        if stack:
            checklist_path = plugin_root / "agents" / "references" / f"review-checklist-{stack}.md"
            if checklist_path.exists():
                parts.extend(["", f"stack checklist ({stack}):", "---", checklist_path.read_text(), "---"])

    return "\n".join(parts)


def run(
    *,
    mode: str,
    run_id: str,
    stage_id: str,
    cwd: Path,
    diff_file: Optional[Path],
    test_results_file: Optional[Path],
    stack: Optional[str],
) -> dict:
    """High-level: build prompt, launch task, poll, fetch result."""
    companion = _locate_companion()
    prompt = _build_prompt(mode, run_id, stage_id, cwd, diff_file, test_results_file, stack)

    # Launch background task.
    launch = _run_companion(
        companion,
        ["task", "--background", "--json", "--prompt", prompt, "--cwd", str(cwd)],
        cwd=cwd,
    )
    job_id = launch.get("jobId")
    if not job_id:
        raise RuntimeError(f"task launch returned no jobId: {launch}")

    # Poll status until terminal.
    elapsed = 0
    while elapsed < _POLL_MAX_SEC:
        status_resp = _run_companion(companion, ["status", job_id], cwd=cwd)
        st = status_resp.get("status")
        if st in ("completed", "failed", "cancelled"):
            break
        if st == "running":
            _sleep(_POLL_INTERVAL_SEC)
            elapsed += _POLL_INTERVAL_SEC
            continue
        # Unknown status — log and break to surface as error.
        break
    else:
        raise TimeoutError(f"codex job {job_id} did not finish within {_POLL_MAX_SEC}s")

    if st != "completed":
        raise RuntimeError(f"codex job {job_id} terminal status: {st}")

    # Fetch result.
    result = _run_companion(companion, ["result", job_id], cwd=cwd)
    final_msg = result.get("finalMessage") or ""
    if not final_msg.strip():
        raise ValueError("codex result had empty finalMessage")

    return {"status": "completed", "summary": final_msg, "raw_output": json.dumps(result)}


def main() -> int:
    parser = argparse.ArgumentParser(prog="codex_call")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_impl = sub.add_parser("implement")
    p_impl.add_argument("--run-id", required=True)
    p_impl.add_argument("--stage-id", required=True)

    p_rev = sub.add_parser("review")
    p_rev.add_argument("--run-id", required=True)
    p_rev.add_argument("--stage-id", required=True)
    p_rev.add_argument("--diff-file", required=True)
    p_rev.add_argument("--test-results-file", required=True)
    p_rev.add_argument("--stack", required=True, choices=["python", "typescript", "ios", "general"])

    args = parser.parse_args()

    cwd = Path.cwd()
    diff_file = Path(args.diff_file) if getattr(args, "diff_file", None) else None
    test_results_file = Path(args.test_results_file) if getattr(args, "test_results_file", None) else None
    stack = getattr(args, "stack", None)

    try:
        out = run(
            mode=args.mode,
            run_id=args.run_id,
            stage_id=args.stage_id,
            cwd=cwd,
            diff_file=diff_file,
            test_results_file=test_results_file,
            stack=stack,
        )
        print(json.dumps(out))
        return 0
    except Exception as exc:
        # Task 10 elaborates the error_class taxonomy + rate-limit detection.
        # Happy-path build only.
        print(json.dumps({"status": "error", "error_class": "worker_failed", "message": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the test, verify it passes**

```bash
python3 -m unittest sdk.tests.test_codex_call -v
```

Expected: `OK` (1 test passes).

- [ ] **Step 6: Commit**

```bash
git add sdk/__init__.py sdk/tests/__init__.py sdk/codex_call.py sdk/tests/test_codex_call.py
git commit -m "Add codex_call: happy-path task→status→result wrapper around codex-companion.mjs"
```

---

## Task 10: codex_call.py — error paths + rate-limit + tests

**Files:**
- Modify: `sdk/codex_call.py`
- Modify: `sdk/tests/test_codex_call.py`

- [ ] **Step 1: Add failing tests for error classes**

Append to `sdk/tests/test_codex_call.py`:

```python
class ErrorPathTest(unittest.TestCase):
    def _run_with_responses(self, responses_iter):
        def fake_run(*args, **kwargs):
            return next(responses_iter)

        with patch.object(subprocess, "run", side_effect=fake_run):
            with patch.object(codex_call, "_locate_companion", return_value=Path("/fake/companion.mjs")):
                with patch.object(codex_call, "_sleep", lambda _: None):
                    return codex_call.run(
                        mode="implement",
                        run_id="run-test",
                        stage_id="stage-1",
                        cwd=Path("/fake/project"),
                        diff_file=None,
                        test_results_file=None,
                        stack=None,
                    )

    def test_rate_limited_raises_rate_limit_error(self):
        responses = iter([
            _FakeCompletedProcess(stdout=json.dumps({"jobId": "j-rl"})),
            _FakeCompletedProcess(stdout=json.dumps({
                "status": "failed",
                "error": "Anthropic API returned 429: rate limit exceeded"
            })),
        ])
        with self.assertRaises(codex_call.RateLimited):
            self._run_with_responses(responses)

    def test_plugin_missing_raises_file_not_found(self):
        with patch.object(codex_call, "_locate_companion", side_effect=FileNotFoundError("plugin missing")):
            with self.assertRaises(FileNotFoundError):
                codex_call.run(
                    mode="implement",
                    run_id="run-test",
                    stage_id="stage-1",
                    cwd=Path("/fake/project"),
                    diff_file=None,
                    test_results_file=None,
                    stack=None,
                )

    def test_main_rate_limited_exits_2(self):
        responses = iter([
            _FakeCompletedProcess(stdout=json.dumps({"jobId": "j-rl"})),
            _FakeCompletedProcess(stdout=json.dumps({
                "status": "failed",
                "error": "rate limit"
            })),
        ])

        def fake_run(*args, **kwargs):
            return next(responses)

        argv_backup = sys.argv[:]
        sys.argv = ["codex_call.py", "implement", "--run-id", "run-x", "--stage-id", "stage-1"]
        try:
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.object(codex_call, "_locate_companion", return_value=Path("/fake/companion.mjs")):
                    with patch.object(codex_call, "_sleep", lambda _: None):
                        rc = codex_call.main()
            self.assertEqual(rc, 2)
        finally:
            sys.argv = argv_backup

    def test_main_other_error_exits_1(self):
        # Simulate task launch failure — no jobId returned.
        responses = iter([
            _FakeCompletedProcess(stdout=json.dumps({"error": "node not found"})),
        ])

        def fake_run(*args, **kwargs):
            return next(responses)

        argv_backup = sys.argv[:]
        sys.argv = ["codex_call.py", "implement", "--run-id", "run-x", "--stage-id", "stage-1"]
        try:
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.object(codex_call, "_locate_companion", return_value=Path("/fake/companion.mjs")):
                    rc = codex_call.main()
            self.assertEqual(rc, 1)
        finally:
            sys.argv = argv_backup
```

- [ ] **Step 2: Run, verify error tests fail**

```bash
python3 -m unittest sdk.tests.test_codex_call -v
```

Expected: `ErrorPathTest` cases fail (RateLimited class not defined; main doesn't differentiate exit 1 vs 2).

- [ ] **Step 3: Patch `sdk/codex_call.py` to handle error classes**

Add near the top, after imports:

```python
class RateLimited(Exception):
    """Raised when codex / its upstream model hits a rate limit. Exit code 2."""
```

Modify `run()` — replace the post-poll status check with:

```python
    if st != "completed":
        # Inspect status_resp for rate-limit hints.
        status_text = json.dumps(status_resp)
        if any(hint.lower() in status_text.lower() for hint in _RATE_LIMIT_HINTS):
            raise RateLimited(f"codex job {job_id} rate-limited: {status_resp.get('error', '')[:200]}")
        raise RuntimeError(f"codex job {job_id} terminal status: {st} ({status_resp.get('error', '')[:200]})")
```

Modify `main()` — replace the bare `except Exception` with classified handling:

```python
    try:
        out = run(
            mode=args.mode,
            run_id=args.run_id,
            stage_id=args.stage_id,
            cwd=cwd,
            diff_file=diff_file,
            test_results_file=test_results_file,
            stack=stack,
        )
        print(json.dumps(out))
        return 0
    except RateLimited as exc:
        print(json.dumps({"status": "error", "error_class": "rate_limited", "message": str(exc)}))
        return 2
    except FileNotFoundError as exc:
        print(json.dumps({"status": "error", "error_class": "plugin_missing", "message": str(exc)}))
        return 1
    except TimeoutError as exc:
        print(json.dumps({"status": "error", "error_class": "timeout", "message": str(exc)}))
        return 1
    except ValueError as exc:
        print(json.dumps({"status": "error", "error_class": "parse_fail", "message": str(exc)}))
        return 1
    except Exception as exc:
        print(json.dumps({"status": "error", "error_class": "worker_failed", "message": str(exc)}))
        return 1
```

- [ ] **Step 4: Run all tests, verify pass**

```bash
python3 -m unittest sdk.tests.test_codex_call -v
```

Expected: 5 tests pass (1 happy-path + 4 error-path).

- [ ] **Step 5: Commit**

```bash
git add sdk/codex_call.py sdk/tests/test_codex_call.py
git commit -m "codex_call: error_class taxonomy + rate-limit detection + exit code dispatch"
```

---

## Task 11: cli.py — run_start + list_runs + tests

**Files:**
- Create: `sdk/cli.py`
- Create: `sdk/tests/test_run_start.py`

- [ ] **Step 1: Write failing tests for run_start**

Write `sdk/tests/test_run_start.py`:

```python
"""Tests for `donace run_start` and `donace list_runs`."""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk import cli


class RunStartTest(unittest.TestCase):
    def test_mints_run_id_and_creates_skeleton(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_id = cli.run_start(cwd=tmp_path)

            self.assertRegex(run_id, r"^run-[0-9a-f]{8}$")
            run_dir = tmp_path / ".ai" / "runs" / run_id
            self.assertTrue(run_dir.is_dir())
            meta = json.loads((run_dir / "meta.json").read_text())
            self.assertEqual(meta["status"], "spec")
            self.assertIn("created_at", meta)

    def test_two_calls_produce_different_ids(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            id1 = cli.run_start(cwd=tmp_path)
            id2 = cli.run_start(cwd=tmp_path)
            self.assertNotEqual(id1, id2)


class ListRunsTest(unittest.TestCase):
    def test_returns_runs_in_creation_order(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            id1 = cli.run_start(cwd=tmp_path)
            id2 = cli.run_start(cwd=tmp_path)
            runs = cli.list_runs(cwd=tmp_path)
            self.assertEqual(set(runs), {id1, id2})

    def test_empty_when_no_runs(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(cli.list_runs(cwd=Path(tmp)), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify fails**

```bash
python3 -m unittest sdk.tests.test_run_start -v
```

Expected: ImportError or AttributeError (`cli` doesn't exist or lacks `run_start`/`list_runs`).

- [ ] **Step 3: Implement `sdk/cli.py` skeleton with run_start + list_runs**

Write `sdk/cli.py`:

```python
"""donace CLI.

Subcommands:
    run_start                 Mint a run-id, create .ai/runs/<id>/meta.json
    list_runs                 Print existing run-ids in creation order
    parse_plan --run-id <id>  Parse plan.md, emit stages JSON to stdout
    stage_status <run-id>     ASCII pipeline view (Task 13)
    mark_completed --run-id   Set meta.json status=completed
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path
from typing import Iterable


def _runs_root(cwd: Path) -> Path:
    return cwd / ".ai" / "runs"


def run_start(cwd: Path) -> str:
    """Mint a run-id, create the run directory + meta.json. Return the run-id."""
    runs_root = _runs_root(cwd)
    runs_root.mkdir(parents=True, exist_ok=True)
    run_id = f"run-{secrets.token_hex(4)}"
    run_dir = runs_root / run_id
    run_dir.mkdir()
    (run_dir / "stages").mkdir()
    meta = {
        "status": "spec",
        "created_at": _utc_now(),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return run_id


def list_runs(cwd: Path) -> list[str]:
    """Return existing run-ids, sorted by creation time (oldest first)."""
    runs_root = _runs_root(cwd)
    if not runs_root.exists():
        return []
    entries = sorted(
        (p for p in runs_root.iterdir() if p.is_dir() and p.name.startswith("run-")),
        key=lambda p: p.stat().st_ctime,
    )
    return [p.name for p in entries]


def mark_completed(cwd: Path, run_id: str) -> None:
    meta_path = _runs_root(cwd) / run_id / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["status"] = "completed"
    meta["completed_at"] = _utc_now()
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="donace")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run_start")

    sub.add_parser("list_runs")

    p_mark = sub.add_parser("mark_completed")
    p_mark.add_argument("--run-id", required=True)

    # parse_plan and stage_status added in later tasks (12, 13).
    p_parse = sub.add_parser("parse_plan")
    p_parse.add_argument("--run-id", required=True)

    p_status = sub.add_parser("stage_status")
    p_status.add_argument("run_id")

    args = parser.parse_args(argv)
    cwd = Path.cwd()

    if args.cmd == "run_start":
        run_id = run_start(cwd)
        run_dir = _runs_root(cwd) / run_id
        print(json.dumps({"run_id": run_id, "run_dir": str(run_dir)}))
        return 0

    if args.cmd == "list_runs":
        for r in list_runs(cwd):
            print(r)
        return 0

    if args.cmd == "mark_completed":
        mark_completed(cwd, args.run_id)
        return 0

    if args.cmd == "parse_plan":
        from sdk.cli import parse_plan_to_json  # implemented in Task 12
        print(parse_plan_to_json(cwd, args.run_id))
        return 0

    if args.cmd == "stage_status":
        from sdk.cli import render_stage_status  # implemented in Task 13
        sys.stdout.write(render_stage_status(cwd, args.run_id))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests, verify pass**

```bash
python3 -m unittest sdk.tests.test_run_start -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add sdk/cli.py sdk/tests/test_run_start.py
git commit -m "Add cli.py: run_start, list_runs, mark_completed (parse_plan/stage_status stubs)"
```

---

## Task 12: cli.py — plan parser + execute test-command tests

**Files:**
- Modify: `sdk/cli.py`
- Create: `sdk/tests/test_plan_parser.py`
- Create: `sdk/tests/test_execute_test_commands.py`

- [ ] **Step 1: Write failing plan-parser tests**

Write `sdk/tests/test_plan_parser.py`:

```python
"""Tests for plan.md parsing in sdk.cli."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk import cli


_PLAN_FIXTURE = """# Plan: example feature

## Stage 1: add health endpoint
- implementer: claude
- goal: expose /health returning 200 OK with build sha
- files: src/server.py, src/server_test.py
- success criteria:
  - GET /health returns 200
  - Response body includes git sha
- tests:
  - python3 -m unittest src.server_test -v

## Stage 2: rename UserSvc fields
- implementer: codex
- goal: rename created -> created_at, updated -> updated_at
- files: src/user.py, migrations/0042.sql
- success criteria:
  - All callers updated
- tests:
  - none: mechanical rename; existing suite covers behavior
"""


class PlanParserTest(unittest.TestCase):
    def _write_plan(self, tmp: Path, content: str) -> str:
        run_id = cli.run_start(cwd=tmp)
        plan = tmp / ".ai" / "runs" / run_id / "plan.md"
        plan.write_text(content)
        return run_id

    def test_parses_two_stages(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_id = self._write_plan(tmp_path, _PLAN_FIXTURE)
            stages = cli.parse_plan(tmp_path, run_id)
            self.assertEqual(len(stages), 2)
            self.assertEqual(stages[0]["sid"], "stage-1")
            self.assertEqual(stages[0]["implementer"], "claude")
            self.assertEqual(stages[0]["name"], "add health endpoint")
            self.assertEqual(stages[1]["implementer"], "codex")
            self.assertEqual(
                stages[0]["tests"],
                ["python3 -m unittest src.server_test -v"],
            )
            self.assertEqual(stages[1]["tests"], ["none: mechanical rename; existing suite covers behavior"])

    def test_invalid_implementer_raises(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad = _PLAN_FIXTURE.replace("implementer: claude", "implementer: gpt-9")
            run_id = self._write_plan(tmp_path, bad)
            with self.assertRaises(ValueError):
                cli.parse_plan(tmp_path, run_id)

    def test_missing_required_field_raises(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Drop the "tests:" block from stage 1.
            bad = _PLAN_FIXTURE.replace("- tests:\n  - python3 -m unittest src.server_test -v\n", "")
            run_id = self._write_plan(tmp_path, bad)
            with self.assertRaises(ValueError):
                cli.parse_plan(tmp_path, run_id)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Write failing test-command tests**

Write `sdk/tests/test_execute_test_commands.py`:

```python
"""Tests for `tests:` bullet handling in sdk.cli (skip none:, run runnable, persist results)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk import cli


class IsNoneMarkerTest(unittest.TestCase):
    def test_none_prefix_skipped(self):
        self.assertTrue(cli.is_none_marker("none: docs-only stage"))
        self.assertTrue(cli.is_none_marker("none: mechanical rename"))

    def test_real_command_not_skipped(self):
        self.assertFalse(cli.is_none_marker("python3 -m unittest tests"))
        self.assertFalse(cli.is_none_marker("npm test"))
        self.assertFalse(cli.is_none_marker("nonexistent_cmd"))


class TestResultsPersistTest(unittest.TestCase):
    def test_format_test_results_md_two_commands(self):
        out = cli.format_test_results_md(
            sid="stage-1",
            attempt=1,
            results=[
                {"command": "python3 -m unittest x", "exit_code": 0, "stdout": "ok\n", "stderr": ""},
                {"command": "npm test", "exit_code": 1, "stdout": "1 failed", "stderr": "AssertionError"},
            ],
        )
        self.assertIn("# Test results: stage-1 (attempt 1)", out)
        self.assertIn("$ python3 -m unittest x", out)
        self.assertIn("exit: 0", out)
        self.assertIn("exit: 1", out)
        self.assertIn("AssertionError", out)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run, verify both fail**

```bash
python3 -m unittest sdk.tests.test_plan_parser sdk.tests.test_execute_test_commands -v
```

Expected: failures (functions not yet defined).

- [ ] **Step 4: Add parser + helpers to `sdk/cli.py`**

Append to `sdk/cli.py`:

```python
# ---- plan parsing ----

_VALID_IMPLEMENTERS = {"claude", "codex"}
_REQUIRED_STAGE_FIELDS = ("implementer", "goal", "files", "success criteria", "tests")


def parse_plan(cwd: Path, run_id: str) -> list[dict]:
    """Parse plan.md into a list of stage dicts.

    Stage dict keys:
      sid         — "stage-N"
      name        — stage title text after "## Stage N: "
      implementer — "claude" | "codex"
      goal        — string
      files       — list[str]
      success_criteria — list[str]
      tests       — list[str]
    """
    plan_path = _runs_root(cwd) / run_id / "plan.md"
    if not plan_path.exists():
        raise FileNotFoundError(f"plan.md not found at {plan_path}")
    text = plan_path.read_text()
    return _parse_plan_text(text)


def parse_plan_to_json(cwd: Path, run_id: str) -> str:
    return json.dumps(parse_plan(cwd, run_id), indent=2)


def _parse_plan_text(text: str) -> list[dict]:
    stages: list[dict] = []
    current: dict | None = None
    current_list_field: str | None = None  # which list field we're currently appending bullets to

    for raw in text.splitlines():
        line = raw.rstrip()

        # Stage header: ## Stage N: name
        m = _STAGE_HEADER_RE.match(line)
        if m:
            if current is not None:
                _validate_stage(current)
                stages.append(current)
            current = {
                "sid": f"stage-{m.group(1)}",
                "name": m.group(2).strip(),
                "files": [],
                "success_criteria": [],
                "tests": [],
            }
            current_list_field = None
            continue

        if current is None:
            continue

        # Scalar field: - implementer: x
        m = _SCALAR_FIELD_RE.match(line)
        if m:
            field, value = m.group(1).strip().lower(), m.group(2).strip()
            if field == "implementer":
                if value not in _VALID_IMPLEMENTERS:
                    raise ValueError(
                        f"stage {current['sid']}: implementer must be 'claude' or 'codex', got {value!r}"
                    )
                current["implementer"] = value
                current_list_field = None
            elif field == "goal":
                current["goal"] = value
                current_list_field = None
            elif field == "files":
                current["files"] = [f.strip() for f in value.split(",") if f.strip()]
                current_list_field = None
            elif field in ("success criteria", "tests"):
                # These are list openers; bullets follow on subsequent lines.
                current_list_field = "success_criteria" if field == "success criteria" else "tests"
            continue

        # List bullet: "  - some text"
        m = _LIST_BULLET_RE.match(line)
        if m and current_list_field:
            current[current_list_field].append(m.group(1).strip())
            continue

        # Blank or unknown — leaves current list_field intact.

    if current is not None:
        _validate_stage(current)
        stages.append(current)

    return stages


_STAGE_HEADER_RE = re.compile(r"^## Stage (\d+): (.+)$")
_SCALAR_FIELD_RE = re.compile(r"^- ([^:]+):(.*)$")
_LIST_BULLET_RE = re.compile(r"^  - (.+)$")


def _validate_stage(stage: dict) -> None:
    for f in ("implementer", "goal"):
        if not stage.get(f):
            raise ValueError(f"stage {stage.get('sid', '?')}: missing required field '{f}'")
    for f in ("files", "success_criteria", "tests"):
        if not stage.get(f):
            raise ValueError(f"stage {stage.get('sid', '?')}: '{f}' is empty")


# ---- test command handling ----

def is_none_marker(bullet: str) -> bool:
    """A `tests:` bullet that begins with 'none:' is a deliberate skip."""
    return bullet.lstrip().lower().startswith("none:")


def format_test_results_md(*, sid: str, attempt: int, results: Iterable[dict]) -> str:
    """Render test-results.md for a single attempt."""
    lines = [f"# Test results: {sid} (attempt {attempt})", ""]
    for i, r in enumerate(results, start=1):
        lines.append(f"## Command {i}")
        lines.append(f"$ {r['command']}")
        lines.append(f"exit: {r['exit_code']}")
        if r.get("stdout"):
            lines.append("stdout (last 2000):")
            lines.append(r["stdout"])
        if r.get("stderr"):
            lines.append("stderr (last 2000):")
            lines.append(r["stderr"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
```

Add `import re` to the top imports if not already present.

- [ ] **Step 5: Run all sdk tests, verify pass**

```bash
python3 -m unittest discover -s sdk/tests -v
```

Expected: all tests pass (run_start tests still pass too).

- [ ] **Step 6: Commit**

```bash
git add sdk/cli.py sdk/tests/test_plan_parser.py sdk/tests/test_execute_test_commands.py
git commit -m "cli: plan parser + tests-bullet helpers (none: skip, results md formatter)"
```

---

## Task 13: cli.py — stage_status renderer + tests

**Files:**
- Modify: `sdk/cli.py`
- Create: `sdk/tests/test_stage_status_render.py`

- [ ] **Step 1: Write failing renderer tests**

Write `sdk/tests/test_stage_status_render.py`:

```python
"""Tests for the ASCII pipeline view in `donace stage_status`."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk import cli


_PLAN = """# Plan: pipeline test

## Stage 1: add health endpoint
- implementer: claude
- goal: expose /health
- files: src/server.py
- success criteria:
  - returns 200
- tests:
  - python3 -m unittest src.server_test

## Stage 2: rename fields
- implementer: codex
- goal: rename
- files: src/user.py
- success criteria:
  - callers updated
- tests:
  - none: mechanical
"""


def _write_plan(cwd: Path) -> str:
    run_id = cli.run_start(cwd=cwd)
    (cwd / ".ai" / "runs" / run_id / "plan.md").write_text(_PLAN)
    return run_id


def _set_stage_status(cwd: Path, run_id: str, sid: str, status_dict: dict) -> None:
    p = cwd / ".ai" / "runs" / run_id / "stages" / sid
    p.mkdir(parents=True, exist_ok=True)
    (p / "status.json").write_text(json.dumps(status_dict))


class GlyphTest(unittest.TestCase):
    def test_glyphs_utf8(self):
        self.assertEqual(cli._glyph_for("passed", utf8=True), "✓")
        self.assertEqual(cli._glyph_for("running", utf8=True), "⟳")
        self.assertEqual(cli._glyph_for("pending", utf8=True), "·")
        self.assertEqual(cli._glyph_for("blocked", utf8=True), "✗")
        self.assertEqual(cli._glyph_for("interrupted", utf8=True), "‖")

    def test_glyphs_ascii_fallback(self):
        self.assertEqual(cli._glyph_for("passed", utf8=False), "*")
        self.assertEqual(cli._glyph_for("running", utf8=False), ">")
        self.assertEqual(cli._glyph_for("pending", utf8=False), ".")
        self.assertEqual(cli._glyph_for("blocked", utf8=False), "!")
        self.assertEqual(cli._glyph_for("interrupted", utf8=False), "|")


class ReviewerDerivationTest(unittest.TestCase):
    def test_opposite_model(self):
        self.assertEqual(cli._reviewer_for("claude"), "codex")
        self.assertEqual(cli._reviewer_for("codex"), "claude")


class StateSummaryTest(unittest.TestCase):
    def test_passed_one_try(self):
        self.assertEqual(cli._state_summary({"status": "passed", "retry_count": 0}), "passed (1 try)")

    def test_passed_three_tries(self):
        self.assertEqual(cli._state_summary({"status": "passed", "retry_count": 2}), "passed (3 tries)")

    def test_running_with_retry(self):
        self.assertEqual(cli._state_summary({"status": "running", "retry_count": 1}), "running, retry 1/2")

    def test_blocked_with_reason(self):
        self.assertEqual(
            cli._state_summary({"status": "blocked", "reason": "tests failed"}),
            "blocked: tests failed",
        )

    def test_interrupted_with_reason(self):
        self.assertEqual(
            cli._state_summary({"status": "interrupted", "reason": "rate_limited"}),
            "interrupted: rate_limited",
        )

    def test_pending(self):
        self.assertEqual(cli._state_summary(None), "pending")


class RenderTest(unittest.TestCase):
    def test_header_and_rows_when_one_passed(self):
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            run_id = _write_plan(cwd)
            _set_stage_status(cwd, run_id, "stage-1", {"status": "passed", "retry_count": 0})
            out = cli.render_stage_status(cwd, run_id, utf8=False, width=120)
            self.assertIn(f"Run {run_id}", out)
            self.assertIn("1/2 stages passed", out)
            self.assertIn("stage-1", out)
            self.assertIn("stage-2", out)
            self.assertIn("claude", out)
            self.assertIn("codex", out)

    def test_tail_extracted_when_blocked_on_tests(self):
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            run_id = _write_plan(cwd)
            _set_stage_status(cwd, run_id, "stage-1", {
                "status": "blocked",
                "retry_count": 2,
                "reason": "tests failed",
            })
            (cwd / ".ai" / "runs" / run_id / "stages" / "stage-1" / "test-results.md").write_text(
                "# Test results: stage-1 (attempt 3)\n\n## Command 1\n$ pytest x\nexit: 1\n"
                "stderr (last 2000):\nAssertionError: expected 5, got 6\n"
            )
            out = cli.render_stage_status(cwd, run_id, utf8=False, width=120)
            self.assertIn("AssertionError", out)
            self.assertIn("blocked: tests failed", out)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify fails**

```bash
python3 -m unittest sdk.tests.test_stage_status_render -v
```

Expected: AttributeError on `cli._glyph_for` etc.

- [ ] **Step 3: Add renderer to `sdk/cli.py`**

Append:

```python
# ---- stage_status rendering ----

import shutil

_GLYPHS_UTF8 = {
    "passed": "✓",
    "running": "⟳",
    "pending": "·",
    "blocked": "✗",
    "interrupted": "‖",
}
_GLYPHS_ASCII = {
    "passed": "*",
    "running": ">",
    "pending": ".",
    "blocked": "!",
    "interrupted": "|",
}


def _glyph_for(status: str, *, utf8: bool) -> str:
    table = _GLYPHS_UTF8 if utf8 else _GLYPHS_ASCII
    return table.get(status, "?")


def _reviewer_for(implementer: str) -> str:
    return {"claude": "codex", "codex": "claude"}[implementer]


def _state_summary(status_json: dict | None) -> str:
    if status_json is None:
        return "pending"
    st = status_json.get("status", "pending")
    rc = status_json.get("retry_count", 0)
    reason = status_json.get("reason")
    if st == "passed":
        return f"passed ({rc + 1} tr{'y' if rc == 0 else 'ies'})"
    if st == "running":
        return f"running, retry {rc}/2" if rc else "running"
    if st in ("blocked", "interrupted"):
        return f"{st}: {reason}" if reason else st
    return st


def _utf8_supported() -> bool:
    enc = (os.environ.get("LANG", "") + os.environ.get("LC_ALL", "")).lower()
    return "utf" in enc


def _relative_time(then_iso: str) -> str:
    """Render 'started X ago' relative to now."""
    try:
        then = time.strptime(then_iso, "%Y-%m-%dT%H:%M:%SZ")
        delta_sec = int(time.time() - time.mktime(then))
    except Exception:
        return "started ?"
    if delta_sec < 60:
        return f"started {delta_sec}s ago"
    if delta_sec < 3600:
        return f"started {delta_sec // 60}m ago"
    if delta_sec < 86400:
        return f"started {delta_sec // 3600}h ago"
    return f"started {delta_sec // 86400}d ago"


def render_stage_status(cwd: Path, run_id: str, *, utf8: bool | None = None, width: int | None = None) -> str:
    if utf8 is None:
        utf8 = _utf8_supported()
    if width is None:
        width = shutil.get_terminal_size((80, 24)).columns

    run_dir = _runs_root(cwd) / run_id
    if not run_dir.exists():
        return f"Run {run_id} not found.\n"

    meta = json.loads((run_dir / "meta.json").read_text())
    stages = parse_plan(cwd, run_id) if (run_dir / "plan.md").exists() else []

    # Per-stage status_json (None if not yet started).
    stage_states = []
    for s in stages:
        sj = run_dir / "stages" / s["sid"] / "status.json"
        stage_states.append(json.loads(sj.read_text()) if sj.exists() else None)

    passed = sum(1 for st in stage_states if st and st.get("status") == "passed")
    overall = meta.get("status", "unknown")

    lines = []
    lines.append(
        f"Run {run_id}  •  {overall}  •  {passed}/{len(stages)} stages passed  •  {_relative_time(meta.get('created_at', ''))}"
    )
    lines.append("")

    # Compute name column width.
    max_name_len = max((len(s["name"]) for s in stages), default=0)
    name_col = min(max_name_len, max(20, width - 60))

    for stage, st in zip(stages, stage_states):
        status = (st or {}).get("status", "pending")
        glyph = _glyph_for(status, utf8=utf8)
        name = stage["name"]
        if len(name) > name_col:
            name = name[: name_col - 1] + "…"
        impl = stage["implementer"]
        rev = _reviewer_for(impl)
        summary = _state_summary(st)
        lines.append(f"  {glyph}  {stage['sid']:7s} {name:<{name_col}}   {impl} → {rev}   {summary}")

    # Tail extraction for the single active or blocked stage.
    active_idx = next(
        (i for i, st in enumerate(stage_states) if st and st.get("status") in ("running", "blocked")),
        None,
    )
    if active_idx is not None:
        sid = stages[active_idx]["sid"]
        st = stage_states[active_idx]
        tail = _extract_tail(run_dir / "stages" / sid, st, utf8=utf8)
        if tail:
            lines.append("")
            lines.append(f"▼ {sid} latest evidence (retry {st.get('retry_count', 0)}):" if utf8
                         else f"v {sid} latest evidence (retry {st.get('retry_count', 0)}):")
            lines.append(tail)

    return "\n".join(lines) + "\n"


def _extract_tail(stage_dir: Path, status_json: dict, *, utf8: bool, max_lines: int = 8) -> str:
    """Last failing test command + tail, OR first [P0] block from review.md."""
    reason = status_json.get("reason", "")
    tr = stage_dir / "test-results.md"
    rv = stage_dir / "review.md"

    if "tests failed" in reason and tr.exists():
        text = tr.read_text()
        # Take the last $-prefixed command + its stderr block.
        chunks = text.split("\n## Command")
        if chunks:
            last = chunks[-1]
            return "  " + "\n  ".join(last.strip().splitlines()[: max_lines + 2])
    if "P0" in reason and rv.exists():
        text = rv.read_text()
        idx = text.find("### [P0]")
        if idx >= 0:
            block = text[idx:].split("\n### [", 1)[0]
            return "  " + "\n  ".join(block.strip().splitlines()[:max_lines])
    return ""
```

Add `import os` and `import shutil` to the top of the file if not already present.

- [ ] **Step 4: Run all tests, verify pass**

```bash
python3 -m unittest discover -s sdk/tests -v
```

Expected: all tests pass.

- [ ] **Step 5: Smoke-render manually**

```bash
cd /Users/minghaojiang/Developer/donace
RUN_ID=$(python3 sdk/cli.py run_start | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["run_id"])')
echo "$RUN_ID"
python3 sdk/cli.py stage_status "$RUN_ID"
```

Expected: a header line + empty rows (no stages yet because no plan.md). Sanity check.

- [ ] **Step 6: Commit**

```bash
git add sdk/cli.py sdk/tests/test_stage_status_render.py
git commit -m "cli: ASCII pipeline view (donace stage_status) with glyphs + tail extraction"
```

---

## Task 14: README install + CLAUDE.md project contract

**Files:**
- Modify: `README.md`
- Create: `CLAUDE.md`

- [ ] **Step 1: Read existing README**

```bash
cat README.md
```

The current README is short (from main). We replace its body but keep the LICENSE / author info if present.

- [ ] **Step 2: Rewrite `README.md`**

Write `README.md`:

```markdown
# donace

Plan / execute / review pipeline that orchestrates Claude Code and Codex on a per-stage basis.

## What it does

- `/donace:chat` — brainstorm with the main LLM, write `spec.md`.
- `/donace:plan <run-id>` — planner subagent reads `spec.md`, emits `plan.md` with stages tagged `implementer: claude|codex`.
- `/donace:execute <run-id>` — for each stage: dispatch implementer (background), run listed `tests:`, dispatch reviewer (the opposite model), commit on PASS. P0 findings or test failures retry the stage up to 2 times; rate-limit hits drive the stage to `interrupted` and resume cleanly.

The main LLM in your Claude Code session orchestrates everything — there is no team-lead subagent. Workers run in the background so you can chat / clarify / interrupt at any time.

## Install

This is a Claude Code plugin. To install for development:

```bash
cd ~/.claude/plugins/local
ln -s /path/to/donace donace
```

Then restart Claude Code (or run `/plugin reload donace`). Skills `/donace:chat`, `/donace:plan`, and `/donace:execute` will be available.

Codex stages additionally require the `openai-codex` plugin to be installed (donace shells out to its `codex-companion.mjs`).

## Quick tour

```bash
# Create a fresh run
/donace:chat
# ... brainstorm with the main LLM, it writes spec.md ...

# Plan it
/donace:plan run-a1b2c3d4

# Edit plan.md if needed (especially `implementer:` tags)

# Run it
/donace:execute run-a1b2c3d4

# At any time, glance at the pipeline
python3 sdk/cli.py stage_status run-a1b2c3d4
```

## Layout

- `skills/{chat,plan,execute}/SKILL.md` — main LLM instructions per skill
- `agents/{planner,implementer,reviewer}.md` — subagent definitions
- `agents/references/review-checklist-{python,typescript,ios,general}.md` — stack-specific reviewer hints
- `agents/prompts/codex-{implementer,reviewer}.md` — codex prompt prefixes
- `sdk/codex_call.py` — wrapper around codex-companion.mjs (`task --background --json` + status + result)
- `sdk/cli.py` — `donace run_start | list_runs | parse_plan | stage_status | mark_completed`
- `.ai/runs/<run-id>/` — run state (gitignored)

## Design

See [docs/superpowers/specs/2026-05-02-donace-simplify-design.md](docs/superpowers/specs/2026-05-02-donace-simplify-design.md) for the full spec, including the reviewer severity contract, stop-state semantics, and bootstrap order.

## License

MIT (see LICENSE).
```

- [ ] **Step 3: Write `CLAUDE.md`**

Write `CLAUDE.md`:

````markdown
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
````

- [ ] **Step 4: Verify both files**

```bash
test -f README.md && test -f CLAUDE.md && wc -l README.md CLAUDE.md
```

Expected: both exist.

- [ ] **Step 5: Final test run**

```bash
python3 -m unittest discover -s sdk/tests -v
```

Expected: all tests pass.

- [ ] **Step 6: Final commit**

```bash
git add README.md CLAUDE.md
git commit -m "Rewrite README + add CLAUDE.md project contract for donace v0"
```

---

## Acceptance for "v0 ships"

After all 14 tasks land:

- [ ] `git log --oneline main..simplify` shows 14 commits (one per task).
- [ ] `python3 -m unittest discover -s sdk/tests -v` reports 100% pass; suite covers codex_call (happy + 4 error paths), run_start (id minting + skeleton), plan parser (valid + invalid + missing fields), test commands (none: skip, results md format), stage_status renderer (glyphs UTF-8 + ASCII, reviewer derivation, state summaries, header + rows + tail extraction).
- [ ] `agents/` contains exactly: `planner.md`, `implementer.md`, `reviewer.md`, `qa.md`, `ui-designer.md`, plus `references/` (4 files) and `prompts/` (2 files).
- [ ] `.claude-plugin/plugin.json` lists 3 skills + 5 agents.
- [ ] Manual smoke: `/donace:chat` → `/donace:plan run-XXX` → `/donace:execute run-XXX` on a tiny scratch task with at least one claude stage and one codex stage; both pass review and commit; user confirms mid-run interactivity (chat with main LLM during a long stage).

If any acceptance bullet fails, the failing task gets re-opened, not the whole plan.
