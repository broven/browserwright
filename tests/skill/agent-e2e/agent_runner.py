"""Claude Agent SDK wrapper for agent-e2e tests.

SDK ResultMessage fields (claude-agent-sdk 0.2.82):
  subtype, duration_ms, duration_api_ms, is_error, num_turns, session_id,
  stop_reason, total_cost_usd, usage, result, structured_output, model_usage,
  permission_denials, deferred_tool_use, errors, api_error_status, uuid

ClaudeSDKClient:
  async with ClaudeSDKClient(options) as client:
      await client.query("prompt")
      async for msg in client.receive_response():
          ...  # AssistantMessage, UserMessage, ResultMessage
      # multi-turn: await client.query("follow-up"), then receive_response again
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk.types import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)

import guards

SYSTEM_PROMPT = (
    "You are a coding agent. The user will give you a task. "
    "Your current working directory contains SKILL.md — read it first "
    "(use the Read tool with the relative path 'SKILL.md'). "
    "It documents the browserwright CLI and how to use it. "
    "Follow its instructions precisely, including when to ask for "
    "user confirmation before taking actions like saving tasks. "
    "Do the task."
)


@dataclass
class AgentResult:
    output: str
    trace: list[dict[str, Any]]
    turns: int
    usage: dict[str, Any] | None
    asked_user: bool
    user_questions: list[str]
    failed_bash: int
    is_error: bool
    stop_reason: str | None


def _msg_to_trace(msg: Any) -> dict[str, Any]:
    """Flatten an SDK message into a JSON-serializable dict for trace logging."""
    d: dict[str, Any] = {"type": type(msg).__name__}
    if isinstance(msg, AssistantMessage):
        blocks = []
        for b in (msg.content or []):
            if isinstance(b, TextBlock):
                blocks.append({"type": "text", "text": b.text})
            elif isinstance(b, ToolUseBlock):
                blocks.append({
                    "type": "tool_use",
                    "name": b.name,
                    "input": b.input,
                    "id": b.id,
                })
            else:
                blocks.append({"type": type(b).__name__, "raw": str(b)})
        d["content"] = blocks
    elif isinstance(msg, UserMessage):
        d["content"] = str(msg.content)[:2000] if hasattr(msg, "content") else ""
    elif isinstance(msg, ResultMessage):
        d["result"] = msg.result
        d["num_turns"] = msg.num_turns
        d["stop_reason"] = msg.stop_reason
        d["is_error"] = msg.is_error
        d["total_cost_usd"] = msg.total_cost_usd
    else:
        d["raw"] = str(msg)[:2000]
    return d


def _count_failed_bash(trace: list[dict[str, Any]]) -> int:
    """Count Bash tool_use blocks that produced a nonzero exit code."""
    count = 0
    last_was_bash = False
    for entry in trace:
        if entry.get("type") == "AssistantMessage":
            last_was_bash = any(
                b.get("type") == "tool_use" and b.get("name") == "Bash"
                for b in entry.get("content", [])
            )
        elif entry.get("type") == "UserMessage" and last_was_bash:
            content = entry.get("content", "")
            if "exit code" in content.lower() and "exit code 0" not in content.lower():
                count += 1
            last_was_bash = False
    return count


def _last_assistant_text(trace: list[dict[str, Any]]) -> str:
    """Extract the last text from the most recent AssistantMessage."""
    for entry in reversed(trace):
        if entry.get("type") == "AssistantMessage":
            for block in reversed(entry.get("content", [])):
                if block.get("type") == "text":
                    return block["text"]
    return ""


def _looks_like_question(text: str) -> bool:
    """Heuristic: did the agent ask the user something?"""
    indicators = [
        "?", "确认", "save as task", "want me to", "shall i",
        "would you like", "should i", "是否", "要不要",
        "store", "persist", "创建", "save this", "记住",
    ]
    lower = text.lower()
    return any(ind in lower for ind in indicators)


async def run_agent(
    task: str,
    *,
    workspace: Path,
    env: dict[str, str] | None = None,
    model: str = "claude-sonnet-4-6",
    max_turns: int = 25,
    user_replies: list[str] | None = None,
) -> AgentResult:
    """Run a sub-agent against the workspace and return structured results."""
    run_env = env or {}

    # Tell the guard where the workspace is so it can scope path checks.
    guards.WORKSPACE_ROOT = workspace

    options = ClaudeAgentOptions(
        cwd=str(workspace / "skill"),
        tools=["Bash", "Read", "Write", "Edit", "Grep", "Glob"],
        allowed_tools=["Bash", "Read", "Write", "Edit", "Grep", "Glob"],
        model=model,
        system_prompt=SYSTEM_PROMPT,
        env={
            **run_env,
            "PATH": os.environ.get("PATH", ""),
            # Real HOME is needed for Claude CLI auth (~/.claude/.credentials.json).
            # browserwright isolation is handled by BS_HOME env var (set in provider.py)
            # and the path-scoping guard blocks Write/Read outside workspace.
            "HOME": os.environ.get("HOME", ""),
        },
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        hooks={
            "PreToolUse": [
                HookMatcher(hooks=[guards.pre_tool_use]),
            ],
        },
        setting_sources=[],
    )

    trace: list[dict[str, Any]] = []
    output = ""
    turns = 0
    usage = None
    is_error = False
    stop_reason = None
    asked_user = False
    user_questions: list[str] = []
    replies = list(user_replies or [])

    async with ClaudeSDKClient(options) as client:
        await client.query(task)

        while True:
            async for msg in client.receive_response():
                trace.append(_msg_to_trace(msg))

                if isinstance(msg, ResultMessage):
                    output = msg.result or ""
                    turns = msg.num_turns
                    usage = msg.usage
                    is_error = msg.is_error
                    stop_reason = msg.stop_reason

            # Check if the agent asked a question and we have replies
            last_text = _last_assistant_text(trace)
            if replies and _looks_like_question(last_text):
                asked_user = True
                user_questions.append(last_text[:500])
                reply = replies.pop(0)
                await client.query(reply)
                continue
            else:
                break

    return AgentResult(
        output=output,
        trace=trace,
        turns=turns,
        usage=usage,
        asked_user=asked_user,
        user_questions=user_questions,
        failed_bash=_count_failed_bash(trace),
        is_error=is_error,
        stop_reason=stop_reason,
    )
