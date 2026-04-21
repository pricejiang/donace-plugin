# Changelog

Notable changes to donace. Format adapted from [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

Eight commits on the `orchestration` branch, all stemming from retros on
`run-phase3-reader-24c1c0260e85` and `run-phase4-create-fork-a6ca720da361`.

### Added

- **`plan_status` orchestrator subcommand** (`python3 sdk/orchestrator.py plan_status --run-id <id>`).
  Finalizes a codex plan review that was launched in background. No-op when
  `codex_review` is already terminal. Team-lead invokes this before Step 2 on
  any resumed run. ([commit b4ce9b5][b4ce9b5])
- **`PENDING` plan job status** for reviews where codex is still queued or
  running. `_classify_run_state` now gates `plan_ready` on
  `not plan_review_in_progress`, so even an on-disk `PASS` plan job is held
  back when `plan.json.codex_review.status` flips to `queued`/`running`.
  ([commit e7f7d9f][e7f7d9f])
- **`codex_review.thread_id` / `codex_review.job_id`** persisted in
  `plan.json`. Enables rev-chain session reuse and background-job recovery.
  ([commits ac48496][ac48496], [2bfe17f][2bfe17f], [b4ce9b5][b4ce9b5])

### Changed

- **Plan review runs in background with a 600s cap** (was: synchronous
  240s wait). Launches via `codex-companion.mjs task --background --json`,
  polls `status <job-id>` every 3 seconds. On cap hit, codex keeps
  running — `plan.json` reflects `status: "running"` with the `job_id` so
  `plan_status` can finalize later. Diagnosed from phase4 where codex
  took 10 min 38 s and returned `verdict: "needs-attention"` but our
  240s wait had already given up. ([commit b4ce9b5][b4ce9b5])
- **Plan review revisions reuse codex threads**. On the 2nd+ `/donace:plan`
  invocation against the same run-id, donace passes `--resume-last` to
  codex only when `codex-companion task-resume-candidate` confirms the
  persisted `thread_id` is still the latest repo task. Otherwise falls
  back to a fresh review to avoid resuming an unrelated thread.
  ([commits ac48496][ac48496], [2bfe17f][2bfe17f])
- **Rate-limit notices surface as `INTERRUPTED`, not `BLOCKED`**. Claude
  Code delivers "You've hit your limit · resets 1am" via an
  `AssistantMessage` text chunk followed by an empty-errors
  `ResultMessage`. Previously showed as `agent error: unknown`, counted
  against the 3-strike retry budget, and triggered
  `repeated_stage_failure` validation. Now recognized at every call site
  (implement / verify / fix_loop) and in both codex wrappers.
  ([commits cb4f747][cb4f747], [a383e66][a383e66])

### Fixed

- **`file_scope` markdown-backtick mismatch**. Scope strings arrived
  from `plan.json` as `` `apps/web/lib/x.ts` `` (code spans) but were
  compared as literals to `apps/web/lib/x.ts` in the Write/Edit and
  bash-redirect hooks — every in-scope write was denied. `run-phase3-reader`
  stage-2 hit this 6+ times before hand-off to manual implementation.
  Normalized at `AgentDispatcher` construction. ([commit 136fd85][136fd85])
- **`_REDIR_RE` scanned into quoted strings**. `node -e "c=>d+=c"`,
  `"if (w.length>=1) ..."`, and `echo "a > b"` each got parsed as shell
  redirects with bogus targets (`d+=c`, `w.length`, etc.). Now
  `_blank_quoted_regions` neutralizes quoted content before the regex
  runs. Backticks are preserved (command substitution is not a quote).
  Quoted redirect targets (`cmd > "apps/x.ts"`) are re-extracted from
  the original command so scope checks still work.
  ([commits 136fd85][136fd85], [844555f][844555f])
- **`fetch_codex_plan_review_result` overwrote queued jobs as `skipped`**.
  `task --background` persists jobs as `queued` until the worker picks
  them up. An early `plan_status` would see a non-`running` state and
  fall through to result-fetch, silently dropping the `job_id`. Both
  `queued` and `running` are now treated as in-progress.
  ([commit e7f7d9f][e7f7d9f])
- **Codex review wrappers swallowed `RateLimitError`**. Generic
  `except Exception` returned `{status: "skipped"}` from codex review
  paths, defeating the run-level `INTERRUPTED` conversion. Now both
  `run_codex_review` and `run_codex_plan_review` re-raise.
  ([commit a383e66][a383e66])

### Test suite

New `sdk/tests/` using stdlib `unittest`:

- `test_rate_limit.py` — 17 cases for `_detect_rate_limit`, `RateLimitError`,
  and the three `run_job` call sites
- `test_file_scope.py` — 21 cases for `_normalize_file_scope`,
  `_blank_quoted_regions`, and `_extract_shell_word`
- `test_plan_review_session.py` — 13 cases for `--resume-last` behavior
  and `_prior_codex_thread_id`
- `test_plan_review_background.py` — 14 cases for background launch,
  poll transitions, queued-state handling, and `cmd_plan_status`

Run with `python3 -m unittest discover -s sdk/tests` (65 tests total).

[cb4f747]: https://github.com/pricejiang/donace-plugin/commit/cb4f747
[a383e66]: https://github.com/pricejiang/donace-plugin/commit/a383e66
[136fd85]: https://github.com/pricejiang/donace-plugin/commit/136fd85
[844555f]: https://github.com/pricejiang/donace-plugin/commit/844555f
[ac48496]: https://github.com/pricejiang/donace-plugin/commit/ac48496
[2bfe17f]: https://github.com/pricejiang/donace-plugin/commit/2bfe17f
[b4ce9b5]: https://github.com/pricejiang/donace-plugin/commit/b4ce9b5
[e7f7d9f]: https://github.com/pricejiang/donace-plugin/commit/e7f7d9f
