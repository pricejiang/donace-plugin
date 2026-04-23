"""Agent dispatch using Claude Agent SDK for agentic loops, Anthropic API for classification.

Each agent gets:
- System prompt loaded from agents/{name}.md (YAML frontmatter stripped)
- Tools determined by the frontmatter `tools` field
- Security hooks: path boundary validation, command blocklist
- EventBus integration via hooks for real-time dashboard streaming
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sdk.events import (
    AgentCompleted,
    AgentFailed,
    AgentMessage,
    AgentStarted,
    AgentTokens,
    AgentToolResult,
    AgentToolUse,
    EventBus,
    HookDenied,
    Stage,
    SubagentCompleted,
    SubagentStarted,
)

try:
    from claude_agent_sdk import (
        query as sdk_query,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        HookMatcher,
    )
    from claude_agent_sdk.types import (
        AssistantMessage,
        PreToolUseHookInput,
        PostToolUseHookInput,
        ResultMessage,
        TextBlock,
    )
    HAS_SDK = True
except ImportError:
    HAS_SDK = False



# ---------------------------------------------------------------------------
# Model ID mapping
# ---------------------------------------------------------------------------

MODEL_MAP = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def _resolve_model(short: str) -> str:
    return MODEL_MAP.get(short, short)


# ---------------------------------------------------------------------------
# Rate-limit detection
# ---------------------------------------------------------------------------
# Claude Code surfaces quota hits via AssistantMessage text right before
# closing the stream with an empty-errors ResultMessage. Without parsing
# those chunks, job_runner sees `agent error: unknown` and treats the
# throttle like a plan failure (counts against 3-strike retry budget,
# trips repeated_stage_failure validation). We want infra throttles to
# pause-and-resume, not block the run.
_RATE_LIMIT_PATTERNS = (
    re.compile(r"hit\s+(?:your\s+)?(?:rate\s+)?limit", re.I),
    re.compile(r"rate[\s-]*limit(?:ed|ing)?(?:\s+exceeded)?", re.I),
    re.compile(r"quota\s+(?:exceeded|exhausted|reached)", re.I),
    re.compile(r"usage\s+(?:cap|limit)\s+(?:reached|exceeded)", re.I),
    re.compile(r"resets?\s+(?:at\s+)?\d", re.I),  # "resets 1am", "resets at 11pm"
)


def _detect_rate_limit(text: str | None) -> str | None:
    """Return the cleaned rate-limit snippet if `text` mentions one, else None.

    Narrow regex set — generic 'limit' words won't match ('size limit',
    'retry limit'). We require a phrase tying 'limit/quota/cap/resets' to
    throttling semantics.
    """
    if not text:
        return None
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    for pattern in _RATE_LIMIT_PATTERNS:
        if pattern.search(cleaned):
            return cleaned
    return None


class RateLimitError(RuntimeError):
    """Raised when a Claude Code harness throttle is detected mid-stream.

    Subclass of RuntimeError so existing `except RuntimeError` paths still
    catch it, but specific enough for job_runner to treat it as INTERRUPTED
    (pause + resume) rather than BLOCKED (counts against retry budget).
    """


# ---------------------------------------------------------------------------
# Plan review: background launch + poll tuning
# ---------------------------------------------------------------------------
# run-phase4-create-fork-a6ca720da361 showed codex genuinely takes 10+ min
# on mid-sized plans (1.3M input tokens, mostly cached). Foreground waits
# with a tight cap threw away real findings. We launch in background and
# poll, capping client-side at 600s. On timeout, codex keeps running and
# team-lead can finalize via cmd_plan_status.
_PLAN_REVIEW_TIMEOUT_S: float = 600.0
_PLAN_REVIEW_POLL_INTERVAL_S: float = 3.0

# Per-stage codex review uses the Haiku wrapper (rate-limit aware) with a
# hard asyncio.wait_for cap. 180s — the original budget — was too tight:
# run-phase5-runA stages 4 and 6 both hit it and dropped their reviews
# silently. Match the plan-review client budget (600s) so larger stage
# diffs still land a verdict. Rate limits propagate as RateLimitError →
# INTERRUPTED in job_runner, so extending the wait doesn't mask throttle.
_PER_STAGE_REVIEW_TIMEOUT_S: int = 600


def _dirty_path_in_scope(path: str, file_scope: list[str] | None) -> bool:
    if file_scope is None:
        return True
    scope = _normalize_file_scope(file_scope) or []
    return any(path == s or path.startswith(s.rstrip("/") + "/") for s in scope)


async def _git_touched_snapshot(cwd: str) -> dict[str, tuple[str, int, int]]:
    """Return git-visible dirty paths with cheap content identity.

    Values are `(porcelain_status, mtime_ns, size)`. The mtime/size pair
    lets timeout reporting distinguish a dirty file that existed before
    an agent ran from one the agent rewrote while keeping the same
    porcelain status.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain", "-uall", "-z",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode != 0:
            return {}

        snapshot: dict[str, tuple[str, int, int]] = {}
        records = stdout.decode(errors="replace").split("\0")
        i = 0
        while i < len(records):
            rec = records[i]
            i += 1
            if len(rec) < 4:
                continue
            status = rec[:2]
            path = rec[3:]
            if status[0] in ("R", "C"):
                i += 1
            full_path = Path(cwd) / path
            try:
                st = full_path.stat()
                snapshot[path] = (status, st.st_mtime_ns, st.st_size)
            except OSError:
                snapshot[path] = (status, 0, 0)
        return snapshot
    except Exception:
        return {}


async def _git_touched_files(
    cwd: str,
    *,
    baseline: dict[str, tuple[str, int, int]] | None = None,
    file_scope: list[str] | None = None,
) -> list[str]:
    """Return dirty paths added/changed since `baseline`. Empty on any error.

    Used by the implementer timeout path so partial writes surface in the
    BLOCKED result without mislabeling pre-existing user edits as work
    done by the hung agent.
    """
    try:
        before = baseline or {}
        after = await _git_touched_snapshot(cwd)
        files: list[str] = [
            path
            for path, identity in after.items()
            if before.get(path) != identity and _dirty_path_in_scope(path, file_scope)
        ]
        return files
    except Exception:
        return []


def _skipped_plan_review(reason: str) -> dict:
    """Default-shape result dict for any early-abort plan review path.

    Missing job_id / thread_id keys intentionally — callers that have them
    add them after this returns. Keeps the 7-field-with-reason pattern
    consistent across every skip site.
    """
    return {
        "status": "skipped",
        "has_major_issues": False,
        "summary": "",
        "findings": [],
        "next_steps": [],
        "output": "",
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# File-scope normalization
# ---------------------------------------------------------------------------
# plan.json stores file paths as markdown code spans: "`apps/web/x.ts`".
# Unstripped, the literal-string comparison in the Write/Edit hook and
# _bash_writes_outside_scope never matches the real relative path — every
# in-scope write gets denied. Stage-2 of run-phase3-reader-24c1c0260e85
# hit this 6+ times on files that were explicitly listed in the plan.
def _normalize_file_scope(scope: list[str] | None) -> list[str] | None:
    """Strip markdown backticks, whitespace, and trailing slashes.

    None passes through (means "no scope restriction"). Empty/all-whitespace
    entries are dropped rather than preserved as a broken match key.
    """
    if scope is None:
        return None
    cleaned: list[str] = []
    for raw in scope:
        if raw is None:
            continue
        s = raw.strip()
        # Peel balanced outer backticks (`apps/x.ts` → apps/x.ts), not inner ones.
        while s.startswith("`") and s.endswith("`") and len(s) >= 2:
            s = s[1:-1].strip()
        s = s.rstrip("/")
        if s:
            cleaned.append(s)
    return cleaned


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object embedded in text."""
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _build_codex_plan_review_prompt(plan_text: str) -> str:
    """Build a structured review prompt for implementation plans."""
    return (
        "Review the implementation plan below before coding starts.\n\n"
        "Only call out issues strong enough to justify revising the plan before implementation. "
        "Focus on missing dependencies, incorrect stage ordering, hidden coupling between stages, "
        "missing verification work, and anything likely to cause rework during the sprint loop.\n\n"
        "Ignore minor wording edits and style nits.\n\n"
        "Return ONLY valid JSON in this exact shape:\n"
        "{\n"
        '  "verdict": "approve" | "needs-attention",\n'
        '  "summary": "short summary",\n'
        '  "findings": [\n'
        "    {\n"
        '      "severity": "high" | "medium" | "low",\n'
        '      "title": "short title",\n'
        '      "body": "why this matters",\n'
        '      "recommendation": "concrete fix"\n'
        "    }\n"
        "  ],\n"
        '  "next_steps": ["optional follow-up"]\n'
        "}\n\n"
        'Use "needs-attention" only when the plan should be revised before implementation. '
        'Use "approve" when remaining comments are optional.\n\n'
        "Implementation plan:\n"
        "```markdown\n"
        f"{plan_text}\n"
        "```"
    )


def _format_plan_review_findings(findings: Any) -> list[str]:
    """Convert structured Codex findings into concise strings."""
    if not isinstance(findings, list):
        return []

    formatted: list[str] = []
    for item in findings:
        if isinstance(item, str):
            text = item.strip()
            if text:
                formatted.append(text)
            continue

        if not isinstance(item, dict):
            text = str(item).strip()
            if text:
                formatted.append(text)
            continue

        severity = str(item.get("severity", "")).strip().upper()
        title = str(item.get("title", "")).strip()
        body = str(item.get("body", "")).strip()
        recommendation = str(item.get("recommendation", "")).strip()

        parts: list[str] = []
        heading = " ".join(part for part in (f"[{severity}]" if severity else "", title) if part).strip()
        if heading:
            parts.append(heading)
        if body:
            parts.append(body)
        if recommendation:
            parts.append(f"Recommended fix: {recommendation}")

        text = " ".join(parts).strip()
        if text:
            formatted.append(text)

    return formatted


# ---------------------------------------------------------------------------
# Security: command blocklist and path validation
# ---------------------------------------------------------------------------

_BLOCKED_COMMANDS = [
    "rm -rf /",
    "rm -rf /*",
    "git push --force",
    "git push -f",
    "git reset --hard",
    "mkfs",
    "dd if=",
    "> /dev/sda",
    ":(){ :|:& };:",
    "prisma migrate reset",
    "prisma db push --force-reset",
    "DROP DATABASE",
    "DROP TABLE",
    "TRUNCATE",
    "DELETE FROM",
]


def _is_blocked_command(command: str) -> str | None:
    """Check if a command matches the blocklist. Returns reason if blocked, None if safe."""
    cmd_stripped = command.strip()
    for blocked in _BLOCKED_COMMANDS:
        if blocked in cmd_stripped:
            return f"blocked: '{blocked}' is not allowed in automated execution"
    return None


# ---------------------------------------------------------------------------
# Per-agent Bash guardrails
# ---------------------------------------------------------------------------
# Every worker agent has a typed tool (Read/Glob/Grep) that beats shelling
# out to `cat/ls/find/grep -r`. The shell forms bring back raw stdout with
# no preview truncation — that's how implementer's #9 fix-loop ballooned
# to 39K output, and how test-engineer's #3 burned 33K exploring tooling.

# Leading commands that have a direct tool equivalent.
_SHELL_READER_ALTERNATIVES = {
    "cat": "Read",
    "less": "Read",
    "more": "Read",
    "head": "Read (use the `limit` parameter for partial reads)",
    "tail": "Read (use `offset` + `limit` for tail-like reads)",
    "ls": "Glob",
    "find": "Glob",
}

# Verification commands the IMPLEMENTER must not run — that work belongs
# to test-engineer / runtime-verifier and happens AFTER implementer returns.
_IMPLEMENTER_VERIFY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bpnpm\s+(?:--filter[=\s]\S+\s+)?(?:run\s+)?(?:test|typecheck|build|lint)(?:\b|:)"),
     "pnpm test/typecheck/build/lint"),
    (re.compile(r"\bnpm\s+(?:run\s+)?(?:test|typecheck|build|lint)\b"),
     "npm test/typecheck/build/lint"),
    (re.compile(r"\byarn\s+(?:run\s+)?(?:test|typecheck|build|lint)\b"),
     "yarn test/typecheck/build/lint"),
    (re.compile(r"\bnpx\s+(?:vitest|jest|tsc|playwright)\b"),
     "npx vitest/jest/tsc/playwright"),
    (re.compile(r"(?:^|[;&|]\s*)(?:vitest|jest|tsc|eslint|prettier)(?:\s|$)"),
     "test runner / typechecker / linter"),
    (re.compile(r"\bcurl\b"), "curl"),
    (re.compile(r"\bwget\b"), "wget"),
    (re.compile(r"\bplaywright\b"), "playwright"),
]


def _bash_forbidden_for_agent(agent_name: str, command: str) -> str | None:
    """Agent-specific Bash guardrails. Returns deny reason or None.

    Applies to worker agents (implementer, test-engineer) where we've
    measured that shell alternatives to typed tools bloat output tokens.
    Other agents (documenter, reviewers, runtime-verifier) pass through.
    """
    cmd = command.strip()
    if not cmd:
        return None

    # Extract the leading command word, skipping env-var assignments like
    # `A=1 cat file`. This is a simple tokeniser, not a full shell parser —
    # it's intentionally blunt: a chained `&& cat` can still slip through,
    # but that's unusual in practice and the prompt rules cover it.
    tokens = cmd.split()
    leading = ""
    for tok in tokens:
        if "=" in tok and not tok.startswith("-"):
            # env-var assignment like FOO=bar; keep looking
            continue
        leading = tok
        break

    # --- Shared: shell readers have typed tool equivalents ---
    if agent_name in ("implementer", "test-engineer"):
        alt = _SHELL_READER_ALTERNATIVES.get(leading)
        if alt:
            return (
                f"`{leading}` via Bash is not allowed for {agent_name} — use the {alt} tool. "
                f"Shell readers bring back raw stdout with no preview truncation, which "
                f"bloats your output context. Pipeline uses like `cmd | {leading}` are "
                f"fine (not blocked here)."
            )
        # `grep -r` / `grep -R` as the leading command
        if leading == "grep" and re.match(r"grep\s+[-\w]*[rR]\b", cmd):
            return (
                f"`grep -r/-R` via Bash is not allowed for {agent_name} — use the Grep tool. "
                f"Pipeline uses like `cmd | grep ...` are fine."
            )

    # --- Implementer-only: no verification (test-engineer's / runtime-verifier's job) ---
    if agent_name == "implementer":
        for pattern, label in _IMPLEMENTER_VERIFY_PATTERNS:
            if pattern.search(cmd):
                return (
                    f"implementer cannot run {label} — that is the verifier's job. "
                    f"Verification runs automatically after you return. If the "
                    f"verifier's failure report is too vague to act on, respond with "
                    f"'NEEDS_CONTEXT: <what's missing>' instead of re-running tests."
                )

    return None


# Paths where test-engineer is allowed to Write/Edit. Everything else is
# denied — source code is implementer's territory. Caught in #3 of
# run-phase1-auth-16a42e9b where test-engineer edited packages/shared/src/
# schemas/auth.ts to add in-source tests instead of writing a proper test
# file, which blurred the source/test boundary.
_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec)/|"      # a test directory segment
    r"\.(?:test|spec)\.[a-zA-Z0-9]+$"          # *.test.* or *.spec.*
)


def _is_test_writable_path(rel_path: str) -> bool:
    """True if rel_path looks like a test file / lives under a test directory."""
    normalized = rel_path.replace("\\", "/")
    return bool(_TEST_PATH_RE.search(normalized))


def _extract_needs_context_line(text: str) -> str | None:
    """Return the message if the agent raised NEEDS_CONTEXT, else None.

    Mirrors the implementer-side helper in job_runner/sprint_loop so the
    test-engineer escape hatch is detected at dispatch time. Checking the
    trailing chunk first because the convention is to emit it as the last
    line of output.
    """
    if not text:
        return None
    for chunk in (text[-2000:], text):
        for line in chunk.splitlines():
            stripped = line.strip()
            if stripped.startswith("NEEDS_CONTEXT:"):
                msg = stripped[len("NEEDS_CONTEXT:"):].strip()
                if msg:
                    return msg
    return None


def _parse_allowed_subagents(tools: list[str]) -> list[str] | None:
    """Extract allowed subagent types from tools list.

    "Agent(worker)" → ["worker"]
    "Agent(worker, researcher)" → ["worker", "researcher"]
    "Agent" (no parens) → None (allow all)
    No "Agent" at all → [] (allow none)
    """
    for tool in tools:
        if tool.startswith("Agent(") and tool.endswith(")"):
            inner = tool[6:-1]
            return [t.strip() for t in inner.split(",") if t.strip()]
        if tool == "Agent":
            return None  # unrestricted
    return []  # Agent not in tools list


def _is_path_outside_cwd(file_path: str, cwd: str) -> bool:
    """Check that file_path is within cwd.

    Relative paths are resolved against cwd (not the orchestrator process cwd).
    """
    try:
        cwd_resolved = Path(cwd).resolve()
        if Path(file_path).is_absolute():
            resolved = Path(file_path).resolve()
        else:
            resolved = (cwd_resolved / file_path).resolve()
        return not (str(resolved).startswith(str(cwd_resolved) + "/") or resolved == cwd_resolved)
    except Exception:
        return True


# Mass-mutating commands that walk directory trees. Banned under file_scope.
_MASS_MUTATORS = (
    "prettier --write",
    "black ",
    "rustfmt ",
    "gofmt -w",
    "cargo fmt",
    "ruff format",
    "ruff check --fix",
    "rubocop -a",
    "rubocop --autocorrect",
)

# Shell redirection: `cmd > file`, `cmd >> file`, `cmd 2> file`, `cmd &> file`.
# Captures the file target, skipping fd duplicates like `2>&1`.
_REDIR_RE = re.compile(r"(?:^|[^0-9&<>])(?:[0-9]|&)?>>?\s*([^\s|;&<>()`]+)")
_SED_PERL_INPLACE_RE = re.compile(r"\b(?:sed|perl)\s+(?:-\S+\s+)*-(?:p)?i\b")

# Shell operators that end a command's argument list when walking tokens.
_SHELL_OPS = frozenset({"|", ";", "&&", "||", "&", "|&", ">", ">>", "<"})


def _blank_quoted_regions(command: str) -> str:
    """Replace inert quoted content with same-length spaces.

    `_REDIR_RE` runs over the raw command and can't distinguish `>` in a
    shell redirect from `>` inside a quoted string (e.g. `node -e "c=>d"`
    where the arrow function matched as redirect target `d`).

    Backticks are command substitutions, not inert quotes. Preserve their
    bodies so nested writes like ``echo `echo hi > /tmp/out` `` still get
    caught by the redirect scanner. Positions are preserved so regex
    offsets still map to the original.

    Shell comments (`#` at BOF or after unquoted whitespace, running to
    the next newline) are blanked too. Without this, a comment containing
    an apostrophe — `# Check if it's rendering...` — flips the quote
    tracker on `'` in `it's`, so every subsequent `>` inside legitimate
    single-quoted tr args is misread as a redirect target.
    """
    comment_blanked = _blank_shell_comments(command)
    out = list(comment_blanked)
    i = 0
    n = len(comment_blanked)

    def blank_quote(start: int, quote: str) -> int:
        j = start + 1
        while j < n:
            if comment_blanked[j] == "\\" and j + 1 < n:
                out[j] = " "
                out[j + 1] = " "
                j += 2
                continue
            if comment_blanked[j] == quote:
                break
            out[j] = " "
            j += 1
        return j + 1

    def scan_backtick_substitution(start: int) -> int:
        j = start + 1
        while j < n:
            if comment_blanked[j] == "\\" and j + 1 < n:
                j += 2
                continue
            if comment_blanked[j] == "`":
                break
            if comment_blanked[j] in ("'", '"'):
                # Quotes inside the nested command are inert for redirect
                # detection, just like quotes in the outer command.
                j = blank_quote(j, comment_blanked[j])
                continue
            j += 1
        return j + 1

    while i < n:
        ch = comment_blanked[i]
        if ch == "\\" and i + 1 < n:
            i += 2  # escaped char, keep both positions untouched
            continue
        if ch == "'":
            i = blank_quote(i, ch)
            continue
        if ch == '"':
            j = i + 1
            while j < n:
                if comment_blanked[j] == "\\" and j + 1 < n:
                    out[j] = " "
                    out[j + 1] = " "
                    j += 2
                    continue
                if comment_blanked[j] == '"':
                    break
                if comment_blanked[j] == "`":
                    j = scan_backtick_substitution(j)
                    continue
                out[j] = " "
                j += 1
            i = j + 1
            continue
        if ch == "`":
            i = scan_backtick_substitution(i)
            continue
        i += 1
    return "".join(out)


_SHELL_WORD_STOP = frozenset(" \t\n|;&<>()`")


def _is_shell_comment_start(command: str, index: int) -> bool:
    """Return True when `#` starts a shell comment outside quotes."""
    if command[index] != "#":
        return False
    if index == 0:
        return True
    # Shell comments start at the beginning of a word. Besides ordinary
    # whitespace/newline, command separators also create a new word, so
    # `;# comment` is a comment even without a space after `;`.
    return command[index - 1] in " \t\n;|&"


def _blank_shell_comments(command: str) -> str:
    """Blank shell comments while preserving positions and quoted content."""
    out = list(command)
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "#":
            if _is_shell_comment_start(command, i):
                while i < n and command[i] != "\n":
                    out[i] = " "
                    i += 1
                continue
        if ch == "'":
            i += 1
            while i < n and command[i] != "'":
                i += 1
            if i < n:
                i += 1
            continue
        if ch == '"':
            i += 1
            while i < n:
                if command[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if command[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == "`":
            i += 1
            while i < n:
                if command[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if command[i] == "`":
                    i += 1
                    break
                i += 1
            continue
        i += 1
    return "".join(out)


def _extract_shell_word(command: str, start: int) -> str:
    """Parse one shell word from `command[start:]`, unquoting as the shell would.

    Companion to `_blank_quoted_regions`: the blanker erases quoted content
    so `_REDIR_RE` doesn't mis-read `>` inside strings, but it also erased
    legitimate quoted redirect targets (`cmd > "apps/x.ts"` → target became
    empty). We find the operator via the blanked text, then re-extract the
    target from the original with quote handling so scope checks work.

    Handles:
    - Leading whitespace (tabs/spaces) skipped
    - Single-quoted segments preserved verbatim
    - Double-quoted segments with `\\"` / `\\\\` / `\\$` / `` \\` `` escapes
    - Backslash escapes outside quotes
    - Adjacent quoted + unquoted concatenation (`"pre"suffix` → `presuffix`)
    - Stops at whitespace or any shell operator byte
    """
    i = start
    n = len(command)
    while i < n and command[i] in " \t":
        i += 1
    if i >= n:
        return ""
    buf: list[str] = []
    while i < n:
        ch = command[i]
        if ch in _SHELL_WORD_STOP:
            break
        if ch == "'":
            i += 1
            while i < n and command[i] != "'":
                buf.append(command[i])
                i += 1
            if i < n:
                i += 1
            continue
        if ch == '"':
            i += 1
            while i < n and command[i] != '"':
                if (
                    command[i] == "\\"
                    and i + 1 < n
                    and command[i + 1] in ('"', "\\", "$", "`")
                ):
                    buf.append(command[i + 1])
                    i += 2
                    continue
                buf.append(command[i])
                i += 1
            if i < n:
                i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(command[i + 1])
            i += 2
            continue
        buf.append(ch)
        i += 1
    return "".join(buf)


def _bash_writes_outside_scope(
    command: str,
    file_scope: list[str],
    cwd: str,
    *,
    allowed_outside_roots: tuple[str, ...] = (),
) -> str | None:
    """Detect bash commands that write files outside `file_scope`.

    Catches the common escape patterns — redirection, sed/perl -i, tee,
    cp/mv destinations, and known mass-mutating formatters. Not a full
    sandbox — unknown write patterns are allowed. Returns a reason string
    if blocked, None if the command is safe (or unrecognized).

    `allowed_outside_roots` is an absolute-path allowlist for agents that
    legitimately need to write outside the project (e.g. runtime-verifier
    stashing temp shell vars in /tmp). Targets under any listed root are
    exempt from both the project-dir boundary AND the file_scope check.
    """
    # Normalize scope once — plan.json uses markdown code spans
    # ("`apps/x.ts`") which the literal-string comparison below won't
    # match against the hook's rel_path ("apps/x.ts").
    file_scope = _normalize_file_scope(file_scope) or []
    command_without_comments = _blank_shell_comments(command)

    # 1. Mass-mutators walk trees — reject outright under file_scope
    for needle in _MASS_MUTATORS:
        if needle in command_without_comments:
            return (
                f"'{needle.strip()}' can modify many files — "
                f"disabled when file_scope is active (scope: {file_scope})"
            )

    write_targets: list[tuple[str, str]] = []  # (target, kind)

    # 2a. Shell redirection — scan the blanked command so JS / Perl / HTML
    # fragments inside -e "..." don't register as redirect operators, but
    # re-extract the target from the ORIGINAL command so quoted paths
    # (`cmd > "apps/x.ts"`) scope-check against their real content instead
    # of the blanked-out placeholder.
    command_for_redir = _blank_quoted_regions(command)
    for m in _REDIR_RE.finditer(command_for_redir):
        matched = m.group(0)
        # Last `>` in the match is the redirect operator; the target starts
        # immediately after (possibly with intervening whitespace).
        op_end = m.start(0) + matched.rfind(">") + 1
        target = _extract_shell_word(command, op_end)
        if not target or target.startswith("&") or target.startswith("/dev/"):
            continue
        write_targets.append((target, "redirection"))

    # Tokenize once for tee/sed/perl/cp/mv
    try:
        tokens = shlex.split(command_without_comments, posix=True)
    except ValueError:
        tokens = []

    # 2b. tee — walk ALL operands after `tee` until shell operator or end.
    # Regex-only capture missed the second+ target (e.g., `tee ok.txt bad.txt`
    # would pass scope check on ok.txt while silently writing bad.txt).
    for i, tok in enumerate(tokens):
        if tok != "tee":
            continue
        j = i + 1
        while j < len(tokens):
            arg = tokens[j]
            if arg in _SHELL_OPS:
                break
            # Skip tee options: -a, -i, --append, etc.
            if arg.startswith("-") and arg != "-":
                j += 1
                continue
            if arg != "-":  # "-" means stdout, not a file
                write_targets.append((arg, "tee"))
            j += 1

    # 2c. sed -i / perl -i / perl -pi — conventionally the last positional is the target.
    # Look for the last non-flag token that looks like a filepath.
    if _SED_PERL_INPLACE_RE.search(command_without_comments):
        for tok in reversed(tokens):
            if tok and not tok.startswith("-") and ("/" in tok or "." in tok):
                write_targets.append((tok, "sed/perl -i"))
                break

    # 2d. cp / mv / install — last positional is destination
    for i, tok in enumerate(tokens):
        if tok in ("cp", "mv", "install"):
            rest = tokens[i + 1:]
            pos = [t for t in rest if not t.startswith("-")]
            if len(pos) >= 2:
                write_targets.append((pos[-1], tok))

    # 3. Validate each target against the scope.
    # Note: Bash does NOT go through the Read/Write/Edit path-boundary
    # check, so we also have to reject writes escaping cwd here.
    cwd_abs = os.path.abspath(cwd)
    allowed_roots = tuple(os.path.abspath(r) for r in allowed_outside_roots)
    for target, kind in write_targets:
        if target.startswith("/dev/"):
            continue
        abs_path = (
            os.path.normpath(target) if os.path.isabs(target)
            else os.path.normpath(os.path.join(cwd_abs, target))
        )
        # Allowlisted outside roots (e.g. /tmp for runtime-verifier) bypass
        # both the project-dir boundary and the file_scope check.
        if any(
            abs_path == root or abs_path.startswith(root.rstrip("/") + "/")
            for root in allowed_roots
        ):
            continue
        # Escape from the project root — Bash would otherwise slip through
        if not (abs_path == cwd_abs or abs_path.startswith(cwd_abs + os.sep)):
            return (
                f"Bash {kind} writes '{abs_path}' outside project directory {cwd_abs}. "
                f"All writes must stay inside the project."
            )
        rel = os.path.relpath(abs_path, cwd_abs)
        in_scope = any(
            rel == s or rel.startswith(s.rstrip("/") + "/")
            for s in file_scope
        )
        if not in_scope:
            return (
                f"Bash {kind} writes '{rel}' outside file_scope {file_scope}. "
                f"This stage must only write files listed in its plan."
            )

    return None


# ---------------------------------------------------------------------------
# Agent markdown loading
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    name: str
    description: str
    tools: list[str]
    model: str
    system_prompt: str
    mcp_servers: list[str] | None = None  # e.g. ["playwright"]


def load_agent_config(agents_dir: str, agent_name: str) -> AgentConfig:
    """Load agent config from agents/{name}.md, parsing YAML frontmatter."""
    path = Path(agents_dir) / f"{agent_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent definition not found: {path}")

    content = path.read_text("utf-8")

    # Parse YAML frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not fm_match:
        return AgentConfig(
            name=agent_name,
            description="",
            tools=["Read", "Bash", "Grep", "Glob"],
            model="sonnet",
            system_prompt=content,
        )

    frontmatter = fm_match.group(1)
    body = content[fm_match.end():]

    # Simple YAML parsing (no pyyaml dependency)
    name = _extract_yaml_str(frontmatter, "name") or agent_name
    description = _extract_yaml_str(frontmatter, "description") or ""
    model = _extract_yaml_str(frontmatter, "model") or "sonnet"
    tools = _extract_yaml_list(frontmatter, "tools") or ["Read", "Bash", "Grep", "Glob"]

    # Parse mcpServers (YAML list with "- item" syntax)
    mcp_servers = None
    mcp_match = re.search(r'^mcpServers:\s*\n((?:\s+-\s+.+\n?)*)', frontmatter, re.MULTILINE)
    if mcp_match:
        mcp_servers = [
            line.strip().lstrip("- ").strip()
            for line in mcp_match.group(1).strip().split("\n")
            if line.strip().startswith("-")
        ]

    return AgentConfig(
        name=name,
        description=description,
        tools=tools,
        model=model,
        system_prompt=body.strip(),
        mcp_servers=mcp_servers,
    )


def _extract_yaml_str(text: str, key: str) -> str | None:
    match = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
    if match:
        return match.group(1).strip().strip('"').strip("'")
    return None


def _extract_yaml_list(text: str, key: str) -> list[str] | None:
    match = re.search(rf'^{key}:\s*\[([^\]]*)\]', text, re.MULTILINE)
    if match:
        items = match.group(1).split(",")
        return [item.strip().strip('"').strip("'") for item in items if item.strip()]
    return None


# ---------------------------------------------------------------------------
# AgentDispatcher
# ---------------------------------------------------------------------------

class AgentDispatcher:
    """Dispatches agent queries using Claude Agent SDK, with EventBus integration."""

    def __init__(
        self,
        agents_dir: str,
        cwd: str,
        bus: EventBus,
        file_scope: list[str] | None = None,
        codex_review_base: str | None = None,
    ) -> None:
        if not HAS_SDK:
            raise RuntimeError(
                "claude-agent-sdk not installed. Run: pip install claude-agent-sdk"
            )
        self.agents_dir = agents_dir
        self.cwd = cwd
        self.bus = bus
        # Strip markdown backticks / trailing slashes once at construction so
        # every downstream string compare (Write/Edit hook, bash redirect
        # checker) gets clean paths. Plan.json stores files as code spans.
        self.file_scope = _normalize_file_scope(file_scope)
        # Explicit codex review base SHA. When set (typically to the pre-stage
        # HEAD captured by cmd_run_job), codex diffs this stage only, not the
        # cumulative merge-base..HEAD range. Left None for verify flows and
        # anywhere else that wants the prior "full diff since main" behavior.
        self.codex_review_base = codex_review_base
        self._agent_configs: dict[str, AgentConfig] = {}

    def _get_config(self, agent_name: str) -> AgentConfig:
        if agent_name not in self._agent_configs:
            self._agent_configs[agent_name] = load_agent_config(self.agents_dir, agent_name)
        return self._agent_configs[agent_name]

    def _make_hooks(self, agent_name: str) -> dict:
        """Build SDK hooks for security checks + EventBus integration."""
        bus = self.bus
        cwd = self.cwd

        async def pre_tool_hook(
            hook_input: PreToolUseHookInput,
            tool_use_id: str | None,
            context: Any,
        ) -> dict:
            tool_name = hook_input["tool_name"]
            tool_input = hook_input.get("tool_input") or {}

            async def deny(reason: str) -> dict:
                """Emit HookDenied (for validate_run analytics) + return block decision."""
                preview: str | None = None
                if isinstance(tool_input, dict):
                    # Compact preview — the full input may be huge (e.g. Write
                    # with a big content block), so cap it.
                    try:
                        preview = json.dumps(tool_input, ensure_ascii=False)[:400]
                    except (TypeError, ValueError):
                        preview = str(tool_input)[:400]
                await bus.emit(HookDenied(
                    agent=agent_name, tool=tool_name,
                    reason=reason, input_preview=preview,
                ))
                return {"decision": "block", "reason": reason}

            # --- Security: command blocklist ---
            if tool_name == "Bash":
                command = tool_input.get("command", "")
                blocked = _is_blocked_command(command)
                if blocked:
                    return await deny(blocked)

                # --- Per-agent Bash guardrails (typed-tool steering + no-verify) ---
                agent_bash_reason = _bash_forbidden_for_agent(agent_name, command)
                if agent_bash_reason:
                    return await deny(agent_bash_reason)

                # --- File scope: detect Bash writes outside the stage's files ---
                # `is not None` (not truthy) so empty list = "no files in scope"
                # = readonly mode where every write is blocked.
                if self.file_scope is not None:
                    # runtime-verifier routinely curls endpoints and stashes
                    # session cookies / response bodies in /tmp. Those writes
                    # never touch the project tree, so the file_scope check
                    # shouldn't apply to them.
                    allowed_outside: tuple[str, ...] = ()
                    if agent_name == "runtime-verifier":
                        allowed_outside = ("/tmp",)
                    scope_reason = _bash_writes_outside_scope(
                        command, self.file_scope, cwd,
                        allowed_outside_roots=allowed_outside,
                    )
                    if scope_reason:
                        return await deny(scope_reason)

            # --- Security: path boundary check ---
            if tool_name in ("Read", "Write", "Edit"):
                file_path = tool_input.get("file_path", "")
                if file_path and _is_path_outside_cwd(file_path, cwd):
                    return await deny(f"path {file_path} is outside project directory {cwd}")

            # --- File scope: restrict Write/Edit ---
            if tool_name in ("Write", "Edit"):
                file_path = tool_input.get("file_path", "")
                if file_path:
                    # Normalize to relative path against cwd for comparison
                    if os.path.isabs(file_path):
                        rel_path = os.path.relpath(file_path, cwd)
                    else:
                        abs_path = os.path.normpath(os.path.join(cwd, file_path))
                        rel_path = os.path.relpath(abs_path, cwd)

                    # test-engineer: only test-looking paths are writable.
                    # No stage.files exemption — normal stages list source
                    # files there, and allowing those would re-open the #3
                    # boundary violation (test-engineer editing
                    # packages/shared/src/schemas/auth.ts) that this guard
                    # was added to prevent. If the plan legitimately asks
                    # test-engineer to edit a non-standard test path,
                    # expand _TEST_PATH_RE instead of punching a hole here.
                    if agent_name == "test-engineer":
                        if not _is_test_writable_path(rel_path):
                            return await deny(
                                f"test-engineer cannot write '{rel_path}' — "
                                f"only test files (under tests/, __tests__/, "
                                f"spec/, or *.test.* / *.spec.*) are allowed. "
                                f"Source-code edits are implementer's job."
                            )
                    elif self.file_scope:
                        # Non-test-engineer agents: restrict to stage files.
                        scope_match = any(
                            rel_path == s or rel_path.startswith(s.rstrip("/") + "/")
                            for s in self.file_scope
                        )
                        if not scope_match:
                            return await deny(
                                f"file '{rel_path}' is outside this stage's file scope: {self.file_scope}"
                            )

            # --- Security: restrict subagent types ---
            # Agent(X) in frontmatter only enforces in --agent mode.
            # We enforce it here for SDK-dispatched agents.
            if tool_name == "Agent":
                config = self._get_config(agent_name)
                allowed_subagents = _parse_allowed_subagents(config.tools)
                if allowed_subagents is not None:
                    subagent_type = tool_input.get("subagent_type") or tool_input.get("type") or ""
                    if subagent_type and subagent_type not in allowed_subagents:
                        return await deny(
                            f"agent '{agent_name}' can only spawn {allowed_subagents}, not '{subagent_type}'"
                        )

            # --- EventBus: emit tool use event (full content for Raw Log) ---
            target = tool_input.get("file_path") or tool_input.get("command", "")
            await bus.emit(AgentToolUse(
                agent=agent_name,
                tool=tool_name,
                target=str(target) if target else None,
                input_preview=json.dumps(tool_input, ensure_ascii=False) if isinstance(tool_input, dict) else str(tool_input),
            ))
            return {}

        async def post_tool_hook(
            hook_input: PostToolUseHookInput,
            tool_use_id: str | None,
            context: Any,
        ) -> dict:
            response_str = str(hook_input.get("tool_response", ""))
            status = "error" if "[error:" in response_str else "success"
            await bus.emit(AgentToolResult(
                agent=agent_name,
                tool=hook_input["tool_name"],
                status=status,
                output_preview=response_str,
            ))
            return {}

        async def subagent_start_hook(
            hook_input: Any,
            tool_use_id: str | None,
            context: Any,
        ) -> dict:
            agent_type = hook_input.get("agent_type", "") if isinstance(hook_input, dict) else getattr(hook_input, "agent_type", "")
            agent_id = hook_input.get("agent_id", "") if isinstance(hook_input, dict) else getattr(hook_input, "agent_id", "")
            await bus.emit(SubagentStarted(
                parent_agent=agent_name,
                subagent_type=agent_type,
                subagent_id=agent_id,
            ))
            return {}

        async def subagent_stop_hook(
            hook_input: Any,
            tool_use_id: str | None,
            context: Any,
        ) -> dict:
            agent_type = hook_input.get("agent_type", "") if isinstance(hook_input, dict) else getattr(hook_input, "agent_type", "")
            agent_id = hook_input.get("agent_id", "") if isinstance(hook_input, dict) else getattr(hook_input, "agent_id", "")
            transcript_path = hook_input.get("agent_transcript_path", "") if isinstance(hook_input, dict) else getattr(hook_input, "agent_transcript_path", "")
            await bus.emit(SubagentCompleted(
                parent_agent=agent_name,
                subagent_type=agent_type,
                subagent_id=agent_id,
                transcript_path=transcript_path,
            ))
            # Phase 2: backfill tool events from transcript
            if transcript_path:
                await self._backfill_from_transcript(agent_name, agent_type, transcript_path)
            return {}

        return {
            "PreToolUse": [HookMatcher(matcher=".*", hooks=[pre_tool_hook])],
            "PostToolUse": [HookMatcher(matcher=".*", hooks=[post_tool_hook])],
            "SubagentStart": [HookMatcher(matcher=".*", hooks=[subagent_start_hook])],
            "SubagentStop": [HookMatcher(matcher=".*", hooks=[subagent_stop_hook])],
        }

    async def _backfill_from_transcript(self, parent_agent: str, subagent_type: str, transcript_path: str) -> None:
        """Read a subagent's transcript and emit tool_use events for dashboard visibility."""
        try:
            content = await asyncio.to_thread(Path(transcript_path).read_text, "utf-8")
            # Transcript is JSONL — one JSON object per line
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Look for tool_use entries (assistant messages with tool calls)
                if entry.get("type") == "assistant":
                    for block in entry.get("message", {}).get("content", []):
                        if block.get("type") == "tool_use":
                            tool_name = block.get("name", "")
                            tool_input = block.get("input", {})
                            target = tool_input.get("file_path") or tool_input.get("command", "")
                            await self.bus.emit(AgentToolUse(
                                agent=f"{parent_agent}/{subagent_type}",
                                tool=tool_name,
                                target=str(target)[:200] if target else None,
                                input_preview=json.dumps(tool_input, ensure_ascii=False)[:300] if isinstance(tool_input, dict) else None,
                            ))
        except FileNotFoundError:
            pass  # transcript may not exist if subagent was cancelled
        except Exception:
            pass  # best-effort backfill, don't crash on parse errors

    # Per-agent timeout (seconds) and max turns.
    AGENT_TIMEOUT: dict[str, int] = {
        # Planner writes .ai/runs/<id>/plan.md. It invokes the
        # superpowers:writing-plans skill for non-trivial tasks, reads
        # a handful of files for context, and produces a structured
        # plan. 600s covers the skill invocation plus small context
        # gathering; longer plans should still fit comfortably.
        "planner": 600,
        "implementer": 900,
        "test-engineer": 600,
        "runtime-verifier": 900,
        "typescript-reviewer": 300,
        "ios-reviewer": 300,
        # Documenter touches 5-7 files (README, CLAUDE.md, CHANGELOG,
        # plan.md status, session log). 300s was too tight — observed runs
        # timed out mid-way through knowledge cards. cmd_document splits
        # core docs and cards into two phases; each gets the full budget.
        "documenter": 600,
    }
    DEFAULT_TIMEOUT = 300  # 5 minutes

    # No max_turns limit — timeout is the safety valve.
    # Subscription plan doesn't charge per token, so turns are not a cost concern.

    # Token budget hint per agent (injected into prompt). Not enforced — just guidance.
    AGENT_TOKEN_BUDGET: dict[str, str] = {
        "test-engineer": "~8,000 output tokens",
        "runtime-verifier": "~5,000 output tokens",
        "documenter": "~5,000 output tokens",
    }

    async def query(
        self, agent: str, prompt: str, model: str = "sonnet",
        tools: list[str] | None = None, role: str | None = None, **_: Any,
    ) -> str:
        """Run an agent query using Claude Agent SDK.

        Uses ClaudeSDKClient (not sdk_query) so we can call disconnect()
        on timeout to kill the underlying CLI process and its subagents.

        Args:
            agent: Agent name (matches agents/{name}.md)
            prompt: User prompt to send
            model: Model shortname override (opus/sonnet/haiku)
            tools: Override the agent's declared tool list. Use this to
                restrict tools further (e.g. strip Write/Edit for read-only
                passes). None → use the agent's declared tools.

        Returns:
            The agent's final text response.

        Raises:
            RuntimeError: on agent error or timeout.
        """
        config = self._get_config(agent)
        model_id = _resolve_model(model or config.model)
        timeout = self.AGENT_TIMEOUT.get(agent, self.DEFAULT_TIMEOUT)

        # Inject token budget hint into prompt (not enforced, just guidance)
        budget = self.AGENT_TOKEN_BUDGET.get(agent)
        if budget:
            prompt = f"TOKEN BUDGET: {budget}. Focus on the specific files listed below.\n\n{prompt}"

        allowed_tools = (tools if tools is not None else config.tools) + ["TodoWrite"]
        # MCP servers are inherited from the user's CLI plugin config. In
        # bypassPermissions mode, any MCP tool the CLI knows about becomes
        # callable unless explicitly disallowed. Block MCP tools the agent
        # didn't opt into by listing them in its `tools:` frontmatter.
        disallowed_tools: list[str] = []
        MCP_OPT_IN_PREFIXES = ("mcp__plugin_playwright",)
        for prefix in MCP_OPT_IN_PREFIXES:
            if not any(t.startswith(prefix) for t in allowed_tools):
                disallowed_tools.append(f"{prefix}*")
        opts: dict[str, Any] = {
            "system_prompt": config.system_prompt,
            "cwd": self.cwd,
            "allowed_tools": allowed_tools,
            "disallowed_tools": disallowed_tools,
            "permission_mode": "bypassPermissions",
            "model": model_id,
            "hooks": self._make_hooks(agent),
        }

        options = ClaudeAgentOptions(**opts)

        # Emit lifecycle events around every dispatch so the dashboard
        # sees planner / reviewer / documenter (which go through here
        # directly from cmd_*) — not only the implementer/test-engineer
        # paths that job_runner used to wrap manually. job_runner's
        # redundant emits have been removed in favour of this.
        await self.bus.emit(AgentStarted(agent=agent, model=model_id, role=role))
        t0 = time.time()

        client = ClaudeSDKClient(options=options)
        touched_baseline: dict[str, tuple[str, int, int]] | None = None
        if agent == "implementer":
            touched_baseline = await _git_touched_snapshot(self.cwd)
        try:
            result = await asyncio.wait_for(
                self._run_client(client, agent, prompt, model_id),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            try:
                await client.disconnect()
            except Exception:
                pass
            touched = []
            if agent == "implementer":
                touched = await _git_touched_files(
                    self.cwd,
                    baseline=touched_baseline,
                    file_scope=self.file_scope,
                )
            error_msg = f"agent={agent} timed out after {timeout}s"
            if touched:
                # Cap the list — porcelain output can be long if a hung
                # implementer touched many files; the first handful is
                # the actionable signal for team-lead / humans.
                preview = ", ".join(touched[:10])
                if len(touched) > 10:
                    preview += f", … +{len(touched) - 10} more"
                error_msg += f" (partial writes: {preview})"
            await self.bus.emit(AgentFailed(agent=agent, error=error_msg))
            raise RuntimeError(error_msg)
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            await self.bus.emit(AgentFailed(agent=agent, error=str(exc)))
            raise
        else:
            await self.bus.emit(AgentCompleted(
                agent=agent, duration_s=round(time.time() - t0, 1),
            ))
            return result

    async def _run_client(self, client: Any, agent: str, prompt: str, model_id: str) -> str:
        """Run an agent via ClaudeSDKClient. Called within wait_for timeout."""
        await client.connect(prompt=prompt)

        final_text = ""
        # Track the most recent assistant text so we can inspect it when
        # the stream closes with an error — the Claude Code harness
        # delivers rate-limit notices as an AssistantMessage first, then
        # an empty-errors ResultMessage, so the error itself is useless
        # without the preceding chunk.
        last_assistant_text = ""
        try:
            async for message in client.receive_messages():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            last_assistant_text = block.text
                            await self.bus.emit(AgentMessage(
                                agent=agent,
                                role="assistant",
                                content_preview=block.text,
                            ))

                if isinstance(message, ResultMessage):
                    if getattr(message, "is_error", False):
                        errors = getattr(message, "errors", []) or []
                        rate_msg = _detect_rate_limit(last_assistant_text)
                        if rate_msg:
                            raise RateLimitError(rate_msg)
                        raise RuntimeError(f"agent error: {'; '.join(errors) if errors else 'unknown'}")
                    final_text = getattr(message, "result", "") or ""
                    usage = getattr(message, "usage", None)
                    if usage:
                        def _u(key: str) -> int:
                            if isinstance(usage, dict):
                                return usage.get(key, 0) or 0
                            return getattr(usage, key, 0) or 0
                        await self.bus.emit(AgentTokens(
                            agent=agent,
                            input_tokens=_u("input_tokens"),
                            output_tokens=_u("output_tokens"),
                            cache_creation_input_tokens=_u("cache_creation_input_tokens"),
                            cache_read_input_tokens=_u("cache_read_input_tokens"),
                        ))
                    break  # ResultMessage = agent done, stop listening
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"agent={agent} model={model_id}: {exc}") from exc
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        return final_text

    # ------------------------------------------------------------------
    # Test engineer dispatch
    # ------------------------------------------------------------------

    async def _get_changed_files(self) -> str:
        """Get list of files changed since last commit (staged + unstaged + untracked)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--name-only", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            diff_files = stdout.decode().strip()
            # Also get untracked files
            proc2 = await asyncio.create_subprocess_exec(
                "git", "ls-files", "--others", "--exclude-standard",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
            stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=10)
            untracked = stdout2.decode().strip()
            all_files = "\n".join(filter(None, [diff_files, untracked]))
            return all_files or "(no changed files detected)"
        except Exception:
            return "(unable to detect changed files)"

    async def run_test_engineer(self, stage: Stage, readonly: bool = False) -> dict:
        """Dispatch test-engineer agent and parse structured output.

        Args:
            stage: Stage to test.
            readonly: If True, the agent may only run existing tests — it cannot
                write new test files. Used by `verify` to avoid mutating the
                working tree during a supposedly read-only check.
        """
        changed_files = await self._get_changed_files()
        # Check if test files already exist in the changed files
        test_files = [f for f in changed_files.splitlines() if 'test' in f.lower() or 'spec' in f.lower()]

        if readonly:
            prompt = (
                f"Run the existing tests for this scope: {stage.name}\n\n"
                f"Files changed:\n```\n{changed_files}\n```\n\n"
                f"READ-ONLY MODE: Do NOT write or edit any files. Only run "
                f"existing tests and report the result. If coverage is "
                f"insufficient, note that in your output — do not create test files.\n\n"
                f"After running the tests, output a summary line in this exact format:\n"
                f"TEST_SUMMARY: passed=N failed=N\n\n"
                f"This summary must reflect the actual test run results."
            )
        elif test_files:
            prompt = (
                f"Run the existing tests for this stage: {stage.name}\n\n"
                f"Files changed by the implementer:\n```\n{changed_files}\n```\n\n"
                f"Test files already written by implementer:\n```\n{chr(10).join(test_files)}\n```\n\n"
                f"Run the test suite. If existing tests cover the changes well, do NOT write new tests. "
                f"Only write additional tests if you find significant gaps in coverage "
                f"(e.g., no error case tests, no edge case tests).\n\n"
                f"After running the tests, output a summary line in this exact format:\n"
                f"TEST_SUMMARY: passed=N failed=N\n\n"
                f"This summary must reflect the actual test run results."
            )
        else:
            prompt = (
                f"Write and run tests for this stage: {stage.name}\n\n"
                f"Files changed by the implementer:\n```\n{changed_files}\n```\n\n"
                f"No test files were written by the implementer. Write tests and run them.\n\n"
                f"After running the tests, output a summary line in this exact format:\n"
                f"TEST_SUMMARY: passed=N failed=N\n\n"
                f"This summary must reflect the actual test run results."
            )

        # In readonly mode, strip Write/Edit from the test-engineer's tool list
        readonly_tools = ["Read", "Bash", "Grep", "Glob"] if readonly else None
        response = await self.query(
            agent="test-engineer", prompt=prompt, model="sonnet", tools=readonly_tools,
        )

        # NEEDS_CONTEXT escape hatch: if test-engineer couldn't run tests
        # (missing test command, opaque tooling failure, etc.) it emits a
        # line like 'NEEDS_CONTEXT: ...' and stops. Without this check the
        # missing TEST_SUMMARY would fall through as passed=0 failed=0,
        # which _collect_failures reads as a clean verifier result, and
        # the job would incorrectly PASS.
        nc_msg = _extract_needs_context_line(response)
        if nc_msg:
            return {
                "status": "error",
                "passed": 0,
                "failed": 0,
                "error": f"NEEDS_CONTEXT: {nc_msg}",
                "output": response[-2000:] if len(response) > 2000 else response,
            }

        # Parse structured output
        passed = 0
        failed = 0
        match = re.search(r"TEST_SUMMARY:\s*passed=(\d+)\s+failed=(\d+)", response)
        if match:
            passed = int(match.group(1))
            failed = int(match.group(2))
        else:
            # Try to infer from common test output patterns
            pass_match = re.search(r"(\d+)\s+(?:passed|passing)", response)
            fail_match = re.search(r"(\d+)\s+(?:failed|failing)", response)
            if pass_match:
                passed = int(pass_match.group(1))
            if fail_match:
                failed = int(fail_match.group(1))

        return {
            "passed": passed,
            "failed": failed,
            "output": response[-2000:] if len(response) > 2000 else response,
        }

    # ------------------------------------------------------------------
    # Codex review (via Agent SDK + codex plugin)
    # ------------------------------------------------------------------

    async def run_codex_review(self) -> dict:
        """Run codex review via Agent SDK, using the codex companion script.

        Wraps the inner work with AgentStarted / AgentCompleted so the
        dashboard surfaces activity — same reason as run_codex_plan_review.
        The underlying `_run_codex_command` uses `sdk_query` directly,
        bypassing `self.query`, so lifecycle emits don't happen automatically.
        """
        await self.bus.emit(AgentStarted(agent="codex-review", model="haiku"))
        t0 = time.time()
        result: dict = {"status": "skipped", "has_issues": False}
        try:
            result = await self._run_codex_review_inner()
            return result
        finally:
            await self.bus.emit(AgentCompleted(
                agent="codex-review",
                duration_s=round(time.time() - t0, 1),
                result_summary=f"status={result.get('status', 'unknown')}",
            ))

    async def _run_codex_review_inner(self) -> dict:
        """Actual codex review body — lifecycle emits are in the public wrapper."""
        codex_plugin_root, companion_script, reason = self._resolve_codex_companion()
        if not codex_plugin_root or not companion_script:
            return {
                "status": "skipped",
                "has_issues": False,
                "output": "",
                "reason": reason or "codex plugin not found",
            }

        # Determine base ref for diff. If the caller pinned a base
        # (cmd_run_job does this with the pre-stage SHA), use it verbatim
        # so review sees only this stage's diff. Otherwise fall back to
        # merge-base(HEAD, origin/main) so verify-style calls still see
        # the full run diff.
        base = self.codex_review_base or "HEAD~1"
        if not self.codex_review_base:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "merge-base", "HEAD", "origin/main",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.cwd,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                if proc.returncode == 0:
                    base = stdout.decode().strip()
            except (asyncio.TimeoutError, Exception):
                pass  # fall back to HEAD~1

        cmd = f"node {shlex.quote(str(companion_script))} review --wait --base {shlex.quote(base)}"

        try:
            output = await asyncio.wait_for(
                self._run_codex_command(cmd, codex_plugin_root),
                timeout=_PER_STAGE_REVIEW_TIMEOUT_S,
            )
            has_issues = bool(output.strip()) and any(
                marker in output for marker in ("[P0]", "[P1]", "[P2]", "[CRITICAL]", "[WARNING]")
            )
            return {
                "status": "completed",
                "has_issues": has_issues,
                "output": output,
            }
        except asyncio.TimeoutError:
            return {
                "status": "skipped",
                "has_issues": False,
                "output": "",
                "reason": f"codex review timed out after {_PER_STAGE_REVIEW_TIMEOUT_S} seconds",
            }
        except RateLimitError:
            raise
        except Exception as e:
            return {
                "status": "skipped",
                "has_issues": False,
                "output": "",
                "reason": f"codex review failed: {e}",
            }

    async def run_codex_plan_review(
        self,
        plan_text: str,
        *,
        resume_last: bool = False,
        resume_thread_id: str | None = None,
    ) -> dict:
        """Run Codex against a staged plan and return structured findings.

        Wraps the inner work with AgentStarted/AgentCompleted emits so the
        dashboard shows activity during this otherwise-silent phase. The
        underlying codex call goes through `sdk_query` (not `self.query`),
        so none of the tool-level events would otherwise surface — the
        run just appears frozen until codex returns minutes later.

        `resume_last=True` preserves the legacy "resume whichever task thread
        Codex thinks is latest" behavior.

        `resume_thread_id` is stricter: we only pass `--resume-last` when the
        companion's current resumable-task candidate matches that saved thread
        id. If some unrelated Codex task ran more recently, we fall back to a
        fresh plan review instead of resuming the wrong conversation.
        """
        await self.bus.emit(AgentStarted(agent="codex-plan-review", model="haiku"))
        t0 = time.time()
        result: dict = {"status": "skipped", "has_major_issues": False}
        try:
            result = await self._run_codex_plan_review_inner(
                plan_text,
                resume_last=resume_last,
                resume_thread_id=resume_thread_id,
            )
            return result
        finally:
            summary_parts: list[str] = [f"status={result.get('status', 'unknown')}"]
            if result.get("has_major_issues"):
                summary_parts.append("verdict=needs-attention")
            await self.bus.emit(AgentCompleted(
                agent="codex-plan-review",
                duration_s=round(time.time() - t0, 1),
                result_summary=" ".join(summary_parts),
            ))

    async def _run_codex_plan_review_inner(
        self,
        plan_text: str,
        *,
        resume_last: bool = False,
        resume_thread_id: str | None = None,
    ) -> dict:
        """Launch codex plan review in background, poll until done or timeout.

        run-phase4-create-fork-a6ca720da361 showed codex genuinely needs
        10+ minutes on a mid-sized plan (1.3M input tokens, most cached).
        Foreground mode with a 240s cap threw away real findings. Now we:

        1. Launch via `task --background --json` — returns jobId in <1s
        2. Poll `status <job-id> --json` every few seconds until terminal
           state or _PLAN_REVIEW_TIMEOUT_S hits
        3. On client timeout: return status="running" with job_id so
           team-lead's cmd_plan_status can finalize later. DON'T cancel
           codex — its thread keeps advancing, findings aren't lost.
        """
        codex_plugin_root, companion_script, reason = self._resolve_codex_companion()
        if not codex_plugin_root or not companion_script:
            return _skipped_plan_review(reason or "codex plugin not found")

        prompt_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".prompt.txt",
                prefix=".codex-plan-review-",
                dir=self.cwd,
                delete=False,
            ) as handle:
                handle.write(_build_codex_plan_review_prompt(plan_text))
                prompt_path = handle.name

            should_resume = False
            if resume_thread_id:
                candidate_thread_id = await self._codex_task_resume_candidate_thread_id(companion_script)
                should_resume = candidate_thread_id == resume_thread_id
            elif resume_last:
                should_resume = True

            task_args = [
                "--json",
                "--background",
                "--cwd", self.cwd,
                "--prompt-file", prompt_path,
            ]
            if should_resume:
                task_args.append("--resume-last")

            launch = await self._run_codex_json_subcommand(
                companion_script, "task", task_args, timeout_s=30.0,
            )
            if not launch or not launch.get("jobId"):
                return _skipped_plan_review("codex background launch failed")

            job_id = str(launch["jobId"])
            launch_thread_id = launch.get("threadId")

            job, terminal = await self._poll_codex_job(companion_script, job_id)
            job_state = job.get("status") if job else None
            thread_id = (job.get("threadId") if job else None) or launch_thread_id

            if not terminal:
                # Client cap hit — codex still running. Persist state,
                # don't cancel. team-lead's cmd_plan_status recovers.
                return {
                    "status": "running",
                    "has_major_issues": False,
                    "summary": "",
                    "findings": [],
                    "next_steps": [],
                    "output": "",
                    "reason": f"codex plan review still running after {int(_PLAN_REVIEW_TIMEOUT_S)} seconds",
                    "job_id": job_id,
                    "thread_id": str(thread_id) if thread_id else None,
                }

            if job_state in ("failed", "cancelled"):
                err = (job.get("error") if job else None) or (job.get("failureMessage") if job else None) or ""
                return {
                    "status": "skipped",
                    "has_major_issues": False,
                    "summary": "",
                    "findings": [],
                    "next_steps": [],
                    "output": "",
                    "reason": f"codex plan review {job_state}" + (f": {err}" if err else ""),
                    "job_id": job_id,
                    "thread_id": str(thread_id) if thread_id else None,
                }

            return await self._parse_completed_plan_review(companion_script, job_id, thread_id)
        except RateLimitError:
            raise
        except Exception as exc:
            return _skipped_plan_review(f"codex plan review failed: {exc}")
        finally:
            if prompt_path:
                try:
                    Path(prompt_path).unlink(missing_ok=True)
                except Exception:
                    pass

    async def _poll_codex_job(
        self, companion_script: Path, job_id: str,
    ) -> tuple[dict | None, bool]:
        """Poll status until terminal or _PLAN_REVIEW_TIMEOUT_S.

        Returns (last job dict, terminal bool). terminal=False means we
        timed out while codex was still running.
        """
        deadline = time.monotonic() + _PLAN_REVIEW_TIMEOUT_S
        last_job: dict | None = None
        while time.monotonic() < deadline:
            status_payload = await self._run_codex_json_subcommand(
                companion_script, "status",
                [job_id, "--json", "--cwd", self.cwd],
                timeout_s=10.0,
            )
            if status_payload is not None:
                last_job = status_payload.get("job") or {}
                state = last_job.get("status")
                if state in ("completed", "failed", "cancelled"):
                    return last_job, True
            await asyncio.sleep(_PLAN_REVIEW_POLL_INTERVAL_S)
        return last_job, False

    async def _parse_completed_plan_review(
        self, companion_script: Path, job_id: str, thread_id: str | None,
    ) -> dict:
        """Fetch `result <job-id>`, parse rawOutput JSON, shape result dict.

        Shared by the inline-wait path (_run_codex_plan_review_inner) and
        the async finalize path (fetch_codex_plan_review_result used by
        team-lead's cmd_plan_status).
        """
        result_payload = await self._run_codex_json_subcommand(
            companion_script, "result",
            [job_id, "--json", "--cwd", self.cwd],
            timeout_s=10.0,
        )
        thread_id_str = str(thread_id) if thread_id else None

        if not result_payload:
            return {
                "status": "skipped",
                "has_major_issues": False,
                "summary": "",
                "findings": [],
                "next_steps": [],
                "output": "",
                "reason": "codex plan review completed but result fetch failed",
                "job_id": job_id,
                "thread_id": thread_id_str,
            }

        stored_job = result_payload.get("storedJob") or {}
        stored_result = stored_job.get("result") or {}
        raw_output = str(
            stored_result.get("finalMessage")
            or stored_result.get("rawOutput")
            or ""
        )

        parsed = _extract_json_object(raw_output)
        if not parsed:
            return {
                "status": "skipped",
                "has_major_issues": False,
                "summary": "",
                "findings": [],
                "next_steps": [],
                "output": raw_output,
                "reason": "codex plan review did not return structured JSON",
                "job_id": job_id,
                "thread_id": thread_id_str,
            }

        verdict = str(parsed.get("verdict", "")).strip().lower()
        summary = str(parsed.get("summary", "")).strip()
        findings = _format_plan_review_findings(parsed.get("findings", []))
        next_steps = parsed.get("next_steps", [])

        return {
            "status": "completed",
            "has_major_issues": verdict == "needs-attention",
            "summary": summary,
            "findings": findings,
            "next_steps": next_steps if isinstance(next_steps, list) else [],
            "output": raw_output,
            "job_id": job_id,
            "thread_id": thread_id_str,
        }

    async def fetch_codex_plan_review_result(self, job_id: str) -> dict:
        """Query codex for a previously-launched plan review job.

        Used by cmd_plan_status to finalize a review that timed out
        client-side. Returns the same result shape as run_codex_plan_review:
        status in {completed, running, skipped}, with job_id/thread_id
        for state persistence.
        """
        codex_plugin_root, companion_script, reason = self._resolve_codex_companion()
        if not codex_plugin_root or not companion_script:
            result = _skipped_plan_review(reason or "codex plugin not found")
            result["job_id"] = job_id
            return result

        status_payload = await self._run_codex_json_subcommand(
            companion_script, "status",
            [job_id, "--json", "--cwd", self.cwd],
            timeout_s=10.0,
        )
        if not status_payload:
            result = _skipped_plan_review("codex status lookup failed")
            result["job_id"] = job_id
            return result

        job = status_payload.get("job") or {}
        state = job.get("status")
        thread_id = job.get("threadId")
        thread_id_str = str(thread_id) if thread_id else None

        if state in ("queued", "running"):
            return {
                "status": "running",
                "has_major_issues": False,
                "summary": "",
                "findings": [],
                "next_steps": [],
                "output": "",
                "reason": f"codex plan review still {state}",
                "job_id": job_id,
                "thread_id": thread_id_str,
            }
        if state in ("failed", "cancelled"):
            err = job.get("error") or job.get("failureMessage") or ""
            return {
                "status": "skipped",
                "has_major_issues": False,
                "summary": "",
                "findings": [],
                "next_steps": [],
                "output": "",
                "reason": f"codex plan review {state}" + (f": {err}" if err else ""),
                "job_id": job_id,
                "thread_id": thread_id_str,
            }
        return await self._parse_completed_plan_review(companion_script, job_id, thread_id)

    async def _run_codex_json_subcommand(
        self,
        companion_script: Path,
        subcommand: str,
        args: list[str],
        *,
        timeout_s: float = 10.0,
    ) -> dict | None:
        """Run `node <companion> <subcommand> <args...>` and parse JSON stdout.

        Single seam for all codex-companion subprocess calls so tests can
        inject fakes without touching asyncio.create_subprocess_exec.
        Returns None on any failure — subprocess error, timeout, non-zero
        exit, or non-JSON output — so callers degrade gracefully.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "node", str(companion_script), subcommand, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except (asyncio.TimeoutError, OSError):
            return None

        if proc.returncode != 0:
            return None

        try:
            return json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    async def _codex_task_resume_candidate_thread_id(self, companion_script: Path) -> str | None:
        """Return codex-companion's current resumable task thread id, if any.

        This mirrors the companion's `task-resume-candidate --json` lookup and
        lets plan review verify that `--resume-last` would continue the same
        saved thread we persisted in plan.json. On any failure, return None and
        let the caller fall back to a fresh review.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "node",
                str(companion_script),
                "task-resume-candidate",
                "--json",
                "--cwd",
                self.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except (asyncio.TimeoutError, OSError):
            return None

        if proc.returncode != 0:
            return None

        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

        candidate = payload.get("candidate")
        if not isinstance(candidate, dict):
            return None
        thread_id = candidate.get("threadId")
        if isinstance(thread_id, str):
            cleaned = thread_id.strip()
            if cleaned:
                return cleaned
        return None

    async def _run_codex_command(self, cmd: str, codex_plugin_root: str) -> str:
        """Execute a codex companion command via Agent SDK and return raw output."""
        prompt = (
            f"Execute this command and return the output verbatim:\n\n"
            f"```\n{cmd}\n```\n\n"
            f"Do not fix any issues. Do not paraphrase or summarize. "
            f"Return the raw command output exactly as-is."
        )

        options = ClaudeAgentOptions(
            system_prompt="You execute commands and return output verbatim. Never fix issues or add commentary.",
            cwd=self.cwd,
            allowed_tools=["Bash"],
            permission_mode="bypassPermissions",
            max_turns=3,
            model=_resolve_model("haiku"),
            env={"CLAUDE_PLUGIN_ROOT": codex_plugin_root},
        )

        all_text: list[str] = []
        last_assistant_text = ""
        async for message in sdk_query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        last_assistant_text = block.text
                        all_text.append(block.text)
            elif isinstance(message, ResultMessage):
                # Check for errors in the result
                if getattr(message, "is_error", False):
                    errors = getattr(message, "errors", []) or []
                    rate_msg = _detect_rate_limit(last_assistant_text)
                    if rate_msg:
                        raise RateLimitError(rate_msg)
                    raise RuntimeError(f"codex agent error: {'; '.join(errors) if errors else 'unknown'}")
                result_text = getattr(message, "result", "") or ""
                if result_text.strip():
                    all_text.append(result_text)

        return max(all_text, key=len) if all_text else ""

    def _resolve_codex_companion(self) -> tuple[str | None, Path | None, str | None]:
        """Resolve the codex plugin root and companion script path."""
        codex_plugin_root = self._find_codex_plugin_root()
        if not codex_plugin_root:
            return None, None, "codex plugin not found"

        companion_script = Path(codex_plugin_root) / "scripts" / "codex-companion.mjs"
        if not companion_script.exists():
            return codex_plugin_root, None, f"codex-companion.mjs not found at {companion_script}"

        return codex_plugin_root, companion_script, None

    def _find_codex_plugin_root(self) -> str | None:
        """Locate the codex plugin root directory."""
        candidates = [
            Path.home() / ".claude" / "plugins" / "cache" / "openai-codex" / "codex",
        ]
        for candidate in candidates:
            if candidate.exists():
                versions = sorted(candidate.iterdir(), reverse=True)
                if versions:
                    return str(versions[0])
        return None

    # ------------------------------------------------------------------
    # Runtime verifier dispatch
    # ------------------------------------------------------------------

    async def run_runtime_verifier(self, stage_name: str, task_context: str = "") -> dict:
        """Dispatch runtime-verifier agent for black-box verification.

        The verifier reads the plan's Success Criteria + Tests for this
        stage from task_context and checks them against the running app.
        """
        prompt_parts: list[str] = []
        if task_context:
            prompt_parts.append(task_context)
        prompt_parts.append(
            f"Verify stage: {stage_name}\n\n"
            f"Look up this stage in the plan above — specifically its Success Criteria "
            f"and Tests sections — and verify each item against the running application. "
            f"Report concrete evidence (HTTP status codes, DB rows, UI state) for each "
            f"criterion.\n\n"
            f"After verification, output a summary line in this exact format:\n"
            f"VERIFICATION_SUMMARY: status=PASS|FAIL score=N/M\n\n"
            f"Where N is the number of criteria passed and M is total must-pass criteria."
        )
        prompt = "\n\n".join(prompt_parts)
        response = await self.query(agent="runtime-verifier", prompt=prompt, model="opus")

        # Parse structured output
        status = "FAIL"
        score = "0/0"
        match = re.search(r"VERIFICATION_SUMMARY:\s*status=(PASS|FAIL)\s+score=(\d+/\d+)", response)
        if match:
            status = match.group(1)
            score = match.group(2)

        return {
            "status": status,
            "score": score,
            "output": response[-2000:] if len(response) > 2000 else response,
        }
