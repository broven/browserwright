"""PreToolUse hook guard for agent-e2e sub-agent.

Restricts the sub-agent to:
- Read/Grep/Glob: only paths inside WORKSPACE_ROOT, blocks .py files
- Bash: single browserwright / browserwright-daemon invocations (no shell injection)
- Write/Edit: only paths inside WORKSPACE_ROOT, blocks .py files
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    from claude_agent_sdk.types import (
        PreToolUseHookInput,
        HookContext,
        SyncHookJSONOutput,
    )
except ModuleNotFoundError:  # pragma: no cover - legacy SDK intentionally disabled.
    PreToolUseHookInput = dict[str, Any]
    HookContext = Any
    SyncHookJSONOutput = dict[str, Any]

# Set by agent_runner before the agent starts.
WORKSPACE_ROOT: Path | None = None

# Bash command prefixes that are allowed
_BASH_PREFIXES = ("browserwright", "browserwright-daemon")

# Shell metacharacters that indicate command chaining / injection.
# Matches: ; | && || ` $( — but NOT a trailing & (backgrounding).
_SHELL_INJECTION_RE = re.compile(r";|\|\|?|&&|`|\$\(")

# File extensions the sub-agent must not read/write (source code)
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


def _path_inside_workspace(path_str: str) -> bool:
    """Check that *path_str* is lexically inside WORKSPACE_ROOT.

    Relative paths are resolved against WORKSPACE_ROOT/skill (the sub-agent's
    CWD). Uses normpath (no symlink resolution) so workspace symlinks are
    allowed. Traversal via .. is collapsed before comparison.
    """
    if WORKSPACE_ROOT is None:
        return False
    try:
        import os.path
        if not os.path.isabs(path_str):
            path_str = os.path.join(str(WORKSPACE_ROOT / "skill"), path_str)
        normed = os.path.normpath(path_str)
        ws_normed = os.path.normpath(str(WORKSPACE_ROOT))
        return normed == ws_normed or normed.startswith(ws_normed + os.sep)
    except (OSError, ValueError):
        return False


async def pre_tool_use(
    input_data: PreToolUseHookInput | dict,
    tool_use_id: str | None,
    context: HookContext,
) -> SyncHookJSONOutput:
    """Guard callback for PreToolUse events."""
    if isinstance(input_data, dict):
        tool = input_data.get("tool_name", "")
        inp = input_data.get("tool_input", {})
    else:
        tool = input_data.tool_name
        inp = input_data.tool_input

    if tool == "Bash":
        cmd = inp.get("command", "").strip()
        # Strip leading env var assignments (BD_PORT=9333 browserwright ...)
        first_line = cmd.split("\n", 1)[0].strip()
        _env_assign = re.compile(r"^[A-Z_]+=\S+\s+")
        cleaned_first = first_line
        while _env_assign.match(cleaned_first):
            cleaned_first = _env_assign.sub("", cleaned_first, count=1)
        if not any(cleaned_first.startswith(p) for p in _BASH_PREFIXES):
            return _deny(
                f"Bash command must start with {_BASH_PREFIXES}; got: {cmd[:80]}"
            )
        # Extract the shell portion (before any heredoc marker).
        shell_part = re.split(r"<<\s*['\"]?\w+['\"]?", cmd, maxsplit=1)[0]
        # Every non-empty line in the shell portion must start with an
        # allowed prefix (or be an env-var assignment like BD_PORT=...).
        _ENV_PREFIX = re.compile(r"^[A-Z_]+=\S+\s+")
        for line in shell_part.splitlines():
            line = line.strip().rstrip("&").strip()  # trailing & = background
            if not line:
                continue
            # Strip leading env assignments (BD_PORT=9333 browserwright ...)
            cleaned = _ENV_PREFIX.sub("", line)
            while _ENV_PREFIX.match(cleaned):
                cleaned = _ENV_PREFIX.sub("", cleaned)
            if not any(cleaned.startswith(p) for p in _BASH_PREFIXES):
                return _deny(
                    f"Bash line not an allowed command: {line[:80]}"
                )
        # Also reject chaining metacharacters within any single line.
        if _SHELL_INJECTION_RE.search(shell_part):
            return _deny(
                f"Bash command contains shell metacharacters: {shell_part[:80]}"
            )
        return _allow()

    if tool in ("Read", "Grep", "Glob"):
        path = inp.get("file_path", "") or inp.get("path", "") or inp.get("pattern", "")
        if any(path.endswith(ext) for ext in _BLOCKED_EXTENSIONS):
            return _deny(f"Reading source code ({path}) is not allowed")
        if path and not path.startswith("*"):
            if not _path_inside_workspace(path):
                return _deny(f"Path outside workspace: {path}")
        return _allow()

    if tool in ("Write", "Edit"):
        path = inp.get("file_path", "")
        if any(path.endswith(ext) for ext in _BLOCKED_EXTENSIONS):
            return _deny(f"Writing source code ({path}) is not allowed")
        if path and not _path_inside_workspace(path):
            return _deny(f"Path outside workspace: {path}")
        return _allow()

    if tool == "TodoWrite":
        return _allow()

    return _deny(f"Tool {tool} is not allowed")
