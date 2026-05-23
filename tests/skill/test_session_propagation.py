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


def test_open_uses_long_lived_ws_not_subprocess(monkeypatch):
    """open() must dispatch the unified BrowserwrightDaemon.openBackgroundTab
    over sess.cdp.send (the long-lived ws), NOT via a CLI subprocess (which
    loses the sessionId binding on exit). De-branched: no groupName on the
    wire (the group is an internal daemon detail), and the result drops the
    extension-only groupId — uniform {targetId,tabId,url,title}."""
    from browserwright.primitives.page import open as open_tab

    sess = _stub_session_for_ws(monkeypatch, response={
        "sessionId": "ws-sid-1",
        "targetId": "ext-tab-42",
        "tabId": 42,
        "url": "https://example.com",
        "title": "Example",
        "groupId": 7,
    })
    result = open_tab("https://example.com")

    # Wire shape: exactly one openBackgroundTab over sess.cdp. The session id
    # threads through as the plain CDP param ``bsSession`` (None here — the
    # stub session is not bound to a ledger record). ``background`` defaults to
    # True; no ``groupName`` (the daemon derives the group from the session).
    assert sess.cdp.calls == [
        ("BrowserwrightDaemon.openBackgroundTab",
         {"session": None, "url": "https://example.com",
          "bsSession": None, "background": True}),
    ], f"unexpected wire calls: {sess.cdp.calls!r}"

    # The sid IS pre-registered in the local session map so a follow-up
    # cdp.attach(target_id) returns the same sid without re-attaching.
    assert sess.cdp._sessions["ext-tab-42"] == "ws-sid-1"

    # Uniform return shape; groupId is the session's tab-group id on extension
    # (the durable reconnect anchor used by recoverSession), -1 on rdp.
    assert result == {
        "targetId": "ext-tab-42",
        "tabId": 42,
        "url": "https://example.com",
        "title": "Example",
        "groupId": 7,
    }
    assert sess.current_target_id == "ext-tab-42"


def test_open_derives_bssession_from_ledger(tmp_bs_home, monkeypatch):
    """open(url) on a session bound via BD_SESSION → ledger must carry
    bsSession=<sid> on the wire so the daemon routes the tab into the right
    session's browser. The group is now an internal daemon detail derived from
    the session — the downstream no longer passes groupName."""
    from browserwright import session_registry as reg
    from browserwright.primitives.page import open as open_tab

    sid = reg.allocate(backend="extension",
                       owner="attach", name="cf-bots")
    monkeypatch.setenv("BD_SESSION", sid)

    sess = _stub_session_for_ws(monkeypatch, response={
        "sessionId": "ws-sid-1", "targetId": "ext-tab-1", "tabId": 1,
        "url": "https://x.test", "title": "X", "groupId": 3,
    })
    open_tab("https://x.test")

    method, params = sess.cdp.calls[0]
    assert method == "BrowserwrightDaemon.openBackgroundTab"
    assert params["bsSession"] == sid
    # The group is no longer part of the downstream wire contract.
    assert "groupName" not in params


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


def test_attached_session_auto_attaches_on_extension(monkeypatch):
    """De-branched (docs §Tier B): _attached_session() no longer refuses on the
    extension backend. With no tab bound it falls back through current_page() →
    open() (a NEW working tab, NOT adopt — so it never steals the user's focused
    tab), then returns the cached sid. No NeedsUserConfirm raise anymore."""
    from browserwright.primitives.interact import _attached_session

    sess = _stub_session_for_ws(monkeypatch, backend="extension", response={
        "sessionId": "ws-sid-1", "targetId": "ext-tab-1", "tabId": 1,
        "url": "https://x.test", "title": "X", "groupId": 3,
    })
    sid = _attached_session()
    # open() registered the daemon-minted (target, sid) pair, so re-attaching
    # the now-current target returns that same upstream sid (not a fresh one).
    assert sid == "ws-sid-1"
    assert sess.current_target_id == "ext-tab-1"
    # It went through the unified open verb, not a NeedsUserConfirm raise.
    assert any(call[0] == "BrowserwrightDaemon.openBackgroundTab"
               for call in sess.cdp.calls)


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
