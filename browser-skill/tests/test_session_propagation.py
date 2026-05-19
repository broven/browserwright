"""Layer 1 + 2 regression tests for session-propagation fixes
(docs/plans/2026-05-19-session-propagation-and-agent-guidance-plan.md).

These tests use the same stub-session pattern as
``test_primitives_offline.py`` — no live daemon required.
"""
from __future__ import annotations

import pytest


class _StubCDP:
    """Captures send() calls so we can assert the wire shape."""

    def __init__(self, response: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._response = response or {}
        self._sessions: dict[str, str] = {}
        self._events: dict[str, object] = {}

    def send(self, method: str, *, session: str | None = None, **params) -> dict:
        self.calls.append((method, {"session": session, **params}))
        return self._response

    def attach(self, target_id: str) -> str:
        return self._sessions.setdefault(target_id, "sid-cached")


def _stub_session_for_ws(monkeypatch, *, backend: str = "extension",
                         response: dict | None = None):
    """Drop a Session with a stub CDP onto the singleton so primitives
    operate against our recorder. Mirrors ``test_primitives_offline._stub_session``
    but for the long-lived-ws path."""
    from browser_skill import session as session_mod

    class _StubSession:
        def __init__(self):
            self.cdp = _StubCDP(response=response)
            self.current_target_id = None
            self._backend_name_cache = backend
            self.daemon = None  # No mode_b_client — primitives must NOT touch it

        @property
        def backend_name(self) -> str:
            return self._backend_name_cache

    sess = _StubSession()
    monkeypatch.setattr(session_mod, "_singleton", sess)
    return sess


def test_open_background_uses_long_lived_ws_not_subprocess(monkeypatch):
    """open_background() must dispatch BrowserDaemon.openBackgroundTab over
    sess.cdp.send (the long-lived ws), NOT via daemon.open_background()
    (CLI subprocess that loses the sessionId binding on exit)."""
    from browser_skill.primitives.page import open_background

    sess = _stub_session_for_ws(monkeypatch, response={
        "sessionId": "ws-sid-1",
        "targetId": "ext-tab-42",
        "tabId": 42,
        "url": "https://example.com",
        "title": "Example",
        "groupId": 7,
    })
    result = open_background("https://example.com", group="Agent-Test")

    # Wire shape: exactly one BrowserDaemon.openBackgroundTab over sess.cdp.
    assert sess.cdp.calls == [
        ("BrowserDaemon.openBackgroundTab",
         {"session": None, "url": "https://example.com",
          "groupName": "Agent-Test"}),
    ], f"unexpected wire calls: {sess.cdp.calls!r}"

    # The sid IS pre-registered in the local session map so a follow-up
    # cdp.attach(target_id) returns the same sid without re-attaching.
    assert sess.cdp._sessions["ext-tab-42"] == "ws-sid-1"

    # Return shape matches the documented contract.
    assert result == {
        "targetId": "ext-tab-42",
        "tabId": 42,
        "url": "https://example.com",
        "title": "Example",
        "groupId": 7,
    }
    assert sess.current_target_id == "ext-tab-42"


def test_close_tab_uses_long_lived_ws_not_subprocess(monkeypatch):
    """close_tab() must dispatch BrowserDaemon.closeTab over sess.cdp.send."""
    from browser_skill.primitives.page import close_tab

    sess = _stub_session_for_ws(monkeypatch, response={
        "ok": True, "tabId": 99,
    })
    # Seed a target_id → sid mapping like a prior open_background would have.
    sess.cdp._sessions["ext-tab-99"] = "ws-sid-99"
    sess.current_target_id = "ext-tab-99"

    result = close_tab(target_id="ext-tab-99")

    # The session_id forwarded to the daemon comes from the local cache
    # (since we have one). Both params are sent.
    assert sess.cdp.calls == [
        ("BrowserDaemon.closeTab",
         {"session": None, "sessionId": "ws-sid-99",
          "targetId": "ext-tab-99"}),
    ], f"unexpected wire calls: {sess.cdp.calls!r}"
    assert result == {"ok": True, "tabId": 99}
    # Local state is cleaned up after a successful close.
    assert "ext-tab-99" not in sess.cdp._sessions
    assert sess.current_target_id is None
