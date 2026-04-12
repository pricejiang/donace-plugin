from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from sdk.events import Stage
from sdk.token_audit import _format_text, _suggestions, audit_prompt_budgets, estimate_tokens


REPO_ROOT = Path(__file__).resolve().parents[1]


class EstimateTokensTests(unittest.TestCase):
    def test_empty_text_costs_zero(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)

    def test_estimate_scales_with_text_length(self) -> None:
        self.assertLess(estimate_tokens("short"), estimate_tokens("x" * 400))


class AuditPromptBudgetTests(unittest.TestCase):
    def test_measurements_cover_stage_flow_and_wrap(self) -> None:
        report = audit_prompt_budgets(
            task="Add dashboard filters",
            stages=[
                Stage(name="Server Filters", has_user_facing_changes=True),
                Stage(name="UI Filters", has_user_facing_changes=True),
            ],
        )
        labels = [item.label for item in report.measurements]
        self.assertIn("stage-1-contract", labels)
        self.assertIn("stage-1-implementer", labels)
        self.assertIn("stage-2-test-engineer", labels)
        self.assertIn("final-review", labels)
        self.assertIn("documenter", labels)

    def test_implementer_prompts_grow_as_context_accumulates(self) -> None:
        report = audit_prompt_budgets(
            task="Ship a four-stage feature",
            stages=[
                Stage(name="Schema", has_user_facing_changes=False),
                Stage(name="API", has_user_facing_changes=True),
                Stage(name="UI", has_user_facing_changes=True),
                Stage(name="Docs", has_user_facing_changes=False),
            ],
        )
        implementer_tokens = [
            item.est_tokens for item in report.measurements if item.category == "implementer"
        ]
        self.assertGreater(len(implementer_tokens), 1)
        self.assertGreater(implementer_tokens[-1], implementer_tokens[0])

    def test_audit_emits_actionable_suggestions(self) -> None:
        report = audit_prompt_budgets(stages=stage_count_to_stages(5))
        self.assertTrue(report.suggestions)
        self.assertTrue(any("Implementer prompts" in item for item in report.suggestions))


def stage_count_to_stages(count: int) -> list[Stage]:
    return [Stage(name=f"Slice {idx}", has_user_facing_changes=True) for idx in range(1, count + 1)]


class FastPathSavingsTests(unittest.TestCase):
    def _stages_with_docs(self) -> list[Stage]:
        return [
            Stage(name="Schema", has_user_facing_changes=False),
            Stage(name="API", has_user_facing_changes=True),
            Stage(name="Docs", has_user_facing_changes=False),
        ]

    # ------------------------------------------------------------------
    # fast_path_savings structure
    # ------------------------------------------------------------------

    def test_no_doc_only_stages_yields_empty_savings(self) -> None:
        report = audit_prompt_budgets(stages=self._stages_with_docs())
        fp = report.to_dict()["fast_path_savings"]
        self.assertEqual(fp, {"stages": [], "total_est_tokens_saved": 0, "stage_count": 0})

    def test_empty_doc_only_list_yields_empty_savings(self) -> None:
        report = audit_prompt_budgets(stages=self._stages_with_docs(), doc_only_stages=[])
        fp = report.to_dict()["fast_path_savings"]
        self.assertEqual(fp, {"stages": [], "total_est_tokens_saved": 0, "stage_count": 0})

    def test_matching_doc_only_stage_appears_in_savings(self) -> None:
        report = audit_prompt_budgets(stages=self._stages_with_docs(), doc_only_stages=["Docs"])
        fp = report.to_dict()["fast_path_savings"]
        self.assertEqual(fp["stage_count"], 1)
        self.assertEqual(len(fp["stages"]), 1)
        self.assertEqual(fp["stages"][0]["name"], "Docs")
        self.assertGreater(fp["stages"][0]["est_tokens_saved"], 0)
        self.assertEqual(fp["total_est_tokens_saved"], fp["stages"][0]["est_tokens_saved"])

    def test_est_tokens_saved_equals_implementer_plus_test_engineer(self) -> None:
        stages = [Stage(name="Docs", has_user_facing_changes=False)]
        report = audit_prompt_budgets(stages=stages, doc_only_stages=["Docs"])
        by_label = {item.label: item for item in report.measurements}
        expected = by_label["stage-1-implementer"].est_tokens + by_label["stage-1-test-engineer"].est_tokens
        fp = report.fast_path_savings
        self.assertEqual(fp["stages"][0]["est_tokens_saved"], expected)
        self.assertEqual(fp["total_est_tokens_saved"], expected)

    def test_multiple_doc_only_stages_all_appear(self) -> None:
        stages = [
            Stage(name="Docs", has_user_facing_changes=False),
            Stage(name="README", has_user_facing_changes=False),
            Stage(name="Core", has_user_facing_changes=True),
        ]
        report = audit_prompt_budgets(stages=stages, doc_only_stages=["Docs", "README"])
        fp = report.fast_path_savings
        self.assertEqual(fp["stage_count"], 2)
        names = {s["name"] for s in fp["stages"]}
        self.assertEqual(names, {"Docs", "README"})
        self.assertEqual(
            fp["total_est_tokens_saved"],
            sum(s["est_tokens_saved"] for s in fp["stages"]),
        )

    def test_unknown_doc_only_stage_silently_ignored(self) -> None:
        report = audit_prompt_budgets(stages=self._stages_with_docs(), doc_only_stages=["NonExistent"])
        fp = report.fast_path_savings
        self.assertEqual(fp["stage_count"], 0)
        self.assertEqual(fp["stages"], [])

    def test_non_doc_only_stage_measurements_unchanged(self) -> None:
        stages = self._stages_with_docs()
        report_without = audit_prompt_budgets(stages=stages)
        report_with = audit_prompt_budgets(stages=stages, doc_only_stages=["Docs"])
        labels_without = {item.label: item.est_tokens for item in report_without.measurements}
        labels_with = {item.label: item.est_tokens for item in report_with.measurements}
        # All measurements should be identical — fast_path_savings is additive only.
        self.assertEqual(labels_without, labels_with)

    # ------------------------------------------------------------------
    # _format_text
    # ------------------------------------------------------------------

    def test_format_text_includes_fast_path_heading_when_stages_present(self) -> None:
        stages = [Stage(name="Docs", has_user_facing_changes=False)]
        report = audit_prompt_budgets(stages=stages, doc_only_stages=["Docs"])
        output = _format_text(report)
        self.assertIn("Fast-path savings:", output)
        self.assertIn("Docs", output)

    def test_format_text_omits_fast_path_heading_when_no_doc_only_stages(self) -> None:
        report = audit_prompt_budgets(stages=self._stages_with_docs())
        output = _format_text(report)
        self.assertNotIn("Fast-path savings:", output)

    # ------------------------------------------------------------------
    # _suggestions
    # ------------------------------------------------------------------

    def test_suggestions_include_fast_path_hint_when_savings_exceed_threshold(self) -> None:
        stages = [Stage(name="Docs", has_user_facing_changes=False)]
        report = audit_prompt_budgets(stages=stages, doc_only_stages=["Docs"])
        # The Docs stage will have implementer + test-engineer tokens >> 200.
        self.assertTrue(any("fast-path" in s for s in report.suggestions))

    def test_suggestions_omit_fast_path_hint_when_no_doc_only_stages(self) -> None:
        report = audit_prompt_budgets(stages=self._stages_with_docs())
        self.assertFalse(any("fast-path" in s for s in report.suggestions))

    # ------------------------------------------------------------------
    # CLI smoke tests
    # ------------------------------------------------------------------

    def test_cli_json_output_is_valid_and_contains_fast_path_savings(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "sdk.token_audit", "--format", "json"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        data = json.loads(result.stdout)
        self.assertIn("fast_path_savings", data)

    def test_cli_text_output_does_not_crash(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "sdk.token_audit", "--format", "text"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertTrue(len(result.stdout.strip()) > 0)


if __name__ == "__main__":
    unittest.main()
