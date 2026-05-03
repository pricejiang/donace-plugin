"""donace CLI.

Subcommands:
    run_start                 Mint a run-id, create .ai/runs/<id>/meta.json
    list_runs                 Print existing run-ids in creation order
    parse_plan --run-id <id>  Parse plan.md, emit stages JSON to stdout (Task 12)
    stage_status <run-id>     ASCII pipeline view (Task 13)
    mark_completed --run-id   Set meta.json status=completed
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Iterable


def _runs_root(cwd: Path) -> Path:
    return cwd / ".ai" / "runs"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_start(cwd: Path) -> str:
    """Mint a run-id, create the run directory + meta.json. Return the run-id."""
    runs_root = _runs_root(cwd)
    runs_root.mkdir(parents=True, exist_ok=True)
    run_id = f"run-{secrets.token_hex(4)}"
    run_dir = runs_root / run_id
    run_dir.mkdir()
    (run_dir / "stages").mkdir()
    meta = {
        "status": "spec",
        "created_at": _utc_now(),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return run_id


def list_runs(cwd: Path) -> list[str]:
    """Return existing run-ids, sorted by creation time (oldest first)."""
    runs_root = _runs_root(cwd)
    if not runs_root.exists():
        return []
    entries = sorted(
        (p for p in runs_root.iterdir() if p.is_dir() and p.name.startswith("run-")),
        key=lambda p: p.stat().st_ctime,
    )
    return [p.name for p in entries]


def mark_completed(cwd: Path, run_id: str) -> None:
    meta_path = _runs_root(cwd) / run_id / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["status"] = "completed"
    meta["completed_at"] = _utc_now()
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")


# ---- plan parsing ----

_VALID_IMPLEMENTERS = {"claude", "codex"}

_STAGE_HEADER_RE = re.compile(r"^## Stage (\d+): (.+)$")
_SCALAR_FIELD_RE = re.compile(r"^- ([^:]+):(.*)$")
_LIST_BULLET_RE = re.compile(r"^  - (.+)$")


def parse_plan(cwd: Path, run_id: str) -> list[dict]:
    """Parse plan.md into a list of stage dicts.

    Stage dict keys:
      sid              — "stage-N"
      name             — stage title text after "## Stage N: "
      implementer      — "claude" | "codex"
      goal             — string
      files            — list[str]
      success_criteria — list[str]
      tests            — list[str]
    """
    plan_path = _runs_root(cwd) / run_id / "plan.md"
    if not plan_path.exists():
        raise FileNotFoundError(f"plan.md not found at {plan_path}")
    return _parse_plan_text(plan_path.read_text())


def parse_plan_to_json(cwd: Path, run_id: str) -> str:
    return json.dumps(parse_plan(cwd, run_id), indent=2)


def _parse_plan_text(text: str) -> list[dict]:
    stages: list[dict] = []
    current: dict | None = None
    current_list_field: str | None = None  # which list field we're appending bullets to

    for raw in text.splitlines():
        line = raw.rstrip()

        # Stage header: ## Stage N: name
        m = _STAGE_HEADER_RE.match(line)
        if m:
            if current is not None:
                _validate_stage(current)
                stages.append(current)
            current = {
                "sid": f"stage-{m.group(1)}",
                "name": m.group(2).strip(),
                "files": [],
                "success_criteria": [],
                "tests": [],
            }
            current_list_field = None
            continue

        if current is None:
            continue

        # Scalar field: - implementer: x
        m = _SCALAR_FIELD_RE.match(line)
        if m:
            field, value = m.group(1).strip().lower(), m.group(2).strip()
            if field == "implementer":
                if value not in _VALID_IMPLEMENTERS:
                    raise ValueError(
                        f"stage {current['sid']}: implementer must be 'claude' or 'codex', got {value!r}"
                    )
                current["implementer"] = value
                current_list_field = None
            elif field == "goal":
                current["goal"] = value
                current_list_field = None
            elif field == "files":
                current["files"] = [f.strip() for f in value.split(",") if f.strip()]
                current_list_field = None
            elif field in ("success criteria", "tests"):
                # These are list openers; bullets follow on subsequent lines.
                current_list_field = "success_criteria" if field == "success criteria" else "tests"
            continue

        # List bullet: "  - some text"
        m = _LIST_BULLET_RE.match(line)
        if m and current_list_field:
            current[current_list_field].append(m.group(1).strip())
            continue

        # Blank or unknown — leaves current_list_field intact.

    if current is not None:
        _validate_stage(current)
        stages.append(current)

    return stages


def _validate_stage(stage: dict) -> None:
    for f in ("implementer", "goal"):
        if not stage.get(f):
            raise ValueError(f"stage {stage.get('sid', '?')}: missing required field '{f}'")
    for f in ("files", "success_criteria", "tests"):
        if not stage.get(f):
            raise ValueError(f"stage {stage.get('sid', '?')}: '{f}' is empty")


# ---- test command handling ----

def is_none_marker(bullet: str) -> bool:
    """A `tests:` bullet that begins with 'none:' is a deliberate skip."""
    return bullet.lstrip().lower().startswith("none:")


def format_test_results_md(*, sid: str, attempt: int, results: Iterable[dict]) -> str:
    """Render test-results.md for a single attempt."""
    lines = [f"# Test results: {sid} (attempt {attempt})", ""]
    for i, r in enumerate(results, start=1):
        lines.append(f"## Command {i}")
        lines.append(f"$ {r['command']}")
        lines.append(f"exit: {r['exit_code']}")
        if r.get("stdout"):
            lines.append("stdout (last 2000):")
            lines.append(r["stdout"])
        if r.get("stderr"):
            lines.append("stderr (last 2000):")
            lines.append(r["stderr"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="donace")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run_start")
    sub.add_parser("list_runs")

    p_mark = sub.add_parser("mark_completed")
    p_mark.add_argument("--run-id", required=True)

    p_parse = sub.add_parser("parse_plan")
    p_parse.add_argument("--run-id", required=True)

    p_status = sub.add_parser("stage_status")
    p_status.add_argument("run_id")

    args = parser.parse_args(argv)
    cwd = Path.cwd()

    if args.cmd == "run_start":
        run_id = run_start(cwd)
        run_dir = _runs_root(cwd) / run_id
        print(json.dumps({"run_id": run_id, "run_dir": str(run_dir)}))
        return 0

    if args.cmd == "list_runs":
        for r in list_runs(cwd):
            print(r)
        return 0

    if args.cmd == "mark_completed":
        mark_completed(cwd, args.run_id)
        return 0

    if args.cmd == "parse_plan":
        # Implemented in Task 12. Until then, the subcommand is registered
        # so argparse doesn't error, but invoking it surfaces a clear message.
        if "parse_plan_to_json" not in globals():
            print("parse_plan not yet implemented (Task 12)", file=sys.stderr)
            return 1
        print(parse_plan_to_json(cwd, args.run_id))  # type: ignore[name-defined]
        return 0

    if args.cmd == "stage_status":
        if "render_stage_status" not in globals():
            print("stage_status not yet implemented (Task 13)", file=sys.stderr)
            return 1
        sys.stdout.write(render_stage_status(cwd, args.run_id))  # type: ignore[name-defined]
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
