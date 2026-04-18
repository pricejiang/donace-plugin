"""CLI entry point: parses args and dispatches to sdk.commands subcommands.

This file used to host a legacy full-pipeline `run(task, ...)` that
dispatched through planner/architect agents. Those agents have been
removed, team-lead writes plans directly, and each pipeline stage
(plan, run_job, verify, review, document, run_complete) is a standalone
subcommand in sdk.commands. The only things that live here now are:

* `_parse_plan_stages` + `_resolve_deps` — plan.md → Stage[] parser used
  by `cmd_plan`.
* `detect_stack` — project stack sniffer used by `cmd_run_complete` and
  the review/verify commands to pick a stack-specific reviewer.
* `cmd_health` — pre-flight check that team-lead runs at session start.
* `main()` — argparse + subcommand dispatch.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Allow running as a script from any cwd:
#   python3 /abs/path/to/sdk/orchestrator.py ...
# Without this, `from sdk.X import Y` below fails unless cwd happens to
# be the plugin root. team-lead dispatches from the user's project cwd,
# so making this script self-bootstrapping is the only robust option.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk.events import Stage


# ---------------------------------------------------------------------------
# Stack detection
# ---------------------------------------------------------------------------

def detect_stack(cwd: str) -> str | None:
    """Detect the project's tech stack by checking characteristic files.

    Checks root and one level of subdirectories to handle monorepos
    (e.g., forkbar-app/package.json). Does not recurse deeper to avoid
    scanning node_modules, .git, etc.
    """
    root = Path(cwd)

    # Check root + immediate subdirectories
    dirs_to_check = [root] + [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")]

    has_ts = False
    has_ios = False
    has_python = False

    for d in dirs_to_check:
        if (d / "package.json").exists() or (d / "tsconfig.json").exists() or (d / "tsconfig.base.json").exists():
            has_ts = True
        if (d / "Podfile").exists() or any(d.glob("*.xcodeproj")) or any(d.glob("*.xcworkspace")):
            has_ios = True
        if (d / "pyproject.toml").exists() or (d / "requirements.txt").exists() or (d / "setup.py").exists():
            has_python = True

    if has_ts and has_ios:
        return "both"
    elif has_ts:
        return "typescript"
    elif has_ios:
        return "ios"
    elif has_python:
        return "python"
    return None


# ---------------------------------------------------------------------------
# Plan.md parsing
# ---------------------------------------------------------------------------

def _parse_plan_stages(content: str) -> list[Stage]:
    """Parse Stage entries from a plan markdown file.

    Handles dependency references in two forms:
    - "Stage 1, Stage 2" (by number)
    - "Auth Guard, API Routes" (by name)

    Both are resolved to actual stage names for the wave scheduler.
    """
    stages: list[Stage] = []

    # Match headers like "## Stage N: Name" or "### Stage N: Name"
    stage_header_pattern = re.compile(r"#{2,3}\s+Stage\s+(\d+):\s+(.+)")
    user_facing_pattern = re.compile(r"\*\*Has user-facing changes\*\*:\s*(Yes|No|yes|no|true|false)", re.IGNORECASE)
    deps_pattern = re.compile(r"\*\*Dependenc(?:y|ies)\*\*:\s*(.+)", re.IGNORECASE)
    turns_pattern = re.compile(r"\*\*Estimated turns\*\*:\s*(\d+)", re.IGNORECASE)
    files_pattern = re.compile(r"\*\*Files(?:\s+to\s+modify)?\*\*:\s*(.+)", re.IGNORECASE)

    # First pass: collect stage number → name mapping
    number_to_name: dict[str, str] = {}
    for line in content.split("\n"):
        m = stage_header_pattern.match(line.strip())
        if m:
            number_to_name[m.group(1)] = m.group(2).strip()

    # Second pass: parse stages with dependencies
    current_name: str | None = None
    current_user_facing = False
    current_deps_raw: list[str] = []
    current_estimated_turns = 0
    current_files: list[str] = []

    for line in content.split("\n"):
        stage_match = stage_header_pattern.match(line.strip())
        if stage_match:
            # Save previous stage
            if current_name:
                stages.append(Stage(
                    name=current_name,
                    has_user_facing_changes=current_user_facing,
                    depends_on=_resolve_deps(current_deps_raw, number_to_name),
                    estimated_turns=current_estimated_turns,
                    files=current_files,
                ))
            current_name = stage_match.group(2).strip()
            current_user_facing = False
            current_deps_raw = []
            current_estimated_turns = 0
            current_files = []
            continue

        uf_match = user_facing_pattern.search(line)
        if uf_match and current_name:
            val = uf_match.group(1).lower()
            current_user_facing = val in ("yes", "true")

        deps_match = deps_pattern.search(line)
        if deps_match and current_name:
            raw = deps_match.group(1).strip()
            if raw.lower() != "none":
                current_deps_raw = [
                    d.strip().removeprefix("Requires").strip()
                    for d in raw.split(",")
                    if d.strip().lower() != "none"
                ]

        turns_match = turns_pattern.search(line)
        if turns_match and current_name:
            current_estimated_turns = int(turns_match.group(1))

        files_match = files_pattern.search(line)
        if files_match and current_name:
            raw_files = files_match.group(1).strip()
            # Parse "path/a.ts (new), path/b.ts (modify)" or "path/a.ts, path/b.ts"
            for part in raw_files.split(","):
                # Strip annotations like "(new)", "(add WS upgrade)"
                path = re.sub(r"\s*\([^)]*\)\s*", "", part).strip()
                if path and path.lower() != "none":
                    current_files.append(path)

    # Save last stage
    if current_name:
        stages.append(Stage(
            name=current_name,
            has_user_facing_changes=current_user_facing,
            depends_on=_resolve_deps(current_deps_raw, number_to_name),
            estimated_turns=current_estimated_turns,
            files=current_files,
        ))

    return stages


def _resolve_deps(raw_deps: list[str], number_to_name: dict[str, str]) -> list[str]:
    """Resolve dependency references to actual stage names.

    Handles:
    - "Stage 1" → name of stage 1
    - "Stage 1, Stage 2" → names of stages 1 and 2
    - "Stages 1-3" → names of stages 1, 2, 3 (range)
    - "Stages 1-3 (some note)" → same, strips parenthetical
    - "Auth Guard" → "Auth Guard" (already a name)
    """
    resolved = []
    single_pattern = re.compile(r"Stage\s+(\d+)", re.IGNORECASE)
    range_pattern = re.compile(r"Stages?\s+(\d+)\s*[-–]\s*(\d+)", re.IGNORECASE)

    for dep in raw_deps:
        # Strip parenthetical notes like "(backend API changes must be deployed)"
        cleaned = re.sub(r"\s*\(.*\)\s*$", "", dep).strip()

        # Try range first: "Stages 1-3"
        range_match = range_pattern.match(cleaned)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            for n in range(start, end + 1):
                if str(n) in number_to_name:
                    resolved.append(number_to_name[str(n)])
            continue

        # Try single: "Stage 1"
        single_match = single_pattern.match(cleaned)
        if single_match and single_match.group(1) in number_to_name:
            resolved.append(number_to_name[single_match.group(1)])
            continue

        # Assume it's already a stage name
        resolved.append(dep)

    return resolved


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def cmd_health(cwd: str | None = None) -> int:
    """Verify orchestrator is reachable and its deps are installed.

    team-lead runs this at session start. If exit code is non-zero, the
    session aborts before run_start — fail fast instead of discovering
    missing deps mid-workflow.

    Returns 0 if all critical checks pass, 1 otherwise.
    """
    result: dict[str, Any] = {
        "status": "ok",
        "python": sys.version.split()[0],
        "orchestrator": str(Path(__file__).resolve()),
        "plugin_root": str(_REPO_ROOT),
        "imports": {},
        "optional": {},
        "errors": [],
        "warnings": [],
    }

    # Critical: internal sdk modules must import
    for mod in ("sdk.events", "sdk.commands", "sdk.agent_dispatch", "sdk.job_runner"):
        try:
            __import__(mod)
            result["imports"][mod] = "ok"
        except Exception as exc:
            result["imports"][mod] = f"error: {exc}"
            result["errors"].append(f"cannot import {mod}: {exc}")

    # Critical: claude-agent-sdk is the core dependency
    try:
        import claude_agent_sdk  # noqa: F401
        result["claude_agent_sdk"] = "ok"
    except ImportError as exc:
        result["claude_agent_sdk"] = f"missing: {exc}"
        result["errors"].append(
            "claude-agent-sdk not installed — run: pip install claude-agent-sdk"
        )

    # Optional: dashboard deps (orchestrator works without them, just no UI)
    for mod, purpose in (
        ("websockets", "dashboard streaming"),
        ("fastapi", "dashboard server"),
        ("uvicorn", "dashboard server"),
    ):
        try:
            __import__(mod)
            result["optional"][mod] = "ok"
        except ImportError:
            result["optional"][mod] = "missing"
            result["warnings"].append(f"{mod} missing (needed for {purpose})")

    # Optional: verify cwd is writable (blocks run_start if not)
    if cwd:
        result["cwd"] = cwd
        try:
            runs_dir = Path(cwd) / ".ai" / "runs"
            runs_dir.mkdir(parents=True, exist_ok=True)
            probe = runs_dir / ".health-probe"
            probe.touch()
            probe.unlink()
            result["cwd_writable"] = True
        except Exception as exc:
            result["cwd_writable"] = False
            result["errors"].append(f"cannot write to {cwd}/.ai/runs: {exc}")

    if result["errors"]:
        result["status"] = "error"
    elif result["warnings"]:
        result["status"] = "degraded"

    print(json.dumps(result, indent=2))
    return 1 if result["errors"] else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point. Dispatches subcommands to sdk.commands."""
    # Catch the legacy `--task` flag early so users who were in the habit
    # of calling the full pipeline get a clear migration message instead
    # of an unhelpful argparse "invalid choice" error. Done before the
    # parser is built so argparse never sees the flag.
    if "--task" in sys.argv[1:]:
        print(
            "ERROR: --task has been removed. The legacy full-pipeline mode "
            "relied on planner/architect agents that no longer exist.\n\n"
            "New flow:\n"
            "  1. Write a plan to .ai/runs/<run-id>/plan.md (team-lead does this)\n"
            "  2. orchestrator.py run_start --run-id <id> --cwd <dir>\n"
            "  3. orchestrator.py plan      --run-id <id> --cwd <dir>\n"
            "  4. orchestrator.py run_job   --stage-id <id> --plan <plan.json> --run-id <id> --cwd <dir>\n"
            "  5. orchestrator.py run_complete --run-id <id> --cwd <dir>\n\n"
            "See agents/team-lead.md for the plan template.",
            file=sys.stderr,
        )
        sys.exit(2)

    parser = argparse.ArgumentParser(description="donace orchestrator")
    subparsers = parser.add_subparsers(dest="command")

    # --- run_start ---
    p = subparsers.add_parser("run_start", help="Start a new run")
    p.add_argument("--run-id", required=True)
    p.add_argument("--cwd", type=str, default=os.getcwd())
    p.add_argument("--dashboard-url", default=None)

    # --- run_complete ---
    p = subparsers.add_parser("run_complete", help="Complete a run")
    p.add_argument("--run-id", required=True)
    p.add_argument("--cwd", type=str, default=os.getcwd())
    p.add_argument("--dashboard-url", default=None)

    # --- list_runs ---
    p = subparsers.add_parser(
        "list_runs",
        help="List runs in this project with state (completed / in_progress / "
             "abandoned / empty). Team-lead uses this at session startup to "
             "detect abandoned runs that may need to be resumed.",
    )
    p.add_argument("--cwd", type=str, default=os.getcwd())
    p.add_argument("--include-archived", action="store_true",
                   help="Also list runs that have been compressed into .ai/archive/")
    p.add_argument("--state", type=str, default=None,
                   choices=["completed", "in_progress", "abandoned", "empty"],
                   help="Only show runs matching this state")

    # --- mark ---
    p = subparsers.add_parser(
        "mark",
        help="Emit a phase start/complete marker. For team-lead to bracket "
             "work that happens outside the orchestrator (e.g. dispatching a "
             "Claude Code sub-agent to write plan.md via the superpowers skill).",
    )
    p.add_argument("--run-id", required=True)
    p.add_argument("--cwd", type=str, default=os.getcwd())
    p.add_argument("--dashboard-url", default=None)
    p.add_argument("--phase", required=True,
                   help="Short phase name, e.g. writing-plan")
    p.add_argument("--status", required=True, choices=["started", "completed"])

    # --- write_plan ---
    p = subparsers.add_parser(
        "write_plan",
        help="Dispatch the planner agent to produce .ai/runs/<id>/plan.md. "
             "Use this for complex tasks instead of having team-lead dispatch "
             "a general-purpose sub-agent — it's dashboard-visible and uses "
             "the same hook/token/validation infrastructure as every other "
             "step.",
    )
    p.add_argument("--cwd", type=str, default=os.getcwd())
    p.add_argument("--run-id", required=True)
    p.add_argument("--dashboard-url", default=None)
    p.add_argument("--task", required=True,
                   help="Task brief for the planner (a short paragraph is fine)")

    # --- plan ---
    p = subparsers.add_parser(
        "plan",
        help="Validate existing plan.md, run codex review, write plan.json sidecar. "
             "Team-lead writes the plan first (either directly or via write_plan).",
    )
    p.add_argument("--cwd", type=str, default=os.getcwd())
    p.add_argument("--run-id", required=True)
    p.add_argument("--dashboard-url", default=None)
    p.add_argument("--skip-codex", action="store_true",
                   help="Skip codex plan review (the only LLM step)")

    # --- run_job ---
    p = subparsers.add_parser("run_job", help="Execute a single stage")
    p.add_argument("--stage-id", required=True)
    p.add_argument("--plan", required=True, dest="plan_path")
    p.add_argument("--cwd", type=str, default=os.getcwd())
    p.add_argument("--run-id", required=True)
    p.add_argument("--dashboard-url", default=None)
    p.add_argument("--skip-agents", type=str, default="",
                   help="Comma-separated: test,codex,runtime")
    p.add_argument("--max-fix-attempts", type=int, default=1,
                   help="Fix attempts on first failure. Default 1 — escalate to team-lead instead of blindly retrying.")

    # --- verify ---
    p = subparsers.add_parser("verify", help="Run verification (read-only)")
    p.add_argument("--cwd", type=str, default=os.getcwd())
    p.add_argument("--run-id", required=True)
    p.add_argument("--dashboard-url", default=None)
    p.add_argument("--agents", type=str, default="test,codex",
                   help="Comma-separated: test,codex,runtime")
    p.add_argument("--scope", type=str, default="")

    # --- review ---
    p = subparsers.add_parser("review", help="Run code review")
    p.add_argument("--cwd", type=str, default=os.getcwd())
    p.add_argument("--run-id", required=True)
    p.add_argument("--dashboard-url", default=None)
    p.add_argument("--reviewer", type=str, default="typescript")

    # --- document ---
    p = subparsers.add_parser("document", help="Update documentation")
    p.add_argument("--cwd", type=str, default=os.getcwd())
    p.add_argument("--run-id", required=True)
    p.add_argument("--dashboard-url", default=None)

    # --- health ---
    p = subparsers.add_parser(
        "health", help="Smoke-test orchestrator reachability and deps",
    )
    p.add_argument("--cwd", type=str, default=None,
                   help="Optional project dir to check for writability")

    args = parser.parse_args()

    # --- health (runs before importing sdk.commands so missing deps
    #              produce a clean report instead of an ImportError) ---
    if args.command == "health":
        sys.exit(cmd_health(cwd=args.cwd))

    # --- Subcommand dispatch ---
    from sdk.commands import (
        cmd_run_start, cmd_run_complete, cmd_plan, cmd_run_job,
        cmd_verify, cmd_review, cmd_document, cmd_mark, cmd_write_plan,
        cmd_list_runs,
    )

    if args.command == "run_start":
        asyncio.run(cmd_run_start(args.run_id, args.cwd, args.dashboard_url))
    elif args.command == "run_complete":
        asyncio.run(cmd_run_complete(args.run_id, args.cwd, args.dashboard_url))
    elif args.command == "list_runs":
        asyncio.run(cmd_list_runs(
            cwd=args.cwd,
            include_archived=args.include_archived,
            state_filter=args.state,
        ))
    elif args.command == "mark":
        asyncio.run(cmd_mark(
            run_id=args.run_id, cwd=args.cwd,
            dashboard_url=args.dashboard_url,
            phase=args.phase, status=args.status,
        ))
    elif args.command == "write_plan":
        asyncio.run(cmd_write_plan(
            run_id=args.run_id, cwd=args.cwd,
            dashboard_url=args.dashboard_url,
            task=args.task,
        ))
    elif args.command == "plan":
        asyncio.run(cmd_plan(
            cwd=args.cwd, run_id=args.run_id,
            dashboard_url=args.dashboard_url,
            skip_codex=args.skip_codex,
        ))
    elif args.command == "run_job":
        skip = set(s for s in args.skip_agents.split(",") if s)
        asyncio.run(cmd_run_job(
            stage_id=args.stage_id, plan_path=args.plan_path,
            cwd=args.cwd, run_id=args.run_id,
            dashboard_url=args.dashboard_url,
            skip_agents=skip, max_fix_attempts=args.max_fix_attempts,
        ))
    elif args.command == "verify":
        agents = set(s for s in args.agents.split(",") if s)
        asyncio.run(cmd_verify(
            cwd=args.cwd, run_id=args.run_id,
            dashboard_url=args.dashboard_url,
            agents=agents, scope=args.scope,
        ))
    elif args.command == "review":
        asyncio.run(cmd_review(
            cwd=args.cwd, run_id=args.run_id,
            dashboard_url=args.dashboard_url, reviewer=args.reviewer,
        ))
    elif args.command == "document":
        asyncio.run(cmd_document(
            cwd=args.cwd, run_id=args.run_id,
            dashboard_url=args.dashboard_url,
        ))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
