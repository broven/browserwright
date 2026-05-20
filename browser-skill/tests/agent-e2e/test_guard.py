"""Unit tests for the PreToolUse guard (no network needed)."""
from __future__ import annotations

import asyncio

import pytest

from guards import pre_tool_use


def _make_input(tool_name: str, tool_input: dict):
    """Build a minimal PreToolUseHookInput-like object."""
    class FakeInput:
        pass
    inp = FakeInput()
    inp.tool_name = tool_name
    inp.tool_input = tool_input
    return inp


def _run(tool_name: str, tool_input: dict) -> dict:
    inp = _make_input(tool_name, tool_input)
    return asyncio.run(pre_tool_use(inp, None, {}))


def test_deny_read_py():
    result = _run("Read", {"file_path": "/some/path/module.py"})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_deny_bash_non_browser():
    result = _run("Bash", {"command": "cat /etc/passwd"})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_allow_read_skill_md():
    result = _run("Read", {"file_path": "/workspace/skill/SKILL.md"})
    assert result == {}


def test_allow_bash_browser_skill():
    result = _run("Bash", {"command": "browser-skill <<'PY'\nprint('hello')\nPY"})
    assert result == {}


def test_allow_bash_browser_daemon():
    result = _run("Bash", {"command": "browser-daemon doctor"})
    assert result == {}


def test_deny_write_py():
    result = _run("Write", {"file_path": "/workspace/hack.py"})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_allow_write_md():
    result = _run("Write", {"file_path": "/workspace/skill/memory.md"})
    assert result == {}
