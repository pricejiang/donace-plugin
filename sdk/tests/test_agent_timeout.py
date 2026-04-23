"""Tests for the idle-watchdog that replaces wallclock agent timeouts.

Motivation: run-phase5-runB-6e7f3779bc67 killed implementer at 900s
wallclock with 5 partial files still mid-edit; retry had to redo all of
it. Wallclock timeouts murder productive agents. `dispatcher.query()`
now tracks SDK message arrivals plus in-flight tool/subagent work; only
agents that go silent past ``AGENT_IDLE_TIMEOUT[agent]`` are killed.

The partial-writes signal from run-phase5-runA is still load-bearing
when implementer does legitimately hang: touched paths get folded into
the RuntimeError message so BLOCKED jobs tell future-you "impl wrote
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


class IdleWatchdogTests(unittest.TestCase):
    """`dispatcher.query()` uses an idle-heartbeat watchdog, not a wallclock cap."""

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
            # Drain any pending async-generator aclose() tasks that
            # `break` inside `async for` leaves behind — avoids Python 3.13
            # "Task was destroyed but it is pending" warnings.
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def test_active_agent_is_not_killed_even_past_idle_threshold(self):
        """An agent calling on_activity within the threshold survives indefinitely.

        Simulates an implementer whose total runtime (0.3s) far exceeds the
        idle threshold (0.05s), but which keeps emitting activity every 0.03s.
        The watchdog must see those heartbeats and never fire.
        """
        dispatcher = self._make_dispatcher()
        dispatcher.AGENT_IDLE_TIMEOUT = {"implementer": 0.05}
        dispatcher.IDLE_CHECK_INTERVAL = 0.02

        async def active(client, agent, prompt, model_id, *, on_activity=None, **_kw):
            for _ in range(10):
                if on_activity is not None:
                    on_activity()
                await asyncio.sleep(0.03)
            return "ok"

        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()

        with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
             patch.object(dispatcher, "_run_client", active):
            result = self._run(dispatcher.query(
                agent="implementer", prompt="do it", model="sonnet",
            ))

        self.assertEqual(result, "ok")

    def test_long_running_tool_execution_is_not_treated_as_idle(self):
        """A quiet tool run keeps the watchdog paused until the tool returns."""
        dispatcher = self._make_dispatcher()
        dispatcher.AGENT_IDLE_TIMEOUT = {"runtime-verifier": 0.05}
        dispatcher.IDLE_CHECK_INTERVAL = 0.02

        class FakeClient:
            def __init__(self, options):
                self.options = options

            async def disconnect(self):
                return None

        async def long_tool(client, agent, prompt, model_id, *, on_activity=None, **_kw):
            pre_tool = client.options.hooks["PreToolUse"][0].hooks[0]
            post_tool = client.options.hooks["PostToolUse"][0].hooks[0]
            await pre_tool({
                "tool_name": "Bash",
                "tool_input": {"command": "npm run dev"},
            }, None, None)
            await asyncio.sleep(0.15)
            await post_tool({
                "tool_name": "Bash",
                "tool_response": "server ready",
            }, None, None)
            return "ok"

        with patch("sdk.agent_dispatch.ClaudeSDKClient", side_effect=FakeClient), \
             patch.object(dispatcher, "_run_client", long_tool):
            result = self._run(dispatcher.query(
                agent="runtime-verifier", prompt="verify it", model="opus",
            ))

        self.assertEqual(result, "ok")

    def test_long_running_subagent_execution_is_not_treated_as_idle(self):
        """A parent agent waiting on a subagent is busy, not idle."""
        dispatcher = self._make_dispatcher()
        dispatcher.AGENT_IDLE_TIMEOUT = {"planner": 0.05}
        dispatcher.IDLE_CHECK_INTERVAL = 0.02

        class FakeClient:
            def __init__(self, options):
                self.options = options

            async def disconnect(self):
                return None

        async def long_subagent(client, agent, prompt, model_id, *, on_activity=None, **_kw):
            start = client.options.hooks["SubagentStart"][0].hooks[0]
            stop = client.options.hooks["SubagentStop"][0].hooks[0]
            await start({
                "agent_type": "explorer",
                "agent_id": "sub-1",
            }, None, None)
            await asyncio.sleep(0.15)
            await stop({
                "agent_type": "explorer",
                "agent_id": "sub-1",
                "agent_transcript_path": "",
            }, None, None)
            return "ok"

        with patch("sdk.agent_dispatch.ClaudeSDKClient", side_effect=FakeClient), \
             patch.object(dispatcher, "_run_client", long_subagent):
            result = self._run(dispatcher.query(
                agent="planner", prompt="plan it", model="opus",
            ))

        self.assertEqual(result, "ok")

    def test_stalled_agent_is_killed_with_idle_error_message(self):
        """An agent that never calls on_activity is killed; message says 'idle'."""
        dispatcher = self._make_dispatcher()
        dispatcher.AGENT_IDLE_TIMEOUT = {"implementer": 0.05}
        dispatcher.IDLE_CHECK_INTERVAL = 0.02

        async def stalled(client, agent, prompt, model_id, *, on_activity=None, **_kw):
            await asyncio.sleep(10)
            return "unreachable"

        async def no_files(*_a, **_kw):
            return []

        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()

        with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
             patch("sdk.agent_dispatch._git_touched_files", no_files), \
             patch.object(dispatcher, "_run_client", stalled):
            with self.assertRaises(RuntimeError) as ctx:
                self._run(dispatcher.query(
                    agent="implementer", prompt="do it", model="sonnet",
                ))

        msg = str(ctx.exception).lower()
        self.assertIn("idle", msg)
        self.assertIn("threshold", msg)

    def test_stalled_implementer_still_reports_partial_writes(self):
        """Idle-kill must preserve the partial-writes signal for implementer."""
        dispatcher = self._make_dispatcher()
        dispatcher.AGENT_IDLE_TIMEOUT = {"implementer": 0.05}
        dispatcher.IDLE_CHECK_INTERVAL = 0.02

        async def fake_snapshot(cwd):
            return {"apps/web/preexisting.ts": (" M", 1, 1)}

        async def fake_touched(cwd, *, baseline=None, file_scope=None):
            # Verifies baseline survives the refactor too — the dispatcher
            # must snapshot git state before creating the run task and
            # re-read it after the kill to compute the diff.
            self.assertEqual(baseline, {"apps/web/preexisting.ts": (" M", 1, 1)})
            return ["apps/web/a.ts", "apps/web/b.ts"]

        async def stalled(client, agent, prompt, model_id, *, on_activity=None, **_kw):
            await asyncio.sleep(10)

        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()

        with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
             patch("sdk.agent_dispatch._git_touched_snapshot", fake_snapshot), \
             patch("sdk.agent_dispatch._git_touched_files", fake_touched), \
             patch.object(dispatcher, "_run_client", stalled):
            with self.assertRaises(RuntimeError) as ctx:
                self._run(dispatcher.query(
                    agent="implementer", prompt="do it", model="sonnet",
                ))

        msg = str(ctx.exception)
        self.assertIn("partial writes", msg)
        self.assertIn("apps/web/a.ts", msg)
        self.assertIn("apps/web/b.ts", msg)

    def test_stalled_kill_with_no_touched_files_has_clean_message(self):
        """When no partial writes exist, error message skips the "(partial writes: )" suffix."""
        dispatcher = self._make_dispatcher()
        dispatcher.AGENT_IDLE_TIMEOUT = {"implementer": 0.05}
        dispatcher.IDLE_CHECK_INTERVAL = 0.02

        async def no_files(*_a, **_kw):
            return []

        async def stalled(client, agent, prompt, model_id, *, on_activity=None, **_kw):
            await asyncio.sleep(10)

        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()

        with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
             patch("sdk.agent_dispatch._git_touched_files", no_files), \
             patch.object(dispatcher, "_run_client", stalled):
            with self.assertRaises(RuntimeError) as ctx:
                self._run(dispatcher.query(
                    agent="implementer", prompt="do it", model="sonnet",
                ))

        msg = str(ctx.exception)
        self.assertNotIn("partial writes", msg)
        self.assertNotIn("()", msg)

    def test_non_implementer_idle_kill_skips_partial_writes(self):
        """Only implementer triggers the git snapshot — other agents skip it."""
        dispatcher = self._make_dispatcher()
        dispatcher.AGENT_IDLE_TIMEOUT = {"runtime-verifier": 0.05}
        dispatcher.IDLE_CHECK_INTERVAL = 0.02

        async def should_not_run(*_a, **_kw):
            self.fail("_git_touched_files must not be called for non-implementer agents")

        async def stalled(client, agent, prompt, model_id, *, on_activity=None, **_kw):
            await asyncio.sleep(10)

        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()

        with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
             patch("sdk.agent_dispatch._git_touched_files", should_not_run), \
             patch.object(dispatcher, "_run_client", stalled):
            with self.assertRaises(RuntimeError) as ctx:
                self._run(dispatcher.query(
                    agent="runtime-verifier", prompt="verify it", model="opus",
                ))

        msg = str(ctx.exception)
        self.assertIn("idle", msg.lower())
        self.assertNotIn("partial writes", msg)

    def test_touched_files_are_diffed_against_baseline_and_file_scope(self):
        """`_git_touched_files` returns only files in scope that changed vs baseline."""
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

        async def fake_snapshot(cwd):
            return after

        with patch("sdk.agent_dispatch._git_touched_snapshot", fake_snapshot):
            touched = self._run(_git_touched_files(
                str(_REPO_ROOT),
                baseline=baseline,
                file_scope=["apps/web"],
            ))

        self.assertEqual(touched, ["apps/web/changed.ts", "apps/web/new.ts"])

    def test_run_client_invokes_on_activity_for_each_assistant_message(self):
        """The real `_run_client` must call on_activity as SDK messages stream in."""
        from sdk.agent_dispatch import AssistantMessage, TextBlock, ResultMessage

        dispatcher = self._make_dispatcher()

        async def fake_stream():
            for text in ("thinking...", "writing..."):
                msg = MagicMock(spec=AssistantMessage)
                msg.content = [MagicMock(spec=TextBlock, text=text)]
                yield msg
            result = MagicMock(spec=ResultMessage)
            result.is_error = False
            result.result = "final"
            result.usage = None
            yield result

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_client.receive_messages = fake_stream

        calls = []

        def record():
            calls.append(True)

        result = self._run(dispatcher._run_client(
            mock_client, "implementer", "prompt", "claude-sonnet-4-6",
            on_activity=record,
        ))

        self.assertEqual(result, "final")
        # At least one heartbeat per message (2 assistant + 1 result = 3 min).
        self.assertGreaterEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
