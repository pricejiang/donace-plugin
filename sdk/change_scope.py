"""Helpers for classifying change scope and identifying low-cost fast paths."""
from __future__ import annotations

from pathlib import Path


DOC_EXTENSIONS = {
    ".md",
    ".mdx",
    ".rst",
    ".adoc",
    ".txt",
}

DOC_FILENAMES = {
    "README",
    "README.md",
    "CHANGELOG",
    "CHANGELOG.md",
    "CLAUDE.md",
}

IGNORABLE_RUNTIME_ARTIFACTS = {
    ".pytest_output.txt",
    "test_results.txt",
    "test_run_output.txt",
    "unittest_results.txt",
}

TRIVIAL_DOC_KEYWORDS = (
    "readme",
    "changelog",
    "claude.md",
    "documentation",
    "docs",
    "doc-only",
    "title",
    "header",
    "typo",
    "comment",
    "comments",
    "rename",
    "spelling",
)


def parse_changed_files(raw: str) -> list[str]:
    """Split git output into normalized repo-relative paths."""
    if not raw:
        return []
    files: list[str] = []
    for line in raw.splitlines():
        item = line.strip()
        if not item or item.startswith("("):
            continue
        files.append(item)
    return files


def is_ignorable_runtime_artifact(path: str) -> bool:
    """Ignore local audit/test artifacts when classifying user changes."""
    name = Path(path).name
    return (
        name in IGNORABLE_RUNTIME_ARTIFACTS
        or path.startswith(".ai/runs/")
    )


def is_documentation_path(path: str) -> bool:
    """Return True when a path is clearly documentation or AI-session metadata."""
    normalized = path.strip().lstrip("./")
    if not normalized:
        return False
    if normalized.startswith(".ai/sessions/") or normalized.startswith(".ai/cards/") or normalized.startswith(".ai/plans/"):
        return True
    pure = Path(normalized)
    return pure.suffix.lower() in DOC_EXTENSIONS or pure.name in DOC_FILENAMES


def relevant_changed_files(paths: list[str]) -> list[str]:
    """Drop runtime artifacts so change classification reflects user-facing edits."""
    return [path for path in paths if not is_ignorable_runtime_artifact(path)]


def is_documentation_only_change(paths: list[str]) -> bool:
    """Return True when every relevant changed file is documentation-like."""
    relevant = relevant_changed_files(paths)
    return bool(relevant) and all(is_documentation_path(path) for path in relevant)


def looks_like_trivial_doc_task(text: str) -> bool:
    """Heuristic for obvious low-risk documentation edits."""
    lowered = text.lower()
    return any(keyword in lowered for keyword in TRIVIAL_DOC_KEYWORDS)
