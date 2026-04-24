"""Tests for cmd_approve_plan / cmd_reject_plan.

Stage 3 of the "let codex fix what codex flagged" flow. After cmd_plan
runs codex review + auto-fix and lands AWAIT_APPROVAL, the main LLM in
the /donace:plan chat evaluates codex's diff against the findings and
calls one of:

- ``orchestrator.py approve_plan --run-id <id> --cwd <dir>`` when the
  fix genuinely addresses every finding. Updates plan.json
  (has_major_issues=False, fix.verdict=approved) and writes a new plan
  job with status=PASS so execute can proceed.
- ``orchestrator.py reject_plan --run-id <id> --cwd <dir> --reason "..."``
  when Claude spots drift / missed findings / unsafe edits. Keeps
  has_major_issues=True, writes plan job with status=REVIEW, records
  Claude's reason on fix.reason for the user to see.

Runs with stdlib unittest:
    python3 -m unittest sdk.tests.test_cmd_approve_plan -v
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk import commands  # noqa: E402


def _await_approval_plan_json() -> dict:
    return {
        "plan_file": ".ai/runs/test-run/plan.md",
        "stages": [
            {
                "id": "stage-1", "name": "S1", "files": ["apps/x.ts"],
                "dependencies": [], "has_user_facing_changes": False,
                "estimated_turns": 3,
            }
        ],
        "codex_review": {
            "status": "completed",
            "has_major_issues": True,
            "summary": "stage 1 vague",
            "findings": [{"severity": "high", "title": "vague"}],
            "next_steps": [],
            "output": "",
            "job_id": "job-review-1",
            "thread_id": "thread-1",
            "fix": {
                "attempted": True,
                "status": "completed",
                "reason": "",
                "diff": "--- a/plan.md\n+++ b/plan.md\n(real diff)",
                "summary": "Tightened stage 1 criteria",
                "scope_ok": True,
                "touched_other_files": [],
                "thread_id": "thread-1",
                "job_id": "job-fix-1",
            },
        },
    }


class CmdApprovePlanTests(unittest.TestCase):
    """approve_plan lifts AWAIT_APPROVAL → PASS atomically."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="donace-approve-"))
        self.run_id = "test-run"
        self.run_dir = self.tmpdir / ".ai" / "runs" / self.run_id
        (self.run_dir / "jobs").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "plan.md").write_text("# dummy\n")
        self.plan_json_path = self.run_dir / "plan.json"
        self.plan_json_path.write_text(json.dumps(_await_approval_plan_json()))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def _latest_plan_job_status(self) -> str | None:
        for p in sorted((self.run_dir / "jobs").glob("job-plan-*.json")):
            j = json.loads(p.read_text())
            if j.get("command") == "plan":
                return j.get("status")
        return None

    def _plan_json(self) -> dict:
        return json.loads(self.plan_json_path.read_text())

    # ------------------------------------------------------------------
    # Approve happy path
    # ------------------------------------------------------------------

    def test_approve_clears_major_issues_and_writes_pass_job(self):
        result = self._run(commands.cmd_approve_plan(
            cwd=str(self.tmpdir), run_id=self.run_id,
            dashboard_url=None, note="All findings addressed, scope looks good",
        ))

        self.assertEqual(result["status"], "approved")
        plan_json = self._plan_json()
        self.assertFalse(plan_json["codex_review"]["has_major_issues"])
        self.assertEqual(
            plan_json["codex_review"]["fix"]["verdict"], "approved",
        )
        self.assertIn(
            "All findings addressed",
            plan_json["codex_review"]["fix"].get("note", ""),
        )
        self.assertEqual(self._latest_plan_job_status(), "PASS")

    def test_approve_without_note_works(self):
        """Note is optional — Claude may approve without prose commentary."""
        result = self._run(commands.cmd_approve_plan(
            cwd=str(self.tmpdir), run_id=self.run_id, dashboard_url=None,
        ))
        self.assertEqual(result["status"], "approved")
        self.assertEqual(self._latest_plan_job_status(), "PASS")

    def test_approve_when_not_awaiting_is_noop_error(self):
        """If plan.json isn't actually AWAIT_APPROVAL (no fix, or fix not
        clean), approve must refuse rather than blindly flip has_major_issues
        and hide whatever state the user was in."""
        # Rewrite without a fix payload.
        data = _await_approval_plan_json()
        data["codex_review"].pop("fix", None)
        self.plan_json_path.write_text(json.dumps(data))

        result = self._run(commands.cmd_approve_plan(
            cwd=str(self.tmpdir), run_id=self.run_id, dashboard_url=None,
        ))
        self.assertEqual(result["status"], "error")
        # plan.json must NOT be mutated when approve refused.
        self.assertTrue(self._plan_json()["codex_review"]["has_major_issues"])

    def test_approve_when_no_plan_json_is_error(self):
        self.plan_json_path.unlink()
        result = self._run(commands.cmd_approve_plan(
            cwd=str(self.tmpdir), run_id=self.run_id, dashboard_url=None,
        ))
        self.assertEqual(result["status"], "error")

    def test_approve_supersedes_prior_plan_jobs(self):
        """A prior AWAIT_APPROVAL plan job shouldn't linger once we lift to PASS."""
        (self.run_dir / "jobs" / "job-plan-old.json").write_text(json.dumps({
            "command": "plan", "status": "AWAIT_APPROVAL",
        }))
        self._run(commands.cmd_approve_plan(
            cwd=str(self.tmpdir), run_id=self.run_id, dashboard_url=None,
        ))

        statuses = [
            json.loads(p.read_text()).get("status")
            for p in (self.run_dir / "jobs").glob("job-plan-*.json")
        ]
        self.assertIn("PASS", statuses)
        self.assertNotIn("AWAIT_APPROVAL", statuses)


class CmdRejectPlanTests(unittest.TestCase):
    """reject_plan keeps AWAIT_APPROVAL from silently passing — Claude flagged
    missed findings or drift, so the plan needs user attention."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="donace-reject-"))
        self.run_id = "test-run"
        self.run_dir = self.tmpdir / ".ai" / "runs" / self.run_id
        (self.run_dir / "jobs").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "plan.md").write_text("# dummy\n")
        self.plan_json_path = self.run_dir / "plan.json"
        self.plan_json_path.write_text(json.dumps(_await_approval_plan_json()))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def _latest_plan_job_status(self) -> str | None:
        for p in sorted((self.run_dir / "jobs").glob("job-plan-*.json")):
            j = json.loads(p.read_text())
            if j.get("command") == "plan":
                return j.get("status")
        return None

    def _plan_json(self) -> dict:
        return json.loads(self.plan_json_path.read_text())

    def test_reject_keeps_major_issues_and_writes_review_job(self):
        result = self._run(commands.cmd_reject_plan(
            cwd=str(self.tmpdir), run_id=self.run_id,
            dashboard_url=None,
            reason="Finding 2 about test coverage was not addressed in the diff",
        ))

        self.assertEqual(result["status"], "rejected")
        plan_json = self._plan_json()
        self.assertTrue(plan_json["codex_review"]["has_major_issues"])
        self.assertEqual(
            plan_json["codex_review"]["fix"]["verdict"], "rejected",
        )
        self.assertIn(
            "test coverage",
            plan_json["codex_review"]["fix"].get("reason", ""),
        )
        self.assertEqual(self._latest_plan_job_status(), "REVIEW")

    def test_reject_requires_reason(self):
        """Claude must explain why it's rejecting so the user sees why they
        have work to do. Empty string → error."""
        result = self._run(commands.cmd_reject_plan(
            cwd=str(self.tmpdir), run_id=self.run_id,
            dashboard_url=None, reason="",
        ))
        self.assertEqual(result["status"], "error")

    def test_reject_when_not_awaiting_is_error(self):
        data = _await_approval_plan_json()
        data["codex_review"].pop("fix", None)
        self.plan_json_path.write_text(json.dumps(data))

        result = self._run(commands.cmd_reject_plan(
            cwd=str(self.tmpdir), run_id=self.run_id,
            dashboard_url=None, reason="unused",
        ))
        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
