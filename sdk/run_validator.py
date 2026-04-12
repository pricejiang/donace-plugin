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


# Estimated LLM tokens saved per fast-path invocation (avoids a full Claude round-trip).
FAST_PATH_EST_TOKENS_SAVED_PER_INVOCATION = 2_000


@dataclass
class RunReport:
    items: list[RunReportItem] = field(default_factory=list)
    token_summary: dict[str, dict[str, int]] = field(default_factory=dict)
    duration_summary: dict[str, float] = field(default_factory=dict)
    stage_summary: dict[str, int] = field(default_factory=dict)
    context_audits: list[dict[str, Any]] = field(default_factory=list)
    fast_path_summary: dict[str, Any] = field(
        default_factory=lambda: {"total_invocations": 0, "agents": [], "total_est_tokens_saved": 0}
    )

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
            "context_audits": self.context_audits,
            "fast_path_summary": self.fast_path_summary,
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
    tokens_by_agent: dict[str, dict[str, int]] = defaultdict(lambda: {"input": 0, "output": 0})
    duration_by_agent: dict[str, float] = defaultdict(float)
    agents_started: set[str] = set()
    agents_skipped: dict[str, str] = {}  # agent → reason
    agents_failed: dict[str, str] = {}   # agent → error
    skill_calls: list[dict] = []
    fix_loop_exhausted: list[dict] = []
    test_runs_per_stage: dict[str, int] = defaultdict(int)
    context_audits: list[dict] = []
    fast_path_agents: list[dict] = []

    for ev in events:
        ev_type = ev.get("type", "")
        agent = ev.get("agent", "")

        if ev_type == "agent.tokens":
            tokens_by_agent[agent]["input"] += ev.get("input_tokens", 0)
            tokens_by_agent[agent]["output"] += ev.get("output_tokens", 0)

        elif ev_type == "agent.completed":
            duration_by_agent[agent] += ev.get("duration_s", 0)

        elif ev_type == "agent.started":
            agents_started.add(agent)
            if agent == "test-engineer":
                stage = ev.get("stage", "unknown")
                test_runs_per_stage[stage] += 1
            if ev.get("model") == "local-fast-path":
                entry: dict[str, Any] = {
                    "agent": agent,
                    "est_tokens_saved": FAST_PATH_EST_TOKENS_SAVED_PER_INVOCATION,
                }
                if "stage" in ev:
                    entry["stage"] = ev["stage"]
                fast_path_agents.append(entry)

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
        elif ev_type == "context.audit":
            context_audits.append({
                "stage": ev.get("stage", ""),
                "consumer": ev.get("consumer", ""),
                "full_tokens": ev.get("full_tokens", 0),
                "compact_tokens": ev.get("compact_tokens", 0),
                "reduction_tokens": ev.get("reduction_tokens", 0),
                "reduction_pct": ev.get("reduction_pct", 0.0),
                "kept_sections": ev.get("kept_sections", []),
                "dropped_sections": ev.get("dropped_sections", []),
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
    report.context_audits = list(context_audits)

    # ── Populate fast-path summary ──
    if fast_path_agents:
        total_saved = sum(entry["est_tokens_saved"] for entry in fast_path_agents)
        report.fast_path_summary = {
            "total_invocations": len(fast_path_agents),
            "agents": fast_path_agents,
            "total_est_tokens_saved": total_saved,
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

    # ── Check 12: Fast-path agent savings (informational) ──
    if fast_path_agents:
        total_saved = report.fast_path_summary["total_est_tokens_saved"]
        count = report.fast_path_summary["total_invocations"]
        report.items.append(RunReportItem(
            severity="INFO",
            check="fast_path_savings",
            message=(
                f"{count} fast-path agent invocation(s) saved an estimated "
                f"{total_saved:,} tokens by bypassing LLM calls"
            ),
            details={
                "total_invocations": count,
                "agents": fast_path_agents,
                "total_est_tokens_saved": total_saved,
            },
        ))

    # ── Check 11: Shadow compact-context audit (informational) ──
    if context_audits:
        avg_reduction = sum(item["reduction_tokens"] for item in context_audits) / len(context_audits)
        best = max(context_audits, key=lambda item: item["reduction_tokens"])
        report.items.append(RunReportItem(
            severity="INFO",
            check="context_shadow_audit",
            message=(
                f"Shadow compact-context audit for {len(context_audits)} implementer prompt(s): "
                f"average reduction {avg_reduction:.0f} tokens; largest saving {best['reduction_tokens']} "
                f"tokens in stage '{best['stage']}'"
            ),
            details={
                "sample_count": len(context_audits),
                "average_reduction_tokens": round(avg_reduction, 1),
                "best_stage": best["stage"],
                "best_reduction_tokens": best["reduction_tokens"],
                "best_reduction_pct": best["reduction_pct"],
                "best_kept_sections": best["kept_sections"],
                "best_dropped_sections": best["dropped_sections"][:6],
            },
        ))

    return report
