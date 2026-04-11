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
import shlex
import tempfile
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
    SubagentCompleted,
    SubagentStarted,
    TaskClass,
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
]


def _is_blocked_command(command: str) -> str | None:
    """Check if a command matches the blocklist. Returns reason if blocked, None if safe."""
    cmd_stripped = command.strip()
    for blocked in _BLOCKED_COMMANDS:
        if blocked in cmd_stripped:
            return f"blocked: '{blocked}' is not allowed in automated execution"
    return None


def _parse_allowed_subagents(tools: list[str]) -> list[str] | None:
    """Extract allowed subagent types from tools list.

    "Agent(sub-implementer)" → ["sub-implementer"]
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

            # --- Security: restrict subagent types ---
            # Agent(sub-implementer) in frontmatter only enforces in --agent mode.
            # We enforce it here for SDK-dispatched agents.
            if tool_name == "Agent":
                config = self._get_config(agent_name)
                allowed_subagents = _parse_allowed_subagents(config.tools)
                if allowed_subagents is not None:
                    subagent_type = tool_input.get("subagent_type") or tool_input.get("type") or ""
                    if subagent_type and subagent_type not in allowed_subagents:
                        return {"decision": "block", "reason": f"agent '{agent_name}' can only spawn {allowed_subagents}, not '{subagent_type}'"}

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
        "planner": 300,
        "architect": 600,
        "implementer": 900,
        "test-engineer": 600,
        "runtime-evaluator": 300,
        "runtime-verifier": 900,
        "typescript-reviewer": 300,
        "ios-reviewer": 300,
    }
    DEFAULT_TIMEOUT = 300  # 5 minutes

    # No max_turns limit — timeout is the safety valve.
    # Subscription plan doesn't charge per token, so turns are not a cost concern.
    # Timeout (AGENT_TIMEOUT) prevents runaway agents.

    async def query(self, agent: str, prompt: str, model: str = "sonnet", **_: Any) -> str:
        """Run an agent query using Claude Agent SDK.

        Uses ClaudeSDKClient (not sdk_query) so we can call disconnect()
        on timeout to kill the underlying CLI process and its subagents.

        Args:
            agent: Agent name (matches agents/{name}.md)
            prompt: User prompt to send
            model: Model shortname override (opus/sonnet/haiku)

        Returns:
            The agent's final text response.

        Raises:
            RuntimeError: on agent error or timeout.
        """
        config = self._get_config(agent)
        model_id = _resolve_model(model or config.model)
        timeout = self.AGENT_TIMEOUT.get(agent, self.DEFAULT_TIMEOUT)

        options = ClaudeAgentOptions(
            system_prompt=config.system_prompt,
            cwd=self.cwd,
            allowed_tools=config.tools + ["TodoWrite"],
            permission_mode="bypassPermissions",
            model=model_id,
            hooks=self._make_hooks(agent),
        )

        client = ClaudeSDKClient(options=options)
        try:
            return await asyncio.wait_for(
                self._run_client(client, agent, prompt, model_id),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # disconnect() kills the CLI process and all its subagents
            try:
                client.disconnect()
            except Exception:
                pass
            raise RuntimeError(f"agent={agent} timed out after {timeout}s")
        except Exception:
            try:
                client.disconnect()
            except Exception:
                pass
            raise

    async def _run_client(self, client: Any, agent: str, prompt: str, model_id: str) -> str:
        """Run an agent via ClaudeSDKClient. Called within wait_for timeout."""
        await client.connect(prompt=prompt)

        final_text = ""
        try:
            async for message in client.receive_messages():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            await self.bus.emit(AgentMessage(
                                agent=agent,
                                role="assistant",
                                content_preview=block.text,
                            ))

                if isinstance(message, ResultMessage):
                    if getattr(message, "is_error", False):
                        errors = getattr(message, "errors", []) or []
                        raise RuntimeError(f"agent error: {'; '.join(errors) if errors else 'unknown'}")
                    final_text = getattr(message, "result", "") or ""
                    usage = getattr(message, "usage", None)
                    if usage:
                        await self.bus.emit(AgentTokens(
                            agent=agent,
                            input_tokens=usage.get("input_tokens", 0) if isinstance(usage, dict) else getattr(usage, "input_tokens", 0),
                            output_tokens=usage.get("output_tokens", 0) if isinstance(usage, dict) else getattr(usage, "output_tokens", 0),
                        ))
                    break  # ResultMessage = agent done, stop listening
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"agent={agent} model={model_id}: {exc}") from exc
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

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

    async def run_test_engineer(self, stage: Stage) -> dict:
        """Dispatch test-engineer agent and parse structured output."""
        changed_files = await self._get_changed_files()
        # Check if test files already exist in the changed files
        test_files = [f for f in changed_files.splitlines() if 'test' in f.lower() or 'spec' in f.lower()]
        if test_files:
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
    # Codex review (via Agent SDK + codex plugin)
    # ------------------------------------------------------------------

    async def run_codex_review(self) -> dict:
        """Run codex review via Agent SDK, using the codex companion script.

        Dispatches a lightweight agent with only Bash access to run the
        codex-companion.mjs script. The absolute path is resolved upfront
        so the agent doesn't need to find it.
        """
        codex_plugin_root, companion_script, reason = self._resolve_codex_companion()
        if not codex_plugin_root or not companion_script:
            return {
                "status": "skipped",
                "has_issues": False,
                "output": "",
                "reason": reason or "codex plugin not found",
            }

        # Determine base ref for diff
        base = "HEAD~1"
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
                timeout=180,  # 3 minutes hard cap — codex should not take longer
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
                "reason": "codex review timed out after 180 seconds",
            }
        except Exception as e:
            return {
                "status": "skipped",
                "has_issues": False,
                "output": "",
                "reason": f"codex review failed: {e}",
            }

    async def run_codex_plan_review(self, plan_text: str) -> dict:
        """Run Codex against a staged plan and return structured findings."""
        codex_plugin_root, companion_script, reason = self._resolve_codex_companion()
        if not codex_plugin_root or not companion_script:
            return {
                "status": "skipped",
                "has_major_issues": False,
                "summary": "",
                "findings": [],
                "output": "",
                "reason": reason or "codex plugin not found",
            }

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

            cmd = (
                f"node {shlex.quote(str(companion_script))} task --json "
                f"--cwd {shlex.quote(self.cwd)} "
                f"--prompt-file {shlex.quote(prompt_path)}"
            )
            payload_raw = await asyncio.wait_for(
                self._run_codex_command(cmd, codex_plugin_root),
                timeout=240,
            )
            payload = _extract_json_object(payload_raw)
            if not payload:
                return {
                    "status": "skipped",
                    "has_major_issues": False,
                    "summary": "",
                    "findings": [],
                    "output": payload_raw,
                    "reason": "codex plan review returned invalid JSON payload",
                }

            payload_status = payload.get("status", 0)
            if payload_status not in (0, "0", None):
                return {
                    "status": "skipped",
                    "has_major_issues": False,
                    "summary": "",
                    "findings": [],
                    "output": str(payload.get("rawOutput", "") or payload_raw),
                    "reason": f"codex plan review task exited with status {payload_status}",
                }

            raw_output = str(payload.get("rawOutput", "") or "")
            parsed = _extract_json_object(raw_output)
            if not parsed:
                return {
                    "status": "skipped",
                    "has_major_issues": False,
                    "summary": "",
                    "findings": [],
                    "output": raw_output or payload_raw,
                    "reason": "codex plan review did not return structured JSON",
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
            }
        except asyncio.TimeoutError:
            return {
                "status": "skipped",
                "has_major_issues": False,
                "summary": "",
                "findings": [],
                "output": "",
                "reason": "codex plan review timed out after 240 seconds",
            }
        except Exception as exc:
            return {
                "status": "skipped",
                "has_major_issues": False,
                "summary": "",
                "findings": [],
                "output": "",
                "reason": f"codex plan review failed: {exc}",
            }
        finally:
            if prompt_path:
                try:
                    Path(prompt_path).unlink(missing_ok=True)
                except Exception:
                    pass

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
        async for message in sdk_query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        all_text.append(block.text)
            elif isinstance(message, ResultMessage):
                # Check for errors in the result
                if getattr(message, "is_error", False):
                    errors = getattr(message, "errors", []) or []
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
    # Runtime evaluator dispatch
    # ------------------------------------------------------------------

    async def run_runtime_evaluator(self, contract: str) -> dict:
        """Dispatch runtime-verifier agent for black-box verification."""
        prompt = (
            f"Verify the following sprint contract against the running application:\n\n"
            f"{contract}\n\n"
            f"After verification, output a summary line in this exact format:\n"
            f"VERIFICATION_SUMMARY: status=PASS|FAIL score=N/M\n\n"
            f"Where N is the number of criteria passed and M is total must-pass criteria."
        )
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
