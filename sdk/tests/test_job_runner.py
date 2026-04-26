"""Tests for job_runner.py behavior paths + cmd_run_job routing decisions.

Runs with stdlib unittest — no new deps. Invoke from repo root:
    python3 -m unittest sdk.tests.test_job_runner -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk import commands as sdk_cmds  # noqa: E402
from sdk.commands import cmd_run_job  # noqa: E402
from sdk.events import EventBus, Stage  # noqa: E402
from sdk.job_runner import JobResult, _collect_failures, run_job  # noqa: E402


class CollectFailuresSeverityTests(unittest.TestCase):
    """_collect_failures: runtime verification FAIL must be blocking.

    Background: the runtime-verifier runs the live app against the plan's
    Success Criteria. A FAIL means a must-pass criterion is broken — that
    is functionally the same as a failing test, not a reviewer opinion.
    Historical behavior downgraded it to 'warning' alongside codex review,
    which let real bugs sail through as PASS (see run-phase5-runA-68cce6952764
    stages 1 and 6: runtime_result.status=FAIL, fix_attempts=0, JobResult=PASS).
    """

    def test_runtime_fail_is_error_severity(self):
        failures = _collect_failures(
            test_result={"passed": 1, "failed": 0, "output": "ok"},
            codex_result={"status": "completed", "has_issues": False, "output": ""},
            runtime_result={"status": "FAIL", "score": "8/9", "output": "criterion broken"},
        )
        runtime_fs = [f for f in failures if f["source"] == "runtime-verifier"]
        self.assertEqual(len(runtime_fs), 1, "runtime FAIL must appear in failures")
        self.assertEqual(
            runtime_fs[0]["severity"],
            "error",
            msg="runtime FAIL must be 'error' so it blocks the job + triggers fix loop",
        )

    def test_runtime_pass_is_not_a_failure(self):
        failures = _collect_failures(
            test_result={"passed": 1, "failed": 0, "output": "ok"},
            codex_result={"status": "completed", "has_issues": False, "output": ""},
            runtime_result={"status": "PASS", "score": "10/10", "output": ""},
        )
        runtime_fs = [f for f in failures if f["source"] == "runtime-verifier"]
        self.assertEqual(runtime_fs, [], "runtime PASS must not produce any failure entry")

    def test_runtime_crash_is_error_severity(self):
        # Crashes already mapped to error pre-fix — guard against regression.
        failures = _collect_failures(
            test_result={"passed": 1, "failed": 0, "output": "ok"},
            codex_result={"status": "completed", "has_issues": False, "output": ""},
            runtime_result={"status": "error", "error": "verifier crashed"},
        )
        runtime_fs = [f for f in failures if f["source"] == "runtime-verifier"]
        self.assertEqual(len(runtime_fs), 1)
        self.assertEqual(runtime_fs[0]["severity"], "error")


class RunJobRuntimeFailTests(unittest.TestCase):
    """Full run_job flow: runtime FAIL must enter fix loop + return BLOCKED.

    End-to-end guard that the severity fix actually propagates through
    the fix-loop gate at line 290 and the BLOCKED return at line 396.
    """

    def _make_stage(self) -> Stage:
        return Stage(
            name="Test Stage",
            has_user_facing_changes=False,
            files=["apps/web/x.ts"],
        )

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_runtime_fail_with_tests_passing_blocks_with_fix_attempt(self):
        stage = self._make_stage()
        bus = EventBus(run_id="test-run")

        query_calls = {"n": 0}
        runtime_calls = {"n": 0}

        async def query_impl(**kwargs):
            query_calls["n"] += 1
            return "implementation attempted"

        async def tests_pass(stage):
            return {"passed": 1, "failed": 0, "output": "ok"}

        async def codex_clean():
            return {"status": "completed", "has_issues": False, "output": ""}

        async def runtime_fail(stage_name, task_context):
            runtime_calls["n"] += 1
            return {"status": "FAIL", "score": "8/9", "output": "PRIVATE owner 404"}

        result: JobResult = self._run(run_job(
            stage=stage,
            cwd=str(_REPO_ROOT),
            bus=bus,
            query=query_impl,
            run_test_engineer=tests_pass,
            run_codex_review=codex_clean,
            run_runtime_verifier=runtime_fail,
            task_context="",
            skip_agents=set(),
            max_fix_attempts=1,
        ))

        self.assertEqual(
            result.status,
            "BLOCKED",
            msg=f"runtime FAIL should block the job; got {result.status}",
        )
        self.assertGreaterEqual(
            result.fix_attempts,
            1,
            msg="fix loop must have entered (severity=error)",
        )
        self.assertEqual(
            query_calls["n"],
            2,
            msg="implementer called once + fix call once",
        )
        self.assertEqual(
            runtime_calls["n"],
            2,
            msg="runtime-verifier must run once initially and once after the fix attempt",
        )
        self.assertEqual(result.runtime_result, {
            "status": "FAIL", "score": "8/9", "output": "PRIVATE owner 404",
        })
        assert result.unresolved is not None
        joined = " ".join(result.unresolved)
        self.assertIn("PRIVATE owner 404", joined)

    def test_runtime_fail_then_pass_after_fix_returns_pass(self):
        stage = self._make_stage()
        bus = EventBus(run_id="test-run")

        query_calls = {"n": 0}
        runtime_calls = {"n": 0}

        async def query_impl(**kwargs):
            query_calls["n"] += 1
            return "implementation or fix attempted"

        async def tests_pass(stage):
            return {"passed": 1, "failed": 0, "output": "ok"}

        async def codex_clean():
            return {"status": "completed", "has_issues": False, "output": ""}

        async def runtime_fails_then_passes(stage_name, task_context):
            runtime_calls["n"] += 1
            if runtime_calls["n"] == 1:
                return {"status": "FAIL", "score": "8/9", "output": "PRIVATE owner 404"}
            return {"status": "PASS", "score": "9/9", "output": "all runtime criteria passed"}

        result: JobResult = self._run(run_job(
            stage=stage,
            cwd=str(_REPO_ROOT),
            bus=bus,
            query=query_impl,
            run_test_engineer=tests_pass,
            run_codex_review=codex_clean,
            run_runtime_verifier=runtime_fails_then_passes,
            task_context="",
            skip_agents=set(),
            max_fix_attempts=1,
        ))

        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.fix_attempts, 1)
        self.assertEqual(query_calls["n"], 2)
        self.assertEqual(runtime_calls["n"], 2)
        self.assertEqual(result.runtime_result, {
            "status": "PASS", "score": "9/9", "output": "all runtime criteria passed",
        })

    def test_runtime_pass_does_not_enter_fix_loop(self):
        stage = self._make_stage()
        bus = EventBus(run_id="test-run")

        query_calls = {"n": 0}

        async def query_impl(**kwargs):
            query_calls["n"] += 1
            return "implementation done"

        async def tests_pass(stage):
            return {"passed": 1, "failed": 0, "output": "ok"}

        async def codex_clean():
            return {"status": "completed", "has_issues": False, "output": ""}

        async def runtime_ok(stage_name, task_context):
            return {"status": "PASS", "score": "10/10", "output": ""}

        result: JobResult = self._run(run_job(
            stage=stage,
            cwd=str(_REPO_ROOT),
            bus=bus,
            query=query_impl,
            run_test_engineer=tests_pass,
            run_codex_review=codex_clean,
            run_runtime_verifier=runtime_ok,
            task_context="",
            skip_agents=set(),
            max_fix_attempts=1,
        ))

        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.fix_attempts, 0)
        self.assertEqual(query_calls["n"], 1, "only implementer call, no fix")


# ---------------------------------------------------------------------------
# cmd_run_job routing — verify-only stages (files=[])
# ---------------------------------------------------------------------------


def _capture_bus_patch():
    """Mirror of test_dashboard_events._capture_bus_patch — intercept _setup_bus."""
    captured: list[EventBus] = []

    async def fake_setup_bus(run_id, dashboard_url, job_id="", interactive=False, cwd=""):
        bus = EventBus(run_id=run_id, interactive=interactive)
        captured.append(bus)
        return bus, None

    return patch.object(sdk_cmds, "_setup_bus", fake_setup_bus), captured


class EmptyFilesStageRoutingTests(unittest.TestCase):
    """cmd_run_job: stage with `files: []` routes to runtime-verifier only.

    Background: the implementer prompt forbids running tests/curl/typecheck,
    so a stage whose Success Criteria are all automated checks + manual QA
    (no files to write) has nothing for the implementer to do. It would
    correctly raise NEEDS_CONTEXT and BLOCK the run — but only after wasting
    an implementer dispatch (~900s potential) and confusing the dashboard.

    Repro: run-phase5-runA-68cce6952764 stage-8 ('Typecheck, Lint, and
    Manual QA', files=[]) → implementer NEEDS_CONTEXT → BLOCKED.

    Fix: route files=[] stages to dispatcher.run_runtime_verifier directly,
    bypassing implementer/test-engineer/codex-review.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = self._tmp.name
        self.run_id = "run-empty-files-test"
        run_dir = Path(self.cwd) / ".ai" / "runs" / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self.plan_path = run_dir / "plan.json"
        self.plan_path.write_text(json.dumps({
            "plan_file": "plan.md",
            "stages": [
                {
                    "id": "stage-verify",
                    "name": "Typecheck and QA",
                    "files": [],
                    "dependencies": [],
                    "has_user_facing_changes": False,
                    "estimated_turns": 0,
                },
            ],
        }))

    def tearDown(self):
        self._tmp.cleanup()

    def _run_stage(
        self,
        runtime_status: str,
        runtime_output: str = "",
        runtime_exc: Exception | None = None,
    ) -> tuple[dict, dict]:
        """Drive cmd_run_job for stage-verify with a fake runtime verdict.

        Returns (result_dict, call_counts).
        """
        calls = {"query": 0, "test": 0, "codex": 0, "runtime": 0}

        class FakeDispatcher:
            def __init__(self, *a, **kw):
                pass

            async def query(self, *a, **kw):
                calls["query"] += 1
                return ""

            async def run_test_engineer(self, *a, **kw):
                calls["test"] += 1
                return {"passed": 0, "failed": 0, "output": ""}

            async def run_codex_review(self):
                calls["codex"] += 1
                return {"status": "skipped", "has_issues": False, "output": ""}

            async def run_runtime_verifier(self, stage_name, task_context):
                calls["runtime"] += 1
                if runtime_exc:
                    raise runtime_exc
                return {
                    "status": runtime_status,
                    "score": "1/1",
                    "output": runtime_output or f"runtime said {runtime_status}",
                }

        patch_obj, _captured = _capture_bus_patch()
        captured_results: list[dict] = []

        def capture_write(cwd, run_id, job_id, payload):
            captured_results.append(payload)

        with patch_obj, \
             patch("sdk.commands._register_job", return_value=Path(self.cwd) / "fake.lock"), \
             patch("sdk.commands._unregister_job"), \
             patch("sdk.commands._write_job_result", side_effect=capture_write), \
             patch("sdk.commands._git_head", return_value=""), \
             patch("sdk.commands._git_dirty_paths", return_value=[]), \
             patch("sdk.commands._git_commit_stage", return_value={"status": "skipped"}), \
             patch("sdk.commands._load_context", return_value=""), \
             patch("sdk.agent_dispatch.AgentDispatcher", FakeDispatcher):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(cmd_run_job(
                    stage_id="stage-verify",
                    plan_path=str(self.plan_path),
                    cwd=self.cwd,
                    run_id=self.run_id,
                    dashboard_url=None,
                ))
            finally:
                loop.close()

        return result, calls

    def test_pass_runs_runtime_verifier_only(self):
        result, calls = self._run_stage("PASS", "all criteria met")

        self.assertEqual(calls["query"], 0, "implementer must not run for files=[] stage")
        self.assertEqual(calls["test"], 0, "test-engineer must not run")
        self.assertEqual(calls["codex"], 0, "codex review must not run")
        self.assertEqual(calls["runtime"], 1, "runtime-verifier must run exactly once")

        self.assertEqual(result["status"], "PASS")
        self.assertIsNotNone(result.get("runtime_result"))
        self.assertEqual(result["runtime_result"]["status"], "PASS")

    def test_fail_blocks_the_stage(self):
        result, calls = self._run_stage("FAIL", "typecheck error in foo.ts")

        self.assertEqual(calls["runtime"], 1)
        self.assertEqual(calls["query"], 0)
        self.assertEqual(
            result["status"],
            "BLOCKED",
            msg=f"runtime FAIL on verify-only stage must BLOCK; got {result}",
        )
        assert result.get("unresolved")
        self.assertIn("typecheck error", " ".join(result["unresolved"]))

    def test_runtime_crash_blocks_the_stage(self):
        result, calls = self._run_stage("PASS", runtime_exc=RuntimeError("browser crashed"))

        self.assertEqual(calls["runtime"], 1)
        self.assertEqual(calls["query"], 0)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["runtime_result"], {
            "status": "error",
            "error": "browser crashed",
        })
        assert result.get("unresolved")
        self.assertIn("runtime-verifier crashed: browser crashed", " ".join(result["unresolved"]))


class VerifyOnlyFastPathSanityCheckTests(unittest.TestCase):
    """cmd_run_job: refuse the verify-only fast-path when plan.md disagrees.

    Repro: run-phase5_5-96ed1d5cf406. plan.md declares 3 new files for
    Stage 1 in bullet-list form; the (then-broken) parser silently produced
    files=[] in plan.json; the fast-path skipped implementer; runtime-verifier
    saw nothing on disk and BLOCKED 3 times in a row.

    Even with the parser fixed, a stale plan.json from before the fix could
    still trigger this. Treat it as ERROR so team-lead stops retrying and
    re-runs the plan command.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = self._tmp.name
        self.run_id = "run-stale-planjson"
        run_dir = Path(self.cwd) / ".ai" / "runs" / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        # plan.md declares files via bullet list — what real plans actually use.
        (run_dir / "plan.md").write_text(
            "# Implementation Plan\n\n"
            "## Stage 1: Display Types\n"
            "**Goal**: create types\n"
            "**Files to modify**:\n"
            "\n"
            "- `apps/web/lib/types/character.ts` (new)\n"
            "- `apps/web/lib/types/world.ts` (new)\n"
            "\n"
            "**Dependencies**: None\n"
            "**Estimated turns**: 5\n"
        )
        # plan.json has files=[] — what an old buggy parser would have written.
        self.plan_path = run_dir / "plan.json"
        self.plan_path.write_text(json.dumps({
            "plan_file": "plan.md",
            "stages": [
                {
                    "id": "stage-1",
                    "name": "Display Types",
                    "files": [],
                    "dependencies": [],
                    "has_user_facing_changes": False,
                    "estimated_turns": 5,
                },
            ],
        }))

    def tearDown(self):
        self._tmp.cleanup()

    def _drive(self, *, stage_id: str = "stage-1") -> tuple[dict, dict]:
        calls = {"query": 0, "test": 0, "codex": 0, "runtime": 0}

        class FakeDispatcher:
            def __init__(self, *a, **kw):
                pass

            async def query(self, *a, **kw):
                calls["query"] += 1
                return ""

            async def run_test_engineer(self, *a, **kw):
                calls["test"] += 1
                return {"passed": 0, "failed": 0, "output": ""}

            async def run_codex_review(self):
                calls["codex"] += 1
                return {"status": "skipped", "has_issues": False, "output": ""}

            async def run_runtime_verifier(self, stage_name, task_context):
                calls["runtime"] += 1
                return {"status": "PASS", "score": "1/1", "output": ""}

        patch_obj, _captured = _capture_bus_patch()

        with patch_obj, \
             patch("sdk.commands._register_job", return_value=Path(self.cwd) / "fake.lock"), \
             patch("sdk.commands._unregister_job"), \
             patch("sdk.commands._write_job_result"), \
             patch("sdk.commands._git_head", return_value=""), \
             patch("sdk.commands._git_dirty_paths", return_value=[]), \
             patch("sdk.commands._git_commit_stage", return_value={"status": "skipped"}), \
             patch("sdk.commands._load_context", return_value=""), \
             patch("sdk.agent_dispatch.AgentDispatcher", FakeDispatcher):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(cmd_run_job(
                    stage_id=stage_id,
                    plan_path=str(self.plan_path),
                    cwd=self.cwd,
                    run_id=self.run_id,
                    dashboard_url=None,
                ))
            finally:
                loop.close()
        return result, calls

    def test_stale_planjson_with_files_in_planmd_is_rejected(self):
        result, calls = self._drive()

        self.assertEqual(
            calls["runtime"], 0,
            msg="must not silently route to runtime-verifier when plan.md "
                "declares files but plan.json's stage.files is []",
        )
        self.assertEqual(calls["query"], 0)
        self.assertEqual(
            result["status"], "ERROR",
            msg=f"must surface as ERROR (parsing/data mismatch); got {result}",
        )
        # Message must point at the cause and the recovery action.
        msg = result.get("error", "") or " ".join(result.get("unresolved", []))
        self.assertIn("plan.md", msg.lower())
        self.assertIn("stage-1", msg.lower())

    def test_duplicate_stage_names_do_not_false_positive(self):
        run_dir = Path(self.cwd) / ".ai" / "runs" / self.run_id
        (run_dir / "plan.md").write_text(
            "# Implementation Plan\n\n"
            "## Stage 1: Cleanup\n"
            "**Files to modify**:\n"
            "- `apps/web/lib/a.ts` (modify)\n"
            "\n"
            "**Dependencies**: None\n"
            "**Estimated turns**: 1\n"
            "\n"
            "## Stage 2: Cleanup\n"
            "**Files to modify**: None\n"
            "**Dependencies**: Stage 1\n"
            "**Estimated turns**: 0\n"
        )
        self.plan_path.write_text(json.dumps({
            "plan_file": "plan.md",
            "stages": [
                {
                    "id": "stage-1",
                    "name": "Cleanup",
                    "files": ["`apps/web/lib/a.ts`"],
                    "dependencies": [],
                    "has_user_facing_changes": False,
                    "estimated_turns": 1,
                },
                {
                    "id": "stage-2",
                    "name": "Cleanup",
                    "files": [],
                    "dependencies": ["Cleanup"],
                    "has_user_facing_changes": False,
                    "estimated_turns": 0,
                },
            ],
        }))

        result, calls = self._drive(stage_id="stage-2")

        self.assertEqual(
            result["status"], "PASS",
            msg=f"later verify-only stage must not inherit files from an earlier same-name stage; got {result}",
        )
        self.assertEqual(calls["runtime"], 1)
        self.assertEqual(calls["query"], 0)


if __name__ == "__main__":
    unittest.main()
