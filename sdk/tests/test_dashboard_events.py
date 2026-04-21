"""Tests for dashboard phase/stage event emissions.

Motivation: the dashboard's phase bar (Boot/Plan/Sprint/Wrap) and stage row
stayed stuck on pending across entire runs because:

1. `StageCompleted` was never emitted — the handler at index.html:389 was dead
   code. A stage could only transition pending → active (via a stage.changed
   retry), never to done.
2. `PhaseStarted`/`PhaseCompleted` were never emitted by any command — the
   `cmd_mark` helper exists but team-lead never calls it, so the four phase
   circles stayed ○ for the entire run.

These tests verify that every orchestrator command now emits the right
phase/stage events so the dashboard mirrors actual run state.

Runs with stdlib unittest:
    python3 -m unittest sdk.tests.test_dashboard_events -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk import commands as sdk_cmds  # noqa: E402
from sdk.commands import (  # noqa: E402
    cmd_plan,
    cmd_plan_status,
    cmd_run_complete,
    cmd_run_job,
    cmd_run_start,
    cmd_verify,
)
from sdk.events import EventBus, Stage  # noqa: E402
from sdk.job_runner import JobResult  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _capture_bus_patch():
    """Return (patch_obj, captured_list) for intercepting _setup_bus.

    The captured list receives each EventBus instance created by the
    command under test, in call order. Tests inspect `bus.get_events()`
    afterward to verify which events reached the bus log.
    """
    captured: list[EventBus] = []

    async def fake_setup_bus(run_id, dashboard_url, job_id="", interactive=False, cwd=""):
        bus = EventBus(run_id=run_id, interactive=interactive)
        captured.append(bus)
        return bus, None

    return patch.object(sdk_cmds, "_setup_bus", fake_setup_bus), captured


def _event_types(bus: EventBus) -> list[str]:
    return [e["type"] for e in bus.get_events()]


def _events_of_type(bus: EventBus, event_type: str) -> list[dict]:
    return [e for e in bus.get_events() if e["type"] == event_type]


class TeardownFlushTests(unittest.TestCase):
    """Command teardown waits for queued dashboard sends before disconnecting."""

    def test_teardown_drains_subscribers_before_disconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            order: list[str] = []

            class FakeEmitter:
                async def disconnect(self):
                    order.append("disconnect")

            async def fake_setup_bus(run_id, dashboard_url, job_id="", interactive=False, cwd=""):
                bus = EventBus(run_id=run_id, interactive=interactive)

                async def subscriber(ev):
                    if ev.type == "phase.completed" and ev.phase == "boot":
                        await asyncio.sleep(0.01)
                        order.append("phase.completed")

                bus.subscribe(subscriber)
                return bus, FakeEmitter()

            with patch.object(sdk_cmds, "_setup_bus", fake_setup_bus):
                _run(cmd_run_start("run-flush-test", tmp, None))

            self.assertEqual(order, ["phase.completed", "disconnect"])


# ---------------------------------------------------------------------------
# cmd_run_start — boot phase bracket
# ---------------------------------------------------------------------------


class RunStartPhaseEventsTests(unittest.TestCase):
    """run_start emits boot.started + boot.completed so the phase bar moves."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = self._tmp.name
        self.run_id = "run-boot-test"

    def tearDown(self):
        self._tmp.cleanup()

    def test_boot_phase_is_bracketed(self):
        patch_obj, captured = _capture_bus_patch()
        with patch_obj:
            _run(cmd_run_start(self.run_id, self.cwd, None))

        self.assertTrue(captured, "no bus captured")
        types = _event_types(captured[0])
        # RunStarted fires first so boot is nested inside the run, and the
        # pair always ships together — boot has no async work to pause on.
        self.assertIn("run.started", types)
        self.assertIn("phase.started", types)
        self.assertIn("phase.completed", types)

        started = _events_of_type(captured[0], "phase.started")
        completed = _events_of_type(captured[0], "phase.completed")
        self.assertTrue(any(e.get("phase") == "boot" for e in started))
        self.assertTrue(any(e.get("phase") == "boot" for e in completed))


# ---------------------------------------------------------------------------
# cmd_plan / cmd_plan_status — plan phase close
# ---------------------------------------------------------------------------


class PlanPhaseEventsTests(unittest.TestCase):
    """cmd_plan emits phase.completed(plan) only on a terminal codex verdict.

    A PENDING verdict (codex still running) must leave the phase active so
    the dashboard doesn't flip plan to done before the review actually lands.
    cmd_plan_status picks it up later when codex finishes.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)
        self.run_id = "run-plan-test"
        self.run_dir = self.cwd / ".ai" / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "plan.md").write_text("## Plan\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _plan_completed_phases(self, bus: EventBus) -> list[str]:
        return [
            e.get("phase")
            for e in _events_of_type(bus, "phase.completed")
            if e.get("phase") == "plan"
        ]

    def test_terminal_pass_closes_plan_phase(self):
        class FakeDispatcher:
            def __init__(self, *a, **kw):
                pass

            async def run_codex_plan_review(self, plan_text, *, resume_thread_id=None):
                return {
                    "status": "completed",
                    "has_major_issues": False,
                    "summary": "clean",
                    "findings": [],
                    "next_steps": [],
                    "output": "{}",
                }

        patch_obj, captured = _capture_bus_patch()
        with patch_obj, \
             patch("sdk.orchestrator._parse_plan_stages", return_value=[
                 Stage(name="S1", has_user_facing_changes=False, files=["a.ts"]),
             ]), \
             patch("sdk.agent_dispatch.AgentDispatcher", FakeDispatcher):
            _run(cmd_plan(str(self.cwd), self.run_id, None))

        self.assertTrue(captured)
        self.assertEqual(self._plan_completed_phases(captured[0]), ["plan"])

    def test_pending_leaves_plan_phase_active(self):
        class FakeDispatcher:
            def __init__(self, *a, **kw):
                pass

            async def run_codex_plan_review(self, plan_text, *, resume_thread_id=None):
                # Codex still running — cmd_plan records PENDING.
                return {
                    "status": "running",
                    "has_major_issues": False,
                    "summary": "",
                    "findings": [],
                    "next_steps": [],
                    "output": "",
                    "job_id": "task-pending",
                    "thread_id": "thread-pending",
                }

        patch_obj, captured = _capture_bus_patch()
        with patch_obj, \
             patch("sdk.orchestrator._parse_plan_stages", return_value=[
                 Stage(name="S1", has_user_facing_changes=False, files=["a.ts"]),
             ]), \
             patch("sdk.agent_dispatch.AgentDispatcher", FakeDispatcher):
            _run(cmd_plan(str(self.cwd), self.run_id, None))

        self.assertTrue(captured)
        # plan.completed would be a lie — codex is still running.
        self.assertEqual(self._plan_completed_phases(captured[0]), [])

    def test_plan_status_finalizes_pending_phase(self):
        (self.run_dir / "plan.json").write_text(json.dumps({
            "plan_file": "plan.md",
            "stages": [],
            "codex_review": {
                "status": "running",
                "has_major_issues": False,
                "job_id": "task-123",
                "thread_id": "thread-123",
            },
        }))

        class FakeDispatcher:
            def __init__(self, *a, **kw):
                pass

            async def fetch_codex_plan_review_result(self, job_id):
                return {
                    "status": "completed",
                    "has_major_issues": False,
                    "summary": "clean",
                    "findings": [],
                    "next_steps": [],
                    "output": "{}",
                    "job_id": job_id,
                    "thread_id": "thread-123",
                }

        patch_obj, captured = _capture_bus_patch()
        with patch_obj, patch("sdk.agent_dispatch.AgentDispatcher", FakeDispatcher):
            _run(cmd_plan_status(str(self.cwd), self.run_id, None))

        self.assertTrue(captured)
        self.assertEqual(self._plan_completed_phases(captured[0]), ["plan"])


# ---------------------------------------------------------------------------
# cmd_run_job — sprint phase + StageCompleted
# ---------------------------------------------------------------------------


class RunJobStageEventsTests(unittest.TestCase):
    """cmd_run_job emits PhaseStarted(sprint) + StageCompleted per stage.

    StageCompleted was the original bug — the event class existed but
    nothing emitted it, so the dashboard stage row never transitioned
    stages out of pending.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = self._tmp.name
        self.run_id = "run-stage-test"
        run_dir = Path(self.cwd) / ".ai" / "runs" / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self.plan_path = run_dir / "plan.json"
        self.plan_path.write_text(json.dumps({
            "plan_file": "plan.md",
            "stages": [
                {"id": "stage-1", "name": "Stage One", "files": ["a.ts"]},
            ],
        }))

    def tearDown(self):
        self._tmp.cleanup()

    def _run_stage(self, job_status: str) -> EventBus:
        """Drive cmd_run_job with a dispatcher + run_job that returns `job_status`."""

        class FakeDispatcher:
            def __init__(self, *a, **kw):
                pass

            async def query(self, *a, **kw):
                return ""

            async def run_test_engineer(self, *a, **kw):
                return {"passed": 0, "failed": 0, "output": "", "status": "skipped"}

            async def run_codex_review(self):
                return {"status": "skipped", "has_issues": False, "output": ""}

            async def run_runtime_verifier(self, *a, **kw):
                return {"status": "skipped"}

        async def fake_run_job(**kwargs):
            return JobResult(status=job_status)

        patch_obj, captured = _capture_bus_patch()
        with patch_obj, \
             patch("sdk.commands._register_job", return_value=Path(self.cwd) / "fake.lock"), \
             patch("sdk.commands._unregister_job"), \
             patch("sdk.commands._write_job_result"), \
             patch("sdk.commands._git_head", return_value=""), \
             patch("sdk.commands._git_dirty_paths", return_value=[]), \
             patch("sdk.commands._git_commit_stage", return_value={"status": "skipped"}), \
             patch("sdk.commands._load_context", return_value=""), \
             patch("sdk.agent_dispatch.AgentDispatcher", FakeDispatcher), \
             patch("sdk.job_runner.run_job", fake_run_job):
            _run(cmd_run_job(
                stage_id="stage-1",
                plan_path=str(self.plan_path),
                cwd=self.cwd,
                run_id=self.run_id,
                dashboard_url=None,
            ))

        self.assertTrue(captured, "no bus captured for run_job")
        return captured[0]

    def test_pass_emits_phase_started_and_stage_completed(self):
        bus = self._run_stage("PASS")
        types = _event_types(bus)
        self.assertIn("phase.started", types)
        self.assertIn("stage.changed", types)
        self.assertIn("stage.completed", types)

        sprint_starts = [
            e for e in _events_of_type(bus, "phase.started") if e.get("phase") == "sprint"
        ]
        self.assertEqual(len(sprint_starts), 1, "expected exactly one sprint start")

        completed = _events_of_type(bus, "stage.completed")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["stage_name"], "Stage One")
        self.assertEqual(completed[0]["status"], "PASS")

    def test_blocked_still_emits_stage_completed(self):
        # A BLOCKED stage needs a closing event too — otherwise the dashboard
        # stage row would stick on 'active' forever.
        bus = self._run_stage("BLOCKED")
        completed = _events_of_type(bus, "stage.completed")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["status"], "BLOCKED")

    def test_interrupted_does_not_emit_stage_completed(self):
        # INTERRUPTED means the stage is paused and may resume — we do NOT
        # want the dashboard to show it as done or failed.
        bus = self._run_stage("INTERRUPTED")
        self.assertEqual(_events_of_type(bus, "stage.completed"), [])
        self.assertTrue(_events_of_type(bus, "job.interrupted"))


# ---------------------------------------------------------------------------
# cmd_verify / cmd_run_complete — sprint → wrap transition
# ---------------------------------------------------------------------------


class WrapPhaseEventsTests(unittest.TestCase):
    """Wrap-phase commands close sprint and open wrap; run_complete closes wrap."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = self._tmp.name
        self.run_id = "run-wrap-test"
        run_dir = Path(self.cwd) / ".ai" / "runs" / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "jobs").mkdir(exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_verify_transitions_sprint_to_wrap(self):
        class FakeDispatcher:
            def __init__(self, *a, **kw):
                pass

            async def run_test_engineer(self, *a, **kw):
                return {"passed": 0, "failed": 0, "output": "", "status": "skipped"}

            async def run_codex_review(self):
                return {"status": "skipped", "has_issues": False, "output": ""}

            async def run_runtime_verifier(self, *a, **kw):
                return {"status": "skipped"}

        patch_obj, captured = _capture_bus_patch()
        with patch_obj, \
             patch("sdk.commands._register_job", return_value=Path(self.cwd) / "fake.lock"), \
             patch("sdk.commands._unregister_job"), \
             patch("sdk.commands._write_job_result"), \
             patch("sdk.commands._supersede_prior_jobs"), \
             patch("sdk.commands._worktree_snapshot", return_value={}), \
             patch("sdk.commands._diff_snapshots", return_value=[]), \
             patch("sdk.agent_dispatch.AgentDispatcher", FakeDispatcher):
            _run(cmd_verify(
                cwd=self.cwd, run_id=self.run_id, dashboard_url=None,
                agents={"test"}, scope="",
            ))

        self.assertTrue(captured)
        bus = captured[0]
        sprint_done = [
            e for e in _events_of_type(bus, "phase.completed") if e.get("phase") == "sprint"
        ]
        wrap_started = [
            e for e in _events_of_type(bus, "phase.started") if e.get("phase") == "wrap"
        ]
        self.assertEqual(len(sprint_done), 1)
        self.assertEqual(len(wrap_started), 1)

    def test_run_complete_closes_wrap(self):
        # run_complete must emit wrap.completed after RunCompleted — that's
        # the last signal the dashboard uses to mark the whole run done.
        patch_obj, captured = _capture_bus_patch()
        with patch_obj, \
             patch("sdk.commands._ensure_wrap_jobs", new_callable=AsyncMock), \
             patch("sdk.commands._fetch_events_from_dashboard", return_value=[]), \
             patch("sdk.commands._cleanup_after_run"):
            _run(cmd_run_complete(self.run_id, self.cwd, None))

        self.assertTrue(captured)
        bus = captured[0]
        types = _event_types(bus)

        self.assertIn("run.completed", types)
        wrap_completed = [
            e for e in _events_of_type(bus, "phase.completed") if e.get("phase") == "wrap"
        ]
        self.assertEqual(len(wrap_completed), 1)

        # The sprint→wrap transition also fires at the start of run_complete
        # as a safety net in case verify/review/document were skipped.
        sprint_completed = [
            e for e in _events_of_type(bus, "phase.completed") if e.get("phase") == "sprint"
        ]
        wrap_started = [
            e for e in _events_of_type(bus, "phase.started") if e.get("phase") == "wrap"
        ]
        self.assertEqual(len(sprint_completed), 1)
        self.assertEqual(len(wrap_started), 1)


if __name__ == "__main__":
    unittest.main()
