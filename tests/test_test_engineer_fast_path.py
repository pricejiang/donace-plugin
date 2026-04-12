from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from sdk.agent_dispatch import AgentDispatcher
from sdk.events import Stage


class TestEngineerFastPathTests(unittest.TestCase):
    def test_doc_stage_runs_local_unittest_without_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tests_dir = Path(tmpdir) / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_sample.py").write_text(
                "import unittest\n\n"
                "class SampleTest(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n"
            )

            dispatcher = AgentDispatcher.__new__(AgentDispatcher)
            dispatcher.cwd = tmpdir

            async def fake_get_changed_files() -> str:
                return "README.md"

            async def fake_query(*args, **kwargs):
                raise AssertionError("Claude query should not be used for doc-only fast path")

            dispatcher._get_changed_files = fake_get_changed_files  # type: ignore[attr-defined]
            dispatcher.query = fake_query  # type: ignore[method-assign]

            result = asyncio.run(
                dispatcher.run_test_engineer(
                    Stage(name="Rename README title and keep changes minimal", has_user_facing_changes=False)
                )
            )

        self.assertEqual(result["failed"], 0)
        self.assertGreaterEqual(result["passed"], 1)
        self.assertIn("Fast path", result["output"])

    def test_non_doc_stage_falls_back_to_agent_query(self) -> None:
        dispatcher = AgentDispatcher.__new__(AgentDispatcher)
        dispatcher.cwd = "/tmp/project"

        async def fake_get_changed_files() -> str:
            return "sdk/orchestrator.py"

        async def fake_query(*args, **kwargs):
            return "Ran checks\nTEST_SUMMARY: passed=2 failed=1"

        dispatcher._get_changed_files = fake_get_changed_files  # type: ignore[attr-defined]
        dispatcher.query = fake_query  # type: ignore[method-assign]

        result = asyncio.run(
            dispatcher.run_test_engineer(
                Stage(name="Implement orchestrator state cleanup", has_user_facing_changes=False)
            )
        )

        self.assertEqual(result["passed"], 2)
        self.assertEqual(result["failed"], 1)


if __name__ == "__main__":
    unittest.main()
