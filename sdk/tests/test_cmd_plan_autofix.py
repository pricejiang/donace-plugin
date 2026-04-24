"""Tests for cmd_plan's auto-fix integration (codex review → codex fix → AWAIT_APPROVAL).

Stage 2 of the "let codex fix what codex flagged" flow. When codex plan
review returns findings, cmd_plan now dispatches codex again with
``run_codex_plan_fix`` (from stage 1) to apply a minimal patch. The
result:

- fix completed + scope_ok + non-empty diff → plan job status
  AWAIT_APPROVAL (main LLM evaluates the diff in the slash-command chat)
- fix failed / empty / scope violation → plan job status REVIEW (unchanged
  current behavior, user manually revises)
- no findings → plan job status PASS (unchanged)

plan.json now carries ``codex_review.fix = {diff, summary, scope_ok,
touched_other_files, ...}`` so downstream consumers (skill, classifier,
approval subcommand) can read the fix payload.

Runs with stdlib unittest. Invoke from repo root:
    python3 -m unittest sdk.tests.test_cmd_plan_autofix -v
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk import commands  # noqa: E402
from sdk.agent_dispatch import AgentDispatcher  # noqa: E402


_PLAN_BODY_V1 = (
    "# Implementation Plan: Stage-Test\n\n"
    "## Overview\nTwo stages covering happy-path sampling.\n\n"
    "## Stage 1: Token Utility\n"
    "**Goal**: add getToken helper\n"
    "**Files to modify**: `apps/web/lib/token.ts` (new)\n"
    "**Dependencies**: None\n**Has user-facing changes**: No\n"
    "**Estimated turns**: 3\n**Success Criteria**:\n- getToken returns string\n"
    "**Tests**: unit test covers default path\n**Status**: Not Started\n\n"
    "## Stage 2: Hook Up\n"
    "**Goal**: use the util in auth\n"
    "**Files to modify**: `apps/web/lib/auth.ts` (modify)\n"
    "**Dependencies**: Stage 1\n**Has user-facing changes**: No\n"
    "**Estimated turns**: 3\n**Success Criteria**:\n- auth uses getToken\n"
    "**Tests**: auth test observes helper\n**Status**: Not Started\n"
)

_PLAN_BODY_V2_AFTER_FIX = _PLAN_BODY_V1.replace(
    "Success Criteria**:\n- getToken returns string",
    "Success Criteria**:\n- getToken returns a non-empty signed string\n- rejects missing env",
)


def _review_approved() -> dict:
    return {
        "status": "completed",
        "has_major_issues": False,
        "summary": "Plan is solid.",
        "findings": [],
        "next_steps": [],
        "output": "",
        "job_id": "job-review-1",
        "thread_id": "thread-review-1",
    }


def _review_with_findings() -> dict:
    return {
        "status": "completed",
        "has_major_issues": True,
        "summary": "Stage 1 success criteria too vague.",
        "findings": [
            {
                "severity": "high",
                "title": "Vague success criteria",
                "body": "Stage 1's 'returns string' is untestable.",
                "recommendation": "Specify 'non-empty signed string'",
            }
        ],
        "next_steps": ["Tighten Stage 1 criteria"],
        "output": "",
        "job_id": "job-review-1",
        "thread_id": "thread-review-1",
    }


class CmdPlanAutofixTests(unittest.TestCase):
    """cmd_plan should auto-invoke run_codex_plan_fix when review returns findings."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="donace-cmd-plan-"))
        self.run_id = "test-run-autofix"
        self.run_dir = self.tmpdir / ".ai" / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.plan_path = self.run_dir / "plan.md"
        self.plan_path.write_text(_PLAN_BODY_V1)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def _run_cmd_plan(self, review_result, fix_result=None, fix_side_effect=None):
        """Invoke cmd_plan with the two codex methods patched.

        ``fix_side_effect`` lets a test mutate plan.md during the fix call
        to simulate codex's edits, then returns ``fix_result``.
        """
        async def fake_review(self_, plan_text, **kw):
            return review_result

        async def fake_fix(self_, plan_path, findings, **kw):
            if fix_side_effect is not None:
                fix_side_effect(plan_path, findings)
            return fix_result or {
                "attempted": False,
                "status": "skipped",
                "reason": "test default",
                "diff": "",
                "summary": "",
                "scope_ok": True,
                "touched_other_files": [],
                "thread_id": None,
                "job_id": None,
            }

        with patch.object(AgentDispatcher, "run_codex_plan_review", fake_review), \
             patch.object(AgentDispatcher, "run_codex_plan_fix", fake_fix):
            return self._run(commands.cmd_plan(
                cwd=str(self.tmpdir),
                run_id=self.run_id,
                dashboard_url=None,
            ))

    def _plan_json(self) -> dict:
        return json.loads((self.run_dir / "plan.json").read_text())

    def _latest_plan_job_status(self) -> str | None:
        jobs_dir = self.run_dir / "jobs"
        for p in sorted(jobs_dir.glob("job-plan-*.json")):
            j = json.loads(p.read_text())
            if j.get("command") == "plan":
                return j.get("status")
        return None

    # ------------------------------------------------------------------
    # Auto-fix success → AWAIT_APPROVAL
    # ------------------------------------------------------------------

    def test_findings_plus_successful_fix_yields_await_approval(self):
        def edit_plan(plan_path, findings):
            plan_path.write_text(_PLAN_BODY_V2_AFTER_FIX)

        fix_result = {
            "attempted": True,
            "status": "completed",
            "reason": "",
            "diff": "--- a/plan.md\n+++ b/plan.md\n@@ -6,1 +6,2 @@\n-old\n+new\n",
            "summary": "Tightened Stage 1 success criteria per finding.",
            "scope_ok": True,
            "touched_other_files": [],
            "thread_id": "thread-review-1",
            "job_id": "job-fix-1",
        }

        self._run_cmd_plan(
            review_result=_review_with_findings(),
            fix_result=fix_result,
            fix_side_effect=edit_plan,
        )

        status = self._latest_plan_job_status()
        self.assertEqual(status, "AWAIT_APPROVAL")
        plan_json = self._plan_json()
        fix_payload = (plan_json.get("codex_review") or {}).get("fix")
        self.assertIsNotNone(fix_payload)
        self.assertTrue(fix_payload["attempted"])
        self.assertTrue(fix_payload["scope_ok"])
        # plan.md on disk reflects the fix (re-parsed for plan.json)
        self.assertIn("non-empty signed string", self.plan_path.read_text())

    # ------------------------------------------------------------------
    # Auto-fix success → stages re-parsed from the new plan.md
    # ------------------------------------------------------------------

    def test_plan_json_stages_reparsed_after_fix(self):
        """If codex's fix adds or renames a stage, plan.json must reflect that.
        Otherwise downstream tools see stale stage metadata."""
        new_plan = _PLAN_BODY_V1 + (
            "\n## Stage 3: New Stage From Fix\n"
            "**Goal**: added by codex\n"
            "**Files to modify**: `apps/web/lib/x.ts` (new)\n"
            "**Dependencies**: Stage 2\n**Has user-facing changes**: No\n"
            "**Estimated turns**: 2\n**Success Criteria**:\n- something\n"
            "**Tests**: a test\n**Status**: Not Started\n"
        )

        def edit_plan(plan_path, findings):
            plan_path.write_text(new_plan)

        fix_result = {
            "attempted": True,
            "status": "completed",
            "reason": "",
            "diff": "(stub diff)",
            "summary": "Added a missing Stage 3.",
            "scope_ok": True,
            "touched_other_files": [],
            "thread_id": "thread-review-1",
            "job_id": "job-fix-1",
        }

        self._run_cmd_plan(
            review_result=_review_with_findings(),
            fix_result=fix_result,
            fix_side_effect=edit_plan,
        )

        plan_json = self._plan_json()
        stage_names = [s["name"] for s in plan_json["stages"]]
        self.assertIn("New Stage From Fix", stage_names,
                      msg="plan.json stages must be re-parsed from post-fix plan.md")

    # ------------------------------------------------------------------
    # Auto-fix failures → REVIEW (don't pretend to approve)
    # ------------------------------------------------------------------

    def test_findings_plus_scope_violating_fix_yields_review(self):
        """Codex touched other files → fallback to REVIEW, fix payload preserved."""
        def edit_plan(plan_path, findings):
            plan_path.write_text(_PLAN_BODY_V2_AFTER_FIX)

        fix_result = {
            "attempted": True,
            "status": "completed",
            "reason": "",
            "diff": "(stub diff)",
            "summary": "Edited plan.md and snuck in plan.json edit",
            "scope_ok": False,
            "touched_other_files": ["plan.json"],
            "thread_id": "thread-review-1",
            "job_id": "job-fix-1",
        }

        self._run_cmd_plan(
            review_result=_review_with_findings(),
            fix_result=fix_result,
            fix_side_effect=edit_plan,
        )

        status = self._latest_plan_job_status()
        self.assertEqual(status, "REVIEW")
        fix_payload = (self._plan_json().get("codex_review") or {}).get("fix") or {}
        self.assertFalse(fix_payload["scope_ok"])
        self.assertIn("plan.json", fix_payload["touched_other_files"])

    def test_findings_plus_empty_fix_yields_review(self):
        """Codex returned completed but made no changes → REVIEW, same as no fix."""
        fix_result = {
            "attempted": True,
            "status": "completed",
            "reason": "",
            "diff": "",
            "summary": "No-op — couldn't find a minimal edit.",
            "scope_ok": True,
            "touched_other_files": [],
            "thread_id": "thread-review-1",
            "job_id": "job-fix-1",
        }

        self._run_cmd_plan(
            review_result=_review_with_findings(),
            fix_result=fix_result,
        )

        self.assertEqual(self._latest_plan_job_status(), "REVIEW")

    def test_findings_plus_failed_fix_yields_review(self):
        """Codex task failed / cancelled / skipped → REVIEW."""
        fix_result = {
            "attempted": True,
            "status": "failed",
            "reason": "codex plan fix failed: connection reset",
            "diff": "",
            "summary": "",
            "scope_ok": True,
            "touched_other_files": [],
            "thread_id": None,
            "job_id": None,
        }

        self._run_cmd_plan(
            review_result=_review_with_findings(),
            fix_result=fix_result,
        )

        self.assertEqual(self._latest_plan_job_status(), "REVIEW")

    # ------------------------------------------------------------------
    # Unchanged paths
    # ------------------------------------------------------------------

    def test_no_findings_skips_fix_and_pass(self):
        """Review verdict "approve" → no fix attempt, status PASS."""
        # Fix must NOT be called if there are no findings.
        fix_calls: list = []

        async def fake_review(self_, plan_text, **kw):
            return _review_approved()

        async def fake_fix(self_, plan_path, findings, **kw):
            fix_calls.append(True)
            return {
                "attempted": True, "status": "completed", "reason": "",
                "diff": "", "summary": "", "scope_ok": True,
                "touched_other_files": [], "thread_id": None, "job_id": None,
            }

        with patch.object(AgentDispatcher, "run_codex_plan_review", fake_review), \
             patch.object(AgentDispatcher, "run_codex_plan_fix", fake_fix):
            self._run(commands.cmd_plan(
                cwd=str(self.tmpdir),
                run_id=self.run_id,
                dashboard_url=None,
            ))

        self.assertEqual(self._latest_plan_job_status(), "PASS")
        self.assertEqual(len(fix_calls), 0,
                         msg="Fix must not run when review has no major issues")

    # ------------------------------------------------------------------
    # State classifier recognizes AWAIT_APPROVAL
    # ------------------------------------------------------------------

    def test_await_approval_blocks_plan_ready(self):
        """_classify_run_state must treat AWAIT_APPROVAL as 'not ready', since
        Claude's judgment hasn't landed yet. Otherwise execute would dispatch
        stages against a plan the user never approved."""
        self._run_cmd_plan(
            review_result=_review_with_findings(),
            fix_result={
                "attempted": True, "status": "completed", "reason": "",
                "diff": "(stub)", "summary": "fixed",
                "scope_ok": True, "touched_other_files": [],
                "thread_id": "t1", "job_id": "j1",
            },
            fix_side_effect=lambda p, _: p.write_text(_PLAN_BODY_V2_AFTER_FIX),
        )

        info = commands._classify_run_state(self.run_dir)
        self.assertNotEqual(info["state"], "not_started",
                            msg="AWAIT_APPROVAL should not let execute start")
        self.assertEqual(info["state"], "incomplete")


if __name__ == "__main__":
    unittest.main()
