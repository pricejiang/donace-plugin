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
import calendar
import json
import os
import re
import secrets
import shutil
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


# ---- stage_status rendering ----

_GLYPHS_UTF8 = {
    "passed": "✓",
    "running": "⟳",
    "pending": "·",
    "blocked": "✗",
    "interrupted": "‖",
}
_GLYPHS_ASCII = {
    "passed": "*",
    "running": ">",
    "pending": ".",
    "blocked": "!",
    "interrupted": "|",
}


def _glyph_for(status: str, *, utf8: bool) -> str:
    table = _GLYPHS_UTF8 if utf8 else _GLYPHS_ASCII
    return table.get(status, "?")


def _reviewer_for(implementer: str) -> str:
    return {"claude": "codex", "codex": "claude"}[implementer]


def _state_summary(status_json: dict | None) -> str:
    if status_json is None:
        return "pending"
    st = status_json.get("status", "pending")
    rc = status_json.get("retry_count", 0)
    reason = status_json.get("reason")
    if st == "passed":
        return f"passed ({rc + 1} tr{'y' if rc == 0 else 'ies'})"
    if st == "running":
        return f"running, retry {rc}/2" if rc else "running"
    if st in ("blocked", "interrupted"):
        return f"{st}: {reason}" if reason else st
    return st


def _utf8_supported() -> bool:
    enc = (os.environ.get("LANG", "") + os.environ.get("LC_ALL", "")).lower()
    return "utf" in enc


def _relative_time(then_iso: str) -> str:
    """Render 'started X ago' relative to now. then_iso is expected in UTC ('...Z')."""
    try:
        # calendar.timegm interprets the struct_time as UTC (mktime would treat it as local).
        then = time.strptime(then_iso, "%Y-%m-%dT%H:%M:%SZ")
        delta_sec = int(time.time() - calendar.timegm(then))
    except Exception:
        return "started ?"
    if delta_sec < 60:
        return f"started {delta_sec}s ago"
    if delta_sec < 3600:
        return f"started {delta_sec // 60}m ago"
    if delta_sec < 86400:
        return f"started {delta_sec // 3600}h ago"
    return f"started {delta_sec // 86400}d ago"


def render_stage_status(cwd: Path, run_id: str, *, utf8: bool | None = None, width: int | None = None) -> str:
    if utf8 is None:
        utf8 = _utf8_supported()
    if width is None:
        width = shutil.get_terminal_size((80, 24)).columns

    run_dir = _runs_root(cwd) / run_id
    if not run_dir.exists():
        return f"Run {run_id} not found.\n"

    meta = json.loads((run_dir / "meta.json").read_text())
    stages = parse_plan(cwd, run_id) if (run_dir / "plan.md").exists() else []

    # Per-stage status_json (None if not yet started).
    stage_states: list[dict | None] = []
    for s in stages:
        sj = run_dir / "stages" / s["sid"] / "status.json"
        stage_states.append(json.loads(sj.read_text()) if sj.exists() else None)

    passed = sum(1 for st in stage_states if st and st.get("status") == "passed")
    overall = meta.get("status", "unknown")

    lines = []
    lines.append(
        f"Run {run_id}  •  {overall}  •  {passed}/{len(stages)} stages passed  •  {_relative_time(meta.get('created_at', ''))}"
    )
    lines.append("")

    # Compute name column width.
    max_name_len = max((len(s["name"]) for s in stages), default=0)
    name_col = min(max_name_len, max(20, width - 60))

    for stage, st in zip(stages, stage_states):
        status = (st or {}).get("status", "pending")
        glyph = _glyph_for(status, utf8=utf8)
        name = stage["name"]
        if len(name) > name_col:
            name = name[: name_col - 1] + "…"
        impl = stage["implementer"]
        rev = _reviewer_for(impl)
        summary = _state_summary(st)
        lines.append(f"  {glyph}  {stage['sid']:7s} {name:<{name_col}}   {impl} → {rev}   {summary}")

    # Tail extraction for the single active or blocked stage.
    active_idx = next(
        (i for i, st in enumerate(stage_states) if st and st.get("status") in ("running", "blocked")),
        None,
    )
    if active_idx is not None:
        sid = stages[active_idx]["sid"]
        st = stage_states[active_idx]
        assert st is not None  # narrowed by the next() filter above
        tail = _extract_tail(run_dir / "stages" / sid, st)
        if tail:
            lines.append("")
            arrow = "▼" if utf8 else "v"
            lines.append(f"{arrow} {sid} latest evidence (retry {st.get('retry_count', 0)}):")
            lines.append(tail)

    return "\n".join(lines) + "\n"


def _extract_tail(stage_dir: Path, status_json: dict, *, max_lines: int = 8) -> str:
    """Last failing test command + tail, OR first [P0] block from review.md."""
    reason = status_json.get("reason", "")
    tr = stage_dir / "test-results.md"
    rv = stage_dir / "review.md"

    if "tests failed" in reason and tr.exists():
        text = tr.read_text()
        # Take the last $-prefixed command + its stderr block.
        chunks = text.split("\n## Command")
        if chunks:
            last = chunks[-1]
            return "  " + "\n  ".join(last.strip().splitlines()[: max_lines + 2])
    if "P0" in reason and rv.exists():
        text = rv.read_text()
        idx = text.find("### [P0]")
        if idx >= 0:
            block = text[idx:].split("\n### [", 1)[0]
            return "  " + "\n  ".join(block.strip().splitlines()[:max_lines])
    return ""


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
