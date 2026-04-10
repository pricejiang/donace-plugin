"""Phase 2 inner loop: per-stage contract -> implement -> verify (parallel) -> fix loop -> gate check."""
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

# run_runtime_evaluator(contract) -> dict  {"status": str, "score": str, "output": str}
RuntimeRunnerFn = Callable[[str], Awaitable[dict]]


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

    # Codex findings — raw output passed through, not parsed by us
    if codex_result.get("has_issues"):
        failures.append({
            "source": "codex-review",
            "description": codex_result.get("output", "codex review found issues"),
            "severity": "error",
        })

    # Runtime failures
    if runtime_result and runtime_result.get("status") == "FAIL":
        failures.append({
            "source": "runtime-evaluator",
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
    run_runtime_evaluator: RuntimeRunnerFn,
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
        run_runtime_evaluator: Function to run runtime evaluator with a contract.
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
    must_stop = False
    skip_next = False

    def _record(sr: StageResult) -> None:
        results.append(sr)
        if on_stage_complete:
            on_stage_complete(sr)

    for idx, stage in enumerate(stages):
        # Skip stages already completed in a previous run
        if stage.name in _completed:
            await bus.emit(StageCompleted(stage_name=stage.name, status="PASS"))
            warnings.append(f"Stage '{stage.name}' already completed in previous run — skipped")
            continue

        if skip_next:
            skip_next = False
            await bus.emit(StageCompleted(stage_name=stage.name, status="SKIPPED"))
            _record(StageResult(
                name=stage.name,
                status="SKIPPED",
                contract="",
                test_result={"passed": 0, "failed": 0},
                codex_result={"status": "skipped", "p1_findings": 0, "findings": []},
                runtime_result=None,
                fix_attempts=0,
            ))
            continue

        if must_stop:
            # Record remaining stages as skipped
            _record(StageResult(
                name=stage.name,
                status="SKIPPED",
                contract="",
                test_result={"passed": 0, "failed": 0},
                codex_result={"status": "skipped", "p1_findings": 0, "findings": []},
                runtime_result=None,
                fix_attempts=0,
                recommendation=None,
            ))
            continue

        await bus.emit(StageChanged(
            stage_name=stage.name,
            stage_index=idx,
            total_stages=len(stages),
        ))

        # --- 1. Sprint contract (mandatory) ---
        await bus.emit(AgentStarted(agent="runtime-evaluator", role="contract"))
        t0 = time.time()
        try:
            contract_prompt = (
                    f"Write sprint contract for: {stage.name}\n\n"
                    f"Context:\n{task_context}"
                ) if task_context else f"Write sprint contract for: {stage.name}"
            contract = await query(
                agent="runtime-evaluator",
                prompt=contract_prompt,
                model="opus",
            )
        except Exception as exc:
            await bus.emit(AgentFailed(agent="runtime-evaluator", error=str(exc)))
            contract = f"[contract generation failed: {exc}]"
            warnings.append(f"Contract generation failed for {stage.name}: {exc}")
        else:
            await bus.emit(AgentCompleted(
                agent="runtime-evaluator",
                duration_s=round(time.time() - t0, 1),
            ))

        # --- Checkpoint: pre-implement ---
        decision = await bus.wait_for_decision("pre-implement")
        if decision == Decision.SKIP_STAGE:
            await bus.emit(StageCompleted(stage_name=stage.name, status="SKIPPED"))
            _record(StageResult(
                name=stage.name,
                status="SKIPPED",
                contract=contract,
                test_result={"passed": 0, "failed": 0},
                codex_result={"status": "skipped", "p1_findings": 0, "findings": []},
                runtime_result=None,
                fix_attempts=0,
            ))
            continue

        # --- 2. Implement (mandatory) ---
        await bus.emit(AgentStarted(agent="implementer", model="sonnet"))
        t0 = time.time()
        try:
            impl_prompt = (
                    f"Implement: {stage.name}\n\n"
                    f"Contract:\n{contract}"
                )
            if task_context:
                impl_prompt = f"{task_context}\n\n{impl_prompt}"
            await query(
                agent="implementer",
                prompt=impl_prompt,
                model="sonnet",
            )
        except Exception as exc:
            await bus.emit(AgentFailed(agent="implementer", error=str(exc)))
            warnings.append(f"Implementation failed for {stage.name}: {exc}")
        else:
            await bus.emit(AgentCompleted(
                agent="implementer",
                duration_s=round(time.time() - t0, 1),
            ))

        # --- 3. Verify (parallel: test + codex mandatory, runtime conditional) ---
        verify_tasks: list[asyncio.Task] = []

        # Test engineer (always runs)
        await bus.emit(AgentStarted(agent="test-engineer", model="sonnet"))
        verify_tasks.append(asyncio.create_task(
            _run_with_timing(run_test_engineer, stage, bus, "test-engineer")
        ))

        # Codex review (always runs)
        await bus.emit(AgentStarted(agent="codex-review"))
        verify_tasks.append(asyncio.create_task(
            _run_with_timing_no_arg(run_codex_review, bus, "codex-review")
        ))

        # Runtime evaluator (conditional)
        runtime_task: asyncio.Task | None = None
        if stage.has_user_facing_changes:
            await bus.emit(AgentStarted(agent="runtime-evaluator", role="verification"))
            runtime_task = asyncio.create_task(
                _run_with_timing_str_arg(run_runtime_evaluator, contract, bus, "runtime-evaluator")
            )
            verify_tasks.append(runtime_task)
        else:
            await bus.emit(AgentSkipped(
                agent="runtime-evaluator",
                reason="no user-facing changes",
            ))

        # Gather all
        raw_results = await asyncio.gather(*verify_tasks, return_exceptions=True)

        test_result = _extract_result(raw_results, 0, {"passed": 0, "failed": 0})
        codex_result = _extract_result(raw_results, 1, {"status": "skipped", "has_issues": False, "output": ""})
        runtime_result = None
        if stage.has_user_facing_changes and len(raw_results) > 2:
            runtime_result = _extract_result(raw_results, 2, {"status": "error", "score": "0/5"})
        elif not stage.has_user_facing_changes:
            runtime_result = None

        # Log exceptions (AgentFailed already emitted by _run_with_timing helpers)
        for i, r in enumerate(raw_results):
            if isinstance(r, Exception):
                agent_name = ["test-engineer", "codex-review", "runtime-evaluator"][i] if i < 3 else f"verify-{i}"
                warnings.append(f"Verification {agent_name} failed with exception: {r}")

        # --- Checkpoint: post-verify ---
        decision = await bus.wait_for_decision("post-verify")
        if decision == Decision.SKIP_FIXES:
            await bus.emit(StageCompleted(stage_name=stage.name, status="PASS"))
            _record(StageResult(
                name=stage.name,
                status="PASS",
                contract=contract,
                test_result=test_result,
                codex_result=codex_result,
                runtime_result=runtime_result,
                fix_attempts=0,
            ))
            continue
        elif decision == Decision.ABORT:
            await bus.emit(StageCompleted(stage_name=stage.name, status="BLOCKED"))
            _record(StageResult(
                name=stage.name,
                status="BLOCKED",
                contract=contract,
                test_result=test_result,
                codex_result=codex_result,
                runtime_result=runtime_result,
                fix_attempts=0,
                recommendation="MUST_STOP",
            ))
            must_stop = True
            continue

        # --- 4. Fix loop (max 3 attempts) ---
        failures = collect_failures(test_result, codex_result, runtime_result)
        fix_attempts = 0
        prev_fingerprint: frozenset[tuple[str, str]] | None = None

        for attempt in range(1, 4):
            if not failures:
                break

            # Detect structural (unfixable) failures: if the failure fingerprint
            # is identical to the previous iteration, the implementer cannot fix
            # them and further attempts just waste tokens.
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
            await bus.emit(FixLoopStarted(
                attempt=attempt,
                max_attempts=3,
                failures=failure_descriptions,
            ))

            # Checkpoint: pre-fix
            decision = await bus.wait_for_decision("pre-fix")
            if decision == Decision.ABORT_FIX_LOOP:
                break

            # Fix
            await bus.emit(AgentStarted(agent="implementer", model="sonnet", role="fix"))
            t0 = time.time()
            try:
                await query(
                    agent="implementer",
                    prompt=f"Fix these issues:\n" + "\n".join(failure_descriptions),
                    model="sonnet",
                )
            except Exception as exc:
                await bus.emit(AgentFailed(agent="implementer", error=str(exc)))
                warnings.append(f"Fix attempt {attempt} failed for {stage.name}: {exc}")
                continue
            else:
                await bus.emit(AgentCompleted(
                    agent="implementer",
                    duration_s=round(time.time() - t0, 1),
                ))

            # Re-verify (test + codex always, runtime only if it ran initially)
            await bus.emit(AgentStarted(agent="test-engineer", model="sonnet"))
            await bus.emit(AgentStarted(agent="codex-review"))
            re_verify_tasks: list[asyncio.Task] = [
                asyncio.create_task(_run_with_timing(run_test_engineer, stage, bus, "test-engineer")),
                asyncio.create_task(_run_with_timing_no_arg(run_codex_review, bus, "codex-review")),
            ]
            if stage.has_user_facing_changes and runtime_result is not None:
                await bus.emit(AgentStarted(agent="runtime-evaluator", role="verification"))
                re_verify_tasks.append(asyncio.create_task(
                    _run_with_timing_str_arg(run_runtime_evaluator, contract, bus, "runtime-evaluator")
                ))
            re_results = await asyncio.gather(*re_verify_tasks, return_exceptions=True)

            test_result = _extract_result(re_results, 0, {"passed": 0, "failed": 0})
            codex_result = _extract_result(re_results, 1, {"status": "skipped", "has_issues": False, "output": ""})
            if stage.has_user_facing_changes and len(re_results) > 2:
                runtime_result = _extract_result(re_results, 2, {"status": "error", "score": "0/5"})
            failures = collect_failures(test_result, codex_result, runtime_result)

        # Emit fix loop resolution status
        if not failures and fix_attempts > 0:
            await bus.emit(FixLoopResolved(attempt=fix_attempts))
        elif failures and fix_attempts > 0:
            # Fires on max attempts reached OR user abort via ABORT_FIX_LOOP
            remaining = [f["description"] for f in failures]
            await bus.emit(FixLoopExhausted(attempt=fix_attempts, remaining_failures=remaining))

        # --- 5. Gate check ---
        recommendation: str | None = None
        if failures:
            if all(f.get("severity") == "warning" for f in failures):
                recommendation = "SKIP_ALLOWED"
            else:
                recommendation = "MUST_STOP"

            await bus.emit(GateReached(recommendation=recommendation))

            decision = await bus.wait_for_decision("stage-gate")

            # MUST_STOP blocks by default. In interactive mode, only an explicit
            # user CONTINUE overrides it. In non-interactive mode (where
            # wait_for_decision returns CONTINUE automatically), MUST_STOP
            # always blocks — the orchestrator cannot silently proceed past
            # error-severity failures.
            should_block = False
            if recommendation == "MUST_STOP":
                if bus.interactive:
                    # User explicitly chose — respect their decision
                    should_block = decision != Decision.CONTINUE
                else:
                    # Non-interactive: MUST_STOP always blocks
                    should_block = True
            if decision == Decision.STOP_LOOP:
                should_block = True
            if decision == Decision.SKIP_NEXT:
                skip_next = True

            if should_block:
                await bus.emit(StageCompleted(stage_name=stage.name, status="BLOCKED"))
                _record(StageResult(
                    name=stage.name,
                    status="BLOCKED",
                    contract=contract,
                    test_result=test_result,
                    codex_result=codex_result,
                    runtime_result=runtime_result,
                    fix_attempts=fix_attempts,
                    unresolved=[f["description"] for f in failures],
                    recommendation=recommendation,
                ))
                must_stop = True
                continue
        # Stage passed — warnings are acceptable, gate check already handled MUST_STOP
        status = "PASS"
        await bus.emit(StageCompleted(stage_name=stage.name, status=status))
        _record(StageResult(
            name=stage.name,
            status=status,
            contract=contract,
            test_result=test_result,
            codex_result=codex_result,
            runtime_result=runtime_result,
            fix_attempts=fix_attempts,
            unresolved=[f["description"] for f in failures] if failures else None,
            recommendation=recommendation,
        ))

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


async def _run_with_timing_str_arg(fn: RuntimeRunnerFn, arg: str, bus: EventBus, agent_name: str) -> dict:
    """Run a string-arg verification function and emit completion event."""
    t0 = time.time()
    try:
        result = await fn(arg)
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
