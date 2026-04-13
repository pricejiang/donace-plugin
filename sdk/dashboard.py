"""Independent FastAPI dashboard server for donace orchestration.

Receives events from orchestrator via WebSocket, serves UI to browser,
relays user decisions back to orchestrator.

Usage:
    python -m sdk.dashboard --port 8741
    python sdk/dashboard.py --port 8741
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, HTMLResponse
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


# ---------------------------------------------------------------------------
# EventStore — SQLite-backed, grouped by run_id
# ---------------------------------------------------------------------------

MAX_RUNS = 10  # Retain at most this many runs


def _default_db_path() -> str:
    """Resolve the default SQLite database path.

    Prefers $CLAUDE_PLUGIN_DATA if set (CC plugin convention),
    falls back to ~/.claude/plugins/data/donace/.
    """
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data:
        base = Path(plugin_data)
    else:
        base = Path.home() / ".claude" / "plugins" / "data" / "donace"
    base.mkdir(parents=True, exist_ok=True)
    return str(base / "dashboard.db")


class EventStore:
    """SQLite-backed event storage, grouped by run_id.

    Each event is stored as a JSON blob.  On startup the store
    enforces MAX_RUNS retention — the oldest runs are deleted.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or _default_db_path()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        self._enforce_retention()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id    TEXT    NOT NULL,
                timestamp REAL    NOT NULL,
                data      TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);
        """)

    def _enforce_retention(self) -> None:
        """Delete oldest runs if there are more than MAX_RUNS."""
        rows = self._conn.execute(
            "SELECT run_id, MIN(id) AS first_id FROM events GROUP BY run_id ORDER BY first_id DESC"
        ).fetchall()
        if len(rows) > MAX_RUNS:
            old_ids = [r[0] for r in rows[MAX_RUNS:]]
            placeholders = ",".join("?" for _ in old_ids)
            self._conn.execute(
                f"DELETE FROM events WHERE run_id IN ({placeholders})", old_ids
            )
            self._conn.commit()

    def append(self, event: dict[str, Any]) -> None:
        run_id = event.get("run_id", "unknown")
        ts = event.get("timestamp", 0)
        self._conn.execute(
            "INSERT INTO events (run_id, timestamp, data) VALUES (?, ?, ?)",
            (run_id, ts, json.dumps(event)),
        )
        self._conn.commit()

    def get_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT data FROM events WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def delete_run(self, run_id: str) -> int:
        """Delete all events for a run.  Returns number of rows deleted."""
        cur = self._conn.execute(
            "DELETE FROM events WHERE run_id = ?", (run_id,)
        )
        self._conn.commit()
        return cur.rowcount

    def list_runs(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("""
            SELECT run_id, MIN(timestamp) AS started, COUNT(*) AS event_count
            FROM events
            GROUP BY run_id
            ORDER BY MIN(id) DESC
        """).fetchall()
        return [
            {"run_id": r[0], "started": r[1], "event_count": r[2]}
            for r in rows
        ]

    @property
    def runs(self) -> dict[str, list[dict[str, Any]]]:
        """Compatibility property for browser replay on connect.

        Returns all runs as a dict.  Only called on new browser connect,
        so the full scan is acceptable.
        """
        result: dict[str, list[dict[str, Any]]] = {}
        rows = self._conn.execute(
            "SELECT run_id, data FROM events ORDER BY id"
        ).fetchall()
        for run_id, data in rows:
            result.setdefault(run_id, []).append(json.loads(data))
        return result

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

store: EventStore | None = None
browser_connections: set[WebSocket] = set()
orchestrator_control_ws: set[WebSocket] = set()
# job_id -> control WebSocket (for targeted interrupt routing)
job_control_ws: dict[str, WebSocket] = {}
# job_id -> metadata (populated from job.registered events, cleared on job.completed/interrupted)
job_registry: dict[str, dict] = {}


def _create_app(db_path: str | None = None) -> FastAPI:
    """Build and return the FastAPI app with all routes."""
    global store
    store = EventStore(db_path)

    _app = FastAPI(title="donace dashboard")

    # --- Orchestrator -> Dashboard (event ingestion, one-way) ---

    @_app.websocket("/api/ingest")
    async def ingest(ws: WebSocket) -> None:
        """Orchestrator pushes events here."""
        await ws.accept()
        try:
            async for raw in ws.iter_text():
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                store.append(event)

                # Enforce retention after a new run starts
                if event.get("type") == "run.started":
                    store._enforce_retention()

                # Maintain job registry
                evt_type = event.get("type", "")
                if evt_type == "job.registered":
                    job_registry[event.get("job_id", "")] = {
                        "job_id": event.get("job_id"),
                        "command": event.get("command"),
                        "stage_id": event.get("stage_id", ""),
                        "pid": event.get("pid"),
                        "run_id": event.get("run_id"),
                        "started_at": event.get("timestamp"),
                    }
                elif evt_type in ("job.completed", "job.interrupted"):
                    job_registry.pop(event.get("job_id", ""), None)

                # Fan out to all connected browsers
                dead: set[WebSocket] = set()
                for browser_ws in browser_connections.copy():
                    try:
                        await browser_ws.send_text(raw)
                    except Exception:
                        dead.add(browser_ws)
                browser_connections.difference_update(dead)
        except WebSocketDisconnect:
            pass

    # --- Dashboard -> Orchestrator (user decisions, one-way) ---

    @_app.websocket("/api/control")
    async def control(ws: WebSocket) -> None:
        """Orchestrator listens here for user decisions relayed from browser."""
        await ws.accept()
        # Track job_id for targeted interrupt routing
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

    # --- Browser <-> Dashboard ---

    @_app.websocket("/ws")
    async def browser_ws_endpoint(ws: WebSocket) -> None:
        """Browser connects here to view events and send decisions.

        No replay on connect — browser fetches history via REST API
        (/api/runs, /api/events/{run_id}) on demand. WebSocket only
        pushes live events from active orchestrator connections.
        """
        await ws.accept()
        browser_connections.add(ws)

        # Listen for decisions from browser and relay to orchestrator
        try:
            async for raw in ws.iter_text():
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # Relay user decisions to orchestrator
                if msg.get("action") == "resolve":
                    dead: set[WebSocket] = set()
                    for orch_ws in orchestrator_control_ws.copy():
                        try:
                            await orch_ws.send_text(raw)
                        except Exception:
                            dead.add(orch_ws)
                    orchestrator_control_ws.difference_update(dead)
        except WebSocketDisconnect:
            pass
        finally:
            browser_connections.discard(ws)

    # --- REST endpoints ---

    @_app.get("/")
    async def index() -> FileResponse:
        static_path = Path(__file__).parent / "static" / "index.html"
        return FileResponse(str(static_path))

    @_app.get("/api/runs")
    async def list_runs() -> list[dict[str, Any]]:
        return store.list_runs()

    @_app.get("/api/events/{run_id}")
    async def get_events(run_id: str, type_filter: str | None = None) -> list[dict[str, Any]]:
        """Get events for a specific run, optionally filtered by event type prefix."""
        events = store.get_run(run_id)
        if type_filter:
            events = [e for e in events if e.get("type", "").startswith(type_filter)]
        return events

    @_app.delete("/api/runs/{run_id}")
    async def delete_run(run_id: str) -> dict[str, Any]:
        """Delete a run and all its events."""
        deleted = store.delete_run(run_id)
        return {"status": "deleted", "run_id": run_id, "events_deleted": deleted}

    @_app.get("/api/jobs/active")
    async def list_active_jobs() -> list[dict[str, Any]]:
        """List currently running jobs."""
        return list(job_registry.values())

    @_app.post("/api/interrupt")
    async def interrupt_job(request: Request) -> dict[str, Any]:
        """Send interrupt signal to a specific job."""
        data = await request.json()
        job_id = data.get("job_id", "")
        reason = data.get("reason", "user requested")

        if job_id not in job_registry:
            return {"error": "job_not_found", "job_id": job_id}

        # Route to specific job's control WebSocket
        ws = job_control_ws.get(job_id)
        if ws:
            try:
                msg = json.dumps({"action": "interrupt", "job_id": job_id, "reason": reason})
                await ws.send_text(msg)
                return {"status": "sent", "job_id": job_id}
            except Exception as exc:
                return {"error": str(exc), "job_id": job_id}

        # Fallback: broadcast to all orchestrator connections
        msg = json.dumps({"action": "interrupt", "job_id": job_id, "reason": reason})
        dead: set[WebSocket] = set()
        for orch_ws in orchestrator_control_ws.copy():
            try:
                await orch_ws.send_text(msg)
            except Exception:
                dead.add(orch_ws)
        orchestrator_control_ws.difference_update(dead)
        return {"status": "broadcast", "job_id": job_id}

    @_app.post("/api/shutdown")
    async def shutdown() -> dict[str, str]:
        """Called by team-lead when session is over."""
        if store:
            store.close()
        asyncio.get_running_loop().call_later(0.5, lambda: os._exit(0))
        return {"status": "shutting_down"}

    @_app.get("/api/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "runs": str(len(store.list_runs())),
            "browsers": str(len(browser_connections)),
            "orchestrators": str(len(orchestrator_control_ws)),
        }

    return _app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not HAS_FASTAPI:
        print("Error: fastapi and uvicorn are required. Install with: pip install fastapi uvicorn",
              file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="donace dashboard server")
    parser.add_argument("--port", type=int, default=8741, help="Port to listen on (default: 8741)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--db", type=str, default=None, help="SQLite database path (default: auto)")
    args = parser.parse_args()

    dashboard_app = _create_app(db_path=args.db)

    print(f"donace dashboard starting on http://localhost:{args.port}", file=sys.stderr)
    uvicorn.run(
        dashboard_app,
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
