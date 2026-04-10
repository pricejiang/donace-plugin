"""Event types, EventBus, Checkpoints, and Decision enum for the donace orchestration SDK."""
from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable


# ---------------------------------------------------------------------------
# Decision enum
# ---------------------------------------------------------------------------

class Decision(str, Enum):
    CONTINUE = "continue"
    SKIP_STAGE = "skip_stage"
    SKIP_FIXES = "skip_fixes"
    ABORT_FIX_LOOP = "abort_fix_loop"
    SKIP_NEXT = "skip_next"
    STOP_LOOP = "stop_loop"
    ABORT = "abort"


# ---------------------------------------------------------------------------
# Checkpoint definitions
# ---------------------------------------------------------------------------

@dataclass
class Checkpoint:
    id: str
    description: str
    context: list[str]
    options: list[str]


CHECKPOINTS: dict[str, Checkpoint] = {
    "pre-implement": Checkpoint(
        id="pre-implement",
        description="Before implementation starts",
        context=["sprint contract", "stage plan"],
        options=["continue", "skip_stage"],
    ),
    "post-verify": Checkpoint(
        id="post-verify",
        description="After all verifiers complete",
        context=["test results", "codex review", "runtime eval"],
        options=["continue", "skip_fixes", "abort"],
    ),
    "pre-fix": Checkpoint(
        id="pre-fix",
        description="Before each fix attempt",
        context=["failure list", "attempt number", "previous fix diff"],
        options=["continue", "abort_fix_loop"],
    ),
    "stage-gate": Checkpoint(
        id="stage-gate",
        description="Stage complete, before next stage",
        context=["stage result", "remaining stages"],
        options=["continue", "skip_next", "stop_loop"],
    ),
}


# ---------------------------------------------------------------------------
# Stage / Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Stage:
    name: str
    has_user_facing_changes: bool


@dataclass
class StageResult:
    name: str
    status: str                          # "PASS", "BLOCKED", "SKIPPED"
    contract: str
    test_result: dict                    # {"passed": int, "failed": int}
    codex_result: dict                   # {"status": str, "p1_findings": int, "findings": list}
    runtime_result: dict | None          # None if skipped
    fix_attempts: int
    unresolved: list[str] | None = None  # None if all resolved
    recommendation: str | None = None    # "SKIP_ALLOWED" or "MUST_STOP", None if PASS


@dataclass
class SprintResult:
    stages: list[StageResult]
    warnings: list[str]
    summary: dict                        # {"passed": int, "blocked": int, "skipped": int, "total": int}


# ---------------------------------------------------------------------------
# Event base class
# ---------------------------------------------------------------------------

def _make_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> float:
    return time.time()


@dataclass
class Event:
    """Base event. All events carry these fields."""
    type: str = field(default="event")
    id: str = field(default_factory=_make_id)
    timestamp: float = field(default_factory=_now)
    run_id: str = field(default="")
    phase: str | None = field(default=None)
    stage: str | None = field(default=None)

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dict suitable for JSON serialisation."""
        d: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if isinstance(v, Enum):
                d[k] = v.value
            elif isinstance(v, list):
                d[k] = [
                    item.value if isinstance(item, Enum) else item for item in v
                ]
            else:
                d[k] = v
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, raw: str) -> Event:
        data = json.loads(raw)
        event_type = data.get("type", "event")
        klass = _EVENT_REGISTRY.get(event_type, Event)
        # Filter dict keys to only those the target class __init__ accepts
        sig = inspect.signature(klass)
        valid_keys = set(sig.parameters.keys())
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return klass(**filtered)


# ---------------------------------------------------------------------------
# Event registry — populated via _register() calls at module bottom
# ---------------------------------------------------------------------------
_EVENT_REGISTRY: dict[str, type[Event]] = {}


def _register(event_type: str, klass: type[Event]) -> None:
    _EVENT_REGISTRY[event_type] = klass


# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------

@dataclass
class RunStarted(Event):
    task: str = ""
    cwd: str = ""
    interactive: bool = False

    def __post_init__(self) -> None:
        self.type = "run.started"


@dataclass
class RunCompleted(Event):
    result_summary: dict | None = None

    def __post_init__(self) -> None:
        self.type = "run.completed"


@dataclass
class RunFailed(Event):
    error: str = ""

    def __post_init__(self) -> None:
        self.type = "run.failed"


@dataclass
class PhaseStarted(Event):
    # phase is already on Event base, but we override to make it required-ish
    def __post_init__(self) -> None:
        self.type = "phase.started"


@dataclass
class PhaseCompleted(Event):
    duration_s: float = 0.0

    def __post_init__(self) -> None:
        self.type = "phase.completed"


# ---------------------------------------------------------------------------
# Agent events
# ---------------------------------------------------------------------------

@dataclass
class AgentStarted(Event):
    agent: str = ""
    model: str | None = None
    prompt: str | None = None
    role: str | None = None

    def __post_init__(self) -> None:
        self.type = "agent.started"


@dataclass
class AgentCompleted(Event):
    agent: str = ""
    duration_s: float | None = None
    result_summary: str | None = None

    def __post_init__(self) -> None:
        self.type = "agent.completed"


@dataclass
class AgentFailed(Event):
    agent: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        self.type = "agent.failed"


@dataclass
class AgentSkipped(Event):
    agent: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        self.type = "agent.skipped"


# ---------------------------------------------------------------------------
# Sprint events (Phase 2)
# ---------------------------------------------------------------------------

@dataclass
class StageChanged(Event):
    stage_name: str = ""
    stage_index: int = 0
    total_stages: int = 0

    def __post_init__(self) -> None:
        self.type = "stage.changed"


@dataclass
class StageCompleted(Event):
    stage_name: str = ""
    status: str = ""  # "PASS", "BLOCKED", "SKIPPED"

    def __post_init__(self) -> None:
        self.type = "stage.completed"


@dataclass
class FixLoopStarted(Event):
    attempt: int = 0
    max_attempts: int = 3
    failures: list[str] | None = None

    def __post_init__(self) -> None:
        self.type = "fix_loop.started"


@dataclass
class FixLoopResolved(Event):
    attempt: int = 0

    def __post_init__(self) -> None:
        self.type = "fix_loop.resolved"


@dataclass
class FixLoopExhausted(Event):
    attempt: int = 0
    remaining_failures: list[str] | None = None

    def __post_init__(self) -> None:
        self.type = "fix_loop.exhausted"


@dataclass
class GateReached(Event):
    recommendation: str = ""  # "PASS", "SKIP_ALLOWED", "MUST_STOP"

    def __post_init__(self) -> None:
        self.type = "gate.reached"


# ---------------------------------------------------------------------------
# Checkpoint events
# ---------------------------------------------------------------------------

@dataclass
class CheckpointReached(Event):
    checkpoint_id: str = ""
    options: list[str] = field(default_factory=list)
    timeout_s: int = 30

    def __post_init__(self) -> None:
        self.type = "checkpoint.reached"


@dataclass
class CheckpointResolved(Event):
    checkpoint_id: str = ""
    decision: str = ""
    source: str = ""  # "user" or "timeout"

    def __post_init__(self) -> None:
        self.type = "checkpoint.resolved"


# ---------------------------------------------------------------------------
# Detail events (fine-grained)
# ---------------------------------------------------------------------------

@dataclass
class AgentMessage(Event):
    agent: str = ""
    role: str = ""
    content_preview: str = ""

    def __post_init__(self) -> None:
        self.type = "agent.message"


@dataclass
class AgentToolUse(Event):
    agent: str = ""
    tool: str = ""
    target: str | None = None
    input_preview: str | None = None

    def __post_init__(self) -> None:
        self.type = "agent.tool_use"


@dataclass
class AgentToolResult(Event):
    agent: str = ""
    tool: str = ""
    status: str = ""
    output_preview: str | None = None

    def __post_init__(self) -> None:
        self.type = "agent.tool_result"


@dataclass
class AgentTokens(Event):
    agent: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        self.type = "agent.tokens"


# ---------------------------------------------------------------------------
# Populate registry
# ---------------------------------------------------------------------------

_register("run.started", RunStarted)
_register("run.completed", RunCompleted)
_register("run.failed", RunFailed)
_register("phase.started", PhaseStarted)
_register("phase.completed", PhaseCompleted)
_register("agent.started", AgentStarted)
_register("agent.completed", AgentCompleted)
_register("agent.failed", AgentFailed)
_register("agent.skipped", AgentSkipped)
_register("stage.changed", StageChanged)
_register("stage.completed", StageCompleted)
_register("fix_loop.started", FixLoopStarted)
_register("fix_loop.resolved", FixLoopResolved)
_register("fix_loop.exhausted", FixLoopExhausted)
_register("gate.reached", GateReached)
_register("checkpoint.reached", CheckpointReached)
_register("checkpoint.resolved", CheckpointResolved)
_register("agent.message", AgentMessage)
_register("agent.tool_use", AgentToolUse)
_register("agent.tool_result", AgentToolResult)
_register("agent.tokens", AgentTokens)


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class EventBus:
    """Central event dispatcher. Lives in the orchestrator process."""

    def __init__(self, run_id: str, interactive: bool = False) -> None:
        self.run_id = run_id
        self.interactive = interactive
        self._subscribers: list[Callable[[Event], Awaitable[None]]] = []
        self._pending: dict[str, asyncio.Future[Decision]] = {}
        self._current_phase: str | None = None
        self._current_stage: str | None = None

    def subscribe(self, handler: Callable[[Event], Awaitable[None]]) -> None:
        """Register an event handler (e.g. WebSocketEmitter.__call__)."""
        self._subscribers.append(handler)

    async def emit(self, event: Event) -> None:
        """Broadcast to all subscribers. Fire-and-forget — never blocks the main loop."""
        # Stamp run_id
        event.run_id = self.run_id

        # Track phase/stage context from events
        if isinstance(event, PhaseStarted):
            self._current_phase = event.phase
            self._current_stage = None  # reset stage on new phase
        elif isinstance(event, StageChanged):
            self._current_stage = event.stage_name

        # Set phase/stage on event if not already set
        if event.phase is None:
            event.phase = self._current_phase
        if event.stage is None:
            event.stage = self._current_stage

        # Fan out
        for handler in self._subscribers:
            asyncio.create_task(handler(event))

    async def wait_for_decision(self, checkpoint: str) -> Decision:
        """
        Pause point for interactive control.

        - interactive=False → returns CONTINUE immediately (zero overhead)
        - interactive=True  → emits checkpoint.reached, waits with timeout
        """
        if not self.interactive:
            return Decision.CONTINUE

        future: asyncio.Future[Decision] = asyncio.get_running_loop().create_future()
        self._pending[checkpoint] = future

        await self.emit(CheckpointReached(
            checkpoint_id=checkpoint,
            options=CHECKPOINTS[checkpoint].options if checkpoint in CHECKPOINTS else ["continue"],
            timeout_s=30,
        ))

        source = "timeout"
        decision = Decision.CONTINUE
        try:
            decision = await asyncio.wait_for(future, timeout=30.0)
            source = "user"
        except asyncio.TimeoutError:
            decision = Decision.CONTINUE
            source = "timeout"
        finally:
            self._pending.pop(checkpoint, None)

        await self.emit(CheckpointResolved(
            checkpoint_id=checkpoint,
            decision=decision.value if isinstance(decision, Decision) else str(decision),
            source=source,
        ))
        return decision

    def resolve(self, checkpoint: str, decision: Decision) -> None:
        """Called when dashboard relays a user action."""
        if checkpoint in self._pending and not self._pending[checkpoint].done():
            self._pending[checkpoint].set_result(decision)


# ---------------------------------------------------------------------------
# TaskClass (used by orchestrator for task classification)
# ---------------------------------------------------------------------------

@dataclass
class TaskClass:
    needs_spec: bool
    needs_plan: bool
    reason: str = ""

    @classmethod
    def from_response(cls, response: dict) -> TaskClass:
        """Parse from Haiku tool_use response."""
        # Extract from tool_use block
        if isinstance(response, dict):
            return cls(
                needs_spec=response.get("needs_spec", True),
                needs_plan=response.get("needs_plan", True),
                reason=response.get("reason", ""),
            )
        return cls(needs_spec=True, needs_plan=True, reason="default")


# ---------------------------------------------------------------------------
# OrchestrationResult (final output)
# ---------------------------------------------------------------------------

@dataclass
class OrchestrationResult:
    sprint: SprintResult
    review: str = ""
    run_id: str = ""

    def to_json_output(self) -> dict:
        """Produce the JSON output dict for stdout (team-lead reads this)."""
        stages = []
        for sr in self.sprint.stages:
            stage_dict: dict[str, Any] = {
                "name": sr.name,
                "status": sr.status,
                "contract": sr.contract,
                "test_result": sr.test_result,
                "codex_result": sr.codex_result,
                "fix_attempts": sr.fix_attempts,
            }
            if sr.runtime_result is not None:
                stage_dict["runtime_result"] = sr.runtime_result
            if sr.unresolved:
                stage_dict["unresolved"] = sr.unresolved
            if sr.recommendation:
                stage_dict["recommendation"] = sr.recommendation
            stages.append(stage_dict)

        return {
            "run_id": self.run_id,
            "stages": stages,
            "warnings": self.sprint.warnings,
            "summary": self.sprint.summary,
            "review": self.review,
        }
