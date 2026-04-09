# Orchestration Design

## Problem

`team-lead` agent's pipeline is controlled by LLM reading markdown instructions. Steps like codex review, test-engineer, and runtime-evaluator are frequently skipped because LLM "decides" they aren't needed. This is a reliability problem — prompt-based flow control is inherently "advisory", not "mandatory".

## Solution

Move all orchestration (Phase 0-3) from LLM to Python code using Claude Agent SDK. Agent definitions stay as markdown. LLM agents do the thinking within each dispatch, but cannot skip or reorder steps.

A real-time web dashboard provides full pipeline visibility, run history, and interactive control points during the sprint loop.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Scope | Full pipeline (Phase 0-3) | All agent activity visible and enforced, not just sprint loop |
| Granularity | Coarse + fine (expandable) | Default: agent lifecycle + phase/stage progress. Expand for tool calls, prompts, token usage |
| Interactivity | Phase 0/1 read-only, Phase 2 interactive, Phase 3 read-only | Control points at sprint loop junctions — skip stage, abort fix loop, override gate |
| Process model | Dashboard is an independent long-lived process | Survives orchestrator crashes, supports replay, keeps history across MUST_STOP restarts |
| Co-design | EventBus designed alongside orchestrator | Avoids bolting on observability after the fact. No rewrite needed for interactivity |
| Performance impact | Negligible | emit() < 0.1ms vs LLM query 10-60s. WebSocket push is fire-and-forget |
| Task classification | LLM (haiku) structured output | Keyword matching can't handle gray areas ("login is slow" might need spec). One call, < $0.001, < 1s |

## Architecture

```
User
  │
  ▼
team-lead.md (Claude Code LLM)
  │  1. Understand user's request
  │  2. Bash: python sdk/dashboard.py &                    ← start dashboard (if not running)
  │  3. Bash: python sdk/orchestrator.py \                 ← start orchestrator
  │       --task "..." --cwd ... --dashboard-url ws://localhost:8741
  │  4. Read JSON result from stdout
  │  5. Report to user
  │  6. (if MUST_STOP: discuss with user, re-run orchestrator)
  │  7. Bash: curl -s localhost:8741/api/shutdown           ← shut down dashboard when done
  │
  ▼
orchestrator.py (Agent SDK, Python)
  │
  │  bus = EventBus(interactive=True)
  │  bus.subscribe(WebSocketEmitter("ws://localhost:8741"))
  │
  │  Phase 0: Boot ─────── bus.emit(phase/agent events)
  │  Phase 1: Plan ─────── bus.emit(phase/agent events)
  │  Phase 2: Sprint Loop ─ bus.emit(stage/agent/checkpoint events)
  │  Phase 3: Wrap ──────── bus.emit(phase/agent events)
  │
  │  stdout: JSON result
  │
  ▼
dashboard.py (independent process, FastAPI)
  │
  │  /api/ingest   orchestrator ──► dashboard  (events, one-way)
  │  /api/control  dashboard ──► orchestrator  (decisions, one-way)
  │  /ws          ◄──► Browser (static/index.html)
  │  /api/shutdown     graceful stop
  │
  │  EventStore: in-memory, grouped by run_id
  │  Survives orchestrator crash — all events up to crash are preserved
  │
  ▼
Browser (http://localhost:8741)
  │
  └─ Full pipeline visibility: phases, agents, stages, checkpoints
  └─ Interactive controls during Phase 2 sprint loop
  └─ Run history: switch between current and past runs
```

## Process Lifecycle

```
team-lead starts dashboard.py ─────────────────────────────── team-lead shuts it down
      │                                                              │
      ├── orchestrator run #1 connects ── pushes events ── disconnects (done or MUST_STOP)
      │
      ├── (user discusses MUST_STOP with team-lead)
      │
      ├── orchestrator run #2 connects ── pushes events ── disconnects
      │
      └── all events from both runs preserved in dashboard ──────────┘
```

| Scenario | Behavior |
|----------|----------|
| Orchestrator completes normally | Events preserved, dashboard stays up for review |
| Orchestrator crashes | Events up to crash preserved, dashboard shows error state |
| MUST_STOP → user re-runs | New run appears in dashboard, previous run still viewable |
| Browser refresh / late join | Full event replay from EventStore |
| Multiple projects | Each gets its own dashboard instance (separate ports) |

## Role Changes

### team-lead.md (simplified to thin launcher + reporter)

```markdown
# Team Lead

You receive tasks from the user and delegate to the SDK orchestrator.

## Steps

1. Summarize the user's request into a clear task description
2. Start the dashboard (if not already running):
   Bash: python sdk/dashboard.py --port 8741 &
3. Run the orchestrator:
   Bash: python sdk/orchestrator.py \
     --task "<task description>" \
     --cwd <project root> \
     --dashboard-url ws://localhost:8741
4. Read the JSON result from stdout
5. If MUST_STOP: present blockers to user, discuss, optionally re-run step 3
6. Present final report: what was built, what passed, what blocked
7. Shut down dashboard:
   Bash: curl -s localhost:8741/api/shutdown
```

### orchestrator.py (replaces team-lead's Phase 0-3 logic)

All orchestration logic moves to Python. Agent markdown files remain as system prompts.

| team-lead.md (before) | orchestrator.py (after) |
|---|---|
| Phase 0: Boot — read sessions, load cards, detect resume | `phase_boot()` — same logic, deterministic |
| Phase 1: Plan — detect stack, dispatch planner/architect | `phase_plan()` — same dispatch, cannot skip steps |
| Phase 2: Sprint loop — dispatch implementer/verifiers/fix | `phase_sprint()` — enforced execution, parallel verify |
| Phase 3: Wrap — final review, session log, knowledge cards | `phase_wrap()` — same dispatch, cannot skip |

Key difference: **Python controls dispatch order. LLM agents do the thinking within each dispatch, but cannot skip or reorder steps.**

## File Structure

```
donace/
  agents/
    team-lead.md          # Simplified to thin launcher
    ...other agents unchanged...
  sdk/
    orchestrator.py       # Full pipeline: Phase 0-3
    sprint_loop.py        # Phase 2 inner loop (called by orchestrator)
    events.py             # Event types, EventBus, Checkpoints, Decision enum
    dashboard.py          # Independent WebSocket server + static file serving
    emitter.py            # WebSocketEmitter — pushes events from orchestrator to dashboard
    static/
      index.html          # Single-file dashboard UI (HTML + CSS + JS)
    requirements.txt      # claude-agent-sdk, fastapi, uvicorn, websockets
```

## Task Classification

Before entering Phase 1, the orchestrator classifies the task using a lightweight LLM call to determine which steps are needed:

```python
async def classify_task(task: str) -> TaskClass:
    response = await client.messages.create(
        model="haiku",  # cheapest, classification is trivial
        messages=[{"role": "user", "content": task}],
        system="Classify this task. Return JSON.",
        tools=[{
            "name": "classify",
            "input_schema": {
                "type": "object",
                "properties": {
                    "needs_spec": {
                        "type": "boolean",
                        "description": "True if this is a new feature/product that needs a spec. False for bug fixes, refactors, single-file changes."
                    },
                    "needs_plan": {
                        "type": "boolean",
                        "description": "True unless this is a trivial single-line fix."
                    },
                    "reason": {"type": "string"}
                }
            }
        }]
    )
    return TaskClass.from_response(response)
```

Cost: < $0.001, latency: < 1s. Worth using LLM over keyword matching because gray areas are common ("login is slow" might need a spec if it involves architectural changes).

## Orchestrator: Full Pipeline

```python
async def run(task: str, cwd: str, bus: EventBus) -> OrchestrationResult:
    await bus.emit(RunStarted(task=task, cwd=cwd))

    # ── Phase 0: Boot ──
    await bus.emit(PhaseStarted(phase="boot"))
    session_context = await load_session_context(cwd)   # reads .ai/sessions/, .ai/cards/
    resume_plan = check_for_resume(cwd)                 # checks .ai/plans/current-plan.md
    stack = detect_stack(cwd)                           # always needed for Phase 3 reviewer selection
    await bus.emit(PhaseCompleted(phase="boot"))

    # ── Phase 1: Plan (skip if resuming) ──
    if not resume_plan:
        await bus.emit(PhaseStarted(phase="plan"))

        # Task classification (lightweight LLM call)
        task_class = await classify_task(task)

        # Planner (skip for bug fixes / refactors)
        if task_class.needs_spec:
            await bus.emit(AgentStarted(agent="planner", model="opus"))
            spec = await query(agent="planner", prompt=task)
            await bus.emit(AgentCompleted(agent="planner"))
        else:
            spec = task

        # Architect (skip for trivial single-line fixes)
        if task_class.needs_plan:
            await bus.emit(AgentStarted(agent="architect", model="opus"))
            plan = await query(agent="architect", prompt=spec)
            await bus.emit(AgentCompleted(agent="architect"))

            # Codex plan review (mandatory when plan exists)
            await bus.emit(AgentStarted(agent="codex-plan-review"))
            review = await run_codex_plan_review(plan)
            await bus.emit(AgentCompleted(agent="codex-plan-review"))

            # Revision loop if major issues
            if review.has_major_issues:
                await bus.emit(AgentStarted(agent="architect", prompt="Revise plan"))
                plan = await query(agent="architect", prompt=f"Revise based on: {review}")
                await bus.emit(AgentCompleted(agent="architect"))
        else:
            plan = make_trivial_plan(task)  # single-stage plan

        await bus.emit(PhaseCompleted(phase="plan"))
    else:
        plan = resume_plan

    # ── Phase 2: Sprint Loop ──
    await bus.emit(PhaseStarted(phase="sprint"))
    sprint_result = await run_sprint_loop(plan, cwd, bus)
    await bus.emit(PhaseCompleted(phase="sprint"))

    # ── Phase 3: Wrap ──
    await bus.emit(PhaseStarted(phase="wrap"))

    # Final stack-specific review
    reviewer = f"{stack}-reviewer"  # "typescript-reviewer" or "ios-reviewer"
    await bus.emit(AgentStarted(agent=reviewer, model="opus"))
    final_review = await query(agent=reviewer, prompt="Full codebase review of all changes...")
    await bus.emit(AgentCompleted(agent=reviewer))

    # Session log + knowledge cards
    await write_session_log(cwd, sprint_result, final_review)
    await bus.emit(PhaseCompleted(phase="wrap"))

    await bus.emit(RunCompleted(result_summary=sprint_result.summary))
    return OrchestrationResult(sprint=sprint_result, review=final_review)
```

### JSON Output (stdout → team-lead)

orchestrator.py prints a JSON result to stdout for team-lead to read:

```json
{
  "stages": [
    {
      "name": "Stage 1: WebSocket Server",
      "status": "PASS",
      "contract": "...",
      "test_result": { "passed": 12, "failed": 0 },
      "codex_result": { "status": "completed", "p1_findings": 0, "findings": [] },
      "runtime_result": { "status": "PASS", "score": "5/5" },
      "fix_attempts": 0
    },
    {
      "name": "Stage 2: Broadcasting",
      "status": "BLOCKED",
      "contract": "...",
      "test_result": { "passed": 8, "failed": 2 },
      "codex_result": { "status": "completed", "p1_findings": 1, "findings": ["..."] },
      "runtime_result": { "status": "FAIL", "score": "3/5" },
      "fix_attempts": 3,
      "unresolved": ["test: broadcast to disconnected client throws unhandled error"],
      "recommendation": "MUST_STOP"
    }
  ],
  "warnings": ["Codex review SKIPPED for Stage 3 — CLI not available"],
  "summary": { "passed": 1, "blocked": 1, "skipped": 0, "total": 2 }
}
```

### SprintResult structure

```python
@dataclass
class StageResult:
    name: str
    status: str                          # "PASS", "BLOCKED", "SKIPPED"
    contract: str
    test_result: dict                    # {"passed": int, "failed": int}
    codex_result: dict                   # {"status": str, "p1_findings": int, "findings": list}
    runtime_result: dict | None          # None if skipped (no user-facing changes)
    fix_attempts: int
    unresolved: list[str] | None         # None if all resolved
    recommendation: str | None           # "SKIP_ALLOWED" or "MUST_STOP", None if PASS

@dataclass
class SprintResult:
    stages: list[StageResult]
    warnings: list[str]                  # skipped steps, degraded services
    summary: dict                        # {"passed": int, "blocked": int, "skipped": int, "total": int}
```

## Sprint Loop (Phase 2)

### Per-stage flow

```python
for stage in plan.stages:
    await bus.emit(StageChanged(stage_name=stage.name))

    # 1. Sprint contract (mandatory)
    await bus.emit(AgentStarted(agent="runtime-evaluator", role="contract"))
    contract = await query(agent="runtime-evaluator", prompt=f"Write sprint contract for: {stage}")
    await bus.emit(AgentCompleted(agent="runtime-evaluator"))

    # Checkpoint: user can skip this stage before implementation starts
    decision = await bus.wait_for_decision("pre-implement")
    if decision == Decision.SKIP_STAGE:
        await bus.emit(StageCompleted(stage_name=stage.name, status="SKIPPED"))
        continue

    # 2. Implement (mandatory)
    await bus.emit(AgentStarted(agent="implementer", model="sonnet"))
    impl_result = await query(agent="implementer", prompt=f"Implement: {stage}\nContract: {contract}")
    await bus.emit(AgentCompleted(agent="implementer"))

    # 3. Verify (parallel, test + codex mandatory, runtime conditional)
    verify_tasks = [
        run_test_engineer(stage),
        run_codex_review()
    ]
    if stage.has_user_facing_changes:
        verify_tasks.append(run_runtime_evaluator(contract))
    else:
        await bus.emit(Event(type="agent.skipped", agent="runtime-evaluator",
                             reason="no user-facing changes"))

    results = await asyncio.gather(*verify_tasks, return_exceptions=True)
    test_result, codex_result = results[0], results[1]
    runtime_result = results[2] if len(results) > 2 else None

    # Checkpoint: user can intervene after seeing verification results
    decision = await bus.wait_for_decision("post-verify")
    if decision == Decision.SKIP_FIXES:
        await bus.emit(StageCompleted(stage_name=stage.name, status="PASS"))
        continue

    # 4. Fix loop (max 3 attempts)
    failures = collect_failures(test_result, codex_result, runtime_result)
    for attempt in range(3):
        if not failures:
            break

        await bus.emit(FixLoopStarted(attempt=attempt + 1, failures=failures))

        decision = await bus.wait_for_decision("pre-fix")
        if decision == Decision.ABORT_FIX_LOOP:
            break

        fix_result = await query(agent="implementer", prompt=f"Fix: {failures}")

        # Re-verify
        test_result, codex_result = await asyncio.gather(
            run_test_engineer(stage),
            run_codex_review()
        )
        failures = collect_failures(test_result, codex_result, runtime_result)

    if not failures:
        await bus.emit(FixLoopResolved(attempt=attempt + 1))

    # 5. Gate check
    if failures:
        if all(f.severity == "warning" for f in failures):
            recommendation = "SKIP_ALLOWED"
        else:
            recommendation = "MUST_STOP"

        await bus.emit(GateReached(recommendation=recommendation))

        decision = await bus.wait_for_decision("stage-gate")
        if recommendation == "MUST_STOP" and decision != Decision.CONTINUE:
            await bus.emit(StageCompleted(stage_name=stage.name, status="BLOCKED"))
            break
    else:
        await bus.emit(StageCompleted(stage_name=stage.name, status="PASS"))
```

### Codex review handling

Codex review is mandatory, but the CLI may be unavailable. Handle gracefully:

| Situation | Action |
|-----------|--------|
| CLI not installed / auth failed | Pre-check at startup. If unavailable, warn but continue. Record `SKIPPED` in output |
| Timeout / network error | Retry once. Still fails → record `SKIPPED` with reason |
| Returns empty | Treat as PASS (no findings) |
| Returns P1 findings | Add to failure list for fix loop |

The key guarantee: **skipped steps are never silent**. They appear in events, in the dashboard, and in the JSON output.

### BLOCKED handling

When fix loop exhausts 3 attempts:

| Failure severity | Recommendation | Behavior |
|-----------------|----------------|----------|
| All warnings | `SKIP_ALLOWED` | Dashboard shows option to continue. Default: continue on timeout |
| Any critical/error | `MUST_STOP` | Sprint loop stops. Returns to team-lead LLM with failure details |

`MUST_STOP` means orchestrator.py stops iterating stages and returns. team-lead LLM cannot pretend it passed — the JSON output makes the failure explicit.

### runtime-evaluator handling

runtime-evaluator has two distinct roles:

| Role | When | Value |
|------|------|-------|
| Sprint contract (pre-sprint) | Every stage | High — defines what "done" means for implementer and test-engineer |
| Runtime verification (post-sprint) | Only stages with user-facing changes | Conditional — catches "tests pass but app doesn't start" |

The contract role always runs. The verification role is controlled by stage metadata:

```python
@dataclass
class Stage:
    name: str
    has_user_facing_changes: bool  # architect annotates this when generating the plan
```

When skipped, the event `agent.skipped` with reason is emitted — visible in dashboard and JSON output.

## Event Schema

All events inherit from a base type. Serialized as JSON over WebSocket.

### Base

```python
@dataclass
class Event:
    id: str                # uuid4
    timestamp: float       # time.time()
    type: str              # dotted event name
    run_id: str            # groups events per orchestrator invocation
    phase: str | None      # "boot", "plan", "sprint", "wrap"
    stage: str | None      # current stage name (Phase 2 only)
```

### Phase Events (full pipeline)

| Event | Fields | When |
|-------|--------|------|
| `run.started` | `run_id`, `task`, `cwd`, `interactive` | Orchestrator begins |
| `run.completed` | `run_id`, `result_summary` | Orchestrator ends |
| `run.failed` | `run_id`, `error` | Orchestrator crashed |
| `phase.started` | `phase: boot\|plan\|sprint\|wrap` | Phase begins |
| `phase.completed` | `phase`, `duration_s` | Phase ends |

### Agent Events (all phases)

| Event | Fields | When |
|-------|--------|------|
| `agent.started` | `agent`, `prompt`, `model` | Agent dispatched |
| `agent.completed` | `agent`, `duration_s`, `result_summary` | Agent returns |
| `agent.failed` | `agent`, `error` | Agent errored |
| `agent.skipped` | `agent`, `reason` | Agent intentionally not run |

### Sprint Events (Phase 2 only)

| Event | Fields | When |
|-------|--------|------|
| `stage.changed` | `stage_name`, `stage_index`, `total_stages` | New stage begins |
| `stage.completed` | `stage_name`, `status: PASS\|BLOCKED\|SKIPPED` | Stage ends |
| `fix_loop.started` | `attempt: int`, `max_attempts: 3`, `failures: list[str]` | Fix cycle begins |
| `fix_loop.resolved` | `attempt` | All failures fixed |
| `fix_loop.exhausted` | `attempt`, `remaining_failures` | Max attempts hit |
| `gate.reached` | `recommendation: PASS\|SKIP_ALLOWED\|MUST_STOP` | Stage gate check |

### Checkpoint Events (Phase 2 interactive)

| Event | Fields | When |
|-------|--------|------|
| `checkpoint.reached` | `checkpoint_id`, `options: list[str]`, `timeout_s` | Awaiting user decision |
| `checkpoint.resolved` | `checkpoint_id`, `decision`, `source: user\|timeout` | Decision made |

### Detail Events (fine-grained — visible on expand, all phases)

| Event | Fields | When |
|-------|--------|------|
| `agent.message` | `agent`, `role`, `content_preview` | Each LLM message turn |
| `agent.tool_use` | `agent`, `tool`, `target`, `input_preview` | Agent calls a tool |
| `agent.tool_result` | `agent`, `tool`, `status`, `output_preview` | Tool returns |
| `agent.tokens` | `agent`, `input_tokens`, `output_tokens` | Per-turn token usage |

## EventBus

Lives in orchestrator.py process. Emits events locally and pushes them to dashboard via WebSocket.

```python
class EventBus:
    def __init__(self, run_id: str, interactive: bool = False):
        self.run_id = run_id
        self.interactive = interactive
        self._subscribers: list[Callable[[Event], Awaitable[None]]] = []
        self._pending: dict[str, asyncio.Future] = {}

    def subscribe(self, handler: Callable[[Event], Awaitable[None]]):
        self._subscribers.append(handler)

    async def emit(self, event: Event):
        """Broadcast to all subscribers. Fire-and-forget, never blocks the main loop."""
        event.run_id = self.run_id
        for handler in self._subscribers:
            asyncio.create_task(handler(event))

    async def wait_for_decision(self, checkpoint: str) -> Decision:
        """
        Pause point for interactive control.
        - interactive=False: returns CONTINUE immediately (zero overhead)
        - interactive=True: emits checkpoint.reached, waits with timeout
        """
        if not self.interactive:
            return Decision.CONTINUE

        future = asyncio.get_event_loop().create_future()
        self._pending[checkpoint] = future

        await self.emit(CheckpointReached(
            checkpoint_id=checkpoint,
            options=CHECKPOINTS[checkpoint].options,
            timeout_s=30
        ))

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
            decision=decision,
            source=source
        ))
        return decision

    def resolve(self, checkpoint: str, decision: Decision):
        """Called when dashboard relays a user action."""
        if checkpoint in self._pending:
            self._pending[checkpoint].set_result(decision)
```

## Communication Protocol

Two separate WebSocket channels between orchestrator and dashboard:

```
orchestrator ──ws──► /api/ingest   ──► dashboard  (events in, one-way)
orchestrator ◄──ws── /api/control  ◄── dashboard  (decisions back, one-way)

dashboard ◄──ws──► /ws ◄──► browser (bidirectional: events out, decisions in)
```

| Channel | Direction | Payload |
|---------|-----------|---------|
| `/api/ingest` | orchestrator → dashboard | `Event` JSON objects (one-way push) |
| `/api/control` | dashboard → orchestrator | `{"action": "resolve", "checkpoint": "...", "decision": "..."}` (one-way push) |
| `/ws` | dashboard ↔ browser | Events to browser, decisions from browser |

### WebSocket Emitter (emitter.py)

Runs inside orchestrator.py. Two connections: one for pushing events, one for receiving decisions.

```python
class WebSocketEmitter:
    """EventBus subscriber that forwards events to dashboard via WebSocket."""

    def __init__(self, dashboard_url: str, bus: EventBus):
        self.dashboard_url = dashboard_url
        self.bus = bus
        self._ingest_ws: WebSocket | None = None
        self._control_ws: WebSocket | None = None

    async def connect(self):
        """Connect both channels. If dashboard not running, continue without it."""
        try:
            self._ingest_ws = await websockets.connect(f"{self.dashboard_url}/api/ingest")
            self._control_ws = await websockets.connect(f"{self.dashboard_url}/api/control")
            asyncio.create_task(self._listen_controls())
        except ConnectionRefusedError:
            print("Warning: Dashboard not running, events will not be streamed")

    async def __call__(self, event: Event):
        """EventBus subscriber interface. Non-blocking push."""
        if self._ingest_ws:
            try:
                await self._ingest_ws.send(event.to_json())
            except ConnectionClosed:
                self._ingest_ws = None

    async def _listen_controls(self):
        """Receive user decisions from dashboard and resolve checkpoints."""
        try:
            async for msg in self._control_ws:
                data = json.loads(msg)
                if data.get("action") == "resolve":
                    self.bus.resolve(data["checkpoint"], Decision(data["decision"]))
        except ConnectionClosed:
            pass
```

### Dashboard Server (dashboard.py)

Independent process. Receives events from orchestrator, serves UI to browser, relays user decisions back.

```python
app = FastAPI()

class EventStore:
    """In-memory event storage, grouped by run_id."""
    def __init__(self):
        self.runs: dict[str, list[Event]] = {}

    def append(self, event: Event):
        self.runs.setdefault(event.run_id, []).append(event)

    def get_run(self, run_id: str) -> list[Event]:
        return self.runs.get(run_id, [])

    def list_runs(self) -> list[dict]:
        return [
            {"run_id": rid, "event_count": len(evts), "started": evts[0].timestamp}
            for rid, evts in self.runs.items()
        ]

store = EventStore()
browser_connections: set[WebSocket] = set()
orchestrator_control_ws: set[WebSocket] = set()


# --- Orchestrator → Dashboard (event ingestion, one-way) ---

@app.websocket("/api/ingest")
async def ingest(ws: WebSocket):
    """Orchestrator pushes events here."""
    await ws.accept()
    try:
        async for raw in ws.iter_text():
            event = Event.from_json(raw)
            store.append(event)
            for browser_ws in browser_connections.copy():
                try:
                    await browser_ws.send_text(raw)
                except:
                    browser_connections.discard(browser_ws)
    except WebSocketDisconnect:
        pass


# --- Dashboard → Orchestrator (user decisions, one-way) ---

@app.websocket("/api/control")
async def control(ws: WebSocket):
    """Orchestrator listens here for user decisions."""
    await ws.accept()
    orchestrator_control_ws.add(ws)
    try:
        await ws.receive()  # blocks until disconnect
    except WebSocketDisconnect:
        orchestrator_control_ws.discard(ws)


# --- Browser ↔ Dashboard ---

@app.websocket("/ws")
async def browser_ws(ws: WebSocket):
    """Browser connects here to view events and send decisions."""
    await ws.accept()
    browser_connections.add(ws)

    # Replay all events for late-joining browsers
    for run_id, events in store.runs.items():
        for event in events:
            await ws.send_text(event.to_json())

    try:
        async for raw in ws.iter_text():
            msg = json.loads(raw)
            # Relay user decisions to orchestrator
            if msg.get("action") == "resolve":
                for orch_ws in orchestrator_control_ws:
                    await orch_ws.send_text(raw)
    except WebSocketDisconnect:
        browser_connections.discard(ws)


# --- REST ---

@app.get("/")
async def index():
    return FileResponse("sdk/static/index.html")

@app.get("/api/runs")
async def list_runs():
    return store.list_runs()

@app.post("/api/shutdown")
async def shutdown():
    """Called by team-lead when session is over."""
    asyncio.get_event_loop().call_later(0.5, lambda: os._exit(0))
    return {"status": "shutting_down"}
```

## Control Points

Checkpoints exist in Phase 2 only. Phase 0/1/3 are read-only — events stream but no pause points.

```python
@dataclass
class Checkpoint:
    id: str
    description: str
    context: list[str]
    options: list[str]

CHECKPOINTS = {
    "pre-implement": Checkpoint(
        id="pre-implement",
        description="Before implementation starts",
        context=["sprint contract", "stage plan"],
        options=["continue", "skip_stage"]
    ),
    "post-verify": Checkpoint(
        id="post-verify",
        description="After all verifiers complete",
        context=["test results", "codex review", "runtime eval"],
        options=["continue", "skip_fixes", "abort"]
    ),
    "pre-fix": Checkpoint(
        id="pre-fix",
        description="Before each fix attempt",
        context=["failure list", "attempt number", "previous fix diff"],
        options=["continue", "abort_fix_loop"]
    ),
    "stage-gate": Checkpoint(
        id="stage-gate",
        description="Stage complete, before next stage",
        context=["stage result", "remaining stages"],
        options=["continue", "skip_next", "stop_loop"]
    ),
}

class Decision(str, Enum):
    CONTINUE = "continue"
    SKIP_STAGE = "skip_stage"
    SKIP_FIXES = "skip_fixes"
    ABORT_FIX_LOOP = "abort_fix_loop"
    SKIP_NEXT = "skip_next"
    STOP_LOOP = "stop_loop"
    ABORT = "abort"
```

## Dashboard UI (static/index.html)

Single HTML file. No build step, no npm, no framework.

### Layout

```
┌──────────────────────────────────────────────────────────────────┐
│  donace dashboard                    run-abc123  ● Connected     │
├────────────────┬─────────────────────────────────────────────────┤
│                │                                                 │
│  PIPELINE      │  AGENT ACTIVITY                                │
│                │                                                 │
│  ● Boot     ✓  │  ┌──────────────────────────────────────┐      │
│  ● Plan     ✓  │  │ ● implementer            running 34s │      │
│    planner  ✓  │  │   Editing src/server.ts:42           │      │
│    architect ✓ │  │   ▸ prompt                           │      │
│    codex rev ✓ │  │   ▸ tool calls (7)                   │      │
│  ● Sprint   ●  │  ├──────────────────────────────────────┤      │
│    Stage 1  ✓  │  │ ● test-engineer          running 12s │      │
│    Stage 2  ●  │  │   Running npm test...                │      │
│    Stage 3  ○  │  ├──────────────────────────────────────┤      │
│  ○ Wrap        │  │ ○ codex-review            waiting    │      │
│                │  └──────────────────────────────────────┘      │
│  FIX LOOP      │                                                 │
│  Attempt 1/3   │  EVENT LOG                                     │
│  ■■□           │  14:23:01  agent.tool_use  Edit server.ts     │
│                │  14:22:58  agent.started   implementer         │
│  CHECKPOINT    │  14:22:45  agent.completed architect           │
│  post-verify   │  14:22:30  phase.started  plan                │
│  [Continue]    │  14:22:01  run.started    "Add WebSocket..."  │
│  [Skip fixes]  │  ...                                           │
│  [Abort]       │                                                 │
│                │  RUNS  [run-abc123 ●] [run-def456 ✓]          │
└────────────────┴─────────────────────────────────────────────────┘
```

### Panels

**Left Panel — Pipeline State**

| Section | Content |
|---------|---------|
| Pipeline | Phase list (Boot/Plan/Sprint/Wrap) with nested agent/stage items. Status: done, active, pending |
| Fix Loop | Visible during Phase 2 fix cycles. Shows attempt count and progress |
| Checkpoint | Action buttons during Phase 2 interactive checkpoints. 30s countdown timer. Hidden otherwise |

**Right Panel — Activity**

| Section | Content |
|---------|---------|
| Agent Cards | One card per active/recent agent. Name, status, elapsed time, last action. Click to expand: full prompt, tool call log, token usage |
| Event Log | Reverse-chronological stream. Color-coded by type. Click to expand details |
| Run Tabs | Switch between current and past runs (all preserved in EventStore) |

### Interaction Flow (Phase 2 only)

1. Sprint loop hits `wait_for_decision("post-verify")`
2. Orchestrator emits `checkpoint.reached` → dashboard → browser
3. Dashboard shows buttons + 30s countdown in left panel
4. User clicks **[Skip fixes]**
5. Browser sends `{"action": "resolve", "checkpoint": "post-verify", "decision": "skip_fixes"}`
6. Dashboard relays to orchestrator via `/api/control`
7. EventBus resolves the future, sprint loop continues
8. `checkpoint.resolved` event streams to browser, buttons disappear

Auto-continue on timeout. Phase 0/1/3: no buttons, events stream read-only.

### Styling

- Dark theme (matches terminal aesthetic)
- Monospace font for logs and prompts
- Status colors: green (pass/running), yellow (waiting/warning), red (failed/blocked), blue (checkpoint)
- Minimal CSS, no animations beyond a subtle pulse on active agents

## Agent Model Assignments

| Agent | Model | Rationale |
|-------|-------|-----------|
| planner | opus | Needs deep product thinking |
| architect | opus | Needs codebase understanding and design judgment |
| runtime-evaluator | opus | Needs judgment for contract negotiation and verification |
| implementer | sonnet | Code writing — fast and capable enough |
| test-engineer | sonnet | Test writing — fast and capable enough |
| typescript-reviewer | opus | Deep review needs strongest model |
| ios-reviewer | opus | Deep review needs strongest model |
| codex review | codex (external) | Independent cross-model review |
| task classification | haiku | Trivial classification, cheapest possible |

## Launch Modes

```bash
# Full interactive mode (team-lead runs these)
python sdk/dashboard.py --port 8741 &
python sdk/orchestrator.py --task "..." --cwd ... --dashboard-url ws://localhost:8741

# Read-only monitoring (no checkpoint pauses)
python sdk/orchestrator.py --task "..." --cwd ... --dashboard-url ws://localhost:8741 --no-interactive

# Headless / CI (no dashboard at all)
python sdk/orchestrator.py --task "..." --cwd ...

# Dashboard only (view past runs, no active orchestrator)
python sdk/dashboard.py --port 8741
```

## Event Replay & Late Join

When a browser connects (or reconnects after refresh):

1. Dashboard sends full event history for all runs from EventStore
2. Browser replays events in order to rebuild current state
3. Subsequent events arrive live via WebSocket

No separate "get current state" API needed. State is derived from the event stream. Single source of truth.

## Open Questions

1. **Port selection** — hardcode 8741 or find an available port? Hardcode is predictable; auto-port needs a way to pass the URL to team-lead.

2. **Event persistence** — write events to `.ai/sessions/events.jsonl` for post-mortem? EventStore is in-memory, lost when dashboard exits. A `FileLogger` subscriber could persist to disk cheaply.

3. **Browser auto-open** — should dashboard.py auto-open the browser on start? Could be a `--open` flag.

4. **Dashboard discovery** — team-lead needs to know if dashboard is already running before starting a new one. Options: check port with `curl`, PID file, or let it fail with "address in use".

5. **Agent definition loading** — should orchestrator.py read the markdown agent files to use as system prompts, or define agents inline? Reading markdown keeps a single source of truth but adds file parsing.

6. **Plan parsing** — how to extract stages from `.ai/plans/current-plan.md`? Regex on markdown headers, or require structured frontmatter?

7. **Cost tracking** — should orchestrator.py track and report token usage per agent per stage?
