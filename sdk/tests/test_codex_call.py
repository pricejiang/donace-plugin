"""Tests for sdk.codex_call.

Mock subprocess invocations of codex-companion.mjs; verify the JSON output
contract on stdout and the exit-code contract.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk import codex_call


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class HappyPathTest(unittest.TestCase):
    def test_implement_completes_with_summary(self):
        # Sequence: task → status (running x1, completed x1) → result.
        responses = iter([
            _FakeCompletedProcess(stdout=json.dumps({"jobId": "j-123"})),
            _FakeCompletedProcess(stdout=json.dumps({"status": "running"})),
            _FakeCompletedProcess(stdout=json.dumps({"status": "completed"})),
            _FakeCompletedProcess(stdout=json.dumps({
                "finalMessage": "stage implemented; tests pass"
            })),
        ])

        def fake_run(*args, **kwargs):
            return next(responses)

        with patch.object(subprocess, "run", side_effect=fake_run):
            with patch.object(codex_call, "_locate_companion", return_value=Path("/fake/companion.mjs")):
                with patch.object(codex_call, "_sleep", lambda _: None):  # skip the 5s waits
                    out = codex_call.run(
                        mode="implement",
                        run_id="run-test",
                        stage_id="stage-1",
                        cwd=Path("/fake/project"),
                        diff_file=None,
                        test_results_file=None,
                        stack=None,
                    )

        self.assertEqual(out["status"], "completed")
        self.assertIn("stage implemented", out["summary"])


if __name__ == "__main__":
    unittest.main()
