"""Tests for `tests:` bullet handling in sdk.cli (skip none:, run runnable, persist results)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk import cli


class IsNoneMarkerTest(unittest.TestCase):
    def test_none_prefix_skipped(self):
        self.assertTrue(cli.is_none_marker("none: docs-only stage"))
        self.assertTrue(cli.is_none_marker("none: mechanical rename"))

    def test_real_command_not_skipped(self):
        self.assertFalse(cli.is_none_marker("python3 -m unittest tests"))
        self.assertFalse(cli.is_none_marker("npm test"))
        self.assertFalse(cli.is_none_marker("nonexistent_cmd"))


class TestResultsPersistTest(unittest.TestCase):
    def test_format_test_results_md_two_commands(self):
        out = cli.format_test_results_md(
            sid="stage-1",
            attempt=1,
            results=[
                {"command": "python3 -m unittest x", "exit_code": 0, "stdout": "ok\n", "stderr": ""},
                {"command": "npm test", "exit_code": 1, "stdout": "1 failed", "stderr": "AssertionError"},
            ],
        )
        self.assertIn("# Test results: stage-1 (attempt 1)", out)
        self.assertIn("$ python3 -m unittest x", out)
        self.assertIn("exit: 0", out)
        self.assertIn("exit: 1", out)
        self.assertIn("AssertionError", out)


if __name__ == "__main__":
    unittest.main()
