"""Tests for the idle-watchdog that replaces wallclock agent timeouts.

Motivation: run-phase5-runB-6e7f3779bc67 killed implementer at 900s
wallclock with 5 partial files still mid-edit; retry had to redo all of
it. Wallclock timeouts murder productive agents. `dispatcher.query()`
now tracks SDK message arrivals plus in-flight tool/subagent work; only
agents that go silent past ``AGENT_IDLE_HARD_TIMEOUT[agent]`` are killed.
Crossing ``AGENT_IDLE_SOFT_TIMEOUT[agent]`` first emits ``AgentStalled``
and (when run_id+job_id are set) writes a `<job-id>.<agent>.stalled`
marker so team-lead / main LLM can prompt the user to ``.continue`` or
``.kill``.

The partial-writes signal from run-phase5-runA is still load-bearing
when implementer does legitimately hang: touched paths get folded into
the RuntimeError message so BLOCKED jobs tell future-you "impl wrote
A.ts, B.ts but hung before returning".

Runs with stdlib unittest:
    python3 -m unittest sdk.tests.test_agent_timeout -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk.agent_dispatch import AgentDispatcher, _git_touched_files  # noqa: E402
from sdk.events import AgentResumed, AgentStalled, Event, EventBus  # noqa: E402


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
        dispatcher.AGENT_IDLE_HARD_TIMEOUT ={"implementer": 0.05}
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
        dispatcher.AGENT_IDLE_HARD_TIMEOUT ={"runtime-verifier": 0.05}
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
        dispatcher.AGENT_IDLE_HARD_TIMEOUT ={"planner": 0.05}
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
        dispatcher.AGENT_IDLE_HARD_TIMEOUT ={"implementer": 0.05}
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
        dispatcher.AGENT_IDLE_HARD_TIMEOUT ={"implementer": 0.05}
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
        dispatcher.AGENT_IDLE_HARD_TIMEOUT ={"implementer": 0.05}
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
        dispatcher.AGENT_IDLE_HARD_TIMEOUT ={"runtime-verifier": 0.05}
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


class StallEventSerializationTests(unittest.TestCase):
    """New stall events must replay as typed events, not bare Event."""

    def test_agent_stalled_round_trips_from_json(self):
        event = AgentStalled(
            agent="implementer",
            idle_s=187,
            soft_threshold_s=180,
            hard_threshold_s=900,
            marker_path=".ai/runs/run-x/jobs/job-run.implementer.stalled",
        )

        decoded = Event.from_json(event.to_json())

        self.assertIsInstance(decoded, AgentStalled)
        self.assertEqual(decoded.agent, "implementer")
        self.assertEqual(decoded.idle_s, 187)
        self.assertEqual(decoded.marker_path, event.marker_path)

    def test_agent_resumed_round_trips_from_json(self):
        event = AgentResumed(
            agent="runtime-verifier",
            waited_s=42,
            via="continue_marker",
        )

        decoded = Event.from_json(event.to_json())

        self.assertIsInstance(decoded, AgentResumed)
        self.assertEqual(decoded.agent, "runtime-verifier")
        self.assertEqual(decoded.waited_s, 42)
        self.assertEqual(decoded.via, "continue_marker")


class SoftStallWatchdogTests(unittest.TestCase):
    """Two-stage watchdog: soft = warn + marker, hard = kill.

    Motivation: run-phase5-runE-dbeea30dbe6d killed opus implementer at
    186s twice in a row. Opus regularly thinks 2-4 min between tool calls;
    that's not a stuck agent, just normal latency. The fix surfaces the
    stall to the user (event + marker file) without burning a retry,
    and only kills if the user-decision marker says so or the hard cap
    fires.
    """

    def _make_dispatcher(self, *, run_id=None, job_id=None, cwd=None):
        return AgentDispatcher(
            agents_dir=str(_REPO_ROOT / "agents"),
            cwd=cwd or str(_REPO_ROOT),
            bus=EventBus(run_id="test-run"),
            codex_review_base="HEAD",
            run_id=run_id,
            job_id=job_id,
        )

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def test_soft_threshold_does_not_kill_within_hard_window(self):
        """Hitting soft warns; agent keeps running until hard fires."""
        from sdk.events import AgentStalled

        dispatcher = self._make_dispatcher()
        dispatcher.AGENT_IDLE_SOFT_TIMEOUT = {"implementer": 0.05}
        dispatcher.AGENT_IDLE_HARD_TIMEOUT = {"implementer": 0.20}
        dispatcher.IDLE_CHECK_INTERVAL = 0.02

        events: list = []
        original_emit = dispatcher.bus.emit

        async def capturing_emit(event):
            events.append(event)
            await original_emit(event)

        dispatcher.bus.emit = capturing_emit

        async def stalled(client, agent, prompt, model_id, *, on_activity=None, **_kw):
            await asyncio.sleep(10)

        async def no_files(*_a, **_kw):
            return []

        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()

        with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
             patch("sdk.agent_dispatch._git_touched_files", no_files), \
             patch.object(dispatcher, "_run_client", stalled):
            with self.assertRaises(RuntimeError) as ctx:
                self._run(dispatcher.query(
                    agent="implementer", prompt="do it", model="opus",
                ))

        # Hard kill fires AFTER soft warn — both should be observed.
        stall_events = [e for e in events if isinstance(e, AgentStalled)]
        self.assertEqual(len(stall_events), 1, "expected exactly one AgentStalled")
        self.assertEqual(stall_events[0].soft_threshold_s, 0.05)
        self.assertEqual(stall_events[0].hard_threshold_s, 0.20)

        msg = str(ctx.exception)
        self.assertIn("idle", msg)
        # Hard threshold value must appear (not soft) so the human sees the
        # actual cap that fired.
        self.assertIn("0.2", msg)

    def test_soft_threshold_writes_marker_when_job_context_present(self):
        """With run_id+job_id, a `<job-id>.<agent>.stalled` marker appears on soft fire."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            jobs_dir = Path(tmp) / ".ai" / "runs" / "run-x" / "jobs"
            jobs_dir.mkdir(parents=True)
            marker = jobs_dir / "job-stage-1-deadbeef.implementer.stalled"

            dispatcher = self._make_dispatcher(
                run_id="run-x", job_id="job-stage-1-deadbeef", cwd=tmp,
            )
            dispatcher.AGENT_IDLE_SOFT_TIMEOUT = {"implementer": 0.05}
            dispatcher.AGENT_IDLE_HARD_TIMEOUT = {"implementer": 0.20}
            dispatcher.IDLE_CHECK_INTERVAL = 0.02

            async def stalled(client, agent, prompt, model_id, *, on_activity=None, **_kw):
                await asyncio.sleep(10)

            async def no_files(*_a, **_kw):
                return []

            async def no_snapshot(*_a, **_kw):
                return {}

            marker_seen_during_run: dict = {}

            async def watcher():
                # Poll for the marker to appear DURING the soft window. This
                # confirms the file is on disk before hard kill removes it.
                for _ in range(40):
                    if marker.exists():
                        marker_seen_during_run["body"] = marker.read_text()
                        break
                    await asyncio.sleep(0.01)

            mock_client = MagicMock()
            mock_client.disconnect = AsyncMock()

            async def race():
                w = asyncio.create_task(watcher())
                try:
                    await dispatcher.query(
                        agent="implementer", prompt="do it", model="opus",
                    )
                except RuntimeError:
                    pass
                await w

            with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
                 patch("sdk.agent_dispatch._git_touched_files", no_files), \
                 patch("sdk.agent_dispatch._git_touched_snapshot", no_snapshot), \
                 patch.object(dispatcher, "_run_client", stalled):
                self._run(race())

            # Marker existed during the soft window
            self.assertIn("body", marker_seen_during_run, "marker never appeared on disk")
            payload = json.loads(marker_seen_during_run["body"])
            self.assertEqual(payload["agent"], "implementer")
            self.assertEqual(payload["soft_threshold_s"], 0.05)
            self.assertEqual(payload["hard_threshold_s"], 0.20)
            self.assertEqual(payload["pid"], os.getpid())
            # Marker is removed when hard fires (no point leaving stale state)
            self.assertFalse(marker.exists(), "marker should be cleaned up on hard kill")

    def test_soft_threshold_emits_event_without_job_context(self):
        """Without run_id/job_id, no marker is written but event still fires."""
        from sdk.events import AgentStalled

        dispatcher = self._make_dispatcher()  # no run_id/job_id
        dispatcher.AGENT_IDLE_SOFT_TIMEOUT = {"implementer": 0.05}
        dispatcher.AGENT_IDLE_HARD_TIMEOUT = {"implementer": 0.20}
        dispatcher.IDLE_CHECK_INTERVAL = 0.02

        events: list = []
        original_emit = dispatcher.bus.emit

        async def capturing_emit(event):
            events.append(event)
            await original_emit(event)

        dispatcher.bus.emit = capturing_emit

        async def stalled(client, agent, prompt, model_id, *, on_activity=None, **_kw):
            await asyncio.sleep(10)

        async def no_files(*_a, **_kw):
            return []

        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()

        with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
             patch("sdk.agent_dispatch._git_touched_files", no_files), \
             patch.object(dispatcher, "_run_client", stalled):
            with self.assertRaises(RuntimeError):
                self._run(dispatcher.query(
                    agent="implementer", prompt="do it", model="opus",
                ))

        stall_events = [e for e in events if isinstance(e, AgentStalled)]
        self.assertEqual(len(stall_events), 1)
        self.assertIsNone(stall_events[0].marker_path)

    def test_continue_marker_resets_soft_state_and_lets_agent_finish(self):
        """`.continue` clears soft + writes activity → agent runs to completion."""
        import tempfile
        from sdk.events import AgentStalled, AgentResumed

        with tempfile.TemporaryDirectory() as tmp:
            jobs_dir = Path(tmp) / ".ai" / "runs" / "run-y" / "jobs"
            jobs_dir.mkdir(parents=True)
            marker = jobs_dir / "job-stage-1-cafebabe.implementer.stalled"
            cont = jobs_dir / "job-stage-1-cafebabe.implementer.continue"

            dispatcher = self._make_dispatcher(
                run_id="run-y", job_id="job-stage-1-cafebabe", cwd=tmp,
            )
            dispatcher.AGENT_IDLE_SOFT_TIMEOUT = {"implementer": 0.05}
            dispatcher.AGENT_IDLE_HARD_TIMEOUT = {"implementer": 5.0}
            dispatcher.IDLE_CHECK_INTERVAL = 0.02

            events: list = []
            original_emit = dispatcher.bus.emit

            async def capturing_emit(event):
                events.append(event)
                await original_emit(event)

            dispatcher.bus.emit = capturing_emit

            async def stall_then_continue(client, agent, prompt, model_id, *, on_activity=None, **_kw):
                # Sit idle long enough for soft to fire, then external
                # poller writes .continue, then we finish cleanly.
                for _ in range(200):
                    if marker.exists():
                        cont.write_text("ok")
                        break
                    await asyncio.sleep(0.01)
                # Wait for watchdog to consume the .continue marker
                for _ in range(200):
                    if not cont.exists():
                        break
                    await asyncio.sleep(0.01)
                return "completed-after-resume"

            async def no_snapshot(*_a, **_kw):
                return {}

            mock_client = MagicMock()
            mock_client.disconnect = AsyncMock()

            with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
                 patch("sdk.agent_dispatch._git_touched_snapshot", no_snapshot), \
                 patch.object(dispatcher, "_run_client", stall_then_continue):
                result = self._run(dispatcher.query(
                    agent="implementer", prompt="do it", model="opus",
                ))

            self.assertEqual(result, "completed-after-resume")
            stall_events = [e for e in events if isinstance(e, AgentStalled)]
            resume_events = [e for e in events if isinstance(e, AgentResumed)]
            self.assertGreaterEqual(len(stall_events), 1)
            self.assertGreaterEqual(len(resume_events), 1)
            self.assertEqual(resume_events[0].via, "continue_marker")
            # All markers cleaned up on success
            self.assertFalse(marker.exists())
            self.assertFalse(cont.exists())

    def test_kill_marker_forces_immediate_cancel_with_user_message(self):
        """`.kill` cancels the SDK call and the error message names the user."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            jobs_dir = Path(tmp) / ".ai" / "runs" / "run-z" / "jobs"
            jobs_dir.mkdir(parents=True)
            kill = jobs_dir / "job-stage-1-feedface.implementer.kill"

            dispatcher = self._make_dispatcher(
                run_id="run-z", job_id="job-stage-1-feedface", cwd=tmp,
            )
            # Hard threshold large so the only way to die is the .kill marker.
            dispatcher.AGENT_IDLE_SOFT_TIMEOUT = {"implementer": 0.05}
            dispatcher.AGENT_IDLE_HARD_TIMEOUT = {"implementer": 5.0}
            dispatcher.IDLE_CHECK_INTERVAL = 0.02

            async def write_kill_then_stall(client, agent, prompt, model_id, *, on_activity=None, **_kw):
                # Wait briefly so query() establishes the watchdog loop, then
                # plant the kill marker and never return.
                await asyncio.sleep(0.03)
                kill.write_text("ok")
                await asyncio.sleep(10)

            async def no_files(*_a, **_kw):
                return []

            async def no_snapshot(*_a, **_kw):
                return {}

            mock_client = MagicMock()
            mock_client.disconnect = AsyncMock()

            with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
                 patch("sdk.agent_dispatch._git_touched_files", no_files), \
                 patch("sdk.agent_dispatch._git_touched_snapshot", no_snapshot), \
                 patch.object(dispatcher, "_run_client", write_kill_then_stall):
                with self.assertRaises(RuntimeError) as ctx:
                    self._run(dispatcher.query(
                        agent="implementer", prompt="do it", model="opus",
                    ))

            msg = str(ctx.exception)
            self.assertIn("user", msg.lower())
            self.assertIn(".kill", msg)
            # Kill marker is consumed by the watchdog
            self.assertFalse(kill.exists())

    def test_self_recovery_clears_soft_state_via_tool_activity(self):
        """A stalled agent that resumes via a tool call clears the marker + emits AgentResumed."""
        import tempfile
        from sdk.events import AgentStalled, AgentResumed

        with tempfile.TemporaryDirectory() as tmp:
            jobs_dir = Path(tmp) / ".ai" / "runs" / "run-w" / "jobs"
            jobs_dir.mkdir(parents=True)
            marker = jobs_dir / "job-stage-1-abadcafe.implementer.stalled"

            dispatcher = self._make_dispatcher(
                run_id="run-w", job_id="job-stage-1-abadcafe", cwd=tmp,
            )
            dispatcher.AGENT_IDLE_SOFT_TIMEOUT = {"implementer": 0.05}
            dispatcher.AGENT_IDLE_HARD_TIMEOUT = {"implementer": 5.0}
            dispatcher.IDLE_CHECK_INTERVAL = 0.02

            events: list = []
            original_emit = dispatcher.bus.emit

            async def capturing_emit(event):
                events.append(event)
                await original_emit(event)

            dispatcher.bus.emit = capturing_emit

            class FakeClient:
                def __init__(self, options):
                    self.options = options

                async def disconnect(self):
                    return None

            async def stall_then_use_tool(client, agent, prompt, model_id, *, on_activity=None, **_kw):
                # Idle past soft so marker is written
                for _ in range(40):
                    if marker.exists():
                        break
                    await asyncio.sleep(0.01)
                # Then "use a tool" — PreToolUse + PostToolUse fire activity
                pre_tool = client.options.hooks["PreToolUse"][0].hooks[0]
                post_tool = client.options.hooks["PostToolUse"][0].hooks[0]
                await pre_tool({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, None, None)
                await asyncio.sleep(0.02)
                await post_tool({"tool_name": "Bash", "tool_response": "hi"}, None, None)
                # Brief settle for watchdog to observe + emit AgentResumed
                await asyncio.sleep(0.05)
                return "ok"

            async def no_snapshot(*_a, **_kw):
                return {}

            with patch("sdk.agent_dispatch.ClaudeSDKClient", side_effect=FakeClient), \
                 patch("sdk.agent_dispatch._git_touched_snapshot", no_snapshot), \
                 patch.object(dispatcher, "_run_client", stall_then_use_tool):
                result = self._run(dispatcher.query(
                    agent="implementer", prompt="do it", model="opus",
                ))

            self.assertEqual(result, "ok")
            stall_events = [e for e in events if isinstance(e, AgentStalled)]
            resume_events = [e for e in events if isinstance(e, AgentResumed)]
            self.assertGreaterEqual(len(stall_events), 1)
            self.assertGreaterEqual(len(resume_events), 1)
            # via must reflect tool-driven recovery, not user marker
            self.assertEqual(resume_events[-1].via, "self_recovered")
            self.assertFalse(marker.exists())

    def test_concurrent_queries_use_agent_scoped_control_markers(self):
        """A .continue for one verifier must not be consumed by its sibling."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            jobs_dir = Path(tmp) / ".ai" / "runs" / "run-v" / "jobs"
            jobs_dir.mkdir(parents=True)
            test_marker = jobs_dir / "job-verify-123.test-engineer.stalled"
            runtime_marker = jobs_dir / "job-verify-123.runtime-verifier.stalled"
            test_continue = jobs_dir / "job-verify-123.test-engineer.continue"
            runtime_continue = jobs_dir / "job-verify-123.runtime-verifier.continue"

            dispatcher = self._make_dispatcher(
                run_id="run-v", job_id="job-verify-123", cwd=tmp,
            )
            dispatcher.AGENT_IDLE_SOFT_TIMEOUT = {
                "test-engineer": 0.05,
                "runtime-verifier": 0.05,
            }
            dispatcher.AGENT_IDLE_HARD_TIMEOUT = {
                "test-engineer": 5.0,
                "runtime-verifier": 5.0,
            }
            dispatcher.IDLE_CHECK_INTERVAL = 0.02

            events: list = []
            original_emit = dispatcher.bus.emit

            async def capturing_emit(event):
                events.append(event)
                await original_emit(event)

            dispatcher.bus.emit = capturing_emit

            async def wait_for(path: Path) -> None:
                for _ in range(200):
                    if path.exists():
                        return
                    await asyncio.sleep(0.01)
                self.fail(f"timed out waiting for {path.name}")

            async def scoped_stall(client, agent, prompt, model_id, *, on_activity=None, **_kw):
                if agent == "test-engineer":
                    await wait_for(test_marker)
                    await wait_for(runtime_marker)
                    test_continue.write_text("ok")
                    for _ in range(200):
                        if not test_continue.exists():
                            break
                        await asyncio.sleep(0.01)
                    return "test resumed"

                if agent == "runtime-verifier":
                    await wait_for(runtime_marker)
                    await wait_for(test_marker)
                    for _ in range(200):
                        if not test_continue.exists():
                            break
                        await asyncio.sleep(0.01)
                    self.assertFalse(runtime_continue.exists())
                    return "runtime untouched"

                self.fail(f"unexpected agent {agent}")

            async def no_snapshot(*_a, **_kw):
                return {}

            mock_client = MagicMock()
            mock_client.disconnect = AsyncMock()

            async def race():
                return await asyncio.gather(
                    dispatcher.query("test-engineer", "test it", model="sonnet"),
                    dispatcher.query("runtime-verifier", "verify it", model="opus"),
                )

            with patch("sdk.agent_dispatch.ClaudeSDKClient", return_value=mock_client), \
                 patch("sdk.agent_dispatch._git_touched_snapshot", no_snapshot), \
                 patch.object(dispatcher, "_run_client", scoped_stall):
                result = self._run(race())

            self.assertEqual(result, ["test resumed", "runtime untouched"])
            resume_events = [
                e for e in events
                if isinstance(e, AgentResumed) and e.via == "continue_marker"
            ]
            self.assertEqual([e.agent for e in resume_events], ["test-engineer"])
            self.assertFalse(test_marker.exists())
            self.assertFalse(runtime_marker.exists())


if __name__ == "__main__":
    unittest.main()
