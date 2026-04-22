"""Single job runner: implement -> verify -> fix loop.

Extracted from sprint_loop.py::_run_single_stage(). Simplified:
- No wave scheduling / dependency graph
- No contract generation (team-lead's plan already carries Success Criteria)
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

from sdk.agent_dispatch import RateLimitError
from sdk.events import (
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
# (stage_name, task_context) -> dict
RuntimeRunnerFn = Callable[[str, str], Awaitable[dict]]


def _collect_failures(
    test_result: dict,
    codex_result: dict,
    runtime_result: dict | None,
) -> list[dict[str, Any]]:
    """Collect failures from verification results.

    Verifier crashes (status=="error") are treated as blocking errors —
    a silent crash used to return PASS, which masked real failures.

    Severity rules:
    - test-engineer failures: error. Tests are the deterministic truth.
    - runtime-verifier FAIL: error. The verifier runs the live app against
      the plan's Success Criteria; a FAIL means a must-pass criterion is
      broken — functionally the same as a failing test.
    - codex review has_issues: warning. Codex findings are reviewer
      opinions that can be subjective; treat as advisory, not blocking.
    """
    failures: list[dict[str, Any]] = []

    # --- test-engineer ---
    if test_result.get("status") == "error":
        failures.append({
            "source": "test-engineer",
            "description": f"test-engineer crashed: {test_result.get('error', 'unknown')}",
            "severity": "error",
        })
    elif test_result.get("failed", 0) > 0:
        failures.append({
            "source": "test-engineer",
            "description": test_result.get("output", "Test failures detected"),
            "severity": "error",
        })

    # --- codex review ---
    if codex_result.get("status") == "error":
        failures.append({
            "source": "codex-review",
            "description": f"codex-review crashed: {codex_result.get('error', 'unknown')}",
            "severity": "error",
        })
    elif codex_result.get("has_issues"):
        failures.append({
            "source": "codex-review",
            "description": codex_result.get("output", "Issues found"),
            "severity": "warning",
        })

    # --- runtime-verifier ---
    if runtime_result:
        if runtime_result.get("status") == "error":
            failures.append({
                "source": "runtime-verifier",
                "description": f"runtime-verifier crashed: {runtime_result.get('error', 'unknown')}",
                "severity": "error",
            })
        elif runtime_result.get("status") == "FAIL":
            failures.append({
                "source": "runtime-verifier",
                "description": runtime_result.get("output", "Runtime verification failed"),
                "severity": "error",
            })

    return failures


def _failure_fingerprint(failures: list[dict]) -> frozenset:
    """Detect structural issues by comparing failure descriptions across attempts."""
    return frozenset(
        (f["source"], f["description"][:200]) for f in failures
    )


def _extract_needs_context(text: str) -> str | None:
    """Return the message if implementer raised NEEDS_CONTEXT, else None.

    Matches a line 'NEEDS_CONTEXT: <msg>' anywhere in the response. Returns
    the message portion (trimmed). Checks the last 2000 chars first since
    the escape hatch is meant to be terminal output.
    """
    if not text:
        return None
    # Scan last 2000 chars (escape-hatch placement) then full text.
    for chunk in (text[-2000:], text):
        for line in chunk.splitlines():
            stripped = line.strip()
            if stripped.startswith("NEEDS_CONTEXT:"):
                msg = stripped[len("NEEDS_CONTEXT:"):].strip()
                if msg:
                    return msg
    return None


async def run_job(
    *,
    stage: Stage,
    cwd: str,
    bus: EventBus,
    query: AgentQueryFn,
    run_test_engineer: TestRunnerFn,
    run_codex_review: CodexRunnerFn,
    run_runtime_verifier: RuntimeRunnerFn | None,
    task_context: str,
    skip_agents: set[str],
    max_fix_attempts: int = 1,
) -> JobResult:
    """Execute a single job: implement -> verify -> fix loop.

    Checks bus.is_cancelled between each step for graceful interrupt.

    Args:
        stage: Stage definition from plan.
        cwd: Project working directory.
        bus: EventBus for events and cancellation.
        query: Agent dispatch function.
        run_test_engineer: Test runner callback.
        run_codex_review: Codex review callback.
        run_runtime_verifier: Runtime verifier callback (None to skip).
        task_context: SharedContext prompt prefix (plan + prior job context).
        skip_agents: Agents to skip ("test", "codex", "runtime").
        max_fix_attempts: Maximum fix loop iterations. Default 1 — on first
            failure, bubble up to team-lead for route-correction instead of
            blindly retrying.

    Returns:
        JobResult with status PASS, BLOCKED, or INTERRUPTED.
    """
    completed_steps: list[str] = []

    # --- 1. Implement ---
    if bus.is_cancelled:
        return JobResult(status="INTERRUPTED", interrupted_at="implement", completed_steps=completed_steps)

    # Lifecycle emits (AgentStarted/Completed/Failed) now live in
    # dispatcher.query(), so no need to wrap each call site here.
    try:
        impl_prompt = (
            f"Implement ONLY this stage: {stage.name}\n\n"
            f"Look up this stage in the plan above. Use its Files to modify list "
            f"as the path list to Read/Edit — do NOT Glob for them. Use its "
            f"Success Criteria + Tests as your target.\n\n"
            f"DO NOT run tests, typecheck, build, lint, or curl endpoints. "
            f"Verifiers run after you return.\n\n"
            f"If the plan is insufficient (missing Files list, vague Success "
            f"Criteria), respond with a line 'NEEDS_CONTEXT: <what's missing>' "
            f"and stop. Do not explore the codebase to compensate."
        )
        if task_context:
            impl_prompt = f"{task_context}\n\n{impl_prompt}"
        impl_output = await query(agent="implementer", prompt=impl_prompt, model="sonnet")
    except RateLimitError as exc:
        # Infra throttle, not a plan failure. INTERRUPTED pauses the run
        # and keeps stage-1 eligible for resume — BLOCKED would count
        # against the 3-strike retry budget.
        return JobResult(
            status="INTERRUPTED",
            unresolved=[f"rate_limited: {exc}"],
            interrupted_at="implement",
            completed_steps=completed_steps,
        )
    except Exception as exc:
        return JobResult(
            status="BLOCKED",
            unresolved=[f"Implementer failed: {exc}"],
            completed_steps=completed_steps,
        )

    # Check for NEEDS_CONTEXT escape hatch — implementer signals plan is
    # insufficient. Bubble to team-lead as BLOCKED so it can enrich the
    # plan before a blind retry.
    needs_context_msg = _extract_needs_context(impl_output)
    if needs_context_msg:
        return JobResult(
            status="BLOCKED",
            unresolved=[f"NEEDS_CONTEXT: {needs_context_msg}"],
            completed_steps=completed_steps,
        )

    completed_steps.append("implement")

    # --- 2. Verify (parallel) ---
    if bus.is_cancelled:
        return JobResult(status="INTERRUPTED", interrupted_at="verify", completed_steps=completed_steps)

    verify_coros = []
    verify_names = []

    if "test" not in skip_agents:
        verify_coros.append(run_test_engineer(stage))
        verify_names.append("test-engineer")
    if "codex" not in skip_agents:
        verify_coros.append(run_codex_review())
        verify_names.append("codex-review")
    if "runtime" not in skip_agents and run_runtime_verifier:
        verify_coros.append(run_runtime_verifier(stage.name, task_context))
        verify_names.append("runtime-verifier")

    test_result: dict = {"passed": 0, "failed": 0, "output": ""}
    codex_result: dict = {"status": "skipped", "has_issues": False, "output": ""}
    runtime_result: dict | None = None

    if verify_coros:
        # Each run_test_engineer / run_codex_review / run_runtime_verifier
        # emits its own AgentStarted/Completed via dispatcher.query (or
        # the codex-review wrapper). No manual emits needed here.
        raw_results = await asyncio.gather(*verify_coros, return_exceptions=True)

        # Rate limit on any verifier → pause the run. Don't collapse it
        # into a {"status": "error"} dict — that routes to _collect_failures
        # as a verifier crash and counts against retries.
        for r in raw_results:
            if isinstance(r, RateLimitError):
                return JobResult(
                    status="INTERRUPTED",
                    unresolved=[f"rate_limited: {r}"],
                    interrupted_at="verify",
                    completed_steps=completed_steps,
                )

        for i, name in enumerate(verify_names):
            r = raw_results[i]
            if isinstance(r, Exception):
                r = {"status": "error", "error": str(r)}
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
                status="INTERRUPTED",
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

        # Fix attempt — query() emits lifecycle with role="fix" so the
        # dashboard can tell this iteration apart from the initial impl.
        try:
            fix_prompt = (
                "The verifiers reported these issues:\n"
                + "\n".join(f"- {f['description']}" for f in error_failures)
                + "\n\nFix them by editing the relevant files. "
                + "Do NOT run tests, typecheck, build, lint, or curl — "
                + "verification already ran and will run again after you return. "
                + "Do NOT re-explore the codebase with Glob/Grep/ls/find — "
                + "read only the files mentioned in the failures above, "
                + "apply the minimal edit that addresses each issue, and return. "
                + "If a failure description is too vague to act on, respond with "
                + "'NEEDS_CONTEXT: <what's missing>' and stop."
            )
            if task_context:
                fix_prompt = f"{task_context}\n\n{fix_prompt}"
            fix_output = await query(
                agent="implementer", prompt=fix_prompt, model="sonnet", role="fix",
            )
        except RateLimitError as exc:
            return JobResult(
                status="INTERRUPTED",
                test_result=test_result,
                codex_result=codex_result,
                runtime_result=runtime_result,
                fix_attempts=fix_attempts,
                unresolved=[f"rate_limited: {exc}"],
                interrupted_at="fix_loop",
                completed_steps=completed_steps,
            )
        except Exception:
            break

        needs_context_msg = _extract_needs_context(fix_output)
        if needs_context_msg:
            unresolved = f"NEEDS_CONTEXT: {needs_context_msg}"
            await bus.emit(FixLoopExhausted(
                attempt=fix_attempts,
                remaining_failures=[unresolved],
            ))
            return JobResult(
                status="BLOCKED",
                test_result=test_result,
                codex_result=codex_result,
                runtime_result=runtime_result,
                fix_attempts=fix_attempts,
                unresolved=[unresolved],
                completed_steps=completed_steps,
            )

        rerun_runtime = any(f["source"] == "runtime-verifier" for f in error_failures)

        # Re-verify the failing surfaces. run_test_engineer and
        # run_runtime_verifier emit their own lifecycle via dispatcher.query.
        if "test" not in skip_agents:
            try:
                test_result = await run_test_engineer(stage)
            except RateLimitError as exc:
                return JobResult(
                    status="INTERRUPTED",
                    test_result=test_result,
                    codex_result=codex_result,
                    runtime_result=runtime_result,
                    fix_attempts=fix_attempts,
                    unresolved=[f"rate_limited: {exc}"],
                    interrupted_at="fix_loop",
                    completed_steps=completed_steps,
                )
            except Exception:
                break

        if rerun_runtime and "runtime" not in skip_agents and run_runtime_verifier:
            try:
                runtime_result = await run_runtime_verifier(stage.name, task_context)
            except RateLimitError as exc:
                return JobResult(
                    status="INTERRUPTED",
                    test_result=test_result,
                    codex_result=codex_result,
                    runtime_result=runtime_result,
                    fix_attempts=fix_attempts,
                    unresolved=[f"rate_limited: {exc}"],
                    interrupted_at="fix_loop",
                    completed_steps=completed_steps,
                )
            except Exception as exc:
                runtime_result = {"status": "error", "error": str(exc)}

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
            status="BLOCKED",
            test_result=test_result, codex_result=codex_result,
            runtime_result=runtime_result, fix_attempts=fix_attempts,
            unresolved=[f["description"] for f in error_failures],
            completed_steps=completed_steps,
        )

    completed_steps.append("done")
    return JobResult(
        status="PASS",
        test_result=test_result, codex_result=codex_result,
        runtime_result=runtime_result, fix_attempts=fix_attempts,
        completed_steps=completed_steps,
    )
