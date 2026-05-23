"""Helpers for reading either Claude trace metadata or Codex SDK raw items."""
from __future__ import annotations

import json
from typing import Any


def get_trace(context: dict) -> list[dict[str, Any]]:
    """Return the legacy Claude trace, or Codex SDK turn items as trace."""
    provider_response = context.get("providerResponse", {})
    meta = provider_response.get("metadata", {}) or {}
    trace = meta.get("trace")
    if isinstance(trace, list):
        return trace

    raw = provider_response.get("raw")
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []

    items = payload.get("items")
    return items if isinstance(items, list) else []


def iter_trace_text(trace: list[dict[str, Any]]):
    """Yield text-like fields from Claude trace blocks or Codex items."""
    for entry in trace:
        for key in (
            "text",
            "message",
            "aggregated_output",
            "output",
            "command",
            "query",
        ):
            value = entry.get(key)
            if isinstance(value, str):
                yield value

        content = entry.get("content", "")
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            if isinstance(block, str):
                yield block
            elif isinstance(block, dict):
                for key in ("text", "content", "command"):
                    value = block.get(key)
                    if isinstance(value, str):
                        yield value
                nested = block.get("content")
                if isinstance(nested, list):
                    for inner in nested:
                        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                            yield inner["text"]
                inp = block.get("input")
                if isinstance(inp, dict) and isinstance(inp.get("command"), str):
                    yield inp["command"]

        error = entry.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            yield error["message"]


def used_browserwright(trace: list[dict[str, Any]]) -> bool:
    """True when either trace shape contains a browserwright shell command."""
    for entry in trace:
        if entry.get("type") in {"exec_command", "command_execution"}:
            if "browserwright" in str(entry.get("command", "")):
                return True

        if entry.get("type") == "AssistantMessage":
            for block in entry.get("content", []):
                if block.get("type") == "tool_use" and block.get("name") == "Bash":
                    cmd = block.get("input", {})
                    if isinstance(cmd, dict):
                        cmd = cmd.get("command", "")
                    if "browserwright" in str(cmd):
                        return True

    return any("browserwright" in text for text in iter_trace_text(trace))


def count_failed_bash(trace: list[dict[str, Any]]) -> int:
    """Count failed shell commands for Claude trace or Codex SDK items."""
    count = 0
    last_was_bash = False
    for entry in trace:
        if entry.get("type") in {"exec_command", "command_execution"}:
            cmd = str(entry.get("command", ""))
            exit_code = entry.get("exit_code")
            if "browserwright" in cmd and isinstance(exit_code, int) and exit_code != 0:
                count += 1
            continue

        if entry.get("type") == "AssistantMessage":
            last_was_bash = any(
                block.get("type") == "tool_use" and block.get("name") == "Bash"
                for block in entry.get("content", [])
            )
        elif entry.get("type") == "UserMessage" and last_was_bash:
            content = entry.get("content", "")
            if "exit code" in content.lower() and "exit code 0" not in content.lower():
                count += 1
            last_was_bash = False
    return count
