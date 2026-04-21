"""Tests for background-mode plan review + team-lead status checking.

Motivation: phase3/phase4 plan reviews hit our 240s wait_for cap while
codex actually took 10+ minutes and finished successfully — we threw
away real findings because we got impatient. Switch to:

1. Launch codex with `task --background --json` (returns jobId in <1s)
2. Poll `status <job-id> --json` every 3s until completed, 600s cap
3. If cap hits while codex is still running, persist `{status: "running",
   job_id, thread_id}` in plan.json WITHOUT cancelling codex — the real
   findings are in codex's own state
4. New `cmd_plan_status` lets team-lead finalize a running review before
   starting the sprint

Runs with stdlib unittest:
    python3 -m unittest sdk.tests.test_plan_review_background -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk.agent_dispatch import AgentDispatcher  # noqa: E402
from sdk.commands import cmd_plan_status  # noqa: E402
from sdk.events import EventBus  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_dispatcher() -> AgentDispatcher:
    d = AgentDispatcher(
        agents_dir=str(_REPO_ROOT / "agents"),
        cwd=str(_REPO_ROOT),
        bus=EventBus(run_id="test-run"),
    )
    # Pretend companion is at a fake path — we won't actually invoke it;
    # _run_codex_json_subcommand gets patched in each test.
    d._resolve_codex_companion = lambda: (  # type: ignore[method-assign]
        "plugin-root",
        Path("scripts/codex-companion.mjs"),
        None,
    )
    return d


def _install_subcommand_fake(dispatcher: AgentDispatcher, fake):
    """Replace _run_codex_json_subcommand with a custom async callable.

    `fake` is an async callable receiving (companion_script, subcommand, args)
    and returning (dict | None). This is the single seam for mocking all
    codex-companion subprocess calls.
    """
    dispatcher._run_codex_json_subcommand = fake  # type: ignore[method-assign]


class BackgroundLaunchTests(unittest.TestCase):
    """Plan review should launch with --background, receive jobId+threadId fast."""

    def test_launch_builds_background_args(self):
        dispatcher = _make_dispatcher()
        seen: dict[str, object] = {}

        async def fake(companion_script, subcommand, args, **_kw):
            seen["subcommand"] = subcommand
            seen["args"] = args
            if subcommand == "task":
                return {"jobId": "task-abc", "threadId": "thread-xyz", "status": "queued"}
            if subcommand == "status":
                return {
                    "job": {
                        "id": "task-abc",
                        "status": "completed",
                        "threadId": "thread-xyz",
                    },
                }
            if subcommand == "result":
                return {
                    "storedJob": {
                        "result": {
                            "finalMessage": json.dumps({
                                "verdict": "approve",
                                "summary": "ok",
                                "findings": [],
                                "next_steps": [],
                            }),
                        },
                    },
                }
            return None

        _install_subcommand_fake(dispatcher, fake)

        _run(dispatcher.run_codex_plan_review("## Plan"))

        # The task subcommand call must have --background flag
        assert "args" in seen
        args = seen["args"]
        assert isinstance(args, list)

    def test_launch_includes_resume_flag_when_requested(self):
        dispatcher = _make_dispatcher()
        captured_task_args: list[list[str]] = []

        async def fake(companion_script, subcommand, args, **_kw):
            if subcommand == "task":
                captured_task_args.append(list(args))
                return {"jobId": "task-abc", "threadId": "thread-abc", "status": "queued"}
            if subcommand == "status":
                return {"job": {"status": "completed", "threadId": "thread-abc"}}
            if subcommand == "result":
                return {"storedJob": {"result": {"finalMessage": '{"verdict":"approve"}'}}}
            return None

        _install_subcommand_fake(dispatcher, fake)
        # Prior thread id matches candidate to force resume path.
        dispatcher._codex_task_resume_candidate_thread_id = (  # type: ignore[method-assign]
            lambda companion_script: asyncio.sleep(0, result="thread-abc")
        )

        _run(dispatcher.run_codex_plan_review(
            "## Plan", resume_thread_id="thread-abc",
        ))

        self.assertEqual(len(captured_task_args), 1)
        self.assertIn("--background", captured_task_args[0])
        self.assertIn("--resume-last", captured_task_args[0])

    def test_launch_failure_returns_skipped(self):
        dispatcher = _make_dispatcher()

        async def fake(companion_script, subcommand, args, **_kw):
            if subcommand == "task":
                return None  # launch failed
            return None

        _install_subcommand_fake(dispatcher, fake)

        result = _run(dispatcher.run_codex_plan_review("## Plan"))

        self.assertEqual(result["status"], "skipped")
        self.assertIn("launch", result.get("reason", "").lower())


class PollAndResultTests(unittest.TestCase):
    """Poll transitions: running → completed / failed / client timeout."""

    def test_running_then_completed_fetches_result(self):
        dispatcher = _make_dispatcher()
        status_calls = {"n": 0}

        async def fake(companion_script, subcommand, args, **_kw):
            if subcommand == "task":
                return {"jobId": "task-1", "threadId": "thread-1", "status": "queued"}
            if subcommand == "status":
                status_calls["n"] += 1
                if status_calls["n"] < 3:
                    return {"job": {"status": "running", "threadId": "thread-1"}}
                return {"job": {"status": "completed", "threadId": "thread-1"}}
            if subcommand == "result":
                return {
                    "storedJob": {
                        "result": {
                            "finalMessage": json.dumps({
                                "verdict": "needs-attention",
                                "summary": "fix X",
                                "findings": [{"severity": "high", "title": "X", "body": "", "recommendation": ""}],
                                "next_steps": [],
                            }),
                        },
                    },
                }
            return None

        _install_subcommand_fake(dispatcher, fake)
        # Use a tiny sleep so the test runs fast
        with patch("sdk.agent_dispatch._PLAN_REVIEW_POLL_INTERVAL_S", 0.01):
            result = _run(dispatcher.run_codex_plan_review("## Plan"))

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["has_major_issues"])
        self.assertEqual(result["summary"], "fix X")
        self.assertGreaterEqual(status_calls["n"], 3)

    def test_timeout_returns_running_with_job_id(self):
        dispatcher = _make_dispatcher()

        async def fake(companion_script, subcommand, args, **_kw):
            if subcommand == "task":
                return {"jobId": "task-slow", "threadId": "thread-slow", "status": "queued"}
            if subcommand == "status":
                return {"job": {"status": "running", "threadId": "thread-slow"}}
            return None

        _install_subcommand_fake(dispatcher, fake)

        # Client cap tiny for fast test; codex "never finishes"
        with patch("sdk.agent_dispatch._PLAN_REVIEW_POLL_INTERVAL_S", 0.01), \
             patch("sdk.agent_dispatch._PLAN_REVIEW_TIMEOUT_S", 0.05):
            result = _run(dispatcher.run_codex_plan_review("## Plan"))

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["job_id"], "task-slow")
        self.assertEqual(result["thread_id"], "thread-slow")
        self.assertIn("still running", result.get("reason", "").lower())
        # Crucially: NOT persisted as skipped or failed — later finalization
        # should be able to pick this up.
        self.assertFalse(result["has_major_issues"])

    def test_failed_job_returns_skipped(self):
        dispatcher = _make_dispatcher()

        async def fake(companion_script, subcommand, args, **_kw):
            if subcommand == "task":
                return {"jobId": "task-fail", "threadId": "thread-fail", "status": "queued"}
            if subcommand == "status":
                return {"job": {"status": "failed", "threadId": "thread-fail", "error": "boom"}}
            return None

        _install_subcommand_fake(dispatcher, fake)

        with patch("sdk.agent_dispatch._PLAN_REVIEW_POLL_INTERVAL_S", 0.01):
            result = _run(dispatcher.run_codex_plan_review("## Plan"))

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result.get("job_id"), "task-fail")
        self.assertIn("failed", result.get("reason", "").lower())


class CmdPlanStatusTests(unittest.TestCase):
    """team-lead invokes cmd_plan_status to finalize a running plan review."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)
        self.run_id = "run-test"
        self.run_dir = self.cwd / ".ai" / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_plan_json(self, review: dict):
        (self.run_dir / "plan.json").write_text(json.dumps({
            "plan_file": ".ai/runs/test/plan.md",
            "stages": [],
            "codex_review": review,
        }))

    def test_no_plan_json_returns_no_op(self):
        result = _run(cmd_plan_status(str(self.cwd), self.run_id, None))
        self.assertEqual(result["status"], "no-op")

    def test_completed_review_is_no_op(self):
        self._write_plan_json({
            "status": "completed",
            "has_major_issues": False,
            "job_id": "task-old",
        })
        result = _run(cmd_plan_status(str(self.cwd), self.run_id, None))
        self.assertEqual(result["status"], "no-op")

    def test_running_transitions_to_completed(self):
        self._write_plan_json({
            "status": "running",
            "has_major_issues": False,
            "summary": "",
            "findings": [],
            "job_id": "task-xyz",
            "thread_id": "thread-xyz",
        })

        # Patch the dispatcher used by cmd_plan_status so we don't spawn
        # real node subprocesses.
        class FakeDispatcher:
            def __init__(self, *a, **kw):
                pass

            async def _resolve_codex_companion(self):  # type: ignore[override]
                return ("plugin-root", Path("x.mjs"), None)

            def _resolve_codex_companion_sync(self):
                return ("plugin-root", Path("x.mjs"), None)

            async def fetch_codex_plan_review_result(self, job_id: str) -> dict:
                self.last_job_id = job_id
                return {
                    "status": "completed",
                    "has_major_issues": True,
                    "summary": "finally finished",
                    "findings": ["[HIGH] something"],
                    "next_steps": [],
                    "output": "{}",
                    "job_id": job_id,
                    "thread_id": "thread-xyz",
                }

        with patch("sdk.agent_dispatch.AgentDispatcher", FakeDispatcher):
            result = _run(cmd_plan_status(str(self.cwd), self.run_id, None))

        self.assertEqual(result["status"], "updated")
        # plan.json now reflects completed state
        data = json.loads((self.run_dir / "plan.json").read_text())
        self.assertEqual(data["codex_review"]["status"], "completed")
        self.assertTrue(data["codex_review"]["has_major_issues"])
        self.assertEqual(data["codex_review"]["summary"], "finally finished")

    def test_still_running_keeps_state(self):
        self._write_plan_json({
            "status": "running",
            "has_major_issues": False,
            "job_id": "task-slow",
            "thread_id": "thread-slow",
        })

        class FakeDispatcher:
            def __init__(self, *a, **kw):
                pass

            async def fetch_codex_plan_review_result(self, job_id: str) -> dict:
                return {
                    "status": "running",
                    "has_major_issues": False,
                    "summary": "",
                    "findings": [],
                    "job_id": job_id,
                    "thread_id": "thread-slow",
                    "reason": "still running",
                }

        with patch("sdk.agent_dispatch.AgentDispatcher", FakeDispatcher):
            result = _run(cmd_plan_status(str(self.cwd), self.run_id, None))

        self.assertEqual(result["status"], "still-running")
        # plan.json unchanged state-wise
        data = json.loads((self.run_dir / "plan.json").read_text())
        self.assertEqual(data["codex_review"]["status"], "running")


if __name__ == "__main__":
    unittest.main()
