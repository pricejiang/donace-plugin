"""Tests that `cmd_write_plan` distinguishes "planner wrote the revision" from
"planner returned without touching plan.md".

Motivation: run-phase5-runE-dbeea30dbe6d rounds 7 and 8. On a revision, the
planner's dispatch got a 45-51k-char prompt (prior plan.md is inlined for
context). Opus 4.6 + extended thinking hit its intermittent post-tool_result
hang, the session ended after ~28s with no file rewrite. The old check
— `plan_path.exists() and stat().st_size >= 100` — returned PASS because
the prior plan.md was still on disk, so cmd_plan fed the stale plan back to
codex, which produced the same findings, which re-dispatched the planner,
which hung again. Loop.

Fix: snapshot plan.md bytes before dispatching, compare after. If the prior
plan was left untouched, treat it as ERROR so the run surfaces the silent
failure instead of quietly recycling a stale artifact.

Runs with stdlib unittest. Invoke from repo root:
    python3 -m unittest sdk.tests.test_write_plan_modified_check -v
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


_PRIOR_PLAN_BODY = (
    "# Implementation Plan: Dummy\n\n"
    "## Overview\nA pre-existing plan sized well above the 100-byte floor.\n\n"
    "## Stage 1: Existing\n"
    "**Goal**: test fixture\n**Files to modify**: None\n"
    "**Dependencies**: None\n**Has user-facing changes**: No\n"
    "**Estimated turns**: 0\n**Success Criteria**:\n- N/A\n"
    "**Tests**: N/A\n**Status**: Not Started\n"
)


class WritePlanModifiedCheckTests(unittest.TestCase):
    """`cmd_write_plan` must check plan.md was actually modified, not just exist."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="donace-writeplan-"))
        self.run_id = "test-run-rev"
        self.run_dir = self.tmpdir / ".ai" / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.plan_path = self.run_dir / "plan.md"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def _invoke_write_plan(self, query_behavior) -> dict:
        """Run cmd_write_plan with AgentDispatcher.query replaced by query_behavior(self, agent, prompt, **kw)."""
        async def fake_query(self_, agent, prompt, **_kw):
            return await query_behavior(self_, agent, prompt)

        with patch.object(AgentDispatcher, "query", fake_query):
            return self._run(commands.cmd_write_plan(
                run_id=self.run_id,
                cwd=str(self.tmpdir),
                dashboard_url=None,
                task="irrelevant revision brief",
            ))

    # ------------------------------------------------------------------
    # Revision flows (prior plan.md already on disk)
    # ------------------------------------------------------------------

    def test_revision_silent_noop_is_error(self):
        """Planner returns without modifying plan.md → ERROR (was PASS before the fix).

        Reproduces the runE-dbeea30dbe6d loop: old contents still on disk,
        old code's `exists() and size>=100` said PASS, stale plan got re-fed
        to codex ad infinitum.
        """
        self.plan_path.write_text(_PRIOR_PLAN_BODY)

        async def planner_does_nothing(_self, _agent, _prompt):
            return "ok"

        result = self._invoke_write_plan(planner_does_nothing)

        self.assertEqual(result["status"], "ERROR")
        self.assertIn("not modified", (result.get("error") or "").lower())
        # The original content must still be on disk — the fix doesn't delete
        # prior progress, the recovery logic already handles that.
        self.assertEqual(self.plan_path.read_text(), _PRIOR_PLAN_BODY)

    def test_revision_with_actual_rewrite_is_pass(self):
        """Planner overwrites plan.md with new content → PASS."""
        self.plan_path.write_text(_PRIOR_PLAN_BODY)
        new_body = _PRIOR_PLAN_BODY.replace("Dummy", "Revised Dummy")

        async def planner_rewrites(_self, _agent, _prompt):
            self.plan_path.write_text(new_body)
            return "rewrote"

        result = self._invoke_write_plan(planner_rewrites)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(self.plan_path.read_text(), new_body)

    def test_revision_with_identical_rewrite_is_error(self):
        """Planner 'rewrites' plan.md with byte-identical content → still ERROR.

        Edge case: some filesystems update mtime on identical rewrite, some
        don't. Comparing bytes — not mtime — is the only reliable way to
        tell "planner actually addressed the revision brief" apart from
        "planner did a no-op touch". runE would have happily recycled a
        stale plan under an mtime-based check.
        """
        self.plan_path.write_text(_PRIOR_PLAN_BODY)

        async def planner_rewrites_identically(_self, _agent, _prompt):
            # Touch the file but write back the same bytes.
            self.plan_path.write_text(_PRIOR_PLAN_BODY)
            return "no changes needed"

        result = self._invoke_write_plan(planner_rewrites_identically)

        self.assertEqual(result["status"], "ERROR")
        self.assertIn("not modified", (result.get("error") or "").lower())

    # ------------------------------------------------------------------
    # Initial write flows (no prior plan.md)
    # ------------------------------------------------------------------

    def test_initial_write_success_is_pass(self):
        """No prior plan, planner writes fresh content above the size floor → PASS."""
        new_body = _PRIOR_PLAN_BODY

        async def planner_writes(_self, _agent, _prompt):
            self.plan_path.write_text(new_body)
            return "fresh plan"

        result = self._invoke_write_plan(planner_writes)

        self.assertEqual(result["status"], "PASS")

    def test_initial_write_with_no_file_is_error(self):
        """No prior plan, planner returns without writing → ERROR (existing behaviour)."""
        async def planner_silent(_self, _agent, _prompt):
            return ""

        result = self._invoke_write_plan(planner_silent)

        self.assertEqual(result["status"], "ERROR")
        self.assertIn("not written", (result.get("error") or "").lower())

    def test_initial_write_tiny_file_is_error(self):
        """No prior plan, planner writes a stub under the 100-byte floor → ERROR."""
        async def planner_writes_stub(_self, _agent, _prompt):
            self.plan_path.write_text("tiny stub\n")
            return "minimal"

        result = self._invoke_write_plan(planner_writes_stub)

        self.assertEqual(result["status"], "ERROR")
        self.assertIn("suspiciously small", (result.get("error") or "").lower())


if __name__ == "__main__":
    unittest.main()
