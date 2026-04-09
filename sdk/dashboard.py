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
import sys
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, HTMLResponse
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


# ---------------------------------------------------------------------------
# EventStore — in-memory, grouped by run_id
# ---------------------------------------------------------------------------

class EventStore:
    """In-memory event storage, grouped by run_id."""

    def __init__(self) -> None:
        self.runs: dict[str, list[dict[str, Any]]] = {}

    def append(self, event: dict[str, Any]) -> None:
        run_id = event.get("run_id", "unknown")
        self.runs.setdefault(run_id, []).append(event)

    def get_run(self, run_id: str) -> list[dict[str, Any]]:
        return self.runs.get(run_id, [])

    def list_runs(self) -> list[dict[str, Any]]:
        result = []
        for rid, evts in self.runs.items():
            result.append({
                "run_id": rid,
                "event_count": len(evts),
                "started": evts[0]["timestamp"] if evts else 0,
            })
        return result


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

store = EventStore()
browser_connections: set[WebSocket] = set()
orchestrator_control_ws: set[WebSocket] = set()


def _create_app() -> FastAPI:
    """Build and return the FastAPI app with all routes."""
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
        orchestrator_control_ws.add(ws)
        try:
            # Keep connection alive — orchestrator reads, dashboard writes
            while True:
                try:
                    await ws.receive_text()
                except WebSocketDisconnect:
                    break
        finally:
            orchestrator_control_ws.discard(ws)

    # --- Browser <-> Dashboard ---

    @_app.websocket("/ws")
    async def browser_ws_endpoint(ws: WebSocket) -> None:
        """Browser connects here to view events and send decisions."""
        await ws.accept()
        browser_connections.add(ws)

        # Replay all events for late-joining browsers
        try:
            for _run_id, events in store.runs.items():
                for event in events:
                    await ws.send_text(json.dumps(event))
        except Exception:
            browser_connections.discard(ws)
            return

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

    @_app.post("/api/shutdown")
    async def shutdown() -> dict[str, str]:
        """Called by team-lead when session is over."""
        asyncio.get_running_loop().call_later(0.5, lambda: os._exit(0))
        return {"status": "shutting_down"}

    @_app.get("/api/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "runs": str(len(store.runs)),
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
    args = parser.parse_args()

    dashboard_app = _create_app()

    print(f"donace dashboard starting on http://localhost:{args.port}", file=sys.stderr)
    uvicorn.run(
        dashboard_app,
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
