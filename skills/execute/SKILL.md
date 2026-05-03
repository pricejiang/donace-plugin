---
name: donace-execute
description: Run the execute loop on a plan.md — for each stage, dispatch implementer (Claude or Codex), run tests, dispatch reviewer (the opposite model), retry on P0 or test failure, commit on PASS. Stays interactive: long-running workers run in background, the main LLM responds to the user between dispatches.
---

# /donace:execute

Iterate the stages of `.ai/runs/<id>/plan.md`, dispatching implementer + reviewer per stage. Per-stage gate: P0 review findings or test failures retry the stage up to 2 times; rate-limit hits drive the stage to `interrupted` without consuming a retry. The 15-step loop, severity contract, and stop-state semantics are all defined inline below.

## Inputs

- `<run-id>`: required.

## Preflight

1. **Worktree state.** Decide path:
   - **Fresh run** (no `stages/<sid>/status.json` files yet): `git status --porcelain` MUST be empty. If dirty, stop and tell the user to commit/stash first.
   - **Resume of an interrupted stage**: allow dirty worktree IF it's the partial work of an `interrupted` stage. Verify HEAD still matches that stage's saved `pre_stage_sha`. If HEAD diverged (unrelated commit, branch switch), mark the stage `blocked` with reason `resume baseline mismatch` and stop.
   - **Resume with a stale `running` stage**: if a prior session died mid-stage and left `status.json.status == "running"`, first rewrite it to `{"status": "interrupted", "reason": "session_ended", ...}` and then follow the interrupted-resume path. Do NOT treat stale `running` as a fresh entry.
   - **Resume with a `blocked` stage**: do NOT auto-rerun it. Stop, show the blocked reason, and ask the user whether they want to keep the partial work for manual fixes or explicitly discard it and restart the stage.

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
- `status.json.status == "running"` → treat as a stale in-flight marker from a previous session: rewrite to `interrupted: session_ended`, then resume in-place using the same `pre_stage_sha` and `retry_count`.
- `status.json.status == "blocked"` → stop immediately and tell the user why this stage is blocked. Do not re-enter automatically.
- No `status.json` yet → fresh entry: capture `pre_stage_sha = git rev-parse HEAD`, write `status.json: {status: "running", retry_count: 0, pre_stage_sha: <sha>}`.

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

On retries, append `--retry-context-file .ai/runs/<id>/stages/<sid>/retry-context.md` to that command.

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
- Persist the retry context to `.ai/runs/<id>/stages/<sid>/retry-context.md` so the next implementer attempt sees the exact failing test output / worker error / P0 block.
- If `retry_count > 2`: write `status.json: {status: "blocked", reason: <one of: "tests failed", "P0 unresolved", "worker errored", "empty diff", ...>}`. Stop the run. Tell the user.
- Else: feed retry context (the failure detail) into the next implementer dispatch; if implementer is codex, include `--retry-context-file .ai/runs/<id>/stages/<sid>/retry-context.md`; goto step 2.

### 11. Stage PASS

- `git add -A && git commit -m "[<sid>] <stage name>"` — commit ALL working-tree changes from this stage. (`stage.files` is a hint, not a commit boundary.)
- Write `status.json: {status: "passed"}`. Preserve `retry_count` for the record.
- The reviewer's final document already lives at `review.md` (saved in step 9); it contains only [P1]/[P2] by definition since any [P0] would have triggered retry.
- Advance to the next stage.

## Run completion

After all stages reach `passed`:

```bash
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
- **"edit plan and restart"** → Stop the current dispatch. Tell the user to decide first whether to:
  - **keep partial work**: do nothing; resume picks it up.
  - **discard partial work**: only on explicit user choice, run `git reset --hard <pre_stage_sha>` and `git clean -fd`, then remove the stage's old evidence files (`status.json`, `retry-context.md`, `diff.patch`, `review.md`, `test-results.md`) so the next `/donace:execute <id>` starts that stage fresh.
  Then they edit `plan.md` and re-invoke `/donace:execute <id>`.

## What `/donace:execute` is NOT

- Not a one-shot — it's re-entrant. If the session ends or the user interrupts, re-running picks up at the first non-`passed` stage.
- Not parallel — stages run serially in v0 (worktree parallelism is v1).
- Not auto-recovering from `blocked` — `blocked` requires user action (edit plan, fix env, etc.) before resume.
- Not running in its own process group — workers are children of the main LLM's session and die if the user closes Claude Code (this is the v2 detach scope, deliberately out of v0).
