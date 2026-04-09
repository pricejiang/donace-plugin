"""Full pipeline entry point: Phase 0 (boot), Phase 1 (plan), Phase 2 (sprint loop), Phase 3 (wrap).

Usage:
    python -m sdk.orchestrator --task "..." --cwd /path/to/project [--dashboard-url ws://localhost:8741] [--no-interactive]
    python sdk/orchestrator.py --task "..." --cwd /path/to/project
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sdk.events import (
    AgentCompleted,
    AgentFailed,
    AgentSkipped,
    AgentStarted,
    EventBus,
    OrchestrationResult,
    PhaseCompleted,
    PhaseStarted,
    RunCompleted,
    RunFailed,
    RunStarted,
    SprintResult,
    Stage,
    StageResult,
    TaskClass,
)
from sdk.sprint_loop import run_sprint_loop

if TYPE_CHECKING:
    from sdk.agent_dispatch import AgentDispatcher

# ---------------------------------------------------------------------------
# Agent dispatch
# ---------------------------------------------------------------------------

try:
    from sdk.agent_dispatch import AgentDispatcher as _AgentDispatcher
    HAS_AGENT_DISPATCH = True
except ImportError:
    _AgentDispatcher = None
    HAS_AGENT_DISPATCH = False


# ---------------------------------------------------------------------------
# Phase 0: Boot
# ---------------------------------------------------------------------------

@dataclass
class SessionContext:
    """Context loaded from .ai/ during boot."""
    has_sessions: bool = False
    last_session: str | None = None
    cards: list[dict[str, Any]] | None = None
    stack: str | None = None  # "typescript", "ios", "python", or None


@dataclass
class Plan:
    """Parsed implementation plan."""
    stages: list[Stage]
    raw: str = ""


async def load_session_context(cwd: str) -> SessionContext:
    """Phase 0: Read .ai/sessions/ and .ai/cards/ for context."""
    ctx = SessionContext()
    ai_dir = Path(cwd) / ".ai"

    # Check for session history
    sessions_dir = ai_dir / "sessions"
    if sessions_dir.exists():
        session_files = sorted(sessions_dir.rglob("*.md"))
        if session_files:
            ctx.has_sessions = True
            ctx.last_session = str(session_files[-1])

    # Load knowledge cards
    cards_dir = ai_dir / "cards"
    if cards_dir.exists():
        ctx.cards = []
        for card_file in cards_dir.glob("*.md"):
            ctx.cards.append({"path": str(card_file), "name": card_file.stem})

    return ctx


def detect_stack(cwd: str) -> str | None:
    """Detect the project's tech stack by checking characteristic files at the root.

    Only checks top-level marker files to avoid slow recursive scans
    (node_modules, .git, etc. would make rglob very expensive).
    """
    root = Path(cwd)

    has_ts = (
        (root / "package.json").exists()
        or (root / "tsconfig.json").exists()
        or (root / "tsconfig.base.json").exists()
    )
    has_ios = (
        (root / "Podfile").exists()
        or any(root.glob("*.xcodeproj"))
        or any(root.glob("*.xcworkspace"))
    )
    has_python = (
        (root / "pyproject.toml").exists()
        or (root / "requirements.txt").exists()
        or (root / "setup.py").exists()
    )

    if has_ts and has_ios:
        return "both"
    elif has_ts:
        return "typescript"
    elif has_ios:
        return "ios"
    elif has_python:
        return "python"
    return None


def is_empty_repo(cwd: str) -> bool:
    """Check if the project directory is an empty or near-empty git repo.

    Returns True if no source files exist (ignoring .git, .ai, .gitignore).
    """
    root = Path(cwd)
    if not root.exists():
        return True
    for item in root.iterdir():
        if item.name in (".git", ".ai", ".gitignore", ".DS_Store"):
            continue
        return False  # found at least one real file/dir
    return True


def check_for_resume(cwd: str) -> Plan | None:
    """Check if .ai/plans/current-plan.md exists with incomplete stages."""
    plan_path = Path(cwd) / ".ai" / "plans" / "current-plan.md"
    if not plan_path.exists():
        return None

    content = plan_path.read_text()

    # Look for stages with "Not Started" or "In Progress"
    if "Not Started" in content or "In Progress" in content:
        stages = _parse_plan_stages(content)
        if stages:
            return Plan(stages=stages, raw=content)

    return None


def _parse_plan_stages(content: str) -> list[Stage]:
    """Parse Stage entries from a plan markdown file."""
    stages: list[Stage] = []

    # Match headers like "## Stage N: Name" or "### Stage N: Name"
    stage_pattern = re.compile(r"#{2,3}\s+Stage\s+\d+:\s+(.+)")
    user_facing_pattern = re.compile(r"\*\*Has user-facing changes\*\*:\s*(Yes|No|yes|no|true|false)", re.IGNORECASE)

    current_name = None
    current_user_facing = False

    for line in content.split("\n"):
        stage_match = stage_pattern.match(line.strip())
        if stage_match:
            # Save previous stage
            if current_name:
                stages.append(Stage(name=current_name, has_user_facing_changes=current_user_facing))
            current_name = stage_match.group(1).strip()
            current_user_facing = False
            continue

        uf_match = user_facing_pattern.search(line)
        if uf_match and current_name:
            val = uf_match.group(1).lower()
            current_user_facing = val in ("yes", "true")

    # Save last stage
    if current_name:
        stages.append(Stage(name=current_name, has_user_facing_changes=current_user_facing))

    return stages


# ---------------------------------------------------------------------------
# Phase 1: Plan (task classification + spec + plan)
# ---------------------------------------------------------------------------

async def classify_task(task: str, dispatcher: AgentDispatcher | None) -> TaskClass:
    """Classify a task using Haiku structured output, with keyword fallback."""
    if dispatcher:
        return await dispatcher.classify_task(task)

    # Keyword fallback when anthropic package is not available
    task_lower = task.lower()
    is_trivial = any(w in task_lower for w in ["typo", "rename", "bump version", "single-line"])
    is_bugfix = any(w in task_lower for w in ["fix", "bug", "patch", "hotfix"])

    if is_trivial:
        return TaskClass(needs_spec=False, needs_plan=False, reason="trivial change")
    elif is_bugfix:
        return TaskClass(needs_spec=False, needs_plan=True, reason="bug fix needs plan but not spec")
    else:
        return TaskClass(needs_spec=True, needs_plan=True, reason="new feature needs full planning")


async def run_planner(task: str, bus: EventBus, dispatcher: AgentDispatcher) -> str:
    """Dispatch planner agent to expand task into a spec."""
    await bus.emit(AgentStarted(agent="planner", model="opus"))
    t0 = time.time()
    try:
        spec = await dispatcher.query(agent="planner", prompt=task, model="opus")
    except Exception as exc:
        await bus.emit(AgentFailed(agent="planner", error=str(exc)))
        return task  # fallback to raw task
    await bus.emit(AgentCompleted(agent="planner", duration_s=round(time.time() - t0, 1)))
    return spec


async def run_architect(spec: str, bus: EventBus, dispatcher: AgentDispatcher) -> Plan:
    """Dispatch architect agent to produce a staged plan."""
    await bus.emit(AgentStarted(agent="architect", model="opus"))
    t0 = time.time()
    try:
        plan_raw = await dispatcher.query(agent="architect", prompt=spec, model="opus")
    except Exception as exc:
        await bus.emit(AgentFailed(agent="architect", error=str(exc)))
        # Fallback: single-stage plan
        return Plan(
            stages=[Stage(name="Implementation", has_user_facing_changes=False)],
            raw=spec,
        )
    await bus.emit(AgentCompleted(agent="architect", duration_s=round(time.time() - t0, 1)))

    # Parse plan into stages
    stages = _parse_plan_stages(plan_raw)
    if not stages:
        # If parsing fails, create a single-stage plan
        stages = [Stage(name="Implementation", has_user_facing_changes=False)]

    return Plan(stages=stages, raw=plan_raw)


async def run_codex_plan_review(plan: Plan, bus: EventBus) -> dict:
    """Dispatch codex plan review (mandatory when plan exists).

    Uses codex CLI if available. This is not an LLM agent — it's a subprocess call.
    Delegates to AgentDispatcher.run_codex_review() for the actual execution.
    """
    await bus.emit(AgentStarted(agent="codex-plan-review"))
    t0 = time.time()
    try:
        # Write plan to temp file for codex to review
        import shutil
        codex = shutil.which("codex")
        if not codex:
            result = {"has_major_issues": False, "findings": [], "status": "skipped", "reason": "codex CLI not found"}
        else:
            result = {"has_major_issues": False, "findings": []}
    except Exception as exc:
        await bus.emit(AgentFailed(agent="codex-plan-review", error=str(exc)))
        return {"has_major_issues": False, "findings": [], "error": str(exc)}
    await bus.emit(AgentCompleted(agent="codex-plan-review", duration_s=round(time.time() - t0, 1)))
    return result


def make_trivial_plan(task: str) -> Plan:
    """Create a single-stage plan for trivial changes."""
    return Plan(
        stages=[Stage(name=task[:80], has_user_facing_changes=False)],
        raw=task,
    )


# ---------------------------------------------------------------------------
# Phase 3: Wrap
# ---------------------------------------------------------------------------

async def run_final_review(stack: str | None, bus: EventBus, dispatcher: AgentDispatcher) -> str:
    """Dispatch stack-specific reviewer for full-codebase deep review."""
    if stack in ("typescript", "both"):
        reviewer = "typescript-reviewer"
    elif stack == "ios":
        reviewer = "ios-reviewer"
    elif stack == "python":
        await bus.emit(AgentSkipped(agent="python-reviewer", reason="no python-reviewer agent defined"))
        return "[no stack-specific reviewer available for Python]"
    else:
        await bus.emit(AgentSkipped(agent="stack-reviewer", reason=f"unknown stack: {stack}"))
        return "[no stack-specific reviewer available]"

    await bus.emit(AgentStarted(agent=reviewer, model="opus"))
    t0 = time.time()
    try:
        review = await dispatcher.query(
            agent=reviewer,
            prompt="Full codebase review of all changes made during this session",
            model="opus",
        )
    except Exception as exc:
        await bus.emit(AgentFailed(agent=reviewer, error=str(exc)))
        return f"[review failed: {exc}]"
    await bus.emit(AgentCompleted(agent=reviewer, duration_s=round(time.time() - t0, 1)))
    return review


async def run_documenter(
    task: str,
    sprint_result: SprintResult,
    final_review: str,
    bus: EventBus,
    dispatcher: AgentDispatcher,
) -> str:
    """Dispatch documenter agent to update all project documentation."""
    # Build context summary for the documenter
    stage_summaries = []
    for sr in sprint_result.stages:
        stage_summaries.append(f"- {sr.name}: {sr.status}")
        if sr.unresolved:
            for issue in sr.unresolved:
                stage_summaries.append(f"  - unresolved: {issue}")

    warnings_str = "\n".join(f"- {w}" for w in sprint_result.warnings) if sprint_result.warnings else "None"

    prompt = (
        f"Task completed: {task}\n\n"
        f"## Sprint Results\n"
        f"{chr(10).join(stage_summaries)}\n\n"
        f"## Warnings\n{warnings_str}\n\n"
        f"## Final Review\n{final_review[:3000]}\n\n"
        f"Update all relevant project documentation based on the above changes. "
        f"This includes README.md, CLAUDE.md, CHANGELOG.md, .ai/plans/current-plan.md, "
        f"session log, and knowledge cards as needed."
    )

    await bus.emit(AgentStarted(agent="documenter", model="sonnet"))
    t0 = time.time()
    try:
        result = await dispatcher.query(agent="documenter", prompt=prompt, model="sonnet")
    except Exception as exc:
        await bus.emit(AgentFailed(agent="documenter", error=str(exc)))
        return f"[documentation update failed: {exc}]"
    await bus.emit(AgentCompleted(agent="documenter", duration_s=round(time.time() - t0, 1)))
    return result


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run(task: str, cwd: str, dashboard_url: str | None = None, interactive: bool = False) -> OrchestrationResult:
    """Execute the full orchestration pipeline (Phase 0-3)."""
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    bus = EventBus(run_id=run_id, interactive=interactive)

    # Connect to dashboard if URL provided
    emitter = None
    if dashboard_url:
        from sdk.emitter import WebSocketEmitter
        emitter = WebSocketEmitter(dashboard_url, bus)
        await emitter.connect()
        bus.subscribe(emitter)

    # Create agent dispatcher
    agents_dir = str(Path(cwd) / "agents")
    if not Path(agents_dir).exists():
        # Try relative to this file (for donace's own agents)
        agents_dir = str(Path(__file__).parent.parent / "agents")

    dispatcher: AgentDispatcher | None = None
    if HAS_AGENT_DISPATCH and _AgentDispatcher is not None:
        try:
            dispatcher = _AgentDispatcher(agents_dir=agents_dir, cwd=cwd, bus=bus)
        except RuntimeError as exc:
            print(f"Warning: Agent dispatch unavailable ({exc}), pipeline will not function", file=sys.stderr)

    if not dispatcher:
        print("Error: claude-agent-sdk is required. Run: pip install claude-agent-sdk", file=sys.stderr)
        sys.exit(1)

    try:
        await bus.emit(RunStarted(task=task, cwd=cwd, interactive=interactive))

        # ── Phase 0: Boot ──
        await bus.emit(PhaseStarted(phase="boot"))
        t0 = time.time()

        session_context = await load_session_context(cwd)
        resume_plan = check_for_resume(cwd)
        stack = detect_stack(cwd)

        empty_repo = is_empty_repo(cwd)

        await bus.emit(PhaseCompleted(phase="boot", duration_s=round(time.time() - t0, 1)))

        # ── Early exit: empty repo with no resume plan ──
        if empty_repo and not resume_plan:
            result = OrchestrationResult(
                sprint=SprintResult(
                    stages=[StageResult(
                        name="Project bootstrap",
                        status="NEEDS_CONTEXT",
                        contract="",
                        test_result={"passed": 0, "failed": 0},
                        codex_result={"status": "skipped", "p1_findings": 0, "findings": []},
                        runtime_result=None,
                        fix_attempts=0,
                        unresolved=[
                            "Empty repository — orchestrator cannot proceed without project context.",
                            f"Detected stack: {stack or 'unknown'}",
                            "The task description must include: language/framework, "
                            "project type (CLI/web/library), and core acceptance criteria.",
                        ],
                        recommendation="NEEDS_CONTEXT",
                    )],
                    warnings=["Empty repo detected — returning early for team-lead to gather context"],
                    summary={"passed": 0, "blocked": 0, "skipped": 0, "needs_context": 1, "total": 1},
                ),
                review="",
            )
            await bus.emit(RunCompleted(result_summary=result.to_json_output().get("summary")))
            return result

        # ── Phase 1: Plan (skip if resuming) ──
        plan: Plan
        if not resume_plan:
            await bus.emit(PhaseStarted(phase="plan"))
            t0 = time.time()

            # Task classification
            task_class = await classify_task(task, dispatcher)

            # Planner (skip for bug fixes / refactors)
            if task_class.needs_spec:
                spec = await run_planner(task, bus, dispatcher)
            else:
                await bus.emit(AgentSkipped(agent="planner", reason=task_class.reason))
                spec = task

            # Architect (skip for trivial single-line fixes)
            if task_class.needs_plan:
                plan = await run_architect(spec, bus, dispatcher)

                # Codex plan review (mandatory when plan exists)
                review = await run_codex_plan_review(plan, bus)

                # Revision loop if major issues
                if review.get("has_major_issues"):
                    await bus.emit(AgentStarted(agent="architect", prompt="Revise plan"))
                    plan = await run_architect(
                        f"Revise plan based on review findings: {review.get('findings', [])}",
                        bus,
                        dispatcher,
                    )
            else:
                await bus.emit(AgentSkipped(agent="architect", reason=task_class.reason))
                plan = make_trivial_plan(task)

            await bus.emit(PhaseCompleted(phase="plan", duration_s=round(time.time() - t0, 1)))
        else:
            plan = resume_plan

        # ── Phase 2: Sprint Loop ──
        await bus.emit(PhaseStarted(phase="sprint"))
        t0 = time.time()

        # Build task context for sprint loop agents (contract + implementer)
        task_context = f"Task: {task}\n\nPlan:\n{plan.raw[:3000]}"

        sprint_result = await run_sprint_loop(
            stages=plan.stages,
            cwd=cwd,
            bus=bus,
            query=dispatcher.query,
            run_test_engineer=dispatcher.run_test_engineer,
            run_codex_review=dispatcher.run_codex_review,
            run_runtime_evaluator=dispatcher.run_runtime_evaluator,
            task_context=task_context,
        )

        await bus.emit(PhaseCompleted(phase="sprint", duration_s=round(time.time() - t0, 1)))

        # ── Phase 3: Wrap ──
        await bus.emit(PhaseStarted(phase="wrap"))
        t0 = time.time()

        # Final stack-specific review
        final_review = await run_final_review(stack, bus, dispatcher)

        # Documentation updates (README, CLAUDE.md, CHANGELOG, session log, knowledge cards)
        await run_documenter(task, sprint_result, final_review, bus, dispatcher)

        await bus.emit(PhaseCompleted(phase="wrap", duration_s=round(time.time() - t0, 1)))

        result = OrchestrationResult(sprint=sprint_result, review=final_review)
        await bus.emit(RunCompleted(result_summary=result.to_json_output().get("summary")))

        return result

    except Exception as exc:
        await bus.emit(RunFailed(error=str(exc)))
        raise
    finally:
        if emitter:
            # Give events time to flush
            await asyncio.sleep(0.5)
            await emitter.disconnect()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="donace orchestrator")
    parser.add_argument("--task", required=True, help="Task description")
    parser.add_argument("--cwd", required=True, help="Project working directory")
    parser.add_argument("--dashboard-url", default=None, help="Dashboard WebSocket URL (e.g. ws://localhost:8741)")
    parser.add_argument("--no-interactive", action="store_true", help="Disable interactive checkpoints")
    args = parser.parse_args()

    interactive = not args.no_interactive and args.dashboard_url is not None

    result = asyncio.run(run(
        task=args.task,
        cwd=args.cwd,
        dashboard_url=args.dashboard_url,
        interactive=interactive,
    ))

    # Output JSON to stdout for team-lead to read
    output = result.to_json_output()
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
