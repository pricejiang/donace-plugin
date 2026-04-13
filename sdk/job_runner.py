"""Single job runner: contract -> implement -> verify -> fix loop.

Extracted from sprint_loop.py::_run_single_stage(). Simplified:
- No wave scheduling / dependency graph
- No checkpoint wait_for_decision calls (team-lead handles decisions)
- Checks bus.is_cancelled between each step for graceful interrupt
- Returns JobResult dataclass

Used by: sdk/commands.py::cmd_run_job()
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from sdk.events import (
    AgentCompleted,
    AgentFailed,
    AgentSkipped,
    AgentStarted,
    EventBus,
    FixLoopExhausted,
    FixLoopResolved,
    FixLoopStarted,
    Stage,
)


@dataclass
class JobResult:
    """Result of a single job execution."""
    status: str                          # "PASS", "BLOCKED", "INTERRUPTED"
    contract: str = ""
    test_result: dict = field(default_factory=dict)
    codex_result: dict = field(default_factory=dict)
    runtime_result: dict | None = None
    fix_attempts: int = 0
    unresolved: list[str] | None = None
    interrupted_at: str = ""
    completed_steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "contract": self.contract,
            "test_result": self.test_result,
            "codex_result": self.codex_result,
            "runtime_result": self.runtime_result,
            "fix_attempts": self.fix_attempts,
            "unresolved": self.unresolved,
            "interrupted_at": self.interrupted_at,
            "completed_steps": self.completed_steps,
        }


# Type aliases (same as sprint_loop.py)
AgentQueryFn = Callable[..., Awaitable[str]]
TestRunnerFn = Callable[[Stage], Awaitable[dict]]
CodexRunnerFn = Callable[[], Awaitable[dict]]
RuntimeRunnerFn = Callable[[str], Awaitable[dict]]


def _collect_failures(
    test_result: dict,
    codex_result: dict,
    runtime_result: dict | None,
) -> list[dict[str, Any]]:
    """Collect failures from verification results.

    Same logic as sprint_loop.collect_failures():
    test failures = error, codex/runtime = warning.
    """
    failures: list[dict[str, Any]] = []

    if test_result.get("failed", 0) > 0:
        failures.append({
            "source": "test-engineer",
            "description": test_result.get("output", "Test failures detected"),
            "severity": "error",
        })

    if codex_result.get("has_issues"):
        failures.append({
            "source": "codex-review",
            "description": codex_result.get("output", "Issues found"),
            "severity": "warning",
        })

    if runtime_result and runtime_result.get("status") == "FAIL":
        failures.append({
            "source": "runtime-verifier",
            "description": runtime_result.get("output", "Runtime verification failed"),
            "severity": "warning",
        })

    return failures


def _failure_fingerprint(failures: list[dict]) -> frozenset:
    """Detect structural issues by comparing failure descriptions across attempts."""
    return frozenset(
        (f["source"], f["description"][:200]) for f in failures
    )


async def run_job(
    *,
    stage: Stage,
    cwd: str,
    bus: EventBus,
    query: AgentQueryFn,
    run_test_engineer: TestRunnerFn,
    run_codex_review: CodexRunnerFn,
    run_runtime_evaluator: RuntimeRunnerFn | None,
    task_context: str,
    skip_agents: set[str],
    max_fix_attempts: int = 3,
) -> JobResult:
    """Execute a single job: contract -> implement -> verify -> fix loop.

    Checks bus.is_cancelled between each step for graceful interrupt.

    Args:
        stage: Stage definition from plan.
        cwd: Project working directory.
        bus: EventBus for events and cancellation.
        query: Agent dispatch function.
        run_test_engineer: Test runner callback.
        run_codex_review: Codex review callback.
        run_runtime_evaluator: Runtime verifier callback (None to skip).
        task_context: SharedContext prompt prefix.
        skip_agents: Agents to skip ("contract", "test", "codex", "runtime").
        max_fix_attempts: Maximum fix loop iterations.

    Returns:
        JobResult with status PASS, BLOCKED, or INTERRUPTED.
    """
    completed_steps: list[str] = []
    contract = ""

    # --- 1. Contract ---
    if bus.is_cancelled:
        return JobResult(status="INTERRUPTED", interrupted_at="contract", completed_steps=completed_steps)

    if "contract" not in skip_agents:
        await bus.emit(AgentStarted(agent="runtime-evaluator", model="opus", role="contract"))
        t0 = time.time()
        try:
            contract_prompt = (
                f"Write sprint contract for: {stage.name}\n\n"
                f"The architect's plan already contains Success Criteria and Tests for this stage "
                f"(included in the context below). Use those as your starting point — expand them "
                f"into specific, testable criteria with exact HTTP status codes, error messages, "
                f"and data assertions. Do NOT re-explore the codebase from scratch.\n\n"
                f"Context:\n{task_context}"
            ) if task_context else f"Write sprint contract for: {stage.name}"
            contract = await query(agent="runtime-evaluator", prompt=contract_prompt, model="opus")
            await bus.emit(AgentCompleted(agent="runtime-evaluator", duration_s=round(time.time() - t0, 1)))
        except Exception as exc:
            await bus.emit(AgentFailed(agent="runtime-evaluator", error=str(exc)))
            contract = ""
        completed_steps.append("contract")
    else:
        await bus.emit(AgentSkipped(agent="runtime-evaluator", reason="skipped by team-lead"))

    # --- 2. Implement ---
    if bus.is_cancelled:
        return JobResult(status="INTERRUPTED", contract=contract, interrupted_at="implement", completed_steps=completed_steps)

    await bus.emit(AgentStarted(agent="implementer", model="sonnet"))
    t0 = time.time()
    try:
        impl_prompt = (
            f"Implement ONLY this stage: {stage.name}\n\n"
        )
        if contract:
            impl_prompt += f"Contract:\n{contract}\n\n"
        impl_prompt += (
            f"SCOPE: Only modify files listed in this stage's plan. "
            f"Do not explore or read files from other stages."
        )
        if task_context:
            impl_prompt = f"{task_context}\n\n{impl_prompt}"
        await query(agent="implementer", prompt=impl_prompt, model="sonnet")
        await bus.emit(AgentCompleted(agent="implementer", duration_s=round(time.time() - t0, 1)))
    except Exception as exc:
        await bus.emit(AgentFailed(agent="implementer", error=str(exc)))
        return JobResult(
            status="BLOCKED",
            contract=contract,
            unresolved=[f"Implementer failed: {exc}"],
            completed_steps=completed_steps,
        )
    completed_steps.append("implement")

    # --- 3. Verify (parallel) ---
    if bus.is_cancelled:
        return JobResult(status="INTERRUPTED", contract=contract, interrupted_at="verify", completed_steps=completed_steps)

    verify_coros = []
    verify_names = []

    if "test" not in skip_agents:
        verify_coros.append(run_test_engineer(stage))
        verify_names.append("test-engineer")
    if "codex" not in skip_agents:
        verify_coros.append(run_codex_review())
        verify_names.append("codex-review")
    if "runtime" not in skip_agents and run_runtime_evaluator and contract:
        verify_coros.append(run_runtime_evaluator(contract))
        verify_names.append("runtime-verifier")

    test_result: dict = {"passed": 0, "failed": 0, "output": ""}
    codex_result: dict = {"status": "skipped", "has_issues": False, "output": ""}
    runtime_result: dict | None = None

    if verify_coros:
        for name in verify_names:
            await bus.emit(AgentStarted(agent=name))

        raw_results = await asyncio.gather(*verify_coros, return_exceptions=True)

        for i, name in enumerate(verify_names):
            r = raw_results[i]
            if isinstance(r, Exception):
                await bus.emit(AgentFailed(agent=name, error=str(r)))
                r = {"status": "error", "error": str(r)}
            else:
                await bus.emit(AgentCompleted(agent=name, duration_s=0))

            if name == "test-engineer":
                test_result = r
            elif name == "codex-review":
                codex_result = r
            elif name == "runtime-verifier":
                runtime_result = r

    completed_steps.append("verify")

    # --- 4. Fix loop ---
    failures = _collect_failures(test_result, codex_result, runtime_result)
    error_failures = [f for f in failures if f["severity"] == "error"]
    fix_attempts = 0
    prev_fingerprint = None

    while error_failures and fix_attempts < max_fix_attempts:
        if bus.is_cancelled:
            return JobResult(
                status="INTERRUPTED", contract=contract,
                test_result=test_result, codex_result=codex_result,
                runtime_result=runtime_result, fix_attempts=fix_attempts,
                interrupted_at="fix_loop", completed_steps=completed_steps,
            )

        fix_attempts += 1
        fp = _failure_fingerprint(error_failures)
        if fp == prev_fingerprint:
            break  # Structural issue — same failures, stop trying
        prev_fingerprint = fp

        await bus.emit(FixLoopStarted(
            attempt=fix_attempts, max_attempts=max_fix_attempts,
            failures=[f["description"][:100] for f in error_failures],
        ))

        # Fix attempt
        await bus.emit(AgentStarted(agent="implementer", model="sonnet", role="fix"))
        t0 = time.time()
        try:
            fix_prompt = (
                "Fix these issues:\n"
                + "\n".join(f"- {f['description']}" for f in error_failures)
            )
            if task_context:
                fix_prompt = f"{task_context}\n\n{fix_prompt}"
            await query(agent="implementer", prompt=fix_prompt, model="sonnet")
            await bus.emit(AgentCompleted(agent="implementer", duration_s=round(time.time() - t0, 1)))
        except Exception as exc:
            await bus.emit(AgentFailed(agent="implementer", error=str(exc)))
            break

        # Re-verify: only tests (codex/runtime don't change between fix iterations)
        if "test" not in skip_agents:
            await bus.emit(AgentStarted(agent="test-engineer", model="sonnet"))
            try:
                test_result = await run_test_engineer(stage)
                await bus.emit(AgentCompleted(agent="test-engineer", duration_s=0))
            except Exception as exc:
                await bus.emit(AgentFailed(agent="test-engineer", error=str(exc)))
                break

        failures = _collect_failures(test_result, codex_result, runtime_result)
        error_failures = [f for f in failures if f["severity"] == "error"]

    # Emit fix loop result
    if not error_failures and fix_attempts > 0:
        await bus.emit(FixLoopResolved(attempt=fix_attempts))
    elif error_failures and fix_attempts > 0:
        await bus.emit(FixLoopExhausted(
            attempt=fix_attempts,
            remaining_failures=[f["description"][:100] for f in error_failures],
        ))

    if error_failures:
        return JobResult(
            status="BLOCKED", contract=contract,
            test_result=test_result, codex_result=codex_result,
            runtime_result=runtime_result, fix_attempts=fix_attempts,
            unresolved=[f["description"] for f in error_failures],
            completed_steps=completed_steps,
        )

    completed_steps.append("done")
    return JobResult(
        status="PASS", contract=contract,
        test_result=test_result, codex_result=codex_result,
        runtime_result=runtime_result, fix_attempts=fix_attempts,
        completed_steps=completed_steps,
    )
