from __future__ import annotations

import gzip
import json
import sys
import threading
import types
from collections import deque

import pytest

from browserwright.errors import CDPError, ElementNotFound, PageLoadFailed


class _StubDaemon:
    def resolve_ws_url(self):
        raise AssertionError("daemon should not be touched")

    def invalidate(self):
        pass


class _FakeCDP:
    _closed = False

    def __init__(self, responses: dict | None = None, events=None):
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []
        self.attached: list[str] = []
        self._sessions: dict[str, str] = {}
        self._events: dict[str | None, deque] = {}
        self._drain_batches = list(events or [])

    def attach(self, target_id: str) -> str:
        self.attached.append(target_id)
        sid = self._sessions.setdefault(target_id, f"sid-{target_id}")
        self._events.setdefault(sid, deque(maxlen=16))
        return sid

    def send(self, method: str, *, session: str | None = None, **params):
        self.calls.append((method, {"session": session, **params}))
        response = self.responses.get(method, {})
        if isinstance(response, list):
            response = response.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(method=method, session=session, **params)
        return response

    def drain_events(self, *, session: str | None = None):
        if self._drain_batches:
            return self._drain_batches.pop(0)
        buf = self._events.get(session)
        if not buf:
            return []
        out = list(buf)
        buf.clear()
        return out


@pytest.fixture
def fake_session(fresh_modules):
    from browserwright.session import Session, with_session

    sess = Session(daemon=_StubDaemon())
    sess.current_target_id = "target-1"
    fake = _FakeCDP(
        {
            "Runtime.evaluate": {"result": {"value": True}},
            "DOM.getDocument": {"root": {"nodeId": 9}},
            "DOM.querySelector": {"nodeId": 0},
            "Target.getTargets": {
                "targetInfos": [
                    {
                        "type": "page",
                        "targetId": "target-1",
                        "url": "https://example.test/start",
                        "title": "Start",
                        "attached": True,
                    },
                    {
                        "type": "page",
                        "targetId": "chrome-1",
                        "url": "chrome://settings",
                        "title": "Settings",
                        "attached": False,
                    },
                ]
            },
        }
    )
    sess._cdp = fake  # type: ignore[attr-defined]
    with with_session(sess):
        yield sess, fake


def test_interact_js_error_and_serialization_paths(fake_session):
    from browserwright.primitives import interact

    _, fake = fake_session
    fake.responses["Runtime.evaluate"] = [
        {"result": {"value": 7}},
        {"result": {"type": "undefined"}},
        {
            "exceptionDetails": {
                "text": "Uncaught",
                "exception": {"description": "ReferenceError: missing"},
            }
        },
        {"result": {"type": "object", "description": "HTMLDivElement"}},
        CDPError(method="Runtime.evaluate", cdp_message="syntax blew up"),
        {"result": {"value": "frame-ok"}},
    ]

    assert interact.js("return 7") == 7
    assert fake.calls[-1][1]["expression"].startswith("(function(){ return 7")
    assert interact.js("return 8", raw=True) is None
    assert fake.calls[-1][1]["expression"] == "return 8"
    with pytest.raises(CDPError, match="ReferenceError: missing"):
        interact.js("return missing")
    with pytest.raises(CDPError, match="non-serializable JS result"):
        interact.js("document.body")
    with pytest.raises(CDPError) as exc:
        interact.js("return (")
    assert exc.value.params == {"expression": "return ("}
    assert exc.value.cdp_message == "syntax blew up"
    assert interact.js("return location.href", target_id="frame-1") == "frame-ok"
    assert fake.attached[-1] == "frame-1"


def test_interact_input_fill_click_upload_and_dispatch_paths(fake_session, monkeypatch):
    from browserwright.primitives import interact

    _, fake = fake_session
    monkeypatch.setattr(interact.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(interact, "wait_for_element", lambda selector, timeout: False)
    with pytest.raises(ElementNotFound):
        interact.fill_input("#late", "x", timeout=0.1)

    monkeypatch.setattr(interact, "wait_for_element", lambda selector, timeout: True)
    fake.responses["Runtime.evaluate"] = {"result": {"value": False}}
    with pytest.raises(ElementNotFound):
        interact.fill_input("#missing", "x", timeout=0.1)

    fake.calls.clear()
    fake.responses["Runtime.evaluate"] = {"result": {"value": True}}
    monkeypatch.setattr(interact.sys, "platform", "darwin")
    interact.click_at_xy(1.5, 2.5, button="right", clicks=2)
    interact.type_text("hello")
    interact.press_key("Enter", modifiers=8)
    interact.scroll(10, 20, dy=-99, dx=4)
    interact.fill_input("input[name=q]", "az", clear_first=True, timeout=0.1)
    interact.dispatch_key(".submit", key="Escape", event="keydown")

    mouse_types = [p["type"] for m, p in fake.calls if m == "Input.dispatchMouseEvent"]
    assert mouse_types == ["mousePressed", "mouseReleased", "mousePressed", "mouseReleased", "mouseWheel"]
    key_types = [p["type"] for m, p in fake.calls if m == "Input.dispatchKeyEvent"]
    assert "rawKeyDown" in key_types
    assert key_types.count("char") >= 3
    expressions = [p["expression"] for m, p in fake.calls if m == "Runtime.evaluate"]
    assert any("focus()" in expression for expression in expressions)
    assert any("KeyboardEvent" in expression and "keydown" in expression for expression in expressions)
    assert any("'input'" in expression and "'change'" in expression for expression in expressions)
    assert any(p.get("text") == "hello" for m, p in fake.calls if m == "Input.insertText")

    fake.responses["DOM.querySelector"] = {"nodeId": 0}
    with pytest.raises(ElementNotFound):
        interact.upload_file("#missing-file", "/tmp/nope")
    fake.responses["DOM.querySelector"] = {"nodeId": 42}
    interact.upload_file("input[type=file]", ["/tmp/a.txt", "/tmp/b.txt"])
    assert fake.calls[-1] == (
        "DOM.setFileInputFiles",
        {"session": "sid-target-1", "files": ["/tmp/a.txt", "/tmp/b.txt"], "nodeId": 42},
    )


def test_interact_waits_and_network_idle_cover_errors_and_timeout(fake_session, monkeypatch):
    from browserwright.primitives import interact

    _, fake = fake_session
    outcomes = [
        CDPError(method="Runtime.evaluate", cdp_message="navigating"),
        True,
        False,
        False,
    ]
    expressions = []

    def fake_js(expression):
        expressions.append(expression)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    real_monotonic = interact.time.monotonic
    monkeypatch.setattr(interact, "js", fake_js)
    monkeypatch.setattr(interact.time, "sleep", lambda _seconds: None)
    assert interact.wait_for_element("#ready", timeout=1, visible=True) is True
    assert "checkVisibility" in expressions[0]
    ticks = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(interact.time, "monotonic", lambda: next(ticks))
    assert interact.wait_for_element("#never", timeout=0.01) is False
    monkeypatch.setattr(interact.time, "monotonic", real_monotonic)

    fake._drain_batches = [
        [{"method": "Network.requestWillBeSent", "params": {"requestId": "r1"}}],
        [{"method": "Network.dataReceived", "params": {"requestId": "r1"}}],
        [{"method": "Network.loadingFailed", "params": {"requestId": "r1"}}],
        [],
    ]
    assert interact.wait_for_network_idle(timeout=1, idle_ms=0) is True
    assert interact.wait_for_network_idle(timeout=0) is False

    fake_session[0].current_target_id = None
    fake._drain_batches = []
    fake._events[None] = deque([{"method": "Target.targetCreated"}], maxlen=4)
    assert interact.drain_events() == [{"method": "Target.targetCreated"}]


def test_page_open_current_goto_reload_close_error_sweep(fake_session, monkeypatch):
    from browserwright.primitives import page

    sess, fake = fake_session
    monkeypatch.setattr(page, "_session_name_and_id", lambda sess: ("job", "sid-job"))
    monkeypatch.setattr("browserwright.session_runtime.register_recovered", lambda *a, **k: None)
    monkeypatch.setattr("browserwright.session_runtime.persist_target", lambda *a, **k: None)

    fake.responses["BrowserwrightDaemon.openBackgroundTab"] = CDPError(
        method="BrowserwrightDaemon.openBackgroundTab", cdp_message="daemon down"
    )
    with pytest.raises(CDPError, match="open failed: daemon down"):
        page.open("https://open-fails.test/")

    fake.responses["BrowserwrightDaemon.openBackgroundTab"] = {}
    with pytest.raises(CDPError, match="empty payload"):
        page.open("https://empty.test/")
    fake.responses["BrowserwrightDaemon.openBackgroundTab"] = {"targetId": "new"}
    with pytest.raises(CDPError, match="incomplete payload"):
        page.open("https://incomplete.test/")

    sess.current_target_id = "stale"
    monkeypatch.setattr(page, "list_tabs", lambda include_chrome=True: [])
    monkeypatch.setattr("browserwright.session_runtime.ensure_session_target", lambda sess: "recovered")
    assert page.current_page() == {"targetId": "recovered", "accuracy": "exact"}
    assert sess.current_target_id is None

    monkeypatch.setattr("browserwright.session_runtime.ensure_session_target", lambda sess: None)
    opened_payload = {"targetId": "fresh", "sessionId": "sid-fresh", "url": "about:blank"}
    fake.responses["BrowserwrightDaemon.openBackgroundTab"] = opened_payload
    assert page.current_page()["targetId"] == "fresh"

    sess.current_target_id = None
    monkeypatch.setattr(page, "list_tabs", lambda include_chrome=True: [])
    monkeypatch.setattr(page, "new_tab", lambda url: {"targetId": "brand-new", "url": url})
    assert page.goto_url("https://new.test/") == {"targetId": "brand-new", "url": "https://new.test/"}

    sess.current_target_id = "target-1"
    fake.responses["Page.navigate"] = CDPError(method="Page.navigate", cdp_message="blocked")
    with pytest.raises(PageLoadFailed, match="blocked"):
        page.goto_url("https://blocked.test/")

    monkeypatch.setattr("browserwright.primitives.inspect.cdp", lambda *a, **k: (_ for _ in ()).throw(
        CDPError(method="Page.reload", cdp_message="reload refused")
    ))
    with pytest.raises(PageLoadFailed, match="reload refused"):
        page.reload(hard=True)

    sess.current_target_id = None
    with pytest.raises(CDPError, match="no current attached tab"):
        page.close_tab()
    sess.current_target_id = "target-1"
    fake._sessions["target-1"] = "sid-target-1"
    fake.responses["BrowserwrightDaemon.closeTab"] = CDPError(
        method="BrowserwrightDaemon.closeTab", cdp_message="no backend"
    )
    with pytest.raises(CDPError, match="close_tab failed: no backend"):
        page.close_tab()
    fake.responses["BrowserwrightDaemon.closeTab"] = {}
    with pytest.raises(CDPError, match="empty close-tab payload"):
        page.close_tab(target_id="target-1")


def test_page_success_paths_current_goto_reload_and_close_cleanup(fake_session, monkeypatch):
    from browserwright.primitives import page

    sess, fake = fake_session
    switched = []
    monkeypatch.setattr(
        page,
        "list_tabs",
        lambda include_chrome=True: [
            {"targetId": "real", "url": "https://real.test/", "title": "Real", "attached": False}
        ],
    )

    def fake_switch(tab):
        switched.append(tab)
        sess.current_target_id = tab["targetId"]
        fake.attach(tab["targetId"])
        return {"targetId": tab["targetId"]}

    monkeypatch.setattr(page, "switch_tab", fake_switch)
    sess.current_target_id = None
    assert page.current_page()["accuracy"] == "unknown"
    assert switched[-1]["targetId"] == "real"
    assert page.goto_url("https://next.test/") == {"url": "https://next.test/"}

    monkeypatch.setattr("browserwright.primitives.inspect.cdp", lambda method, **kwargs: fake.calls.append((method, kwargs)) or {})
    monkeypatch.setattr("browserwright.primitives.inspect.page_info", lambda: {"ready": "complete"})
    monkeypatch.setattr(page, "wait_for_load", lambda: True)
    assert page.reload(hard=True) == {"ready": "complete"}
    assert fake.calls[-1] == ("Page.reload", {"session_id": "sid-real", "ignoreCache": True})

    fake.responses["BrowserwrightDaemon.closeTab"] = {"ok": False, "tabId": 55}
    fake._sessions["real"] = "sid-real"
    fake._events["sid-real"] = deque([{"method": "Network.requestWillBeSent"}])
    sess.current_target_id = "real"
    assert page.close_tab() == {"ok": False, "tabId": 55}
    assert "real" not in fake._sessions
    assert "sid-real" not in fake._events
    assert sess.current_target_id is None


def test_http_get_fetch_fallback_gzip_and_headers(monkeypatch):
    from browserwright.primitives.http import http_get

    def blocked_import(name, *args, **kwargs):
        if name == "fetch_use":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    class FakeResp:
        headers = {"Content-Encoding": "gzip"}

        def read(self):
            return gzip.compress(b"compressed ok")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    requests = []
    real_import = __import__
    monkeypatch.setenv("BROWSER_USE_API_KEY", "key")
    monkeypatch.setattr("builtins.__import__", blocked_import)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout: requests.append((req, timeout)) or FakeResp(),
    )

    assert http_get("https://example.test/", headers={"X-Test": "1"}, timeout=1.5) == "compressed ok"
    req, timeout = requests[0]
    assert timeout == 1.5
    assert req.headers["X-test"] == "1"
    assert req.headers["User-agent"] == "Mozilla/5.0"
    assert req.headers["Accept-encoding"] == "gzip"

    calls = []
    monkeypatch.setattr("builtins.__import__", real_import)
    monkeypatch.setitem(
        sys.modules,
        "fetch_use",
        types.SimpleNamespace(
            fetch_sync=lambda url, **kwargs: calls.append((url, kwargs)) or types.SimpleNamespace(text="proxied")
        ),
    )
    assert http_get("https://proxied.test/", timeout=2) == "proxied"
    assert calls == [("https://proxied.test/", {"headers": None, "timeout_ms": 2000})]


def test_site_memory_host_resolution_confirm_and_reads(tmp_bs_home, fake_session, monkeypatch):
    from browserwright.primitives import page, site
    from browserwright.errors import NeedsUserConfirm

    sess, _ = fake_session
    assert site._resolve_host("https://sub.example.co.uk/path") == "example.co.uk"

    sess.current_target_id = "target-1"
    monkeypatch.setattr(
        page,
        "list_tabs",
        lambda: [
            {"targetId": "target-1", "url": "https://docs.python.org/3/", "title": "Docs"},
            {"targetId": "other", "url": "https://ignored.test/", "title": "Other"},
        ],
    )
    assert site._resolve_host(None) == "python.org"

    sess.current_target_id = None
    with pytest.raises(ValueError, match="no host given"):
        site._resolve_host(None)
    with pytest.raises(NeedsUserConfirm) as exc:
        site.remember_preference("daemon.preferred_backend", "rdp")
    assert exc.value.proposal == {"key": "daemon.preferred_backend", "value": "rdp"}
    assert site.remember_preference("daemon.preferred_backend", "rdp", confirm=False) == {
        "key": "daemon.preferred_backend",
        "value": "rdp",
        "previous": None,
    }
    assert site.memory_read()["global"]["frontmatter"]["daemon"]["preferred_backend"] == "rdp"

    site.remember("https://docs.example.test/path", "offline dense note")
    out = site.memory_read("docs.example.test")
    assert out["current_site"]["site"] == "example.test"
    assert "offline dense note" in out["current_site"]["data"]["body"]


def test_cdp_send_attach_detach_readonly_close_and_read_loop():
    from browserwright.cdp import CDPSession

    cdp = CDPSession.__new__(CDPSession)
    cdp._lock = threading.Lock()
    cdp._inflight_cv = threading.Condition(cdp._lock)
    cdp._next_id = 1
    cdp._inflight = {}
    cdp._events = {None: deque(maxlen=4)}
    cdp._sessions = {}
    cdp._closed = False
    cdp._closed_reason = None
    sent = []

    class FakeWS:
        closed = 0

        def send(self, payload):
            frame = json.loads(payload)
            sent.append(frame)
            if frame["method"] == "Target.attachToTarget":
                result = {"sessionId": "sid-ro" if frame["params"].get("flags") else "sid-own"}
            elif frame["method"] == "Target.detachFromTarget":
                result = {"detached": True}
            else:
                result = {"ok": True}
            with cdp._inflight_cv:
                cdp._inflight[frame["id"]] = {"id": frame["id"], "result": result}
                cdp._inflight_cv.notify_all()

        def close(self):
            self.closed += 1

    cdp._ws = FakeWS()

    assert CDPSession.send(cdp, "Runtime.evaluate", session="sid", expression="1") == {"ok": True}
    assert sent[-1]["sessionId"] == "sid"
    assert CDPSession.attach_readonly(cdp, "target-ro") == "sid-ro"
    assert cdp._sessions == {}
    assert sent[-1]["params"]["flags"] == {"allowSecondaryReadOnly": True}
    assert CDPSession.attach(cdp, "target-1") == "sid-own"
    assert CDPSession.attach(cdp, "target-1") == "sid-own"
    assert [frame["method"] for frame in sent].count("Target.attachToTarget") == 2

    cdp._events["sid-own"].append({"method": "Page.loadEventFired"})
    cdp.detach("target-1")
    assert "target-1" not in cdp._sessions
    assert "sid-own" not in cdp._events

    cdp.close()
    cdp.close()
    assert cdp._closed is True
    assert cdp._ws.closed == 1

    reader = CDPSession.__new__(CDPSession)
    reader._ws = iter([
        "{bad json",
        json.dumps({"id": 99, "result": {"ok": True}}),
        json.dumps({"method": "Browser.event", "params": {"root": True}}),
        json.dumps({"sessionId": "sid-new", "method": "Runtime.consoleAPICalled"}),
    ])
    reader._lock = threading.Lock()
    reader._inflight_cv = threading.Condition(reader._lock)
    reader._inflight = {99: {}}
    reader._events = {None: deque(maxlen=4)}
    reader._closed = False
    reader._closed_reason = None

    reader._read_loop()
    assert reader._inflight[99]["result"] == {"ok": True}
    assert list(reader._events[None]) == [
        {"method": "Browser.event", "params": {"root": True}, "sessionId": None}
    ]
    assert list(reader._events["sid-new"]) == [
        {"method": "Runtime.consoleAPICalled", "params": {}, "sessionId": "sid-new"}
    ]
