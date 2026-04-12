from __future__ import annotations

import asyncio
import unittest

from sdk.events import EventBus, Stage
from sdk.orchestrator import SharedContext
from sdk.sprint_loop import _run_single_stage, _select_implementer_context


def make_shared_context() -> SharedContext:
    shared_ctx = SharedContext(
        run_id="rollout",
        cwd="/tmp/project",
        task="Ship a multi-stage reporting improvement safely.",
    )
    shared_ctx.add("Project", "Stack: python\nCwd: /tmp/project")
    shared_ctx.add("Plan", "Stages:\n- Stage 1: API\n- Stage 2: UI\n\nFull plan:\n" + ("plan " * 300))
    shared_ctx.add("Stage Result: API", "Status: PASS\nTests: 5 passed, 0 failed\nFix attempts: 0\n" + ("detail " * 40))
    return shared_ctx


class SprintLoopCompactRolloutTests(unittest.TestCase):
    def test_select_implementer_context_prefers_compact_sections(self) -> None:
        shared_ctx = make_shared_context()
        full = shared_ctx.to_prompt_prefix()
        compact = _select_implementer_context(full, shared_ctx.sections)

        self.assertIn("## Task", compact)
        self.assertIn("## Plan", compact)
        self.assertNotIn("Run Context: rollout", compact)
        self.assertLess(len(compact), len(full))

    def test_single_stage_uses_compact_context_for_implementer_prompt(self) -> None:
        shared_ctx = make_shared_context()
        full_context = shared_ctx.to_prompt_prefix()
        prompts: list[tuple[str, str]] = []

        async def fake_query(agent: str, prompt: str, model: str = "sonnet", **_: object) -> str:
            prompts.append((agent, prompt))
            if agent == "runtime-evaluator":
                return "contract"
            return "ok"

        async def fake_tests(stage: Stage) -> dict:
            return {"passed": 3, "failed": 0, "output": "TEST_SUMMARY: passed=3 failed=0"}

        async def fake_codex() -> dict:
            return {"status": "skipped", "has_issues": False, "output": ""}

        async def fake_runtime(_: str) -> dict:
            return {"status": "PASS", "score": "1/1", "output": ""}

        result = asyncio.run(
            _run_single_stage(
                Stage(name="Token Audit Fast-Path Modeling", has_user_facing_changes=False),
                idx=0,
                total_stages=1,
                bus=EventBus(run_id="rollout", interactive=False),
                query=fake_query,
                run_test_engineer=fake_tests,
                run_codex_review=fake_codex,
                run_runtime_evaluator=fake_runtime,
                task_context=full_context,
                warnings=[],
                task_context_sections=shared_ctx.sections,
            )
        )

        self.assertEqual(result.status, "PASS")
        implementer_prompt = next(prompt for agent, prompt in prompts if agent == "implementer")
        self.assertIn("<run-context>", implementer_prompt)
        self.assertIn("## Task", implementer_prompt)
        self.assertIn("## Plan", implementer_prompt)
        self.assertNotIn("Run Context: rollout", implementer_prompt)


if __name__ == "__main__":
    unittest.main()
