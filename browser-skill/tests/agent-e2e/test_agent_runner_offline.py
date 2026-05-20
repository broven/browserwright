"""Offline tests for agent_runner helper functions (no SDK call needed)."""
from __future__ import annotations

from agent_runner import (
    _count_failed_bash,
    _last_assistant_text,
    _looks_like_question,
    _msg_to_trace,
)


def test_count_failed_bash():
    trace = [
        {"type": "AssistantMessage", "content": [{"type": "tool_use", "name": "Bash"}]},
        {"type": "UserMessage", "content": "Exit code 1\ncommand not found"},
        {"type": "AssistantMessage", "content": [{"type": "tool_use", "name": "Bash"}]},
        {"type": "UserMessage", "content": "page_info() returned ok"},
        {"type": "AssistantMessage", "content": [{"type": "text", "text": "Done"}]},
    ]
    assert _count_failed_bash(trace) == 1


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
    assert not _looks_like_question("I have completed the task.")
    assert not _looks_like_question("The page shows example content.")
