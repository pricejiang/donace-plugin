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
    cwd: str = "",
) -> tuple[EventBus, WebSocketEmitter | None]:
    """Create EventBus and optionally connect to dashboard.

    If dashboard_url is None and cwd is provided, auto-discovers the URL
    from .ai/runs/{run_id}/dashboard_url (written by run_start).
    """
    url = _resolve_dashboard_url(cwd, run_id, dashboard_url) if cwd else dashboard_url
    bus = EventBus(run_id=run_id, interactive=interactive)
    emitter = None
    if url:
        emitter = WebSocketEmitter(url, bus, job_id=job_id)
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


def _worktree_snapshot(cwd: str) -> dict[str, tuple]:
    """Snapshot dirty-file state with content identity.

    Returns dict of file_path -> (status, mtime_ns, size). Content identity
    (mtime + size) matters because a file dirty before verify starts can
    be rewritten during verify and keep the same porcelain status letter
    (still "M"), so status-only snapshots miss the mutation.

    `-uall` expands untracked directories to individual files. Without it,
    `git status --porcelain` collapses an untracked dir to a single
    `?? dir/` entry, and mutating files inside the dir leaves that
    entry's directory mtime/size unchanged — a blind spot.

    `-z` uses NUL-terminated records with no C-style quoting. Without it,
    paths containing spaces, quotes, unicode, or other specials come out
    quoted like `"has space.txt"` — my previous code tried to stat that
    literal quoted string and silently recorded (status, 0, 0) for every
    such file, missing mutations entirely.

    Files not in the output are assumed clean and untouched — if verify
    mutates a previously-clean tracked file, it will appear in the
    `after` snapshot as newly dirty.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "-uall", "-z"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        )
        if result.returncode != 0:
            return {}
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}

    snapshot: dict[str, tuple] = {}
    records = result.stdout.split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        # Valid record needs at least "XY path" (≥4 chars: 2 status + space + 1 char path)
        if len(rec) < 4:
            continue
        status = rec[:2]
        path = rec[3:]

        # Rename/copy records in -z format span TWO fields:
        #   "R  new_name\0old_name\0"
        # Consume the old-name auxiliary; we only record the new name.
        if status[0] in ("R", "C"):
            i += 1

        full_path = Path(cwd) / path
        try:
            st = full_path.stat()
            snapshot[path] = (status, st.st_mtime_ns, st.st_size)
        except OSError:
            # File deleted or missing — record status without stat
            snapshot[path] = (status, 0, 0)
    return snapshot


def _diff_snapshots(before: dict[str, tuple], after: dict[str, tuple]) -> list[str]:
    """Return paths whose identity (status/mtime/size) differs between snapshots."""
    changed: list[str] = []
    keys = set(before.keys()) | set(after.keys())
    for k in sorted(keys):
        if before.get(k) != after.get(k):
            changed.append(k)
    return changed


def _register_job(cwd: str, run_id: str, job_id: str, command: str = "") -> Path:
    """Create job lock file for dashboard registry.

    The `command` field is written alongside the pid so wrap-phase
    collision detection can see in-flight jobs (which don't yet have
    a *.json result).
    """
    lock_dir = Path(cwd) / ".ai" / "runs" / run_id / "jobs"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{job_id}.lock"
    lock_path.write_text(json.dumps({
        "pid": os.getpid(),
        "job_id": job_id,
        "run_id": run_id,
        "command": command,
    }))
    return lock_path


def _unregister_job(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _agents_dir() -> str:
    return str(Path(__file__).parent.parent / "agents")


DEFAULT_DASHBOARD_URL = "ws://localhost:8741"


def _resolve_dashboard_url(cwd: str, run_id: str, explicit_url: str | None) -> str | None:
    """Resolve dashboard URL: explicit arg > persisted from run_start > default.

    Falls back to ws://localhost:8741 so events always stream to dashboard
    even if team-lead forgets --dashboard-url or skips run_start.
    """
    if explicit_url:
        return explicit_url
    url_file = Path(cwd) / ".ai" / "runs" / run_id / "dashboard_url"
    if url_file.exists():
        return url_file.read_text().strip()
    return DEFAULT_DASHBOARD_URL


def _load_context(cwd: str, run_id: str, level: str = "full") -> str:
    """Load accumulated SharedContext at different detail levels.

    Levels:
      - "full": All context files — for implementer
      - "changed_files": Only plan + job results (no verbose output) — for test-engineer
      - "summary": One-line status per job — for documenter, reviewer
    """
    context_dir = Path(cwd) / ".ai" / "runs" / run_id / "context"
    if not context_dir.exists():
        return ""

    if level == "full":
        parts = []
        for f in sorted(context_dir.glob("*.md")):
            parts.append(f.read_text())
        return "\n\n".join(parts)

    if level == "changed_files":
        parts = []
        plan_file = context_dir / "plan.md"
        if plan_file.exists():
            parts.append(plan_file.read_text())
        # Include job results but only status + file info, not verbose output
        for f in sorted(context_dir.glob("job-*.md")):
            content = f.read_text()
            # Take only the first few lines (status + test summary)
            lines = content.strip().split("\n")[:5]
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    if level == "summary":
        parts = []
        for f in sorted(context_dir.glob("*.md")):
            content = f.read_text()
            # Take only the header line from each file
            first_line = content.strip().split("\n")[0] if content.strip() else ""
            parts.append(first_line)
        return "\n".join(parts)

    return ""


# ---------------------------------------------------------------------------
# run_start
# ---------------------------------------------------------------------------

async def cmd_run_start(run_id: str, cwd: str, dashboard_url: str | None) -> dict:
    """Start a new run. Creates directory structure, emits run.started.

    Persists dashboard_url and an explicit start-time marker so cleanup
    can distinguish "PNG created during this run" from pre-existing user
    assets. Directory mtime is unreliable — it updates on every child
    file write, so by cleanup time it's ~now, defeating the filter.
    """
    import time as _time

    run_dir = Path(cwd) / ".ai" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "jobs").mkdir(exist_ok=True)
    (run_dir / "context").mkdir(exist_ok=True)

    # Explicit start-time file — read by _cleanup_after_run to filter PNGs
    (run_dir / ".start_time").write_text(str(_time.time()))

    # Persist dashboard URL for auto-discovery by subsequent commands
    if dashboard_url:
        (run_dir / "dashboard_url").write_text(dashboard_url)

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

WRAP_LOCK_WAIT_TIMEOUT_S = 600  # 10 min hard ceiling per wrap job


def _pid_alive(pid: int) -> bool:
    """Is `pid` a live process? Used to detect stale lock files."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = probe only
    except ProcessLookupError:
        return False
    except PermissionError:
        # Another user's process — alive but we can't signal it.
        # Treat as alive (conservative) so we don't falsely clear its lock.
        return True
    except OSError:
        return False
    return True


def _scan_job_state(jobs_dir: Path) -> tuple[set[str], set[str]]:
    """Return (completed_commands, in_flight_commands) from disk state.

    Completed = any *.json result exists for this command (even an old one).
    In-flight = a *.lock with a LIVE pid exists for this command.

    Completed and in_flight are reported independently — a command can be
    both (e.g., team-lead ran review once, then re-ran it; the old JSON
    and a new live lock coexist). Callers must wait for in_flight locks
    regardless of whether completed also contains the command.

    Stale locks (pid dead) are removed so they don't block waiting.
    """
    completed: set[str] = set()
    in_flight: set[str] = set()
    if not jobs_dir.exists():
        return completed, in_flight

    for f in jobs_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if cmd := data.get("command"):
                completed.add(cmd)
        except (json.JSONDecodeError, OSError):
            continue

    for lock in jobs_dir.glob("*.lock"):
        try:
            data = json.loads(lock.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        cmd = data.get("command")
        pid = data.get("pid")
        if not cmd:
            continue
        if _pid_alive(pid):
            in_flight.add(cmd)
        else:
            # Stale lock — pid is dead. Remove so future scans don't see it.
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass

    return completed, in_flight


async def _wait_for_wrap_locks(
    jobs_dir: Path, wrap_commands: set[str], timeout_s: int,
) -> set[str]:
    """Poll until the given wrap commands finish (lock → json) or timeout.

    Returns the set of commands that were still in-flight when we gave up.
    """
    import time as _time
    deadline = _time.time() + timeout_s
    last_remaining = set(wrap_commands)

    while _time.time() < deadline:
        _completed, in_flight = _scan_job_state(jobs_dir)
        remaining = wrap_commands & in_flight
        if not remaining:
            return set()
        if remaining != last_remaining:
            print(
                f"Waiting for wrap jobs to finish: {sorted(remaining)}",
                file=sys.stderr,
            )
            last_remaining = remaining
        await asyncio.sleep(2)

    # Timeout — return whatever is still in-flight
    _completed, in_flight = _scan_job_state(jobs_dir)
    return wrap_commands & in_flight


async def _ensure_wrap_jobs(
    run_id: str, cwd: str, dashboard_url: str | None, jobs_dir: Path,
) -> None:
    """Ensure review + document jobs have a recorded outcome before run_complete.

    Three possible states per wrap command:
      - already has a *.json result → leave it alone
      - has an in-flight *.lock → wait for it to finish (up to 10 min)
      - neither → dispatch inline

    Timed-out wrap locks get a crash marker so the aggregate summary
    sees the failure instead of silently reporting PASS.
    """
    completed, in_flight = _scan_job_state(jobs_dir)
    wrap_cmds = {"review", "document"}

    # Step 1: wait for any wrap jobs team-lead already started
    waiting_on = wrap_cmds & in_flight
    if waiting_on:
        still_running = await _wait_for_wrap_locks(
            jobs_dir, waiting_on, WRAP_LOCK_WAIT_TIMEOUT_S,
        )
        for cmd in still_running:
            print(
                f"Warning: wrap job '{cmd}' did not finish in {WRAP_LOCK_WAIT_TIMEOUT_S}s — marking as ERROR",
                file=sys.stderr,
            )
            _write_wrap_failure_marker(
                cwd, run_id, cmd,
                f"wrap job still in-flight after {WRAP_LOCK_WAIT_TIMEOUT_S}s timeout",
            )
        # Re-scan after waiting
        completed, in_flight = _scan_job_state(jobs_dir)

    # Step 2: dispatch anything still missing (team-lead never started it)
    if "review" not in completed and "review" not in in_flight:
        try:
            from sdk.orchestrator import detect_stack
            stack = detect_stack(cwd)
            reviewer = "typescript" if stack in ("typescript", "both") else "ios" if stack == "ios" else None
            if reviewer:
                await cmd_review(cwd=cwd, run_id=run_id, dashboard_url=dashboard_url, reviewer=reviewer)
        except Exception as exc:
            print(f"Warning: wrap-phase review failed: {exc}", file=sys.stderr)
            # Defense-in-depth: cmd_review normally persists its own error
            # result, but if the crash happened before that code ran (e.g.,
            # _setup_bus or _register_job), write a marker here so the
            # aggregate summary still reports the wrap failure.
            _write_wrap_failure_marker(cwd, run_id, "review", str(exc))

    if "document" not in completed and "document" not in in_flight:
        try:
            await cmd_document(cwd=cwd, run_id=run_id, dashboard_url=dashboard_url)
        except Exception as exc:
            print(f"Warning: wrap-phase document failed: {exc}", file=sys.stderr)
            _write_wrap_failure_marker(cwd, run_id, "document", str(exc))


def _write_wrap_failure_marker(cwd: str, run_id: str, command: str, error: str) -> None:
    """Persist a stand-in ERROR job result when a wrap-phase cmd crashes
    or a wrap lock times out.

    Dedup only against EXISTING ERROR records for the same command — not
    against PASS records. A prior PASS followed by a rerun that times out
    is two separate events; suppressing the ERROR would leave the stale
    PASS as the only aggregate signal for this command.
    """
    jobs_dir = Path(cwd) / ".ai" / "runs" / run_id / "jobs"
    for f in jobs_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if data.get("command") == command and data.get("status") == "ERROR":
                return  # ERROR already recorded for this command; don't duplicate
        except (json.JSONDecodeError, OSError):
            continue

    marker_id = f"job-{command}-wrap-crash-{uuid.uuid4().hex[:8]}"
    _write_job_result(cwd, run_id, marker_id, {
        "command": command,
        "status": "ERROR",
        "error": f"wrap-phase crash before job start: {error}",
    })


async def cmd_run_complete(run_id: str, cwd: str, dashboard_url: str | None) -> dict:
    """Complete a run. Runs missing wrap-phase jobs, aggregates results, emits run.completed."""
    run_dir = Path(cwd) / ".ai" / "runs" / run_id
    jobs_dir = run_dir / "jobs"

    # Run missing wrap-phase jobs (review, document) if team-lead skipped them
    await _ensure_wrap_jobs(run_id, cwd, dashboard_url, jobs_dir)

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
        "failed": sum(1 for j in job_results if j.get("status") in ("FAIL", "ERROR")),
        "overall": "PASS" if blocked == 0 and interrupted == 0 and total > 0 and all(j.get("status") not in ("FAIL", "ERROR") for j in job_results) else "INCOMPLETE",
    }

    result = {"run_id": run_id, "jobs": job_results, "summary": summary}

    # Persist
    result_path = run_dir / "result.json"
    result_path.write_text(json.dumps(result, indent=2))

    # Update routing hints
    try:
        from sdk.run_validator import update_routing_hints, RunReport
        from sdk.orchestrator import detect_stack
        stack = detect_stack(cwd)
        codex_had_issues = any(
            j.get("codex_result", {}).get("has_issues") for j in job_results
        )
        runtime_had_issues = any(
            j.get("runtime_result", {}).get("status") == "FAIL" for j in job_results
        )
        total_fix_loops = sum(j.get("fix_attempts", 0) for j in job_results)
        update_routing_hints(
            report=RunReport(),  # Minimal — hints only need aggregate stats
            stack=stack,
            codex_had_issues=codex_had_issues,
            runtime_had_issues=runtime_had_issues,
            fix_loops_used=total_fix_loops,
        )
    except Exception:
        pass  # Non-critical — don't fail run_complete for hints

    # Cleanup: screenshots and playwright cache (plan files stay in run dir)
    _cleanup_after_run(cwd, run_id)

    bus, emitter = await _setup_bus(run_id, dashboard_url, cwd=cwd)
    try:
        await bus.emit(RunCompleted(result_summary=json.dumps(summary)))
        print(json.dumps(result, indent=2))
        return result
    finally:
        await _teardown(emitter)


def _cleanup_after_run(cwd: str, run_id: str) -> None:
    """Clean up artifacts after a completed run.

    Only cleans up targets known to be produced by this run:
    - .playwright-mcp/ directory (Playwright MCP plugin's screenshot cache)
    - Untracked *.png files at project root created AFTER this run started
      (we compare mtime against the run directory's creation time — files
      predating the run are user assets, not ours to delete)

    Plan files are NOT deleted here — they live under .ai/runs/{run_id}/plan.md
    and are part of that run's directory. The run directory is the unit of
    lifecycle, not the plan file.
    """
    import subprocess

    project = Path(cwd)

    # 1. Delete .playwright-mcp/ screenshots (plugin-managed, always safe to clean)
    playwright_dir = project / ".playwright-mcp"
    if playwright_dir.is_dir():
        for f in playwright_dir.glob("*.png"):
            try:
                f.unlink()
            except OSError:
                pass

    # 2. Delete untracked *.png at project root, but only if created during this run.
    # Use the explicit .start_time marker written by run_start — directory
    # mtime changes on every child write, so by cleanup time it's ≈now
    # and would incorrectly exclude screenshots created early in the run.
    run_dir = project / ".ai" / "runs" / run_id
    start_file = run_dir / ".start_time"
    try:
        run_start_ts = float(start_file.read_text().strip())
    except (OSError, ValueError):
        # No start marker — skip PNG cleanup entirely to avoid false positives
        return

    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "*.png"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            f = project / line.strip()
            if not f.exists() or f.parent != project:
                continue
            try:
                if f.stat().st_mtime < run_start_ts:
                    continue  # pre-dates this run, leave it alone
                f.unlink()
            except OSError:
                pass
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

async def cmd_plan(
    cwd: str,
    run_id: str,
    dashboard_url: str | None,
    skip_codex: bool = False,
) -> dict:
    """Validate an existing plan.md, optionally run codex plan review, write plan.json sidecar.

    Team-lead is responsible for writing `.ai/runs/<run_id>/plan.md`
    before calling this — either by hand, or by dispatching a subagent
    that uses the superpowers:writing-plans skill. This command does no
    LLM planning of its own — it's pure parse + validate + optional
    codex review. Cost: ~3K tokens (codex) vs ~30K previously.
    """
    job_id = f"job-plan-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id, cwd=cwd)
    lock_path = _register_job(cwd, run_id, job_id, command="plan")

    try:
        await bus.emit(JobRegistered(job_id=job_id, command="plan", pid=os.getpid()))
        await bus.emit(JobStarted(job_id=job_id, command="plan"))

        run_dir = Path(cwd) / ".ai" / "runs" / run_id
        plan_file = run_dir / "plan.md"
        if not plan_file.exists():
            raise RuntimeError(
                f"No plan at {plan_file}. Team-lead must write the plan "
                f"before calling `plan`. Use the template in agents/team-lead.md, "
                f"or dispatch a subagent that invokes superpowers:writing-plans."
            )

        # Parse stages from the plan team-lead wrote
        from sdk.orchestrator import _parse_plan_stages
        plan_content = plan_file.read_text("utf-8")
        stages = _parse_plan_stages(plan_content)
        if not stages:
            raise RuntimeError(
                f"No parseable stages found in {plan_file}. Plan must use "
                f"`## Stage N: Name` headers with **Files**, **Dependencies**, "
                f"and **Status** fields. See agents/team-lead.md for the template."
            )

        # Optional codex plan review — the one LLM call in this pipeline
        codex_review: dict[str, Any] = {"status": "skipped", "has_major_issues": False}
        if not skip_codex:
            try:
                from sdk.agent_dispatch import AgentDispatcher
                dispatcher = AgentDispatcher(agents_dir=_agents_dir(), cwd=cwd, bus=bus)
                codex_review = await dispatcher.run_codex_plan_review(plan_content)
            except Exception as exc:
                codex_review = {
                    "status": "error",
                    "has_major_issues": False,
                    "error": str(exc),
                }

        # Generate JSON sidecar
        plan_json = {
            "plan_file": str(plan_file.relative_to(cwd) if plan_file.is_relative_to(cwd) else plan_file),
            "stages": [
                {
                    "id": f"stage-{i+1}",
                    "name": s.name,
                    "files": s.files,
                    "dependencies": s.depends_on,
                    "has_user_facing_changes": s.has_user_facing_changes,
                    "estimated_turns": s.estimated_turns,
                }
                for i, s in enumerate(stages)
            ],
            "codex_review": codex_review,
        }

        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "plan.json").write_text(json.dumps(plan_json, indent=2))

        # Status reflects codex verdict: PASS if clean, REVIEW if issues flagged.
        # Team-lead decides whether to revise or proceed.
        status = "REVIEW" if codex_review.get("has_major_issues") else "PASS"
        await bus.emit(JobCompleted(
            job_id=job_id, command="plan", status=status,
            result_summary=f"{len(stages)} stages, codex: {codex_review.get('status', 'skipped')}",
        ))
        _write_job_result(cwd, run_id, job_id, {"command": "plan", "status": status, "plan": plan_json})

        # Write plan context for subsequent jobs
        context_dir = run_dir / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        plan_ctx = f"# Plan\n\nFrom: {plan_file}\n\n"
        for s in plan_json["stages"]:
            plan_ctx += f"## {s['id']}: {s['name']}\n"
            plan_ctx += f"- Files: {', '.join(s['files']) if s['files'] else 'TBD'}\n"
            plan_ctx += f"- Dependencies: {', '.join(s['dependencies']) if s['dependencies'] else 'None'}\n\n"
        (context_dir / "plan.md").write_text(plan_ctx)

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
    max_fix_attempts: int = 1,
) -> dict:
    """Execute a single stage: implement -> verify -> fix loop."""
    # Guard: reject jobs on already-completed runs
    result_file = Path(cwd) / ".ai" / "runs" / run_id / "result.json"
    if result_file.exists():
        raise RuntimeError(
            f"Run {run_id} is already completed (result.json exists). "
            f"Start a new run with run_start instead."
        )

    job_id = f"job-{stage_id}-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id, cwd=cwd)
    lock_path = _register_job(cwd, run_id, job_id, command="run_job")
    skip = skip_agents or set()

    try:
        await bus.emit(JobRegistered(
            job_id=job_id, command="run_job", stage_id=stage_id, pid=os.getpid(),
        ))
        await bus.emit(JobStarted(job_id=job_id, command="run_job"))

        # Load plan and find stage (resolve relative paths against cwd)
        resolved_plan = Path(plan_path) if Path(plan_path).is_absolute() else Path(cwd) / plan_path
        plan_data = json.loads(resolved_plan.read_text())
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
            files=stage_def.get("files", []),
        )

        # Load accumulated context — full detail for implementer
        task_context = _load_context(cwd, run_id, level="full")

        # Dispatch — file_scope limits Write/Edit to stage files (for parallel safety)
        from sdk.agent_dispatch import AgentDispatcher
        from sdk.job_runner import run_job

        file_scope = stage_def.get("files") or None
        if not file_scope:
            print(f"Warning: stage '{stage_id}' has no files list — file-scope restriction disabled", file=sys.stderr)
        dispatcher = AgentDispatcher(agents_dir=_agents_dir(), cwd=cwd, bus=bus, file_scope=file_scope)
        result = await run_job(
            stage=stage,
            cwd=cwd,
            bus=bus,
            query=dispatcher.query,
            run_test_engineer=dispatcher.run_test_engineer,
            run_codex_review=dispatcher.run_codex_review,
            run_runtime_verifier=dispatcher.run_runtime_verifier if "runtime" not in skip else None,
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
            context_dir = Path(cwd) / ".ai" / "runs" / run_id / "context"
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
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id, cwd=cwd)
    lock_path = _register_job(cwd, run_id, job_id, command="verify")
    active_agents = agents or {"test", "codex"}

    try:
        await bus.emit(JobRegistered(job_id=job_id, command="verify", pid=os.getpid()))
        await bus.emit(JobStarted(job_id=job_id, command="verify"))

        # Snapshot of the working tree BEFORE verify runs — used for a
        # post-run dirty check. A mutated tree means verify leaked writes
        # despite readonly mode.
        pre_snapshot = _worktree_snapshot(cwd)

        from sdk.agent_dispatch import AgentDispatcher
        # file_scope=[] means "no files in scope" → Bash hook blocks all
        # writes (redirection, sed -i, cp, tee, mass mutators, /tmp writes).
        # This is the enforcement layer. The dirty check below is the
        # safety net for writes that slip past the pattern detector.
        dispatcher = AgentDispatcher(
            agents_dir=_agents_dir(), cwd=cwd, bus=bus, file_scope=[],
        )

        dummy_stage = Stage(name="Full verification", has_user_facing_changes=True)
        coros = []
        names = []

        if "test" in active_agents:
            # verify is documented as read-only — prevent the test-engineer
            # from writing new test files during a check.
            coros.append(dispatcher.run_test_engineer(dummy_stage, readonly=True))
            names.append("test")
        if "codex" in active_agents:
            coros.append(dispatcher.run_codex_review())
            names.append("codex")
        if "runtime" in active_agents:
            coros.append(dispatcher.run_runtime_verifier("Full verification", ""))
            names.append("runtime")

        results: dict[str, Any] = {}
        if coros:
            raw = await asyncio.gather(*coros, return_exceptions=True)
            for name, r in zip(names, raw):
                results[name] = {"status": "error", "error": str(r)} if isinstance(r, Exception) else r

        # Post-run dirty-worktree check. If verify mutated any files,
        # something slipped past the Bash hook. Fail the job and report
        # the leaked files — don't let a "read-only" command silently
        # change the working tree.
        post_snapshot = _worktree_snapshot(cwd)
        leaked = _diff_snapshots(pre_snapshot, post_snapshot)
        if leaked:
            results["_dirty_check"] = {
                "status": "error",
                "error": f"verify mutated the working tree (files: {leaked[:20]})",
                "files": leaked,
            }

        # Any verifier crash counts as failure — silent errors used to
        # produce PASS, hiding the fact that verification never ran.
        has_errors = (
            results.get("test", {}).get("failed", 0) > 0
            or results.get("runtime", {}).get("status") == "FAIL"
            or any(r.get("status") == "error" for r in results.values())
        )
        status = "FAIL" if has_errors else "PASS"

        await bus.emit(JobCompleted(job_id=job_id, command="verify", status=status))

        job_result = {"command": "verify", "status": status, "results": results}
        _write_job_result(cwd, run_id, job_id, job_result)

        # Write verify context
        context_dir = Path(cwd) / ".ai" / "runs" / run_id / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        verify_ctx = f"## Verification: {status}\n"
        if "test" in results:
            t = results["test"]
            verify_ctx += f"- Tests: {t.get('passed', 0)} passed, {t.get('failed', 0)} failed\n"
        if "codex" in results:
            verify_ctx += f"- Codex: {'issues found' if results['codex'].get('has_issues') else 'clean'}\n"
        (context_dir / f"verify-{job_id}.md").write_text(verify_ctx)

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
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id, cwd=cwd)
    lock_path = _register_job(cwd, run_id, job_id, command="review")

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
        # Persist an error result so run_complete's aggregate sees the failure.
        # Without this, wrap-phase crashes are invisible in the final summary.
        error_result = {
            "command": "review", "status": "ERROR",
            "reviewer": reviewer, "error": str(exc),
        }
        _write_job_result(cwd, run_id, job_id, error_result)
        print(json.dumps(error_result, indent=2), file=sys.stderr)
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
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id, cwd=cwd)
    lock_path = _register_job(cwd, run_id, job_id, command="document")

    try:
        await bus.emit(JobRegistered(job_id=job_id, command="document", pid=os.getpid()))
        await bus.emit(JobStarted(job_id=job_id, command="document"))

        from sdk.agent_dispatch import AgentDispatcher
        dispatcher = AgentDispatcher(agents_dir=_agents_dir(), cwd=cwd, bus=bus)

        # Summary-level context for documenter (doesn't need verbose details)
        context = _load_context(cwd, run_id, level="summary")

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
        error_result = {"command": "document", "status": "ERROR", "error": str(exc)}
        _write_job_result(cwd, run_id, job_id, error_result)
        print(json.dumps(error_result, indent=2), file=sys.stderr)
        raise
    finally:
        _unregister_job(lock_path)
        await _teardown(emitter)
