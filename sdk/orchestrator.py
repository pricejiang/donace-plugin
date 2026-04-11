"""Full pipeline entry point: Phase 0 (boot), Phase 1 (plan), Phase 2 (sprint loop), Phase 3 (wrap).

Usage:
    python -m sdk.orchestrator --task "..." --cwd /path/to/project [--dashboard-url ws://localhost:8741] [--no-interactive]
    python sdk/orchestrator.py --task "..." --cwd /path/to/project
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
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
    RunValidation,
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
# Shared context — accumulated across phases, passed to each agent
# ---------------------------------------------------------------------------

class SharedContext:
    """Pipeline-wide context that accumulates as each phase runs.

    Avoids redundant codebase exploration — each agent gets a summary
    of what previous agents already discovered and produced.
    Persisted to .ai/runs/{run_id}/context.md for debugging.
    """

    def __init__(self, run_id: str, cwd: str, task: str) -> None:
        self.run_id = run_id
        self.cwd = cwd
        self.sections: list[str] = [f"# Run Context: {run_id}", f"\n## Task\n{task}"]

    def add(self, heading: str, content: str) -> None:
        self.sections.append(f"\n## {heading}\n{content}")

    def to_prompt_prefix(self) -> str:
        """Return the full context as a prompt prefix block."""
        body = "\n".join(self.sections)
        return f"<run-context>\n{body}\n</run-context>\n\n"

    def save(self) -> None:
        """Persist to .ai/runs/{run_id}/context.md for debugging."""
        path = Path(self.cwd) / ".ai" / "runs" / f"{self.run_id}.context.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.sections))


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
                ))
            current_name = stage_match.group(2).strip()
            current_user_facing = False
            current_deps_raw = []
            current_estimated_turns = 0
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

    # Save last stage
    if current_name:
        stages.append(Stage(
            name=current_name,
            has_user_facing_changes=current_user_facing,
            depends_on=_resolve_deps(current_deps_raw, number_to_name),
            estimated_turns=current_estimated_turns,
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

    # Architect writes plan to .ai/plans/current-plan.md.
    # The agent's return value is often a summary, not the full plan.
    # Read the actual file if it exists.
    plan_file = Path(dispatcher.cwd) / ".ai" / "plans" / "current-plan.md"
    if plan_file.exists():
        plan_content = plan_file.read_text("utf-8")
        # Use file content if it has stage headers; otherwise fall back to agent output
        file_stages = _parse_plan_stages(plan_content)
        if file_stages:
            return Plan(stages=file_stages, raw=plan_content)

    # Fallback: try parsing the agent's direct response
    stages = _parse_plan_stages(plan_raw)
    if not stages:
        stages = [Stage(name="Implementation", has_user_facing_changes=False)]

    return Plan(stages=stages, raw=plan_raw)


async def run_codex_plan_review(plan: Plan, bus: EventBus, dispatcher: AgentDispatcher) -> dict:
    """Dispatch Codex plan review (mandatory when plan exists)."""
    await bus.emit(AgentStarted(agent="codex-plan-review"))
    t0 = time.time()
    try:
        result = await dispatcher.run_codex_plan_review(plan.raw)
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

async def run_final_review(stack: str | None, shared_ctx: SharedContext, bus: EventBus, dispatcher: AgentDispatcher) -> str:
    """Dispatch stack-specific reviewer for full-codebase deep review."""
    if stack in ("typescript", "both"):
        reviewer = "typescript-reviewer"
    elif stack == "ios":
        reviewer = "ios-reviewer"
    elif stack == "python":
        await bus.emit(AgentSkipped(agent="final-review", reason="no reviewer agent for Python projects"))
        return "[no stack-specific reviewer available for Python]"
    else:
        await bus.emit(AgentSkipped(agent="final-review", reason=f"no reviewer agent for stack: {stack}"))
        return "[no stack-specific reviewer available]"

    await bus.emit(AgentStarted(agent=reviewer, model="opus"))
    t0 = time.time()
    try:
        prompt = (
            f"{shared_ctx.to_prompt_prefix()}"
            f"Full codebase review of all changes made during this session. "
            f"Context about what changed is in <run-context> above."
        )
        review = await dispatcher.query(
            agent=reviewer,
            prompt=prompt,
            model="opus",
        )
    except Exception as exc:
        await bus.emit(AgentFailed(agent=reviewer, error=str(exc)))
        return f"[review failed: {exc}]"
    await bus.emit(AgentCompleted(agent=reviewer, duration_s=round(time.time() - t0, 1)))
    return review


async def run_documenter(
    shared_ctx: SharedContext,
    final_review: str,
    bus: EventBus,
    dispatcher: AgentDispatcher,
) -> str:
    """Dispatch documenter agent to update all project documentation.

    Uses the shared context so the documenter doesn't need to explore the codebase.
    """
    shared_ctx.add("Final Review", final_review[:3000] if final_review else "(no review)")

    prompt = (
        f"{shared_ctx.to_prompt_prefix()}"
        f"All context about what changed is in <run-context> above. "
        f"Do NOT explore the codebase to discover what changed — the context is complete.\n\n"
        f"Update all relevant project documentation: README.md, CLAUDE.md, CHANGELOG.md, "
        f".ai/plans/current-plan.md, session log, and knowledge cards as needed."
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

# ---------------------------------------------------------------------------
# Run state persistence (for resume)
# ---------------------------------------------------------------------------

@dataclass
class RunState:
    """Persisted state of an in-progress run. Written after each stage completes."""
    run_id: str
    task: str
    cwd: str
    phase: str                           # "plan", "sprint", "wrap"
    plan_raw: str = ""
    stages_completed: list[dict] = field(default_factory=list)  # list of StageResult dicts
    stages_remaining: list[dict] = field(default_factory=list)  # list of Stage dicts
    warnings: list[str] = field(default_factory=list)

    def save(self) -> None:
        state_path = Path(self.cwd) / ".ai" / "runs" / f"{self.run_id}.state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "run_id": self.run_id,
            "task": self.task,
            "cwd": self.cwd,
            "phase": self.phase,
            "plan_raw": self.plan_raw,
            "stages_completed": self.stages_completed,
            "stages_remaining": self.stages_remaining,
            "warnings": self.warnings,
        }, indent=2))

    def delete(self) -> None:
        state_path = Path(self.cwd) / ".ai" / "runs" / f"{self.run_id}.state.json"
        try:
            state_path.unlink(missing_ok=True)
        except Exception:
            pass


def _find_incomplete_run(cwd: str) -> RunState | None:
    """Check .ai/runs/ for a state file without a matching result file (= incomplete run)."""
    runs_dir = Path(cwd) / ".ai" / "runs"
    if not runs_dir.exists():
        return None

    for state_file in sorted(runs_dir.glob("*.state.json"), reverse=True):
        run_id = state_file.stem.replace(".state", "")
        result_file = runs_dir / f"{run_id}.json"
        if not result_file.exists():
            # Found an incomplete run
            try:
                data = json.loads(state_file.read_text())
                return RunState(
                    run_id=data["run_id"],
                    task=data["task"],
                    cwd=data["cwd"],
                    phase=data.get("phase", "sprint"),
                    plan_raw=data.get("plan_raw", ""),
                    stages_completed=data.get("stages_completed", []),
                    stages_remaining=data.get("stages_remaining", []),
                    warnings=data.get("warnings", []),
                )
            except (json.JSONDecodeError, KeyError):
                continue
    return None


# ---------------------------------------------------------------------------
# Process lock
# ---------------------------------------------------------------------------

def _acquire_lock(cwd: str, run_id: str) -> Path:
    """Acquire a project-level lock. Kill any stale orchestrator for this project."""
    lock_path = Path(cwd) / ".ai" / "runs" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if lock_path.exists():
        try:
            lock_data = json.loads(lock_path.read_text())
            old_pid = lock_data.get("pid")
            old_run = lock_data.get("run_id", "unknown")
            if old_pid:
                # Check if process is still alive
                os.kill(old_pid, 0)  # raises OSError if dead
                # Still alive — kill it
                print(f"Warning: killing stale orchestrator (pid={old_pid}, run={old_run})", file=sys.stderr)
                os.kill(old_pid, 9)
                import time as _time
                _time.sleep(0.5)  # wait for process to die
        except (OSError, json.JSONDecodeError, KeyError):
            pass  # process already dead or lock corrupt — clean up

    lock_path.write_text(json.dumps({"pid": os.getpid(), "run_id": run_id}))
    return lock_path


def _release_lock(cwd: str) -> None:
    """Release the project-level lock."""
    lock_path = Path(cwd) / ".ai" / "runs" / ".lock"
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


async def run(task: str, cwd: str, dashboard_url: str | None = None, interactive: bool = False) -> OrchestrationResult:
    """Execute the full orchestration pipeline (Phase 0-3).

    Automatically resumes incomplete runs if a .state.json file exists
    without a corresponding result .json file.
    """
    # ── Check for incomplete run to resume ──
    prev_run = _find_incomplete_run(cwd)
    if prev_run:
        run_id = prev_run.run_id
        print(f"Resuming incomplete run {run_id} ({len(prev_run.stages_completed)} stages done)", file=sys.stderr)
    else:
        run_id = f"run-{uuid.uuid4().hex[:8]}"

    lock_path = _acquire_lock(cwd, run_id)
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

    # Initialize run state for persistence
    run_state = RunState(run_id=run_id, task=task, cwd=cwd, phase="boot")

    # Shared context — accumulated across phases
    shared_ctx = SharedContext(run_id=run_id, cwd=cwd, task=task)

    try:
        await bus.emit(RunStarted(task=task, cwd=cwd, interactive=interactive))

        # ── Phase 0: Boot ──
        await bus.emit(PhaseStarted(phase="boot"))
        t0 = time.time()

        session_context = await load_session_context(cwd)
        stack = detect_stack(cwd)
        empty_repo = is_empty_repo(cwd)

        shared_ctx.add("Project", f"Stack: {stack or 'unknown'}\nCwd: {cwd}")
        await bus.emit(PhaseCompleted(phase="boot", duration_s=round(time.time() - t0, 1)))

        # ── Early exit: empty repo ──
        if empty_repo and not prev_run:
            result = OrchestrationResult(
                run_id=run_id,
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
        if prev_run and prev_run.phase in ("sprint", "wrap"):
            # Resuming — rebuild plan from state
            completed_names = {s["name"] for s in prev_run.stages_completed}
            remaining_stages = [
                Stage(name=s["name"], has_user_facing_changes=s.get("has_user_facing_changes", False), depends_on=s.get("depends_on", []))
                for s in prev_run.stages_remaining
                if s["name"] not in completed_names
            ]
            all_stages = [
                Stage(name=s["name"], has_user_facing_changes=s.get("has_user_facing_changes", False), depends_on=s.get("depends_on", []))
                for s in prev_run.stages_remaining
            ]
            plan = Plan(stages=all_stages, raw=prev_run.plan_raw)
        else:
            resume_plan = check_for_resume(cwd)
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
                    review = await run_codex_plan_review(plan, bus, dispatcher)

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

        # Add plan to shared context
        stage_list = "\n".join(
            f"- Stage {i+1}: {s.name} (user-facing: {s.has_user_facing_changes}, depends: {s.depends_on or 'none'})"
            for i, s in enumerate(plan.stages)
        )
        shared_ctx.add("Plan", f"Stages:\n{stage_list}\n\nFull plan:\n{plan.raw[:3000]}")
        shared_ctx.save()

        # Save plan to run state (for resume)
        run_state.phase = "sprint"
        run_state.plan_raw = plan.raw
        run_state.stages_remaining = [
            {"name": s.name, "has_user_facing_changes": s.has_user_facing_changes, "depends_on": s.depends_on}
            for s in plan.stages
        ]
        if prev_run:
            run_state.stages_completed = prev_run.stages_completed
            run_state.warnings = prev_run.warnings
        run_state.save()

        # ── Phase 2: Sprint Loop ──
        await bus.emit(PhaseStarted(phase="sprint"))
        t0 = time.time()

        # Build task context from shared context
        task_context = shared_ctx.to_prompt_prefix()

        # Build list of already-completed stage names (for resume skip)
        completed_stage_names: set[str] = set()
        if prev_run:
            completed_stage_names = {s["name"] for s in prev_run.stages_completed}

        sprint_result = await run_sprint_loop(
            stages=plan.stages,
            cwd=cwd,
            bus=bus,
            query=dispatcher.query,
            run_test_engineer=dispatcher.run_test_engineer,
            run_codex_review=dispatcher.run_codex_review,
            run_runtime_evaluator=dispatcher.run_runtime_evaluator,
            task_context=task_context,
            completed_stage_names=completed_stage_names,
            on_stage_complete=lambda sr: _on_stage_complete(run_state, shared_ctx, sr),
        )

        # Merge warnings from previous run
        if prev_run:
            sprint_result.warnings = prev_run.warnings + sprint_result.warnings
            # Prepend completed stages from previous run
            prev_stage_results = [
                StageResult(**s) for s in prev_run.stages_completed
            ]
            sprint_result.stages = prev_stage_results + sprint_result.stages
            # Recalculate summary
            passed = sum(1 for r in sprint_result.stages if r.status == "PASS")
            blocked = sum(1 for r in sprint_result.stages if r.status == "BLOCKED")
            skipped = sum(1 for r in sprint_result.stages if r.status == "SKIPPED")
            sprint_result.summary = {
                "passed": passed, "blocked": blocked,
                "skipped": skipped, "total": len(sprint_result.stages),
            }

        await bus.emit(PhaseCompleted(phase="sprint", duration_s=round(time.time() - t0, 1)))

        # ── Phase 3: Wrap ──
        run_state.phase = "wrap"
        run_state.save()

        await bus.emit(PhaseStarted(phase="wrap"))
        t0 = time.time()

        # Final stack-specific review
        final_review = await run_final_review(stack, shared_ctx, bus, dispatcher)

        # Documentation updates (README, CLAUDE.md, CHANGELOG, session log, knowledge cards)
        await run_documenter(shared_ctx, final_review, bus, dispatcher)

        # Run validation (pure Python, zero LLM cost)
        from sdk.run_validator import validate_run
        validation_report = validate_run(
            events=bus.get_events(),
            sprint_result=sprint_result,
            stack=stack,
        )

        # Emit validation report as event for dashboard
        await bus.emit(RunValidation(validation=validation_report.to_dict()))

        await bus.emit(PhaseCompleted(phase="wrap", duration_s=round(time.time() - t0, 1)))

        result = OrchestrationResult(sprint=sprint_result, review=final_review, run_id=run_id, validation=validation_report)
        await bus.emit(RunCompleted(result_summary=result.to_json_output().get("summary")))

        # Clean up state file — run is complete, result file will be written by main()
        run_state.delete()

        return result

    except Exception as exc:
        await bus.emit(RunFailed(error=str(exc)))
        # State file is NOT deleted on failure — enables resume on next run
        raise
    finally:
        _release_lock(cwd)
        if emitter:
            # Give events time to flush
            await asyncio.sleep(0.5)
            await emitter.disconnect()


def _on_stage_complete(run_state: RunState, shared_ctx: SharedContext, stage_result: StageResult) -> None:
    """Callback: persist stage result + update shared context after each stage."""
    run_state.stages_completed.append({
        "name": stage_result.name,
        "status": stage_result.status,
        "contract": stage_result.contract,
        "test_result": stage_result.test_result,
        "codex_result": stage_result.codex_result,
        "runtime_result": stage_result.runtime_result,
        "fix_attempts": stage_result.fix_attempts,
        "unresolved": stage_result.unresolved,
        "recommendation": stage_result.recommendation,
    })
    run_state.save()

    # Append stage result to shared context — subsequent agents see what happened
    test_info = stage_result.test_result
    summary = (
        f"Status: {stage_result.status}\n"
        f"Tests: {test_info.get('passed', 0)} passed, {test_info.get('failed', 0)} failed\n"
        f"Fix attempts: {stage_result.fix_attempts}"
    )
    if stage_result.unresolved:
        summary += f"\nUnresolved: {', '.join(stage_result.unresolved[:3])}"
    shared_ctx.add(f"Stage Result: {stage_result.name}", summary)
    shared_ctx.save()


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

    # Write JSON result to .ai/runs/ for team-lead to read
    output = result.to_json_output()
    output_json = json.dumps(output, indent=2)

    runs_dir = Path(args.cwd) / ".ai" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    result_path = runs_dir / f"{result.run_id}.json"
    result_path.write_text(output_json)

    # Also print to stdout for convenience
    print(output_json)


if __name__ == "__main__":
    main()
