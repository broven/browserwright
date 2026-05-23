"""Daemon error messages must point clients at the recovery path."""


def test_requires_sessionid_error_mentions_recovery_methods():
    """The 'requires a sessionId' error must name both
    BrowserwrightDaemon.attachActiveTab AND BrowserwrightDaemon.openBackgroundTab so
    the client knows what to call next, NOT just what is missing."""
    from browserwright.daemon.server.extension_upstream import (
        _build_requires_session_error,
    )
    msg = _build_requires_session_error("Input.insertText")
    assert "Input.insertText" in msg
    assert "BrowserwrightDaemon.attachActiveTab" in msg
    assert "BrowserwrightDaemon.openBackgroundTab" in msg


def test_create_target_error_names_real_verbs():
    """4c: Target.createTarget must fast-fail with a message naming the real
    tab-opening verbs — the skill primitive open_background() and the daemon
    verb openBackgroundTab — not the misleading 'requires a sessionId' and not
    the non-existent new_page()."""
    from browserwright.daemon.server.extension_upstream import _build_create_target_error
    msg = _build_create_target_error()
    assert "open_background" in msg
    assert "openBackgroundTab" in msg
    assert "new_page" not in msg          # not a real browserwright primitive
    assert "sessionId" not in msg


def test_unknown_sessionid_error_mentions_subprocess_cause():
    """'unknown sessionId' must hint that the binding was likely released
    by a transient ws (CLI subprocess) so the client knows to re-attach
    from the same ws."""
    from browserwright.daemon.server.extension_upstream import (
        _build_unknown_session_error,
    )
    msg = _build_unknown_session_error("c110-DEADBEEF")
    assert "c110-DEADBEEF" in msg
    assert "subprocess" in msg.lower() or "transient" in msg.lower()
    assert "re-attach" in msg.lower() or "reattach" in msg.lower()
