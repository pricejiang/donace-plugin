"""Run validator — pure Python checks on pipeline run quality.

Analyzes event stream + aggregated job results to detect anomalies:
token waste, missing agents, hook denies, agent re-reading same files,
NEEDS_CONTEXT frequency, fix-loop exhaustion, etc.

Zero LLM cost, runs in < 1 second. Output feeds back into result.json
so team-lead can reflect on the run and spot pipeline improvements.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


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

MAX_AGENT_OUTPUT_TOKENS = 25_000          # single-call output that feels high
TEST_TO_IMPL_TOKEN_RATIO = 2.0            # test-engineer > 2× implementer = weird
REQUIRED_SPRINT_AGENTS = {"implementer", "test-engineer"}

# New-check thresholds
NEEDS_CONTEXT_THRESHOLD = 2               # 2+ NEEDS_CONTEXT → plan likely under-specified
HOOK_DENY_THRESHOLD = 3                   # 3+ hook denies for one agent → prompt rules not working
REPEATED_STAGE_FAILURE_THRESHOLD = 2      # same stage_id BLOCKED ≥ 2× → systemic issue
DUPLICATE_READ_THRESHOLD = 3              # same (agent, file) Read ≥ 3× → thrashing


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def validate_run(
    events: list[dict[str, Any]],
    job_results: list[dict[str, Any]],
    stack: str | None,
) -> RunReport:
    """Validate a completed run and return a diagnostic report.

    Args:
        events: Full event list for this run (from dashboard or local log).
            Can be empty — signals-by-job-result checks still run.
        job_results: Aggregated job result dicts from .ai/runs/<id>/jobs/*.json.
        stack: Detected project stack (typescript/ios/python/both/None).
    """
    report = RunReport()

    # ── Pass 1: gather stats from events ──
    tokens_by_agent: dict[str, dict[str, int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    )
    duration_by_agent: dict[str, float] = defaultdict(float)
    agents_started: set[str] = set()
    agents_skipped: dict[str, str] = {}
    agents_failed: dict[str, str] = {}
    skill_calls: list[dict] = []
    fix_loop_exhausted: list[dict] = []
    test_runs_per_stage: dict[str, int] = defaultdict(int)
    hook_denies_by_agent: dict[str, list[dict]] = defaultdict(list)
    reads_by_agent_file: dict[tuple[str, str, str], int] = defaultdict(int)  # (agent, stage, target) → count

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
                if stage and stage != "unknown":
                    test_runs_per_stage[stage] += 1

        elif ev_type == "agent.skipped":
            agents_skipped[agent] = ev.get("reason", "")

        elif ev_type == "agent.failed":
            agents_failed[agent] = ev.get("error", "")

        elif ev_type == "agent.tool_use":
            tool = ev.get("tool", "")
            if tool == "Skill":
                skill_calls.append({
                    "agent": agent,
                    "skill": ev.get("input_preview", ""),
                    "stage": ev.get("stage", ""),
                })
            elif tool == "Read":
                target = ev.get("target", "") or ""
                stage = ev.get("stage", "") or ""
                if target and stage and stage != "unknown":
                    reads_by_agent_file[(agent, stage, target)] += 1

        elif ev_type == "hook.denied":
            hook_denies_by_agent[agent].append({
                "tool": ev.get("tool", ""),
                "reason": ev.get("reason", ""),
                "input_preview": ev.get("input_preview", ""),
                "stage": ev.get("stage", ""),
            })

        elif ev_type == "fix_loop.exhausted":
            fix_loop_exhausted.append({
                "stage": ev.get("stage", ""),
                "attempt": ev.get("attempt", 0),
                "remaining": ev.get("remaining_failures", []),
            })

    # ── Pass 2: summaries derived from job_results ──
    report.token_summary = dict(tokens_by_agent)
    report.duration_summary = dict(duration_by_agent)

    run_job_results = [j for j in job_results if j.get("command") == "run_job"]

    status_counts: dict[str, int] = defaultdict(int)
    for j in run_job_results:
        status_counts[j.get("status", "UNKNOWN")] += 1
    report.stage_summary = {
        "passed": status_counts.get("PASS", 0),
        "blocked": status_counts.get("BLOCKED", 0),
        "skipped": status_counts.get("SKIPPED", 0),
        "partial": status_counts.get("PARTIAL", 0),
        "interrupted": status_counts.get("INTERRUPTED", 0),
        "review": status_counts.get("REVIEW", 0),
        "total": len(run_job_results),
    }

    # ── Check 1: Missing required agents ──
    # Only meaningful if we have event data; dashboard may have been down.
    if events and run_job_results:
        for required in REQUIRED_SPRINT_AGENTS:
            if required not in agents_started:
                report.items.append(RunReportItem(
                    severity="ERROR",
                    check="missing_agent",
                    message=f"Required agent '{required}' never started during run",
                ))

    # ── Check 2: Final review skipped when stack is known ──
    if stack and stack != "unknown" and events:
        reviewer = (
            "typescript-reviewer" if stack in ("typescript", "both")
            else "ios-reviewer" if stack == "ios"
            else None
        )
        if reviewer and reviewer not in agents_started:
            skip_reason = agents_skipped.get("final-review", "")
            report.items.append(RunReportItem(
                severity="WARNING",
                check="final_review_skipped",
                message=f"Stack is '{stack}' but no {reviewer} invocation observed",
                details={"stack": stack, "reviewer": reviewer, "reason": skip_reason},
            ))

    # ── Check 3: Token anomaly (single agent > threshold output) ──
    for agent, usage in tokens_by_agent.items():
        if usage["output"] > MAX_AGENT_OUTPUT_TOKENS:
            report.items.append(RunReportItem(
                severity="WARNING",
                check="token_anomaly",
                message=f"'{agent}' produced {usage['output']:,} output tokens (threshold: {MAX_AGENT_OUTPUT_TOKENS:,})",
                details={
                    "agent": agent,
                    "output_tokens": usage["output"],
                    "threshold": MAX_AGENT_OUTPUT_TOKENS,
                },
            ))

    # ── Check 4: Token ratio (test-engineer vs implementer) ──
    impl_out = tokens_by_agent.get("implementer", {}).get("output", 0)
    test_out = tokens_by_agent.get("test-engineer", {}).get("output", 0)
    if impl_out > 0 and test_out > impl_out * TEST_TO_IMPL_TOKEN_RATIO:
        ratio = round(test_out / impl_out, 1)
        report.items.append(RunReportItem(
            severity="WARNING",
            check="token_ratio_anomaly",
            message=f"test-engineer output ({test_out:,}) is {ratio}× implementer output ({impl_out:,})",
            details={"test_output": test_out, "impl_output": impl_out, "ratio": ratio},
        ))

    # ── Check 5: Skill calls from worker agents (informational) ──
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
            message=(
                f"Fix loop exhausted after {exhausted['attempt']} attempts "
                f"in '{exhausted['stage']}' with {len(remaining)} unresolved issue(s)"
            ),
            details=exhausted,
        ))

    # ── Check 7: All stages blocked ──
    blocked = status_counts.get("BLOCKED", 0)
    if run_job_results and blocked == len(run_job_results):
        report.items.append(RunReportItem(
            severity="ERROR",
            check="all_stages_blocked",
            message="All implementation stages are BLOCKED — nothing was successfully implemented",
        ))

    # ── Check 8: Agent timeouts ──
    for agent, error in agents_failed.items():
        if "timed out" in error.lower():
            report.items.append(RunReportItem(
                severity="WARNING",
                check="agent_timeout",
                message=f"'{agent}' timed out: {error}",
                details={"agent": agent, "error": error},
            ))

    # ── Check 9: Excessive test reruns ──
    for stage, count in test_runs_per_stage.items():
        if count > 2:
            report.items.append(RunReportItem(
                severity="INFO",
                check="excessive_test_reruns",
                message=f"test-engineer ran {count} times in stage '{stage}'",
                details={"stage": stage, "count": count},
            ))

    # ── Check 10 (NEW): NEEDS_CONTEXT frequency ──
    # A job signals NEEDS_CONTEXT by returning BLOCKED with an unresolved
    # line starting with "NEEDS_CONTEXT:". Multiple in one run means the
    # plan is consistently under-specified — team-lead should enrich.
    needs_context_jobs: list[dict] = []
    for j in job_results:
        unresolved = j.get("unresolved") or []
        for u in unresolved:
            if isinstance(u, str) and "NEEDS_CONTEXT:" in u:
                needs_context_jobs.append({
                    "stage_id": j.get("stage_id") or j.get("command", "?"),
                    "message": u,
                })
                break
    if len(needs_context_jobs) >= NEEDS_CONTEXT_THRESHOLD:
        report.items.append(RunReportItem(
            severity="WARNING",
            check="needs_context_frequency",
            message=(
                f"{len(needs_context_jobs)} job(s) raised NEEDS_CONTEXT — "
                f"the plan is likely under-specified for this pipeline"
            ),
            details={"count": len(needs_context_jobs), "jobs": needs_context_jobs},
        ))
    elif needs_context_jobs:
        # Single NEEDS_CONTEXT is informational, not a warning
        report.items.append(RunReportItem(
            severity="INFO",
            check="needs_context",
            message=f"1 job raised NEEDS_CONTEXT: {needs_context_jobs[0]['message']}",
            details=needs_context_jobs[0],
        ))

    # ── Check 11 (NEW): Hook deny volume per agent ──
    # Prompt-level "don't run pnpm test" is soft; hook is hard. If an agent
    # still triggers N+ hook denies, the prompt isn't sinking in and we may
    # need to strengthen the agent MD or change the task shape.
    for agent, denies in hook_denies_by_agent.items():
        if len(denies) >= HOOK_DENY_THRESHOLD:
            # Aggregate deny reasons to surface the pattern
            reason_counts: dict[str, int] = defaultdict(int)
            for d in denies:
                # Take the first line of reason as the key
                key = d.get("reason", "").split(". ")[0][:80]
                reason_counts[key] += 1
            top_reasons = sorted(reason_counts.items(), key=lambda x: -x[1])[:3]
            report.items.append(RunReportItem(
                severity="WARNING",
                check="hook_deny_volume",
                message=(
                    f"'{agent}' hit hook deny {len(denies)} times — "
                    f"prompt rules aren't landing. Top reasons: "
                    f"{', '.join(f'{r} (×{c})' for r, c in top_reasons)}"
                ),
                details={
                    "agent": agent,
                    "count": len(denies),
                    "top_reasons": dict(top_reasons),
                    "samples": denies[:5],
                },
            ))

    # ── Check 12 (NEW): Repeated stage failures ──
    # Multiple BLOCKED attempts at the same stage_id = systemic issue.
    # Typical cause: team-lead retries a BLOCKED stage without changing
    # anything, or the fix loop resumes after a structural failure.
    stage_blocked_counts: dict[str, int] = defaultdict(int)
    for j in job_results:
        if j.get("status") != "BLOCKED":
            continue
        sid = j.get("stage_id")
        if sid:
            stage_blocked_counts[sid] += 1
    for stage_id, count in stage_blocked_counts.items():
        if count >= REPEATED_STAGE_FAILURE_THRESHOLD:
            report.items.append(RunReportItem(
                severity="ERROR",
                check="repeated_stage_failure",
                message=(
                    f"stage '{stage_id}' BLOCKED {count} times in this run — "
                    f"a retry without a plan change won't fix it"
                ),
                details={"stage_id": stage_id, "count": count},
            ))

    # ── Check 13 (NEW): Agent re-reading same file (thrashing signal) ──
    # Reading the same file 3+ times in the same stage means the agent
    # keeps losing context or can't find what it needs — symptoms of a
    # fragmented plan or bad file organization.
    duplicate_reads: list[dict] = []
    for (agent, stage, target), count in reads_by_agent_file.items():
        if count >= DUPLICATE_READ_THRESHOLD:
            duplicate_reads.append({
                "agent": agent, "stage": stage, "file": target, "count": count,
            })
    if duplicate_reads:
        # Rank by count so the worst cases surface first
        duplicate_reads.sort(key=lambda d: -d["count"])
        worst = duplicate_reads[0]
        total_wasted_reads = sum(d["count"] - 1 for d in duplicate_reads)  # first read is legit
        report.items.append(RunReportItem(
            severity="INFO" if total_wasted_reads < 5 else "WARNING",
            check="duplicate_reads",
            message=(
                f"{len(duplicate_reads)} (agent, stage, file) combo(s) re-read ≥ "
                f"{DUPLICATE_READ_THRESHOLD}×. Worst: '{worst['agent']}' read "
                f"'{worst['file']}' {worst['count']}× in stage '{worst['stage']}'"
            ),
            details={"combos": duplicate_reads[:10], "wasted_reads": total_wasted_reads},
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


def update_routing_hints(
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
