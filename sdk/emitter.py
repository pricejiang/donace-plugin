"""WebSocketEmitter — pushes events from orchestrator to dashboard, receives decisions."""
from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False
    WebSocketClientProtocol = None  # type: ignore

from sdk.events import Decision, Event, EventBus


class WebSocketEmitter:
    """EventBus subscriber that forwards events to the dashboard via WebSocket.

    Two connections:
      - /api/ingest: orchestrator -> dashboard (events, one-way push)
      - /api/control: dashboard -> orchestrator (decisions, one-way receive)
    """

    def __init__(self, dashboard_url: str, bus: EventBus) -> None:
        self.dashboard_url = dashboard_url.rstrip("/")
        self.bus = bus
        self._ingest_ws: WebSocketClientProtocol | None = None
        self._control_ws: WebSocketClientProtocol | None = None
        self._control_task: asyncio.Task | None = None
        self._connected = False

    async def connect(self) -> None:
        """Connect both channels. If dashboard not running, continue without it."""
        if not HAS_WEBSOCKETS:
            print("Warning: websockets package not installed, dashboard streaming disabled",
                  file=sys.stderr)
            return

        try:
            self._ingest_ws = await websockets.connect(
                f"{self.dashboard_url}/api/ingest",
                ping_interval=20,
                ping_timeout=20,
            )
            self._control_ws = await websockets.connect(
                f"{self.dashboard_url}/api/control",
                ping_interval=20,
                ping_timeout=20,
            )
            self._control_task = asyncio.create_task(self._listen_controls())
            self._connected = True
        except (ConnectionRefusedError, OSError, ValueError) as exc:
            print(f"Warning: Dashboard not running ({exc}), events will not be streamed",
                  file=sys.stderr)

    async def disconnect(self) -> None:
        """Cleanly close both WebSocket connections."""
        if self._control_task and not self._control_task.done():
            self._control_task.cancel()
            try:
                await self._control_task
            except asyncio.CancelledError:
                pass

        if self._ingest_ws:
            try:
                await self._ingest_ws.close()
            except Exception:
                pass
            self._ingest_ws = None

        if self._control_ws:
            try:
                await self._control_ws.close()
            except Exception:
                pass
            self._control_ws = None

        self._connected = False

    async def __call__(self, event: Event) -> None:
        """EventBus subscriber interface. Non-blocking push to dashboard."""
        if not self._ingest_ws:
            return

        try:
            await self._ingest_ws.send(event.to_json())
        except Exception:
            # Connection lost — disable further sends
            self._ingest_ws = None

    async def _listen_controls(self) -> None:
        """Receive user decisions from dashboard and resolve checkpoints on EventBus."""
        if not self._control_ws:
            return

        try:
            async for msg in self._control_ws:
                try:
                    data = json.loads(msg)
                    if data.get("action") == "resolve":
                        checkpoint = data["checkpoint"]
                        decision = Decision(data["decision"])
                        self.bus.resolve(checkpoint, decision)
                except (json.JSONDecodeError, KeyError, ValueError) as exc:
                    print(f"Warning: Invalid control message: {exc}", file=sys.stderr)
        except asyncio.CancelledError:
            pass
        except Exception:
            # Connection closed
            self._control_ws = None

    @property
    def is_connected(self) -> bool:
        return self._connected
