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
    def test_implement_completes_with_summary_and_passes_write_flag(self):
        # Sequence: task → status (running x1, completed x1) → result.
        # 1.0.2 shape: status returns {job:{status}}, result returns {storedJob:{result:{rawOutput}}}.
        responses = iter([
            _FakeCompletedProcess(stdout=json.dumps({"jobId": "j-123", "status": "queued"})),
            _FakeCompletedProcess(stdout=json.dumps({"job": {"status": "running"}})),
            _FakeCompletedProcess(stdout=json.dumps({"job": {"status": "completed"}})),
            _FakeCompletedProcess(stdout=json.dumps({
                "storedJob": {"result": {"rawOutput": "stage implemented; tests pass"}}
            })),
        ])

        captured_calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            captured_calls.append(list(args))
            return next(responses)

        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.object(codex_call, "_locate_companion", return_value=Path("/fake/companion.mjs")):
                    with patch.object(codex_call, "_sleep", lambda _: None):  # skip the 5s waits
                        out = codex_call.run(
                            mode="implement",
                            run_id="run-test",
                            stage_id="stage-1",
                            cwd=cwd,
                            diff_file=None,
                            retry_context_file=None,
                            test_results_file=None,
                            stack=None,
                        )

            self.assertEqual(out["status"], "completed")
            self.assertIn("stage implemented", out["summary"])

            # First subprocess call is the task launch — must include --write and --prompt-file.
            launch_args = captured_calls[0]
            self.assertIn("task", launch_args)
            self.assertIn("--background", launch_args)
            self.assertIn("--json", launch_args)
            self.assertIn("--write", launch_args)
            self.assertIn("--prompt-file", launch_args)
            # status / result calls must include --json.
            self.assertIn("--json", captured_calls[1])  # status
            self.assertIn("--json", captured_calls[-1])  # result

            # Prompt file should have been written for evidence.
            prompt_file = cwd / ".ai" / "runs" / "run-test" / "stages" / "stage-1" / "codex-implement-prompt.txt"
            self.assertTrue(prompt_file.exists())

    def test_review_mode_does_not_pass_write_flag(self):
        # Review is read-only: codex must NOT be allowed to mutate files.
        responses = iter([
            _FakeCompletedProcess(stdout=json.dumps({"jobId": "j-rev", "status": "queued"})),
            _FakeCompletedProcess(stdout=json.dumps({"job": {"status": "completed"}})),
            _FakeCompletedProcess(stdout=json.dumps({
                "storedJob": {"result": {"rawOutput": "### [P0]\nnone\n"}}
            })),
        ])

        captured_calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            captured_calls.append(list(args))
            return next(responses)

        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.object(codex_call, "_locate_companion", return_value=Path("/fake/companion.mjs")):
                    with patch.object(codex_call, "_sleep", lambda _: None):
                        codex_call.run(
                            mode="review",
                            run_id="run-test",
                            stage_id="stage-1",
                            cwd=cwd,
                            diff_file=None,
                            retry_context_file=None,
                            test_results_file=None,
                            stack=None,
                        )

            launch_args = captured_calls[0]
            self.assertIn("task", launch_args)
            self.assertNotIn("--write", launch_args)


class ErrorPathTest(unittest.TestCase):
    def _run_with_responses(self, responses_iter):
        def fake_run(*args, **kwargs):
            return next(responses_iter)

        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.object(codex_call, "_locate_companion", return_value=Path("/fake/companion.mjs")):
                    with patch.object(codex_call, "_sleep", lambda _: None):
                        return codex_call.run(
                            mode="implement",
                            run_id="run-test",
                            stage_id="stage-1",
                            cwd=cwd,
                            diff_file=None,
                            retry_context_file=None,
                            test_results_file=None,
                            stack=None,
                        )

    def test_rate_limited_raises_rate_limit_error(self):
        responses = iter([
            _FakeCompletedProcess(stdout=json.dumps({"jobId": "j-rl", "status": "queued"})),
            _FakeCompletedProcess(stdout=json.dumps({
                "job": {
                    "status": "failed",
                    "errorMessage": "Anthropic API returned 429: rate limit exceeded",
                }
            })),
        ])
        with self.assertRaises(codex_call.RateLimited):
            self._run_with_responses(responses)

    def test_plugin_missing_raises_file_not_found(self):
        with patch.object(codex_call, "_locate_companion", side_effect=FileNotFoundError("plugin missing")):
            with self.assertRaises(FileNotFoundError):
                with TemporaryDirectory() as tmp:
                    codex_call.run(
                        mode="implement",
                        run_id="run-test",
                        stage_id="stage-1",
                        cwd=Path(tmp),
                        diff_file=None,
                        retry_context_file=None,
                        test_results_file=None,
                        stack=None,
                    )

    def test_main_rate_limited_exits_2(self):
        responses = iter([
            _FakeCompletedProcess(stdout=json.dumps({"jobId": "j-rl", "status": "queued"})),
            _FakeCompletedProcess(stdout=json.dumps({
                "job": {"status": "failed", "errorMessage": "rate limit"}
            })),
        ])

        def fake_run(*args, **kwargs):
            return next(responses)

        argv_backup = sys.argv[:]
        cwd_backup = os.getcwd()
        with TemporaryDirectory() as tmp:
            os.chdir(tmp)
            sys.argv = ["codex_call.py", "implement", "--run-id", "run-x", "--stage-id", "stage-1"]
            try:
                with patch.object(subprocess, "run", side_effect=fake_run):
                    with patch.object(codex_call, "_locate_companion", return_value=Path("/fake/companion.mjs")):
                        with patch.object(codex_call, "_sleep", lambda _: None):
                            rc = codex_call.main()
                self.assertEqual(rc, 2)
            finally:
                sys.argv = argv_backup
                os.chdir(cwd_backup)

    def test_main_other_error_exits_1(self):
        # Simulate task launch failure — no jobId returned.
        responses = iter([
            _FakeCompletedProcess(stdout=json.dumps({"error": "node not found"})),
        ])

        def fake_run(*args, **kwargs):
            return next(responses)

        argv_backup = sys.argv[:]
        cwd_backup = os.getcwd()
        with TemporaryDirectory() as tmp:
            os.chdir(tmp)
            sys.argv = ["codex_call.py", "implement", "--run-id", "run-x", "--stage-id", "stage-1"]
            try:
                with patch.object(subprocess, "run", side_effect=fake_run):
                    with patch.object(codex_call, "_locate_companion", return_value=Path("/fake/companion.mjs")):
                        rc = codex_call.main()
                self.assertEqual(rc, 1)
            finally:
                sys.argv = argv_backup
                os.chdir(cwd_backup)


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
            (plugin_root / "prompts").mkdir(parents=True)
            (plugin_root / "prompts" / "codex-implementer.md").write_text("IMPLEMENT PREFIX")

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
            (plugin_root / "prompts").mkdir(parents=True)
            (plugin_root / "references").mkdir(parents=True)
            (plugin_root / "prompts" / "codex-reviewer.md").write_text("REVIEW PREFIX")
            (plugin_root / "references" / "review-checklist-python.md").write_text("CHECKLIST")

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
