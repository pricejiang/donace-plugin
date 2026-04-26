"""Tests for sdk.orchestrator._parse_plan_stages.

Background: run-phase5_5-96ed1d5cf406 spent 3 stage-1 attempts being skipped
through cmd_run_job's verify-only fast-path because the parser couldn't read
the multi-line bullet list under `**Files to modify**:`. Every code-bearing
stage in that plan parsed to `files=[]`, the fast-path saw an empty list and
routed straight to runtime-verifier, implementer never ran.

These tests pin both forms (inline + multi-line bullet list) so the parser
can't silently regress on either.
"""

import os
import sys
import unittest

# Repo-root on sys.path so `sdk.*` resolves regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sdk.orchestrator import _parse_plan_stages  # noqa: E402


def _strip_backticks(paths):
    """Helper: parser may keep or strip backticks. Compare on the inner path."""
    out = []
    for p in paths:
        s = p.strip()
        while s.startswith("`") and s.endswith("`") and len(s) >= 2:
            s = s[1:-1].strip()
        out.append(s)
    return out


class InlineFilesFormTests(unittest.TestCase):
    """Regression guard for the original inline form."""

    def test_inline_comma_separated_files(self):
        content = (
            "## Stage 1: Token Utility\n"
            "**Goal**: add getToken helper\n"
            "**Files to modify**: `apps/web/lib/token.ts` (new), `apps/web/lib/auth.ts` (modify)\n"
            "**Dependencies**: None\n"
            "**Has user-facing changes**: No\n"
            "**Estimated turns**: 3\n"
        )
        stages = _parse_plan_stages(content)
        self.assertEqual(len(stages), 1)
        self.assertEqual(
            _strip_backticks(stages[0].files),
            ["apps/web/lib/token.ts", "apps/web/lib/auth.ts"],
        )


class MultiLineBulletFilesFormTests(unittest.TestCase):
    """Multi-line bullet list under `**Files to modify**:` — the format
    real plans actually use (run-phase5_5-96ed1d5cf406)."""

    def test_three_files_under_bold_header(self):
        content = (
            "## Stage 1: Display Types + SSR Fetchers\n"
            "**Goal**: create web-only display types\n"
            "**Files to modify**:\n"
            "- `apps/web/lib/types/character.ts` (new)\n"
            "- `apps/web/lib/types/world.ts` (new)\n"
            "- `apps/web/lib/profile-ssr.ts` (new)\n"
            "\n"
            "**Dependencies**: None\n"
            "**Has user-facing changes**: No\n"
            "**Estimated turns**: 12\n"
        )
        stages = _parse_plan_stages(content)
        self.assertEqual(len(stages), 1)
        self.assertEqual(
            _strip_backticks(stages[0].files),
            [
                "apps/web/lib/types/character.ts",
                "apps/web/lib/types/world.ts",
                "apps/web/lib/profile-ssr.ts",
            ],
        )

    def test_bullet_list_allows_blank_spacer_after_header(self):
        content = (
            "## Stage 1: Display Types + SSR Fetchers\n"
            "**Goal**: create web-only display types\n"
            "**Files to modify**:\n"
            "\n"
            "- `apps/web/lib/types/character.ts` (new)\n"
            "- `apps/web/lib/types/world.ts` (new)\n"
            "\n"
            "**Dependencies**: None\n"
            "**Estimated turns**: 12\n"
        )
        stages = _parse_plan_stages(content)
        self.assertEqual(len(stages), 1)
        self.assertEqual(
            _strip_backticks(stages[0].files),
            [
                "apps/web/lib/types/character.ts",
                "apps/web/lib/types/world.ts",
            ],
        )

    def test_bullet_list_terminates_at_next_bold_field(self):
        """No blank line between the bullet list and the next field — the
        next `**Foo**:` must end the file collection so we don't suck in
        Dependencies/Success Criteria as fake paths."""
        content = (
            "## Stage 1: X\n"
            "**Files to modify**:\n"
            "- `apps/web/lib/a.ts` (new)\n"
            "- `apps/web/lib/b.ts` (new)\n"
            "**Dependencies**: None\n"
            "**Estimated turns**: 5\n"
        )
        stages = _parse_plan_stages(content)
        self.assertEqual(len(stages), 1)
        self.assertEqual(
            _strip_backticks(stages[0].files),
            ["apps/web/lib/a.ts", "apps/web/lib/b.ts"],
        )

    def test_bullet_list_with_asterisk_marker(self):
        content = (
            "## Stage 1: X\n"
            "**Files to modify**:\n"
            "* `apps/x.ts` (new)\n"
            "* `apps/y.ts` (modify)\n"
            "\n"
            "**Estimated turns**: 2\n"
        )
        stages = _parse_plan_stages(content)
        self.assertEqual(len(stages), 1)
        self.assertEqual(
            _strip_backticks(stages[0].files),
            ["apps/x.ts", "apps/y.ts"],
        )

    def test_multi_stage_plan_with_bullet_lists(self):
        """End-to-end: a 2-stage plan in bullet-list form, both stages
        should populate files correctly."""
        content = (
            "## Stage 1: Types\n"
            "**Files to modify**:\n"
            "- `apps/web/lib/types/character.ts` (new)\n"
            "- `apps/web/lib/types/world.ts` (new)\n"
            "\n"
            "**Dependencies**: None\n"
            "**Estimated turns**: 6\n"
            "\n"
            "## Stage 2: Routes\n"
            "**Files to modify**:\n"
            "- `apps/web/app/character/[id]/page.tsx` (new)\n"
            "\n"
            "**Dependencies**: Stage 1\n"
            "**Estimated turns**: 5\n"
        )
        stages = _parse_plan_stages(content)
        self.assertEqual(len(stages), 2)
        self.assertEqual(
            _strip_backticks(stages[0].files),
            ["apps/web/lib/types/character.ts", "apps/web/lib/types/world.ts"],
        )
        self.assertEqual(
            _strip_backticks(stages[1].files),
            ["apps/web/app/character/[id]/page.tsx"],
        )

    def test_files_to_modify_none_keeps_files_empty(self):
        """Verify-only stages (Files to modify: None) must still parse to
        files=[] — the existing fast-path depends on this signal."""
        content = (
            "## Stage 1: Typecheck + QA\n"
            "**Files to modify**: None\n"
            "**Dependencies**: None\n"
            "**Estimated turns**: 0\n"
        )
        stages = _parse_plan_stages(content)
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0].files, [])


if __name__ == "__main__":
    unittest.main()
