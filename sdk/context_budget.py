"""Helpers for building smaller prompt contexts for orchestration agents.

These helpers are intentionally pure and deterministic so we can validate token
and CPU cost locally before wiring them into the live orchestrator.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


CHARS_PER_TOKEN = 4


@dataclass
class ContextSection:
    heading: str
    body: str


@dataclass
class ContextAudit:
    consumer: str
    full_tokens: int
    compact_tokens: int
    kept_sections: list[str]
    dropped_sections: list[str]
    reduction_tokens: int
    reduction_pct: float

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def parse_sections(raw_sections: list[str]) -> list[ContextSection]:
    """Parse SharedContext.sections into heading/body records."""
    parsed: list[ContextSection] = []
    for raw in raw_sections:
        text = raw.strip()
        if not text:
            continue
        lines = text.splitlines()
        heading_line = lines[0].strip()
        if heading_line.startswith("#"):
            heading = heading_line.lstrip("#").strip()
            body = "\n".join(lines[1:]).strip()
        else:
            heading = ""
            body = text
        parsed.append(ContextSection(heading=heading, body=body))
    return parsed


def _clip(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def build_stage_context(
    raw_sections: list[str],
    *,
    max_recent_stage_results: int = 2,
    plan_chars: int = 1600,
) -> str:
    """Build a compact context for stage execution agents.

    Keeps task/project/plan plus only the most recent stage results. This is the
    highest-leverage optimization target because implementer and contract
    generation prompts currently receive the full accumulated run context.
    """
    sections = parse_sections(raw_sections)
    task = next((s for s in sections if s.heading == "Task"), None)
    project = next((s for s in sections if s.heading == "Project"), None)
    plan = next((s for s in sections if s.heading == "Plan"), None)
    stage_results = [s for s in sections if s.heading.startswith("Stage Result:")]

    compact: list[str] = []
    if task:
        compact.append(f"## Task\n{task.body}")
    if project:
        compact.append(f"## Project\n{project.body}")
    if plan:
        compact.append(f"## Plan\n{_clip(plan.body, plan_chars)}")
    if stage_results:
        recent = stage_results[-max_recent_stage_results:]
        for section in recent:
            compact.append(f"## {section.heading}\n{section.body}")

    return "<run-context>\n" + "\n\n".join(compact) + "\n</run-context>\n"


def build_wrap_context(
    raw_sections: list[str],
    *,
    max_recent_stage_results: int = 4,
    plan_chars: int = 2000,
    final_review_chars: int = 1200,
) -> str:
    """Build a compact context for final review/documentation agents."""
    sections = parse_sections(raw_sections)
    stage_context = build_stage_context(
        raw_sections,
        max_recent_stage_results=max_recent_stage_results,
        plan_chars=plan_chars,
    ).strip()
    final_review = next((s for s in sections if s.heading == "Final Review"), None)
    parts = [stage_context]
    if final_review:
        parts.append(f"## Final Review\n{_clip(final_review.body, final_review_chars)}")
    return "\n\n".join(parts) + "\n"


def audit_stage_context(
    raw_sections: list[str],
    *,
    full_context: str,
    consumer: str,
    max_recent_stage_results: int = 2,
    plan_chars: int = 1600,
) -> ContextAudit:
    """Compare the current full context with the candidate compact stage context."""
    sections = parse_sections(raw_sections)
    compact = build_stage_context(
        raw_sections,
        max_recent_stage_results=max_recent_stage_results,
        plan_chars=plan_chars,
    )

    stage_results = [section for section in sections if section.heading.startswith("Stage Result:")]
    recent_stage_headings = {section.heading for section in stage_results[-max_recent_stage_results:]}

    kept: list[str] = []
    dropped: list[str] = []
    for section in sections:
        heading = section.heading or "(unheaded)"
        if heading in {"Task", "Project", "Plan"} or heading in recent_stage_headings:
            kept.append(heading)
        else:
            dropped.append(heading)

    full_tokens = estimate_tokens(full_context)
    compact_tokens = estimate_tokens(compact)
    reduction_tokens = max(0, full_tokens - compact_tokens)
    reduction_pct = (reduction_tokens / full_tokens * 100.0) if full_tokens else 0.0

    return ContextAudit(
        consumer=consumer,
        full_tokens=full_tokens,
        compact_tokens=compact_tokens,
        kept_sections=kept,
        dropped_sections=dropped,
        reduction_tokens=reduction_tokens,
        reduction_pct=round(reduction_pct, 1),
    )
