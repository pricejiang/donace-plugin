from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sdk.run_validator import RunReport


class ContextAuditPersistenceTests(unittest.TestCase):
    def test_run_report_exposes_context_audits(self) -> None:
        report = RunReport(
            context_audits=[
                {
                    "stage": "UI",
                    "consumer": "implementer",
                    "full_tokens": 1200,
                    "compact_tokens": 700,
                }
            ]
        )
        as_dict = report.to_dict()
        self.assertIn("context_audits", as_dict)
        self.assertEqual(as_dict["context_audits"][0]["stage"], "UI")

    def test_context_audit_json_shape(self) -> None:
        payload = {
            "run_id": "run-abc123",
            "count": 1,
            "audits": [
                {
                    "type": "context.audit",
                    "stage": "API",
                    "consumer": "implementer",
                    "full_tokens": 1000,
                    "compact_tokens": 600,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "run-abc123.context-audit.json"
            path.write_text(json.dumps(payload, indent=2))
            loaded = json.loads(path.read_text())
        self.assertEqual(loaded["count"], 1)
        self.assertEqual(loaded["audits"][0]["stage"], "API")


if __name__ == "__main__":
    unittest.main()
