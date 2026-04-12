from __future__ import annotations

import unittest

from sdk.orchestrator import _parse_plan_stages


class PlanParseTests(unittest.TestCase):
    def test_dependency_comments_do_not_create_fake_stage_names(self) -> None:
        content = """
# Plan

## Stage 1: Validator Fast-Path Detection
**Dependencies**: None
**Has user-facing changes**: No
**Estimated turns**: 10

## Stage 2: Token Audit Fast-Path Modeling
**Dependencies**: None (parallel with Stage 1 — both read existing patterns independently)
**Has user-facing changes**: Yes
**Estimated turns**: 12

## Stage 3: Integration Tests
**Dependencies**: Requires Stage 1
**Has user-facing changes**: No
**Estimated turns**: 8
""".strip()

        stages = _parse_plan_stages(content)
        self.assertEqual(len(stages), 3)
        self.assertEqual(stages[1].depends_on, [])
        self.assertEqual(stages[2].depends_on, ["Validator Fast-Path Detection"])

    def test_named_dependencies_drop_parenthetical_comments(self) -> None:
        content = """
## Stage 1: API
**Dependencies**: None
**Has user-facing changes**: Yes
**Estimated turns**: 5

## Stage 2: UI
**Dependencies**: API (can run after schema is stable)
**Has user-facing changes**: Yes
**Estimated turns**: 5
""".strip()

        stages = _parse_plan_stages(content)
        self.assertEqual(stages[1].depends_on, ["API"])


if __name__ == "__main__":
    unittest.main()
