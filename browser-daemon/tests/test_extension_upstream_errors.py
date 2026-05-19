"""Daemon error messages must point clients at the recovery path."""
import pytest


def test_requires_sessionid_error_mentions_recovery_methods():
    """The 'requires a sessionId' error must name both
    BrowserDaemon.attachActiveTab AND BrowserDaemon.openBackgroundTab so
    the client knows what to call next, NOT just what is missing."""
    # Build the exact error string the upstream emits today.
    from browser_daemon.server.extension_upstream import (
        _build_requires_session_error,  # add this helper in Step 3
    )
    msg = _build_requires_session_error("Input.insertText")
    assert "Input.insertText" in msg
    assert "BrowserDaemon.attachActiveTab" in msg
    assert "BrowserDaemon.openBackgroundTab" in msg


def test_unknown_sessionid_error_mentions_subprocess_cause():
    """'unknown sessionId' must hint that the binding was likely released
    by a transient ws (CLI subprocess) so the client knows to re-attach
    from the same ws."""
    from browser_daemon.server.extension_upstream import (
        _build_unknown_session_error,  # add this helper in Step 3
    )
    msg = _build_unknown_session_error("c110-DEADBEEF")
    assert "c110-DEADBEEF" in msg
    assert "subprocess" in msg.lower() or "transient" in msg.lower()
    assert "re-attach" in msg.lower() or "reattach" in msg.lower()
