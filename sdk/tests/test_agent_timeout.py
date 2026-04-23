"""Tests for implementer-timeout partial-return.

Motivation: run-phase5-runA stage-3 timed out at 900s on the first
implementer attempt. The retry observed `auto_commit: skipped — no
git-visible changes`, which meant the timed-out implementer had
actually written code to disk before the hang — but the timeout path
only raised `agent=implementer timed out after 900s`, losing that signal.
Team-lead (and the human reading BLOCKED) couldn't tell whether to retry
from scratch or resume.

Fix: when `query()` hits the wait_for timeout, capture `git status
--porcelain` and surface the touched paths in the RuntimeError message
so the BLOCKED job's `unresolved` string tells future-you "impl wrote
A.ts, B.ts but hung before returning".

Runs with stdlib unittest:
    python3 -m unittest sdk.tests.test_agent_timeout -v
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk.agent_dispatch import AgentDispatcher  # noqa: E402
from sdk.events import EventBus  # noqa: E402


class TimeoutPartialReturnTests(unittest.TestCase):
    """`dispatcher.query()` on timeout must include touched files in the error."""

    def _make_dispatcher(self) -> AgentDispatcher:
        return AgentDispatcher(
            agents_dir=str(_REPO_ROOT / "agents"),
            cwd=str(_REPO_ROOT),
            bus=EventBus(run_id="test-run"),
            codex_review_base="HEAD",
        )

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_timeout_includes_touched_files(self):
        dispatcher = self._make_dispatcher()
        # Force an immediate timeout without a real sleep.
        dispatcher.AGENT_TIMEOUT = {**dispatcher.AGENT_TIMEOUT, "implementer": 0}

        async def fake_touched(cwd: str) -> list[str]:
            return ["apps/web/a.ts", "apps/web/b.ts"]

        async def never_completes(*a, **kw):
            await asyncio.sleep(10)

        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()

        with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
             patch("sdk.agent_dispatch._git_touched_files", fake_touched), \
             patch.object(dispatcher, "_run_client", never_completes):
            with self.assertRaises(RuntimeError) as ctx:
                self._run(dispatcher.query(
                    agent="implementer", prompt="do it", model="sonnet",
                ))

        msg = str(ctx.exception)
        self.assertIn("timed out", msg.lower())
        self.assertIn("apps/web/a.ts", msg)
        self.assertIn("apps/web/b.ts", msg)

    def test_timeout_with_no_touched_files_has_clean_message(self):
        # Regression guard: when implementer hung without writing anything,
        # don't tack on an empty "(partial writes: )" suffix.
        dispatcher = self._make_dispatcher()
        dispatcher.AGENT_TIMEOUT = {**dispatcher.AGENT_TIMEOUT, "implementer": 0}

        async def no_files(cwd: str) -> list[str]:
            return []

        async def never_completes(*a, **kw):
            await asyncio.sleep(10)

        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()

        with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
             patch("sdk.agent_dispatch._git_touched_files", no_files), \
             patch.object(dispatcher, "_run_client", never_completes):
            with self.assertRaises(RuntimeError) as ctx:
                self._run(dispatcher.query(
                    agent="implementer", prompt="do it", model="sonnet",
                ))

        msg = str(ctx.exception)
        self.assertIn("timed out", msg.lower())
        self.assertNotIn("partial writes", msg)
        self.assertNotIn("()", msg)


if __name__ == "__main__":
    unittest.main()
