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
import time
import uuid
from pathlib import Path
from typing import Any

from sdk.events import (
    EventBus,
    JobCompleted,
    JobInterrupted,
    JobRegistered,
    JobStarted,
    PhaseCompleted,
    PhaseStarted,
    RunCompleted,
    RunStarted,
    RunValidation,
    Stage,
    StageChanged,
    StageCompleted,
    StagesAnnounced,
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


async def _teardown(emitter: WebSocketEmitter | None, bus: EventBus | None = None) -> None:
    if bus:
        await bus.drain()
    if emitter:
        await emitter.disconnect()


def _prior_codex_thread_id(run_dir: Path) -> str | None:
    """Return the codex thread_id from the run's prior plan.json, if any.

    Signals "this is a plan revision, not a fresh plan" so the caller
    can ask codex to resume the thread instead of re-ingesting the full
    plan. Returns None on first plan, corrupt JSON, missing thread_id
    (older companion versions), or any read error — safe to always call.
    """
    plan_json_path = run_dir / "plan.json"
    if not plan_json_path.exists():
        return None
    try:
        data = json.loads(plan_json_path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    review = data.get("codex_review")
    if not isinstance(review, dict):
        return None
    thread_id = review.get("thread_id")
    if isinstance(thread_id, str) and thread_id:
        return thread_id
    return None


_PLAN_REVIEW_IN_PROGRESS_STATES = {"queued", "running"}
_PLAN_REVIEW_PASS_STATES = {"completed", "pass"}


def _codex_plan_review_allows_execute(codex_review: dict[str, Any]) -> bool:
    """Return True only when codex review produced an affirmative terminal gate."""
    status = str(codex_review.get("status", "")).strip().lower()
    if status in _PLAN_REVIEW_IN_PROGRESS_STATES:
        return False
    if codex_review.get("has_major_issues"):
        return False
    if status in _PLAN_REVIEW_PASS_STATES:
        return True
    # `--skip-codex` is the only intentional no-review path. Accidental
    # skipped/error review results must block execute instead of becoming PASS.
    if status == "skipped" and codex_review.get("skip_allowed"):
        return True
    return False


async def _maybe_apply_codex_plan_fix(
    *,
    dispatcher: Any,
    codex_review: dict[str, Any],
    plan_file: Path,
    plan_content: str,
    stages: list[Any],
) -> tuple[dict[str, Any], str, list[Any]]:
    """Run the codex auto-fix when review flags major issues, return updated state.

    Returns (codex_review, plan_content, stages). When no fix is run the
    inputs pass through unchanged. When the fix runs, the review dict gets
    a ``fix`` subfield, and — if the patch landed on disk — plan_content
    and stages are re-read from the post-fix plan.md so plan.json stays in
    sync with the file team-lead is about to hand to implementer.
    """
    if codex_review.get("status") != "completed":
        return codex_review, plan_content, stages
    if not codex_review.get("has_major_issues"):
        return codex_review, plan_content, stages

    findings = codex_review.get("findings") or []
    if not findings:
        # Review flagged issues but didn't itemize them; codex fix has no
        # structured payload to work from, so skip rather than guess.
        return codex_review, plan_content, stages

    try:
        fix_result = await dispatcher.run_codex_plan_fix(
            plan_path=plan_file,
            findings=_coerce_findings_for_fix(findings),
            resume_thread_id=codex_review.get("thread_id"),
        )
    except Exception as exc:
        fix_result = {
            "attempted": True,
            "status": "failed",
            "reason": f"codex plan fix raised: {exc}",
            "diff": "",
            "summary": "",
            "scope_ok": True,
            "touched_other_files": [],
            "thread_id": None,
            "job_id": None,
        }

    updated_review = {**codex_review, "fix": fix_result}

    # If codex actually edited plan.md, re-read + re-parse so plan.json's
    # stages reflect what implementer will receive. If it didn't edit (empty
    # diff), the original content is still on disk and re-parsing is a no-op.
    new_content = plan_content
    new_stages = stages
    if fix_result.get("diff"):
        try:
            from sdk.orchestrator import _parse_plan_stages
            new_content = plan_file.read_text("utf-8")
            new_stages = _parse_plan_stages(new_content) or stages
        except Exception:
            # Re-parse failure shouldn't sink the whole plan command —
            # keep the prior content/stages and let the caller still see
            # the fix payload in codex_review for diagnostics.
            pass

    return updated_review, new_content, new_stages


def _coerce_findings_for_fix(findings: Any) -> list[dict]:
    """Normalize the review's findings list into dicts the fix prompt expects.

    _format_plan_review_findings may have downgraded dicts to strings for
    display; the fix prompt needs the structured fields back.
    """
    out: list[dict] = []
    if not isinstance(findings, list):
        return out
    for item in findings:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            # Best-effort: treat the whole string as the body so codex has
            # some signal even when structure was lost upstream.
            out.append({
                "severity": "info",
                "title": item[:80],
                "body": item,
                "recommendation": "",
            })
    return out


def _plan_status_from_codex_review(codex_review: dict[str, Any]) -> str:
    """Derive the plan job status from the codex review + optional auto-fix.

    Returns:
      - PENDING: review still running (queued / running)
      - REVIEW: review found major issues and either no fix was attempted or
        the fix failed / violated scope / produced no diff — user must
        manually revise
      - AWAIT_APPROVAL: review found issues AND codex auto-fix produced a
        clean in-scope patch — waiting on the main LLM's approval verdict
        (via cmd_approve_plan or cmd_reject_plan). Execute must not start.
      - ERROR: codex review failed, was accidentally skipped, or returned an
        unknown status. This is infrastructure failure, not plan approval.
      - PASS: review had no major issues OR Claude has already approved a
        prior fix
    """
    status = str(codex_review.get("status", "")).strip().lower()
    if status in _PLAN_REVIEW_IN_PROGRESS_STATES:
        return "PENDING"
    if status == "skipped" and codex_review.get("skip_allowed"):
        return "PASS"
    if status not in _PLAN_REVIEW_PASS_STATES:
        return "ERROR"
    if codex_review.get("has_major_issues"):
        fix = codex_review.get("fix") or {}
        if (
            fix.get("attempted")
            and fix.get("status") == "completed"
            and fix.get("scope_ok")
            and fix.get("diff")
            and not fix.get("verdict")
        ):
            return "AWAIT_APPROVAL"
        return "REVIEW"
    return "PASS"


def _write_job_result(cwd: str, run_id: str, job_id: str, result: dict) -> Path:
    """Persist job result to .ai/runs/{run_id}/jobs/{job_id}.json."""
    jobs_dir = Path(cwd) / ".ai" / "runs" / run_id / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / f"{job_id}.json"
    path.write_text(json.dumps(result, indent=2))
    return path


def _supersede_prior_jobs(cwd: str, run_id: str, command: str, current_job_id: str) -> None:
    """Delete prior job-<command>-*.json files from this run.

    Used for idempotent commands (write_plan, plan, review, verify, document)
    where the latest invocation represents the current intent. Without this,
    a PARTIAL/ERROR/REVIEW job from an earlier attempt lingers in jobs/ and
    blocks aggregate PASS after a successful retry.

    Stage jobs are NOT superseded — each stage is unique work.
    """
    jobs_dir = Path(cwd) / ".ai" / "runs" / run_id / "jobs"
    if not jobs_dir.exists():
        return
    for old in jobs_dir.glob(f"job-{command}-*.json"):
        if old.stem != current_job_id:
            try:
                old.unlink()
            except OSError:
                pass


def _has_prior_job_status(cwd: str, run_id: str, command: str, statuses: set[str]) -> bool:
    """Return True if this run already has a result for command in statuses."""
    jobs_dir = Path(cwd) / ".ai" / "runs" / run_id / "jobs"
    if not jobs_dir.exists():
        return False
    for f in jobs_dir.glob(f"job-{command}-*.json"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("command") == command and data.get("status") in statuses:
            return True
    return False


def _write_error_unless_prior_progress(
    cwd: str,
    run_id: str,
    job_id: str,
    command: str,
    result: dict,
) -> Path | None:
    """Persist ERROR unless a prior PASS/PARTIAL already represents progress."""
    if _has_prior_job_status(cwd, run_id, command, {"PASS", "PARTIAL"}):
        return None
    return _write_job_result(cwd, run_id, job_id, result)


IDEMPOTENT_JOB_COMMANDS = {"plan", "write_plan", "verify", "review", "document"}
PROGRESS_JOB_STATUSES = {"PASS", "PARTIAL"}


def _filter_superseded_idempotent_jobs(job_results: list[dict]) -> list[dict]:
    """Drop stale non-progress results for idempotent commands."""
    commands_with_pass = {
        j.get("command")
        for j in job_results
        if j.get("command") in IDEMPOTENT_JOB_COMMANDS
        and j.get("status") == "PASS"
    }
    commands_with_progress = {
        j.get("command")
        for j in job_results
        if j.get("command") in IDEMPOTENT_JOB_COMMANDS
        and j.get("status") in PROGRESS_JOB_STATUSES
    }
    filtered: list[dict] = []
    for job in job_results:
        command = job.get("command")
        status = job.get("status")
        if command in commands_with_pass and status != "PASS":
            continue
        if command in commands_with_progress and status == "ERROR":
            continue
        filtered.append(job)
    return filtered


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


def _git_head(cwd: str) -> str | None:
    """Return current HEAD SHA, or None if not a git repo / no commits."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _git_is_dirty(cwd: str) -> bool:
    """True iff the worktree has uncommitted or untracked changes.

    Returns False if cwd isn't a git repo — there's nothing to be dirty
    about, so callers that gate on dirtiness can safely no-op on non-repo
    cwds.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "-uall"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        )
        if result.returncode != 0:
            return False
        return bool(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _normalize_git_paths(cwd: str, files: list[str]) -> tuple[list[str], list[str]]:
    """Return repo-relative paths safe to hand to git, plus skipped entries."""
    project = Path(cwd).resolve()
    normalized: list[str] = []
    skipped: list[str] = []

    for raw in files:
        path = str(raw).strip()
        if not path:
            continue

        candidate = Path(path)
        if candidate.is_absolute():
            try:
                rel = candidate.resolve().relative_to(project)
                norm = rel.as_posix()
            except ValueError:
                skipped.append(path)
                continue
        else:
            norm = os.path.normpath(path).replace("\\", "/")
            if norm in ("", ".") or norm == ".." or norm.startswith("../"):
                skipped.append(path)
                continue

        if norm not in normalized:
            normalized.append(norm)

    return normalized, skipped


def _parse_porcelain_paths(output: str) -> list[str]:
    """Parse `git status --porcelain -z` output into changed paths."""
    paths: list[str] = []
    records = output.split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if len(rec) < 4:
            continue
        status = rec[:2]
        path = rec[3:]
        if path and path not in paths:
            paths.append(path)
        if status[0] in ("R", "C"):
            i += 1
    return paths


def _git_dirty_paths(cwd: str, files: list[str]) -> list[str]:
    """Return dirty tracked/untracked paths under the provided path list."""
    import subprocess

    paths, _ = _normalize_git_paths(cwd, files)
    if not paths:
        return []

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "-uall", "-z", "--"] + paths,
            capture_output=True, text=True, cwd=cwd, timeout=15,
        )
        if result.returncode != 0:
            return []
        return _parse_porcelain_paths(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _git_changed_roots(cwd: str, files: list[str]) -> list[str]:
    """Return input roots that currently contain a git-visible change."""
    changed: list[str] = []
    for path in files:
        if _git_dirty_paths(cwd, [path]):
            changed.append(path)
    return changed


def _git_commit_stage(
    cwd: str,
    files: list[str],
    message: str,
    expected_head: str | None = None,
    pre_stage_dirty_files: list[str] | None = None,
) -> dict[str, Any]:
    """Commit only the provided file paths and return commit metadata.

    The real index may contain user-staged work, so this builds the commit
    through a temporary index. After moving HEAD, it refreshes the real index
    for just the committed paths so unrelated staged edits are left alone.
    """
    import subprocess
    import tempfile

    paths, skipped_paths = _normalize_git_paths(cwd, files)
    result: dict[str, Any] = {
        "status": "skipped",
        "skipped_paths": skipped_paths,
    }
    if not paths:
        result["reason"] = "stage has no commit-safe file paths"
        return result
    if pre_stage_dirty_files:
        result["reason"] = (
            "stage files were dirty before run_job started; refusing to "
            "bundle pre-existing edits into the stage commit"
        )
        result["pre_stage_dirty_files"] = pre_stage_dirty_files
        return result

    parent = expected_head or _git_head(cwd)
    if not parent:
        result["reason"] = "not a git repository or HEAD is unavailable"
        return result
    current_head = _git_head(cwd)
    if current_head != parent:
        result["reason"] = (
            "HEAD moved while the stage was running; likely parallel run_job "
            "or external git activity"
        )
        result["expected_head"] = parent
        result["actual_head"] = current_head
        return result

    changed_roots = _git_changed_roots(cwd, paths)
    if not changed_roots:
        result["reason"] = "stage files have no git-visible changes"
        return result

    tmp_index = tempfile.NamedTemporaryFile(prefix="orchestrator-index-", delete=False)
    tmp_index.close()
    # GIT_INDEX_FILE should point at a path Git can create. An existing empty
    # file can be treated as a corrupt index by some Git versions.
    Path(tmp_index.name).unlink(missing_ok=True)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = tmp_index.name

    def git(args: list[str], timeout: int = 30):
        return subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            timeout=timeout,
        )

    try:
        read_tree = git(["read-tree", parent], timeout=15)
        if read_tree.returncode != 0:
            result["reason"] = f"failed to initialise temporary index: {read_tree.stderr.strip()}"
            return result

        add = git(["add", "-A", "--"] + changed_roots, timeout=30)
        if add.returncode != 0:
            result["reason"] = f"failed to stage stage files: {add.stderr.strip()}"
            return result

        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--"] + changed_roots,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            timeout=15,
        )
        if diff.returncode == 0:
            result["reason"] = "temporary index has no staged diff for stage files"
            return result
        if diff.returncode != 1:
            result["reason"] = f"failed to inspect staged diff: {diff.stderr.strip()}"
            return result

        tree = git(["write-tree"], timeout=15)
        if tree.returncode != 0:
            result["reason"] = f"failed to write temporary tree: {tree.stderr.strip()}"
            return result
        tree_sha = tree.stdout.strip()

        commit = subprocess.run(
            ["git", "commit-tree", tree_sha, "-p", parent, "-m", message],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=30,
        )
        if commit.returncode != 0:
            result["reason"] = f"failed to create commit: {commit.stderr.strip()}"
            return result
        commit_sha = commit.stdout.strip()

        update = subprocess.run(
            ["git", "update-ref", "-m", message, "HEAD", commit_sha, parent],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=15,
        )
        if update.returncode != 0:
            result["reason"] = f"failed to move HEAD: {update.stderr.strip()}"
            result["commit_sha"] = commit_sha
            return result

        # The temporary-index commit moved HEAD without touching the real
        # index. Refresh only the committed paths; unrelated staged changes
        # stay staged.
        reset = subprocess.run(
            ["git", "reset", "-q", "--"] + changed_roots,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=15,
        )
        if reset.returncode != 0:
            result["index_refresh_warning"] = reset.stderr.strip()

        result.update({
            "status": "committed",
            "commit_sha": commit_sha,
            "files": changed_roots,
        })
        return result
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        result["reason"] = "git subprocess failed while creating stage commit"
        return result
    finally:
        try:
            Path(tmp_index.name).unlink(missing_ok=True)
        except OSError:
            pass


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


def _fetch_events_from_dashboard(cwd: str, run_id: str, timeout_s: float = 2.0) -> list[dict]:
    """Fetch this run's full event list from the dashboard via HTTP GET.

    Returns [] if the dashboard URL isn't persisted, the server is down,
    the request times out, or the response isn't valid JSON. Failure is
    silent on purpose — validation is nice-to-have, and run_complete must
    not fail just because the dashboard isn't reachable.

    The dashboard stores events in SQLite and serves them at
    GET http://<host>:<port>/api/events/{run_id}. Since emitters connect
    via ws://, we convert the scheme before the request.
    """
    import urllib.request
    import urllib.error

    url_file = Path(cwd) / ".ai" / "runs" / run_id / "dashboard_url"
    if not url_file.exists():
        return []
    ws_url = url_file.read_text().strip()
    if not ws_url:
        return []
    http_url = ws_url.replace("ws://", "http://", 1).replace("wss://", "https://", 1)
    endpoint = f"{http_url.rstrip('/')}/api/events/{run_id}"
    try:
        with urllib.request.urlopen(endpoint, timeout=timeout_s) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data if isinstance(data, list) else []
    except (urllib.error.URLError, json.JSONDecodeError, OSError, TimeoutError):
        return []


def _resolve_dashboard_url(cwd: str, run_id: str, explicit_url: str | None) -> str | None:
    """Resolve dashboard URL: explicit arg > persisted from run_start > none.

    If neither is available, return None — NO dashboard connection.
    Previously this fell back to ws://localhost:8741 ("just in case team-lead
    forgot --dashboard-url"), but that caused unit tests and any script
    passing dashboard_url=None on a fresh tempdir to silently connect to
    whatever dashboard happened to be running on localhost:8741 and
    pollute it with throwaway run_ids. Team-lead's documented flow runs
    `run_start --dashboard-url ...` which writes the file for subsequent
    commands to auto-discover; that path still works.
    """
    if explicit_url:
        return explicit_url
    url_file = Path(cwd) / ".ai" / "runs" / run_id / "dashboard_url"
    if url_file.exists():
        return url_file.read_text().strip()
    return None


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

    Before creating the new run dir, opportunistically archives old
    completed runs down to the HOT_RUN_LIMIT cap (see _enforce_run_retention).
    """
    import time as _time

    # Retention: archive older completed runs so .ai/runs/ doesn't grow
    # unbounded. Best-effort — failures don't block run_start.
    try:
        archived = _enforce_run_retention(cwd)
        if archived:
            print(
                f"Archived {len(archived)} older completed run(s) to .ai/archive/",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: run retention failed: {exc}", file=sys.stderr)

    # Pre-flight: warn if the worktree is dirty. Per-stage commits skip
    # stages whose own files were already dirty when run_job started, so
    # users still get a successful run but not a misleading stage commit.
    # Warn-and-continue rather than hard-reject: dirty unrelated files do
    # not prevent temporary-index commits for clean stage scopes.
    if _git_is_dirty(cwd):
        print(
            "Warning: worktree has uncommitted changes. Per-stage commits "
            "will skip any stage whose files were already dirty when that "
            "stage starts. Stash or commit first for cleaner history.",
            file=sys.stderr,
        )

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
        # Dashboard phase bar: boot is whatever run_start does (dir creation,
        # retention, dashboard URL persistence). No long work happens here,
        # so we bracket it with a started/completed pair emitted back-to-back.
        await bus.emit(PhaseStarted(phase="boot"))
        await bus.emit(PhaseCompleted(phase="boot"))
        result = {"status": "started", "run_id": run_id, "run_dir": str(run_dir)}
        print(json.dumps(result, indent=2))
        return result
    finally:
        await _teardown(emitter, bus)


# ---------------------------------------------------------------------------
# list_runs — survey .ai/runs/ (and optionally .ai/archive/) so team-lead
# can detect incomplete runs on session startup and decide whether to
# resume them or start fresh.
# ---------------------------------------------------------------------------

def _format_iso_utc(ts: float) -> str:
    """Unix timestamp → ISO-8601 UTC string (no fractional seconds)."""
    import datetime as _dt
    if not ts:
        return ""
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def cmd_list_runs(
    cwd: str,
    include_archived: bool = False,
    state_filter: str | None = None,
) -> list[dict]:
    """List runs in this project with their state.

    Output format (list of dicts, printed as JSON to stdout):
      {
        "run_id": str,
        "state": "completed" | "in_progress" | "incomplete" | "not_started" | "empty",
        "started_at": str (ISO-8601 UTC, "" if unknown),
        "archived": bool,
        ...state-specific fields...
      }

    `state_filter` may be a single state or a comma-separated list
    (e.g. "incomplete,not_started") — runs matching any listed state
    are kept.

    Sorted by started_at descending (newest first). Dashboard-ordering
    compatible.
    """
    results: list[dict] = []

    runs_root = Path(cwd) / ".ai" / "runs"
    if runs_root.exists():
        for d in runs_root.iterdir():
            if not d.is_dir():
                continue
            info = _classify_run_state(d)
            entry: dict = {
                "run_id": d.name,
                "state": info["state"],
                "started_at": _format_iso_utc(_run_start_time(d)),
                "archived": False,
                "progress": info["progress"],
            }
            if info["jobs_completed"]:
                entry["jobs_completed"] = info["jobs_completed"]  # label → status
            if info["live_locks"]:
                entry["live_locks"] = info["live_locks"]
            if info["stale_locks"]:
                entry["stale_locks"] = info["stale_locks"]
            results.append(entry)

    if include_archived:
        archive_root = Path(cwd) / ".ai" / "archive"
        if archive_root.exists():
            for f in archive_root.glob("*.tar.gz"):
                results.append({
                    "run_id": f.stem.removesuffix(".tar"),
                    "state": "completed",
                    "started_at": _format_iso_utc(f.stat().st_mtime),
                    "archived": True,
                    "archive_path": str(f.relative_to(cwd) if f.is_relative_to(cwd) else f),
                })

    # Filter by state if requested. Accepts comma-separated list so
    # callers can ask for multiple states in one pass, e.g. "/donace:execute"
    # wants both `not_started` (plan done, never ran) and `incomplete`
    # (partially ran, can resume).
    if state_filter:
        allowed = {s.strip() for s in state_filter.split(",") if s.strip()}
        results = [r for r in results if r["state"] in allowed]

    # Sort newest first (empty started_at sorts to the end)
    results.sort(key=lambda r: r.get("started_at") or "", reverse=True)

    print(json.dumps(results, indent=2))
    return results


# ---------------------------------------------------------------------------
# write_plan — dispatch the planner agent to write .ai/runs/<id>/plan.md.
# Team-lead uses this instead of spawning a Claude Code general-purpose
# sub-agent with the writing-plans skill: the dispatched planner goes
# through the SDK, so every tool_use / token / hook_deny event streams
# to the dashboard and feeds into validate_run.
# ---------------------------------------------------------------------------

async def cmd_write_plan(
    run_id: str,
    cwd: str,
    dashboard_url: str | None,
    task: str,
) -> dict:
    """Dispatch the planner agent to produce .ai/runs/<run-id>/plan.md."""
    # job_id command prefix must match the literal "write_plan" command
    # name so _supersede_prior_jobs' glob `job-write_plan-*.json` finds
    # prior attempts. Using a dash here (`job-write-plan-...`) would
    # orphan stale ERROR jobs across retries.
    job_id = f"job-write_plan-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id, cwd=cwd)
    lock_path = _register_job(cwd, run_id, job_id, command="write_plan")

    run_dir = Path(cwd) / ".ai" / "runs" / run_id
    plan_path = run_dir / "plan.md"
    plan_rel = plan_path.relative_to(cwd) if plan_path.is_relative_to(cwd) else plan_path
    had_previous_plan = False
    previous_plan_bytes: bytes | None = None

    def _recover_plan_artifact() -> tuple[str | None, bool]:
        """Restore/remove plan.md after a failed write_plan attempt.

        Returns (note, safe_to_preserve_prior_progress). If recovery fails,
        callers must persist the ERROR job so aggregate state cannot hide a
        corrupted artifact behind an older PASS result.
        """
        try:
            if previous_plan_bytes is not None:
                plan_path.parent.mkdir(parents=True, exist_ok=True)
                plan_path.write_bytes(previous_plan_bytes)
                return "restored_prior_plan", True
            if had_previous_plan:
                return "prior_plan_not_restored_no_snapshot", False
            if plan_path.exists():
                plan_path.unlink()
                return "removed_invalid_plan", True
            return None, True
        except OSError as exc:
            return f"artifact_recovery_failed: {exc}", False

    try:
        await bus.emit(JobRegistered(
            job_id=job_id, command="write_plan", pid=os.getpid(),
        ))
        await bus.emit(JobStarted(job_id=job_id, command="write_plan"))
        # Dashboard phase bar: plan is active until cmd_plan's codex review
        # lands a terminal verdict. Idempotent — a revision loop that re-runs
        # write_plan just keeps phases[plan]='active'.
        await bus.emit(PhaseStarted(phase="plan"))

        # If a prior plan exists (e.g. revision after codex review), hand
        # it to the planner so it can revise rather than rewrite blind.
        existing_plan = ""
        if plan_path.exists():
            had_previous_plan = True
            previous_plan_bytes = plan_path.read_bytes()
            existing_plan = previous_plan_bytes.decode("utf-8", errors="replace")

        prompt_parts: list[str] = [f"Task:\n{task}"]
        prompt_parts.append(
            f"Write the plan to: {plan_path}\n"
            f"(relative to cwd: {plan_rel})"
        )
        if existing_plan:
            prompt_parts.append(
                "A PRIOR plan already exists at that path. The task above is "
                "likely a revision brief (e.g. codex plan review findings). "
                "Read the existing plan, address the feedback, and overwrite "
                "it with the revised version.\n\n"
                "--- existing plan ---\n"
                f"{existing_plan}\n"
                "--- end existing plan ---"
            )
        prompt = "\n\n".join(prompt_parts)

        # file_scope pins planner's Write/Edit to plan.md only. Planner
        # has Read/Grep/Glob/Skill for context; no source-code mutation.
        run_dir.mkdir(parents=True, exist_ok=True)
        from sdk.agent_dispatch import AgentDispatcher
        dispatcher = AgentDispatcher(
            agents_dir=_agents_dir(), cwd=cwd, bus=bus,
            file_scope=[str(plan_rel)],
            run_id=run_id, job_id=job_id,
        )
        await dispatcher.query("planner", prompt, model="opus")

        # Verify the planner actually wrote plan.md.
        # On a revision, the prior file is still on disk (and still above the
        # 100-byte floor), so existence + size can't distinguish "planner
        # rewrote the plan" from "planner hung silently and left the stale
        # file untouched". run-phase5-runE-dbeea30dbe6d rounds 7/8 hit the
        # latter and recycled the stale plan back to codex, producing an
        # infinite revision loop. Byte-compare against the pre-dispatch
        # snapshot — mtime isn't reliable (filesystems differ on identical
        # rewrites) and a no-op touch is just as bad as no touch.
        if not plan_path.exists():
            status = "ERROR"
            error: str | None = (
                f"planner returned but {plan_rel} was not written"
            )
        else:
            wrote_size = plan_path.stat().st_size
            if wrote_size < 100:
                status = "ERROR"
                error = f"{plan_rel} is suspiciously small ({wrote_size} bytes)"
            elif (
                had_previous_plan
                and previous_plan_bytes is not None
                and plan_path.read_bytes() == previous_plan_bytes
            ):
                status = "ERROR"
                error = (
                    f"planner returned but {plan_rel} was not modified "
                    f"(prior bytes unchanged — likely a silent session hang; "
                    f"re-running write_plan will retry with a fresh dispatch)"
                )
            else:
                status = "PASS"
                error = None

        result_payload: dict = {
            "command": "write_plan",
            "status": status,
            "plan_path": str(plan_rel),
        }
        if error:
            result_payload["error"] = error

        recovery_note: str | None = None
        recovery_safe = True
        if status != "PASS":
            recovery_note, recovery_safe = _recover_plan_artifact()
            if recovery_note:
                result_payload["artifact_recovery"] = recovery_note

        await bus.emit(JobCompleted(
            job_id=job_id, command="write_plan", status=status,
            result_summary=error or f"plan written to {plan_rel}",
        ))

        if status == "PASS":
            _supersede_prior_jobs(cwd, run_id, "write_plan", job_id)
            _write_job_result(cwd, run_id, job_id, result_payload)
        else:
            # ERROR path: use the "don't clobber prior progress" writer so
            # a failed revision doesn't erase the prior PASS record.
            if recovery_safe:
                _write_error_unless_prior_progress(
                    cwd, run_id, job_id, "write_plan", result_payload,
                )
            else:
                _write_job_result(cwd, run_id, job_id, result_payload)

        print(json.dumps(result_payload, indent=2))
        return result_payload

    except Exception as exc:
        await bus.emit(JobCompleted(
            job_id=job_id, command="write_plan", status="ERROR",
            result_summary=str(exc),
        ))
        error_result = {
            "command": "write_plan", "status": "ERROR",
            "error": str(exc),
        }
        recovery_note, recovery_safe = _recover_plan_artifact()
        if recovery_note:
            error_result["artifact_recovery"] = recovery_note
        if recovery_safe:
            _write_error_unless_prior_progress(
                cwd, run_id, job_id, "write_plan", error_result,
            )
        else:
            _write_job_result(cwd, run_id, job_id, error_result)
        print(json.dumps(error_result, indent=2), file=sys.stderr)
        raise
    finally:
        _unregister_job(lock_path)
        await _teardown(emitter, bus)


# ---------------------------------------------------------------------------
# mark — lightweight phase-marker for team-lead work that happens outside
# the orchestrator process (e.g. dispatching a Claude Code sub-agent to
# run the writing-plans skill). One emit, no side effects.
# ---------------------------------------------------------------------------

async def cmd_mark(
    run_id: str,
    cwd: str,
    dashboard_url: str | None,
    phase: str,
    status: str,
) -> dict:
    """Emit a PhaseStarted or PhaseCompleted event.

    Used by team-lead to bracket work that happens outside the orchestrator
    (e.g. general-purpose sub-agents writing plan.md via the superpowers
    skill). Without this, the dashboard goes silent during those phases
    even though real work is happening.
    """
    if status not in ("started", "completed"):
        raise ValueError(f"--status must be 'started' or 'completed', got '{status}'")

    bus, emitter = await _setup_bus(run_id, dashboard_url, cwd=cwd)
    try:
        event: PhaseStarted | PhaseCompleted
        if status == "started":
            event = PhaseStarted(phase=phase)
        else:
            event = PhaseCompleted(phase=phase)
        await bus.emit(event)
        result = {
            "status": "ok",
            "run_id": run_id,
            "phase": phase,
            "event_type": event.type,
        }
        print(json.dumps(result, indent=2))
        return result
    finally:
        await _teardown(emitter, bus)


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


# ---------------------------------------------------------------------------
# Run retention: classify each run dir by state and archive old completed ones
# so `.ai/runs/` doesn't grow unbounded and glob scans stay fast.
# ---------------------------------------------------------------------------

HOT_RUN_LIMIT = 20  # how many completed runs stay uncompressed in .ai/runs/
STALE_NOT_STARTED_DAYS = 7  # archive plan-only runs older than this


def _classify_run_state(run_dir: Path) -> dict[str, Any]:
    """Inspect a single run dir and return {state, jobs_completed, progress, ...}.

    States:
      completed    — result.json exists (cmd_run_complete ran)
      in_progress  — at least one *.lock has a live pid
      incomplete   — execute attempted (run_job:* entry in jobs_completed),
                     a stale lock exists, or the plan phase needs attention;
                     team-lead/plan skill may resume or revise
      not_started  — a PASS plan is ready but no run_job has been
                     dispatched; `/donace:execute` is the next step
      empty        — fresh run_start dir with nothing inside yet

    `jobs_completed` is a dict mapping label → status ("PASS", "BLOCKED",
    etc.) so callers can distinguish successful stages from ones that
    need rework. Reading plan.json alone does NOT tell you what finished
    — that lives in jobs/job-*.json.

    `progress` summarises the run at a glance so team-lead's resume
    decision doesn't need to cross-reference files:
      plan_done, stages_total, stages_passed, stages_blocked,
      review_done, document_done, run_completed.
    """
    info: dict[str, Any] = {
        "state": "empty",
        "live_locks": [],
        "stale_locks": [],
        "jobs_completed": {},  # label → status
    }
    result_json_path = run_dir / "result.json"
    result_json_exists = result_json_path.exists()
    if result_json_exists:
        info["state"] = "completed"

    jobs_dir = run_dir / "jobs"
    if jobs_dir.exists():
        job_entries: list[tuple[int, str, dict[str, Any]]] = []
        for jf in jobs_dir.glob("*.json"):
            try:
                data = json.loads(jf.read_text())
                stat = jf.stat()
            except (json.JSONDecodeError, OSError):
                continue
            job_entries.append((stat.st_mtime_ns, jf.name, data))

        # Later retries supersede earlier attempts for resume/progress
        # decisions. Ties fall back to filename for deterministic output.
        for _mtime_ns, _name, data in sorted(job_entries):
            cmd = data.get("command", "") or ""
            stage = data.get("stage_id")
            status = data.get("status", "UNKNOWN") or "UNKNOWN"
            label = f"{cmd}:{stage}" if stage else cmd
            if not label:
                label = _name.removesuffix(".json")
            info["jobs_completed"][label] = status

        for lock in jobs_dir.glob("*.lock"):
            try:
                data = json.loads(lock.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            pid = data.get("pid")
            lock_info = {
                "job_id": data.get("job_id"),
                "command": data.get("command"),
                "pid": pid,
            }
            if _pid_alive(pid):
                info["live_locks"].append(lock_info)
            else:
                info["stale_locks"].append(lock_info)

    plan_json_path = run_dir / "plan.json"
    plan_json_ready = False
    plan_review_in_progress = False
    plan_review_allows_execute = True
    if plan_json_path.exists():
        try:
            plan_data_for_state = json.loads(plan_json_path.read_text())
            codex_review = plan_data_for_state.get("codex_review") or {}
            stages = plan_data_for_state.get("stages") or []
            plan_review_in_progress = (
                codex_review.get("status") in _PLAN_REVIEW_IN_PROGRESS_STATES
            )
            plan_review_allows_execute = _codex_plan_review_allows_execute(codex_review)
            plan_json_ready = (
                bool(stages)
                and plan_review_allows_execute
            )
        except (json.JSONDecodeError, OSError):
            plan_json_ready = False
            plan_review_allows_execute = False

    jobs_completed = info["jobs_completed"]
    plan_status = jobs_completed.get("plan")
    write_plan_status = jobs_completed.get("write_plan")
    has_run_job = any(k.startswith("run_job:") for k in jobs_completed)
    has_plan_material = (
        plan_status is not None
        or write_plan_status is not None
        or (run_dir / "plan.md").exists()
        or plan_json_path.exists()
    )
    plan_ready = (
        (plan_status == "PASS" and plan_review_allows_execute)
        or plan_json_ready
    )
    plan_needs_attention = (
        (plan_status is not None and plan_status != "PASS")
        or (write_plan_status is not None and write_plan_status != "PASS")
        or (has_plan_material and not plan_ready)
    )

    # Override state based on observed live/stale activity.
    #
    # `not_started` means the plan phase PASSed and execute has not been
    # attempted. Plan-only failures and half-finished plan phases are
    # `incomplete` so `/donace:plan` can resume or revise them.
    if info["state"] != "completed":
        if info["live_locks"]:
            info["state"] = "in_progress"
        elif info["stale_locks"] or has_run_job or plan_needs_attention:
            info["state"] = "incomplete"
        elif plan_ready:
            info["state"] = "not_started"

    # Progress: how much of the plan actually happened?
    stages_total = 0
    current_plan_stage_ids: set[str] = set()
    if plan_json_path.exists():
        try:
            plan_data = json.loads(plan_json_path.read_text())
            stages = plan_data.get("stages") or []
            current_plan_stage_ids = {
                str(stage["id"])
                for stage in stages
                if isinstance(stage, dict) and stage.get("id")
            }
            stages_total = len(current_plan_stage_ids)
        except (json.JSONDecodeError, OSError):
            pass

    current_jobs = info["jobs_completed"]
    current_stage_statuses = {
        label.split(":", 1)[1]: status
        for label, status in current_jobs.items()
        if label.startswith("run_job:")
        and label.split(":", 1)[1] in current_plan_stage_ids
    }
    stages_passed = sum(
        1 for status in current_stage_statuses.values() if status == "PASS"
    )
    stages_blocked = sum(
        1
        for status in current_stage_statuses.values()
        if status in ("BLOCKED", "FAIL", "ERROR", "PARTIAL")
    )
    review_done = current_jobs.get("review") == "PASS"
    document_done = current_jobs.get("document") in ("PASS", "PARTIAL")

    info["progress"] = {
        "plan_done": (run_dir / "plan.md").exists() or plan_json_path.exists(),
        "stages_total": stages_total,
        "stages_passed": stages_passed,
        "stages_blocked": stages_blocked,
        "review_done": review_done,
        "document_done": document_done,
        "run_completed": result_json_exists,
    }

    return info


def _run_start_time(run_dir: Path) -> float:
    """Unix timestamp of when this run started. Falls back to dir mtime."""
    start_file = run_dir / ".start_time"
    try:
        return float(start_file.read_text().strip())
    except (OSError, ValueError):
        try:
            return run_dir.stat().st_mtime
        except OSError:
            return 0.0


def _archive_run(run_dir: Path, archive_dir: Path) -> Path:
    """tar.gz a run dir into .ai/archive/<run_id>.tar.gz, then delete original."""
    import shutil
    import tarfile
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{run_dir.name}.tar.gz"
    # Write to a tmp name first so an interrupted archive doesn't leave a
    # half-written .tar.gz sitting next to a deleted source.
    tmp_path = archive_path.with_suffix(".tar.gz.partial")
    try:
        with tarfile.open(tmp_path, "w:gz") as tar:
            tar.add(run_dir, arcname=run_dir.name)
        os.replace(tmp_path, archive_path)  # atomic on same fs
        shutil.rmtree(run_dir)
    except (OSError, tarfile.TarError):
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return archive_path


def _enforce_run_retention(
    cwd: str,
    limit: int = HOT_RUN_LIMIT,
    stale_not_started_days: int = STALE_NOT_STARTED_DAYS,
) -> list[Path]:
    """Archive stale runs so `.ai/runs/` does not grow unbounded.

    Two classes of runs are archived:
      1. `completed` runs beyond `limit` — keep the `limit` newest hot.
      2. `not_started` runs older than `stale_not_started_days` — plan
         was written but never executed; if the user walked away, it
         should not linger forever.

    Never archived: `in_progress` (live process), `incomplete` (mid-
    execute, the user may resume), recent `not_started` (may still be
    headed to /donace:execute), `empty`. Best-effort: individual
    archive failures are swallowed so a flaky file doesn't block run_start.
    """
    runs_root = Path(cwd) / ".ai" / "runs"
    if not runs_root.exists():
        return []
    archive_dir = Path(cwd) / ".ai" / "archive"

    classified: list[tuple[float, Path, dict[str, Any]]] = []
    for d in runs_root.iterdir():
        if not d.is_dir():
            continue
        classified.append((_run_start_time(d), d, _classify_run_state(d)))

    archived: list[Path] = []

    # 1. Completed runs beyond the hot-cache cap
    completed = [(t, d) for t, d, info in classified if info["state"] == "completed"]
    if len(completed) > limit:
        completed.sort(key=lambda x: -x[0])  # newest first
        for _, d in completed[limit:]:
            try:
                archived.append(_archive_run(d, archive_dir))
            except (OSError, Exception):  # noqa: BLE001 — truly best-effort
                pass

    # 2. Stale not_started runs (plan-only, abandoned)
    cutoff = time.time() - (stale_not_started_days * 86400)
    for t, d, info in classified:
        if info["state"] != "not_started":
            continue
        if t <= 0 or t >= cutoff:
            # Unknown age (t==0) or still fresh → leave alone
            continue
        try:
            archived.append(_archive_run(d, archive_dir))
        except (OSError, Exception):  # noqa: BLE001
            pass

    return archived


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

    If the command already has PASS/PARTIAL progress, preserve that
    aggregate state; the failed rerun still appears in live events/stderr.
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
    _write_error_unless_prior_progress(cwd, run_id, marker_id, command, {
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
    job_results = _filter_superseded_idempotent_jobs(job_results)

    passed = sum(1 for j in job_results if j.get("status") == "PASS")
    blocked = sum(1 for j in job_results if j.get("status") == "BLOCKED")
    interrupted = sum(1 for j in job_results if j.get("status") == "INTERRUPTED")
    failed = sum(1 for j in job_results if j.get("status") in ("FAIL", "ERROR"))
    needs_review = sum(1 for j in job_results if j.get("status") == "REVIEW")
    partial = sum(1 for j in job_results if j.get("status") == "PARTIAL")
    total = len(job_results)

    summary = {
        "passed": passed,
        "blocked": blocked,
        "interrupted": interrupted,
        "failed": failed,
        "needs_review": needs_review,
        "partial": partial,
        "total": total,
        # Only PASS when every job is PASS. REVIEW, PARTIAL, BLOCKED,
        # INTERRUPTED, FAIL, ERROR, or any other non-PASS status →
        # INCOMPLETE. Team-lead reads per-job details to decide severity.
        "overall": "PASS" if total > 0 and passed == total else "INCOMPLETE",
    }

    # --- Run validation: reflect on the run so team-lead can improve ---
    from sdk.orchestrator import detect_stack
    stack = detect_stack(cwd)
    validation_dict: dict | None = None
    try:
        from sdk.run_validator import validate_run
        events = _fetch_events_from_dashboard(cwd, run_id)
        report = validate_run(events=events, job_results=job_results, stack=stack)
        validation_dict = report.to_dict()
    except Exception as exc:
        # Non-critical: record the failure but don't block run_complete
        validation_dict = {"status": "error", "error": str(exc)}

    result = {"run_id": run_id, "jobs": job_results, "summary": summary}
    if validation_dict is not None:
        result["validation"] = validation_dict

    # Persist
    result_path = run_dir / "result.json"
    result_path.write_text(json.dumps(result, indent=2))

    # Update routing hints (EMA signals for next run's stack-specific routing)
    try:
        from sdk.run_validator import update_routing_hints
        codex_had_issues = any(
            j.get("codex_result", {}).get("has_issues") for j in job_results
        )
        runtime_had_issues = any(
            j.get("runtime_result", {}).get("status") == "FAIL" for j in job_results
        )
        total_fix_loops = sum(j.get("fix_attempts", 0) for j in job_results)
        update_routing_hints(
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
        # Dashboard phase bar: make sure sprint is closed and wrap is open
        # before the run-completion bundle lands. Idempotent — verify/review/
        # document may have already emitted these.
        await bus.emit(PhaseCompleted(phase="sprint"))
        await bus.emit(PhaseStarted(phase="wrap"))
        if validation_dict is not None and validation_dict.get("status") != "error":
            await bus.emit(RunValidation(validation=validation_dict))
        await bus.emit(RunCompleted(result_summary=json.dumps(summary)))
        # Wrap is done once run.completed is emitted.
        await bus.emit(PhaseCompleted(phase="wrap"))
        print(json.dumps(result, indent=2))
        return result
    finally:
        await _teardown(emitter, bus)


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

        # Optional codex plan review — the one LLM call in this pipeline.
        # Resume the prior codex thread when we can: the rev1→rev2→rev3
        # loop (user iterates on the same plan.md) then stays in one
        # session, so codex references its earlier findings instead of
        # re-deriving everything from a 26KB plan every round.
        prior_thread_id = _prior_codex_thread_id(run_dir)
        codex_review: dict[str, Any] = {"status": "skipped", "has_major_issues": False}
        dispatcher = None  # reused for auto-fix if review surfaces findings
        if not skip_codex:
            try:
                from sdk.agent_dispatch import AgentDispatcher
                dispatcher = AgentDispatcher(agents_dir=_agents_dir(), cwd=cwd, bus=bus)
                codex_review = await dispatcher.run_codex_plan_review(
                    plan_content,
                    resume_thread_id=prior_thread_id,
                )
            except Exception as exc:
                codex_review = {
                    "status": "error",
                    "has_major_issues": False,
                    "error": str(exc),
                }
        else:
            codex_review = {
                "status": "skipped",
                "has_major_issues": False,
                "summary": "",
                "findings": [],
                "next_steps": [],
                "output": "",
                "reason": "codex plan review explicitly skipped by --skip-codex",
                "skip_allowed": True,
            }

        # Auto-fix: if the review settled with findings, dispatch codex once
        # more (via the same thread) to apply a minimal patch to plan.md.
        # Re-parses stages because the fix may have added / renamed stages.
        # See agent_dispatch.run_codex_plan_fix for the scope verification.
        if dispatcher is not None:
            codex_review, plan_content, stages = await _maybe_apply_codex_plan_fix(
                dispatcher=dispatcher,
                codex_review=codex_review,
                plan_file=plan_file,
                plan_content=plan_content,
                stages=stages,
            )

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

        # Dashboard stage row: broadcast the whole plan so every slot shows
        # its real name before any run_job has started. Without this, future
        # slots render as "Stage" until team-lead walks down to them.
        await bus.emit(StagesAnnounced(
            stages=[
                {
                    "index": i,
                    "id": stage_def["id"],
                    "name": stage_def["name"],
                    "estimated_turns": stage_def["estimated_turns"],
                }
                for i, stage_def in enumerate(plan_json["stages"])
            ],
        ))

        # Status reflects codex verdict. A still-running review is not a
        # PASS: plan_status must finalize it before execute can proceed.
        status = _plan_status_from_codex_review(codex_review)
        await bus.emit(JobCompleted(
            job_id=job_id, command="plan", status=status,
            result_summary=f"{len(stages)} stages, codex: {codex_review.get('status', 'skipped')}",
        ))
        # Dashboard phase bar: terminal verdict (PASS/REVIEW) closes the plan
        # phase. PENDING (codex still running) leaves it active — plan_status
        # will emit the completion when codex lands.
        if status != "PENDING":
            await bus.emit(PhaseCompleted(phase="plan"))
        _supersede_prior_jobs(cwd, run_id, "plan", job_id)
        _write_job_result(cwd, run_id, job_id, {"command": "plan", "status": status, "plan": plan_json})

        # Write the full plan.md (verbatim) into context so subsequent
        # implementer + runtime-verifier prompts can read each stage's
        # Success Criteria and Tests — not just its id/files/deps.
        context_dir = run_dir / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "plan.md").write_text(plan_content)

        print(json.dumps(plan_json, indent=2))
        return plan_json

    except Exception as exc:
        await bus.emit(JobCompleted(job_id=job_id, command="plan", status="ERROR", result_summary=str(exc)))
        error_result = {"command": "plan", "status": "ERROR", "error": str(exc)}
        _write_error_unless_prior_progress(cwd, run_id, job_id, "plan", error_result)
        print(json.dumps(error_result, indent=2), file=sys.stderr)
        raise
    finally:
        _unregister_job(lock_path)
        await _teardown(emitter, bus)


# ---------------------------------------------------------------------------
# plan_status — finalize a background codex plan review
# ---------------------------------------------------------------------------
# When cmd_plan's client-side poll caps out (600s) while codex is still
# working, plan.json's codex_review carries {"status": "running", "job_id":
# ..., "thread_id": ...}. team-lead calls this before /donace:execute to
# pull the real findings from codex once they exist.

async def cmd_plan_status(
    cwd: str,
    run_id: str,
    dashboard_url: str | None,
) -> dict:
    """Check + finalize a background codex plan review.

    Behavior:
    - No plan.json, or codex_review already terminal → no-op
    - codex_review.status == "running" → poll codex; if completed, update
      plan.json with findings; if still running, leave as-is

    Returns {"status": "no-op" | "updated" | "still-running" | "error", ...}.
    Prints the result JSON so team-lead can parse stdout.
    """
    run_dir = Path(cwd) / ".ai" / "runs" / run_id
    plan_json_path = run_dir / "plan.json"
    if not plan_json_path.exists():
        result = {"status": "no-op", "reason": "no plan.json"}
        print(json.dumps(result, indent=2))
        return result
    try:
        plan_json = json.loads(plan_json_path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        result = {"status": "error", "reason": f"plan.json unreadable: {exc}"}
        print(json.dumps(result, indent=2))
        return result

    review = plan_json.get("codex_review") or {}
    review_state = review.get("status")
    if review_state != "running":
        result = {"status": "no-op", "reason": f"codex_review.status={review_state}"}
        print(json.dumps(result, indent=2))
        return result

    job_id = review.get("job_id")
    if not job_id:
        result = {"status": "error", "reason": "codex_review.status=running but no job_id"}
        print(json.dumps(result, indent=2))
        return result

    status_job_id = f"job-plan-status-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=status_job_id, cwd=cwd)
    try:
        from sdk.agent_dispatch import AgentDispatcher
        dispatcher = AgentDispatcher(agents_dir=_agents_dir(), cwd=cwd, bus=bus)
        plan_file = run_dir / "plan.md"
        plan_content = plan_file.read_text("utf-8") if plan_file.exists() else ""
        initial_review = (
            review.get("initial_review")
            if isinstance(review.get("initial_review"), dict)
            else None
        )
        updated_review = await dispatcher.fetch_codex_plan_review_result(
            str(job_id),
            plan_text=plan_content or None,
            initial_review=initial_review,
        )

        new_state = updated_review.get("status")
        if new_state == "running":
            result = {
                "status": "still-running",
                "job_id": job_id,
                "thread_id": updated_review.get("thread_id"),
                "reason": updated_review.get("reason", ""),
            }
            print(json.dumps(result, indent=2))
            return result

        # If the background review landed findings, run the auto-fix here
        # too — otherwise plan_status would drop the run into REVIEW and
        # miss the AWAIT_APPROVAL shortcut that the foreground cmd_plan
        # path takes. Re-parse stages if the fix edits plan.md.
        stages_list = plan_json.get("stages") or []
        if plan_content and updated_review.get("has_major_issues"):
            from sdk.orchestrator import _parse_plan_stages
            parsed_stages = _parse_plan_stages(plan_content)
            updated_review, plan_content, parsed_stages = await _maybe_apply_codex_plan_fix(
                dispatcher=dispatcher,
                codex_review=updated_review,
                plan_file=plan_file,
                plan_content=plan_content,
                stages=parsed_stages,
            )
            stages_list = [
                {
                    "id": f"stage-{i+1}",
                    "name": s.name,
                    "files": s.files,
                    "dependencies": s.depends_on,
                    "has_user_facing_changes": s.has_user_facing_changes,
                    "estimated_turns": s.estimated_turns,
                }
                for i, s in enumerate(parsed_stages)
            ]
            if stages_list:
                plan_json["stages"] = stages_list
            # Keep context/plan.md in sync with any post-fix edits.
            (run_dir / "context").mkdir(parents=True, exist_ok=True)
            (run_dir / "context" / "plan.md").write_text(plan_content)

        # Dashboard phase bar: codex landed a terminal verdict — close the
        # plan phase. Emit before teardown so the event reaches the bus.
        await bus.emit(PhaseCompleted(phase="plan"))
    finally:
        await _teardown(emitter, bus)

    plan_json["codex_review"] = updated_review
    plan_json_path.write_text(json.dumps(plan_json, indent=2))
    finalized_plan_status = _plan_status_from_codex_review(updated_review)
    _write_job_result(
        cwd,
        run_id,
        status_job_id,
        {
            "command": "plan",
            "status": finalized_plan_status,
            "plan": plan_json,
        },
    )
    _supersede_prior_jobs(cwd, run_id, "plan", status_job_id)
    result = {
        "status": "updated",
        "codex_status": new_state,
        "has_major_issues": bool(updated_review.get("has_major_issues")),
        "plan_status": finalized_plan_status,
        "job_id": job_id,
        "thread_id": updated_review.get("thread_id"),
    }
    print(json.dumps(result, indent=2))
    return result


# ---------------------------------------------------------------------------
# approve_plan / reject_plan — Claude's verdict on codex's auto-fix
# ---------------------------------------------------------------------------
# Resolution path for the AWAIT_APPROVAL state cmd_plan writes when codex's
# review finds issues and the auto-fix (run_codex_plan_fix) lands a clean
# in-scope patch. The main LLM in the /donace:plan chat evaluates diff vs
# findings and calls one of these to unblock execute.

def _await_approval_fix_payload(plan_json: dict) -> dict | None:
    """Return the fix dict when plan.json is in the AWAIT_APPROVAL shape, else None.

    Centralizes the "is this plan actually waiting on Claude's verdict?"
    precondition used by both approve and reject. Looser than the exact
    status-classifier check because callers care about "can I legitimately
    record a verdict", not "which bucket does the classifier pick".
    """
    review = plan_json.get("codex_review") or {}
    if not review.get("has_major_issues"):
        return None
    fix = review.get("fix") or {}
    if not fix.get("attempted"):
        return None
    if fix.get("status") != "completed":
        return None
    if not fix.get("scope_ok"):
        return None
    if not fix.get("diff"):
        return None
    if fix.get("verdict"):
        return None
    return fix


async def cmd_approve_plan(
    cwd: str,
    run_id: str,
    dashboard_url: str | None,
    note: str | None = None,
) -> dict:
    """Mark the codex auto-fix as approved — plan moves from AWAIT_APPROVAL to PASS.

    Called by the main LLM (from /donace:plan) when codex's fix diff
    genuinely addresses every finding without scope drift. Writes:
    - codex_review.has_major_issues = False (the review is now resolved)
    - codex_review.fix.verdict = "approved"
    - codex_review.fix.note = <Claude's reasoning, optional>

    Plus a new plan job file with status=PASS so /donace:execute's
    classifier sees a green plan phase.
    """
    run_dir = Path(cwd) / ".ai" / "runs" / run_id
    plan_json_path = run_dir / "plan.json"
    if not plan_json_path.exists():
        result = {"status": "error", "reason": "no plan.json"}
        print(json.dumps(result, indent=2))
        return result
    try:
        plan_json = json.loads(plan_json_path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        result = {"status": "error", "reason": f"plan.json unreadable: {exc}"}
        print(json.dumps(result, indent=2))
        return result

    fix = _await_approval_fix_payload(plan_json)
    if fix is None:
        result = {
            "status": "error",
            "reason": "plan is not in AWAIT_APPROVAL state (no clean fix to approve)",
        }
        print(json.dumps(result, indent=2))
        return result

    job_id = f"job-plan-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id, cwd=cwd)
    try:
        await bus.emit(JobRegistered(job_id=job_id, command="plan", pid=os.getpid()))
        await bus.emit(JobStarted(job_id=job_id, command="plan"))

        review = plan_json.setdefault("codex_review", {})
        fix_payload = review.setdefault("fix", {})
        fix_payload["verdict"] = "approved"
        if note:
            fix_payload["note"] = note
        review["has_major_issues"] = False
        plan_json_path.write_text(json.dumps(plan_json, indent=2))

        await bus.emit(JobCompleted(
            job_id=job_id, command="plan", status="PASS",
            result_summary="Claude approved codex fix",
        ))
        await bus.emit(PhaseCompleted(phase="plan"))
        _supersede_prior_jobs(cwd, run_id, "plan", job_id)
        _write_job_result(cwd, run_id, job_id, {
            "command": "plan",
            "status": "PASS",
            "plan": plan_json,
            "approval": {
                "verdict": "approved",
                "note": note or "",
            },
        })

        result = {
            "status": "approved",
            "plan_status": "PASS",
            "job_id": job_id,
            "note": note or "",
        }
        print(json.dumps(result, indent=2))
        return result
    finally:
        await _teardown(emitter, bus)


async def cmd_reject_plan(
    cwd: str,
    run_id: str,
    dashboard_url: str | None,
    reason: str,
) -> dict:
    """Mark the codex auto-fix as rejected — plan falls back to REVIEW.

    Called by the main LLM when it reads codex's diff and decides the fix
    missed a finding, drifted scope, or introduced a new risk. Writes:
    - codex_review.fix.verdict = "rejected"
    - codex_review.fix.reason = <why Claude rejected>
    - has_major_issues stays True — user must manually revise plan.md

    The reason is mandatory: Claude must tell the user what it saw that
    prompted the rejection, otherwise the "pause for user" loop lacks the
    one piece of information the user actually needs.
    """
    reason = (reason or "").strip()
    if not reason:
        result = {"status": "error", "reason": "reject_plan requires a non-empty --reason"}
        print(json.dumps(result, indent=2))
        return result

    run_dir = Path(cwd) / ".ai" / "runs" / run_id
    plan_json_path = run_dir / "plan.json"
    if not plan_json_path.exists():
        result = {"status": "error", "reason": "no plan.json"}
        print(json.dumps(result, indent=2))
        return result
    try:
        plan_json = json.loads(plan_json_path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        result = {"status": "error", "reason": f"plan.json unreadable: {exc}"}
        print(json.dumps(result, indent=2))
        return result

    fix = _await_approval_fix_payload(plan_json)
    if fix is None:
        result = {
            "status": "error",
            "reason": "plan is not in AWAIT_APPROVAL state (no fix to reject)",
        }
        print(json.dumps(result, indent=2))
        return result

    job_id = f"job-plan-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id, cwd=cwd)
    try:
        await bus.emit(JobRegistered(job_id=job_id, command="plan", pid=os.getpid()))
        await bus.emit(JobStarted(job_id=job_id, command="plan"))

        review = plan_json.setdefault("codex_review", {})
        fix_payload = review.setdefault("fix", {})
        fix_payload["verdict"] = "rejected"
        fix_payload["reason"] = reason
        # has_major_issues stays True — the revision loop is still live.
        plan_json_path.write_text(json.dumps(plan_json, indent=2))

        await bus.emit(JobCompleted(
            job_id=job_id, command="plan", status="REVIEW",
            result_summary=f"Claude rejected codex fix: {reason[:80]}",
        ))
        await bus.emit(PhaseCompleted(phase="plan"))
        _supersede_prior_jobs(cwd, run_id, "plan", job_id)
        _write_job_result(cwd, run_id, job_id, {
            "command": "plan",
            "status": "REVIEW",
            "plan": plan_json,
            "approval": {
                "verdict": "rejected",
                "reason": reason,
            },
        })

        result = {
            "status": "rejected",
            "plan_status": "REVIEW",
            "job_id": job_id,
            "reason": reason,
        }
        print(json.dumps(result, indent=2))
        return result
    finally:
        await _teardown(emitter, bus)


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
        # Dashboard phase bar: sprint is active whenever any stage is running.
        # Idempotent — re-emitting just keeps phases[sprint]='active'.
        await bus.emit(PhaseStarted(phase="sprint"))

        # Load plan and find stage (resolve relative paths against cwd)
        resolved_plan = Path(plan_path) if Path(plan_path).is_absolute() else Path(cwd) / plan_path
        plan_data = json.loads(resolved_plan.read_text())
        stage_def = None
        stage_index = 0
        stages_data = plan_data.get("stages", [])
        for i, s in enumerate(stages_data):
            if s["id"] == stage_id:
                stage_def = s
                stage_index = i
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

        stage_files = stage_def.get("files") or []

        # Capture pre-stage HEAD and dirty state. The SHA is passed to the
        # dispatcher as codex review base so review diffs this stage only.
        # Dirty stage files are recorded so the PASS-time auto-commit can
        # refuse to bundle edits that existed before this job started.
        pre_stage_sha = _git_head(cwd)
        pre_stage_dirty_files = _git_dirty_paths(cwd, stage_files)

        # Dispatch — file_scope limits Write/Edit to this stage's files.
        from sdk.agent_dispatch import AgentDispatcher, RateLimitError
        from sdk.job_runner import JobResult, run_job

        file_scope = stage_files or None
        dispatcher = AgentDispatcher(
            agents_dir=_agents_dir(),
            cwd=cwd,
            bus=bus,
            file_scope=file_scope,
            codex_review_base=pre_stage_sha,
            run_id=run_id,
            job_id=job_id,
        )
        await bus.emit(StageChanged(
            stage_name=stage.name,
            stage_index=stage_index,
            total_stages=len(stages_data),
            estimated_turns=stage.estimated_turns,
        ))
        if not stage_files:
            # Verify-only stage: no files to modify → skip implementer (who
            # would raise NEEDS_CONTEXT), skip test-engineer / codex (they
            # need a diff to review), and route straight to runtime-verifier.
            # Matches the Stage 8 ('Typecheck, Lint, QA') pattern that used
            # to waste a 900s implementer dispatch.
            try:
                runtime_result = await dispatcher.run_runtime_verifier(stage.name, task_context)
            except RateLimitError as exc:
                result = JobResult(
                    status="INTERRUPTED",
                    unresolved=[f"rate_limited: {exc}"],
                    interrupted_at="verify",
                )
            except Exception as exc:
                runtime_result = {"status": "error", "error": str(exc)}
                result = JobResult(
                    status="BLOCKED",
                    runtime_result=runtime_result,
                    unresolved=[f"runtime-verifier crashed: {exc}"],
                    completed_steps=["verify"],
                )
            else:
                if runtime_result.get("status") == "PASS":
                    result = JobResult(
                        status="PASS",
                        runtime_result=runtime_result,
                        completed_steps=["verify", "done"],
                    )
                else:
                    unresolved = (
                        runtime_result.get("output")
                        or runtime_result.get("error")
                        or "Runtime verification failed"
                    )
                    result = JobResult(
                        status="BLOCKED",
                        runtime_result=runtime_result,
                        unresolved=[unresolved],
                        completed_steps=["verify"],
                    )
        else:
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
            # No StageCompleted — the stage isn't finished, it's paused and may resume.
        else:
            await bus.emit(JobCompleted(
                job_id=job_id, command="run_job", status=result.status,
                result_summary=f"{stage.name}: {result.status}",
            ))
            # Dashboard stage row: flip this stage out of pending/active. "PASS"
            # → done (✓), "SKIPPED" → skipped (—), everything else → failed (✗).
            await bus.emit(StageCompleted(
                stage_name=stage.name, status=result.status,
            ))

        # Build result dict
        job_result = {"command": "run_job", "stage_id": stage_id, **result.to_dict()}
        if pre_stage_sha:
            job_result["pre_stage_sha"] = pre_stage_sha
        if pre_stage_dirty_files:
            job_result["pre_stage_dirty_files"] = pre_stage_dirty_files

        # Per-stage commit on PASS. Bounds the next stage's codex review
        # scope — without this, every review diffs merge-base..HEAD and
        # context grows linearly with the run. The helper commits only the
        # listed stage files, handles deletions, and records skip reasons
        # instead of silently doing nothing.
        if result.status == "PASS" and pre_stage_sha:
            commit_msg = f"[{stage_id}] {stage.name}"
            commit_info = _git_commit_stage(
                cwd,
                stage_files,
                commit_msg,
                expected_head=pre_stage_sha,
                pre_stage_dirty_files=pre_stage_dirty_files,
            )
            job_result["auto_commit"] = commit_info
            if commit_info.get("status") == "committed":
                job_result["commit_sha"] = commit_info.get("commit_sha")

        # Persist result
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
        await _teardown(emitter, bus)


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
        # Dashboard phase bar: verify is the first wrap-phase command. Close
        # sprint, open wrap. Both emits are idempotent — safe to re-emit from
        # review/document/run_complete.
        await bus.emit(PhaseCompleted(phase="sprint"))
        await bus.emit(PhaseStarted(phase="wrap"))

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
            run_id=run_id, job_id=job_id,
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

        _supersede_prior_jobs(cwd, run_id, "verify", job_id)
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
        await _teardown(emitter, bus)


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
        # Dashboard phase bar: wrap phase — see cmd_verify for rationale.
        await bus.emit(PhaseCompleted(phase="sprint"))
        await bus.emit(PhaseStarted(phase="wrap"))

        from sdk.agent_dispatch import AgentDispatcher
        dispatcher = AgentDispatcher(
            agents_dir=_agents_dir(), cwd=cwd, bus=bus,
            run_id=run_id, job_id=job_id,
        )

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

        _supersede_prior_jobs(cwd, run_id, "review", job_id)
        job_result = {"command": "review", "status": "PASS", "reviewer": reviewer, "findings": result_text}
        _write_job_result(cwd, run_id, job_id, job_result)
        print(json.dumps(job_result, indent=2))
        return job_result

    except Exception as exc:
        await bus.emit(JobCompleted(job_id=job_id, command="review", status="ERROR", result_summary=str(exc)))
        # Persist unless a prior PASS/PARTIAL already represents progress.
        error_result = {
            "command": "review", "status": "ERROR",
            "reviewer": reviewer, "error": str(exc),
        }
        _write_error_unless_prior_progress(cwd, run_id, job_id, "review", error_result)
        print(json.dumps(error_result, indent=2), file=sys.stderr)
        raise
    finally:
        _unregister_job(lock_path)
        await _teardown(emitter, bus)


# ---------------------------------------------------------------------------
# document
# ---------------------------------------------------------------------------

def _docs_touched_since(bus, mark_idx: int) -> list[str]:
    """List Write/Edit targets emitted after mark_idx in the bus event log.

    Used for partial-output detection when the documenter agent times out
    or errors: files it did update before the crash still count as work.
    """
    touched: list[str] = []
    events = bus.get_events()[mark_idx:]
    for ev in events:
        if ev.get("type") != "agent.tool_use":
            continue
        if ev.get("tool") not in ("Write", "Edit"):
            continue
        target = ev.get("target") or ""
        if target and target not in touched:
            touched.append(target)
    return touched


async def cmd_document(
    cwd: str,
    run_id: str,
    dashboard_url: str | None,
) -> dict:
    """Update documentation in two phases.

    Phase A (core): README, CLAUDE.md, CHANGELOG, plan.md status, session log.
    Phase B (cards): .ai/cards/* knowledge cards.

    Splitting lets us report PARTIAL when core docs land but cards don't —
    previously a 300s timeout on the single-shot documenter would mark the
    whole job ERROR even though most of the work was already on disk.
    """
    job_id = f"job-document-{uuid.uuid4().hex[:8]}"
    bus, emitter = await _setup_bus(run_id, dashboard_url, job_id=job_id, cwd=cwd)
    lock_path = _register_job(cwd, run_id, job_id, command="document")

    try:
        await bus.emit(JobRegistered(job_id=job_id, command="document", pid=os.getpid()))
        await bus.emit(JobStarted(job_id=job_id, command="document"))
        # Dashboard phase bar: wrap phase — see cmd_verify for rationale.
        await bus.emit(PhaseCompleted(phase="sprint"))
        await bus.emit(PhaseStarted(phase="wrap"))

        from sdk.agent_dispatch import AgentDispatcher
        dispatcher = AgentDispatcher(
            agents_dir=_agents_dir(), cwd=cwd, bus=bus,
            run_id=run_id, job_id=job_id,
        )

        context = _load_context(cwd, run_id, level="summary")

        # ---- Phase A: core docs + session log ----
        core_mark = len(bus.get_events())
        core_prompt = (
            f"{context}\n\n"
            "Phase 1 of 2: update the core project documentation only. "
            "In scope: README.md, CLAUDE.md, CHANGELOG.md, plan.md status "
            "fields, and the session log under .ai/sessions/. "
            "Out of scope for this phase: knowledge cards under .ai/cards/ — "
            "those are handled in a follow-up call. Do not touch them yet."
        )
        core_status = "PASS"
        core_error: str | None = None
        core_output = ""
        try:
            core_output = await dispatcher.query("documenter", core_prompt, model="sonnet")
        except Exception as exc:
            core_error = str(exc)
            core_status = "PARTIAL" if _docs_touched_since(bus, core_mark) else "ERROR"
        core_touched = _docs_touched_since(bus, core_mark)

        # ---- Phase B: knowledge cards (only if core landed) ----
        cards_mark = len(bus.get_events())
        cards_status: str
        cards_error: str | None = None
        cards_output = ""
        if core_status == "ERROR":
            cards_status = "SKIPPED"
        else:
            cards_prompt = (
                f"{context}\n\n"
                "Phase 2 of 2: promote reusable insights from this run into "
                "knowledge cards under .ai/cards/. "
                "If the session log you just wrote has a 'Knowledge proposals' "
                "section, use those; otherwise review the plan + run outcomes "
                "and decide whether any insight meets the bar for a card. "
                "If nothing qualifies, say so and exit — don't invent cards. "
                "Do NOT re-edit README / CLAUDE.md / CHANGELOG / session log "
                "— Phase 1 already handled those."
            )
            try:
                cards_output = await dispatcher.query("documenter", cards_prompt, model="sonnet")
                cards_status = "PASS"
            except Exception as exc:
                cards_error = str(exc)
                cards_status = "PARTIAL" if _docs_touched_since(bus, cards_mark) else "ERROR"
        cards_touched = _docs_touched_since(bus, cards_mark)

        # ---- Aggregate ----
        if core_status == "PASS" and cards_status == "PASS":
            overall = "PASS"
            summary = f"docs+cards: {len(core_touched)+len(cards_touched)} file(s) updated"
        elif core_status == "PASS" and cards_status in ("SKIPPED",):
            overall = "PASS"
            summary = f"docs: {len(core_touched)} file(s) updated; cards skipped"
        elif core_status in ("PASS", "PARTIAL") or cards_status == "PARTIAL":
            overall = "PARTIAL"
            summary = (
                f"core={core_status} ({len(core_touched)} file(s)), "
                f"cards={cards_status} ({len(cards_touched)} file(s))"
            )
        else:
            overall = "ERROR"
            summary = core_error or cards_error or "documenter failed"

        await bus.emit(JobCompleted(
            job_id=job_id, command="document", status=overall,
            result_summary=summary[:200],
        ))

        job_result = {
            "command": "document",
            "status": overall,
            "summary": summary,
            "core": {
                "status": core_status,
                "touched": core_touched,
                "error": core_error,
                "output": core_output[:500],
            },
            "cards": {
                "status": cards_status,
                "touched": cards_touched,
                "error": cards_error,
                "output": cards_output[:500],
            },
        }
        # Only supersede when the new result represents forward progress.
        # overall=ERROR means the core phase crashed before writing anything,
        # so the rerun added zero new state — keep any prior PASS/PARTIAL
        # intact instead of regressing a previously good run. Team-lead
        # reruns to recover.
        if overall != "ERROR":
            _supersede_prior_jobs(cwd, run_id, "document", job_id)
            _write_job_result(cwd, run_id, job_id, job_result)
        else:
            _write_error_unless_prior_progress(cwd, run_id, job_id, "document", job_result)
        print(json.dumps(job_result, indent=2))
        return job_result

    except Exception as exc:
        await bus.emit(JobCompleted(job_id=job_id, command="document", status="ERROR", result_summary=str(exc)))
        error_result = {"command": "document", "status": "ERROR", "error": str(exc)}
        _write_error_unless_prior_progress(cwd, run_id, job_id, "document", error_result)
        print(json.dumps(error_result, indent=2), file=sys.stderr)
        raise
    finally:
        _unregister_job(lock_path)
        await _teardown(emitter, bus)
