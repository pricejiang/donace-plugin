"""Tests for rate-limit detection in agent dispatch + job_runner.

Runs with stdlib unittest — no new deps. Invoke from repo root:
    python -m unittest sdk.tests.test_rate_limit -v
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

# Make repo root importable so `from sdk import ...` works from test dir.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk.agent_dispatch import AgentDispatcher, RateLimitError, _detect_rate_limit  # noqa: E402
from sdk.job_runner import JobResult, run_job  # noqa: E402
from sdk.events import EventBus, Stage  # noqa: E402


class DetectRateLimitTests(unittest.TestCase):
    """Pure function: given assistant text, return rate-limit snippet or None."""

    def test_matches_canonical_claude_code_phrasing(self):
        # Exact phrase from the user's production log
        text = "You've hit your limit \u00b7 resets 1am (America/Los_Angeles)"
        self.assertIsNotNone(_detect_rate_limit(text))

    def test_matches_lowercase_variant(self):
        self.assertIsNotNone(_detect_rate_limit("you have hit your limit, try again later"))

    def test_matches_rate_limit_phrase(self):
        self.assertIsNotNone(_detect_rate_limit("Request failed: rate limit exceeded"))

    def test_matches_quota_phrase(self):
        self.assertIsNotNone(_detect_rate_limit("Quota exhausted for this hour"))

    def test_matches_resets_at_phrase(self):
        self.assertIsNotNone(_detect_rate_limit("usage cap reached, resets at 11pm"))

    def test_returns_none_for_unrelated_error(self):
        self.assertIsNone(_detect_rate_limit("File not found: foo.txt"))

    def test_returns_none_for_empty(self):
        self.assertIsNone(_detect_rate_limit(""))
        self.assertIsNone(_detect_rate_limit(None))  # type: ignore[arg-type]

    def test_returns_trimmed_snippet(self):
        text = "\n\n  You've hit your limit · resets 1am (America/Los_Angeles)  \n"
        result = _detect_rate_limit(text)
        assert result is not None
        # Should be the clean message, not raw surrounding whitespace.
        self.assertFalse(result.startswith("\n"))
        self.assertFalse(result.endswith(" "))
        self.assertIn("hit your limit", result)

    def test_does_not_match_false_positive_limit(self):
        # "limit" alone should NOT match — too generic.
        self.assertIsNone(_detect_rate_limit("The size limit is 100MB"))
        self.assertIsNone(_detect_rate_limit("Upper limit of retries: 3"))


class RateLimitErrorTests(unittest.TestCase):
    """RateLimitError carries the raw message for downstream surfaces."""

    def test_has_message(self):
        err = RateLimitError("You've hit your limit · resets 1am")
        self.assertEqual(str(err), "You've hit your limit · resets 1am")

    def test_is_runtime_error_subclass(self):
        # So existing `except RuntimeError` paths still catch it if they want.
        self.assertTrue(issubclass(RateLimitError, RuntimeError))


class CodexRateLimitPropagationTests(unittest.TestCase):
    """Codex wrapper paths must not downgrade rate limits to skipped results."""

    def _make_dispatcher(self) -> AgentDispatcher:
        dispatcher = AgentDispatcher(
            agents_dir=str(_REPO_ROOT / "agents"),
            cwd=str(_REPO_ROOT),
            bus=EventBus(run_id="test-run"),
            codex_review_base="HEAD",
        )
        dispatcher._resolve_codex_companion = lambda: ("plugin-root", Path("companion.mjs"), None)  # type: ignore[method-assign]
        return dispatcher

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_codex_review_propagates_rate_limit(self):
        dispatcher = self._make_dispatcher()

        async def rate_limited_command(cmd, codex_plugin_root):
            raise RateLimitError("You've hit your limit \u00b7 resets 1am")

        dispatcher._run_codex_command = rate_limited_command  # type: ignore[method-assign]

        with self.assertRaises(RateLimitError):
            self._run(dispatcher.run_codex_review())

    def test_codex_plan_review_propagates_rate_limit(self):
        dispatcher = self._make_dispatcher()

        async def rate_limited_command(cmd, codex_plugin_root):
            raise RateLimitError("You've hit your limit \u00b7 resets 1am")

        dispatcher._run_codex_command = rate_limited_command  # type: ignore[method-assign]

        with self.assertRaises(RateLimitError):
            self._run(dispatcher.run_codex_plan_review("## Plan"))


class JobRunnerRateLimitTests(unittest.TestCase):
    """job_runner must convert RateLimitError → INTERRUPTED, not BLOCKED.

    BLOCKED counts against the 3-strike retry budget and triggers
    repeated_stage_failure validation. Rate-limits are infra, not plan errors —
    they should pause the run and resume after the quota resets.
    """

    def _make_stage(self) -> Stage:
        return Stage(
            name="Test Stage",
            has_user_facing_changes=False,
            files=["apps/web/x.ts"],
        )

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_rate_limit_in_implementer_is_interrupted_not_blocked(self):
        stage = self._make_stage()
        bus = EventBus(run_id="test-run")

        async def query_that_rate_limits(**kwargs):
            raise RateLimitError("You've hit your limit \u00b7 resets 1am (America/Los_Angeles)")

        async def no_tests(stage):
            return {"passed": 0, "failed": 0, "output": "", "status": "skipped"}

        async def no_codex():
            return {"status": "skipped", "has_issues": False, "output": ""}

        result: JobResult = self._run(run_job(
            stage=stage,
            cwd=str(_REPO_ROOT),
            bus=bus,
            query=query_that_rate_limits,
            run_test_engineer=no_tests,
            run_codex_review=no_codex,
            run_runtime_verifier=None,
            task_context="",
            skip_agents={"test", "codex", "runtime"},
        ))

        self.assertEqual(result.status, "INTERRUPTED", msg=f"got {result}")
        self.assertEqual(result.interrupted_at, "implement")
        assert result.unresolved is not None
        joined = " ".join(result.unresolved)
        self.assertIn("rate", joined.lower())
        self.assertIn("hit your limit", joined)

    def test_rate_limit_in_verifier_is_interrupted(self):
        # Implementer returns fine, but test-engineer hits a rate limit.
        stage = self._make_stage()
        bus = EventBus(run_id="test-run")

        async def query_ok(**kwargs):
            return "implementation done"

        async def tests_rate_limited(stage):
            raise RateLimitError("You've hit your limit \u00b7 resets 1am")

        async def no_codex():
            return {"status": "skipped", "has_issues": False, "output": ""}

        result: JobResult = self._run(run_job(
            stage=stage,
            cwd=str(_REPO_ROOT),
            bus=bus,
            query=query_ok,
            run_test_engineer=tests_rate_limited,
            run_codex_review=no_codex,
            run_runtime_verifier=None,
            task_context="",
            skip_agents={"codex", "runtime"},
        ))

        self.assertEqual(result.status, "INTERRUPTED")
        self.assertEqual(result.interrupted_at, "verify")
        assert result.unresolved is not None
        self.assertIn("rate_limited", " ".join(result.unresolved))

    def test_rate_limit_in_fix_loop_is_interrupted(self):
        # First implementer call OK; tests fail; fix attempt hits rate limit.
        stage = self._make_stage()
        bus = EventBus(run_id="test-run")

        call_count = {"n": 0}

        async def query_fails_on_second(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "first impl"
            raise RateLimitError("rate limit exceeded, resets 2am")

        async def tests_that_fail(stage):
            return {"passed": 0, "failed": 1, "output": "FAIL"}

        async def no_codex():
            return {"status": "skipped", "has_issues": False, "output": ""}

        result: JobResult = self._run(run_job(
            stage=stage,
            cwd=str(_REPO_ROOT),
            bus=bus,
            query=query_fails_on_second,
            run_test_engineer=tests_that_fail,
            run_codex_review=no_codex,
            run_runtime_verifier=None,
            task_context="",
            skip_agents={"codex", "runtime"},
            max_fix_attempts=2,
        ))

        self.assertEqual(result.status, "INTERRUPTED")
        self.assertEqual(result.interrupted_at, "fix_loop")
        assert result.unresolved is not None
        self.assertIn("rate_limited", " ".join(result.unresolved))

    def test_generic_runtime_error_still_blocked(self):
        # Regression check: non-rate-limit exceptions keep old BLOCKED behavior.
        stage = self._make_stage()
        bus = EventBus(run_id="test-run")

        async def query_that_errors(**kwargs):
            raise RuntimeError("agent error: unknown")

        async def no_tests(stage):
            return {"passed": 0, "failed": 0, "output": "", "status": "skipped"}

        async def no_codex():
            return {"status": "skipped", "has_issues": False, "output": ""}

        result: JobResult = self._run(run_job(
            stage=stage,
            cwd=str(_REPO_ROOT),
            bus=bus,
            query=query_that_errors,
            run_test_engineer=no_tests,
            run_codex_review=no_codex,
            run_runtime_verifier=None,
            task_context="",
            skip_agents={"test", "codex", "runtime"},
        ))

        self.assertEqual(result.status, "BLOCKED")


if __name__ == "__main__":
    unittest.main()
