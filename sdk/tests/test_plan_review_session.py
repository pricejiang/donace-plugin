"""Tests for plan review session reuse via codex `task --resume-last`.

Rationale: the plan.md revision loop (rev1→rev2→rev3 as user iterates) currently
re-sends the full 26KB plan every round, and codex re-derives findings from
scratch without memory of prior rounds. codex-companion.mjs's `task` subcommand
supports `--resume-last` / `--resume` to continue the last per-repo thread,
which lets codex respond with delta-aware comments ("my prior concern about X
is addressed; new issue Y") and keeps the JSON output convention warm.

Only the `task` subcommand supports this — `review` / `adversarial-review`
are stateless by design, so code review keeps its current behavior.

Runs with stdlib unittest. Invoke from repo root:
    python3 -m unittest sdk.tests.test_plan_review_session -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk.agent_dispatch import AgentDispatcher  # noqa: E402
from sdk.commands import _prior_codex_thread_id, cmd_plan  # noqa: E402
from sdk.events import EventBus, Stage  # noqa: E402


class _FakeCodexSeam:
    """Records calls to _run_codex_json_subcommand and returns canned per-subcommand
    responses. Plan review now goes through this seam exclusively (background launch
    → status poll → result fetch), so tests install it to observe the task args
    (--background, --resume-last, etc.) without spawning real subprocesses.
    """

    def __init__(
        self,
        job_id: str = "task-abc123",
        thread_id: str | None = "thread-abc123",
        finding_json: str = '{"verdict":"approve","summary":"ok","findings":[],"next_steps":[]}',
    ):
        self.calls: list[tuple[str, list[str]]] = []
        self.job_id = job_id
        self.thread_id = thread_id
        self.finding_json = finding_json

    async def __call__(self, companion_script, subcommand, args, **_kw):
        self.calls.append((subcommand, list(args)))
        if subcommand == "task":
            payload: dict = {"jobId": self.job_id, "status": "queued"}
            if self.thread_id is not None:
                payload["threadId"] = self.thread_id
            return payload
        if subcommand == "status":
            job: dict = {"id": self.job_id, "status": "completed"}
            if self.thread_id is not None:
                job["threadId"] = self.thread_id
            return {"job": job}
        if subcommand == "result":
            return {"storedJob": {"result": {"finalMessage": self.finding_json}}}
        return None

    def first_task_args(self) -> list[str] | None:
        for sub, args in self.calls:
            if sub == "task":
                return args
        return None


class PlanReviewSessionTests(unittest.TestCase):
    """run_codex_plan_review must thread `--resume-last` through to codex
    when the caller marks the call as a revision, and must surface the
    returned threadId so the caller can persist it."""

    def _make_dispatcher(self) -> AgentDispatcher:
        dispatcher = AgentDispatcher(
            agents_dir=str(_REPO_ROOT / "agents"),
            cwd=str(_REPO_ROOT),
            bus=EventBus(run_id="test-run"),
        )
        # Skip companion-resolution filesystem check — return fake paths.
        dispatcher._resolve_codex_companion = lambda: (  # type: ignore[method-assign]
            "plugin-root",
            Path("scripts/codex-companion.mjs"),
            None,
        )
        return dispatcher

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_default_does_not_request_resume(self):
        dispatcher = self._make_dispatcher()
        seam = _FakeCodexSeam()
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]

        self._run(dispatcher.run_codex_plan_review("## Plan\n\n### Stage 1"))

        task_args = seam.first_task_args()
        self.assertIsNotNone(task_args)
        assert task_args is not None
        self.assertIn("--background", task_args)
        self.assertNotIn("--resume-last", task_args)

    def test_resume_last_adds_flag(self):
        dispatcher = self._make_dispatcher()
        seam = _FakeCodexSeam()
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]

        self._run(dispatcher.run_codex_plan_review(
            "## Plan\n\n### Stage 1",
            resume_last=True,
        ))

        task_args = seam.first_task_args()
        assert task_args is not None
        self.assertIn("--resume-last", task_args)

    def test_resume_thread_id_adds_flag_when_candidate_matches(self):
        dispatcher = self._make_dispatcher()
        seam = _FakeCodexSeam()
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]
        dispatcher._codex_task_resume_candidate_thread_id = (  # type: ignore[method-assign]
            lambda companion_script: asyncio.sleep(0, result="thread-abc123")
        )

        self._run(dispatcher.run_codex_plan_review(
            "## Plan\n\n### Stage 1",
            resume_thread_id="thread-abc123",
        ))

        task_args = seam.first_task_args()
        assert task_args is not None
        self.assertIn("--resume-last", task_args)

    def test_resume_thread_id_mismatch_skips_flag(self):
        dispatcher = self._make_dispatcher()
        seam = _FakeCodexSeam()
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]
        dispatcher._codex_task_resume_candidate_thread_id = (  # type: ignore[method-assign]
            lambda companion_script: asyncio.sleep(0, result="thread-other")
        )

        self._run(dispatcher.run_codex_plan_review(
            "## Plan\n\n### Stage 1",
            resume_thread_id="thread-abc123",
        ))

        task_args = seam.first_task_args()
        assert task_args is not None
        self.assertNotIn("--resume-last", task_args)

    def test_response_surfaces_thread_id(self):
        # Callers need thread_id so they can persist it in plan.json and
        # pass resume_thread_id on the next revision.
        dispatcher = self._make_dispatcher()
        seam = _FakeCodexSeam(thread_id="thread-xyz-789")
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]

        result = self._run(dispatcher.run_codex_plan_review("## Plan"))

        self.assertEqual(result.get("thread_id"), "thread-xyz-789")

    def test_missing_thread_id_is_none(self):
        # Older companion versions / edge cases may omit threadId. Caller
        # can detect missing-id state and fall back to fresh mode.
        dispatcher = self._make_dispatcher()
        seam = _FakeCodexSeam(thread_id=None)
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]

        result = self._run(dispatcher.run_codex_plan_review("## Plan"))

        self.assertIsNone(result.get("thread_id"))


class PriorThreadIdDetectionTests(unittest.TestCase):
    """_prior_codex_thread_id is the signal commands.py uses to decide
    whether a plan.md write is a fresh plan or a revision."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_no_plan_json_returns_none(self):
        self.assertIsNone(_prior_codex_thread_id(self.run_dir))

    def test_plan_json_without_review_returns_none(self):
        (self.run_dir / "plan.json").write_text(json.dumps({"stages": []}))
        self.assertIsNone(_prior_codex_thread_id(self.run_dir))

    def test_review_without_thread_id_returns_none(self):
        (self.run_dir / "plan.json").write_text(json.dumps({
            "codex_review": {"status": "completed", "has_major_issues": False},
        }))
        self.assertIsNone(_prior_codex_thread_id(self.run_dir))

    def test_thread_id_extracted(self):
        (self.run_dir / "plan.json").write_text(json.dumps({
            "codex_review": {"status": "completed", "thread_id": "thread-xyz"},
        }))
        self.assertEqual(_prior_codex_thread_id(self.run_dir), "thread-xyz")

    def test_corrupt_json_returns_none(self):
        # Don't crash cmd_write_plan just because a stray write corrupted
        # the sidecar — revert to fresh-review behavior.
        (self.run_dir / "plan.json").write_text("{not: valid json}")
        self.assertIsNone(_prior_codex_thread_id(self.run_dir))

    def test_null_thread_id_returns_none(self):
        # run_codex_plan_review writes thread_id=None when companion
        # didn't report one. That must not be treated as resumable.
        (self.run_dir / "plan.json").write_text(json.dumps({
            "codex_review": {"status": "completed", "thread_id": None},
        }))
        self.assertIsNone(_prior_codex_thread_id(self.run_dir))

    def test_empty_string_thread_id_returns_none(self):
        (self.run_dir / "plan.json").write_text(json.dumps({
            "codex_review": {"status": "completed", "thread_id": ""},
        }))
        self.assertIsNone(_prior_codex_thread_id(self.run_dir))


class CmdPlanResumeThreadTests(unittest.TestCase):
    """cmd_plan should pass the saved plan-review thread id through unchanged."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmpdir.name)
        self.run_id = "run-123"
        self.run_dir = self.cwd / ".ai" / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "plan.md").write_text("## Placeholder plan\n")
        (self.run_dir / "plan.json").write_text(json.dumps({
            "codex_review": {"status": "completed", "thread_id": "thread-prev"},
        }))

    def tearDown(self):
        self._tmpdir.cleanup()

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_cmd_plan_passes_saved_thread_id(self):
        seen: dict[str, object] = {}

        class FakeDispatcher:
            def __init__(self, *args, **kwargs):
                pass

            async def run_codex_plan_review(self, plan_text: str, *, resume_last: bool = False, resume_thread_id: str | None = None) -> dict:
                seen["resume_last"] = resume_last
                seen["resume_thread_id"] = resume_thread_id
                return {
                    "status": "completed",
                    "has_major_issues": False,
                    "thread_id": "thread-prev",
                }

        with patch("sdk.orchestrator._parse_plan_stages", return_value=[
            Stage(name="Stage 1", has_user_facing_changes=False, files=["apps/web/x.ts"]),
        ]), patch("sdk.agent_dispatch.AgentDispatcher", FakeDispatcher):
            self._run(cmd_plan(str(self.cwd), self.run_id, None))

        self.assertEqual(seen.get("resume_thread_id"), "thread-prev")
        self.assertFalse(bool(seen.get("resume_last")))


if __name__ == "__main__":
    unittest.main()
