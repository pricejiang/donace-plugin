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
from sdk.commands import _prior_codex_thread_id  # noqa: E402
from sdk.events import EventBus  # noqa: E402


class _CapturedCmd:
    """Records the cmd that would have been sent to codex."""

    def __init__(self, payload: dict):
        self.cmd: str | None = None
        self.payload = payload

    async def __call__(self, cmd: str, codex_plugin_root: str) -> str:
        self.cmd = cmd
        return json.dumps(self.payload)


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

    # --- JSON response used by the fake _run_codex_command ---
    def _valid_task_payload(self, thread_id: str = "thread-abc123") -> dict:
        inner = {
            "verdict": "approve",
            "summary": "looks good",
            "findings": [],
            "next_steps": [],
        }
        return {
            "status": 0,
            "threadId": thread_id,
            "rawOutput": json.dumps(inner),
        }

    def test_default_does_not_request_resume(self):
        dispatcher = self._make_dispatcher()
        capture = _CapturedCmd(self._valid_task_payload())
        dispatcher._run_codex_command = capture  # type: ignore[method-assign]

        self._run(dispatcher.run_codex_plan_review("## Plan\n\n### Stage 1"))

        self.assertIsNotNone(capture.cmd)
        assert capture.cmd is not None
        self.assertIn("task", capture.cmd)
        self.assertNotIn("--resume-last", capture.cmd)
        self.assertNotIn("--resume", capture.cmd)

    def test_resume_last_adds_flag(self):
        dispatcher = self._make_dispatcher()
        capture = _CapturedCmd(self._valid_task_payload())
        dispatcher._run_codex_command = capture  # type: ignore[method-assign]

        self._run(dispatcher.run_codex_plan_review(
            "## Plan\n\n### Stage 1",
            resume_last=True,
        ))

        assert capture.cmd is not None
        self.assertIn("--resume-last", capture.cmd)

    def test_response_surfaces_thread_id(self):
        # Callers need thread_id so they can persist it in plan.json and
        # pass resume_last=True on the next revision.
        dispatcher = self._make_dispatcher()
        capture = _CapturedCmd(self._valid_task_payload("thread-xyz-789"))
        dispatcher._run_codex_command = capture  # type: ignore[method-assign]

        result = self._run(dispatcher.run_codex_plan_review("## Plan"))

        self.assertEqual(result.get("thread_id"), "thread-xyz-789")

    def test_missing_thread_id_is_none(self):
        # Older companion versions / edge cases may omit threadId. Caller
        # can detect missing-id state and fall back to fresh mode.
        dispatcher = self._make_dispatcher()
        payload = self._valid_task_payload()
        payload.pop("threadId", None)
        capture = _CapturedCmd(payload)
        dispatcher._run_codex_command = capture  # type: ignore[method-assign]

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


if __name__ == "__main__":
    unittest.main()
