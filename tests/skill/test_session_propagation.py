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
    from browserwright import session as session_mod

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
    """open_background() must dispatch BrowserwrightDaemon.openBackgroundTab over
    sess.cdp.send (the long-lived ws), NOT via daemon.open_background()
    (CLI subprocess that loses the sessionId binding on exit)."""
    from browserwright.primitives.page import open_background

    sess = _stub_session_for_ws(monkeypatch, response={
        "sessionId": "ws-sid-1",
        "targetId": "ext-tab-42",
        "tabId": 42,
        "url": "https://example.com",
        "title": "Example",
        "groupId": 7,
    })
    result = open_background("https://example.com", group="Agent-Test")

    # Wire shape: exactly one BrowserwrightDaemon.openBackgroundTab over sess.cdp.
    # The session id threads through as the plain CDP param ``bsSession``
    # (None here — the stub session is not bound to a ledger record).
    assert sess.cdp.calls == [
        ("BrowserwrightDaemon.openBackgroundTab",
         {"session": None, "url": "https://example.com",
          "groupName": "Agent-Test", "bsSession": None}),
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


def test_open_background_derives_group_and_bssession_from_ledger(
    tmp_bs_home, monkeypatch
):
    """open_background(url) with no explicit group, on a session named
    --name=cf-bots, must carry groupName='cf-bots' and bsSession=<sid> on the
    wire (derived from BD_SESSION → ledger)."""
    from browserwright import session_registry as reg
    from browserwright.primitives.page import open_background

    sid = reg.allocate(backend="extension", daemon_endpoint="default",
                       owner="attach", name="cf-bots")
    monkeypatch.setenv("BD_SESSION", sid)

    sess = _stub_session_for_ws(monkeypatch, response={
        "sessionId": "ws-sid-1", "targetId": "ext-tab-1", "tabId": 1,
        "url": "https://x.test", "title": "X", "groupId": 3,
    })
    open_background("https://x.test")

    method, params = sess.cdp.calls[0]
    assert method == "BrowserwrightDaemon.openBackgroundTab"
    assert params["groupName"] == "cf-bots"
    assert params["bsSession"] == sid


def test_close_tab_uses_long_lived_ws_not_subprocess(monkeypatch):
    """close_tab() must dispatch BrowserwrightDaemon.closeTab over sess.cdp.send."""
    from browserwright.primitives.page import close_tab

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
        ("BrowserwrightDaemon.closeTab",
         {"session": None, "sessionId": "ws-sid-99",
          "targetId": "ext-tab-99"}),
    ], f"unexpected wire calls: {sess.cdp.calls!r}"
    assert result == {"ok": True, "tabId": 99}
    # Local state is cleaned up after a successful close.
    assert "ext-tab-99" not in sess.cdp._sessions
    assert sess.current_target_id is None


def test_attached_session_raises_on_extension_without_attach(monkeypatch):
    """On extension backend, _attached_session() must refuse to silently
    auto-attach the user's focused tab — raise NeedsUserConfirm with both
    open_background AND attach_active named, with open_background listed
    FIRST (the new default rule)."""
    from browserwright.errors import NeedsUserConfirm
    from browserwright.primitives.interact import _attached_session

    _stub_session_for_ws(monkeypatch, backend="extension")  # no target attached
    with pytest.raises(NeedsUserConfirm) as exc_info:
        _attached_session()
    proposal = exc_info.value.proposal or ""
    assert "open_background" in proposal
    assert "attach_active" in proposal
    # Default rule: open_background listed before attach_active.
    assert proposal.index("open_background") < proposal.index("attach_active")


def test_attached_session_auto_attaches_on_rdp(monkeypatch):
    """On rdp/env backends (isolated Chrome), _attached_session() may still
    auto-attach via current_page() — no user collision there."""
    from browserwright.primitives.interact import _attached_session

    sess = _stub_session_for_ws(monkeypatch, backend="rdp")
    # Pre-seed a current_target_id so current_page() short-circuits and
    # _attached_session returns the cached sid. (We're asserting the
    # extension-only branch does NOT fire here, not full rdp behaviour.)
    sess.current_target_id = "rdp-target-1"
    sid = _attached_session()
    assert sid == "sid-cached"  # from _StubCDP.attach default
