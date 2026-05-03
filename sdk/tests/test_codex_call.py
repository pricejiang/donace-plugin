"""Tests for sdk.codex_call.

Mock subprocess invocations of codex-companion.mjs; verify the JSON output
contract on stdout and the exit-code contract.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
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
                        retry_context_file=None,
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
                        retry_context_file=None,
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
                    retry_context_file=None,
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


class PromptBuildTest(unittest.TestCase):
    def test_extract_stage_block_returns_only_target_stage(self):
        plan_text = """# Plan: example

## Stage 1: first
- implementer: claude
- goal: one

## Stage 2: second
- implementer: codex
- goal: two
"""
        block = codex_call._extract_stage_block(plan_text, "stage-2")
        self.assertIn("## Stage 2: second", block)
        self.assertIn("- goal: two", block)
        self.assertNotIn("## Stage 1: first", block)

    def test_build_prompt_for_implement_includes_stage_block_and_retry_context(self):
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            plugin_root = cwd / "plugin"
            (plugin_root / "agents" / "prompts").mkdir(parents=True)
            (plugin_root / "agents" / "prompts" / "codex-implementer.md").write_text("IMPLEMENT PREFIX")

            run_dir = cwd / ".ai" / "runs" / "run-test"
            run_dir.mkdir(parents=True)
            (run_dir / "plan.md").write_text(
                "# Plan: example\n\n"
                "## Stage 1: add endpoint\n"
                "- implementer: codex\n"
                "- goal: add endpoint\n"
                "- files: a.py\n"
                "- success criteria:\n"
                "  - works\n"
                "- tests:\n"
                "  - python3 -m unittest\n"
                "\n"
                "## Stage 2: follow-up\n"
                "- implementer: claude\n"
                "- goal: another\n"
                "- files: b.py\n"
                "- success criteria:\n"
                "  - works\n"
                "- tests:\n"
                "  - none: docs-only\n"
            )
            retry_path = run_dir / "retry-context.md"
            retry_path.write_text("stderr tail goes here")

            old_plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
            os.environ["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
            try:
                prompt = codex_call._build_prompt(
                    "implement",
                    "run-test",
                    "stage-1",
                    cwd,
                    None,
                    retry_path,
                    None,
                    None,
                )
            finally:
                if old_plugin_root is None:
                    os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
                else:
                    os.environ["CLAUDE_PLUGIN_ROOT"] = old_plugin_root

            self.assertIn("IMPLEMENT PREFIX", prompt)
            self.assertIn("stage block:", prompt)
            self.assertIn("## Stage 1: add endpoint", prompt)
            self.assertNotIn("## Stage 2: follow-up", prompt)
            self.assertIn("retry context:", prompt)
            self.assertIn("stderr tail goes here", prompt)

    def test_build_prompt_for_review_includes_stage_block_diff_and_tests(self):
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            plugin_root = cwd / "plugin"
            (plugin_root / "agents" / "prompts").mkdir(parents=True)
            (plugin_root / "agents" / "references").mkdir(parents=True)
            (plugin_root / "agents" / "prompts" / "codex-reviewer.md").write_text("REVIEW PREFIX")
            (plugin_root / "agents" / "references" / "review-checklist-python.md").write_text("CHECKLIST")

            run_dir = cwd / ".ai" / "runs" / "run-test"
            run_dir.mkdir(parents=True)
            (run_dir / "plan.md").write_text(
                "# Plan: example\n\n"
                "## Stage 1: add endpoint\n"
                "- implementer: claude\n"
                "- goal: add endpoint\n"
                "- files: a.py\n"
                "- success criteria:\n"
                "  - works\n"
                "- tests:\n"
                "  - python3 -m unittest\n"
            )
            diff_path = run_dir / "diff.patch"
            diff_path.write_text("diff --git a/a.py b/a.py")
            test_results_path = run_dir / "test-results.md"
            test_results_path.write_text("# Test results\n\nexit: 0\n")

            old_plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
            os.environ["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
            try:
                prompt = codex_call._build_prompt(
                    "review",
                    "run-test",
                    "stage-1",
                    cwd,
                    diff_path,
                    None,
                    test_results_path,
                    "python",
                )
            finally:
                if old_plugin_root is None:
                    os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
                else:
                    os.environ["CLAUDE_PLUGIN_ROOT"] = old_plugin_root

            self.assertIn("REVIEW PREFIX", prompt)
            self.assertIn("## Stage 1: add endpoint", prompt)
            self.assertIn("diff --git a/a.py b/a.py", prompt)
            self.assertIn("# Test results", prompt)
            self.assertIn("CHECKLIST", prompt)


if __name__ == "__main__":
    unittest.main()
