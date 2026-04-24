"""Tests for `AgentDispatcher.run_codex_plan_fix`.

Motivation: the plan.md revision loop today goes user → planner dispatch
→ codex review → findings → user re-invoke → planner dispatch → codex
review → … . Opus 4.6 hangs intermittently on the huge revision prompt
(run-phase5-runE-dbeea30dbe6d rounds 7/8). Direction: after a codex
review finds issues, let codex itself apply a minimal edit to plan.md,
return the diff, and hand it to the main LLM for sanity-checking. Only
pause for user intervention when the LLM isn't satisfied with the diff.

This file covers the dispatcher-level piece only — the `run_codex_plan_fix`
method that wraps `codex task --write` and verifies scope across the cwd
(only plan.md may change). The cmd_plan / skill-side wiring lands in
follow-up commits.

Runs with stdlib unittest. Invoke from repo root:
    python3 -m unittest sdk.tests.test_codex_plan_fix -v
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk.agent_dispatch import AgentDispatcher  # noqa: E402
from sdk.events import EventBus  # noqa: E402


_ORIG_PLAN = "# Implementation Plan: Stub\n\n## Stage 1\n**Goal**: original\n"
_FIXED_PLAN = "# Implementation Plan: Stub\n\n## Stage 1\n**Goal**: revised per codex findings\n"
_FINDINGS_SAMPLE = [
    {
        "severity": "high",
        "title": "Missing field",
        "body": "Stage 1 lacks concrete success criteria",
        "recommendation": "Add testable assertions",
    }
]


class _PlanFixSeam:
    """Fake codex-companion seam that returns canned payloads for the
    task-launch / poll / result flow, and optionally mutates files on disk
    during the `task` call to simulate codex editing plan.md.
    """

    def __init__(
        self,
        *,
        file_mutations: list[tuple[Path, bytes]] | None = None,
        final_message: str = "Applied minimal edits per findings.",
        thread_id: str | None = "thread-plan-fix-abc",
        job_id: str = "job-fix-xyz",
        launch_returns_none: bool = False,
    ):
        self.calls: list[tuple[str, list[str]]] = []
        self.file_mutations = file_mutations or []
        self.final_message = final_message
        self.thread_id = thread_id
        self.job_id = job_id
        self.launch_returns_none = launch_returns_none

    async def __call__(
        self,
        companion_script: Path,
        subcommand: str,
        args: list[str],
        **_kw: Any,
    ) -> dict | None:
        self.calls.append((subcommand, list(args)))
        if subcommand == "task":
            if self.launch_returns_none:
                return None
            # Simulate codex editing files in the project.
            for path, content in self.file_mutations:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
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
            return {"storedJob": {"result": {"finalMessage": self.final_message}}}
        if subcommand == "cancel":
            return {"jobId": self.job_id, "status": "cancelled"}
        return None

    def first_task_args(self) -> list[str] | None:
        for sub, args in self.calls:
            if sub == "task":
                return args
        return None


class PlanFixBasicsTests(unittest.TestCase):
    """Launch shape, result shape, and scope-verification basics."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="donace-planfix-"))
        self.run_dir = self.tmpdir / ".ai" / "runs" / "test-run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.plan_path = self.run_dir / "plan.md"
        self.plan_path.write_text(_ORIG_PLAN)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_dispatcher(self) -> AgentDispatcher:
        d = AgentDispatcher(
            agents_dir=str(_REPO_ROOT / "agents"),
            cwd=str(self.tmpdir),
            bus=EventBus(run_id="test-run"),
        )
        d._resolve_codex_companion = lambda: (  # type: ignore[method-assign]
            "plugin-root", Path("scripts/codex-companion.mjs"), None,
        )
        return d

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    # ------------------------------------------------------------------
    # Launch shape
    # ------------------------------------------------------------------

    def test_launch_passes_write_flag(self):
        """The whole point: codex must have --write so it can edit plan.md."""
        dispatcher = self._make_dispatcher()
        seam = _PlanFixSeam(file_mutations=[(self.plan_path, _FIXED_PLAN.encode())])
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]

        self._run(dispatcher.run_codex_plan_fix(
            plan_path=self.plan_path,
            findings=_FINDINGS_SAMPLE,
        ))

        task_args = seam.first_task_args()
        assert task_args is not None
        self.assertIn("--write", task_args)
        self.assertIn("--background", task_args)

    def test_resume_thread_id_threads_through_when_candidate_matches(self):
        """Fix should continue the review's thread so it reuses the findings context."""
        dispatcher = self._make_dispatcher()
        seam = _PlanFixSeam(file_mutations=[(self.plan_path, _FIXED_PLAN.encode())])
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]
        dispatcher._codex_task_resume_candidate_thread_id = (  # type: ignore[method-assign]
            lambda companion_script: asyncio.sleep(0, result="thread-plan-fix-abc")
        )

        self._run(dispatcher.run_codex_plan_fix(
            plan_path=self.plan_path,
            findings=_FINDINGS_SAMPLE,
            resume_thread_id="thread-plan-fix-abc",
        ))

        task_args = seam.first_task_args()
        assert task_args is not None
        self.assertIn("--resume-last", task_args)

    def test_resume_thread_id_mismatch_skips_resume(self):
        """If codex's current candidate thread doesn't match, fall back to fresh."""
        dispatcher = self._make_dispatcher()
        seam = _PlanFixSeam(file_mutations=[(self.plan_path, _FIXED_PLAN.encode())])
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]
        dispatcher._codex_task_resume_candidate_thread_id = (  # type: ignore[method-assign]
            lambda companion_script: asyncio.sleep(0, result="thread-other")
        )

        self._run(dispatcher.run_codex_plan_fix(
            plan_path=self.plan_path,
            findings=_FINDINGS_SAMPLE,
            resume_thread_id="thread-plan-fix-abc",
        ))

        task_args = seam.first_task_args()
        assert task_args is not None
        self.assertNotIn("--resume-last", task_args)

    # ------------------------------------------------------------------
    # Result shape
    # ------------------------------------------------------------------

    def test_successful_fix_returns_unified_diff(self):
        """Codex edits plan.md → result includes a non-empty unified diff."""
        dispatcher = self._make_dispatcher()
        seam = _PlanFixSeam(file_mutations=[(self.plan_path, _FIXED_PLAN.encode())])
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]

        result = self._run(dispatcher.run_codex_plan_fix(
            plan_path=self.plan_path,
            findings=_FINDINGS_SAMPLE,
        ))

        self.assertTrue(result["attempted"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["scope_ok"])
        self.assertIn("revised per codex findings", result["diff"])
        self.assertIn("original", result["diff"])  # context from the old version
        self.assertEqual(result["touched_other_files"], [])
        self.assertEqual(result["thread_id"], "thread-plan-fix-abc")
        self.assertIn("Applied minimal edits", result["summary"])

    def test_no_op_fix_returns_empty_diff(self):
        """If codex completes without editing plan.md, diff is empty — caller
        decides whether to treat that as success or failure."""
        dispatcher = self._make_dispatcher()
        seam = _PlanFixSeam(file_mutations=[])  # no mutation
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]

        result = self._run(dispatcher.run_codex_plan_fix(
            plan_path=self.plan_path,
            findings=_FINDINGS_SAMPLE,
        ))

        self.assertTrue(result["attempted"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["diff"], "")
        self.assertTrue(result["scope_ok"])  # no files touched at all
        self.assertEqual(result["touched_other_files"], [])

    # ------------------------------------------------------------------
    # Scope verification
    # ------------------------------------------------------------------

    def test_scope_violation_when_other_run_dir_file_touched(self):
        """If codex edits plan.json alongside plan.md, scope_ok=False and
        the offending path is surfaced. The user sees exactly what was
        modified outside the intended single-file patch.
        """
        dispatcher = self._make_dispatcher()
        plan_json = self.run_dir / "plan.json"
        seam = _PlanFixSeam(file_mutations=[
            (self.plan_path, _FIXED_PLAN.encode()),
            (plan_json, b'{"injected": true}'),
        ])
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]

        result = self._run(dispatcher.run_codex_plan_fix(
            plan_path=self.plan_path,
            findings=_FINDINGS_SAMPLE,
        ))

        self.assertFalse(result["scope_ok"])
        self.assertIn(
            str(plan_json.relative_to(self.tmpdir)),
            result["touched_other_files"],
        )
        # Diff is still populated — caller might still want to see what codex
        # tried to do before rejecting.
        self.assertIn("revised per codex findings", result["diff"])

    def test_scope_violation_when_project_file_outside_run_dir_touched(self):
        """Scope check covers the whole project cwd, not just .ai/runs/<id>."""
        dispatcher = self._make_dispatcher()
        source_file = self.tmpdir / "apps" / "web" / "feature.ts"
        seam = _PlanFixSeam(file_mutations=[
            (self.plan_path, _FIXED_PLAN.encode()),
            (source_file, b"export const leaked = true;\n"),
        ])
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]

        result = self._run(dispatcher.run_codex_plan_fix(
            plan_path=self.plan_path,
            findings=_FINDINGS_SAMPLE,
        ))

        self.assertFalse(result["scope_ok"])
        self.assertIn(
            str(source_file.relative_to(self.tmpdir)),
            result["touched_other_files"],
        )

    def test_scope_violation_when_new_file_created_in_run_dir(self):
        """Scope check catches file creation, not just modification."""
        dispatcher = self._make_dispatcher()
        stray = self.run_dir / "notes.md"
        seam = _PlanFixSeam(file_mutations=[
            (self.plan_path, _FIXED_PLAN.encode()),
            (stray, b"codex side-note"),
        ])
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]

        result = self._run(dispatcher.run_codex_plan_fix(
            plan_path=self.plan_path,
            findings=_FINDINGS_SAMPLE,
        ))

        self.assertFalse(result["scope_ok"])
        self.assertIn(
            str(stray.relative_to(self.tmpdir)),
            result["touched_other_files"],
        )

    def test_timeout_cancels_background_write_task(self):
        """A timed-out --write task must be cancelled so it cannot keep editing."""
        dispatcher = self._make_dispatcher()
        seam = _PlanFixSeam(file_mutations=[(self.plan_path, _FIXED_PLAN.encode())])
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]

        with patch("sdk.agent_dispatch._PLAN_REVIEW_TIMEOUT_S", 0.0):
            result = self._run(dispatcher.run_codex_plan_fix(
                plan_path=self.plan_path,
                findings=_FINDINGS_SAMPLE,
            ))

        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(any(subcommand == "cancel" for subcommand, _ in seam.calls))
        self.assertIn("cancelled", result["reason"])

    # ------------------------------------------------------------------
    # Failure modes
    # ------------------------------------------------------------------

    def test_launch_failure_marks_not_attempted(self):
        """If the task launch returns no jobId, attempted=False, status=skipped."""
        dispatcher = self._make_dispatcher()
        seam = _PlanFixSeam(launch_returns_none=True)
        dispatcher._run_codex_json_subcommand = seam  # type: ignore[method-assign]

        result = self._run(dispatcher.run_codex_plan_fix(
            plan_path=self.plan_path,
            findings=_FINDINGS_SAMPLE,
        ))

        self.assertFalse(result["attempted"])
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["diff"], "")

    def test_codex_companion_missing_marks_not_attempted(self):
        """If codex plugin isn't installed, skip cleanly with a reason string."""
        dispatcher = self._make_dispatcher()
        dispatcher._resolve_codex_companion = lambda: (  # type: ignore[method-assign]
            None, None, "codex plugin not found",
        )

        result = self._run(dispatcher.run_codex_plan_fix(
            plan_path=self.plan_path,
            findings=_FINDINGS_SAMPLE,
        ))

        self.assertFalse(result["attempted"])
        self.assertEqual(result["status"], "skipped")
        self.assertIn("codex plugin not found", result.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
