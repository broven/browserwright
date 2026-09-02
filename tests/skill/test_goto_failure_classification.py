"""Regression: `page.goto()` failures must be classified by real cause.

The original `_page_load_failed()` had two branches that returned the SAME
`reason` ("network") and the SAME fix, so the `if` was dead: every non-timeout
navigation failure — interrupted navigations, closed targets, detached frames,
CDP/relay faults — was reported as a network problem, and the original
exception was thrown away entirely. A 240-page crawl produced 153 of these and
sent the operator to debug their connection instead of the relay.

Each test below fails against that old implementation.
"""
from __future__ import annotations

import pytest

from browserwright.errors import PageLoadFailed
from browserwright.repl import _smart_goto


class _PWError(Exception):
    """Stand-in for playwright's Error (matched on message, not type)."""


class TimeoutError(Exception):  # noqa: A001 - mirrors playwright's class name
    pass


def _fail(exc: BaseException) -> PageLoadFailed:
    return _smart_goto._page_load_failed("https://example.com/p", "commit", exc)


CASES = [
    pytest.param(
        _PWError('Page.goto: net::ERR_NAME_NOT_RESOLVED at https://nope.invalid/'),
        "network",
        id="dns",
    ),
    pytest.param(
        _PWError("Page.goto: net::ERR_CERT_AUTHORITY_INVALID"),
        "network",
        id="ssl",
    ),
    pytest.param(
        _PWError(
            'Page.goto: Navigation to "https://x.com/a" is interrupted by '
            'another navigation to "https://x.com/b"'
        ),
        "navigation-interrupted",
        id="interrupted",
    ),
    pytest.param(
        _PWError("Page.goto: net::ERR_ABORTED at https://example.com/dl"),
        "navigation-interrupted",
        id="aborted",
    ),
    pytest.param(
        _PWError(
            "Page.goto: Target page, context or browser has been closed"
        ),
        "target-closed",
        id="target-closed",
    ),
    pytest.param(
        _PWError("Page.goto: frame was detached"),
        "frame-detached",
        id="frame-detached",
    ),
    pytest.param(
        _PWError("Protocol error (Page.navigate): Debugger is not attached"),
        "cdp-transport",
        id="cdp",
    ),
    pytest.param(
        # THE field case. The extension caps every chrome.debugger command at
        # 9000ms (background.js DEBUGGER_COMMAND_TIMEOUT_MS) and says "timed
        # out" — which does NOT contain the substring "timeout", so the old
        # classifier's timeout check missed it and it landed in "network".
        _PWError(
            "Protocol error (Page.navigate): chrome.debugger.sendCommand "
            "timed out after 9000ms (Page.navigate tabId=42); the command may "
            "still land in Chrome"
        ),
        "extension-budget",
        id="extension-9s-budget",
    ),
    pytest.param(
        _PWError(
            "Protocol error (Page.navigate): relay send failed: "
            "ConnectionError('extension relay closed: stale app-level heartbeat')"
        ),
        "cdp-transport",
        id="relay-stale",
    ),
    pytest.param(
        _PWError("something nobody has seen before"),
        "unknown",
        id="unknown",
    ),
]


@pytest.mark.parametrize("exc,expected_reason", CASES)
def test_reason_matches_real_cause(exc, expected_reason):
    assert _fail(exc).reason == expected_reason


def test_timeout_keeps_the_phase_as_its_reason():
    err = _fail(TimeoutError("Page.goto: Timeout 60000ms exceeded."))
    assert err.reason == "commit"
    assert "http_get" in err.fix


def test_every_bucket_has_a_distinct_fix():
    """The dead `if` was invisible because both branches shared a fix string."""
    fixes = {}
    for param in CASES:
        exc, reason = param.values
        err = _fail(exc)
        assert err.fix, f"{reason} has no fix"
        assert fixes.setdefault(err.fix, reason) == reason, (
            f"{reason} reuses the fix of {fixes[err.fix]} — a shared fix means "
            "the classification cannot change what the agent does next"
        )


def test_non_network_causes_do_not_tell_the_user_to_check_the_network():
    err = _fail(_PWError("Protocol error (Page.navigate): Target closed"))
    assert err.reason != "network"
    assert "check the URL and network" not in err.fix


def test_transport_timeouts_are_not_reported_as_the_site_timing_out():
    """A relay/extension budget expiring is not "the site did not respond"."""
    for msg in (
        "Protocol error (Page.navigate): chrome.debugger.sendCommand timed out "
        "after 9000ms (Page.navigate tabId=7)",
        "Protocol error (Page.navigate): relay send failed: TimeoutError()",
    ):
        err = _fail(_PWError(msg))
        assert err.reason not in ("network", "commit", "timeout"), err.reason
        assert "site did not respond" not in err.fix


@pytest.mark.parametrize("exc,_reason", CASES)
def test_original_exception_type_and_message_survive(exc, _reason):
    """The most damaging part of the old code: raw detail was discarded."""
    err = _fail(exc)
    assert err.detail
    assert type(exc).__name__ in err.detail
    assert str(exc)[:40] in err.detail
    assert str(exc)[:40] in str(err)


def test_detail_is_bounded_and_drops_playwright_call_log():
    exc = _PWError("net::ERR_FAILED at https://a/\nCall log:\n  - navigating\n" + "x" * 5000)
    err = _fail(exc)
    assert "Call log" not in err.detail
    assert len(err.detail) < 400
