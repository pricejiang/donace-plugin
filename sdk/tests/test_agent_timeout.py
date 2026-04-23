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

from sdk.agent_dispatch import AgentDispatcher, _git_touched_files  # noqa: E402
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

        async def fake_snapshot(cwd: str) -> dict[str, tuple[str, int, int]]:
            return {"apps/web/preexisting.ts": (" M", 1, 1)}

        async def fake_touched(
            cwd: str,
            *,
            baseline: dict[str, tuple[str, int, int]] | None = None,
            file_scope: list[str] | None = None,
        ) -> list[str]:
            self.assertEqual(baseline, {"apps/web/preexisting.ts": (" M", 1, 1)})
            return ["apps/web/a.ts", "apps/web/b.ts"]

        async def never_completes(*a, **kw):
            await asyncio.sleep(10)

        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()

        with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
             patch("sdk.agent_dispatch._git_touched_snapshot", fake_snapshot), \
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

        async def no_files(
            cwd: str,
            *,
            baseline: dict[str, tuple[str, int, int]] | None = None,
            file_scope: list[str] | None = None,
        ) -> list[str]:
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

    def test_touched_files_are_diffed_against_baseline_and_file_scope(self):
        baseline = {
            "apps/web/old.ts": (" M", 10, 100),
            "apps/web/changed.ts": (" M", 10, 100),
            "apps/backend/outside.ts": (" M", 10, 100),
        }
        after = {
            "apps/web/old.ts": (" M", 10, 100),
            "apps/web/changed.ts": (" M", 11, 120),
            "apps/web/new.ts": ("??", 12, 50),
            "apps/backend/outside.ts": (" M", 11, 120),
        }

        async def fake_snapshot(cwd: str) -> dict[str, tuple[str, int, int]]:
            return after

        with patch("sdk.agent_dispatch._git_touched_snapshot", fake_snapshot):
            touched = self._run(_git_touched_files(
                str(_REPO_ROOT),
                baseline=baseline,
                file_scope=["apps/web"],
            ))

        self.assertEqual(touched, ["apps/web/changed.ts", "apps/web/new.ts"])

    def test_non_implementer_timeout_does_not_report_partial_writes(self):
        dispatcher = self._make_dispatcher()
        dispatcher.AGENT_TIMEOUT = {**dispatcher.AGENT_TIMEOUT, "runtime-verifier": 0}

        async def fake_touched(*a, **kw):
            return ["apps/web/a.ts"]

        async def never_completes(*a, **kw):
            await asyncio.sleep(10)

        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()

        with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
             patch("sdk.agent_dispatch._git_touched_files", fake_touched), \
             patch.object(dispatcher, "_run_client", never_completes):
            with self.assertRaises(RuntimeError) as ctx:
                self._run(dispatcher.query(
                    agent="runtime-verifier", prompt="verify it", model="opus",
                ))

        msg = str(ctx.exception)
        self.assertIn("timed out", msg.lower())
        self.assertNotIn("partial writes", msg)


if __name__ == "__main__":
    unittest.main()
