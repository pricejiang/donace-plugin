"""Token audit helpers for the orchestration pipeline.

The audit is deterministic and does not call any LLMs. It reuses the current
prompt assembly patterns so we can estimate where Claude input tokens are spent
before changing orchestration behavior.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sdk.events import Stage, StageResult
from sdk.orchestrator import SharedContext, _parse_plan_stages


CHARS_PER_TOKEN = 4
DEFAULT_TASK = "Build a user-facing feature with staged implementation and verification."


@dataclass
class PromptMeasurement:
    label: str
    chars: int
    est_tokens: int
    category: str


@dataclass
class AuditReport:
    measurements: list[PromptMeasurement]
    totals_by_category: dict[str, int]
    suggestions: list[str]
    fast_path_savings: dict = field(
        default_factory=lambda: {"stages": [], "total_est_tokens_saved": 0, "stage_count": 0}
    )

    def to_dict(self) -> dict:
        return {
            "measurements": [asdict(item) for item in self.measurements],
            "totals_by_category": dict(self.totals_by_category),
            "suggestions": list(self.suggestions),
            "fast_path_savings": self.fast_path_savings,
        }


def estimate_tokens(text: str) -> int:
    """Use a cheap approximation that is good enough for relative comparisons."""
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _plan_markdown(stages: list[Stage]) -> str:
    parts = ["# Implementation Plan: Token Audit Scenario", "", "## Overview", "Synthetic audit plan."]
    for idx, stage in enumerate(stages, start=1):
        deps = ", ".join(stage.depends_on) if stage.depends_on else "None"
        user_facing = "Yes" if stage.has_user_facing_changes else "No"
        parts.extend([
            "",
            f"## Stage {idx}: {stage.name}",
            "**Goal**: Synthetic stage for prompt-cost auditing",
            "**Success Criteria**: Code builds and verifiers pass",
            f"**Files to modify**: src/module_{idx}.py, tests/test_module_{idx}.py",
            f"**Dependencies**: {deps}",
            f"**Has user-facing changes**: {user_facing}",
            f"**Tests**: test_{idx}_happy_path, test_{idx}_error_path",
            "**Risk**: Low",
            "**Status**: Not Started",
        ])
    return "\n".join(parts)


def _build_shared_context(task: str, cwd: str, stages: list[Stage], plan_raw: str, stack: str) -> SharedContext:
    shared_ctx = SharedContext(run_id="audit", cwd=cwd, task=task)
    shared_ctx.add("Project", f"Stack: {stack}\nCwd: {cwd}")
    stage_list = "\n".join(
        f"- Stage {idx + 1}: {stage.name} (user-facing: {stage.has_user_facing_changes}, depends: {stage.depends_on or 'none'})"
        for idx, stage in enumerate(stages)
    )
    shared_ctx.add("Plan", f"Stages:\n{stage_list}\n\nFull plan:\n{plan_raw[:3000]}")
    return shared_ctx


def _contract_text(stage_name: str) -> str:
    return "\n".join([
        f"## Sprint Contract: {stage_name}",
        "",
        "### Must pass (blocking)",
        "- [ ] Happy path works end-to-end",
        "- [ ] Failure path reports a clear error",
        "- [ ] State persists correctly",
        "",
        "### Should pass (non-blocking)",
        "- [ ] Logs remain clean during normal usage",
        "",
        "### Out of scope for this sprint",
        "- [ ] Follow-up polish and refactors",
    ])


def _test_prompt(stage_name: str) -> str:
    changed_files = "\n".join([
        f"src/{stage_name.lower().replace(' ', '_')}.ts",
        f"tests/{stage_name.lower().replace(' ', '_')}.test.ts",
    ])
    return (
        f"Write and run tests for this stage: {stage_name}\n\n"
        f"Files changed by the implementer:\n```\n{changed_files}\n```\n\n"
        "Focus your tests on these files. Do not explore the codebase to find what changed — "
        "the list above is complete.\n\n"
        "After running the tests, output a summary line in this exact format:\n"
        "TEST_SUMMARY: passed=N failed=N\n\n"
        "This summary must reflect the actual test run results."
    )


def _append_stage_result(shared_ctx: SharedContext, stage_result: StageResult) -> None:
    test_info = stage_result.test_result
    summary = (
        f"Status: {stage_result.status}\n"
        f"Tests: {test_info.get('passed', 0)} passed, {test_info.get('failed', 0)} failed\n"
        f"Fix attempts: {stage_result.fix_attempts}"
    )
    if stage_result.unresolved:
        summary += f"\nUnresolved: {', '.join(stage_result.unresolved[:3])}"
    shared_ctx.add(f"Stage Result: {stage_result.name}", summary)


def _measure(label: str, text: str, category: str) -> PromptMeasurement:
    return PromptMeasurement(
        label=label,
        chars=len(text),
        est_tokens=estimate_tokens(text),
        category=category,
    )


def _suggestions(
    measurements: list[PromptMeasurement],
    fast_path_savings: dict | None = None,
) -> list[str]:
    by_label = {item.label: item for item in measurements}
    implementers = [item for item in measurements if item.category == "implementer"]
    contracts = [item for item in measurements if item.category == "contract"]
    totals_by_category: dict[str, int] = {}
    for item in measurements:
        totals_by_category[item.category] = totals_by_category.get(item.category, 0) + item.est_tokens
    suggestions: list[str] = []

    if totals_by_category:
        largest_category = max(totals_by_category, key=totals_by_category.get)
        if largest_category == "implementer":
            suggestions.append(
                "Implementer prompts are the largest cumulative Claude input cost. Shrink stage execution context before optimizing smaller one-off prompts."
            )

    if len(implementers) >= 2:
        growth = implementers[-1].est_tokens - implementers[0].est_tokens
        growth_ratio = implementers[-1].est_tokens / max(1, implementers[0].est_tokens)
        if growth > 250 or growth_ratio > 1.35:
            suggestions.append(
                "Implementer prompts grow materially across stages. Trim `run-context` for stage execution to plan + current stage + last 1-2 stage summaries."
            )

    if contracts:
        avg_contract = sum(item.est_tokens for item in contracts) / len(contracts)
        if avg_contract > 500:
            suggestions.append(
                "Sprint-contract prompts are expensive. Generate contracts from a compact stage spec instead of the full shared context."
            )

    final_review = by_label.get("final-review")
    documenter = by_label.get("documenter")
    if final_review and final_review.est_tokens > 1200:
        suggestions.append(
            "Final review prompt is large. Pass changed-file lists and compact stage outcomes instead of the full accumulated context."
        )
    if documenter and documenter.est_tokens > 1400:
        suggestions.append(
            "Documenter is one of the largest prompts. Replace full-context docs updates with a structured summary: changed files, final review findings, and completed stages."
        )

    if fast_path_savings:
        for stage in fast_path_savings.get("stages", []):
            if stage.get("est_tokens_saved", 0) > 200:
                suggestions.append(
                    "Doc-only stages are eligible for fast-path execution — skip implementer and test-engineer Claude calls to save tokens on stages that only update documentation."
                )
                break

    if not suggestions:
        suggestions.append("Current synthetic audit does not show a single dominant hotspot. Start with runtime measurements from `agent.tokens` on a real run.")
    return suggestions


def audit_prompt_budgets(
    task: str = DEFAULT_TASK,
    stages: list[Stage] | None = None,
    plan_raw: str | None = None,
    cwd: str = "/tmp/project",
    stack: str = "typescript",
    doc_only_stages: list[str] | None = None,
) -> AuditReport:
    """Audit prompt sizes for the current orchestration strategy.

    Args:
        doc_only_stages: Optional list of stage names that are documentation-only
            and eligible for fast-path execution (skipping implementer + test-engineer
            Claude calls). Passing ``None`` or an empty list leaves the output
            identical to the pre-fast-path behaviour.
    """
    if stages is None:
        stages = [
            Stage(name=f"Stage {idx}", has_user_facing_changes=True)
            for idx in range(1, 6)
        ]
    if plan_raw is None:
        plan_raw = _plan_markdown(stages)

    shared_ctx = _build_shared_context(task=task, cwd=cwd, stages=stages, plan_raw=plan_raw, stack=stack)
    measurements: list[PromptMeasurement] = []

    # Track per-stage implementer + test-engineer token costs for fast-path savings.
    _stage_tokens: dict[str, dict[str, int]] = {}

    for idx, stage in enumerate(stages, start=1):
        task_context = shared_ctx.to_prompt_prefix()
        contract_prompt = (
            f"Write sprint contract for: {stage.name}\n\nContext:\n{task_context}"
            if task_context else f"Write sprint contract for: {stage.name}"
        )
        contract = _contract_text(stage.name)
        implement_prompt = f"{task_context}\n\nImplement: {stage.name}\n\nContract:\n{contract}"

        impl_m = _measure(f"stage-{idx}-implementer", implement_prompt, "implementer")
        test_m = _measure(f"stage-{idx}-test-engineer", _test_prompt(stage.name), "test-engineer")

        measurements.append(_measure(f"stage-{idx}-contract", contract_prompt, "contract"))
        measurements.append(impl_m)
        measurements.append(test_m)

        _stage_tokens[stage.name] = {
            "implementer": impl_m.est_tokens,
            "test-engineer": test_m.est_tokens,
        }

        _append_stage_result(
            shared_ctx,
            StageResult(
                name=stage.name,
                status="PASS",
                contract=contract,
                test_result={"passed": 8, "failed": 0},
                codex_result={"status": "completed", "has_issues": False, "output": ""},
                runtime_result={"status": "PASS", "score": "3/3"} if stage.has_user_facing_changes else None,
                fix_attempts=0,
            ),
        )

    final_review_prompt = (
        f"{shared_ctx.to_prompt_prefix()}"
        "Full codebase review of all changes made during this session. "
        "Context about what changed is in <run-context> above."
    )
    measurements.append(_measure("final-review", final_review_prompt, "wrap"))

    final_review = (
        "## Review Summary\n"
        "| Severity | Count |\n|----------|-------|\n| Critical | 0 |\n| Warning | 2 |\n"
        "Verdict: WARNING — a few issues remain.\n"
    ) * 20
    shared_ctx.add("Final Review", final_review[:3000] if final_review else "(no review)")
    documenter_prompt = (
        f"{shared_ctx.to_prompt_prefix()}"
        "All context about what changed is in <run-context> above. "
        "Do NOT explore the codebase to discover what changed — the context is complete.\n\n"
        "Update all relevant project documentation: README.md, CLAUDE.md, CHANGELOG.md, "
        ".ai/plans/current-plan.md, session log, and knowledge cards as needed."
    )
    measurements.append(_measure("documenter", documenter_prompt, "wrap"))

    totals_by_category: dict[str, int] = {}
    for item in measurements:
        totals_by_category[item.category] = totals_by_category.get(item.category, 0) + item.est_tokens

    # Compute fast-path savings for doc-only stages.
    _doc_only_set = set(doc_only_stages) if doc_only_stages else set()
    fp_stages: list[dict] = []
    for stage_name, tokens in _stage_tokens.items():
        if stage_name in _doc_only_set:
            saved = tokens["implementer"] + tokens["test-engineer"]
            fp_stages.append({"name": stage_name, "est_tokens_saved": saved})
    fast_path_savings: dict = {
        "stages": fp_stages,
        "total_est_tokens_saved": sum(s["est_tokens_saved"] for s in fp_stages),
        "stage_count": len(fp_stages),
    }

    return AuditReport(
        measurements=measurements,
        totals_by_category=totals_by_category,
        suggestions=_suggestions(measurements, fast_path_savings=fast_path_savings),
        fast_path_savings=fast_path_savings,
    )


def load_plan(plan_path: str) -> tuple[list[Stage], str]:
    content = Path(plan_path).read_text("utf-8")
    stages = _parse_plan_stages(content)
    if not stages:
        raise ValueError(f"Could not parse any stages from {plan_path}")
    return stages, content


def _format_text(report: AuditReport) -> str:
    lines = ["Token Audit", ""]
    lines.append("Top prompt hotspots:")
    for item in sorted(report.measurements, key=lambda entry: entry.est_tokens, reverse=True)[:8]:
        lines.append(
            f"- {item.label}: {item.est_tokens} est tokens ({item.chars} chars, category={item.category})"
        )
    lines.append("")
    lines.append("Totals by category:")
    for category, total in sorted(report.totals_by_category.items(), key=lambda entry: entry[1], reverse=True):
        lines.append(f"- {category}: {total} est tokens")

    fp_stages = report.fast_path_savings.get("stages", [])
    if fp_stages:
        lines.append("")
        lines.append("Fast-path savings:")
        for stage in fp_stages:
            lines.append(f"- {stage['name']}: {stage['est_tokens_saved']} est tokens saved (implementer + test-engineer skipped)")
        lines.append(f"  Total: {report.fast_path_savings['total_est_tokens_saved']} est tokens saved across {report.fast_path_savings['stage_count']} stage(s)")

    lines.append("")
    lines.append("Suggested next changes:")
    for suggestion in report.suggestions:
        lines.append(f"- {suggestion}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit orchestration prompt/token hotspots without calling Claude")
    parser.add_argument("plan_path", nargs="?", help="Path to a .ai/plans/current-plan.md file (positional shorthand for --plan)")
    parser.add_argument("--task", default=DEFAULT_TASK, help="Task description used to seed the shared context")
    parser.add_argument("--plan", help="Path to a real .ai/plans/current-plan.md file")
    parser.add_argument("--stage-count", type=int, default=5, help="Synthetic stage count when --plan is omitted")
    parser.add_argument("--doc-only-stages", help="Comma-separated stage names eligible for fast-path execution (e.g. Stage1,Stage2)")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()

    plan_source = args.plan_path or args.plan
    if plan_source:
        stages, plan_raw = load_plan(plan_source)
    else:
        stages = [Stage(name=f"Audit Slice {idx}", has_user_facing_changes=True) for idx in range(1, args.stage_count + 1)]
        plan_raw = _plan_markdown(stages)

    doc_only: list[str] | None = None
    if args.doc_only_stages:
        doc_only = [s.strip() for s in args.doc_only_stages.split(",") if s.strip()]

    report = audit_prompt_budgets(task=args.task, stages=stages, plan_raw=plan_raw, doc_only_stages=doc_only)
    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(_format_text(report))


if __name__ == "__main__":
    main()
