from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from sdk.context_budget import audit_stage_context
from sdk.events import EventBus, SprintResult, Stage, StageResult
from sdk.orchestrator import SharedContext, run_documenter
from sdk.run_validator import validate_run


def make_shared_context() -> SharedContext:
    shared_ctx = SharedContext(run_id="shadow", cwd="/tmp/project", task="Ship the current stage safely.")
    shared_ctx.add("Project", "Stack: typescript\nCwd: /tmp/project")
    shared_ctx.add("Plan", "Stages:\n- Stage 1: API\n- Stage 2: UI\n\nFull plan:\n" + ("plan " * 300))
    for idx in range(1, 5):
        shared_ctx.add(
            f"Stage Result: Slice {idx}",
            f"Status: PASS\nTests: {idx} passed, 0 failed\nFix attempts: 0\n" + ("detail " * 40),
        )
    return shared_ctx


class ShadowContextAuditTests(unittest.TestCase):
    def test_audit_reports_kept_and_dropped_sections(self) -> None:
        shared_ctx = make_shared_context()
        full_context = shared_ctx.to_prompt_prefix()
        audit = audit_stage_context(shared_ctx.sections, full_context=full_context, consumer="implementer")
        self.assertGreater(audit.full_tokens, audit.compact_tokens)
        self.assertIn("Task", audit.kept_sections)
        self.assertIn("Plan", audit.kept_sections)
        self.assertIn("Stage Result: Slice 4", audit.kept_sections)
        self.assertIn("Stage Result: Slice 1", audit.dropped_sections)

    def test_validator_summarizes_shadow_audit(self) -> None:
        report = validate_run(
            events=[
                {"type": "agent.started", "agent": "implementer", "stage": "UI"},
                {"type": "agent.started", "agent": "test-engineer", "stage": "UI"},
                {
                    "type": "context.audit",
                    "stage": "UI",
                    "consumer": "implementer",
                    "full_tokens": 1200,
                    "compact_tokens": 700,
                    "reduction_tokens": 500,
                    "reduction_pct": 41.7,
                    "kept_sections": ["Task", "Project", "Plan", "Stage Result: Slice 4"],
                    "dropped_sections": ["Stage Result: Slice 1", "Stage Result: Slice 2"],
                },
            ],
            sprint_result=SprintResult(
                stages=[
                    StageResult(
                        name="UI",
                        status="PASS",
                        contract="contract",
                        test_result={"passed": 5, "failed": 0},
                        codex_result={"status": "completed", "has_issues": False, "output": ""},
                        runtime_result=None,
                        fix_attempts=0,
                    )
                ],
                warnings=[],
                summary={"passed": 1, "blocked": 0, "skipped": 0, "total": 1},
            ),
            stack="typescript",
        )
        item = next((entry for entry in report.items if entry.check == "context_shadow_audit"), None)
        self.assertIsNotNone(item)
        self.assertIn("average reduction", item.message)


class FastPathDetectionTests(unittest.TestCase):
    """Tests for Check 12: fast-path agent detection in validate_run."""

    def _base_sprint(self) -> SprintResult:
        return SprintResult(
            stages=[
                StageResult(
                    name="API",
                    status="PASS",
                    contract="contract",
                    test_result={"passed": 3, "failed": 0},
                    codex_result={"status": "completed", "has_issues": False, "output": ""},
                    runtime_result=None,
                    fix_attempts=0,
                )
            ],
            warnings=[],
            summary={"passed": 1, "blocked": 0, "skipped": 0, "total": 1},
        )

    def test_fast_path_documenter_events_produce_summary(self) -> None:
        report = validate_run(
            events=[
                {"type": "agent.started", "agent": "implementer"},
                {"type": "agent.started", "agent": "test-engineer", "stage": "API"},
                {"type": "agent.started", "agent": "documenter", "model": "local-fast-path"},
                {"type": "agent.completed", "agent": "documenter", "duration_s": 0.1},
            ],
            sprint_result=self._base_sprint(),
            stack=None,
        )
        fp = report.fast_path_summary
        self.assertEqual(fp["total_invocations"], 1)
        self.assertEqual(len(fp["agents"]), 1)
        self.assertEqual(fp["agents"][0]["agent"], "documenter")
        self.assertGreater(fp["total_est_tokens_saved"], 0)
        self.assertGreater(fp["agents"][0]["est_tokens_saved"], 0)

        items_with_check = [i for i in report.items if i.check == "fast_path_savings"]
        self.assertEqual(len(items_with_check), 1)
        self.assertEqual(items_with_check[0].severity, "INFO")

    def test_fast_path_test_engineer_events_produce_summary(self) -> None:
        report = validate_run(
            events=[
                {"type": "agent.started", "agent": "implementer"},
                {"type": "agent.started", "agent": "test-engineer", "stage": "API"},
                {"type": "agent.started", "agent": "test-engineer", "model": "local-fast-path", "stage": "API"},
                {"type": "agent.completed", "agent": "test-engineer", "duration_s": 0.05},
            ],
            sprint_result=self._base_sprint(),
            stack=None,
        )
        fp = report.fast_path_summary
        self.assertEqual(fp["total_invocations"], 1)
        self.assertEqual(fp["agents"][0]["agent"], "test-engineer")
        self.assertEqual(fp["agents"][0].get("stage"), "API")

    def test_no_fast_path_events_produce_empty_summary(self) -> None:
        report = validate_run(
            events=[
                {"type": "agent.started", "agent": "implementer"},
                {"type": "agent.started", "agent": "test-engineer", "stage": "API"},
                {"type": "agent.started", "agent": "documenter", "model": "gpt-4"},
            ],
            sprint_result=self._base_sprint(),
            stack=None,
        )
        fp = report.fast_path_summary
        self.assertEqual(fp["total_invocations"], 0)
        self.assertEqual(fp["agents"], [])
        self.assertEqual(fp["total_est_tokens_saved"], 0)

        items_with_check = [i for i in report.items if i.check == "fast_path_savings"]
        self.assertEqual(len(items_with_check), 0)

    def test_shadow_audit_plus_fast_path_combined(self) -> None:
        report = validate_run(
            events=[
                {"type": "agent.started", "agent": "implementer"},
                {"type": "agent.started", "agent": "test-engineer", "stage": "API"},
                {"type": "agent.started", "agent": "documenter", "model": "local-fast-path"},
                {
                    "type": "context.audit",
                    "stage": "API",
                    "consumer": "implementer",
                    "full_tokens": 1000,
                    "compact_tokens": 600,
                    "reduction_tokens": 400,
                    "reduction_pct": 40.0,
                    "kept_sections": ["Task", "Plan"],
                    "dropped_sections": ["Stage Result: Slice 1"],
                },
            ],
            sprint_result=self._base_sprint(),
            stack=None,
        )
        self.assertGreater(len(report.context_audits), 0)
        self.assertGreater(report.fast_path_summary["total_invocations"], 0)

    def test_multiple_fast_path_agents_accumulate(self) -> None:
        report = validate_run(
            events=[
                {"type": "agent.started", "agent": "implementer"},
                {"type": "agent.started", "agent": "test-engineer", "stage": "API"},
                {"type": "agent.started", "agent": "documenter", "model": "local-fast-path"},
                {"type": "agent.started", "agent": "test-engineer", "model": "local-fast-path", "stage": "API"},
            ],
            sprint_result=self._base_sprint(),
            stack=None,
        )
        fp = report.fast_path_summary
        self.assertEqual(fp["total_invocations"], 2)
        self.assertEqual(len(fp["agents"]), 2)
        agent_names = {entry["agent"] for entry in fp["agents"]}
        self.assertIn("documenter", agent_names)
        self.assertIn("test-engineer", agent_names)
        individual_total = sum(entry["est_tokens_saved"] for entry in fp["agents"])
        self.assertEqual(fp["total_est_tokens_saved"], individual_total)


class IntegrationTests(unittest.TestCase):
    """End-to-end integration: real agent fast-paths → EventBus → validate_run."""

    def _base_sprint(self, stage_name: str = "API") -> SprintResult:
        return SprintResult(
            stages=[
                StageResult(
                    name=stage_name,
                    status="PASS",
                    contract="contract",
                    test_result={"passed": 1, "failed": 0},
                    codex_result={"status": "completed", "has_issues": False, "output": ""},
                    runtime_result=None,
                    fix_attempts=0,
                )
            ],
            warnings=[],
            summary={"passed": 1, "blocked": 0, "skipped": 0, "total": 1},
        )

    # ------------------------------------------------------------------
    # E2E: run_documenter fast-path → EventBus → validate_run
    # ------------------------------------------------------------------

    def test_e2e_documenter_fast_path_events_model_field(self) -> None:
        """EventBus.get_events() from a real run_documenter fast-path includes model="local-fast-path"."""
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_ctx = SharedContext(
                run_id="run-int-doc",
                cwd=tmpdir,
                task="Rename README title and keep changes minimal",
            )

            class _StubDispatcher:
                async def _get_changed_files(self) -> str:
                    return "README.md"

                async def query(self, *args, **kwargs):  # pragma: no cover
                    raise AssertionError("Claude should not be called on fast path")

            bus = EventBus(run_id="run-int-doc", interactive=False)
            asyncio.run(run_documenter(shared_ctx, "[no reviewer]", bus, _StubDispatcher()))  # type: ignore[arg-type]

        events = bus.get_events()
        started_events = [e for e in events if e.get("type") == "agent.started" and e.get("agent") == "documenter"]
        self.assertTrue(len(started_events) >= 1, "Expected at least one agent.started for documenter")
        fast_path_events = [e for e in started_events if e.get("model") == "local-fast-path"]
        self.assertEqual(len(fast_path_events), 1, "Expected exactly one agent.started with model=local-fast-path")

    def test_e2e_documenter_fast_path_into_validate_run(self) -> None:
        """EventBus events from run_documenter fast-path produce fast_path_summary in validate_run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_ctx = SharedContext(
                run_id="run-int-doc2",
                cwd=tmpdir,
                task="Rename README title and keep changes minimal",
            )

            class _StubDispatcher:
                async def _get_changed_files(self) -> str:
                    return "README.md"

                async def query(self, *args, **kwargs):  # pragma: no cover
                    raise AssertionError("Claude should not be called on fast path")

            bus = EventBus(run_id="run-int-doc2", interactive=False)
            asyncio.run(run_documenter(shared_ctx, "[no reviewer]", bus, _StubDispatcher()))  # type: ignore[arg-type]

        report = validate_run(
            events=bus.get_events(),
            sprint_result=self._base_sprint(),
            stack=None,
        )
        fp = report.fast_path_summary
        self.assertEqual(fp["total_invocations"], 1)
        self.assertEqual(len(fp["agents"]), 1)
        self.assertEqual(fp["agents"][0]["agent"], "documenter")
        self.assertGreater(fp["total_est_tokens_saved"], 0)

    # ------------------------------------------------------------------
    # E2E: AgentDispatcher.run_test_engineer fast-path → EventBus → validate_run
    # ------------------------------------------------------------------

    def test_e2e_test_engineer_fast_path_events_model_field(self) -> None:
        """EventBus.get_events() from a real run_test_engineer fast-path includes model="local-fast-path"."""
        from sdk.agent_dispatch import AgentDispatcher

        with tempfile.TemporaryDirectory() as tmpdir:
            tests_dir = Path(tmpdir) / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_sample.py").write_text(
                "import unittest\n\n"
                "class S(unittest.TestCase):\n"
                "    def test_ok(self): self.assertTrue(True)\n"
            )

            dispatcher = AgentDispatcher.__new__(AgentDispatcher)
            dispatcher.cwd = tmpdir
            bus = EventBus(run_id="run-int-te", interactive=False)
            dispatcher.bus = bus

            async def fake_get_changed_files() -> str:
                return "README.md"

            async def fake_query(*args, **kwargs):  # pragma: no cover
                raise AssertionError("Claude should not be called on fast path")

            dispatcher._get_changed_files = fake_get_changed_files  # type: ignore[attr-defined]
            dispatcher.query = fake_query  # type: ignore[method-assign]

            asyncio.run(
                dispatcher.run_test_engineer(
                    Stage(name="Rename README title and keep changes minimal", has_user_facing_changes=False)
                )
            )

        events = bus.get_events()
        started = [e for e in events if e.get("type") == "agent.started" and e.get("agent") == "test-engineer"]
        self.assertTrue(len(started) >= 1, "Expected at least one agent.started for test-engineer")
        fast_path = [e for e in started if e.get("model") == "local-fast-path"]
        self.assertEqual(len(fast_path), 1, "Expected exactly one agent.started with model=local-fast-path")

    def test_e2e_test_engineer_fast_path_into_validate_run(self) -> None:
        """EventBus events from run_test_engineer fast-path produce fast_path_summary in validate_run."""
        from sdk.agent_dispatch import AgentDispatcher

        with tempfile.TemporaryDirectory() as tmpdir:
            tests_dir = Path(tmpdir) / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_sample.py").write_text(
                "import unittest\n\n"
                "class S(unittest.TestCase):\n"
                "    def test_ok(self): self.assertTrue(True)\n"
            )

            dispatcher = AgentDispatcher.__new__(AgentDispatcher)
            dispatcher.cwd = tmpdir
            bus = EventBus(run_id="run-int-te2", interactive=False)
            dispatcher.bus = bus

            async def fake_get_changed_files() -> str:
                return "README.md"

            async def fake_query(*args, **kwargs):  # pragma: no cover
                raise AssertionError("Claude should not be called on fast path")

            dispatcher._get_changed_files = fake_get_changed_files  # type: ignore[attr-defined]
            dispatcher.query = fake_query  # type: ignore[method-assign]

            asyncio.run(
                dispatcher.run_test_engineer(
                    Stage(name="Rename README title and keep changes minimal", has_user_facing_changes=False)
                )
            )

        report = validate_run(
            events=bus.get_events(),
            sprint_result=self._base_sprint(),
            stack=None,
        )
        fp = report.fast_path_summary
        self.assertEqual(fp["total_invocations"], 1)
        self.assertEqual(len(fp["agents"]), 1)
        self.assertEqual(fp["agents"][0]["agent"], "test-engineer")

    # ------------------------------------------------------------------
    # RunReport.to_dict() JSON serialization roundtrip
    # ------------------------------------------------------------------

    def test_run_report_to_dict_json_serializable(self) -> None:
        """report.to_dict() with fast_path_summary is JSON-serializable and has correct key types."""
        report = validate_run(
            events=[
                {"type": "agent.started", "agent": "implementer"},
                {"type": "agent.started", "agent": "test-engineer", "stage": "API"},
                {"type": "agent.started", "agent": "documenter", "model": "local-fast-path"},
                {"type": "agent.completed", "agent": "documenter", "duration_s": 0.05},
            ],
            sprint_result=self._base_sprint(),
            stack=None,
        )
        d = report.to_dict()
        # Must not raise
        serialized = json.dumps(d)
        reparsed = json.loads(serialized)

        fp = reparsed["fast_path_summary"]
        self.assertIsInstance(fp["total_invocations"], int)
        self.assertIsInstance(fp["agents"], list)
        self.assertIsInstance(fp["total_est_tokens_saved"], int)
        self.assertEqual(len(fp["agents"]), 1)
        self.assertIsInstance(fp["agents"][0]["agent"], str)
        self.assertIsInstance(fp["agents"][0]["est_tokens_saved"], int)

    def test_run_report_to_dict_empty_fast_path_exact_structure(self) -> None:
        """report.to_dict()['fast_path_summary'] equals exact empty structure when no fast-path events."""
        report = validate_run(
            events=[
                {"type": "agent.started", "agent": "implementer"},
                {"type": "agent.started", "agent": "test-engineer", "stage": "API"},
            ],
            sprint_result=self._base_sprint(),
            stack=None,
        )
        fp = report.to_dict()["fast_path_summary"]
        self.assertEqual(fp, {"total_invocations": 0, "agents": [], "total_est_tokens_saved": 0})
        # Also JSON-serializable
        json.dumps(fp)  # must not raise

    # ------------------------------------------------------------------
    # INFO check item message format
    # ------------------------------------------------------------------

    def test_fast_path_savings_item_message_substrings(self) -> None:
        """fast_path_savings check item message contains required substrings and severity is INFO."""
        report = validate_run(
            events=[
                {"type": "agent.started", "agent": "implementer"},
                {"type": "agent.started", "agent": "test-engineer", "stage": "API"},
                {"type": "agent.started", "agent": "documenter", "model": "local-fast-path"},
            ],
            sprint_result=self._base_sprint(),
            stack=None,
        )
        items = [i for i in report.items if i.check == "fast_path_savings"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.severity, "INFO")
        self.assertIn("fast-path agent invocation", item.message)
        self.assertIn("tokens by bypassing LLM calls", item.message)

    def test_fast_path_savings_item_details_match_summary(self) -> None:
        """fast_path_savings check item details dict mirrors report.fast_path_summary."""
        report = validate_run(
            events=[
                {"type": "agent.started", "agent": "implementer"},
                {"type": "agent.started", "agent": "test-engineer", "stage": "API"},
                {"type": "agent.started", "agent": "documenter", "model": "local-fast-path"},
            ],
            sprint_result=self._base_sprint(),
            stack=None,
        )
        items = [i for i in report.items if i.check == "fast_path_savings"]
        self.assertEqual(len(items), 1)
        details = items[0].details
        self.assertIsNotNone(details)
        self.assertEqual(details["total_invocations"], report.fast_path_summary["total_invocations"])
        self.assertEqual(details["total_est_tokens_saved"], report.fast_path_summary["total_est_tokens_saved"])
        self.assertEqual(details["agents"], report.fast_path_summary["agents"])

    def test_no_duplicate_fast_path_savings_check_items(self) -> None:
        """Exactly one fast_path_savings item in report.items regardless of number of fast-path agents."""
        report = validate_run(
            events=[
                {"type": "agent.started", "agent": "implementer"},
                {"type": "agent.started", "agent": "test-engineer", "stage": "API"},
                {"type": "agent.started", "agent": "documenter", "model": "local-fast-path"},
                {"type": "agent.started", "agent": "test-engineer", "model": "local-fast-path", "stage": "API"},
                {"type": "agent.started", "agent": "documenter", "model": "local-fast-path"},
            ],
            sprint_result=self._base_sprint(),
            stack=None,
        )
        items = [i for i in report.items if i.check == "fast_path_savings"]
        self.assertEqual(len(items), 1)

    # ------------------------------------------------------------------
    # Combined shadow audit + fast-path — both items appear once each
    # ------------------------------------------------------------------

    def test_combined_shadow_audit_and_fast_path_exactly_one_check_each(self) -> None:
        """Single event stream produces exactly one context_shadow_audit and one fast_path_savings item."""
        report = validate_run(
            events=[
                {"type": "agent.started", "agent": "implementer"},
                {"type": "agent.started", "agent": "test-engineer", "stage": "API"},
                {"type": "agent.started", "agent": "documenter", "model": "local-fast-path"},
                {
                    "type": "context.audit",
                    "stage": "API",
                    "consumer": "implementer",
                    "full_tokens": 1000,
                    "compact_tokens": 600,
                    "reduction_tokens": 400,
                    "reduction_pct": 40.0,
                    "kept_sections": ["Task", "Plan"],
                    "dropped_sections": ["Stage Result: Slice 1"],
                },
            ],
            sprint_result=self._base_sprint(),
            stack=None,
        )
        shadow_items = [i for i in report.items if i.check == "context_shadow_audit"]
        fp_items = [i for i in report.items if i.check == "fast_path_savings"]
        self.assertEqual(len(shadow_items), 1)
        self.assertEqual(len(fp_items), 1)
        self.assertGreater(len(report.context_audits), 0)
        self.assertGreater(report.fast_path_summary["total_invocations"], 0)

    # ------------------------------------------------------------------
    # Token accumulation: total == N × FAST_PATH_EST_TOKENS_SAVED_PER_INVOCATION
    # ------------------------------------------------------------------

    def test_multiple_fast_path_total_equals_n_times_constant(self) -> None:
        """total_est_tokens_saved == N × 2000 (FAST_PATH_EST_TOKENS_SAVED_PER_INVOCATION)."""
        from sdk.run_validator import FAST_PATH_EST_TOKENS_SAVED_PER_INVOCATION

        n = 3
        events: list[dict] = [
            {"type": "agent.started", "agent": "implementer"},
            {"type": "agent.started", "agent": "test-engineer", "stage": "API"},
        ]
        for _ in range(n):
            events.append({"type": "agent.started", "agent": "documenter", "model": "local-fast-path"})

        report = validate_run(events=events, sprint_result=self._base_sprint(), stack=None)
        fp = report.fast_path_summary
        self.assertEqual(fp["total_invocations"], n)
        self.assertEqual(fp["total_est_tokens_saved"], n * FAST_PATH_EST_TOKENS_SAVED_PER_INVOCATION)
        individual_total = sum(a["est_tokens_saved"] for a in fp["agents"])
        self.assertEqual(fp["total_est_tokens_saved"], individual_total)


if __name__ == "__main__":
    unittest.main()
