from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from sdk.events import EventBus
from sdk.orchestrator import SharedContext, run_documenter


class _StubDispatcher:
    def __init__(self, changed_files: str) -> None:
        self.changed_files = changed_files
        self.query_called = False

    async def _get_changed_files(self) -> str:
        return self.changed_files

    async def query(self, *args, **kwargs):
        self.query_called = True
        raise AssertionError("Claude documenter should not run for trivial doc-only fast path")


class DocumenterFastPathTests(unittest.TestCase):
    def test_doc_only_wrap_writes_session_log_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_ctx = SharedContext(
                run_id="run-fastdoc",
                cwd=tmpdir,
                task="Rename README title from Donace to Donace Plugin and keep changes minimal",
            )
            bus = EventBus(run_id="run-fastdoc", interactive=False)
            dispatcher = _StubDispatcher("README.md\n.ai/runs/run-fastdoc.json")

            result = asyncio.run(run_documenter(shared_ctx, "[no stack-specific reviewer available for Python]", bus, dispatcher))  # type: ignore[arg-type]

            session_root = Path(tmpdir) / ".ai" / "sessions"
            session_files = list(session_root.rglob("*.md"))
            session_count = len(session_files)
            session_text = session_files[0].read_text() if session_files else ""

        self.assertIn("Fast path", result)
        self.assertEqual(session_count, 1)
        self.assertIn("Rename README title", session_text)
        self.assertFalse(dispatcher.query_called)
        event_types = [event["type"] for event in bus.get_events()]
        self.assertIn("agent.started", event_types)
        self.assertIn("agent.completed", event_types)


if __name__ == "__main__":
    unittest.main()
