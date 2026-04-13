# Hybrid Orchestration Phase 1: Infrastructure

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor orchestrator from monolithic pipeline runner into a subcommand-based toolbox that team-lead can invoke independently.

**Architecture:** Split `orchestrator.py::run()` into independent CLI subcommands (`run_start`, `run_complete`, `plan`, `run_job`, `verify`, `review`, `document`). Refactor `sprint_loop.py` from wave-based scheduler into single job runner. Add EventBus cancellation flag and interrupt support. Keep old `--task` entry for backwards compat.

**Tech Stack:** Python 3.11+, claude-agent-sdk, FastAPI, argparse subparsers

**Design Doc:** `/HYBRID_ORCHESTRATION_DESIGN.md`

---

## File Structure

### New Files
- `sdk/job_runner.py` — Single job execution: contract → implement → verify → fix loop. Extracted and simplified from `sprint_loop.py`.
- `sdk/commands.py` — Subcommand implementations: `cmd_run_start`, `cmd_run_complete`, `cmd_plan`, `cmd_run_job`, `cmd_verify`, `cmd_review`, `cmd_document`.
- `tests/test_events_cancel.py` — Tests for EventBus cancellation flag.
- `tests/test_job_runner.py` — Tests for single job runner.
- `tests/test_commands.py` — Tests for CLI subcommands.

### Modified Files
- `sdk/events.py` — Add cancellation flag to EventBus. Add job-level events (`JobStarted`, `JobCompleted`, `JobInterrupted`, `JobRegistered`).
- `sdk/emitter.py` — Handle "interrupt" action in `_listen_controls()`. Connect control WebSocket with job_id query param.
- `sdk/orchestrator.py` — Replace single `main()` with argparse subparsers. Keep `run()` as `--task` compat. Replace cwd-level lock with job-level registration.
- `sdk/dashboard.py` — Add `/api/jobs/active` and `/api/interrupt` endpoints. Add job registry.
- `sdk/run_validator.py` — Accept job-level results in addition to SprintResult.

### Unchanged Files
- `sdk/agent_dispatch.py` — All agent dispatch logic stays as-is for Phase 1. Sub-implementer removal is Phase 3.
- `sdk/static/index.html` — UI changes deferred to Phase 4.
- All agent `.md` files — Unchanged until Phase 3.

---

### Task 1: EventBus Cancellation Flag

**Files:**
- Modify: `sdk/events.py` (EventBus class, lines 447-532)
- Create: `tests/test_events_cancel.py`

- [ ] **Step 1: Write failing tests for cancellation**

```python
# tests/test_events_cancel.py
"""Tests for EventBus cancellation flag."""
import asyncio
import pytest
from sdk.events import EventBus


def test_bus_not_cancelled_by_default():
    bus = EventBus(run_id="test-run")
    assert bus.is_cancelled is False
    assert bus.cancel_reason == ""


def test_bus_cancel_sets_flag():
    bus = EventBus(run_id="test-run")
    bus.cancel("user requested")
    assert bus.is_cancelled is True
    assert bus.cancel_reason == "user requested"


def test_bus_cancel_is_irreversible():
    bus = EventBus(run_id="test-run")
    bus.cancel("reason")
    # No way to un-cancel
    assert bus.is_cancelled is True


@pytest.mark.asyncio
async def test_bus_cancel_resolves_pending_checkpoint():
    """If a checkpoint is waiting, cancel should resolve it with ABORT."""
    bus = EventBus(run_id="test-run", interactive=True)

    async def cancel_after_delay():
        await asyncio.sleep(0.1)
        bus.cancel("user abort")

    asyncio.create_task(cancel_after_delay())
    from sdk.events import Decision
    decision = await bus.wait_for_decision("post-verify")
    assert decision == Decision.ABORT
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd /Users/minghaojiang/Developer/donace && python -m pytest tests/test_events_cancel.py -v`
Expected: FAIL — `EventBus` has no `is_cancelled`, `cancel_reason`, or `cancel()` method.

- [ ] **Step 3: Implement cancellation flag on EventBus**

In `sdk/events.py`, add to `EventBus.__init__()`:

```python
self._cancelled = False
self._cancel_reason = ""
```

Add methods:

```python
def cancel(self, reason: str) -> None:
    """Mark this bus as cancelled. Irreversible. Unblocks any pending checkpoint."""
    self._cancelled = True
    self._cancel_reason = reason
    # Unblock any pending wait_for_decision
    for fut in self._pending_decisions.values():
        if not fut.done():
            fut.set_result(Decision.ABORT)

@property
def is_cancelled(self) -> bool:
    return self._cancelled

@property
def cancel_reason(self) -> str:
    return self._cancel_reason
```

In `EventBus.__init__()`, add a dict to track pending decision futures:

```python
self._pending_decisions: dict[str, asyncio.Future] = {}
```

In `EventBus.wait_for_decision()`, register the future before waiting:

```python
async def wait_for_decision(self, checkpoint: str) -> Decision:
    if self._cancelled:
        return Decision.ABORT
    # ... existing checkpoint logic, but store future in self._pending_decisions[checkpoint]
    # ... clean up from self._pending_decisions after resolved
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `cd /Users/minghaojiang/Developer/donace && python -m pytest tests/test_events_cancel.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add sdk/events.py tests/test_events_cancel.py
git commit -m "feat: add cancellation flag to EventBus for graceful interrupt support"
```

---

### Task 2: Job-Level Events

**Files:**
- Modify: `sdk/events.py` (add new Event subclasses after existing event definitions)

- [ ] **Step 1: Add job-level event classes**

Add these event classes in `sdk/events.py` after the existing event definitions (around line 440):

```python
@dataclass
class JobRegistered(Event):
    """Emitted when an orchestrator process registers with dashboard."""
    job_id: str = ""
    command: str = ""       # "plan", "run_job", "verify", "review", "document"
    stage_id: str = ""      # Only for run_job
    pid: int = 0

    def __post_init__(self):
        self.type = "job.registered"
        super().__post_init__()


@dataclass
class JobStarted(Event):
    """Emitted when a job begins execution."""
    job_id: str = ""
    command: str = ""

    def __post_init__(self):
        self.type = "job.started"
        super().__post_init__()


@dataclass
class JobCompleted(Event):
    """Emitted when a job finishes (pass or block)."""
    job_id: str = ""
    command: str = ""
    status: str = ""        # "PASS", "BLOCKED", "ERROR"
    result_summary: str = ""

    def __post_init__(self):
        self.type = "job.completed"
        super().__post_init__()


@dataclass
class JobInterrupted(Event):
    """Emitted when a job is interrupted by user."""
    job_id: str = ""
    command: str = ""
    reason: str = ""
    completed_steps: list = field(default_factory=list)
    interrupted_at: str = ""

    def __post_init__(self):
        self.type = "job.interrupted"
        super().__post_init__()
```

- [ ] **Step 2: Verify import works**

Run: `cd /Users/minghaojiang/Developer/donace && python -c "from sdk.events import JobRegistered, JobStarted, JobCompleted, JobInterrupted; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add sdk/events.py
git commit -m "feat: add job-level event types for hybrid orchestration"
```

---

### Task 3: Emitter Interrupt Support

**Files:**
- Modify: `sdk/emitter.py` (lines 96-115, `_listen_controls()`)

- [ ] **Step 1: Extend _listen_controls() to handle interrupt**

In `sdk/emitter.py`, modify `_listen_controls()` to handle both "resolve" and "interrupt" actions:

```python
async def _listen_controls(self) -> None:
    """Receive user decisions and interrupt signals from dashboard."""
    if not self._control_ws:
        return

    try:
        async for msg in self._control_ws:
            try:
                data = json.loads(msg)
                action = data.get("action")

                if action == "resolve":
                    checkpoint = data["checkpoint"]
                    decision = Decision(data["decision"])
                    self.bus.resolve(checkpoint, decision)

                elif action == "interrupt":
                    reason = data.get("reason", "user requested")
                    self.bus.cancel(reason)

            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                print(f"Warning: Invalid control message: {exc}", file=sys.stderr)
    except asyncio.CancelledError:
        pass
    except Exception:
        self._control_ws = None
```

- [ ] **Step 2: Add job_id to control WebSocket connection URL**

In `sdk/emitter.py`, modify `connect()` to accept and use `job_id`:

```python
class WebSocketEmitter:
    def __init__(self, dashboard_url: str, bus: EventBus, job_id: str = ""):
        self._dashboard_url = dashboard_url
        self.bus = bus
        self._job_id = job_id
        # ... rest unchanged

    async def connect(self) -> None:
        try:
            import websockets
            ingest_url = f"{self._dashboard_url}/api/ingest"
            control_url = f"{self._dashboard_url}/api/control"
            if self._job_id:
                control_url += f"?job_id={self._job_id}"
            # ... rest unchanged
```

- [ ] **Step 3: Verify existing functionality still works**

Run: `cd /Users/minghaojiang/Developer/donace && python -c "from sdk.emitter import WebSocketEmitter; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add sdk/emitter.py
git commit -m "feat: add interrupt handling and job_id binding to WebSocket emitter"
```

---

### Task 4: Single Job Runner

**Files:**
- Create: `sdk/job_runner.py`
- Create: `tests/test_job_runner.py`

This extracts the core implement → verify → fix loop from `sprint_loop.py::_run_single_stage()` into a standalone, reusable function.

- [ ] **Step 1: Write failing test for job runner**

```python
# tests/test_job_runner.py
"""Tests for single job runner."""
import asyncio
import pytest
from sdk.events import EventBus, Stage
from sdk.job_runner import run_job, JobResult


@pytest.mark.asyncio
async def test_run_job_pass():
    """Job passes when implement succeeds and tests pass."""
    bus = EventBus(run_id="test-run")
    stage = Stage(name="Test stage", has_user_facing_changes=False)

    async def mock_query(agent, prompt, model="sonnet", **kw):
        return "Implementation complete."

    async def mock_test(s):
        return {"passed": 3, "failed": 0, "output": "All pass"}

    async def mock_codex():
        return {"status": "clean", "has_issues": False, "output": ""}

    result = await run_job(
        stage=stage,
        cwd="/tmp/test",
        bus=bus,
        query=mock_query,
        run_test_engineer=mock_test,
        run_codex_review=mock_codex,
        run_runtime_evaluator=None,
        task_context="",
        skip_agents=set(),
        max_fix_attempts=3,
    )

    assert result.status == "PASS"
    assert result.test_result["failed"] == 0


@pytest.mark.asyncio
async def test_run_job_blocked_on_implement_failure():
    """Job is BLOCKED if implementer fails."""
    bus = EventBus(run_id="test-run")
    stage = Stage(name="Test stage", has_user_facing_changes=False)

    async def mock_query(agent, prompt, model="sonnet", **kw):
        raise RuntimeError("Agent timed out")

    async def mock_test(s):
        return {"passed": 0, "failed": 0, "output": ""}

    async def mock_codex():
        return {"status": "clean", "has_issues": False, "output": ""}

    result = await run_job(
        stage=stage,
        cwd="/tmp/test",
        bus=bus,
        query=mock_query,
        run_test_engineer=mock_test,
        run_codex_review=mock_codex,
        run_runtime_evaluator=None,
        task_context="",
        skip_agents=set(),
        max_fix_attempts=3,
    )

    assert result.status == "BLOCKED"


@pytest.mark.asyncio
async def test_run_job_respects_cancellation():
    """Job returns INTERRUPTED if bus is cancelled."""
    bus = EventBus(run_id="test-run")
    bus.cancel("user abort")
    stage = Stage(name="Test stage", has_user_facing_changes=False)

    async def mock_query(agent, prompt, model="sonnet", **kw):
        return "done"

    async def mock_test(s):
        return {"passed": 1, "failed": 0, "output": ""}

    async def mock_codex():
        return {"status": "clean", "has_issues": False, "output": ""}

    result = await run_job(
        stage=stage,
        cwd="/tmp/test",
        bus=bus,
        query=mock_query,
        run_test_engineer=mock_test,
        run_codex_review=mock_codex,
        run_runtime_evaluator=None,
        task_context="",
        skip_agents=set(),
        max_fix_attempts=3,
    )

    assert result.status == "INTERRUPTED"


@pytest.mark.asyncio
async def test_run_job_skips_agents():
    """Skip contract and codex when specified."""
    bus = EventBus(run_id="test-run")
    stage = Stage(name="Test stage", has_user_facing_changes=False)
    agents_called = []

    async def mock_query(agent, prompt, model="sonnet", **kw):
        agents_called.append(agent)
        return "done"

    async def mock_test(s):
        return {"passed": 1, "failed": 0, "output": ""}

    async def mock_codex():
        agents_called.append("codex")
        return {"status": "clean", "has_issues": False, "output": ""}

    result = await run_job(
        stage=stage,
        cwd="/tmp/test",
        bus=bus,
        query=mock_query,
        run_test_engineer=mock_test,
        run_codex_review=mock_codex,
        run_runtime_evaluator=None,
        task_context="",
        skip_agents={"contract", "codex"},
        max_fix_attempts=3,
    )

    assert result.status == "PASS"
    assert "runtime-evaluator" not in agents_called
    assert "codex" not in agents_called
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd /Users/minghaojiang/Developer/donace && python -m pytest tests/test_job_runner.py -v`
Expected: FAIL — `sdk.job_runner` does not exist.

- [ ] **Step 3: Implement job_runner.py**

Create `sdk/job_runner.py`. This is extracted and simplified from `sprint_loop.py::_run_single_stage()` (lines 552-776). Key simplifications:
- No wave scheduling / dependency graph
- No checkpoint `wait_for_decision` calls (team-lead makes those decisions)
- Returns `JobResult` dataclass instead of `StageResult`
- Checks `bus.is_cancelled` between each step

```python
"""Single job runner: contract → implement → verify → fix loop.

Extracted from sprint_loop.py. Each run_job orchestrator invocation
uses this to execute one stage's complete development cycle.
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
    changed_files: list[str] = field(default_factory=list)

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
            "changed_files": self.changed_files,
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
    """Collect failures from verification results."""
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
    """Create comparable fingerprint to detect structural (unfixable) issues."""
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
    """Execute a single job: contract → implement → verify → fix loop.

    This is the core execution unit. team-lead invokes this via
    `python3 -m sdk.orchestrator run_job`.

    Args:
        stage: Stage definition from plan JSON.
        cwd: Project working directory.
        bus: EventBus for emitting events and checking cancellation.
        query: Agent dispatch function.
        run_test_engineer: Test runner callback.
        run_codex_review: Codex review callback.
        run_runtime_evaluator: Runtime verifier callback (None to skip).
        task_context: SharedContext prompt prefix.
        skip_agents: Set of agents to skip ("contract", "test", "codex", "runtime").
        max_fix_attempts: Maximum fix loop iterations.

    Returns:
        JobResult with status PASS, BLOCKED, or INTERRUPTED.
    """
    completed_steps: list[str] = []
    contract = ""

    # --- Step 1: Contract ---
    if bus.is_cancelled:
        return JobResult(status="INTERRUPTED", interrupted_at="contract", completed_steps=completed_steps)

    if "contract" not in skip_agents:
        try:
            await bus.emit(AgentStarted(agent="runtime-evaluator", model="opus", prompt="", role="contract"))
            contract_prompt = (
                f"{task_context}\n\n"
                f"Stage: {stage.name}\n\n"
                f"Write specific, testable acceptance criteria for this stage. "
                f"Expand the architect's success criteria into concrete test cases."
            )
            contract = await query("runtime-evaluator", contract_prompt, model="opus")
            await bus.emit(AgentCompleted(agent="runtime-evaluator", duration_s=0, result_summary=contract[:200]))
            completed_steps.append("contract")
        except Exception as exc:
            await bus.emit(AgentFailed(agent="runtime-evaluator", error=str(exc)))
            # Contract failure is not fatal — continue without contract
            contract = ""
            completed_steps.append("contract (failed, continuing)")
    else:
        await bus.emit(AgentSkipped(agent="runtime-evaluator", reason="skipped by team-lead"))

    # --- Step 2: Implement ---
    if bus.is_cancelled:
        return JobResult(status="INTERRUPTED", contract=contract, interrupted_at="implement", completed_steps=completed_steps)

    try:
        await bus.emit(AgentStarted(agent="implementer", model="sonnet", prompt="", role="implement"))
        impl_prompt = (
            f"{task_context}\n\n"
            f"## Task\n{stage.name}\n\n"
        )
        if contract:
            impl_prompt += f"## Acceptance Criteria\n{contract}\n\n"
        impl_prompt += (
            f"## SCOPE\nOnly modify files relevant to this stage. "
            f"Do not modify files belonging to other stages."
        )
        impl_result = await query("implementer", impl_prompt, model="sonnet")
        await bus.emit(AgentCompleted(agent="implementer", duration_s=0, result_summary=impl_result[:200]))
        completed_steps.append("implement")
    except Exception as exc:
        await bus.emit(AgentFailed(agent="implementer", error=str(exc)))
        return JobResult(
            status="BLOCKED",
            contract=contract,
            unresolved=[f"Implementer failed: {exc}"],
            completed_steps=completed_steps,
        )

    # --- Step 3: Verify ---
    if bus.is_cancelled:
        return JobResult(status="INTERRUPTED", contract=contract, interrupted_at="verify", completed_steps=completed_steps)

    # Run verification agents in parallel
    verify_tasks = []

    if "test" not in skip_agents:
        verify_tasks.append(("test-engineer", run_test_engineer(stage)))
    if "codex" not in skip_agents:
        verify_tasks.append(("codex-review", run_codex_review()))
    if "runtime" not in skip_agents and run_runtime_evaluator and contract:
        verify_tasks.append(("runtime-verifier", run_runtime_evaluator(contract)))

    test_result: dict = {"passed": 0, "failed": 0, "output": ""}
    codex_result: dict = {"status": "skipped", "has_issues": False, "output": ""}
    runtime_result: dict | None = None

    if verify_tasks:
        results = await asyncio.gather(
            *[t[1] for t in verify_tasks],
            return_exceptions=True,
        )
        for i, (agent_name, _) in enumerate(verify_tasks):
            r = results[i]
            if isinstance(r, Exception):
                r = {"status": "error", "error": str(r)}
            if agent_name == "test-engineer":
                test_result = r
            elif agent_name == "codex-review":
                codex_result = r
            elif agent_name == "runtime-verifier":
                runtime_result = r

    completed_steps.append("verify")

    # --- Step 4: Fix Loop ---
    failures = _collect_failures(test_result, codex_result, runtime_result)
    error_failures = [f for f in failures if f["severity"] == "error"]

    fix_attempts = 0
    prev_fingerprint = None

    while error_failures and fix_attempts < max_fix_attempts:
        if bus.is_cancelled:
            return JobResult(
                status="INTERRUPTED",
                contract=contract,
                test_result=test_result,
                codex_result=codex_result,
                runtime_result=runtime_result,
                fix_attempts=fix_attempts,
                interrupted_at="fix_loop",
                completed_steps=completed_steps,
            )

        fix_attempts += 1
        fp = _failure_fingerprint(error_failures)
        if fp == prev_fingerprint:
            # Same failures — structural issue, stop trying
            break
        prev_fingerprint = fp

        await bus.emit(FixLoopStarted(
            attempt=fix_attempts,
            max_attempts=max_fix_attempts,
            failures=[f["description"][:100] for f in error_failures],
        ))

        # Fix attempt
        fix_prompt = (
            f"{task_context}\n\n"
            f"## Fix Required\nThe following test failures need to be fixed:\n\n"
            + "\n".join(f"- {f['description']}" for f in error_failures)
        )
        try:
            await query("implementer", fix_prompt, model="sonnet")
        except Exception:
            break  # Fix attempt failed, stop trying

        # Re-verify (only tests, not codex/runtime)
        if "test" not in skip_agents:
            test_result = await run_test_engineer(stage)

        failures = _collect_failures(test_result, codex_result, runtime_result)
        error_failures = [f for f in failures if f["severity"] == "error"]

    if not error_failures:
        if fix_attempts > 0:
            await bus.emit(FixLoopResolved(attempt=fix_attempts))
        completed_steps.append("fix_loop")
        return JobResult(
            status="PASS",
            contract=contract,
            test_result=test_result,
            codex_result=codex_result,
            runtime_result=runtime_result,
            fix_attempts=fix_attempts,
            completed_steps=completed_steps,
        )

    if fix_attempts >= max_fix_attempts:
        await bus.emit(FixLoopExhausted(
            attempt=fix_attempts,
            remaining_failures=[f["description"][:100] for f in error_failures],
        ))

    return JobResult(
        status="BLOCKED",
        contract=contract,
        test_result=test_result,
        codex_result=codex_result,
        runtime_result=runtime_result,
        fix_attempts=fix_attempts,
        unresolved=[f["description"] for f in error_failures],
        completed_steps=completed_steps,
    )
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `cd /Users/minghaojiang/Developer/donace && python -m pytest tests/test_job_runner.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add sdk/job_runner.py tests/test_job_runner.py
git commit -m "feat: add single job runner extracted from sprint loop"
```

---

### Task 5: CLI Subcommand Infrastructure

**Files:**
- Create: `sdk/commands.py`
- Modify: `sdk/orchestrator.py` (main() function, lines 875-906)

- [ ] **Step 1: Create commands.py with subcommand stubs**

```python
"""Subcommand implementations for hybrid orchestration.

Each function is a complete CLI command that orchestrator.py dispatches to.
All commands share: --run-id, --cwd, --dashboard-url.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from sdk.events import (
    EventBus,
    JobCompleted,
    JobRegistered,
    JobStarted,
    RunCompleted,
    RunFailed,
    RunStarted,
    Stage,
)
from sdk.emitter import WebSocketEmitter


async def _setup_bus(run_id: str, dashboard_url: str | None, job_id: str = "", interactive: bool = False) -> tuple[EventBus, WebSocketEmitter | None]:
    """Create EventBus and optionally connect to dashboard."""
    bus = EventBus(run_id=run_id, interactive=interactive)
    emitter = None
    if dashboard_url:
        emitter = WebSocketEmitter(dashboard_url, bus, job_id=job_id)
        await emitter.connect()
        if emitter.is_connected:
            bus.subscribe(emitter)
    return bus, emitter


async def _teardown(emitter: WebSocketEmitter | None) -> None:
    """Disconnect from dashboard."""
    if emitter:
        await emitter.disconnect()


def _write_job_result(cwd: str, run_id: str, job_id: str, result: dict) -> Path:
    """Write job result JSON to .ai/runs/{run_id}/jobs/{job_id}.json."""
    jobs_dir = Path(cwd) / ".ai" / "runs" / run_id / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / f"{job_id}.json"
    path.write_text(json.dumps(result, indent=2))
    return path


def _register_job(cwd: str, run_id: str, job_id: str) -> Path:
    """Create job lock file for registration."""
    lock_dir = Path(cwd) / ".ai" / "runs" / run_id / "jobs"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{job_id}.lock"
    lock_path.write_text(json.dumps({"pid": os.getpid(), "job_id": job_id, "run_id": run_id}))
    return lock_path


def _unregister_job(lock_path: Path) -> None:
    """Remove job lock file."""
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# run_start
# ---------------------------------------------------------------------------

async def cmd_run_start(run_id: str, cwd: str, dashboard_url: str | None) -> dict:
    """Start a new run. Creates run directory and emits run.started."""
    run_dir = Path(cwd) / ".ai" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "jobs").mkdir(exist_ok=True)
    (run_dir / "context").mkdir(exist_ok=True)

    bus, emitter = await _setup_bus(run_id, dashboard_url)
    try:
        await bus.emit(RunStarted(task="", cwd=cwd, interactive=False))
        return {"status": "started", "run_id": run_id, "run_dir": str(run_dir)}
    finally:
        await _teardown(emitter)


# ---------------------------------------------------------------------------
# run_complete
# ---------------------------------------------------------------------------

async def cmd_run_complete(run_id: str, cwd: str, dashboard_url: str | None) -> dict:
    """Complete a run. Aggregates job results, runs validator, emits run.completed."""
    run_dir = Path(cwd) / ".ai" / "runs" / run_id
    jobs_dir = run_dir / "jobs"

    # Aggregate job results
    job_results = []
    if jobs_dir.exists():
        for f in sorted(jobs_dir.glob("*.json")):
            try:
                job_results.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                continue

    # Summary
    passed = sum(1 for j in job_results if j.get("status") == "PASS")
    blocked = sum(1 for j in job_results if j.get("status") == "BLOCKED")
    interrupted = sum(1 for j in job_results if j.get("status") == "INTERRUPTED")
    total = len(job_results)

    summary = {
        "passed": passed,
        "blocked": blocked,
        "interrupted": interrupted,
        "total": total,
        "overall": "PASS" if blocked == 0 and interrupted == 0 and total > 0 else "INCOMPLETE",
    }

    result = {
        "run_id": run_id,
        "jobs": job_results,
        "summary": summary,
    }

    # Write aggregated result
    result_path = run_dir / "result.json"
    result_path.write_text(json.dumps(result, indent=2))

    bus, emitter = await _setup_bus(run_id, dashboard_url)
    try:
        await bus.emit(RunCompleted(result_summary=json.dumps(summary)))
        return result
    finally:
        await _teardown(emitter)


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

async def cmd_plan(
    task: str,
    cwd: str,
    run_id: str,
    dashboard_url: str | None,
    skip_planner: bool = False,
    skip_codex: bool = False,
) -> dict:
    """Run planning pipeline: planner → architect → codex plan review.

    Returns plan JSON with stages, files, dependencies, estimates.
    """
    job_id = f"job-plan-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id)
    lock_path = _register_job(cwd, run_id, job_id)

    try:
        await bus.emit(JobRegistered(job_id=job_id, command="plan", pid=os.getpid()))
        await bus.emit(JobStarted(job_id=job_id, command="plan"))

        # Import here to avoid circular imports
        from sdk.agent_dispatch import AgentDispatcher
        from sdk.orchestrator import run_planner, run_architect, run_codex_plan_review, _parse_plan_stages

        dispatcher = AgentDispatcher(
            agents_dir=str(Path(__file__).parent.parent / "agents"),
            cwd=cwd,
            bus=bus,
        )

        # Step 1: Planner (optional)
        spec = task
        if not skip_planner:
            try:
                spec = await run_planner(task, bus, dispatcher)
            except Exception:
                spec = task  # Fall back to raw task

        # Step 2: Architect
        plan = await run_architect(spec, bus, dispatcher)

        # Step 3: Codex plan review (optional)
        codex_review = {"has_major_issues": False}
        if not skip_codex and plan.raw:
            try:
                codex_review = await run_codex_plan_review(plan, bus, dispatcher)
            except Exception:
                pass

        # Step 4: Generate JSON sidecar
        plan_json = {
            "task": task,
            "stages": [
                {
                    "id": f"stage-{i+1}",
                    "name": s.name,
                    "files": [],  # Architect doesn't always provide files; team-lead can supplement
                    "dependencies": s.depends_on,
                    "has_user_facing_changes": s.has_user_facing_changes,
                    "estimated_turns": s.estimated_turns,
                }
                for i, s in enumerate(plan.stages)
            ],
            "codex_review": codex_review,
        }

        # Write JSON sidecar
        plans_dir = Path(cwd) / ".ai" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        json_path = plans_dir / "current-plan.json"
        json_path.write_text(json.dumps(plan_json, indent=2))

        await bus.emit(JobCompleted(job_id=job_id, command="plan", status="PASS", result_summary=f"{len(plan.stages)} stages"))

        # Write job result
        job_result = {"command": "plan", "status": "PASS", "plan": plan_json}
        _write_job_result(cwd, run_id, job_id, job_result)

        # Also write to stdout for team-lead
        print(json.dumps(plan_json, indent=2))
        return plan_json

    except Exception as exc:
        await bus.emit(JobCompleted(job_id=job_id, command="plan", status="ERROR", result_summary=str(exc)))
        raise
    finally:
        _unregister_job(lock_path)
        await _teardown(emitter)


# ---------------------------------------------------------------------------
# run_job
# ---------------------------------------------------------------------------

async def cmd_run_job(
    stage_id: str,
    plan_path: str,
    cwd: str,
    run_id: str,
    dashboard_url: str | None,
    skip_agents: set[str] | None = None,
    max_fix_attempts: int = 3,
) -> dict:
    """Execute a single stage: contract → implement → verify → fix loop."""
    job_id = f"job-{stage_id}-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id)
    lock_path = _register_job(cwd, run_id, job_id)
    skip = skip_agents or set()

    try:
        await bus.emit(JobRegistered(job_id=job_id, command="run_job", stage_id=stage_id, pid=os.getpid()))
        await bus.emit(JobStarted(job_id=job_id, command="run_job"))

        # Load plan and find stage
        plan_data = json.loads(Path(plan_path).read_text())
        stage_def = None
        for s in plan_data.get("stages", []):
            if s["id"] == stage_id:
                stage_def = s
                break
        if not stage_def:
            raise ValueError(f"Stage {stage_id} not found in plan")

        stage = Stage(
            name=stage_def["name"],
            has_user_facing_changes=stage_def.get("has_user_facing_changes", False),
            depends_on=stage_def.get("dependencies", []),
            estimated_turns=stage_def.get("estimated_turns", 0),
        )

        # Load shared context
        context_dir = Path(cwd) / ".ai" / "runs" / run_id / "context"
        task_context = ""
        if context_dir.exists():
            for ctx_file in sorted(context_dir.glob("*.md")):
                task_context += ctx_file.read_text() + "\n\n"

        # Setup dispatcher
        from sdk.agent_dispatch import AgentDispatcher
        dispatcher = AgentDispatcher(
            agents_dir=str(Path(__file__).parent.parent / "agents"),
            cwd=cwd,
            bus=bus,
        )

        # Run job
        from sdk.job_runner import run_job
        result = await run_job(
            stage=stage,
            cwd=cwd,
            bus=bus,
            query=dispatcher.query,
            run_test_engineer=dispatcher.run_test_engineer,
            run_codex_review=dispatcher.run_codex_review,
            run_runtime_evaluator=dispatcher.run_runtime_evaluator if "runtime" not in skip else None,
            task_context=task_context,
            skip_agents=skip,
            max_fix_attempts=max_fix_attempts,
        )

        # Emit completion
        status = result.status
        if status == "INTERRUPTED":
            from sdk.events import JobInterrupted
            await bus.emit(JobInterrupted(
                job_id=job_id,
                command="run_job",
                reason=bus.cancel_reason,
                completed_steps=result.completed_steps,
                interrupted_at=result.interrupted_at,
            ))
        else:
            await bus.emit(JobCompleted(job_id=job_id, command="run_job", status=status, result_summary=f"{stage.name}: {status}"))

        # Write job result
        job_result = {"command": "run_job", "stage_id": stage_id, **result.to_dict()}
        _write_job_result(cwd, run_id, job_id, job_result)

        # Write context for subsequent jobs
        if result.status in ("PASS", "BLOCKED"):
            context_dir.mkdir(parents=True, exist_ok=True)
            ctx_path = context_dir / f"job-{stage_id}.md"
            ctx_content = f"## {stage.name}\n\nStatus: {result.status}\n"
            if result.test_result:
                ctx_content += f"Tests: {result.test_result.get('passed', 0)} passed, {result.test_result.get('failed', 0)} failed\n"
            ctx_path.write_text(ctx_content)

        # Output for team-lead
        print(json.dumps(job_result, indent=2))
        return job_result

    except Exception as exc:
        await bus.emit(JobCompleted(job_id=job_id, command="run_job", status="ERROR", result_summary=str(exc)))
        error_result = {"command": "run_job", "stage_id": stage_id, "status": "ERROR", "error": str(exc)}
        _write_job_result(cwd, run_id, job_id, error_result)
        print(json.dumps(error_result, indent=2), file=sys.stderr)
        return error_result
    finally:
        _unregister_job(lock_path)
        await _teardown(emitter)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

async def cmd_verify(
    cwd: str,
    run_id: str,
    dashboard_url: str | None,
    agents: set[str] | None = None,
    scope: str = "",
) -> dict:
    """Run verification on existing code. Read-only — does not modify code."""
    job_id = f"job-verify-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id)
    lock_path = _register_job(cwd, run_id, job_id)
    active_agents = agents or {"test", "codex"}

    try:
        await bus.emit(JobRegistered(job_id=job_id, command="verify", pid=os.getpid()))
        await bus.emit(JobStarted(job_id=job_id, command="verify"))

        from sdk.agent_dispatch import AgentDispatcher
        dispatcher = AgentDispatcher(
            agents_dir=str(Path(__file__).parent.parent / "agents"),
            cwd=cwd,
            bus=bus,
        )

        results = {}
        tasks = []
        task_names = []

        # A dummy stage for test-engineer API compat
        dummy_stage = Stage(name="Full verification", has_user_facing_changes=True)

        if "test" in active_agents:
            tasks.append(dispatcher.run_test_engineer(dummy_stage))
            task_names.append("test")
        if "codex" in active_agents:
            tasks.append(dispatcher.run_codex_review())
            task_names.append("codex")
        if "runtime" in active_agents:
            tasks.append(dispatcher.run_runtime_evaluator("Verify all acceptance criteria"))
            task_names.append("runtime")

        if tasks:
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)
            for name, r in zip(task_names, raw_results):
                if isinstance(r, Exception):
                    results[name] = {"status": "error", "error": str(r)}
                else:
                    results[name] = r

        # Determine overall status
        has_errors = False
        if "test" in results and results["test"].get("failed", 0) > 0:
            has_errors = True
        if "runtime" in results and results["runtime"].get("status") == "FAIL":
            has_errors = True

        status = "FAIL" if has_errors else "PASS"

        await bus.emit(JobCompleted(job_id=job_id, command="verify", status=status, result_summary=f"verify: {status}"))

        job_result = {"command": "verify", "status": status, "results": results}
        _write_job_result(cwd, run_id, job_id, job_result)
        print(json.dumps(job_result, indent=2))
        return job_result

    except Exception as exc:
        await bus.emit(JobCompleted(job_id=job_id, command="verify", status="ERROR", result_summary=str(exc)))
        raise
    finally:
        _unregister_job(lock_path)
        await _teardown(emitter)


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

async def cmd_review(
    cwd: str,
    run_id: str,
    dashboard_url: str | None,
    reviewer: str = "typescript",
) -> dict:
    """Run code review. Dispatches reviewer agent."""
    job_id = f"job-review-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id)
    lock_path = _register_job(cwd, run_id, job_id)

    try:
        await bus.emit(JobRegistered(job_id=job_id, command="review", pid=os.getpid()))
        await bus.emit(JobStarted(job_id=job_id, command="review"))

        from sdk.agent_dispatch import AgentDispatcher
        dispatcher = AgentDispatcher(
            agents_dir=str(Path(__file__).parent.parent / "agents"),
            cwd=cwd,
            bus=bus,
        )

        agent_name = f"{reviewer}-reviewer"
        review_prompt = (
            f"Review the codebase for bugs, security vulnerabilities, "
            f"and code quality issues. Focus on recent changes."
        )
        result_text = await dispatcher.query(agent_name, review_prompt, model="opus")

        await bus.emit(JobCompleted(job_id=job_id, command="review", status="PASS", result_summary=result_text[:200]))

        job_result = {"command": "review", "status": "PASS", "reviewer": reviewer, "findings": result_text}
        _write_job_result(cwd, run_id, job_id, job_result)
        print(json.dumps(job_result, indent=2))
        return job_result

    except Exception as exc:
        await bus.emit(JobCompleted(job_id=job_id, command="review", status="ERROR", result_summary=str(exc)))
        raise
    finally:
        _unregister_job(lock_path)
        await _teardown(emitter)


# ---------------------------------------------------------------------------
# document
# ---------------------------------------------------------------------------

async def cmd_document(
    cwd: str,
    run_id: str,
    dashboard_url: str | None,
) -> dict:
    """Update documentation. Dispatches documenter agent."""
    job_id = f"job-document-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id)
    lock_path = _register_job(cwd, run_id, job_id)

    try:
        await bus.emit(JobRegistered(job_id=job_id, command="document", pid=os.getpid()))
        await bus.emit(JobStarted(job_id=job_id, command="document"))

        from sdk.agent_dispatch import AgentDispatcher
        dispatcher = AgentDispatcher(
            agents_dir=str(Path(__file__).parent.parent / "agents"),
            cwd=cwd,
            bus=bus,
        )

        # Load context for documenter
        context = ""
        context_dir = Path(cwd) / ".ai" / "runs" / run_id / "context"
        if context_dir.exists():
            for ctx_file in sorted(context_dir.glob("*.md")):
                context += ctx_file.read_text() + "\n\n"

        doc_prompt = (
            f"{context}\n\n"
            f"Update project documentation: README.md, CLAUDE.md, CHANGELOG.md, "
            f"and any knowledge cards in .ai/cards/."
        )
        result_text = await dispatcher.query("documenter", doc_prompt, model="sonnet")

        await bus.emit(JobCompleted(job_id=job_id, command="document", status="PASS", result_summary=result_text[:200]))

        job_result = {"command": "document", "status": "PASS", "output": result_text[:500]}
        _write_job_result(cwd, run_id, job_id, job_result)
        print(json.dumps(job_result, indent=2))
        return job_result

    except Exception as exc:
        await bus.emit(JobCompleted(job_id=job_id, command="document", status="ERROR", result_summary=str(exc)))
        raise
    finally:
        _unregister_job(lock_path)
        await _teardown(emitter)
```

- [ ] **Step 2: Verify import works**

Run: `cd /Users/minghaojiang/Developer/donace && python -c "from sdk.commands import cmd_run_start, cmd_run_complete, cmd_plan, cmd_run_job, cmd_verify, cmd_review, cmd_document; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add sdk/commands.py
git commit -m "feat: add subcommand implementations for hybrid orchestration"
```

---

### Task 6: CLI Subparser Entry Point

**Files:**
- Modify: `sdk/orchestrator.py` (replace `main()` at lines 875-906)

- [ ] **Step 1: Add subparser-based main() alongside existing main()**

Replace the existing `main()` in `sdk/orchestrator.py` with a new version that supports both the old `--task` interface and new subcommands:

```python
def main() -> None:
    """CLI entry point. Supports both legacy --task and new subcommands."""
    import argparse

    parser = argparse.ArgumentParser(description="donace orchestrator")
    subparsers = parser.add_subparsers(dest="command")

    # --- Legacy: --task (backwards compat) ---
    parser.add_argument("--task", type=str, help="(Legacy) Task description — runs full pipeline")
    parser.add_argument("--cwd", type=str, default=os.getcwd())
    parser.add_argument("--dashboard-url", type=str, default=None)
    parser.add_argument("--no-interactive", action="store_true")

    # --- run_start ---
    p_start = subparsers.add_parser("run_start", help="Start a new run")
    p_start.add_argument("--run-id", required=True)
    p_start.add_argument("--cwd", type=str, default=os.getcwd())
    p_start.add_argument("--dashboard-url", type=str, default=None)

    # --- run_complete ---
    p_complete = subparsers.add_parser("run_complete", help="Complete a run")
    p_complete.add_argument("--run-id", required=True)
    p_complete.add_argument("--cwd", type=str, default=os.getcwd())
    p_complete.add_argument("--dashboard-url", type=str, default=None)

    # --- plan ---
    p_plan = subparsers.add_parser("plan", help="Run planning pipeline")
    p_plan.add_argument("--task", required=True)
    p_plan.add_argument("--cwd", type=str, default=os.getcwd())
    p_plan.add_argument("--run-id", required=True)
    p_plan.add_argument("--dashboard-url", type=str, default=None)
    p_plan.add_argument("--skip-planner", action="store_true")
    p_plan.add_argument("--skip-codex", action="store_true")

    # --- run_job ---
    p_job = subparsers.add_parser("run_job", help="Execute a single stage")
    p_job.add_argument("--stage-id", required=True)
    p_job.add_argument("--plan", required=True, dest="plan_path")
    p_job.add_argument("--cwd", type=str, default=os.getcwd())
    p_job.add_argument("--run-id", required=True)
    p_job.add_argument("--dashboard-url", type=str, default=None)
    p_job.add_argument("--skip-agents", type=str, default="", help="Comma-separated: contract,test,codex,runtime")
    p_job.add_argument("--max-fix-attempts", type=int, default=3)

    # --- verify ---
    p_verify = subparsers.add_parser("verify", help="Run verification (read-only)")
    p_verify.add_argument("--cwd", type=str, default=os.getcwd())
    p_verify.add_argument("--run-id", required=True)
    p_verify.add_argument("--dashboard-url", type=str, default=None)
    p_verify.add_argument("--agents", type=str, default="test,codex", help="Comma-separated: test,codex,runtime")
    p_verify.add_argument("--scope", type=str, default="")

    # --- review ---
    p_review = subparsers.add_parser("review", help="Run code review")
    p_review.add_argument("--cwd", type=str, default=os.getcwd())
    p_review.add_argument("--run-id", required=True)
    p_review.add_argument("--dashboard-url", type=str, default=None)
    p_review.add_argument("--reviewer", type=str, default="typescript")

    # --- document ---
    p_doc = subparsers.add_parser("document", help="Update documentation")
    p_doc.add_argument("--cwd", type=str, default=os.getcwd())
    p_doc.add_argument("--run-id", required=True)
    p_doc.add_argument("--dashboard-url", type=str, default=None)

    args = parser.parse_args()

    # Legacy mode: --task without subcommand
    if args.command is None and args.task:
        interactive = not args.no_interactive
        result = asyncio.run(run(
            task=args.task,
            cwd=args.cwd,
            dashboard_url=args.dashboard_url,
            interactive=interactive,
        ))
        output = result.to_json_output()
        run_id = output.get("run_id", "unknown")
        result_path = Path(args.cwd) / ".ai" / "runs" / f"{run_id}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(output, indent=2))
        print(json.dumps(output, indent=2))
        return

    # Subcommand dispatch
    from sdk.commands import (
        cmd_run_start, cmd_run_complete, cmd_plan, cmd_run_job,
        cmd_verify, cmd_review, cmd_document,
    )

    if args.command == "run_start":
        asyncio.run(cmd_run_start(args.run_id, args.cwd, args.dashboard_url))

    elif args.command == "run_complete":
        asyncio.run(cmd_run_complete(args.run_id, args.cwd, args.dashboard_url))

    elif args.command == "plan":
        asyncio.run(cmd_plan(
            task=args.task, cwd=args.cwd, run_id=args.run_id,
            dashboard_url=args.dashboard_url,
            skip_planner=args.skip_planner, skip_codex=args.skip_codex,
        ))

    elif args.command == "run_job":
        skip = set(args.skip_agents.split(",")) if args.skip_agents else set()
        asyncio.run(cmd_run_job(
            stage_id=args.stage_id, plan_path=args.plan_path,
            cwd=args.cwd, run_id=args.run_id,
            dashboard_url=args.dashboard_url,
            skip_agents=skip, max_fix_attempts=args.max_fix_attempts,
        ))

    elif args.command == "verify":
        agents = set(args.agents.split(",")) if args.agents else {"test", "codex"}
        asyncio.run(cmd_verify(
            cwd=args.cwd, run_id=args.run_id,
            dashboard_url=args.dashboard_url,
            agents=agents, scope=args.scope,
        ))

    elif args.command == "review":
        asyncio.run(cmd_review(
            cwd=args.cwd, run_id=args.run_id,
            dashboard_url=args.dashboard_url,
            reviewer=args.reviewer,
        ))

    elif args.command == "document":
        asyncio.run(cmd_document(
            cwd=args.cwd, run_id=args.run_id,
            dashboard_url=args.dashboard_url,
        ))

    else:
        parser.print_help()
        sys.exit(1)
```

- [ ] **Step 2: Verify legacy mode still works**

Run: `cd /Users/minghaojiang/Developer/donace && python -m sdk.orchestrator --help`
Expected: Shows both legacy `--task` args and subcommands.

Run: `cd /Users/minghaojiang/Developer/donace && python -m sdk.orchestrator run_start --help`
Expected: Shows `--run-id`, `--cwd`, `--dashboard-url` args.

- [ ] **Step 3: Commit**

```bash
git add sdk/orchestrator.py
git commit -m "feat: add subcommand CLI for hybrid orchestration, keep --task compat"
```

---

### Task 7: Dashboard Job Registry and Interrupt API

**Files:**
- Modify: `sdk/dashboard.py`

- [ ] **Step 1: Add job registry and new endpoints**

In `sdk/dashboard.py`, add to the `_create_app()` function:

```python
# Job registry: job_id -> metadata (populated from job.registered events)
job_registry: dict[str, dict] = {}

# Track which control WebSocket belongs to which job
job_control_ws: dict[str, WebSocket] = {}
```

Add a new `/api/control` handler that accepts `job_id` query param:

```python
@_app.websocket("/api/control")
async def control(ws: WebSocket) -> None:
    """Orchestrator listens here for user decisions relayed from browser."""
    await ws.accept()
    # Extract job_id from query params
    job_id = ws.query_params.get("job_id", "")
    if job_id:
        job_control_ws[job_id] = ws
    orchestrator_control_ws.add(ws)
    try:
        while True:
            try:
                await ws.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        orchestrator_control_ws.discard(ws)
        if job_id:
            job_control_ws.pop(job_id, None)
```

Update the ingest handler to populate job registry from `job.registered` events:

```python
# Inside the ingest handler, after store.append(event):
if event.get("type") == "job.registered":
    job_registry[event.get("job_id", "")] = {
        "job_id": event.get("job_id"),
        "command": event.get("command"),
        "stage_id": event.get("stage_id", ""),
        "pid": event.get("pid"),
        "started_at": event.get("timestamp"),
    }
elif event.get("type") in ("job.completed", "job.interrupted"):
    job_registry.pop(event.get("job_id", ""), None)
```

Add new REST endpoints:

```python
@_app.get("/api/jobs/active")
async def list_active_jobs() -> list[dict[str, Any]]:
    """List currently running jobs."""
    return list(job_registry.values())

@_app.post("/api/interrupt")
async def interrupt_job(request_data: dict) -> dict[str, Any]:
    """Send interrupt signal to a specific job."""
    job_id = request_data.get("job_id", "")
    reason = request_data.get("reason", "user requested")

    if job_id not in job_registry:
        return {"error": "job_not_found", "job_id": job_id}

    # Route to specific job's control WebSocket
    ws = job_control_ws.get(job_id)
    if not ws:
        # Fallback: broadcast to all orchestrator connections
        msg = json.dumps({"action": "interrupt", "job_id": job_id, "reason": reason})
        for orch_ws in orchestrator_control_ws.copy():
            try:
                await orch_ws.send_text(msg)
            except Exception:
                pass
        return {"status": "broadcast", "job_id": job_id}

    try:
        msg = json.dumps({"action": "interrupt", "job_id": job_id, "reason": reason})
        await ws.send_text(msg)
        return {"status": "sent", "job_id": job_id}
    except Exception as exc:
        return {"error": str(exc), "job_id": job_id}
```

Note: The `/api/interrupt` endpoint needs to accept a JSON body. Use FastAPI's Request object:

```python
from fastapi import Request

@_app.post("/api/interrupt")
async def interrupt_job(request: Request) -> dict[str, Any]:
    request_data = await request.json()
    # ... rest as above
```

- [ ] **Step 2: Verify dashboard starts**

Run: `cd /Users/minghaojiang/Developer/donace && python -c "from sdk.dashboard import _create_app; app = _create_app(); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add sdk/dashboard.py
git commit -m "feat: add job registry, /api/jobs/active, and /api/interrupt to dashboard"
```

---

### Task 8: Integration Smoke Test

**Files:**
- Create: `tests/test_commands_integration.py`

- [ ] **Step 1: Write integration test for run_start → run_complete lifecycle**

```python
# tests/test_commands_integration.py
"""Integration tests for subcommand lifecycle."""
import asyncio
import json
import os
import tempfile
import pytest
from pathlib import Path
from sdk.commands import cmd_run_start, cmd_run_complete


@pytest.mark.asyncio
async def test_run_lifecycle():
    """run_start creates directory, run_complete aggregates results."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_id = "test-run-001"

        # Start run
        result = await cmd_run_start(run_id, tmpdir, dashboard_url=None)
        assert result["status"] == "started"

        run_dir = Path(tmpdir) / ".ai" / "runs" / run_id
        assert run_dir.exists()
        assert (run_dir / "jobs").exists()
        assert (run_dir / "context").exists()

        # Simulate a job result
        jobs_dir = run_dir / "jobs"
        job_result = {"command": "run_job", "stage_id": "stage-1", "status": "PASS"}
        (jobs_dir / "job-stage-1.json").write_text(json.dumps(job_result))

        # Complete run
        result = await cmd_run_complete(run_id, tmpdir, dashboard_url=None)
        assert result["summary"]["passed"] == 1
        assert result["summary"]["total"] == 1
        assert result["summary"]["overall"] == "PASS"

        # Check result file exists
        assert (run_dir / "result.json").exists()


@pytest.mark.asyncio
async def test_run_complete_with_blocked():
    """run_complete reports INCOMPLETE when a job is blocked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_id = "test-run-002"
        await cmd_run_start(run_id, tmpdir, dashboard_url=None)

        jobs_dir = Path(tmpdir) / ".ai" / "runs" / run_id / "jobs"
        (jobs_dir / "job-1.json").write_text(json.dumps({"status": "PASS"}))
        (jobs_dir / "job-2.json").write_text(json.dumps({"status": "BLOCKED"}))

        result = await cmd_run_complete(run_id, tmpdir, dashboard_url=None)
        assert result["summary"]["passed"] == 1
        assert result["summary"]["blocked"] == 1
        assert result["summary"]["overall"] == "INCOMPLETE"
```

- [ ] **Step 2: Run tests**

Run: `cd /Users/minghaojiang/Developer/donace && python -m pytest tests/test_commands_integration.py -v`
Expected: All tests PASS.

- [ ] **Step 3: Verify CLI help shows all subcommands**

Run: `cd /Users/minghaojiang/Developer/donace && python -m sdk.orchestrator --help`
Expected: Shows `run_start`, `run_complete`, `plan`, `run_job`, `verify`, `review`, `document` subcommands.

Run: `cd /Users/minghaojiang/Developer/donace && python -m sdk.orchestrator run_job --help`
Expected: Shows `--stage-id`, `--plan`, `--cwd`, `--run-id`, `--skip-agents`, `--max-fix-attempts`.

- [ ] **Step 4: Commit**

```bash
git add tests/test_commands_integration.py
git commit -m "test: add integration tests for subcommand lifecycle"
```

---

## Summary

| Task | What it does | Key files |
|------|-------------|-----------|
| 1 | EventBus cancellation flag | `sdk/events.py` |
| 2 | Job-level events | `sdk/events.py` |
| 3 | Emitter interrupt support | `sdk/emitter.py` |
| 4 | Single job runner | `sdk/job_runner.py` (new) |
| 5 | Subcommand implementations | `sdk/commands.py` (new) |
| 6 | CLI subparser entry point | `sdk/orchestrator.py` |
| 7 | Dashboard job registry + interrupt | `sdk/dashboard.py` |
| 8 | Integration smoke test | `tests/test_commands_integration.py` |

After all tasks: the orchestrator supports both `--task` (legacy full pipeline) and new subcommands (`run_start`, `plan`, `run_job`, `verify`, `review`, `document`, `run_complete`). Phase 2 (concurrency + plan schema) and Phase 3 (team-lead rewrite) build on this foundation.
