"""Tests for the ASCII pipeline view in `donace stage_status`."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk import cli


_PLAN = """# Plan: pipeline test

## Stage 1: add health endpoint
- implementer: claude
- goal: expose /health
- files: src/server.py
- success criteria:
  - returns 200
- tests:
  - python3 -m unittest src.server_test

## Stage 2: rename fields
- implementer: codex
- goal: rename
- files: src/user.py
- success criteria:
  - callers updated
- tests:
  - none: mechanical
"""


def _write_plan(cwd: Path) -> str:
    run_id = cli.run_start(cwd=cwd)
    (cwd / ".ai" / "runs" / run_id / "plan.md").write_text(_PLAN)
    return run_id


def _set_stage_status(cwd: Path, run_id: str, sid: str, status_dict: dict) -> None:
    p = cwd / ".ai" / "runs" / run_id / "stages" / sid
    p.mkdir(parents=True, exist_ok=True)
    (p / "status.json").write_text(json.dumps(status_dict))


class GlyphTest(unittest.TestCase):
    def test_glyphs_utf8(self):
        self.assertEqual(cli._glyph_for("passed", utf8=True), "✓")
        self.assertEqual(cli._glyph_for("running", utf8=True), "⟳")
        self.assertEqual(cli._glyph_for("pending", utf8=True), "·")
        self.assertEqual(cli._glyph_for("blocked", utf8=True), "✗")
        self.assertEqual(cli._glyph_for("interrupted", utf8=True), "‖")

    def test_glyphs_ascii_fallback(self):
        self.assertEqual(cli._glyph_for("passed", utf8=False), "*")
        self.assertEqual(cli._glyph_for("running", utf8=False), ">")
        self.assertEqual(cli._glyph_for("pending", utf8=False), ".")
        self.assertEqual(cli._glyph_for("blocked", utf8=False), "!")
        self.assertEqual(cli._glyph_for("interrupted", utf8=False), "|")


class ReviewerDerivationTest(unittest.TestCase):
    def test_opposite_model(self):
        self.assertEqual(cli._reviewer_for("claude"), "codex")
        self.assertEqual(cli._reviewer_for("codex"), "claude")


class StateSummaryTest(unittest.TestCase):
    def test_passed_one_try(self):
        self.assertEqual(cli._state_summary({"status": "passed", "retry_count": 0}), "passed (1 try)")

    def test_passed_three_tries(self):
        self.assertEqual(cli._state_summary({"status": "passed", "retry_count": 2}), "passed (3 tries)")

    def test_running_with_retry(self):
        self.assertEqual(cli._state_summary({"status": "running", "retry_count": 1}), "running, retry 1/2")

    def test_blocked_with_reason(self):
        self.assertEqual(
            cli._state_summary({"status": "blocked", "reason": "tests failed"}),
            "blocked: tests failed",
        )

    def test_interrupted_with_reason(self):
        self.assertEqual(
            cli._state_summary({"status": "interrupted", "reason": "rate_limited"}),
            "interrupted: rate_limited",
        )

    def test_pending(self):
        self.assertEqual(cli._state_summary(None), "pending")


class RenderTest(unittest.TestCase):
    def test_header_and_rows_when_one_passed(self):
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            run_id = _write_plan(cwd)
            _set_stage_status(cwd, run_id, "stage-1", {"status": "passed", "retry_count": 0})
            out = cli.render_stage_status(cwd, run_id, utf8=False, width=120)
            self.assertIn(f"Run {run_id}", out)
            self.assertIn("1/2 stages passed", out)
            self.assertIn("stage-1", out)
            self.assertIn("stage-2", out)
            self.assertIn("claude", out)
            self.assertIn("codex", out)

    def test_tail_extracted_when_blocked_on_tests(self):
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            run_id = _write_plan(cwd)
            _set_stage_status(cwd, run_id, "stage-1", {
                "status": "blocked",
                "retry_count": 2,
                "reason": "tests failed",
            })
            (cwd / ".ai" / "runs" / run_id / "stages" / "stage-1" / "test-results.md").write_text(
                "# Test results: stage-1 (attempt 3)\n\n## Command 1\n$ pytest x\nexit: 1\n"
                "stderr (last 2000):\nAssertionError: expected 5, got 6\n"
            )
            out = cli.render_stage_status(cwd, run_id, utf8=False, width=120)
            self.assertIn("AssertionError", out)
            self.assertIn("blocked: tests failed", out)


if __name__ == "__main__":
    unittest.main()
