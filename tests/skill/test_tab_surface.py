"""Unit tests for the agent-facing tab primitives ``tabs()`` / ``switch_tab()``.

These drive the AGENT CDP path with a fake ``Session`` (no daemon, no
Playwright), covering: enumeration shape, current-tab marking, URL-substring
resolution (unique / ambiguous / no-match), Page-object resolution via the
short-lived marker, and the ``bind_target`` delegation behind ``switch_tab``.
"""
from __future__ import annotations

import pytest

from browserwright.errors import CDPError
from browserwright.session import Session, with_session
from browserwright.tab_surface import TabMatchError


class _StubDaemon:
    def resolve_ws_url(self):
        raise AssertionError("daemon should not be touched")

    def invalidate(self):
        pass


class _FakeCDP:
    _closed = False

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []
        self.attached: list[str] = []
        self._sessions: dict[str, str] = {}

    def attach(self, target_id: str) -> str:
        self.attached.append(target_id)
        return self._sessions.setdefault(target_id, f"sid-{target_id}")

    def send(self, method: str, *, session: str | None = None, **params):
        self.calls.append((method, {"session": session, **params}))
        response = self.responses.get(method, {})
        if isinstance(response, list):
            response = response.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


TARGETS = {
    "targetInfos": [
        {
            "type": "page",
            "targetId": "target-form",
            "url": "https://app.example.com/signup",
            "title": "Signup",
            "attached": True,
        },
        {
            "type": "page",
            "targetId": "target-docs",
            "url": "https://docs.example.com/api-keys",
            "title": "API keys",
            "attached": True,
        },
        {
            "type": "page",
            "targetId": "target-blank",
            "url": "about:blank",
            "title": "",
            "attached": True,
        },
        {
            "type": "other",
            "targetId": "target-ignored",
            "url": "https://example.com/ignored",
            "title": "",
            "attached": False,
        },
    ]
}


@pytest.fixture
def fake_session(fresh_modules):
    sess = Session(daemon=_StubDaemon())
    sess.current_target_id = "target-form"
    fake = _FakeCDP({"Target.getTargets": TARGETS})
    sess._cdp = fake  # type: ignore[attr-defined]
    with with_session(sess):
        yield sess, fake


def test_tabs_shape_and_current_mark(fake_session):
    from browserwright import tabs

    sess, fake = fake_session
    out = tabs()
    # internal (about:blank) + non-page targets are filtered out
    assert [t["targetId"] for t in out] == ["target-form", "target-docs"]
    by_id = {t["targetId"]: t for t in out}
    assert by_id["target-form"]["current"] is True
    assert by_id["target-docs"]["current"] is False
    assert by_id["target-docs"]["url"] == "https://docs.example.com/api-keys"
    assert by_id["target-docs"]["title"] == "API keys"


def test_tabs_empty_when_no_targets(fake_session):
    from browserwright import tabs

    sess, fake = fake_session
    fake.responses["Target.getTargets"] = {"targetInfos": []}
    assert tabs() == []


def test_switch_tab_by_unique_url_substring(fake_session):
    from browserwright import switch_tab

    sess, fake = fake_session
    result = switch_tab("docs.example.com/api-keys")
    assert result == {"targetId": "target-docs"}
    assert sess.current_target_id == "target-docs"
    # attach + activateTarget happened through the agent CDP path
    assert fake.attached[-1] == "target-docs"
    assert fake.calls[-1] == (
        "Target.activateTarget", {"session": None, "targetId": "target-docs"},
    )


def test_switch_tab_url_match_is_case_insensitive(fake_session):
    from browserwright import switch_tab

    assert switch_tab("Docs.Example.COM/API")["targetId"] == "target-docs"


def test_switch_tab_no_match_lists_tabs(fake_session):
    from browserwright import switch_tab

    with pytest.raises(TabMatchError, match="no open tab matches"):
        switch_tab("stripe.com/pay")
    # current tab untouched
    assert fake_session[0].current_target_id == "target-form"


def test_switch_tab_ambiguous_match_errors(fake_session):
    from browserwright import switch_tab

    sess, fake = fake_session
    fake.responses["Target.getTargets"] = {
        "targetInfos": [
            {
                "type": "page",
                "targetId": "a",
                "url": "https://app.example.com/a",
                "title": "A",
                "attached": True,
            },
            {
                "type": "page",
                "targetId": "b",
                "url": "https://app.example.com/a?tab=2",
                "title": "B",
                "attached": True,
            },
        ]
    }
    with pytest.raises(TabMatchError, match="matches 2 tabs"):
        switch_tab("app.example.com/a")


def test_switch_tab_rejects_non_string_non_page(fake_session):
    from browserwright import switch_tab

    with pytest.raises(TabMatchError, match="URL substring"):
        switch_tab(42)


class _FakePage:
    """A Playwright-Page-shaped object whose marker check answers via URL."""

    def __init__(self, url: str):
        self.url = url
        self.evaluate = lambda *a, **k: None  # callable — duck-type Page


def test_switch_tab_page_object_match(fake_session):
    from browserwright import switch_tab

    sess, fake = fake_session
    fake.responses["Runtime.evaluate"] = {"result": {"value": True}}

    # A page that reads the marker installed on target-docs is resolved to it.
    hits: list[str] = []

    def marker_reads(marker_target):
        # pretend the given Page is the docs tab
        return marker_target == "target-docs"

    import browserwright.repl.playwright_handle as ph

    # Monkeypatch the marker helpers so the fake page "reads" the marker of
    # exactly one target.
    def fake_install(sess_, target_id):
        hits.append(target_id)
        return True, ("k", "v", None, "sid")

    def fake_read(page, key, value):
        return marker_reads(hits[-1])

    ph._install_target_marker = fake_install
    ph._page_has_target_marker = fake_read
    ph._clear_target_marker = lambda *a: None

    result = switch_tab(_FakePage("https://docs.example.com/api-keys"))
    assert result["targetId"] == "target-docs"
    assert sess.current_target_id == "target-docs"


def test_switch_tab_page_object_no_match(fake_session):
    from browserwright import switch_tab

    sess, fake = fake_session
    fake.responses["Runtime.evaluate"] = {"result": {"value": True}}

    import browserwright.repl.playwright_handle as ph

    ph._install_target_marker = lambda sess_, tid: (True, ("k", "v", None, "s"))
    ph._page_has_target_marker = lambda page, k, v: False
    ph._clear_target_marker = lambda *a: None

    with pytest.raises(TabMatchError, match="does not belong"):
        switch_tab(_FakePage("https://elsewhere.example.com/"))


def test_switch_tab_dead_target_surfaces_cdperror(fake_session):
    from browserwright import switch_tab

    sess, fake = fake_session

    def boom(target_id):
        raise CDPError(
            method="Target.attachToTarget", cdp_message="no such target")

    sess.cdp.attach = boom  # type: ignore[attr-defined]
    with pytest.raises(TabMatchError, match="could not attach"):
        switch_tab("docs.example.com/api-keys")


def test_switch_tab_no_tabs_at_all(fake_session):
    from browserwright import switch_tab

    sess, fake = fake_session
    fake.responses["Target.getTargets"] = {"targetInfos": []}
    with pytest.raises(TabMatchError, match="no open tabs"):
        switch_tab("anything")
