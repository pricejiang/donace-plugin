"""Run validator — pure Python checks on pipeline run quality.

Analyzes event stream and sprint results to detect anomalies:
token waste, missing agents, unexpected behavior, etc.
Zero LLM cost, runs in < 1 second.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sdk.events import SprintResult


# ---------------------------------------------------------------------------
# Report types
# ---------------------------------------------------------------------------

@dataclass
class RunReportItem:
    severity: str           # "ERROR", "WARNING", "INFO"
    check: str              # e.g., "missing_agent", "token_anomaly"
    message: str            # human-readable description
    details: dict | None = None


@dataclass
class RunReport:
    items: list[RunReportItem] = field(default_factory=list)
    token_summary: dict[str, dict[str, int]] = field(default_factory=dict)
    duration_summary: dict[str, float] = field(default_factory=dict)
    stage_summary: dict[str, int] = field(default_factory=dict)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.items if i.severity == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.items if i.severity == "WARNING")

    def to_dict(self) -> dict[str, Any]:
        return {
            "errors": self.error_count,
            "warnings": self.warning_count,
            "items": [
                {"severity": i.severity, "check": i.check, "message": i.message, "details": i.details}
                for i in self.items
            ],
            "token_summary": self.token_summary,
            "duration_summary": self.duration_summary,
            "stage_summary": self.stage_summary,
        }


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

MAX_AGENT_OUTPUT_TOKENS = 25_000
TEST_TO_IMPL_TOKEN_RATIO = 2.0
REQUIRED_SPRINT_AGENTS = {"implementer", "test-engineer"}


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def validate_run(
    events: list[dict[str, Any]],
    sprint_result: SprintResult,
    stack: str | None,
) -> RunReport:
    """Validate a completed run and return a diagnostic report."""
    report = RunReport()

    # ── Gather data from events ──
    tokens_by_agent: dict[str, dict[str, int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    )
    duration_by_agent: dict[str, float] = defaultdict(float)
    agents_started: set[str] = set()
    agents_skipped: dict[str, str] = {}  # agent → reason
    agents_failed: dict[str, str] = {}   # agent → error
    skill_calls: list[dict] = []
    fix_loop_exhausted: list[dict] = []
    test_runs_per_stage: dict[str, int] = defaultdict(int)

    for ev in events:
        ev_type = ev.get("type", "")
        agent = ev.get("agent", "")

        if ev_type == "agent.tokens":
            tokens_by_agent[agent]["input"] += ev.get("input_tokens", 0)
            tokens_by_agent[agent]["output"] += ev.get("output_tokens", 0)
            tokens_by_agent[agent]["cache_creation"] += ev.get("cache_creation_input_tokens", 0)
            tokens_by_agent[agent]["cache_read"] += ev.get("cache_read_input_tokens", 0)

        elif ev_type == "agent.completed":
            duration_by_agent[agent] += ev.get("duration_s", 0)

        elif ev_type == "agent.started":
            agents_started.add(agent)
            if agent == "test-engineer":
                stage = ev.get("stage", "unknown")
                test_runs_per_stage[stage] += 1

        elif ev_type == "agent.skipped":
            agents_skipped[agent] = ev.get("reason", "")

        elif ev_type == "agent.failed":
            agents_failed[agent] = ev.get("error", "")

        elif ev_type == "agent.tool_use" and ev.get("tool") == "Skill":
            skill_calls.append({
                "agent": agent,
                "skill": ev.get("input_preview", ""),
                "stage": ev.get("stage", ""),
            })

        elif ev_type == "fix_loop.exhausted":
            fix_loop_exhausted.append({
                "stage": ev.get("stage", ""),
                "attempt": ev.get("attempt", 0),
                "remaining": ev.get("remaining_failures", []),
            })

    # ── Populate summaries ──
    report.token_summary = dict(tokens_by_agent)
    report.duration_summary = dict(duration_by_agent)
    report.stage_summary = {
        "passed": sum(1 for s in sprint_result.stages if s.status == "PASS"),
        "blocked": sum(1 for s in sprint_result.stages if s.status == "BLOCKED"),
        "skipped": sum(1 for s in sprint_result.stages if s.status == "SKIPPED"),
        "total": len(sprint_result.stages),
    }

    # ── Check 1: Missing required agents ──
    for required in REQUIRED_SPRINT_AGENTS:
        if required not in agents_started:
            report.items.append(RunReportItem(
                severity="ERROR",
                check="missing_agent",
                message=f"Required agent '{required}' never started during sprint",
            ))

    # ── Check 2: Final review skipped when stack is known ──
    if stack and stack != "unknown":
        reviewer = "typescript-reviewer" if stack in ("typescript", "both") else "ios-reviewer" if stack == "ios" else None
        if reviewer and reviewer not in agents_started:
            skip_reason = agents_skipped.get("final-review", "")
            report.items.append(RunReportItem(
                severity="WARNING",
                check="final_review_skipped",
                message=f"Stack is '{stack}' but final review was skipped: {skip_reason}",
                details={"stack": stack, "reviewer": reviewer, "reason": skip_reason},
            ))

    # ── Check 3: Token anomaly (single agent > 25K output) ──
    for agent, usage in tokens_by_agent.items():
        if agent.startswith("planner") or agent.startswith("architect"):
            continue  # plan/architecture output can be long
        if usage["output"] > MAX_AGENT_OUTPUT_TOKENS:
            report.items.append(RunReportItem(
                severity="WARNING",
                check="token_anomaly",
                message=f"'{agent}' produced {usage['output']:,} output tokens (threshold: {MAX_AGENT_OUTPUT_TOKENS:,})",
                details={"agent": agent, "output_tokens": usage["output"], "threshold": MAX_AGENT_OUTPUT_TOKENS},
            ))

    # ── Check 4: Token ratio (test-engineer vs implementer) ──
    impl_out = tokens_by_agent.get("implementer", {}).get("output", 0)
    test_out = tokens_by_agent.get("test-engineer", {}).get("output", 0)
    if impl_out > 0 and test_out > impl_out * TEST_TO_IMPL_TOKEN_RATIO:
        ratio = round(test_out / impl_out, 1)
        report.items.append(RunReportItem(
            severity="WARNING",
            check="token_ratio_anomaly",
            message=f"test-engineer output ({test_out:,}) is {ratio}x implementer output ({impl_out:,})",
            details={"test_output": test_out, "impl_output": impl_out, "ratio": ratio},
        ))

    # ── Check 5: Skill calls (informational) ──
    for call in skill_calls:
        report.items.append(RunReportItem(
            severity="INFO",
            check="skill_call",
            message=f"'{call['agent']}' invoked skill: {call['skill']}",
            details=call,
        ))

    # ── Check 6: Fix loop exhausted ──
    for exhausted in fix_loop_exhausted:
        remaining = exhausted.get("remaining", [])
        report.items.append(RunReportItem(
            severity="WARNING",
            check="fix_loop_exhausted",
            message=f"Fix loop exhausted after {exhausted['attempt']} attempts in '{exhausted['stage']}' with {len(remaining)} unresolved issue(s)",
            details=exhausted,
        ))

    # ── Check 7: All stages blocked ──
    if sprint_result.stages and all(s.status == "BLOCKED" for s in sprint_result.stages):
        report.items.append(RunReportItem(
            severity="ERROR",
            check="all_stages_blocked",
            message="All stages are BLOCKED — nothing was successfully implemented",
        ))

    # ── Check 8: Codex review skipped unexpectedly ──
    if "codex-review" in agents_skipped:
        reason = agents_skipped["codex-review"]
        if "not found" not in reason.lower() and "disabled" not in reason.lower():
            report.items.append(RunReportItem(
                severity="WARNING",
                check="codex_skipped",
                message=f"Codex review was skipped: {reason}",
                details={"reason": reason},
            ))

    # ── Check 9: Agent timeouts ──
    for agent, error in agents_failed.items():
        if "timed out" in error.lower():
            report.items.append(RunReportItem(
                severity="WARNING",
                check="agent_timeout",
                message=f"'{agent}' timed out: {error}",
                details={"agent": agent, "error": error},
            ))

    # ── Check 10: Excessive test reruns ──
    for stage, count in test_runs_per_stage.items():
        if count > 2:
            report.items.append(RunReportItem(
                severity="INFO",
                check="excessive_test_reruns",
                message=f"test-engineer ran {count} times in stage '{stage}'",
                details={"stage": stage, "count": count},
            ))

    return report


# ---------------------------------------------------------------------------
# Routing hints — accumulate across runs for team-lead reference
# ---------------------------------------------------------------------------

def _hints_path() -> str:
    """Default routing hints file path."""
    import os
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data:
        base = plugin_data
    else:
        base = os.path.join(os.path.expanduser("~"), ".claude", "plugins", "data", "donace")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "routing_hints.json")


def load_routing_hints(stack: str | None = None) -> dict[str, Any]:
    """Load routing hints for a tech stack. Returns empty dict if no data."""
    import json
    path = _hints_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if stack and stack in data.get("stack_hints", {}):
        return data["stack_hints"][stack]
    return data


def update_routing_hints(
    report: RunReport,
    stack: str | None,
    codex_had_issues: bool,
    runtime_had_issues: bool,
    fix_loops_used: int,
) -> None:
    """Update routing hints after a run completes.

    Uses exponential moving average — recent runs weighted more heavily.
    """
    import json
    path = _hints_path()

    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"stack_hints": {}, "total_runs": 0}

    data["total_runs"] = data.get("total_runs", 0) + 1

    if stack:
        hints = data["stack_hints"].setdefault(stack, {
            "codex_useful_rate": 0.5,
            "runtime_useful_rate": 0.5,
            "avg_fix_loops": 1.0,
        })

        # Exponential moving average (alpha=0.3 — recent runs weighted more)
        alpha = 0.3
        hints["codex_useful_rate"] = round(
            alpha * (1.0 if codex_had_issues else 0.0)
            + (1 - alpha) * hints.get("codex_useful_rate", 0.5),
            3,
        )
        hints["runtime_useful_rate"] = round(
            alpha * (1.0 if runtime_had_issues else 0.0)
            + (1 - alpha) * hints.get("runtime_useful_rate", 0.5),
            3,
        )
        hints["avg_fix_loops"] = round(
            alpha * fix_loops_used
            + (1 - alpha) * hints.get("avg_fix_loops", 1.0),
            2,
        )

    with open(path, "w") as f:
        json.dump(data, f, indent=2)
