from __future__ import annotations

import base64
import sys
import threading
import types
from collections import deque
from pathlib import Path

import pytest

from browserwright.errors import CDPError, ElementNotFound, NeedsUserConfirm


_ONE_BY_ONE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lT6k"
    "9QAAAABJRU5ErkJggg=="
)


class _FakeCDP:
    _closed = False

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []
        self.attached: list[str] = []
        self._sessions: dict[str, str] = {}
        self._events: dict[str | None, deque] = {}

    def attach(self, target_id: str) -> str:
        self.attached.append(target_id)
        sid = self._sessions.setdefault(target_id, f"sid-{target_id}")
        self._events.setdefault(sid, deque(maxlen=8))
        return sid

    def attach_readonly(self, target_id: str) -> str:
        return f"readonly-{target_id}"

    def send(self, method: str, *, session: str | None = None, **params):
        self.calls.append((method, {"session": session, **params}))
        response = self.responses.get(method, {})
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(method=method, session=session, **params)
        return response

    def drain_events(self, *, session: str | None = None):
        buf = self._events.get(session)
        if not buf:
            return []
        out = list(buf)
        buf.clear()
        return out


class _StubDaemon:
    def resolve_ws_url(self):
        raise AssertionError("daemon should not be touched")

    def invalidate(self):
        pass


@pytest.fixture
def fake_session(fresh_modules):
    from browserwright.session import Session, with_session

    sess = Session(daemon=_StubDaemon())
    sess.current_target_id = "target-1"
    sess._cdp = _FakeCDP()  # type: ignore[attr-defined]
    with with_session(sess):
        yield sess, sess._cdp


def test_inspect_cdp_attaches_current_target_by_default(fake_session):
    from browserwright.primitives.inspect import cdp

    sess, fake = fake_session
    out = cdp("Runtime.evaluate", expression="1 + 1")

    assert out == {}
    assert fake.attached == [sess.current_target_id]
    assert fake.calls[-1] == (
        "Runtime.evaluate",
        {"session": "sid-target-1", "expression": "1 + 1"},
    )


def test_inspect_cdp_honors_explicit_session_id(fake_session):
    from browserwright.primitives.inspect import cdp

    _, fake = fake_session
    cdp("DOM.getDocument", session_id="readonly-1", depth=-1)

    assert fake.attached == []
    assert fake.calls[-1] == (
        "DOM.getDocument",
        {"session": "readonly-1", "depth": -1},
    )


def test_page_info_delegates_to_js(monkeypatch):
    from browserwright.primitives import interact
    from browserwright.primitives.inspect import page_info

    seen = {}

    def fake_js(code):
        seen["code"] = code
        return {"url": "https://example.test/", "ready": "complete"}

    monkeypatch.setattr(interact, "js", fake_js)

    assert page_info()["ready"] == "complete"
    assert "document.readyState" in seen["code"]
    assert "window.innerWidth" in seen["code"]


def test_capture_screenshot_annotates_writes_and_always_clears(
    fake_session, monkeypatch, tmp_path
):
    from browserwright.primitives import inspect as inspect_mod

    _, fake = fake_session
    fake.responses["Page.captureScreenshot"] = {
        "data": base64.b64encode(_ONE_BY_ONE_PNG).decode()
    }
    cleared = []
    legend = [{"n": 0, "role": "button", "name": "Save", "x": 10, "y": 20}]
    monkeypatch.setattr(inspect_mod, "_draw_set_of_mark", lambda: (legend, "paint failed"))
    monkeypatch.setattr(inspect_mod, "_clear_set_of_mark", lambda: cleared.append(True))

    out = inspect_mod.capture_screenshot(
        str(tmp_path / "shot.png"), full=True, annotate=True
    )

    assert out == {
        "path": str(tmp_path / "shot.png"),
        "legend": legend,
        "mark_error": "paint failed",
    }
    assert Path(out["path"]).read_bytes() == _ONE_BY_ONE_PNG
    assert cleared == [True]
    assert fake.calls[-1] == (
        "Page.captureScreenshot",
        {"session": "sid-target-1", "format": "png", "captureBeyondViewport": True},
    )


def test_draw_set_of_mark_keeps_legend_when_overlay_js_fails(monkeypatch):
    from browserwright.primitives import inspect as inspect_mod
    from browserwright.primitives import interact

    monkeypatch.setattr(
        inspect_mod,
        "snapshot",
        lambda text=False: {
            "nodes": [{"role": "link", "name": "Docs", "x": 7, "y": 9}]
        },
    )
    monkeypatch.setattr(interact, "js", lambda _code: (_ for _ in ()).throw(RuntimeError("no dom")))

    legend, err = inspect_mod._draw_set_of_mark()

    assert legend == [{"n": 0, "role": "link", "name": "Docs", "x": 7, "y": 9}]
    assert err == "RuntimeError: no dom"


def test_snapshot_injects_options_and_formats_text(monkeypatch):
    from browserwright.primitives import interact
    from browserwright.primitives.inspect import snapshot

    captured = {}

    def fake_js(code):
        captured["code"] = code
        return {
            "nodes": [
                {
                    "role": "checkbox",
                    "name": "Subscribe",
                    "x": 3,
                    "y": 4,
                    "type": "checkbox",
                    "checked": False,
                    "disabled": True,
                    "href": "https://example.test/",
                    "frame": "iframe#1",
                }
            ]
        }

    monkeypatch.setattr(interact, "js", fake_js)

    out = snapshot(
        interactive_only=False,
        max_nodes=5,
        max_depth=2,
        scope="#main",
        include_href=False,
    )

    assert '"interactiveOnly": false' in captured["code"]
    assert '"maxNodes": 5' in captured["code"]
    assert '"scope": "#main"' in captured["code"]
    assert out["text"] == (
        '[0] checkbox "Subscribe" (3,4) type=checkbox disabled '
        "checked=False href=https://example.test/ iframe#1"
    )


def test_describe_page_injects_numeric_options(monkeypatch):
    from browserwright.primitives import interact
    from browserwright.primitives.inspect import describe_page

    captured = {}

    def fake_js(code):
        captured["code"] = code
        return {}

    monkeypatch.setattr(interact, "js", fake_js)

    assert describe_page(max_nodes=3, max_vars=4, min_area_frac=0.2, viewport_only=True) == {}
    assert '"maxNodes": 3' in captured["code"]
    assert '"maxVars": 4' in captured["code"]
    assert '"minAreaFrac": 0.2' in captured["code"]
    assert '"viewportOnly": true' in captured["code"]


def test_diff_snapshot_reports_added_removed_changed_and_caps_items():
    from browserwright.primitives.inspect import diff_snapshot

    before = {
        "nodes": [
            {"role": "button", "name": "Save", "x": 10, "y": 10, "disabled": False},
            {"role": "link", "name": "Old", "x": 100, "y": 10, "href": "/old"},
        ]
    }
    after = {
        "nodes": [
            {"role": "button", "name": "Save", "x": 40, "y": 10, "disabled": True},
            {"role": "link", "name": "New", "x": 200, "y": 10, "href": "/new"},
        ]
    }

    out = diff_snapshot(before, after, max_items=1, bucket=64)

    assert out["summary"] == "1 added, 1 removed, 1 changed"
    assert out["added"] == [{"role": "link", "name": "New", "x": 200, "y": 10, "href": "/new"}]
    assert out["removed"] == [{"role": "link", "name": "Old", "x": 100, "y": 10, "href": "/old"}]
    assert out["changed"][0]["changes"] == {"disabled": [False, True]}
    assert out["changed"][0]["moved"] == [[10, 10], [40, 10]]


def test_attached_session_uses_recovered_target(monkeypatch):
    from browserwright.primitives import interact
    from browserwright.session import Session, with_session

    sess = Session(daemon=_StubDaemon())
    sess._cdp = _FakeCDP()  # type: ignore[attr-defined]
    sess.current_target_id = None

    def recover(session):
        session.current_target_id = "recovered"
        return True

    monkeypatch.setattr("browserwright.session_runtime.ensure_session_target", recover)
    monkeypatch.setattr(
        "browserwright.primitives.page.current_page",
        lambda: (_ for _ in ()).throw(AssertionError("current_page not expected")),
    )

    with with_session(sess):
        assert interact._attached_session() == "sid-recovered"


def test_js_raw_undefined_returns_none(fake_session):
    from browserwright.primitives.interact import js

    _, fake = fake_session
    fake.responses["Runtime.evaluate"] = {"result": {"type": "undefined"}}

    assert js("return 1", raw=True) is None
    assert fake.calls[-1][1]["expression"] == "return 1"


def test_js_exception_details_surface_description(fake_session):
    from browserwright.primitives.interact import js

    _, fake = fake_session
    fake.responses["Runtime.evaluate"] = {
        "exceptionDetails": {
            "text": "Uncaught",
            "exception": {"description": "ReferenceError: nope is not defined"},
        }
    }

    with pytest.raises(CDPError, match="ReferenceError"):
        js("return nope")


def test_click_at_xy_repeats_mouse_sequence(fake_session, monkeypatch):
    from browserwright.primitives.interact import click_at_xy

    _, fake = fake_session
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    assert click_at_xy(1, 2, button="right", clicks=2) == {
        "x": 1,
        "y": 2,
        "button": "right",
        "clicks": 2,
    }
    mouse_events = [p for m, p in fake.calls if m == "Input.dispatchMouseEvent"]
    assert [event["type"] for event in mouse_events] == [
        "mousePressed",
        "mouseReleased",
        "mousePressed",
        "mouseReleased",
    ]
    assert all(event["button"] == "right" for event in mouse_events)


def test_fill_input_timeout_miss_raises_element_not_found(monkeypatch):
    from browserwright.primitives import interact

    monkeypatch.setattr(interact, "wait_for_element", lambda selector, timeout: False)

    with pytest.raises(ElementNotFound):
        interact.fill_input("#late", "text", timeout=0.1)


def test_wait_for_element_visible_ignores_cdp_errors_then_succeeds(monkeypatch):
    from browserwright.primitives import interact

    expressions = []
    outcomes = [
        CDPError(method="Runtime.evaluate", cdp_message="navigating"),
        True,
    ]

    def fake_js(expression):
        expressions.append(expression)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(interact, "js", fake_js)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    assert interact.wait_for_element("#ready", timeout=1.0, visible=True) is True
    assert "checkVisibility" in expressions[0]
    assert "getComputedStyle" in expressions[0]


def test_drain_events_without_current_target_drains_root_session():
    from browserwright.primitives import interact
    from browserwright.session import Session, with_session

    sess = Session(daemon=_StubDaemon())
    fake = _FakeCDP()
    fake._events[None] = deque([{"method": "Target.targetCreated"}], maxlen=8)
    sess._cdp = fake  # type: ignore[attr-defined]
    sess.current_target_id = None

    with with_session(sess):
        assert interact.drain_events() == [{"method": "Target.targetCreated"}]
        assert interact.drain_events() == []


def test_wait_for_network_idle_returns_false_when_timeout_is_immediate(fake_session):
    from browserwright.primitives.interact import wait_for_network_idle

    assert wait_for_network_idle(timeout=0.0) is False


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Cannot access contents of url chrome://settings", True),
        ("Cannot attach to this target (devtools://devtools)", True),
        ("Cannot access https://example.test", False),
        ("daemon unavailable", False),
        (None, False),
    ],
)
def test_nonattachable_internal_url_error_detection(message, expected):
    from browserwright.primitives.page import _is_nonattachable_internal_url_error

    assert _is_nonattachable_internal_url_error(message) is expected


def test_attach_active_success_registers_session_and_enables_domains(monkeypatch):
    from browserwright.primitives.page import attach_active
    from browserwright.session import Session, with_session

    fake = _FakeCDP(
        {
            "BrowserwrightDaemon.attachActiveTab": {
                "targetId": "attached",
                "sessionId": "sid-attached",
                "tabId": 12,
                "url": "https://example.test/",
                "title": "Example",
            },
            "DOM.enable": CDPError(method="DOM.enable", cdp_message="ignored"),
        }
    )
    sess = Session(daemon=_StubDaemon())
    sess._cdp = fake  # type: ignore[attr-defined]
    persisted = []
    monkeypatch.setattr(
        "browserwright.session_runtime.persist_target",
        lambda target_id, **kwargs: persisted.append((target_id, kwargs)),
    )

    with with_session(sess):
        out = attach_active()

    assert out == {
        "targetId": "attached",
        "tabId": 12,
        "url": "https://example.test/",
        "title": "Example",
    }
    assert sess.current_target_id == "attached"
    assert fake._sessions["attached"] == "sid-attached"
    assert persisted == [("attached", {"sess": sess})]
    assert [m for m, _ in fake.calls[-4:]] == [
        "Page.enable",
        "Runtime.enable",
        "DOM.enable",
        "Network.enable",
    ]


@pytest.mark.parametrize(
    "payload, message",
    [
        ({}, "empty response"),
        ({"targetId": "t"}, "malformed daemon response"),
    ],
)
def test_attach_active_rejects_empty_or_malformed_daemon_payload(payload, message):
    from browserwright.primitives.page import attach_active
    from browserwright.session import Session, with_session

    sess = Session(daemon=_StubDaemon())
    sess._cdp = _FakeCDP({"BrowserwrightDaemon.attachActiveTab": payload})  # type: ignore[attr-defined]

    with with_session(sess), pytest.raises(CDPError, match=message):
        attach_active()


def test_attach_readonly_delegates_to_cdp(fake_session):
    from browserwright.primitives.page import attach_readonly

    assert attach_readonly("target-2") == "readonly-target-2"


def test_current_tab_uses_recovered_target(monkeypatch, fake_session):
    from browserwright.primitives import page

    sess, _ = fake_session
    sess.current_target_id = None

    def recover(session):
        session.current_target_id = "recovered"
        return True

    monkeypatch.setattr("browserwright.session_runtime.ensure_session_target", recover)
    monkeypatch.setattr(
        page,
        "list_tabs",
        lambda include_chrome=True: [
            {
                "targetId": "recovered",
                "url": "https://example.test/",
                "title": "Recovered",
                "attached": True,
            }
        ],
    )

    assert page.current_tab()["title"] == "Recovered"


def test_goto_url_switches_to_existing_tab_before_navigating(monkeypatch, fake_session):
    from browserwright.primitives import page

    sess, fake = fake_session
    sess.current_target_id = None
    switched = []
    monkeypatch.setattr(
        page,
        "list_tabs",
        lambda include_chrome=True: [
            {"targetId": "real", "url": "https://start.test/", "title": "Start"}
        ],
    )

    def fake_switch(tab):
        switched.append(tab)
        sess.current_target_id = tab["targetId"]
        return {"targetId": tab["targetId"]}

    monkeypatch.setattr(page, "switch_tab", fake_switch)

    assert page.goto_url("https://next.test/") == {"url": "https://next.test/"}
    assert switched == [{"targetId": "real", "url": "https://start.test/", "title": "Start"}]
    assert fake.calls[-1] == (
        "Page.navigate",
        {"session": "sid-real", "url": "https://next.test/"},
    )


def test_goto_url_opens_new_tab_when_no_existing_tab(monkeypatch, fake_session):
    from browserwright.primitives import page

    sess, _ = fake_session
    sess.current_target_id = None
    monkeypatch.setattr(page, "list_tabs", lambda include_chrome=True: [])
    monkeypatch.setattr(page, "new_tab", lambda url: {"targetId": "new", "url": url})

    assert page.goto_url("https://fresh.test/") == {
        "targetId": "new",
        "url": "https://fresh.test/",
    }


def test_goto_url_wraps_navigation_cdp_error(fake_session):
    from browserwright.errors import PageLoadFailed
    from browserwright.primitives.page import goto_url

    _, fake = fake_session
    fake.responses["Page.navigate"] = CDPError(
        method="Page.navigate", cdp_message="navigation failed"
    )

    with pytest.raises(PageLoadFailed, match="navigation failed"):
        goto_url("https://bad.test/")


def test_reload_uses_hard_flag_and_returns_page_info(monkeypatch, fake_session):
    from browserwright.primitives import page

    _, fake = fake_session
    monkeypatch.setattr(page, "wait_for_load", lambda: True)
    monkeypatch.setattr("browserwright.primitives.inspect.page_info", lambda: {"ready": "complete"})

    assert page.reload(hard=True) == {"ready": "complete"}
    assert fake.calls[-1] == (
        "Page.reload",
        {"session": "sid-target-1", "ignoreCache": True},
    )


def test_current_page_returns_recovered_target_even_before_it_appears_in_targets(
    monkeypatch, fake_session
):
    from browserwright.primitives import page

    sess, _ = fake_session
    sess.current_target_id = "stale"
    monkeypatch.setattr(page, "list_tabs", lambda include_chrome=True: [])
    monkeypatch.setattr("browserwright.session_runtime.ensure_session_target", lambda sess: "recovered")

    assert page.current_page() == {"targetId": "recovered", "accuracy": "exact"}
    assert sess.current_target_id is None


def test_close_tab_without_current_target_is_actionable():
    from browserwright.primitives.page import close_tab
    from browserwright.session import Session, with_session

    sess = Session(daemon=_StubDaemon())
    sess._cdp = _FakeCDP()  # type: ignore[attr-defined]
    sess.current_target_id = None

    with with_session(sess), pytest.raises(CDPError, match="no current attached tab"):
        close_tab()


@pytest.mark.parametrize(
    "payload, message",
    [
        ({}, "empty payload"),
        ({"targetId": "t"}, "incomplete payload"),
    ],
)
def test_open_rejects_empty_or_incomplete_payload(payload, message, monkeypatch):
    from browserwright.primitives import page
    from browserwright.session import Session, with_session

    sess = Session(daemon=_StubDaemon())
    sess._cdp = _FakeCDP({"BrowserwrightDaemon.openBackgroundTab": payload})  # type: ignore[attr-defined]
    monkeypatch.setattr(page, "_session_name_and_id", lambda sess: ("name", "sid"))

    with with_session(sess), pytest.raises(CDPError, match=message):
        page.open("https://example.test/")


def test_resolve_host_infers_from_current_tab(monkeypatch, fake_session):
    from browserwright.primitives import page, site

    sess, _ = fake_session
    sess.current_target_id = "target-1"
    monkeypatch.setattr(
        page,
        "list_tabs",
        lambda: [
            {
                "targetId": "target-1",
                "url": "https://news.ycombinator.com/item?id=1",
                "title": "HN",
            }
        ],
    )

    assert site._resolve_host(None) == "ycombinator.com"


def test_remember_preference_confirm_requires_user_assent():
    from browserwright.primitives.site import remember_preference

    with pytest.raises(NeedsUserConfirm) as exc:
        remember_preference("theme", "dark")

    assert exc.value.proposal == {"key": "theme", "value": "dark"}


def test_memory_read_includes_explicit_site(tmp_bs_home, fresh_modules):
    from browserwright.primitives.site import memory_read, remember

    remember("https://docs.example.test/path", "offline coverage note")

    out = memory_read("docs.example.test")

    assert "global" in out
    assert out["current_site"]["site"] == "example.test"
    assert "offline coverage note" in out["current_site"]["data"]["body"]


def test_http_get_uses_fetch_use_proxy_when_api_key_is_set(monkeypatch):
    from browserwright.primitives.http import http_get

    calls = []

    def fetch_sync(url, **kwargs):
        calls.append((url, kwargs))
        return types.SimpleNamespace(text="proxied")

    monkeypatch.setenv("BROWSER_USE_API_KEY", "key")
    monkeypatch.setitem(sys.modules, "fetch_use", types.SimpleNamespace(fetch_sync=fetch_sync))

    assert http_get("https://example.test/", headers={"X-Test": "1"}, timeout=1.25) == "proxied"
    assert calls == [
        (
            "https://example.test/",
            {"headers": {"X-Test": "1"}, "timeout_ms": 1250},
        )
    ]


def test_unix_socket_adapter_ignores_tcp_options():
    import socket
    from browserwright.cdp import _UnixSocketAdapter

    raw = types.SimpleNamespace(calls=[], fileno=lambda: 123)

    def setsockopt(level, optname, value):
        raw.calls.append((level, optname, value))

    raw.setsockopt = setsockopt
    adapter = _UnixSocketAdapter(raw)

    assert adapter.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True) is None
    adapter.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, True)
    assert raw.calls == [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, True)]
    assert adapter.fileno() == 123


def test_cdp_attach_reuses_cached_session_and_enables_domains():
    from browserwright.cdp import CDPSession

    cdp = object.__new__(CDPSession)
    cdp._sessions = {}
    cdp._events = {}
    calls = []

    def fake_send(method, **params):
        calls.append((method, params))
        if method == "Target.attachToTarget":
            return {"sessionId": "sid-1"}
        if method == "Runtime.enable":
            raise CDPError(method=method, cdp_message="ignored")
        return {}

    cdp.send = fake_send

    assert CDPSession.attach(cdp, "target") == "sid-1"
    assert CDPSession.attach(cdp, "target") == "sid-1"
    assert cdp._sessions == {"target": "sid-1"}
    assert list(cdp._events) == ["sid-1"]
    assert [method for method, _ in calls] == [
        "Target.attachToTarget",
        "Page.enable",
        "Runtime.enable",
        "DOM.enable",
        "Network.enable",
    ]


def test_cdp_drain_events_clears_buffer():
    from browserwright.cdp import CDPSession

    cdp = object.__new__(CDPSession)
    cdp._lock = threading.Lock()
    cdp._events = {"sid": deque([{"method": "Page.loadEventFired"}], maxlen=8)}

    assert CDPSession.drain_events(cdp, session="sid") == [{"method": "Page.loadEventFired"}]
    assert CDPSession.drain_events(cdp, session="sid") == []


def test_cdp_send_closed_raises_actionable_error():
    from browserwright.cdp import CDPSession

    cdp = object.__new__(CDPSession)
    cdp._closed = True
    cdp._closed_reason = "bye"

    with pytest.raises(CDPError, match="ws closed: bye"):
        CDPSession.send(cdp, "Runtime.evaluate", expression="1")
