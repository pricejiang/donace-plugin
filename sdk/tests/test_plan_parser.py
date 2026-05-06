"""Tests for plan.md parsing in sdk.cli."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk import cli


_PLAN_FIXTURE = """# Plan: example feature

## Stage 1: add health endpoint
- implementer: claude
- goal: expose /health returning 200 OK with build sha
- files: src/server.py, src/server_test.py
- success criteria:
  - GET /health returns 200
  - Response body includes git sha
- tests:
  - python3 -m unittest src.server_test -v

## Stage 2: rename UserSvc fields
- implementer: codex
- goal: rename created -> created_at, updated -> updated_at
- files: src/user.py, migrations/0042.sql
- success criteria:
  - All callers updated
- tests:
  - none: mechanical rename; existing suite covers behavior
"""


class PlanParserTest(unittest.TestCase):
    def _write_plan(self, tmp: Path, content: str) -> str:
        run_id = cli.run_start(cwd=tmp)
        plan = tmp / ".ai" / "runs" / run_id / "plan.md"
        plan.write_text(content)
        return run_id

    def test_parses_two_stages(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_id = self._write_plan(tmp_path, _PLAN_FIXTURE)
            stages = cli.parse_plan(tmp_path, run_id)
            self.assertEqual(len(stages), 2)
            self.assertEqual(stages[0]["sid"], "stage-1")
            self.assertEqual(stages[0]["implementer"], "claude")
            self.assertEqual(stages[0]["name"], "add health endpoint")
            self.assertEqual(stages[1]["implementer"], "codex")
            self.assertEqual(
                stages[0]["tests"],
                ["python3 -m unittest src.server_test -v"],
            )
            self.assertEqual(stages[1]["tests"], ["none: mechanical rename; existing suite covers behavior"])

    def test_invalid_implementer_raises(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad = _PLAN_FIXTURE.replace("implementer: claude", "implementer: gpt-9")
            run_id = self._write_plan(tmp_path, bad)
            with self.assertRaises(ValueError):
                cli.parse_plan(tmp_path, run_id)

    def test_missing_required_field_raises(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Drop the "tests:" block from stage 1.
            bad = _PLAN_FIXTURE.replace("- tests:\n  - python3 -m unittest src.server_test -v\n", "")
            run_id = self._write_plan(tmp_path, bad)
            with self.assertRaises(ValueError):
                cli.parse_plan(tmp_path, run_id)


if __name__ == "__main__":
    unittest.main()
