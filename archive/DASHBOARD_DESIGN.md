# Dashboard Design

Real-time web dashboard for monitoring and controlling the full orchestration pipeline (Phase 0-3). Runs as an independent localhost process, survives orchestrator restarts, supports multiple runs.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Scope | Full pipeline (Phase 0-3) | All agent activity visible, not just sprint loop |
| Granularity | Coarse + fine (expandable) | Default: agent lifecycle + phase/stage progress. Expand for tool calls, prompts, token usage |
| Interactivity | Phase 0/1 read-only, Phase 2 interactive, Phase 3 read-only | Control points at sprint loop junctions — skip stage, abort fix loop, override gate |
| Process model | Dashboard is an independent long-lived process | Survives orchestrator crashes, supports replay, keeps history across MUST_STOP restarts |
| Co-design | EventBus designed alongside orchestrator | Avoids bolting on observability after the fact. No rewrite needed for interactivity |
| Performance impact | Negligible | emit() < 0.1ms vs LLM query 10-60s. WebSocket push is fire-and-forget |

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
  │  bus.subscribe(WebSocketEmitter("ws://localhost:8741/api/ingest"))
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
  │  /api/ingest  ◄── WebSocket ── orchestrator pushes events
  │  /api/control ──► WebSocket ──► orchestrator receives decisions
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
| Multiple projects | Each gets its own dashboard port (or single dashboard with run_id tabs) |

## Role Changes

### team-lead.md (simplified)

Reduced from full orchestrator to thin launcher + reporter:

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

### orchestrator.py (new — replaces team-lead's Phase 0-3 logic)

All orchestration logic moves to Python. Agent markdown files remain as system prompts.

| team-lead.md (before) | orchestrator.py (after) |
|---|---|
| Phase 0: Boot — read sessions, load cards, detect resume | `phase_boot()` — same logic, deterministic |
| Phase 1: Plan — detect stack, dispatch planner/architect | `phase_plan()` — same dispatch, cannot skip steps |
| Phase 2: Sprint loop — dispatch implementer/verifiers/fix | `phase_sprint()` — already designed in SDK_ORCHESTRATION_DESIGN.md |
| Phase 3: Wrap — final review, session log, knowledge cards | `phase_wrap()` — same dispatch, cannot skip |

Key difference: **Python controls dispatch order. LLM agents do the thinking within each dispatch, but cannot skip or reorder steps.**

## File Structure

```
sdk/
  orchestrator.py     # Full pipeline: Phase 0-3, receives bus
  sprint_loop.py      # Phase 2 inner loop (called by orchestrator)
  events.py           # Event types, EventBus, Checkpoints, Decision enum
  dashboard.py        # Independent WebSocket server + static file serving
  emitter.py          # WebSocketEmitter — pushes events from orchestrator to dashboard
  static/
    index.html        # Single-file dashboard UI (HTML + CSS + JS)
  requirements.txt    # claude-agent-sdk, fastapi, uvicorn, websockets
```

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

### Example Events

Run start (Phase 0):
```json
{
  "id": "evt-001",
  "timestamp": 1712567800.000,
  "type": "run.started",
  "run_id": "run-abc123",
  "phase": null,
  "stage": null,
  "task": "Add WebSocket support",
  "cwd": "/Users/dev/myproject",
  "interactive": true
}
```

Agent dispatch (Phase 1):
```json
{
  "id": "evt-042",
  "timestamp": 1712567850.500,
  "type": "agent.started",
  "run_id": "run-abc123",
  "phase": "plan",
  "stage": null,
  "agent": "architect",
  "prompt": "Analyze codebase and produce staged plan for: Add WebSocket support...",
  "model": "sonnet"
}
```

Checkpoint (Phase 2):
```json
{
  "id": "evt-089",
  "timestamp": 1712567920.100,
  "type": "checkpoint.reached",
  "run_id": "run-abc123",
  "phase": "sprint",
  "stage": "Stage 2: Broadcasting",
  "checkpoint_id": "post-verify",
  "options": ["continue", "skip_fixes", "abort"],
  "timeout_s": 30
}
```

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

## WebSocket Emitter (emitter.py)

Runs inside orchestrator.py. Pushes events to the dashboard server.

```python
class WebSocketEmitter:
    """EventBus subscriber that forwards events to dashboard via WebSocket."""

    def __init__(self, dashboard_url: str):
        self.dashboard_url = dashboard_url   # ws://localhost:8741/api/ingest
        self._ws: WebSocket | None = None
        self._queue: asyncio.Queue[Event] = asyncio.Queue()

    async def connect(self):
        """Connect to dashboard. Retries once, then runs without dashboard."""
        try:
            self._ws = await websockets.connect(self.dashboard_url)
            asyncio.create_task(self._listen_for_controls())
        except ConnectionRefusedError:
            print("Warning: Dashboard not running, events will not be streamed")

    async def __call__(self, event: Event):
        """EventBus subscriber interface. Non-blocking push."""
        if self._ws:
            try:
                await self._ws.send(event.to_json())
            except ConnectionClosed:
                self._ws = None  # dashboard died, continue without it

    async def _listen_for_controls(self):
        """Receive user decisions from dashboard and resolve checkpoints."""
        try:
            async for msg in self._ws:
                data = json.loads(msg)
                if data.get("action") == "resolve":
                    self.bus.resolve(data["checkpoint"], Decision(data["decision"]))
        except ConnectionClosed:
            pass
```

## Dashboard Server (dashboard.py)

Independent process. Receives events from orchestrator, serves UI to browser, relays user decisions back.

```python
app = FastAPI()

class EventStore:
    """In-memory event storage, grouped by run_id."""
    def __init__(self):
        self.runs: dict[str, list[Event]] = {}  # run_id → events

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
orchestrator_connections: set[WebSocket] = set()


# --- Orchestrator → Dashboard (event ingestion) ---

@app.websocket("/api/ingest")
async def ingest(ws: WebSocket):
    """Orchestrator connects here to push events."""
    await ws.accept()
    orchestrator_connections.add(ws)
    try:
        async for raw in ws.iter_text():
            event = Event.from_json(raw)
            store.append(event)
            # Fan out to all browser clients
            for browser_ws in browser_connections.copy():
                try:
                    await browser_ws.send_text(raw)
                except:
                    browser_connections.discard(browser_ws)
    except WebSocketDisconnect:
        orchestrator_connections.discard(ws)


# --- Browser ↔ Dashboard (viewing + control) ---

@app.websocket("/ws")
async def browser_ws(ws: WebSocket):
    """Browser connects here to view events and send decisions."""
    await ws.accept()
    browser_connections.add(ws)

    # Replay: send all events for the latest run (or all runs)
    for run_id, events in store.runs.items():
        for event in events:
            await ws.send_text(event.to_json())

    try:
        async for raw in ws.iter_text():
            msg = json.loads(raw)
            # Relay user decisions to orchestrator
            if msg.get("action") == "resolve":
                for orch_ws in orchestrator_connections:
                    await orch_ws.send_text(raw)
    except WebSocketDisconnect:
        browser_connections.discard(ws)


# --- REST endpoints ---

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

### Startup

```bash
# dashboard.py standalone launch
python sdk/dashboard.py --port 8741

# Prints:
# Dashboard: http://localhost:8741
# Waiting for orchestrator connection...
```

## Control Points

Checkpoints are in Phase 2 only (sprint loop). Phase 0/1/3 are read-only — events stream but no pause points.

```python
@dataclass
class Checkpoint:
    id: str
    description: str
    context: list[str]      # what's visible at this point
    options: list[str]       # available user actions

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
```

### Decision Enum

```python
class Decision(str, Enum):
    CONTINUE = "continue"
    SKIP_STAGE = "skip_stage"
    SKIP_FIXES = "skip_fixes"
    ABORT_FIX_LOOP = "abort_fix_loop"
    SKIP_NEXT = "skip_next"
    STOP_LOOP = "stop_loop"
    ABORT = "abort"
```

## Integration: orchestrator.py

Full pipeline orchestration with EventBus wired through all phases.

```python
async def run(task: str, cwd: str, bus: EventBus) -> OrchestrationResult:
    await bus.emit(RunStarted(task=task, cwd=cwd))

    # ── Phase 0: Boot ──
    await bus.emit(PhaseStarted(phase="boot"))
    await bus.emit(AgentStarted(agent="context-loader", prompt="..."))
    session_context = await load_session_context(cwd)   # reads .ai/sessions/, .ai/cards/
    resume_plan = check_for_resume(cwd)                 # checks .ai/plans/current-plan.md
    await bus.emit(AgentCompleted(agent="context-loader", result_summary="..."))
    await bus.emit(PhaseCompleted(phase="boot"))

    # ── Phase 1: Plan (skip if resuming) ──
    if not resume_plan:
        await bus.emit(PhaseStarted(phase="plan"))

        # Stack detection (deterministic, no LLM needed)
        stack = detect_stack(cwd)
        await bus.emit(Event(type="plan.stack_detected", stack=stack))

        # Planner (skip for bug fixes)
        if task_needs_spec(task):
            await bus.emit(AgentStarted(agent="planner", prompt=task, model="opus"))
            spec = await query(agent="planner", prompt=task)
            await bus.emit(AgentCompleted(agent="planner", result_summary=spec[:200]))
        else:
            spec = task

        # Architect
        await bus.emit(AgentStarted(agent="architect", prompt=spec, model="sonnet"))
        plan = await query(agent="architect", prompt=spec)
        await bus.emit(AgentCompleted(agent="architect", result_summary=plan[:200]))

        # Codex plan review (mandatory)
        await bus.emit(AgentStarted(agent="codex-plan-review", prompt=plan))
        review = await run_codex_plan_review(plan)
        await bus.emit(AgentCompleted(agent="codex-plan-review", result_summary=review[:200]))

        # Revision loop if major issues
        if review.has_major_issues:
            await bus.emit(AgentStarted(agent="architect", prompt=f"Revise: {review}"))
            plan = await query(agent="architect", prompt=f"Revise plan based on: {review}")
            await bus.emit(AgentCompleted(agent="architect", result_summary="revised"))

        await bus.emit(PhaseCompleted(phase="plan"))
    else:
        plan = resume_plan

    # ── Phase 2: Sprint Loop ──
    await bus.emit(PhaseStarted(phase="sprint"))
    sprint_result = await run_sprint_loop(plan, cwd, bus)   # existing design
    await bus.emit(PhaseCompleted(phase="sprint"))

    # ── Phase 3: Wrap ──
    await bus.emit(PhaseStarted(phase="wrap"))

    # Final stack-specific review
    reviewer = f"{stack}-reviewer"  # "typescript-reviewer" or "ios-reviewer"
    await bus.emit(AgentStarted(agent=reviewer, prompt="Full codebase review...", model="opus"))
    final_review = await query(agent=reviewer, prompt="...")
    await bus.emit(AgentCompleted(agent=reviewer, result_summary=final_review[:200]))

    # Session log + knowledge cards
    await bus.emit(AgentStarted(agent="session-writer", prompt="Write session log..."))
    await write_session_log(cwd, sprint_result, final_review)
    await bus.emit(AgentCompleted(agent="session-writer"))

    await bus.emit(PhaseCompleted(phase="wrap"))
    await bus.emit(RunCompleted(result_summary=sprint_result.summary))

    return OrchestrationResult(sprint=sprint_result, review=final_review)
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
| Pipeline | Phase list (Boot/Plan/Sprint/Wrap) with nested agent/stage items. Status: ✓ done, ● active, ○ pending |
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
6. Dashboard relays to orchestrator via `/api/ingest` WebSocket
7. EventBus resolves the future, sprint loop continues
8. `checkpoint.resolved` event streams to browser, buttons disappear

Auto-continue on timeout. Phase 0/1/3: no buttons, events stream read-only.

### Styling

- Dark theme (matches terminal aesthetic)
- Monospace font for logs and prompts
- Status colors: green (pass/running), yellow (waiting/warning), red (failed/blocked), blue (checkpoint)
- Minimal CSS, no animations beyond a subtle pulse on active agents

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

## Communication Protocol

Three WebSocket channels, all through dashboard.py:

```
orchestrator ──ws──► /api/ingest  ──► dashboard ──ws──► /ws ──► browser
                                                                   │
browser ──ws──► /ws ──► dashboard ──ws──► /api/ingest ──► orchestrator
                                    (relay user decisions)
```

| Channel | Direction | Payload |
|---------|-----------|---------|
| `/api/ingest` (orchestrator → dashboard) | Events push | `Event` JSON objects |
| `/api/ingest` (dashboard → orchestrator) | Decision relay | `{"action": "resolve", "checkpoint": "...", "decision": "..."}` |
| `/ws` (dashboard ↔ browser) | Bidirectional | Events to browser, decisions from browser |

## Open Questions

1. **Port selection** — hardcode 8741 or find an available port? Hardcode is predictable; auto-port needs a way to pass the URL to team-lead.

2. **Event persistence** — write events to `.ai/sessions/events.jsonl` for post-mortem? EventStore is in-memory, lost when dashboard exits. A `FileLogger` subscriber could persist to disk cheaply.

3. **Browser auto-open** — should dashboard.py auto-open the browser on start? `webbrowser.open()` is one line but some users find it annoying. Could be a `--open` flag.

4. **Dashboard discovery** — team-lead needs to know if dashboard is already running before starting a new one. Options: check port with `curl`, PID file, or just let it fail with "address in use".

5. **Multi-project** — two orchestrators running on different projects simultaneously. Options: one dashboard per project (different ports) or single dashboard with run_id filtering. Leaning toward one-dashboard-per-project for simplicity.
