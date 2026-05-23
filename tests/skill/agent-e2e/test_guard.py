"""Unit tests for the PreToolUse guard (no network needed)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import guards
from guards import pre_tool_use

# Set workspace root for path scoping tests.
_WS = Path("/tmp/fake-workspace")
guards.WORKSPACE_ROOT = _WS


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


def _is_deny(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


# --- Bash ---

def test_deny_bash_non_browser():
    assert _is_deny(_run("Bash", {"command": "cat /etc/passwd"}))


def test_allow_bash_browserwright():
    assert _run("Bash", {"command": "browserwright <<'PY'\nprint('hello')\nPY"}) == {}


def test_allow_bash_browserwright_daemon():
    assert _run("Bash", {"command": "browserwright-daemon doctor"}) == {}


def test_deny_bash_injection_semicolon():
    assert _is_deny(_run("Bash", {"command": "browserwright foo; cat /etc/passwd"}))


def test_deny_bash_injection_pipe():
    assert _is_deny(_run("Bash", {"command": "browserwright foo | cat /etc/passwd"}))


def test_deny_bash_injection_and():
    assert _is_deny(_run("Bash", {"command": "browserwright foo && rm -rf /"}))


def test_deny_bash_injection_subshell():
    assert _is_deny(_run("Bash", {"command": "browserwright $(cat /etc/passwd)"}))


def test_deny_bash_injection_backtick():
    assert _is_deny(_run("Bash", {"command": "browserwright `cat /etc/passwd`"}))


# --- Read/Grep path scoping ---

def test_deny_read_py():
    assert _is_deny(_run("Read", {"file_path": "/tmp/fake-workspace/module.py"}))


def test_allow_read_skill_md():
    assert _run("Read", {"file_path": "/tmp/fake-workspace/skill/SKILL.md"}) == {}


def test_allow_read_relative_skill_md():
    """Agent uses relative path 'SKILL.md' from its CWD (workspace/skill/)."""
    assert _run("Read", {"file_path": "SKILL.md"}) == {}


def test_deny_read_relative_traversal():
    """Traversal via .. should be caught after normpath."""
    assert _is_deny(_run("Read", {"file_path": "../../../etc/passwd"}))


def test_deny_read_outside_workspace():
    assert _is_deny(_run("Read", {"file_path": "/etc/passwd"}))


def test_deny_read_home_real():
    assert _is_deny(_run("Read", {"file_path": "/Users/metajs/.bashrc"}))


def test_deny_grep_outside_workspace():
    assert _is_deny(_run("Grep", {"path": "/etc/"}))


# --- Glob ---

def test_allow_glob_relative_pattern():
    # Glob with just a pattern (no path) is OK — relative to CWD
    assert _run("Glob", {"pattern": "**/*.md"}) == {}


def test_deny_glob_outside_workspace():
    assert _is_deny(_run("Glob", {"path": "/etc/", "pattern": "*"}))


# --- Write/Edit ---

def test_deny_write_py():
    assert _is_deny(_run("Write", {"file_path": "/tmp/fake-workspace/hack.py"}))


def test_allow_write_md():
    assert _run("Write", {"file_path": "/tmp/fake-workspace/skill/memory.md"}) == {}


def test_deny_write_outside_workspace():
    assert _is_deny(_run("Write", {"file_path": "/tmp/other/file.md"}))


def test_deny_edit_outside_workspace():
    assert _is_deny(_run("Edit", {"file_path": "/Users/metajs/.browserwright/global.md"}))


# --- Unknown tool ---

def test_allow_bash_heredoc_with_pipe_in_python():
    """Pipe inside heredoc Python code must NOT trigger injection check."""
    cmd = "browserwright <<'PY'\nx = 5 | 3\nprint(x)\nPY"
    assert _run("Bash", {"command": cmd}) == {}


def test_deny_bash_pipe_before_heredoc():
    cmd = "browserwright foo | cat <<'PY'\nPY"
    assert _is_deny(_run("Bash", {"command": cmd}))


def test_deny_bash_multiline_with_sleep():
    """Multiline command where second line is sleep (not browserwright)."""
    cmd = "browserwright-daemon launch-chrome &\nsleep 3\necho done"
    assert _is_deny(_run("Bash", {"command": cmd}))


def test_allow_bash_env_prefix():
    """BD_PORT=9333 BD_BACKEND=rdp browserwright <<'PY' ... is allowed."""
    cmd = "BD_PORT=9333 BD_BACKEND=rdp browserwright <<'PY'\nprint('hi')\nPY"
    assert _run("Bash", {"command": cmd}) == {}


def test_allow_bash_background_single():
    """browserwright-daemon serve & (backgrounding single command) is OK."""
    cmd = "browserwright-daemon serve --backend extension &"
    assert _run("Bash", {"command": cmd}) == {}


def test_deny_unknown_tool():
    assert _is_deny(_run("WebFetch", {"url": "https://example.com"}))
