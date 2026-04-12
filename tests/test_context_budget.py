from __future__ import annotations

import unittest

from sdk.context_budget import build_stage_context, build_wrap_context
from sdk.events import StageResult
from sdk.orchestrator import SharedContext
from sdk.token_audit import estimate_tokens


def make_shared_context() -> SharedContext:
    shared_ctx = SharedContext(run_id="test", cwd="/tmp/project", task="Add a multi-stage feature.")
    shared_ctx.add("Project", "Stack: typescript\nCwd: /tmp/project")
    shared_ctx.add("Plan", "Stages:\n- Stage 1: API\n- Stage 2: UI\n\nFull plan:\n" + ("plan " * 500))
    for idx in range(1, 5):
        shared_ctx.add(
            f"Stage Result: Slice {idx}",
            f"Status: PASS\nTests: {idx} passed, 0 failed\nFix attempts: 0\n" + ("detail " * 50),
        )
    shared_ctx.add("Final Review", "warning " * 300)
    return shared_ctx


class ContextBudgetTests(unittest.TestCase):
    def test_compact_stage_context_keeps_core_sections(self) -> None:
        shared_ctx = make_shared_context()
        context = build_stage_context(shared_ctx.sections, max_recent_stage_results=2)
        self.assertIn("## Task", context)
        self.assertIn("## Project", context)
        self.assertIn("## Plan", context)
        self.assertIn("## Stage Result: Slice 4", context)
        self.assertNotIn("## Stage Result: Slice 1", context)

    def test_compact_stage_context_is_smaller_than_full(self) -> None:
        shared_ctx = make_shared_context()
        full = shared_ctx.to_prompt_prefix()
        compact = build_stage_context(shared_ctx.sections, max_recent_stage_results=2)
        self.assertLess(estimate_tokens(compact), estimate_tokens(full))

    def test_compact_wrap_context_keeps_final_review_but_clips_it(self) -> None:
        shared_ctx = make_shared_context()
        wrapped = build_wrap_context(shared_ctx.sections, final_review_chars=120)
        self.assertIn("## Final Review", wrapped)
        self.assertLess(len(wrapped), len(shared_ctx.to_prompt_prefix()))


if __name__ == "__main__":
    unittest.main()
