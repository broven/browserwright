"""F-4 catch-up: 13 primitives + 3 Layer-3 re-exports (v0.5.1).

Each test stubs ``current_session().cdp`` so we can assert which CDP
methods get dispatched without needing a real Chrome. The contract here
is "calling ``X`` from the REPL surface sends the right CDP call(s)" —
the agent-facing contract that an integration / live test would also
need to satisfy.

Tests run entirely offline. No daemon, no Chrome, no popup risk.
"""
from __future__ import annotations

from typing import Any

import pytest


class _FakeCDP:
    """Stand-in for ``CDPSession`` — records every ``send()`` call and
    returns user-supplied responses for specific methods."""

    _closed = False  # ``Session.cdp`` property checks this attribute

    def __init__(self, responses: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._responses = responses or {}

    def send(self, method: str, *, session: str | None = None, **params) -> Any:
        self.calls.append((method, {"session": session, **params}))
        return self._responses.get(method, {})

    def attach(self, target_id: str) -> str:
        return f"sid-for-{target_id}"

    def drain_events(self, *, session: str | None = None) -> list[dict]:
        # populated per-test via .events
        evts = getattr(self, "events", [])
        self.events = []
        return evts


@pytest.fixture
def patched_session(monkeypatch, fresh_modules):
    """Yield a ``(session, fake_cdp)`` tuple where session.current_target_id
    is set + session.cdp is the recording fake."""
    from browserwright.session import Session, with_session

    sess = Session()
    sess.current_target_id = "target-test"
    fake = _FakeCDP(responses={
        "Runtime.evaluate": {"result": {"value": True}},
        "DOM.getDocument": {"root": {"nodeId": 1}},
        "DOM.querySelector": {"nodeId": 42},
        "Target.getTargets": {"targetInfos": [
            {"type": "page", "targetId": "page-1",
             "url": "https://example.com/", "title": "Real"},
            {"type": "page", "targetId": "chrome-1",
             "url": "chrome://settings", "title": "Settings"},
            {"type": "iframe", "targetId": "iframe-1",
             "url": "https://embed.example/widget", "title": ""},
        ]},
    })
    # Session.cdp is a lazy-init @property — patch the private slot.
    sess._cdp = fake  # type: ignore[attr-defined]
    sess._owns_cdp = False  # type: ignore[attr-defined]
    with with_session(sess):
        yield sess, fake


# ---- input primitives ------------------------------------------------


def test_type_text_dispatches_input_insertText(patched_session):
    from browserwright import type_text
    _, fake = patched_session
    type_text("hello")
    methods = [m for m, _ in fake.calls]
    assert "Input.insertText" in methods
    insert = next(p for m, p in fake.calls if m == "Input.insertText")
    assert insert["text"] == "hello"


def test_press_key_special_key_dispatches_keydown_and_keyup(patched_session):
    from browserwright import press_key
    _, fake = patched_session
    press_key("Enter")
    # Enter has text='\r' (single char) so the sequence is
    # keyDown + char + keyUp — listeners checking `e.key` see Enter and
    # `e.keyCode` sees 13.
    types = [p["type"] for m, p in fake.calls if m == "Input.dispatchKeyEvent"]
    assert types == ["keyDown", "char", "keyUp"]
    # Virtual keycode 13 surfaces for Enter listeners.
    payload = [p for m, p in fake.calls if m == "Input.dispatchKeyEvent"][0]
    assert payload["windowsVirtualKeyCode"] == 13
    assert payload["code"] == "Enter"


def test_press_key_modifier_only_no_char(patched_session):
    from browserwright import press_key
    _, fake = patched_session
    # Backspace has empty text → no 'char' event, just keyDown + keyUp.
    press_key("Backspace")
    types = [p["type"] for m, p in fake.calls if m == "Input.dispatchKeyEvent"]
    assert types == ["keyDown", "keyUp"]


def test_press_key_printable_char_emits_char_event(patched_session):
    from browserwright import press_key
    _, fake = patched_session
    press_key("a")
    types = [p["type"] for m, p in fake.calls if m == "Input.dispatchKeyEvent"]
    # Printable char gets keyDown + char + keyUp (the 'char' is what makes
    # the keyboard listener see a real "a" typed).
    assert types == ["keyDown", "char", "keyUp"]


def test_scroll_dispatches_mousewheel(patched_session):
    from browserwright import scroll
    _, fake = patched_session
    scroll(100, 200, dy=-500)
    wheels = [p for m, p in fake.calls if m == "Input.dispatchMouseEvent"
              and p["type"] == "mouseWheel"]
    assert len(wheels) == 1
    assert wheels[0]["x"] == 100.0
    assert wheels[0]["y"] == 200.0
    assert wheels[0]["deltaY"] == -500.0


def test_fill_input_raises_element_not_found_when_missing(patched_session):
    from browserwright import fill_input
    from browserwright.errors import ElementNotFound
    sess, fake = patched_session
    # js() round-trip returns the Runtime.evaluate result.value — set
    # to False so the focus() returns false.
    fake._responses["Runtime.evaluate"] = {"result": {"value": False}}
    with pytest.raises(ElementNotFound):
        fill_input("#nope", "text")


def test_fill_input_dispatches_input_change_events_on_success(patched_session):
    from browserwright import fill_input
    sess, fake = patched_session
    fake._responses["Runtime.evaluate"] = {"result": {"value": True}}
    fill_input("input[name=q]", "hi", clear_first=False)
    # Should have evaluated the focus + final dispatch JS expressions.
    evals = [p["expression"] for m, p in fake.calls if m == "Runtime.evaluate"]
    assert any("focus()" in e for e in evals)
    assert any("'input'" in e and "'change'" in e for e in evals)


def test_dispatch_key_runs_js_with_keyboardevent(patched_session):
    from browserwright import dispatch_key
    _, fake = patched_session
    dispatch_key(".submit", key="Enter")
    evals = [p["expression"] for m, p in fake.calls if m == "Runtime.evaluate"]
    assert any("KeyboardEvent" in e and ".submit" in e for e in evals)


def test_upload_file_dispatches_setFileInputFiles(patched_session):
    from browserwright import upload_file
    sess, fake = patched_session
    upload_file("input[type=file]", "/tmp/test.png")
    methods = [m for m, _ in fake.calls]
    assert "DOM.getDocument" in methods
    assert "DOM.querySelector" in methods
    assert "DOM.setFileInputFiles" in methods
    setf = next(p for m, p in fake.calls if m == "DOM.setFileInputFiles")
    assert setf["files"] == ["/tmp/test.png"]
    assert setf["nodeId"] == 42


def test_upload_file_raises_element_not_found(patched_session):
    from browserwright import upload_file
    from browserwright.errors import ElementNotFound
    sess, fake = patched_session
    fake._responses["DOM.querySelector"] = {"nodeId": 0}
    with pytest.raises(ElementNotFound):
        upload_file("#missing", "/tmp/x")


# ---- waiting + events ------------------------------------------------


def test_wait_for_element_returns_true_when_match(patched_session):
    from browserwright import wait_for_element
    _, fake = patched_session
    fake._responses["Runtime.evaluate"] = {"result": {"value": True}}
    assert wait_for_element("h1", timeout=0.5) is True


def test_wait_for_element_returns_false_on_timeout(patched_session):
    from browserwright import wait_for_element
    _, fake = patched_session
    fake._responses["Runtime.evaluate"] = {"result": {"value": False}}
    assert wait_for_element("#never", timeout=0.5) is False


def test_wait_for_network_idle_returns_true_when_no_events(patched_session,
                                                            monkeypatch):
    from browserwright import wait_for_network_idle
    _, fake = patched_session
    fake.events = []
    # No in-flight requests + no Network.* activity → idle immediately.
    monkeypatch.setattr("time.sleep", lambda _s: None)
    assert wait_for_network_idle(timeout=0.2, idle_ms=10) is True


def test_wait_for_network_idle_tracks_inflight_requests(patched_session,
                                                         monkeypatch):
    from browserwright import wait_for_network_idle
    _, fake = patched_session

    # First drain: a request is pending; second drain: it finishes.
    drain_seq = [
        [{"method": "Network.requestWillBeSent",
          "params": {"requestId": "r-1"}}],
        [{"method": "Network.loadingFinished",
          "params": {"requestId": "r-1"}}],
    ]

    def _patched_drain(self, *, session=None):
        return drain_seq.pop(0) if drain_seq else []

    monkeypatch.setattr(_FakeCDP, "drain_events", _patched_drain)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    assert wait_for_network_idle(timeout=2.0, idle_ms=10) is True


def test_drain_events_passes_through_to_cdp(patched_session):
    from browserwright import drain_events
    _, fake = patched_session
    fake.events = [{"method": "Page.frameAttached"}]
    out = drain_events()
    assert out == [{"method": "Page.frameAttached"}]


# ---- navigation extras -----------------------------------------------


def test_ensure_real_tab_picks_first_real_when_on_chrome_internal(
        patched_session, monkeypatch):
    from browserwright import ensure_real_tab
    from browserwright.primitives import page

    # current_tab returns a chrome:// page → ensure_real_tab should
    # switch to the first non-chrome tab.
    monkeypatch.setattr(page, "current_tab",
                        lambda: {"targetId": "chrome-1",
                                 "url": "chrome://settings"})
    out = ensure_real_tab()
    assert out is not None
    assert out["url"] == "https://example.com/"


def test_iframe_target_matches_substring(patched_session):
    from browserwright import iframe_target
    assert iframe_target("embed.example") == "iframe-1"
    assert iframe_target("never-matches") is None


# ---- HTTP escape hatch -----------------------------------------------


def test_http_get_returns_decoded_text(monkeypatch):
    """``http_get`` falls back to stdlib urllib when no proxy env is set."""
    from browserwright import http_get

    class _FakeResp:
        headers = {"Content-Encoding": ""}

        def read(self):
            return b"<html>ok</html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **kw: _FakeResp())
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)
    out = http_get("https://example.com/")
    assert "<html>ok</html>" in out


def test_http_get_decodes_gzip(monkeypatch):
    import gzip
    from browserwright import http_get

    class _FakeResp:
        headers = {"Content-Encoding": "gzip"}

        def read(self):
            return gzip.compress(b"hello compressed")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **kw: _FakeResp())
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)
    out = http_get("https://example.com/")
    assert out == "hello compressed"


# ---- Layer-3 re-exports ---------------------------------------------


def test_run_task_slash_form_normalises_site(monkeypatch, fresh_modules):
    """``run_task("site/name")`` and ``run_task("site", "name")`` both
    delegate to ``task_runner.run_task`` with the same args."""
    from browserwright.primitives import discovery_api

    captured: list[tuple] = []

    def _fake_run(site, name, **kw):
        captured.append((site, name, kw))
        return "ok"

    monkeypatch.setattr(discovery_api, "_run_task", _fake_run)
    discovery_api.run_task("github.com/list_issues", state="open")
    discovery_api.run_task("github.com", "list_issues", state="open")
    assert len(captured) == 2
    assert captured[0] == captured[1]
    assert captured[0] == ("github.com", "list_issues", {"state": "open"})


def test_run_task_rejects_bare_site(fresh_modules):
    from browserwright.primitives import discovery_api
    with pytest.raises(ValueError, match="task name"):
        discovery_api.run_task("github.com")


def test_list_site_skills_passes_through_filters(monkeypatch, fresh_modules):
    from browserwright.primitives import discovery_api

    captured = {}

    def _fake_list(*, site=None, query=None):
        captured["site"] = site
        captured["query"] = query
        return [{"site": "github.com", "name": "list_issues"}]

    monkeypatch.setattr(discovery_api, "list_tasks", _fake_list)
    out = discovery_api.list_site_skills(site="github.com", query="issue")
    assert captured == {"site": "github.com", "query": "issue"}
    assert out == [{"site": "github.com", "name": "list_issues"}]


def test_load_site_skill_imports_module(tmp_bs_home, fresh_modules):
    """End-to-end: bundled github.com/list_issues task module loads."""
    from browserwright.primitives import discovery_api

    mod = discovery_api.load_site_skill("github.com", "list_issues")
    assert hasattr(mod, "run")  # canonical task entry point
    assert callable(mod.run)
    # Bug 1 (v0.3.1) eTLD+1 normalisation still works — same module file.
    mod2 = discovery_api.load_site_skill("www.github.com/list_issues")
    assert hasattr(mod2, "run")
    assert mod2.run.__code__.co_filename == mod.run.__code__.co_filename
