"""Subprocess wrapper around the openai-codex `codex-companion.mjs` script.

Two CLI modes (mirrors the spec section "Codex shell helper"):

    codex_call.py implement --run-id <id> --stage-id <sid>
    codex_call.py review    --run-id <id> --stage-id <sid> \
                            --diff-file <path> \
                            --test-results-file <path> \
                            --stack <python|typescript|ios|general>

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


def _build_prompt(
    mode: str,
    run_id: str,
    stage_id: str,
    cwd: Path,
    diff_file: Optional[Path],
    test_results_file: Optional[Path],
    stack: Optional[str],
) -> str:
    """Compose the prompt: prefix file + payload."""
    plugin_root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", cwd))

    if mode == "implement":
        prefix_path = plugin_root / "agents" / "prompts" / "codex-implementer.md"
    elif mode == "review":
        prefix_path = plugin_root / "agents" / "prompts" / "codex-reviewer.md"
    else:
        raise ValueError(f"Unknown mode: {mode}")

    prefix = prefix_path.read_text() if prefix_path.exists() else ""

    parts = [prefix, "", f"run-id: {run_id}", f"stage-id: {stage_id}", f"cwd: {cwd}"]

    plan_path = cwd / ".ai" / "runs" / run_id / "plan.md"
    if plan_path.exists():
        parts.extend(["", "plan.md (find your stage):", "---", plan_path.read_text(), "---"])

    if mode == "review":
        if diff_file and diff_file.exists():
            parts.extend(["", "diff:", "---", diff_file.read_text(), "---"])
        if test_results_file and test_results_file.exists():
            parts.extend(["", "test results:", "---", test_results_file.read_text(), "---"])
        if stack:
            checklist_path = plugin_root / "agents" / "references" / f"review-checklist-{stack}.md"
            if checklist_path.exists():
                parts.extend(["", f"stack checklist ({stack}):", "---", checklist_path.read_text(), "---"])

    return "\n".join(parts)


def run(
    *,
    mode: str,
    run_id: str,
    stage_id: str,
    cwd: Path,
    diff_file: Optional[Path],
    test_results_file: Optional[Path],
    stack: Optional[str],
) -> dict:
    """High-level: build prompt, launch task, poll, fetch result."""
    companion = _locate_companion()
    prompt = _build_prompt(mode, run_id, stage_id, cwd, diff_file, test_results_file, stack)

    # Launch background task.
    launch = _run_companion(
        companion,
        ["task", "--background", "--json", "--prompt", prompt, "--cwd", str(cwd)],
        cwd=cwd,
    )
    job_id = launch.get("jobId")
    if not job_id:
        raise RuntimeError(f"task launch returned no jobId: {launch}")

    # Poll status until terminal.
    elapsed = 0
    status_resp: dict = {}
    st: str = ""
    while elapsed < _POLL_MAX_SEC:
        status_resp = _run_companion(companion, ["status", job_id], cwd=cwd)
        st = status_resp.get("status", "")
        if st in ("completed", "failed", "cancelled"):
            break
        if st == "running":
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
            raise RateLimited(f"codex job {job_id} rate-limited: {status_resp.get('error', '')[:200]}")
        raise RuntimeError(f"codex job {job_id} terminal status: {st} ({status_resp.get('error', '')[:200]})")

    # Fetch result.
    result = _run_companion(companion, ["result", job_id], cwd=cwd)
    final_msg = result.get("finalMessage") or ""
    if not final_msg.strip():
        raise ValueError("codex result had empty finalMessage")

    return {"status": "completed", "summary": final_msg, "raw_output": json.dumps(result)}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="codex_call")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_impl = sub.add_parser("implement")
    p_impl.add_argument("--run-id", required=True)
    p_impl.add_argument("--stage-id", required=True)

    p_rev = sub.add_parser("review")
    p_rev.add_argument("--run-id", required=True)
    p_rev.add_argument("--stage-id", required=True)
    p_rev.add_argument("--diff-file", required=True)
    p_rev.add_argument("--test-results-file", required=True)
    p_rev.add_argument("--stack", required=True, choices=["python", "typescript", "ios", "general"])

    args = parser.parse_args(argv)

    cwd = Path.cwd()
    diff_file = Path(args.diff_file) if getattr(args, "diff_file", None) else None
    test_results_file = Path(args.test_results_file) if getattr(args, "test_results_file", None) else None
    stack = getattr(args, "stack", None)

    try:
        out = run(
            mode=args.mode,
            run_id=args.run_id,
            stage_id=args.stage_id,
            cwd=cwd,
            diff_file=diff_file,
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
