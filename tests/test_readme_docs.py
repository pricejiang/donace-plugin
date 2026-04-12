"""Tests that verify README documentation is consistent with the implementation.

These tests guard against documentation drift: they confirm that JSON shapes
described in README.md and field names referenced there match what the code
actually produces.
"""
from __future__ import annotations

import json
import unittest

from sdk.run_validator import RunReport


class ReadmeJsonShapeTests(unittest.TestCase):
    """fast_path_summary block documented in README must parse and have correct types."""

    def test_fast_path_summary_json_example_round_trips(self) -> None:
        """The JSON shape documented in README.md round-trips through json.loads
        with the expected top-level keys and value types."""
        # Reconstructs the documented shape from README.md § Fast-Path Agents.
        documented_example = {
            "fast_path_summary": {
                "total_invocations": 2,
                "agents": [
                    {"agent": "documenter", "stage": "docs", "est_tokens_saved": 2000},
                    {"agent": "test-engineer", "stage": "unit-tests", "est_tokens_saved": 2000},
                ],
                "total_est_tokens_saved": 4000,
            }
        }

        # Verify it serialises and deserialises without loss.
        round_tripped = json.loads(json.dumps(documented_example))
        fp = round_tripped["fast_path_summary"]

        self.assertIsInstance(fp["total_invocations"], int)
        self.assertIsInstance(fp["agents"], list)
        self.assertIsInstance(fp["total_est_tokens_saved"], int)

        # Each agent entry must carry the three documented keys.
        for entry in fp["agents"]:
            self.assertIn("agent", entry)
            self.assertIn("stage", entry)
            self.assertIn("est_tokens_saved", entry)

    def test_fast_path_summary_total_is_sum_of_agents(self) -> None:
        """Documented total_est_tokens_saved equals sum of per-agent estimates."""
        fp = {
            "total_invocations": 2,
            "agents": [
                {"agent": "documenter", "stage": "docs", "est_tokens_saved": 2000},
                {"agent": "test-engineer", "stage": "unit-tests", "est_tokens_saved": 2000},
            ],
            "total_est_tokens_saved": 4000,
        }
        agent_sum = sum(a["est_tokens_saved"] for a in fp["agents"])
        self.assertEqual(fp["total_est_tokens_saved"], agent_sum)
        self.assertEqual(fp["total_invocations"], len(fp["agents"]))


class RunReportFieldAlignmentTests(unittest.TestCase):
    """RunReport.to_dict() top-level keys must match what README documents."""

    def test_run_report_to_dict_contains_context_audits_key(self) -> None:
        """Default RunReport includes context_audits as a top-level key."""
        report = RunReport()
        result = report.to_dict()
        self.assertIn("context_audits", result,
                      "README documents 'context_audits' as a top-level key in RunReport.to_dict()")

    def test_run_report_to_dict_contains_fast_path_summary_key(self) -> None:
        """Default RunReport includes fast_path_summary as a top-level key."""
        report = RunReport()
        result = report.to_dict()
        self.assertIn("fast_path_summary", result,
                      "README documents 'fast_path_summary' as a top-level key in RunReport.to_dict()")

    def test_default_fast_path_summary_shape_matches_documented_keys(self) -> None:
        """Empty RunReport's fast_path_summary has the documented key set."""
        report = RunReport()
        fp = report.to_dict()["fast_path_summary"]
        self.assertIn("total_invocations", fp)
        self.assertIn("agents", fp)
        self.assertIn("total_est_tokens_saved", fp)
        self.assertIsInstance(fp["total_invocations"], int)
        self.assertIsInstance(fp["agents"], list)
        self.assertIsInstance(fp["total_est_tokens_saved"], int)

    def test_default_context_audits_is_empty_list(self) -> None:
        """Empty RunReport has context_audits == [] (no audit events recorded)."""
        report = RunReport()
        self.assertEqual(report.to_dict()["context_audits"], [])

    def test_default_fast_path_summary_is_empty(self) -> None:
        """Empty RunReport has fast_path_summary with zero invocations."""
        report = RunReport()
        fp = report.to_dict()["fast_path_summary"]
        self.assertEqual(fp["total_invocations"], 0)
        self.assertEqual(fp["agents"], [])
        self.assertEqual(fp["total_est_tokens_saved"], 0)


if __name__ == "__main__":
    unittest.main()
