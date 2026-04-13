"""Subcommand implementations for hybrid orchestration.

Each async function is a complete CLI command that orchestrator.py dispatches to.
All commands share: --run-id, --cwd, --dashboard-url.

Usage: python3 -m sdk.orchestrator <command> [args]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from sdk.events import (
    EventBus,
    JobCompleted,
    JobInterrupted,
    JobRegistered,
    JobStarted,
    RunCompleted,
    RunStarted,
    Stage,
)
from sdk.emitter import WebSocketEmitter


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _setup_bus(
    run_id: str,
    dashboard_url: str | None,
    job_id: str = "",
    interactive: bool = False,
) -> tuple[EventBus, WebSocketEmitter | None]:
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
    if emitter:
        await emitter.disconnect()


def _write_job_result(cwd: str, run_id: str, job_id: str, result: dict) -> Path:
    """Persist job result to .ai/runs/{run_id}/jobs/{job_id}.json."""
    jobs_dir = Path(cwd) / ".ai" / "runs" / run_id / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / f"{job_id}.json"
    path.write_text(json.dumps(result, indent=2))
    return path


def _register_job(cwd: str, run_id: str, job_id: str) -> Path:
    """Create job lock file for dashboard registry."""
    lock_dir = Path(cwd) / ".ai" / "runs" / run_id / "jobs"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{job_id}.lock"
    lock_path.write_text(json.dumps({
        "pid": os.getpid(), "job_id": job_id, "run_id": run_id,
    }))
    return lock_path


def _unregister_job(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _agents_dir() -> str:
    return str(Path(__file__).parent.parent / "agents")


# ---------------------------------------------------------------------------
# run_start
# ---------------------------------------------------------------------------

async def cmd_run_start(run_id: str, cwd: str, dashboard_url: str | None) -> dict:
    """Start a new run. Creates directory structure, emits run.started."""
    run_dir = Path(cwd) / ".ai" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "jobs").mkdir(exist_ok=True)
    (run_dir / "context").mkdir(exist_ok=True)

    bus, emitter = await _setup_bus(run_id, dashboard_url)
    try:
        await bus.emit(RunStarted(task="", cwd=cwd, interactive=False))
        result = {"status": "started", "run_id": run_id, "run_dir": str(run_dir)}
        print(json.dumps(result, indent=2))
        return result
    finally:
        await _teardown(emitter)


# ---------------------------------------------------------------------------
# run_complete
# ---------------------------------------------------------------------------

async def cmd_run_complete(run_id: str, cwd: str, dashboard_url: str | None) -> dict:
    """Complete a run. Aggregates job results, emits run.completed."""
    run_dir = Path(cwd) / ".ai" / "runs" / run_id
    jobs_dir = run_dir / "jobs"

    # Aggregate job results (skip .lock files)
    job_results = []
    if jobs_dir.exists():
        for f in sorted(jobs_dir.glob("*.json")):
            try:
                job_results.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                continue

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

    result = {"run_id": run_id, "jobs": job_results, "summary": summary}

    # Persist
    result_path = run_dir / "result.json"
    result_path.write_text(json.dumps(result, indent=2))

    bus, emitter = await _setup_bus(run_id, dashboard_url)
    try:
        await bus.emit(RunCompleted(result_summary=json.dumps(summary)))
        print(json.dumps(result, indent=2))
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
    """Planning pipeline: planner -> architect -> codex plan review.

    Writes Markdown plan + JSON sidecar. Returns plan JSON.
    """
    job_id = f"job-plan-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id)
    lock_path = _register_job(cwd, run_id, job_id)

    try:
        await bus.emit(JobRegistered(job_id=job_id, command="plan", pid=os.getpid()))
        await bus.emit(JobStarted(job_id=job_id, command="plan"))

        from sdk.agent_dispatch import AgentDispatcher
        from sdk.orchestrator import run_planner, run_architect, run_codex_plan_review

        dispatcher = AgentDispatcher(agents_dir=_agents_dir(), cwd=cwd, bus=bus)

        # 1. Planner (optional)
        spec = task
        if not skip_planner:
            try:
                spec = await run_planner(task, bus, dispatcher)
            except Exception:
                spec = task

        # 2. Architect
        plan = await run_architect(spec, bus, dispatcher)

        # 3. Codex plan review (optional)
        codex_review: dict[str, Any] = {"has_major_issues": False}
        if not skip_codex and plan.raw:
            try:
                codex_review = await run_codex_plan_review(plan, bus, dispatcher)
            except Exception:
                pass

        # 4. Generate JSON sidecar
        plan_json = {
            "task": task,
            "stages": [
                {
                    "id": f"stage-{i+1}",
                    "name": s.name,
                    "files": s.files,
                    "dependencies": s.depends_on,
                    "has_user_facing_changes": s.has_user_facing_changes,
                    "estimated_turns": s.estimated_turns,
                }
                for i, s in enumerate(plan.stages)
            ],
            "codex_review": codex_review,
        }

        plans_dir = Path(cwd) / ".ai" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        (plans_dir / "current-plan.json").write_text(json.dumps(plan_json, indent=2))

        await bus.emit(JobCompleted(
            job_id=job_id, command="plan", status="PASS",
            result_summary=f"{len(plan.stages)} stages",
        ))
        _write_job_result(cwd, run_id, job_id, {"command": "plan", "status": "PASS", "plan": plan_json})

        print(json.dumps(plan_json, indent=2))
        return plan_json

    except Exception as exc:
        await bus.emit(JobCompleted(job_id=job_id, command="plan", status="ERROR", result_summary=str(exc)))
        error_result = {"command": "plan", "status": "ERROR", "error": str(exc)}
        _write_job_result(cwd, run_id, job_id, error_result)
        print(json.dumps(error_result, indent=2), file=sys.stderr)
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
    """Execute a single stage: contract -> implement -> verify -> fix loop."""
    job_id = f"job-{stage_id}-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id)
    lock_path = _register_job(cwd, run_id, job_id)
    skip = skip_agents or set()

    try:
        await bus.emit(JobRegistered(
            job_id=job_id, command="run_job", stage_id=stage_id, pid=os.getpid(),
        ))
        await bus.emit(JobStarted(job_id=job_id, command="run_job"))

        # Load plan and find stage
        plan_data = json.loads(Path(plan_path).read_text())
        stage_def = None
        for s in plan_data.get("stages", []):
            if s["id"] == stage_id:
                stage_def = s
                break
        if not stage_def:
            raise ValueError(f"Stage '{stage_id}' not found in plan at {plan_path}")

        stage = Stage(
            name=stage_def["name"],
            has_user_facing_changes=stage_def.get("has_user_facing_changes", False),
            depends_on=stage_def.get("dependencies", []),
            estimated_turns=stage_def.get("estimated_turns", 0),
        )

        # Load accumulated context from prior jobs
        context_dir = Path(cwd) / ".ai" / "runs" / run_id / "context"
        task_context = ""
        if context_dir.exists():
            for ctx_file in sorted(context_dir.glob("*.md")):
                task_context += ctx_file.read_text() + "\n\n"

        # Dispatch
        from sdk.agent_dispatch import AgentDispatcher
        from sdk.job_runner import run_job

        dispatcher = AgentDispatcher(agents_dir=_agents_dir(), cwd=cwd, bus=bus)
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
        if result.status == "INTERRUPTED":
            await bus.emit(JobInterrupted(
                job_id=job_id, command="run_job", reason=bus.cancel_reason,
                completed_steps=result.completed_steps, interrupted_at=result.interrupted_at,
            ))
        else:
            await bus.emit(JobCompleted(
                job_id=job_id, command="run_job", status=result.status,
                result_summary=f"{stage.name}: {result.status}",
            ))

        # Persist result
        job_result = {"command": "run_job", "stage_id": stage_id, **result.to_dict()}
        _write_job_result(cwd, run_id, job_id, job_result)

        # Write context for subsequent jobs
        if result.status in ("PASS", "BLOCKED"):
            context_dir.mkdir(parents=True, exist_ok=True)
            ctx_path = context_dir / f"job-{stage_id}.md"
            ctx_content = f"## {stage.name}\n\nStatus: {result.status}\n"
            if result.test_result:
                p = result.test_result.get("passed", 0)
                f = result.test_result.get("failed", 0)
                ctx_content += f"Tests: {p} passed, {f} failed\n"
            ctx_path.write_text(ctx_content)

        print(json.dumps(job_result, indent=2))
        return job_result

    except Exception as exc:
        await bus.emit(JobCompleted(
            job_id=job_id, command="run_job", status="ERROR", result_summary=str(exc),
        ))
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
    """Run verification on existing code. Read-only."""
    job_id = f"job-verify-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id)
    lock_path = _register_job(cwd, run_id, job_id)
    active_agents = agents or {"test", "codex"}

    try:
        await bus.emit(JobRegistered(job_id=job_id, command="verify", pid=os.getpid()))
        await bus.emit(JobStarted(job_id=job_id, command="verify"))

        from sdk.agent_dispatch import AgentDispatcher
        dispatcher = AgentDispatcher(agents_dir=_agents_dir(), cwd=cwd, bus=bus)

        dummy_stage = Stage(name="Full verification", has_user_facing_changes=True)
        coros = []
        names = []

        if "test" in active_agents:
            coros.append(dispatcher.run_test_engineer(dummy_stage))
            names.append("test")
        if "codex" in active_agents:
            coros.append(dispatcher.run_codex_review())
            names.append("codex")
        if "runtime" in active_agents:
            coros.append(dispatcher.run_runtime_evaluator("Verify all acceptance criteria"))
            names.append("runtime")

        results: dict[str, Any] = {}
        if coros:
            raw = await asyncio.gather(*coros, return_exceptions=True)
            for name, r in zip(names, raw):
                results[name] = {"status": "error", "error": str(r)} if isinstance(r, Exception) else r

        has_errors = (
            results.get("test", {}).get("failed", 0) > 0
            or results.get("runtime", {}).get("status") == "FAIL"
        )
        status = "FAIL" if has_errors else "PASS"

        await bus.emit(JobCompleted(job_id=job_id, command="verify", status=status))

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
        dispatcher = AgentDispatcher(agents_dir=_agents_dir(), cwd=cwd, bus=bus)

        agent_name = f"{reviewer}-reviewer"
        prompt = (
            "Review the codebase for bugs, security vulnerabilities, "
            "and code quality issues. Focus on recent changes."
        )
        result_text = await dispatcher.query(agent_name, prompt, model="opus")

        await bus.emit(JobCompleted(
            job_id=job_id, command="review", status="PASS",
            result_summary=result_text[:200],
        ))

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
        dispatcher = AgentDispatcher(agents_dir=_agents_dir(), cwd=cwd, bus=bus)

        # Load context for documenter
        context = ""
        context_dir = Path(cwd) / ".ai" / "runs" / run_id / "context"
        if context_dir.exists():
            for ctx_file in sorted(context_dir.glob("*.md")):
                context += ctx_file.read_text() + "\n\n"

        prompt = (
            f"{context}\n\n"
            "Update project documentation: README.md, CLAUDE.md, CHANGELOG.md, "
            "and any knowledge cards in .ai/cards/."
        )
        result_text = await dispatcher.query("documenter", prompt, model="sonnet")

        await bus.emit(JobCompleted(
            job_id=job_id, command="document", status="PASS",
            result_summary=result_text[:200],
        ))

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
