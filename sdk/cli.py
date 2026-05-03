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
import secrets
import sys
import time
from pathlib import Path


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
