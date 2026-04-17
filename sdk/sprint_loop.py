"""Phase 2 inner loop: per-stage implement -> verify (parallel) -> fix loop -> gate check."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Awaitable

from sdk.events import (
    AgentCompleted,
    AgentFailed,
    AgentSkipped,
    AgentStarted,
    Decision,
    EventBus,
    FixLoopExhausted,
    FixLoopResolved,
    FixLoopStarted,
    GateReached,
    SprintResult,
    Stage,
    StageChanged,
    StageCompleted,
    StageResult,
)


# ---------------------------------------------------------------------------
# Type aliases for agent dispatch functions
# ---------------------------------------------------------------------------

# query(agent, prompt, model) -> str
AgentQueryFn = Callable[..., Awaitable[str]]

# run_test_engineer(stage) -> dict  {"passed": int, "failed": int, "output": str}
TestRunnerFn = Callable[[Stage], Awaitable[dict]]

# run_codex_review() -> dict  {"status": str, "p1_findings": int, "findings": list}
CodexRunnerFn = Callable[[], Awaitable[dict]]

# run_runtime_verifier(stage_name, task_context) -> dict  {"status": str, "score": str, "output": str}
RuntimeRunnerFn = Callable[[str, str], Awaitable[dict]]


# ---------------------------------------------------------------------------
# Failure collection
# ---------------------------------------------------------------------------

def collect_failures(
    test_result: dict,
    codex_result: dict,
    runtime_result: dict | None,
) -> list[dict[str, Any]]:
    """Collect failures from verification results.

    Each failure is a dict with "source", "description", and "severity".
    """
    failures: list[dict[str, Any]] = []

    # Test failures
    if test_result.get("failed", 0) > 0:
        failures.append({
            "source": "test-engineer",
            "description": test_result.get("output", f"{test_result['failed']} test(s) failed"),
            "severity": "error",
        })

    # Codex findings — review issues, not build/test failures.
    # Severity is "warning" so they enter the fix loop but don't trigger
    # MUST_STOP (which would halt the entire pipeline in non-interactive mode).
    if codex_result.get("has_issues"):
        failures.append({
            "source": "codex-review",
            "description": codex_result.get("output", "codex review found issues"),
            "severity": "warning",
        })

    # Runtime failures
    if runtime_result and runtime_result.get("status") == "FAIL":
        failures.append({
            "source": "runtime-verifier",
            "description": runtime_result.get("output", "Runtime verification failed"),
            "severity": "warning",  # runtime failures are warnings by default
        })

    return failures


def _failure_fingerprint(failures: list[dict[str, Any]]) -> frozenset[tuple[str, str]]:
    """Create a comparable fingerprint from failures to detect structural (unfixable) issues.

    If the fingerprint is identical across fix loop iterations, the failures
    are structural and further attempts will just waste tokens.
    """
    return frozenset(
        (f.get("source", ""), f.get("description", "")[:200])
        for f in failures
    )


# ---------------------------------------------------------------------------
# Sprint loop
# ---------------------------------------------------------------------------

# Callback type for stage completion persistence
OnStageCompleteFn = Callable[[StageResult], None] | None


async def run_sprint_loop(
    stages: list[Stage],
    cwd: str,
    bus: EventBus,
    query: AgentQueryFn,
    run_test_engineer: TestRunnerFn,
    run_codex_review: CodexRunnerFn,
    run_runtime_verifier: RuntimeRunnerFn,
    task_context: str = "",
    completed_stage_names: set[str] | None = None,
    on_stage_complete: OnStageCompleteFn = None,
) -> SprintResult:
    """Execute the Phase 2 sprint loop over all stages.

    Args:
        stages: List of Stage objects from the plan.
        cwd: Working directory.
        bus: EventBus for emitting events and waiting for decisions.
        query: Function to dispatch an agent query.
        run_test_engineer: Function to run test engineer for a stage.
        run_codex_review: Function to run codex review.
        run_runtime_verifier: Function to run runtime verifier (stage_name, task_context).
        task_context: Original task + plan text for agent prompts.
        completed_stage_names: Stages already completed in a previous run (skip these).
        on_stage_complete: Callback invoked after each stage completes (for state persistence).

    Returns:
        SprintResult with per-stage results, warnings, and summary.
        Note: when resuming, only newly-run stages are included. The orchestrator
        merges these with previously-completed stages from the run state.
    """
    _completed = completed_stage_names or set()
    results: list[StageResult] = []
    warnings: list[str] = []

    def _record(sr: StageResult) -> None:
        results.append(sr)
        if on_stage_complete:
            on_stage_complete(sr)

    # Populate dashboard with all stage names upfront (pending status)
    for idx, stage in enumerate(stages):
        await bus.emit(StageChanged(
            stage_name=stage.name,
            stage_index=idx,
            total_stages=len(stages),
            estimated_turns=stage.estimated_turns,
        ))
        if stage.name in (_completed or set()):
            await bus.emit(StageCompleted(stage_name=stage.name, status="PASS"))

    # --- Wave-based scheduler ---
    # Build dependency graph: stages run in parallel when their dependencies are met.
    # Falls back to serial execution when all stages depend on the previous one.
    stage_map = {s.name: s for s in stages}
    completed_names: set[str] = set(_completed)
    blocked_names: set[str] = set()
    remaining = [s for s in stages if s.name not in _completed]

    # Mark already-completed stages
    for s in stages:
        if s.name in _completed:
            await bus.emit(StageCompleted(stage_name=s.name, status="PASS"))
            warnings.append(f"Stage '{s.name}' already completed in previous run — skipped")

    wave_num = 0
    while remaining:
        # Find stages whose dependencies are all satisfied (completed or previously done)
        ready = [
            s for s in remaining
            if all(d in completed_names for d in s.depends_on)
            and not any(d in blocked_names for d in s.depends_on)
        ]

        # Stages blocked by a failed dependency
        newly_blocked = [
            s for s in remaining
            if any(d in blocked_names for d in s.depends_on)
            and s not in ready
        ]
        for s in newly_blocked:
            _record(StageResult(
                name=s.name, status="SKIPPED",
                test_result={"passed": 0, "failed": 0},
                codex_result={"status": "skipped", "has_issues": False, "output": ""},
                runtime_result=None, fix_attempts=0,
                recommendation="dependency blocked",
            ))
            await bus.emit(StageCompleted(stage_name=s.name, status="SKIPPED"))
            blocked_names.add(s.name)
            remaining.remove(s)

        if not ready:
            # Circular dependency or all remaining stages blocked
            for s in remaining:
                _record(StageResult(
                    name=s.name, status="BLOCKED",
                    test_result={"passed": 0, "failed": 0},
                    codex_result={"status": "skipped", "has_issues": False, "output": ""},
                    runtime_result=None, fix_attempts=0,
                    recommendation="unresolvable dependency",
                ))
                await bus.emit(StageCompleted(stage_name=s.name, status="BLOCKED"))
            break

        wave_num += 1

        if len(ready) == 1:
            # Single stage — run directly (no parallel overhead)
            stage = ready[0]
            idx = stages.index(stage)
            result = await _run_single_stage(
                stage, idx, len(stages), bus, query,
                run_test_engineer, run_codex_review, run_runtime_verifier,
                task_context, warnings,
            )
            _record(result)
            remaining.remove(stage)
            if result.status == "PASS":
                completed_names.add(stage.name)
            elif result.status == "BLOCKED":
                blocked_names.add(stage.name)
                if result.recommendation == "MUST_STOP":
                    # MUST_STOP: skip all remaining stages
                    for s in remaining:
                        _record(StageResult(
                            name=s.name, status="SKIPPED",
                            test_result={"passed": 0, "failed": 0},
                            codex_result={"status": "skipped", "has_issues": False, "output": ""},
                            runtime_result=None, fix_attempts=0,
                        ))
                        await bus.emit(StageCompleted(stage_name=s.name, status="SKIPPED"))
                    remaining.clear()
        else:
            # Multiple independent stages — parallel implement, then unified verify.
            # This saves tokens: N implements + 1 verify instead of N × (implement + verify).

            # Phase 1: parallel implement
            impl_tasks = []
            for stage in ready:
                idx = stages.index(stage)
                impl_tasks.append(
                    _implement_stage(
                        stage, idx, len(stages), bus, query, task_context, warnings,
                    )
                )
            impl_results = await asyncio.gather(*impl_tasks, return_exceptions=True)

            # Collect successful implementations; record failures
            implemented: list[Stage] = []
            for stage, result in zip(ready, impl_results):
                if isinstance(result, Exception):
                    sr = StageResult(
                        name=stage.name, status="BLOCKED",
                        test_result={"passed": 0, "failed": 0},
                        codex_result={"status": "skipped", "has_issues": False, "output": ""},
                        runtime_result=None, fix_attempts=0,
                        recommendation=f"implementation exception: {result}",
                    )
                    _record(sr)
                    blocked_names.add(stage.name)
                    remaining.remove(stage)
                    await bus.emit(StageCompleted(stage_name=stage.name, status="BLOCKED"))
                else:
                    implemented.append(stage)

            if not implemented:
                continue  # all stages in this wave failed

            # Phase 2: unified verify (one test run + one codex review for all stages)
            wave_stage_names = [s.name for s in implemented]
            has_user_facing = any(s.has_user_facing_changes for s in implemented)
            # Use a synthetic stage that represents the entire wave
            wave_stage = Stage(
                name=f"Wave {wave_num}: {', '.join(wave_stage_names)}",
                has_user_facing_changes=has_user_facing,
            )

            test_result, codex_result, runtime_result = await _run_unified_verify(
                wave_stage, task_context, bus,
                run_test_engineer, run_codex_review, run_runtime_verifier, warnings,
            )

            # Checkpoint: post-verify (same as single-stage path)
            decision = await bus.wait_for_decision("post-verify")
            if decision == Decision.SKIP_FIXES:
                for stage in implemented:
                    remaining.remove(stage)
                    sr = StageResult(
                        name=stage.name, status="PASS",
                        test_result=test_result, codex_result=codex_result,
                        runtime_result=runtime_result, fix_attempts=0,
                    )
                    _record(sr)
                    completed_names.add(stage.name)
                    await bus.emit(StageCompleted(stage_name=stage.name, status="PASS"))
                continue
            elif decision == Decision.ABORT:
                for stage in implemented:
                    remaining.remove(stage)
                    sr = StageResult(
                        name=stage.name, status="BLOCKED",
                        test_result=test_result, codex_result=codex_result,
                        runtime_result=runtime_result, fix_attempts=0,
                        recommendation="MUST_STOP",
                    )
                    _record(sr)
                    blocked_names.add(stage.name)
                    await bus.emit(StageCompleted(stage_name=stage.name, status="BLOCKED"))
                continue

            # Phase 3: unified fix loop (if needed)
            failures = collect_failures(test_result, codex_result, runtime_result)
            fix_attempts = 0
            prev_fingerprint: frozenset[tuple[str, str]] | None = None

            for attempt in range(1, 4):
                if not failures:
                    break

                current_fingerprint = _failure_fingerprint(failures)
                if prev_fingerprint is not None and current_fingerprint == prev_fingerprint:
                    warnings.append(f"Wave {wave_num} fix loop: failures unchanged after attempt {attempt - 1}")
                    break
                prev_fingerprint = current_fingerprint

                fix_attempts = attempt
                failure_descriptions = [f["description"] for f in failures]
                await bus.emit(FixLoopStarted(attempt=attempt, max_attempts=3, failures=failure_descriptions))

                decision = await bus.wait_for_decision("pre-fix")
                if decision == Decision.ABORT_FIX_LOOP:
                    break

                await bus.emit(AgentStarted(agent="implementer", model="sonnet", role="fix"))
                t0 = time.time()
                try:
                    fix_prompt = (
                        "The verifiers reported these issues:\n"
                        + "\n".join(f"- {d}" for d in failure_descriptions)
                        + "\n\nFix them by editing the relevant files. "
                        + "Do NOT run tests, typecheck, build, lint, or curl. "
                        + "Do NOT re-explore with Glob/Grep/ls/find — read only "
                        + "the files named in the failures, apply the minimal "
                        + "edit, and return. If a failure is too vague to act "
                        + "on, respond with 'NEEDS_CONTEXT: <what's missing>' "
                        + "and stop."
                    )
                    await query(agent="implementer", prompt=fix_prompt, model="sonnet")
                except Exception as exc:
                    await bus.emit(AgentFailed(agent="implementer", error=str(exc)))
                    warnings.append(f"Wave {wave_num} fix attempt {attempt} failed: {exc}")
                    continue
                else:
                    await bus.emit(AgentCompleted(agent="implementer", duration_s=round(time.time() - t0, 1)))

                # Re-verify: only run tests in fix loop
                await bus.emit(AgentStarted(agent="test-engineer", model="sonnet"))
                re_test_result = await _run_with_timing(run_test_engineer, wave_stage, bus, "test-engineer")
                test_result = re_test_result
                failures = collect_failures(test_result, codex_result, runtime_result)

            if not failures and fix_attempts > 0:
                await bus.emit(FixLoopResolved(attempt=fix_attempts))
            elif failures and fix_attempts > 0:
                await bus.emit(FixLoopExhausted(attempt=fix_attempts, remaining_failures=[f["description"] for f in failures]))

            # Phase 4: gate check for the wave
            recommendation: str | None = None
            wave_status = "PASS"
            if failures:
                if all(f.get("severity") == "warning" for f in failures):
                    recommendation = "SKIP_ALLOWED"
                else:
                    recommendation = "MUST_STOP"
                await bus.emit(GateReached(recommendation=recommendation))
                decision = await bus.wait_for_decision("stage-gate")

                should_block = False
                if recommendation == "MUST_STOP":
                    should_block = not bus.interactive or decision != Decision.CONTINUE
                if decision == Decision.STOP_LOOP:
                    should_block = True
                if should_block:
                    wave_status = "BLOCKED"

            # Handle SKIP_NEXT: mark the first stage of the next wave as skipped
            if decision == Decision.SKIP_NEXT:
                # Find the first remaining stage that would be in the next wave
                next_ready = [
                    s for s in remaining
                    if s not in implemented
                    and all(d in completed_names or d in {st.name for st in implemented} for d in s.depends_on)
                ]
                if next_ready:
                    skip_target = next_ready[0]
                    remaining.remove(skip_target)
                    _record(StageResult(
                        name=skip_target.name, status="SKIPPED",
                        test_result={"passed": 0, "failed": 0},
                        codex_result={"status": "skipped", "has_issues": False, "output": ""},
                        runtime_result=None, fix_attempts=0,
                        recommendation="skipped by user (SKIP_NEXT)",
                    ))
                    await bus.emit(StageCompleted(stage_name=skip_target.name, status="SKIPPED"))

            # Record results for all stages in this wave
            for stage in implemented:
                remaining.remove(stage)
                sr = StageResult(
                    name=stage.name, status=wave_status,
                    test_result=test_result, codex_result=codex_result,
                    runtime_result=runtime_result, fix_attempts=fix_attempts,
                    unresolved=[f["description"] for f in failures] if failures else None,
                    recommendation=recommendation,
                )
                _record(sr)
                await bus.emit(StageCompleted(stage_name=stage.name, status=wave_status))
                if wave_status == "PASS":
                    completed_names.add(stage.name)
                elif wave_status == "BLOCKED":
                    blocked_names.add(stage.name)

    # --- Build summary ---
    passed = sum(1 for r in results if r.status == "PASS")
    blocked = sum(1 for r in results if r.status == "BLOCKED")
    skipped = sum(1 for r in results if r.status == "SKIPPED")

    return SprintResult(
        stages=results,
        warnings=warnings,
        summary={
            "passed": passed,
            "blocked": blocked,
            "skipped": skipped,
            "total": len(results),
        },
    )


# ---------------------------------------------------------------------------
# Wave helpers: implement-only + unified verify
# ---------------------------------------------------------------------------

async def _implement_stage(
    stage: Stage,
    idx: int,
    total_stages: int,
    bus: EventBus,
    query: AgentQueryFn,
    task_context: str,
    warnings: list[str],
) -> None:
    """Run implement for a single stage.

    Used by the wave scheduler for parallel implementation.
    Verification is done separately in _run_unified_verify.
    """
    await bus.emit(StageChanged(stage_name=stage.name, stage_index=idx, total_stages=total_stages))

    await bus.emit(AgentStarted(agent="implementer", model="sonnet"))
    t0 = time.time()
    try:
        impl_prompt = (
            f"Implement ONLY this stage: {stage.name}\n\n"
            f"Look up this stage in the plan above. Use its Files to modify "
            f"list as the path list to Read/Edit — do NOT Glob for them. Use "
            f"its Success Criteria + Tests as your target.\n\n"
            f"DO NOT run tests, typecheck, build, lint, or curl endpoints. "
            f"Verifiers run after you return.\n\n"
            f"If the plan is insufficient (missing Files list, vague Success "
            f"Criteria), respond with a line 'NEEDS_CONTEXT: <what's missing>' "
            f"and stop. Do not explore the codebase to compensate."
        )
        if task_context:
            impl_prompt = f"{task_context}\n\n{impl_prompt}"
        await query(agent="implementer", prompt=impl_prompt, model="sonnet")
    except Exception as exc:
        await bus.emit(AgentFailed(agent="implementer", error=str(exc)))
        warnings.append(f"Implementation failed for {stage.name}: {exc}")
        raise  # propagate to wave handler — don't verify half-finished work
    else:
        await bus.emit(AgentCompleted(agent="implementer", duration_s=round(time.time() - t0, 1)))


async def _run_unified_verify(
    wave_stage: Stage,
    task_context: str,
    bus: EventBus,
    run_test_engineer: TestRunnerFn,
    run_codex_review: CodexRunnerFn,
    run_runtime_verifier: RuntimeRunnerFn,
    warnings: list[str],
) -> tuple[dict, dict, dict | None]:
    """Run test-engineer + codex-review + runtime-verifier once for a wave of stages."""
    verify_tasks: list[asyncio.Task] = []

    await bus.emit(AgentStarted(agent="test-engineer", model="sonnet"))
    verify_tasks.append(asyncio.create_task(
        _run_with_timing(run_test_engineer, wave_stage, bus, "test-engineer")
    ))

    await bus.emit(AgentStarted(agent="codex-review"))
    verify_tasks.append(asyncio.create_task(
        _run_with_timing_no_arg(run_codex_review, bus, "codex-review")
    ))

    if wave_stage.has_user_facing_changes:
        await bus.emit(AgentStarted(agent="runtime-verifier", role="verification"))
        verify_tasks.append(asyncio.create_task(
            _run_with_timing_verifier(run_runtime_verifier, wave_stage.name, task_context, bus, "runtime-verifier")
        ))
    else:
        await bus.emit(AgentSkipped(agent="runtime-verifier", reason="no user-facing changes in wave"))

    raw_results = await asyncio.gather(*verify_tasks, return_exceptions=True)

    test_result = _extract_result(raw_results, 0, {"passed": 0, "failed": 0})
    codex_result = _extract_result(raw_results, 1, {"status": "skipped", "has_issues": False, "output": ""})
    runtime_result = None
    if wave_stage.has_user_facing_changes and len(raw_results) > 2:
        runtime_result = _extract_result(raw_results, 2, {"status": "error", "score": "0/5"})

    for i, r in enumerate(raw_results):
        if isinstance(r, Exception):
            agent_name = ["test-engineer", "codex-review", "runtime-verifier"][i] if i < 3 else f"verify-{i}"
            warnings.append(f"Verification {agent_name} failed with exception: {r}")

    return test_result, codex_result, runtime_result


# ---------------------------------------------------------------------------
# Single-stage execution (used when wave has only 1 stage — full pipeline)
# ---------------------------------------------------------------------------

async def _run_single_stage(
    stage: Stage,
    idx: int,
    total_stages: int,
    bus: EventBus,
    query: AgentQueryFn,
    run_test_engineer: TestRunnerFn,
    run_codex_review: CodexRunnerFn,
    run_runtime_verifier: RuntimeRunnerFn,
    task_context: str,
    warnings: list[str],
) -> StageResult:
    """Execute a single stage: implement → verify → fix loop → gate."""

    await bus.emit(StageChanged(
        stage_name=stage.name,
        stage_index=idx,
        total_stages=total_stages,
    ))

    # --- Checkpoint: pre-implement ---
    decision = await bus.wait_for_decision("pre-implement")
    if decision == Decision.SKIP_STAGE:
        await bus.emit(StageCompleted(stage_name=stage.name, status="SKIPPED"))
        return StageResult(
            name=stage.name, status="SKIPPED",
            test_result={"passed": 0, "failed": 0},
            codex_result={"status": "skipped", "has_issues": False, "output": ""},
            runtime_result=None, fix_attempts=0,
        )

    # --- 1. Implement (mandatory) ---
    await bus.emit(AgentStarted(agent="implementer", model="sonnet"))
    t0 = time.time()
    try:
        impl_prompt = (
            f"Implement ONLY this stage: {stage.name}\n\n"
            f"Look up this stage in the plan above. Use its Files to modify "
            f"list as the path list to Read/Edit — do NOT Glob for them. Use "
            f"its Success Criteria + Tests as your target.\n\n"
            f"DO NOT run tests, typecheck, build, lint, or curl endpoints. "
            f"Verifiers run after you return.\n\n"
            f"If the plan is insufficient (missing Files list, vague Success "
            f"Criteria), respond with a line 'NEEDS_CONTEXT: <what's missing>' "
            f"and stop. Do not explore the codebase to compensate."
        )
        if task_context:
            impl_prompt = f"{task_context}\n\n{impl_prompt}"
        await query(agent="implementer", prompt=impl_prompt, model="sonnet")
    except Exception as exc:
        await bus.emit(AgentFailed(agent="implementer", error=str(exc)))
        warnings.append(f"Implementation failed for {stage.name}: {exc}")
        # Don't verify half-finished work — skip straight to BLOCKED
        await bus.emit(StageCompleted(stage_name=stage.name, status="BLOCKED"))
        return StageResult(
            name=stage.name, status="BLOCKED",
            test_result={"passed": 0, "failed": 0},
            codex_result={"status": "skipped", "has_issues": False, "output": ""},
            runtime_result=None, fix_attempts=0,
            unresolved=[f"implementer failed: {exc}"],
            recommendation="MUST_STOP",
        )
    else:
        await bus.emit(AgentCompleted(agent="implementer", duration_s=round(time.time() - t0, 1)))

    # --- 2. Verify (parallel) ---
    verify_tasks: list[asyncio.Task] = []

    await bus.emit(AgentStarted(agent="test-engineer", model="sonnet"))
    verify_tasks.append(asyncio.create_task(
        _run_with_timing(run_test_engineer, stage, bus, "test-engineer")
    ))

    await bus.emit(AgentStarted(agent="codex-review"))
    verify_tasks.append(asyncio.create_task(
        _run_with_timing_no_arg(run_codex_review, bus, "codex-review")
    ))

    if stage.has_user_facing_changes:
        await bus.emit(AgentStarted(agent="runtime-verifier", role="verification"))
        verify_tasks.append(asyncio.create_task(
            _run_with_timing_verifier(run_runtime_verifier, stage.name, task_context, bus, "runtime-verifier")
        ))
    else:
        await bus.emit(AgentSkipped(agent="runtime-verifier", reason="no user-facing changes"))

    raw_results = await asyncio.gather(*verify_tasks, return_exceptions=True)

    test_result = _extract_result(raw_results, 0, {"passed": 0, "failed": 0})
    codex_result = _extract_result(raw_results, 1, {"status": "skipped", "has_issues": False, "output": ""})
    runtime_result = None
    if stage.has_user_facing_changes and len(raw_results) > 2:
        runtime_result = _extract_result(raw_results, 2, {"status": "error", "score": "0/5"})

    for i, r in enumerate(raw_results):
        if isinstance(r, Exception):
            agent_name = ["test-engineer", "codex-review", "runtime-verifier"][i] if i < 3 else f"verify-{i}"
            warnings.append(f"Verification {agent_name} failed with exception: {r}")

    # --- Checkpoint: post-verify ---
    decision = await bus.wait_for_decision("post-verify")
    if decision == Decision.SKIP_FIXES:
        await bus.emit(StageCompleted(stage_name=stage.name, status="PASS"))
        return StageResult(
            name=stage.name, status="PASS",
            test_result=test_result, codex_result=codex_result,
            runtime_result=runtime_result, fix_attempts=0,
        )
    elif decision == Decision.ABORT:
        await bus.emit(StageCompleted(stage_name=stage.name, status="BLOCKED"))
        return StageResult(
            name=stage.name, status="BLOCKED",
            test_result=test_result, codex_result=codex_result,
            runtime_result=runtime_result, fix_attempts=0,
            recommendation="MUST_STOP",
        )

    # --- 3. Fix loop (max 1 attempt — escalate to team-lead on first failure) ---
    failures = collect_failures(test_result, codex_result, runtime_result)
    fix_attempts = 0
    prev_fingerprint: frozenset[tuple[str, str]] | None = None

    for attempt in range(1, 2):
        if not failures:
            break

        current_fingerprint = _failure_fingerprint(failures)
        if prev_fingerprint is not None and current_fingerprint == prev_fingerprint:
            warnings.append(
                f"Fix loop exited early for {stage.name}: "
                f"failures unchanged after attempt {attempt - 1} "
                f"(structural issue, not fixable by implementer)"
            )
            break
        prev_fingerprint = current_fingerprint

        fix_attempts = attempt
        failure_descriptions = [f["description"] for f in failures]
        await bus.emit(FixLoopStarted(attempt=attempt, max_attempts=1, failures=failure_descriptions))

        decision = await bus.wait_for_decision("pre-fix")
        if decision == Decision.ABORT_FIX_LOOP:
            break

        await bus.emit(AgentStarted(agent="implementer", model="sonnet", role="fix"))
        t0 = time.time()
        try:
            fix_prompt = (
                "The verifiers reported these issues:\n"
                + "\n".join(f"- {d}" for d in failure_descriptions)
                + "\n\nFix them by editing the relevant files. "
                + "Do NOT run tests, typecheck, build, lint, or curl. "
                + "Do NOT re-explore with Glob/Grep/ls/find — read only "
                + "the files named in the failures, apply the minimal "
                + "edit, and return. If a failure is too vague to act "
                + "on, respond with 'NEEDS_CONTEXT: <what's missing>' "
                + "and stop."
            )
            await query(agent="implementer", prompt=fix_prompt, model="sonnet")
        except Exception as exc:
            await bus.emit(AgentFailed(agent="implementer", error=str(exc)))
            warnings.append(f"Fix attempt {attempt} failed for {stage.name}: {exc}")
            continue
        else:
            await bus.emit(AgentCompleted(agent="implementer", duration_s=round(time.time() - t0, 1)))

        # Re-verify: only run tests. Codex review and runtime verification
        # don't need to rerun on every fix attempt — they check code quality
        # and runtime behavior, which rarely changes between fix iterations.
        await bus.emit(AgentStarted(agent="test-engineer", model="sonnet"))
        re_test_result = await _run_with_timing(run_test_engineer, stage, bus, "test-engineer")
        test_result = re_test_result
        # Keep codex_result and runtime_result from the initial verify
        failures = collect_failures(test_result, codex_result, runtime_result)

    # Emit fix loop resolution
    if not failures and fix_attempts > 0:
        await bus.emit(FixLoopResolved(attempt=fix_attempts))
    elif failures and fix_attempts > 0:
        remaining_failures = [f["description"] for f in failures]
        await bus.emit(FixLoopExhausted(attempt=fix_attempts, remaining_failures=remaining_failures))

    # --- 5. Gate check ---
    recommendation: str | None = None
    if failures:
        if all(f.get("severity") == "warning" for f in failures):
            recommendation = "SKIP_ALLOWED"
        else:
            recommendation = "MUST_STOP"

        await bus.emit(GateReached(recommendation=recommendation))
        decision = await bus.wait_for_decision("stage-gate")

        should_block = False
        if recommendation == "MUST_STOP":
            if bus.interactive:
                should_block = decision != Decision.CONTINUE
            else:
                should_block = True
        if decision == Decision.STOP_LOOP:
            should_block = True

        if should_block:
            await bus.emit(StageCompleted(stage_name=stage.name, status="BLOCKED"))
            return StageResult(
                name=stage.name, status="BLOCKED",
                test_result=test_result, codex_result=codex_result,
                runtime_result=runtime_result, fix_attempts=fix_attempts,
                unresolved=[f["description"] for f in failures],
                recommendation=recommendation,
            )

    # Stage passed
    await bus.emit(StageCompleted(stage_name=stage.name, status="PASS"))
    return StageResult(
        name=stage.name, status="PASS",
        test_result=test_result, codex_result=codex_result,
        runtime_result=runtime_result, fix_attempts=fix_attempts,
        unresolved=[f["description"] for f in failures] if failures else None,
        recommendation=recommendation,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _run_with_timing(fn: TestRunnerFn, stage: Stage, bus: EventBus, agent_name: str) -> dict:
    """Run a stage-based verification function and emit completion event."""
    t0 = time.time()
    try:
        result = await fn(stage)
    except Exception as exc:
        await bus.emit(AgentFailed(agent=agent_name, error=str(exc)))
        raise
    else:
        await bus.emit(AgentCompleted(agent=agent_name, duration_s=round(time.time() - t0, 1)))
        return result


async def _run_with_timing_no_arg(fn: CodexRunnerFn, bus: EventBus, agent_name: str) -> dict:
    """Run a no-arg verification function and emit completion event."""
    t0 = time.time()
    try:
        result = await fn()
    except Exception as exc:
        await bus.emit(AgentFailed(agent=agent_name, error=str(exc)))
        raise
    else:
        await bus.emit(AgentCompleted(agent=agent_name, duration_s=round(time.time() - t0, 1)))
        return result


async def _run_with_timing_verifier(
    fn: RuntimeRunnerFn, stage_name: str, task_context: str,
    bus: EventBus, agent_name: str,
) -> dict:
    """Run runtime-verifier and emit completion event."""
    t0 = time.time()
    try:
        result = await fn(stage_name, task_context)
    except Exception as exc:
        await bus.emit(AgentFailed(agent=agent_name, error=str(exc)))
        raise
    else:
        await bus.emit(AgentCompleted(agent=agent_name, duration_s=round(time.time() - t0, 1)))
        return result


def _extract_result(results: list, idx: int, default: dict) -> dict:
    """Safely extract a result from gathered results, returning default on error."""
    if idx < len(results):
        r = results[idx]
        if isinstance(r, Exception):
            return default
        return r
    return default
