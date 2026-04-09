"""Real agent dispatch using Anthropic Messages API with local tool execution.

Each agent gets:
- System prompt loaded from agents/{name}.md (YAML frontmatter stripped)
- Tools determined by the frontmatter `tools` field
- An agentic loop: prompt → model response → tool execution → continue

All tool execution happens locally on the user's filesystem.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sdk.events import (
    AgentMessage,
    AgentTokens,
    AgentToolResult,
    AgentToolUse,
    EventBus,
    Stage,
    TaskClass,
)

try:
    from anthropic import AsyncAnthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False
    AsyncAnthropic = None  # type: ignore


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
# Tool definitions (JSON schemas for the Anthropic API)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: dict[str, dict] = {
    "Bash": {
        "name": "bash",
        "description": "Execute a bash command and return stdout + stderr. Use for running tests, builds, git commands, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command to execute"},
            },
            "required": ["command"],
        },
    },
    "Read": {
        "name": "read",
        "description": "Read a file from the filesystem. Returns content with line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file to read"},
                "offset": {"type": "integer", "description": "Line number to start reading from (0-based)"},
                "limit": {"type": "integer", "description": "Maximum number of lines to read"},
            },
            "required": ["file_path"],
        },
    },
    "Write": {
        "name": "write",
        "description": "Write content to a file, creating it and parent directories if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file to write"},
                "content": {"type": "string", "description": "Content to write to the file"},
            },
            "required": ["file_path", "content"],
        },
    },
    "Edit": {
        "name": "edit",
        "description": "Edit a file by replacing an exact string match with new content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file to edit"},
                "old_string": {"type": "string", "description": "Exact string to find (must be unique in the file)"},
                "new_string": {"type": "string", "description": "Replacement string"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    "Glob": {
        "name": "glob",
        "description": "Find files matching a glob pattern. Returns matching file paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g., '**/*.ts', 'src/**/*.py')"},
                "path": {"type": "string", "description": "Directory to search in. Defaults to cwd."},
            },
            "required": ["pattern"],
        },
    },
    "Grep": {
        "name": "grep",
        "description": "Search file contents using regex. Returns matching lines with file paths and line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Directory or file to search in. Defaults to cwd."},
                "glob": {"type": "string", "description": "File pattern filter (e.g., '*.ts')"},
            },
            "required": ["pattern"],
        },
    },
}

# Reverse map: API tool name → our tool key
_TOOL_NAME_MAP = {schema["name"]: key for key, schema in TOOL_SCHEMAS.items()}

MAX_TOOL_OUTPUT = 30_000  # characters — truncate tool results beyond this

# Commands that should never be executed in an automated context
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
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _is_blocked_command(command: str) -> str | None:
    """Check if a command matches the blocklist. Returns reason if blocked, None if safe."""
    cmd_stripped = command.strip()
    for blocked in _BLOCKED_COMMANDS:
        if blocked in cmd_stripped:
            return f"blocked: '{blocked}' is not allowed in automated execution"
    return None


async def _exec_bash(command: str, cwd: str) -> str:
    blocked = _is_blocked_command(command)
    if blocked:
        return f"[error: {blocked}]"
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        output = stdout.decode("utf-8", errors="replace")
        if len(output) > MAX_TOOL_OUTPUT:
            output = output[:MAX_TOOL_OUTPUT] + f"\n... (truncated, {len(output)} total chars)"
        exit_info = f"\n[exit code: {proc.returncode}]" if proc.returncode != 0 else ""
        return output + exit_info
    except asyncio.TimeoutError:
        return "[error: command timed out after 120 seconds]"
    except Exception as e:
        return f"[error: {e}]"


async def _exec_read(file_path: str, offset: int | None = None, limit: int | None = None, **_: Any) -> str:
    try:
        path = Path(file_path)
        if not path.exists():
            return f"[error: file not found: {file_path}]"
        if path.is_dir():
            return f"[error: {file_path} is a directory, not a file]"
        content = await asyncio.to_thread(path.read_text, "utf-8")
        lines = content.splitlines(keepends=True)
        start = offset or 0
        end = start + limit if limit else len(lines)
        selected = lines[start:end]
        numbered = "".join(f"{i + start + 1}\t{line}" for i, line in enumerate(selected))
        if len(numbered) > MAX_TOOL_OUTPUT:
            numbered = numbered[:MAX_TOOL_OUTPUT] + f"\n... (truncated)"
        return numbered
    except Exception as e:
        return f"[error: {e}]"


async def _exec_write(file_path: str, content: str, **_: Any) -> str:
    try:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, content, "utf-8")
        return f"Successfully wrote {len(content)} bytes to {file_path}"
    except Exception as e:
        return f"[error: {e}]"


async def _exec_edit(file_path: str, old_string: str, new_string: str, **_: Any) -> str:
    try:
        path = Path(file_path)
        if not path.exists():
            return f"[error: file not found: {file_path}]"
        content = await asyncio.to_thread(path.read_text, "utf-8")
        count = content.count(old_string)
        if count == 0:
            return f"[error: old_string not found in {file_path}]"
        if count > 1:
            return f"[error: old_string found {count} times in {file_path} — must be unique]"
        new_content = content.replace(old_string, new_string, 1)
        await asyncio.to_thread(path.write_text, new_content, "utf-8")
        return f"Successfully edited {file_path}"
    except Exception as e:
        return f"[error: {e}]"


async def _exec_glob(pattern: str, path: str | None = None, cwd: str = ".", **_: Any) -> str:
    try:
        root = Path(path or cwd)
        matches = sorted(str(p) for p in root.glob(pattern))
        if not matches:
            return "No files found"
        result = "\n".join(matches[:200])
        if len(matches) > 200:
            result += f"\n... ({len(matches)} total matches, showing first 200)"
        return result
    except Exception as e:
        return f"[error: {e}]"


async def _exec_grep(pattern: str, path: str | None = None, cwd: str = ".", glob_filter: str | None = None, **_: Any) -> str:
    search_path = path or cwd
    # Try ripgrep first, fall back to grep
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--no-heading", "--line-number", "--color=never", "-e", pattern]
        if glob_filter:
            cmd.extend(["--glob", glob_filter])
        cmd.append(search_path)
    else:
        cmd = ["grep", "-rn", "-E", pattern, search_path]
        if glob_filter:
            cmd.extend(["--include", glob_filter])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode("utf-8", errors="replace")
        if not output.strip():
            return "No matches found"
        if len(output) > MAX_TOOL_OUTPUT:
            output = output[:MAX_TOOL_OUTPUT] + "\n... (truncated)"
        return output
    except asyncio.TimeoutError:
        return "[error: grep timed out]"
    except Exception as e:
        return f"[error: {e}]"


TOOL_EXECUTORS = {
    "bash": _exec_bash,
    "read": _exec_read,
    "write": _exec_write,
    "edit": _exec_edit,
    "glob": _exec_glob,
    "grep": _exec_grep,
}


async def execute_tool(tool_name: str, tool_input: dict, cwd: str) -> str:
    """Execute a tool and return the result as a string."""
    executor = TOOL_EXECUTORS.get(tool_name)
    if not executor:
        return f"[error: unknown tool: {tool_name}]"

    if tool_name == "bash":
        return await executor(tool_input.get("command", ""), cwd)
    elif tool_name == "grep":
        return await executor(
            pattern=tool_input.get("pattern", ""),
            path=tool_input.get("path"),
            cwd=cwd,
            glob_filter=tool_input.get("glob"),
        )
    elif tool_name == "glob":
        return await executor(
            pattern=tool_input.get("pattern", ""),
            path=tool_input.get("path"),
            cwd=cwd,
        )
    else:
        return await executor(cwd=cwd, **tool_input)


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

    return AgentConfig(
        name=name,
        description=description,
        tools=tools,
        model=model,
        system_prompt=body.strip(),
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
    """Dispatches agent queries using Anthropic Messages API with local tool execution."""

    def __init__(self, agents_dir: str, cwd: str, bus: EventBus) -> None:
        if not HAS_ANTHROPIC:
            raise RuntimeError(
                "anthropic package not installed. Run: pip install anthropic"
            )
        self.client = AsyncAnthropic()
        self.agents_dir = agents_dir
        self.cwd = cwd
        self.bus = bus
        self._agent_configs: dict[str, AgentConfig] = {}

    def _get_config(self, agent_name: str) -> AgentConfig:
        if agent_name not in self._agent_configs:
            self._agent_configs[agent_name] = load_agent_config(self.agents_dir, agent_name)
        return self._agent_configs[agent_name]

    def _get_tools(self, agent_name: str) -> list[dict]:
        config = self._get_config(agent_name)
        return [TOOL_SCHEMAS[t] for t in config.tools if t in TOOL_SCHEMAS]

    async def query(self, agent: str, prompt: str, model: str = "sonnet", **_: Any) -> str:
        """Run an agentic loop: send prompt, execute tools, return final text response.

        Args:
            agent: Agent name (matches agents/{name}.md)
            prompt: User prompt to send
            model: Model shortname override (opus/sonnet/haiku)

        Returns:
            The agent's final text response.
        """
        config = self._get_config(agent)
        model_id = _resolve_model(model or config.model)
        tools = self._get_tools(agent)

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        total_input_tokens = 0
        total_output_tokens = 0
        max_turns = 50  # safety limit

        # Opus can produce longer outputs (plans, specs); sonnet/haiku stay lower
        max_tokens = 32768 if "opus" in model_id else 16384

        for turn in range(max_turns):
            response = await self.client.messages.create(
                model=model_id,
                system=config.system_prompt,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
            )

            # Track token usage
            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens
            await self.bus.emit(AgentTokens(
                agent=agent,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ))

            # Emit text content as messages
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    await self.bus.emit(AgentMessage(
                        agent=agent,
                        role="assistant",
                        content_preview=block.text[:500],
                    ))

            # If no tool use, we're done
            if response.stop_reason == "end_turn":
                return self._extract_text(response)

            # Process tool calls
            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_name = block.name
                        tool_input = block.input

                        # Emit tool_use event
                        target = tool_input.get("file_path") or tool_input.get("command", "")
                        await self.bus.emit(AgentToolUse(
                            agent=agent,
                            tool=tool_name,
                            target=str(target)[:200] if target else None,
                            input_preview=json.dumps(tool_input)[:300],
                        ))

                        # Execute tool
                        result = await execute_tool(tool_name, tool_input, self.cwd)

                        # Emit tool_result event
                        status = "error" if result.startswith("[error:") else "success"
                        await self.bus.emit(AgentToolResult(
                            agent=agent,
                            tool=tool_name,
                            status=status,
                            output_preview=result[:300],
                        ))

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })

                # Add assistant response + tool results to messages
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
            else:
                # Unexpected stop reason
                return self._extract_text(response)

        # Safety limit reached
        return self._extract_text(response)

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Extract text content from an API response."""
        parts = []
        for block in response.content:
            if block.type == "text":
                parts.append(block.text)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Task classification (uses Haiku, no tool loop needed)
    # ------------------------------------------------------------------

    async def classify_task(self, task: str) -> TaskClass:
        """Classify a task using Haiku structured output via tool_use."""
        response = await self.client.messages.create(
            model=_resolve_model("haiku"),
            system="You classify development tasks. Use the classify tool to return your classification.",
            messages=[{"role": "user", "content": task}],
            tools=[{
                "name": "classify",
                "description": "Classify a development task",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "needs_spec": {
                            "type": "boolean",
                            "description": "True if this is a new feature/product that needs a spec. False for bug fixes, refactors, single-file changes.",
                        },
                        "needs_plan": {
                            "type": "boolean",
                            "description": "True unless this is a trivial single-line fix.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Brief reason for the classification.",
                        },
                    },
                    "required": ["needs_spec", "needs_plan", "reason"],
                },
            }],
            tool_choice={"type": "tool", "name": "classify"},
            max_tokens=256,
        )

        # Extract tool_use result
        for block in response.content:
            if block.type == "tool_use" and block.name == "classify":
                return TaskClass(
                    needs_spec=block.input.get("needs_spec", True),
                    needs_plan=block.input.get("needs_plan", True),
                    reason=block.input.get("reason", ""),
                )

        # Fallback
        return TaskClass(needs_spec=True, needs_plan=True, reason="classification failed, defaulting to full planning")

    # ------------------------------------------------------------------
    # Test engineer dispatch
    # ------------------------------------------------------------------

    async def run_test_engineer(self, stage: Stage) -> dict:
        """Dispatch test-engineer agent and parse structured output."""
        prompt = (
            f"Write and run tests for this stage: {stage.name}\n\n"
            f"After running the tests, output a summary line in this exact format:\n"
            f"TEST_SUMMARY: passed=N failed=N\n\n"
            f"This summary must reflect the actual test run results."
        )
        response = await self.query(agent="test-engineer", prompt=prompt, model="sonnet")

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
    # Codex review (CLI subprocess, not an agent)
    # ------------------------------------------------------------------

    async def run_codex_review(self) -> dict:
        """Run codex CLI review. Retries once on timeout/error per design doc."""
        codex = shutil.which("codex")
        if not codex:
            return {
                "status": "skipped",
                "p1_findings": 0,
                "findings": [],
                "reason": "codex CLI not found in PATH",
            }

        last_error = ""
        for attempt in range(2):  # retry once on failure
            try:
                result = await self._run_codex_review_once(codex)
                if result["status"] != "error" or attempt == 1:
                    return result
                last_error = result.get("reason", "unknown error")
            except asyncio.TimeoutError:
                last_error = "codex review timed out after 300 seconds"
                if attempt == 1:
                    break
            except Exception as e:
                last_error = f"codex review failed: {e}"
                if attempt == 1:
                    break

        return {
            "status": "skipped",
            "p1_findings": 0,
            "findings": [],
            "reason": f"{last_error} (after retry)",
        }

    async def _run_codex_review_once(self, codex: str) -> dict:
        """Single attempt at running codex review."""
        # Get the base branch for diff
        base = "HEAD~1"
        proc = await asyncio.create_subprocess_exec(
            "git", "merge-base", "HEAD", "origin/main",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0:
                base = stdout.decode().strip()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

        # Run codex review
        proc = await asyncio.create_subprocess_exec(
            codex, "review",
            "--base", base,
            "-c", 'model_reasoning_effort="xhigh"',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self.cwd,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise  # propagate to retry logic in run_codex_review
        output = stdout.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            return {
                "status": "error",
                "p1_findings": 0,
                "findings": [],
                "reason": f"codex exited with code {proc.returncode}: {output[:500]}",
            }

        # Parse codex output for findings
        findings = []
        p1_count = 0
        for line in output.splitlines():
            line_stripped = line.strip()
            if line_stripped.startswith("[P1]") or line_stripped.startswith("[CRITICAL]"):
                findings.append(line_stripped)
                p1_count += 1
            elif line_stripped.startswith("[P2]") or line_stripped.startswith("[WARNING]"):
                findings.append(line_stripped)

        return {
            "status": "completed",
            "p1_findings": p1_count,
            "findings": findings,
            "output": output[-2000:] if len(output) > 2000 else output,
        }

    # ------------------------------------------------------------------
    # Runtime evaluator dispatch
    # ------------------------------------------------------------------

    async def run_runtime_evaluator(self, contract: str) -> dict:
        """Dispatch runtime-evaluator agent in verification mode."""
        prompt = (
            f"Verify the following sprint contract against the running application:\n\n"
            f"{contract}\n\n"
            f"After verification, output a summary line in this exact format:\n"
            f"VERIFICATION_SUMMARY: status=PASS|FAIL score=N/M\n\n"
            f"Where N is the number of criteria passed and M is total must-pass criteria."
        )
        response = await self.query(agent="runtime-evaluator", prompt=prompt, model="opus")

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
