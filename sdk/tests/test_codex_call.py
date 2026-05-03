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


class ErrorPathTest(unittest.TestCase):
    def _run_with_responses(self, responses_iter):
        def fake_run(*args, **kwargs):
            return next(responses_iter)

        with patch.object(subprocess, "run", side_effect=fake_run):
            with patch.object(codex_call, "_locate_companion", return_value=Path("/fake/companion.mjs")):
                with patch.object(codex_call, "_sleep", lambda _: None):
                    return codex_call.run(
                        mode="implement",
                        run_id="run-test",
                        stage_id="stage-1",
                        cwd=Path("/fake/project"),
                        diff_file=None,
                        test_results_file=None,
                        stack=None,
                    )

    def test_rate_limited_raises_rate_limit_error(self):
        responses = iter([
            _FakeCompletedProcess(stdout=json.dumps({"jobId": "j-rl"})),
            _FakeCompletedProcess(stdout=json.dumps({
                "status": "failed",
                "error": "Anthropic API returned 429: rate limit exceeded"
            })),
        ])
        with self.assertRaises(codex_call.RateLimited):
            self._run_with_responses(responses)

    def test_plugin_missing_raises_file_not_found(self):
        with patch.object(codex_call, "_locate_companion", side_effect=FileNotFoundError("plugin missing")):
            with self.assertRaises(FileNotFoundError):
                codex_call.run(
                    mode="implement",
                    run_id="run-test",
                    stage_id="stage-1",
                    cwd=Path("/fake/project"),
                    diff_file=None,
                    test_results_file=None,
                    stack=None,
                )

    def test_main_rate_limited_exits_2(self):
        responses = iter([
            _FakeCompletedProcess(stdout=json.dumps({"jobId": "j-rl"})),
            _FakeCompletedProcess(stdout=json.dumps({
                "status": "failed",
                "error": "rate limit"
            })),
        ])

        def fake_run(*args, **kwargs):
            return next(responses)

        argv_backup = sys.argv[:]
        sys.argv = ["codex_call.py", "implement", "--run-id", "run-x", "--stage-id", "stage-1"]
        try:
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.object(codex_call, "_locate_companion", return_value=Path("/fake/companion.mjs")):
                    with patch.object(codex_call, "_sleep", lambda _: None):
                        rc = codex_call.main()
            self.assertEqual(rc, 2)
        finally:
            sys.argv = argv_backup

    def test_main_other_error_exits_1(self):
        # Simulate task launch failure — no jobId returned.
        responses = iter([
            _FakeCompletedProcess(stdout=json.dumps({"error": "node not found"})),
        ])

        def fake_run(*args, **kwargs):
            return next(responses)

        argv_backup = sys.argv[:]
        sys.argv = ["codex_call.py", "implement", "--run-id", "run-x", "--stage-id", "stage-1"]
        try:
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.object(codex_call, "_locate_companion", return_value=Path("/fake/companion.mjs")):
                    rc = codex_call.main()
            self.assertEqual(rc, 1)
        finally:
            sys.argv = argv_backup


if __name__ == "__main__":
    unittest.main()
