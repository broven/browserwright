"""Offline tests for agent_runner helper functions (no SDK call needed)."""
from __future__ import annotations

from agent_runner import (
    _count_failed_bash,
    _last_assistant_text,
    _looks_like_question,
)


def test_count_failed_bash_one_failure():
    trace = [
        {"type": "AssistantMessage", "content": [{"type": "tool_use", "name": "Bash"}]},
        {"type": "UserMessage", "content": "Exit code 1\ncommand not found"},
        {"type": "AssistantMessage", "content": [{"type": "tool_use", "name": "Bash"}]},
        {"type": "UserMessage", "content": "page_info() returned ok"},
        {"type": "AssistantMessage", "content": [{"type": "text", "text": "Done"}]},
    ]
    assert _count_failed_bash(trace) == 1


def test_count_failed_bash_ignores_read_errors():
    """Only Bash tool_use blocks should be counted, not Read errors."""
    trace = [
        {"type": "AssistantMessage", "content": [{"type": "tool_use", "name": "Read"}]},
        {"type": "UserMessage", "content": "Exit code 1\nfile not found"},
        {"type": "AssistantMessage", "content": [{"type": "tool_use", "name": "Bash"}]},
        {"type": "UserMessage", "content": "Exit code 0\nok"},
    ]
    assert _count_failed_bash(trace) == 0


def test_count_failed_bash_ignores_exit_code_zero():
    trace = [
        {"type": "AssistantMessage", "content": [{"type": "tool_use", "name": "Bash"}]},
        {"type": "UserMessage", "content": "Exit code 0\nall good"},
    ]
    assert _count_failed_bash(trace) == 0


def test_count_failed_bash_ignores_benign_error_word():
    """A UserMessage mentioning 'error' after a non-Bash tool should not count."""
    trace = [
        {"type": "AssistantMessage", "content": [{"type": "tool_use", "name": "Write"}]},
        {"type": "UserMessage", "content": "no errors found in the document"},
    ]
    assert _count_failed_bash(trace) == 0


def test_last_assistant_text():
    trace = [
        {"type": "AssistantMessage", "content": [{"type": "text", "text": "First"}]},
        {"type": "UserMessage", "content": "ok"},
        {"type": "AssistantMessage", "content": [
            {"type": "tool_use", "name": "Read"},
            {"type": "text", "text": "Should I save as task?"},
        ]},
    ]
    assert _last_assistant_text(trace) == "Should I save as task?"


def test_looks_like_question():
    assert _looks_like_question("Should I save as task?")
    assert _looks_like_question("确认写入偏好？")
    assert _looks_like_question("Would you like me to proceed?")
    assert _looks_like_question("Want me to save this as a reusable task?")
    assert _looks_like_question("要不要保存？")
    assert _looks_like_question("是否要将这个流程保存为可复用的 task?")
    assert _looks_like_question("Shall I save this preference?")
    assert _looks_like_question("I'll store this for next time.")
    assert _looks_like_question("创建一个定时任务？")
    assert not _looks_like_question("I have completed the flow.")
    assert not _looks_like_question("The page shows example content.")
