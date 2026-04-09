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
import re
import shutil
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
    from claude_agent_sdk import (
        query as sdk_query,
        ClaudeAgentOptions,
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
]


def _is_blocked_command(command: str) -> str | None:
    """Check if a command matches the blocklist. Returns reason if blocked, None if safe."""
    cmd_stripped = command.strip()
    for blocked in _BLOCKED_COMMANDS:
        if blocked in cmd_stripped:
            return f"blocked: '{blocked}' is not allowed in automated execution"
    return None


def _is_path_outside_cwd(file_path: str, cwd: str) -> bool:
    """Check that file_path is within cwd."""
    try:
        resolved = Path(file_path).resolve()
        cwd_resolved = Path(cwd).resolve()
        return not (str(resolved).startswith(str(cwd_resolved) + "/") or resolved == cwd_resolved)
    except Exception:
        return True


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
    """Dispatches agent queries using Claude Agent SDK, with EventBus integration."""

    def __init__(self, agents_dir: str, cwd: str, bus: EventBus) -> None:
        if not HAS_SDK:
            raise RuntimeError(
                "claude-agent-sdk not installed. Run: pip install claude-agent-sdk"
            )
        self.agents_dir = agents_dir
        self.cwd = cwd
        self.bus = bus
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

            # --- Security: command blocklist ---
            if tool_name == "Bash":
                command = tool_input.get("command", "")
                blocked = _is_blocked_command(command)
                if blocked:
                    return {"decision": "block", "reason": blocked}

            # --- Security: path boundary check ---
            if tool_name in ("Read", "Write", "Edit"):
                file_path = tool_input.get("file_path", "")
                if file_path and _is_path_outside_cwd(file_path, cwd):
                    return {"decision": "block", "reason": f"path {file_path} is outside project directory {cwd}"}

            # --- EventBus: emit tool use event ---
            target = tool_input.get("file_path") or tool_input.get("command", "")
            await bus.emit(AgentToolUse(
                agent=agent_name,
                tool=tool_name,
                target=str(target)[:200] if target else None,
                input_preview=json.dumps(tool_input)[:300] if isinstance(tool_input, dict) else str(tool_input)[:300],
            ))
            return {}

        async def post_tool_hook(
            hook_input: PostToolUseHookInput,
            tool_use_id: str | None,
            context: Any,
        ) -> dict:
            response_str = str(hook_input.get("tool_response", ""))[:300]
            status = "error" if "[error:" in response_str else "success"
            await bus.emit(AgentToolResult(
                agent=agent_name,
                tool=hook_input["tool_name"],
                status=status,
                output_preview=response_str,
            ))
            return {}

        return {
            "PreToolUse": [HookMatcher(matcher=".*", hooks=[pre_tool_hook])],
            "PostToolUse": [HookMatcher(matcher=".*", hooks=[post_tool_hook])],
        }

    async def query(self, agent: str, prompt: str, model: str = "sonnet", **_: Any) -> str:
        """Run an agent query using Claude Agent SDK.

        Args:
            agent: Agent name (matches agents/{name}.md)
            prompt: User prompt to send
            model: Model shortname override (opus/sonnet/haiku)

        Returns:
            The agent's final text response.
        """
        config = self._get_config(agent)
        model_id = _resolve_model(model or config.model)

        options = ClaudeAgentOptions(
            system_prompt=config.system_prompt,
            cwd=self.cwd,
            allowed_tools=config.tools,
            permission_mode="bypassPermissions",
            max_turns=50,
            model=model_id,
            hooks=self._make_hooks(agent),
        )

        final_text = ""
        async for message in sdk_query(prompt=prompt, options=options):
            # Stream text content for EventBus
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        await self.bus.emit(AgentMessage(
                            agent=agent,
                            role="assistant",
                            content_preview=block.text[:500],
                        ))

            # Capture final result
            if isinstance(message, ResultMessage):
                final_text = getattr(message, "result", "") or ""
                # Emit token usage
                usage = getattr(message, "usage", None)
                if usage:
                    await self.bus.emit(AgentTokens(
                        agent=agent,
                        input_tokens=usage.get("input_tokens", 0) if isinstance(usage, dict) else getattr(usage, "input_tokens", 0),
                        output_tokens=usage.get("output_tokens", 0) if isinstance(usage, dict) else getattr(usage, "output_tokens", 0),
                    ))

        return final_text

    # ------------------------------------------------------------------
    # Task classification (lightweight SDK query, no tools needed)
    # ------------------------------------------------------------------

    async def classify_task(self, task: str) -> TaskClass:
        """Classify a task using a lightweight SDK query."""
        classify_prompt = (
            f"Classify this development task. Respond with ONLY a JSON object, no other text:\n\n"
            f"Task: {task}\n\n"
            f'{{"needs_spec": true/false, "needs_plan": true/false, "reason": "..."}}\n\n'
            f"needs_spec: true if this is a new feature/product that needs a spec. "
            f"False for bug fixes, refactors, single-file changes.\n"
            f"needs_plan: true unless this is a trivial single-line fix."
        )

        options = ClaudeAgentOptions(
            system_prompt="You classify development tasks. Respond with only valid JSON.",
            cwd=self.cwd,
            allowed_tools=[],
            permission_mode="bypassPermissions",
            max_turns=1,
            model=_resolve_model("haiku"),
        )

        result_text = ""
        async for message in sdk_query(prompt=classify_prompt, options=options):
            if isinstance(message, ResultMessage):
                result_text = getattr(message, "result", "") or ""

        # Parse JSON from response
        try:
            # Find JSON object in response (in case of extra text)
            json_match = re.search(r'\{[^}]+\}', result_text)
            if json_match:
                data = json.loads(json_match.group())
                return TaskClass(
                    needs_spec=data.get("needs_spec", True),
                    needs_plan=data.get("needs_plan", True),
                    reason=data.get("reason", ""),
                )
        except (json.JSONDecodeError, KeyError):
            pass

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
