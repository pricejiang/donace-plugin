"""Subprocess wrapper around the openai-codex `codex-companion.mjs` script.

Three CLI modes (mirrors the spec section "Codex shell helper"):

    codex_call.py implement   --run-id <id> --stage-id <sid> \
                              [--retry-context-file <path>]
    codex_call.py review      --run-id <id> --stage-id <sid> \
                              --diff-file <path> \
                              --test-results-file <path> \
                              --stack <python|typescript|ios|general>
    codex_call.py review-plan --run-id <id>

`review-plan` is dispatched by `/donace:plan` after the planner subagent finishes.
It is advisory (the orchestrator does NOT gate on its findings).

Stdout JSON contract:
    {"status": "completed", "summary": "...", "raw_output": "..."}    # success
    {"status": "error", "error_class": "rate_limited|...", "message": "..."}

Exit codes:
    0 — success
    1 — retry-eligible error
    2 — rate_limited
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

_PLUGIN_CACHE = Path.home() / ".claude" / "plugins" / "cache" / "openai-codex" / "codex"
_POLL_INTERVAL_SEC = 5
_POLL_MAX_SEC = 600

# Sentinel substrings codex-companion's status response may surface for rate limit.
_RATE_LIMIT_HINTS = (
    "rate limit",
    "rate-limit",
    "429",
    "RateLimitError",
)

_STAGE_HEADER_RE = re.compile(r"^## Stage (\d+): .+$")


class RateLimited(Exception):
    """Raised when codex / its upstream model hits a rate limit. Exit code 2."""


def _locate_companion() -> Path:
    """Find the most recent codex-companion.mjs under ~/.claude/plugins/cache/openai-codex."""
    if not _PLUGIN_CACHE.exists():
        raise FileNotFoundError(
            f"openai-codex plugin not found at {_PLUGIN_CACHE}. "
            "Install the plugin via /plugin install openai-codex/codex first."
        )
    candidates = sorted(_PLUGIN_CACHE.glob("*/scripts/codex-companion.mjs"))
    if not candidates:
        raise FileNotFoundError(
            f"No codex-companion.mjs under {_PLUGIN_CACHE}. Plugin may be partially installed."
        )
    return candidates[-1]  # most recent version


def _sleep(seconds: int) -> None:
    """Indirection so tests can stub it out."""
    time.sleep(seconds)


def _run_companion(companion: Path, args: list[str], cwd: Path) -> dict:
    """Invoke `node <companion> <args>` and parse stdout as JSON.

    Raises FileNotFoundError if node is missing, ValueError on parse failure,
    RuntimeError if the companion exits non-zero.
    """
    proc = subprocess.run(
        ["node", str(companion), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"codex-companion exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"codex-companion stdout was not JSON: {proc.stdout[:500]}") from exc


def _extract_stage_block(plan_text: str, stage_id: str) -> str:
    """Return the verbatim markdown block for one stage from plan.md."""
    stage_num = stage_id.removeprefix("stage-")
    target_header = f"## Stage {stage_num}:"
    lines = plan_text.splitlines()
    start_idx: int | None = None

    for i, line in enumerate(lines):
        if line.startswith(target_header):
            start_idx = i
            break

    if start_idx is None:
        raise ValueError(f"stage {stage_id} not found in plan.md")

    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if _STAGE_HEADER_RE.match(lines[i]):
            end_idx = i
            break

    return "\n".join(lines[start_idx:end_idx]).strip()


def _build_prompt(
    mode: str,
    run_id: str,
    stage_id: Optional[str],
    cwd: Path,
    diff_file: Optional[Path],
    retry_context_file: Optional[Path],
    test_results_file: Optional[Path],
    stack: Optional[str],
) -> str:
    """Compose the prompt: prefix file + payload.

    `stage_id` is required for implement / review (per-stage modes) and ignored
    for review-plan (whole-plan mode).
    """
    plugin_root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", cwd))

    if mode == "implement":
        prefix_path = plugin_root / "prompts" / "codex-implementer.md"
    elif mode == "review":
        prefix_path = plugin_root / "prompts" / "codex-reviewer.md"
    elif mode == "review-plan":
        prefix_path = plugin_root / "prompts" / "codex-plan-reviewer.md"
    else:
        raise ValueError(f"Unknown mode: {mode}")

    prefix = prefix_path.read_text() if prefix_path.exists() else ""

    parts = [prefix, "", f"run-id: {run_id}", f"cwd: {cwd}"]
    if mode != "review-plan":
        parts.insert(3, f"stage-id: {stage_id}")

    run_dir = cwd / ".ai" / "runs" / run_id

    if mode == "review-plan":
        spec_path = run_dir / "spec.md"
        plan_path = run_dir / "plan.md"
        if not spec_path.exists():
            raise FileNotFoundError(f"spec.md not found: {spec_path}")
        if not plan_path.exists():
            raise FileNotFoundError(f"plan.md not found: {plan_path}")
        parts.extend(["", "spec.md:", "---", spec_path.read_text(), "---"])
        parts.extend(["", "plan.md:", "---", plan_path.read_text(), "---"])
        return "\n".join(parts)

    plan_path = run_dir / "plan.md"
    if plan_path.exists():
        plan_text = plan_path.read_text()
        parts.extend(["", "stage block:", "---", _extract_stage_block(plan_text, stage_id), "---"])

    if mode == "implement" and retry_context_file:
        if not retry_context_file.exists():
            raise FileNotFoundError(f"retry context file not found: {retry_context_file}")
        parts.extend(["", "retry context:", "---", retry_context_file.read_text(), "---"])

    if mode == "review":
        if diff_file and diff_file.exists():
            parts.extend(["", "diff:", "---", diff_file.read_text(), "---"])
        if test_results_file and test_results_file.exists():
            parts.extend(["", "test results:", "---", test_results_file.read_text(), "---"])
        if stack:
            checklist_path = plugin_root / "references" / f"review-checklist-{stack}.md"
            if checklist_path.exists():
                parts.extend(["", f"stack checklist ({stack}):", "---", checklist_path.read_text(), "---"])

    return "\n".join(parts)


def run(
    *,
    mode: str,
    run_id: str,
    stage_id: Optional[str],
    cwd: Path,
    diff_file: Optional[Path],
    retry_context_file: Optional[Path],
    test_results_file: Optional[Path],
    stack: Optional[str],
) -> dict:
    """High-level: build prompt, launch task, poll, fetch result.

    Targets codex-companion 1.0.2's CLI: prompt is delivered via `--prompt-file`
    (positional + `--prompt` are no longer accepted), `status` / `result` need
    `--json`, and `task` needs `--write` so codex can edit files in implement mode.

    `stage_id` is required for implement / review and ignored for review-plan
    (which targets the whole plan, no stage scope).
    """
    if mode != "review-plan" and not stage_id:
        raise ValueError(f"stage_id is required for mode={mode}")

    companion = _locate_companion()
    prompt = _build_prompt(
        mode,
        run_id,
        stage_id,
        cwd,
        diff_file,
        retry_context_file,
        test_results_file,
        stack,
    )

    # Persist the prompt to disk so codex-companion can read it via --prompt-file.
    # Per-stage modes go into the stage dir; plan review goes into the run dir.
    if mode == "review-plan":
        evidence_dir = cwd / ".ai" / "runs" / run_id
    else:
        evidence_dir = cwd / ".ai" / "runs" / run_id / "stages" / stage_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = evidence_dir / f"codex-{mode}-prompt.txt"
    prompt_path.write_text(prompt)

    # Launch background task. `--write` only for implement (reviews are read-only).
    task_args = ["task", "--background", "--json"]
    if mode == "implement":
        task_args.append("--write")
    task_args.extend(["--prompt-file", str(prompt_path), "--cwd", str(cwd)])
    launch = _run_companion(companion, task_args, cwd=cwd)
    job_id = launch.get("jobId")
    if not job_id:
        raise RuntimeError(f"task launch returned no jobId: {launch}")

    # Poll status until terminal.
    elapsed = 0
    status_resp: dict = {}
    st: str = ""
    while elapsed < _POLL_MAX_SEC:
        status_resp = _run_companion(companion, ["status", job_id, "--json"], cwd=cwd)
        job_obj = status_resp.get("job") or {}
        st = job_obj.get("status", "")
        if st in ("completed", "failed", "cancelled"):
            break
        if st in ("queued", "running"):
            _sleep(_POLL_INTERVAL_SEC)
            elapsed += _POLL_INTERVAL_SEC
            continue
        # Unknown status — break to surface as error.
        break
    else:
        raise TimeoutError(f"codex job {job_id} did not finish within {_POLL_MAX_SEC}s")

    if st != "completed":
        # Inspect the entire status response for rate-limit hints before
        # falling through to a generic worker_failed.
        status_text = json.dumps(status_resp).lower()
        if any(hint.lower() in status_text for hint in _RATE_LIMIT_HINTS):
            err = (status_resp.get("job") or {}).get("errorMessage", "")
            raise RateLimited(f"codex job {job_id} rate-limited: {str(err)[:200]}")
        err = (status_resp.get("job") or {}).get("errorMessage", "")
        raise RuntimeError(f"codex job {job_id} terminal status: {st} ({str(err)[:200]})")

    # Fetch result. 1.0.2 returns {job, storedJob}; the worker output lives
    # under storedJob.result.rawOutput (with codex.stdout / rendered as fallbacks).
    result = _run_companion(companion, ["result", job_id, "--json"], cwd=cwd)
    stored = result.get("storedJob") or {}
    result_block = stored.get("result") or {}
    final_msg = result_block.get("rawOutput") or ""
    if not final_msg.strip():
        codex_block = result_block.get("codex") or {}
        final_msg = codex_block.get("stdout") or stored.get("rendered") or ""
    if not final_msg.strip():
        raise ValueError("codex result had empty rawOutput / rendered")

    return {"status": "completed", "summary": final_msg, "raw_output": json.dumps(result)}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="codex_call")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_impl = sub.add_parser("implement")
    p_impl.add_argument("--run-id", required=True)
    p_impl.add_argument("--stage-id", required=True)
    p_impl.add_argument("--retry-context-file")

    p_rev = sub.add_parser("review")
    p_rev.add_argument("--run-id", required=True)
    p_rev.add_argument("--stage-id", required=True)
    p_rev.add_argument("--diff-file", required=True)
    p_rev.add_argument("--test-results-file", required=True)
    p_rev.add_argument("--stack", required=True, choices=["python", "typescript", "ios", "general"])

    p_plan = sub.add_parser("review-plan")
    p_plan.add_argument("--run-id", required=True)

    args = parser.parse_args(argv)

    cwd = Path.cwd()
    diff_file = Path(args.diff_file) if getattr(args, "diff_file", None) else None
    retry_context_file = Path(args.retry_context_file) if getattr(args, "retry_context_file", None) else None
    test_results_file = Path(args.test_results_file) if getattr(args, "test_results_file", None) else None
    stack = getattr(args, "stack", None)

    try:
        out = run(
            mode=args.mode,
            run_id=args.run_id,
            stage_id=getattr(args, "stage_id", None),
            cwd=cwd,
            diff_file=diff_file,
            retry_context_file=retry_context_file,
            test_results_file=test_results_file,
            stack=stack,
        )
        print(json.dumps(out))
        return 0
    except RateLimited as exc:
        print(json.dumps({"status": "error", "error_class": "rate_limited", "message": str(exc)}))
        return 2
    except FileNotFoundError as exc:
        print(json.dumps({"status": "error", "error_class": "plugin_missing", "message": str(exc)}))
        return 1
    except TimeoutError as exc:
        print(json.dumps({"status": "error", "error_class": "timeout", "message": str(exc)}))
        return 1
    except ValueError as exc:
        print(json.dumps({"status": "error", "error_class": "parse_fail", "message": str(exc)}))
        return 1
    except Exception as exc:
        print(json.dumps({"status": "error", "error_class": "worker_failed", "message": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
