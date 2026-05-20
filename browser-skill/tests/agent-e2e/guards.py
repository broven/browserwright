"""PreToolUse hook guard for agent-e2e sub-agent.

Restricts the sub-agent to:
- Read/Grep/Glob: only files inside the workspace (skill/) — blocks .py files
- Bash: only commands starting with `browser-skill` or `browser-daemon`
- Write/Edit: only inside the workspace
"""
from __future__ import annotations

from claude_agent_sdk.types import (
    PreToolUseHookInput,
    HookContext,
    SyncHookJSONOutput,
)

# Tools that are always allowed without inspection
_ALWAYS_ALLOW = {"Glob", "TodoWrite"}

# Bash command prefixes that are allowed
_BASH_PREFIXES = ("browser-skill", "browser-daemon")

# File extensions the sub-agent must not read (source code)
_BLOCKED_EXTENSIONS = (".py", ".pyc", ".pyo")


def _deny(reason: str) -> SyncHookJSONOutput:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _allow() -> SyncHookJSONOutput:
    return {}


async def pre_tool_use(
    input_data: PreToolUseHookInput | dict,
    tool_use_id: str | None,
    context: HookContext,
) -> SyncHookJSONOutput:
    """Guard callback for PreToolUse events."""
    # SDK may pass a dict or a typed dataclass depending on the transport.
    if isinstance(input_data, dict):
        tool = input_data.get("tool_name", "")
        inp = input_data.get("tool_input", {})
    else:
        tool = input_data.tool_name
        inp = input_data.tool_input

    if tool in _ALWAYS_ALLOW:
        return _allow()

    if tool == "Bash":
        cmd = inp.get("command", "")
        if not any(cmd.strip().startswith(p) for p in _BASH_PREFIXES):
            return _deny(
                f"Bash command must start with {_BASH_PREFIXES}; got: {cmd[:80]}"
            )
        return _allow()

    if tool in ("Read", "Grep"):
        path = inp.get("file_path", "") or inp.get("path", "")
        if any(path.endswith(ext) for ext in _BLOCKED_EXTENSIONS):
            return _deny(f"Reading source code ({path}) is not allowed")
        return _allow()

    if tool in ("Write", "Edit"):
        path = inp.get("file_path", "")
        if any(path.endswith(ext) for ext in _BLOCKED_EXTENSIONS):
            return _deny(f"Writing source code ({path}) is not allowed")
        return _allow()

    # Default: allow (Glob, etc.)
    return _allow()
