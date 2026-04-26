"""Tests for background-mode plan review + team-lead status checking.

Motivation: phase3/phase4 plan reviews hit our 240s wait_for cap while
codex actually took 10+ minutes and finished successfully — we threw
away real findings because we got impatient. Switch to:

1. Launch codex with `task --background --json` (returns jobId in <1s)
2. Poll `status <job-id> --json` every 3s until completed, 600s cap
3. Run a second background audit pass that searches for missed blocking
   findings before the user spends another revision cycle
4. If cap hits while codex is still running, persist `{status: "running",
   job_id, thread_id}` in plan.json WITHOUT cancelling codex — the real
   findings are in codex's own state
5. New `cmd_plan_status` lets team-lead finalize a running review before
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
from sdk.commands import (  # noqa: E402
    _classify_run_state,
    _plan_status_from_codex_review,
    cmd_plan,
    cmd_plan_status,
)
from sdk.events import EventBus, Stage  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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

        self.assertGreaterEqual(len(captured_task_args), 1)
        self.assertIn("--background", captured_task_args[0])
        self.assertIn("--write", captured_task_args[0])
        self.assertIn("--resume-last", captured_task_args[0])

    def test_launch_uses_write_sandbox_for_review_thread(self):
        """Review is logically read-only but needs a write-enabled thread.

        The follow-up plan-fix turn resumes the review thread to preserve
        context. If the original thread is read-only, Codex cannot apply the
        fix even when the fix job passes --write.
        """
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

        _run(dispatcher.run_codex_plan_review("## Plan"))

        self.assertGreaterEqual(len(captured_task_args), 1)
        for args in captured_task_args:
            self.assertIn("--background", args)
            self.assertIn("--write", args)

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
        self.assertIn("fix X", result["summary"])
        self.assertGreaterEqual(status_calls["n"], 3)

    def test_second_pass_adds_missed_blocking_findings(self):
        dispatcher = _make_dispatcher()
        task_ids = ["task-initial", "task-audit"]
        result_payloads = [
            {
                "verdict": "approve",
                "summary": "first pass saw no blockers",
                "findings": [],
                "next_steps": [],
            },
            {
                "verdict": "needs-attention",
                "summary": "audit found a missed dependency",
                "findings": [{
                    "severity": "high",
                    "title": "Missing dependency",
                    "body": "Stage 2 uses the API before Stage 1 creates it.",
                    "recommendation": "Add a Stage 2 dependency on Stage 1.",
                }],
                "next_steps": ["Add dependency edge"],
            },
        ]
        calls = {"task": 0, "result": 0}

        async def fake(companion_script, subcommand, args, **_kw):
            if subcommand == "task":
                job_id = task_ids[calls["task"]]
                calls["task"] += 1
                return {"jobId": job_id, "threadId": "thread-review", "status": "queued"}
            if subcommand == "status":
                job_id = args[0]
                return {"job": {"id": job_id, "status": "completed", "threadId": "thread-review"}}
            if subcommand == "result":
                payload = result_payloads[calls["result"]]
                calls["result"] += 1
                return {"storedJob": {"result": {"finalMessage": json.dumps(payload)}}}
            return None

        _install_subcommand_fake(dispatcher, fake)
        dispatcher._codex_task_resume_candidate_thread_id = (  # type: ignore[method-assign]
            lambda companion_script: asyncio.sleep(0, result="thread-review")
        )

        result = _run(dispatcher.run_codex_plan_review("## Plan"))

        self.assertEqual(calls["task"], 2)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["has_major_issues"])
        self.assertEqual(result["audit_job_id"], "task-audit")
        self.assertTrue(any("Missing dependency" in f for f in result["findings"]))

    def test_audit_approve_preserves_initial_findings(self):
        """Most common needs-attention path: audit finds no extra blockers."""
        dispatcher = _make_dispatcher()
        task_ids = ["task-initial", "task-audit"]
        result_payloads = [
            {
                "verdict": "needs-attention",
                "summary": "stage 1 is vague",
                "findings": [{
                    "severity": "high",
                    "title": "Vague success criteria",
                    "body": "Stage 1 cannot be verified objectively.",
                    "recommendation": "Add concrete observable outcomes.",
                }],
                "next_steps": ["Tighten Stage 1"],
            },
            {
                "verdict": "approve",
                "summary": "no additional blocking issues",
                "findings": [],
                "next_steps": [],
            },
        ]
        calls = {"task": 0, "result": 0}

        async def fake(companion_script, subcommand, args, **_kw):
            if subcommand == "task":
                job_id = task_ids[calls["task"]]
                calls["task"] += 1
                return {"jobId": job_id, "threadId": "thread-review", "status": "queued"}
            if subcommand == "status":
                job_id = args[0]
                return {"job": {"id": job_id, "status": "completed", "threadId": "thread-review"}}
            if subcommand == "result":
                payload = result_payloads[calls["result"]]
                calls["result"] += 1
                return {"storedJob": {"result": {"finalMessage": json.dumps(payload)}}}
            return None

        _install_subcommand_fake(dispatcher, fake)
        dispatcher._codex_task_resume_candidate_thread_id = (  # type: ignore[method-assign]
            lambda companion_script: asyncio.sleep(0, result="thread-review")
        )

        result = _run(dispatcher.run_codex_plan_review("## Plan"))

        self.assertEqual(calls["task"], 2)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["has_major_issues"])
        self.assertEqual(result["audit_status"], "completed")
        self.assertEqual(len(result["findings"]), 1)
        self.assertIn("Vague success criteria", result["findings"][0])
        self.assertIn("no additional blocking issues", result["summary"])

    def test_audit_timeout_returns_resumable_audit_state(self):
        dispatcher = _make_dispatcher()
        calls = {"task": 0}

        async def fake(companion_script, subcommand, args, **_kw):
            if subcommand == "task":
                calls["task"] += 1
                job_id = "task-initial" if calls["task"] == 1 else "task-audit"
                return {"jobId": job_id, "threadId": "thread-review", "status": "queued"}
            if subcommand == "status":
                job_id = args[0]
                status = "completed" if job_id == "task-initial" else "running"
                return {"job": {"id": job_id, "status": status, "threadId": "thread-review"}}
            if subcommand == "result":
                return {
                    "storedJob": {
                        "result": {
                            "finalMessage": json.dumps({
                                "verdict": "approve",
                                "summary": "initial clean",
                                "findings": [],
                                "next_steps": [],
                            }),
                        },
                    },
                }
            return None

        _install_subcommand_fake(dispatcher, fake)

        with patch("sdk.agent_dispatch._PLAN_REVIEW_POLL_INTERVAL_S", 0.01), \
             patch("sdk.agent_dispatch._PLAN_REVIEW_TIMEOUT_S", 0.05):
            result = _run(dispatcher.run_codex_plan_review("## Plan"))

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["phase"], "audit")
        self.assertEqual(result["job_id"], "task-audit")
        self.assertIn("initial_review", result)

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

    def test_failed_job_reason_uses_summary_when_error_fields_absent(self):
        dispatcher = _make_dispatcher()

        async def fake(companion_script, subcommand, args, **_kw):
            if subcommand == "task":
                return {"jobId": "task-fail", "threadId": "thread-fail", "status": "queued"}
            if subcommand == "status":
                return {
                    "job": {
                        "status": "failed",
                        "threadId": "thread-fail",
                        "summary": "The 'gpt-5.5' model requires a newer version of Codex.",
                    },
                }
            return None

        _install_subcommand_fake(dispatcher, fake)

        with patch("sdk.agent_dispatch._PLAN_REVIEW_POLL_INTERVAL_S", 0.01):
            result = _run(dispatcher.run_codex_plan_review("## Plan"))

        self.assertEqual(result["status"], "skipped")
        self.assertIn("gpt-5.5", result.get("reason", ""))

    def test_fetch_queued_job_keeps_running_state(self):
        dispatcher = _make_dispatcher()
        subcommands: list[str] = []

        async def fake(companion_script, subcommand, args, **_kw):
            subcommands.append(subcommand)
            if subcommand == "status":
                return {"job": {"status": "queued", "threadId": "thread-queued"}}
            if subcommand == "result":
                return {"storedJob": {"result": {"finalMessage": '{"verdict":"approve"}'}}}
            return None

        _install_subcommand_fake(dispatcher, fake)

        result = _run(dispatcher.fetch_codex_plan_review_result("task-queued"))

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["job_id"], "task-queued")
        self.assertEqual(result["thread_id"], "thread-queued")
        self.assertIn("queued", result.get("reason", ""))
        self.assertNotIn("result", subcommands)


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
        jobs_dir = self.run_dir / "jobs"
        jobs_dir.mkdir()
        (jobs_dir / "job-plan-old.json").write_text(json.dumps({
            "command": "plan",
            "status": "PENDING",
        }))

        # Patch the dispatcher used by cmd_plan_status so we don't spawn
        # real node subprocesses.
        class FakeDispatcher:
            def __init__(self, *a, **kw):
                pass

            async def _resolve_codex_companion(self):  # type: ignore[override]
                return ("plugin-root", Path("x.mjs"), None)

            def _resolve_codex_companion_sync(self):
                return ("plugin-root", Path("x.mjs"), None)

            async def fetch_codex_plan_review_result(self, job_id: str, **_kw) -> dict:
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
        classified = _classify_run_state(self.run_dir)
        self.assertEqual(classified["jobs_completed"]["plan"], "REVIEW")

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

            async def fetch_codex_plan_review_result(self, job_id: str, **_kw) -> dict:
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

    def test_completed_clean_review_promotes_pending_plan_to_pass(self):
        self._write_plan_json({
            "status": "running",
            "has_major_issues": False,
            "job_id": "task-clean",
            "thread_id": "thread-clean",
        })
        jobs_dir = self.run_dir / "jobs"
        jobs_dir.mkdir()
        (jobs_dir / "job-plan-old.json").write_text(json.dumps({
            "command": "plan",
            "status": "PENDING",
        }))

        class FakeDispatcher:
            def __init__(self, *a, **kw):
                pass

            async def fetch_codex_plan_review_result(self, job_id: str, **_kw) -> dict:
                return {
                    "status": "completed",
                    "has_major_issues": False,
                    "summary": "clean",
                    "findings": [],
                    "next_steps": [],
                    "output": "{}",
                    "job_id": job_id,
                    "thread_id": "thread-clean",
                }

        with patch("sdk.agent_dispatch.AgentDispatcher", FakeDispatcher):
            result = _run(cmd_plan_status(str(self.cwd), self.run_id, None))

        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["plan_status"], "PASS")
        classified = _classify_run_state(self.run_dir)
        self.assertEqual(classified["jobs_completed"]["plan"], "PASS")
        self.assertEqual(classified["state"], "not_started")


class CmdPlanPendingGateTests(unittest.TestCase):
    """cmd_plan must not publish a PASS plan while codex review is pending."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)
        self.run_id = "run-pending"
        self.run_dir = self.cwd / ".ai" / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "plan.md").write_text("## Placeholder plan\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_running_plan_review_records_pending_not_pass(self):
        class FakeDispatcher:
            def __init__(self, *a, **kw):
                pass

            async def run_codex_plan_review(self, plan_text: str, *, resume_thread_id=None) -> dict:
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

        with patch("sdk.orchestrator._parse_plan_stages", return_value=[
            Stage(name="Stage 1", has_user_facing_changes=False, files=["apps/web/x.ts"]),
        ]), patch("sdk.agent_dispatch.AgentDispatcher", FakeDispatcher):
            _run(cmd_plan(str(self.cwd), self.run_id, None))

        classified = _classify_run_state(self.run_dir)
        self.assertEqual(classified["jobs_completed"]["plan"], "PENDING")
        self.assertEqual(classified["state"], "incomplete")

        plan_json = json.loads((self.run_dir / "plan.json").read_text())
        self.assertEqual(plan_json["codex_review"]["status"], "running")

    def test_running_plan_review_blocks_legacy_pass_job(self):
        (self.run_dir / "plan.json").write_text(json.dumps({
            "plan_file": ".ai/runs/run-pending/plan.md",
            "stages": [{"id": "stage-1", "name": "Stage 1"}],
            "codex_review": {
                "status": "running",
                "has_major_issues": False,
                "job_id": "task-old",
            },
        }))
        jobs_dir = self.run_dir / "jobs"
        jobs_dir.mkdir()
        (jobs_dir / "job-plan-old.json").write_text(json.dumps({
            "command": "plan",
            "status": "PASS",
        }))

        classified = _classify_run_state(self.run_dir)

        self.assertEqual(classified["jobs_completed"]["plan"], "PASS")
        self.assertEqual(classified["state"], "incomplete")

    def test_skipped_plan_review_records_error_not_pass(self):
        class FakeDispatcher:
            def __init__(self, *a, **kw):
                pass

            async def run_codex_plan_review(self, plan_text: str, *, resume_thread_id=None) -> dict:
                return {
                    "status": "skipped",
                    "has_major_issues": False,
                    "summary": "",
                    "findings": [],
                    "next_steps": [],
                    "output": "",
                    "reason": "codex plan review failed: gpt-5.5 requires newer Codex",
                    "job_id": "task-failed",
                    "thread_id": "thread-failed",
                }

        with patch("sdk.orchestrator._parse_plan_stages", return_value=[
            Stage(name="Stage 1", has_user_facing_changes=False, files=["apps/web/x.ts"]),
        ]), patch("sdk.agent_dispatch.AgentDispatcher", FakeDispatcher):
            _run(cmd_plan(str(self.cwd), self.run_id, None))

        classified = _classify_run_state(self.run_dir)
        self.assertEqual(classified["jobs_completed"]["plan"], "ERROR")
        self.assertEqual(classified["state"], "incomplete")

        plan_json = json.loads((self.run_dir / "plan.json").read_text())
        self.assertEqual(plan_json["codex_review"]["status"], "skipped")
        self.assertIn("gpt-5.5", plan_json["codex_review"]["reason"])

    def test_skipped_plan_review_blocks_legacy_pass_job(self):
        (self.run_dir / "plan.json").write_text(json.dumps({
            "plan_file": ".ai/runs/run-pending/plan.md",
            "stages": [{"id": "stage-1", "name": "Stage 1"}],
            "codex_review": {
                "status": "skipped",
                "has_major_issues": False,
                "reason": "codex plan review failed",
                "job_id": "task-old",
            },
        }))
        jobs_dir = self.run_dir / "jobs"
        jobs_dir.mkdir()
        (jobs_dir / "job-plan-old.json").write_text(json.dumps({
            "command": "plan",
            "status": "PASS",
        }))

        classified = _classify_run_state(self.run_dir)

        self.assertEqual(classified["jobs_completed"]["plan"], "PASS")
        self.assertEqual(classified["state"], "incomplete")

    def test_explicit_skip_codex_is_marked_allowed_and_passes(self):
        with patch("sdk.orchestrator._parse_plan_stages", return_value=[
            Stage(name="Stage 1", has_user_facing_changes=False, files=["apps/web/x.ts"]),
        ]):
            _run(cmd_plan(str(self.cwd), self.run_id, None, skip_codex=True))

        classified = _classify_run_state(self.run_dir)
        self.assertEqual(classified["jobs_completed"]["plan"], "PASS")
        self.assertEqual(classified["state"], "not_started")

        plan_json = json.loads((self.run_dir / "plan.json").read_text())
        self.assertEqual(plan_json["codex_review"]["status"], "skipped")
        self.assertTrue(plan_json["codex_review"]["skip_allowed"])

    def test_plan_status_helper_rejects_accidental_skips(self):
        self.assertEqual(
            _plan_status_from_codex_review({
                "status": "skipped",
                "has_major_issues": False,
                "reason": "codex failed",
            }),
            "ERROR",
        )
        self.assertEqual(
            _plan_status_from_codex_review({
                "status": "skipped",
                "has_major_issues": False,
                "skip_allowed": True,
            }),
            "PASS",
        )


if __name__ == "__main__":
    unittest.main()
