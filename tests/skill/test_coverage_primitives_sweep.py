from __future__ import annotations

import gzip
import json
import sys
import threading
import types
from collections import deque

import pytest

from browserwright.errors import CDPError


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


def test_session_tabs_filters_internal_and_bind_target_activates(fake_session):
    from browserwright import session_runtime as rt

    sess, fake = fake_session
    all_tabs = rt.session_tabs(sess)
    assert [t["targetId"] for t in all_tabs] == ["target-1", "chrome-1"]
    real_tabs = rt.session_tabs(sess, include_internal=False)
    assert [t["targetId"] for t in real_tabs] == ["target-1"]

    assert rt.bind_target(sess, "target-1") == {"targetId": "target-1"}
    assert fake.attached[-1] == "target-1"
    assert sess.current_target_id == "target-1"
    assert fake.calls[-1] == (
        "Target.activateTarget", {"session": None, "targetId": "target-1"},
    )

    # activateTarget failures are tolerated (best-effort focus).
    fake.responses["Target.activateTarget"] = CDPError(
        method="Target.activateTarget", cdp_message="nope")
    assert rt.bind_target(sess, "target-1") == {"targetId": "target-1"}


def test_open_session_tab_error_and_success_paths(fake_session):
    from browserwright import session_runtime as rt

    sess, fake = fake_session
    fake.responses["BrowserwrightDaemon.openBackgroundTab"] = CDPError(
        method="BrowserwrightDaemon.openBackgroundTab", cdp_message="daemon down"
    )
    with pytest.raises(CDPError, match="open failed: daemon down"):
        rt.open_session_tab(sess, "https://open-fails.test/")

    fake.responses["BrowserwrightDaemon.openBackgroundTab"] = {}
    with pytest.raises(CDPError, match="empty payload"):
        rt.open_session_tab(sess, "https://empty.test/")

    fake.responses["BrowserwrightDaemon.openBackgroundTab"] = {"targetId": "new"}
    with pytest.raises(CDPError, match="incomplete payload"):
        rt.open_session_tab(sess, "https://incomplete.test/")

    fake.responses["BrowserwrightDaemon.openBackgroundTab"] = {
        "targetId": "fresh", "sessionId": "sid-fresh", "tabId": 7,
        "url": "https://ok.test/", "groupId": 3,
    }
    out = rt.open_session_tab(sess, "https://ok.test/")
    assert out == {
        "targetId": "fresh", "tabId": 7, "url": "https://ok.test/",
        "title": "", "groupId": 3,
    }
    # register_recovered wired the daemon-minted session into local state.
    assert sess.current_target_id == "fresh"
    assert fake._sessions["fresh"] == "sid-fresh"


def test_resolve_current_target_reuse_recover_adopt_open(fake_session, monkeypatch):
    from browserwright import session_runtime as rt

    sess, fake = fake_session

    # 1. Cached current target still live → exact.
    assert rt.resolve_current_target(sess)["accuracy"] == "exact"

    # 2. Stale cache + recovery finds a target not in the tab list.
    sess.current_target_id = "stale"
    monkeypatch.setattr(rt, "ensure_session_target", lambda s: "recovered")
    monkeypatch.setattr(rt, "session_tabs", lambda s, **k: [])
    assert rt.resolve_current_target(sess) == {
        "targetId": "recovered", "accuracy": "exact"}
    assert sess.current_target_id is None

    # 3. No recovery, an existing real tab is adopted (bound).
    monkeypatch.setattr(rt, "ensure_session_target", lambda s: None)
    monkeypatch.setattr(
        rt, "session_tabs",
        lambda s, **k: [{"targetId": "real", "url": "https://real.test/",
                         "title": "Real", "attached": False}],
    )
    out = rt.resolve_current_target(sess)
    assert out == {"targetId": "real", "url": "https://real.test/",
                   "title": "Real", "accuracy": "unknown"}
    assert sess.current_target_id == "real"

    # 4. Empty session → open a fresh working tab (NOT adopt).
    sess.current_target_id = None
    monkeypatch.setattr(rt, "session_tabs", lambda s, **k: [])
    fake.responses["BrowserwrightDaemon.openBackgroundTab"] = {
        "targetId": "fresh", "sessionId": "sid-fresh", "url": "about:blank",
    }
    out = rt.resolve_current_target(sess)
    assert out["targetId"] == "fresh"
    assert out["accuracy"] == "unknown"


def test_close_session_tab_error_paths_and_state_cleanup(fake_session):
    from browserwright import session_runtime as rt

    sess, fake = fake_session
    sess.current_target_id = None
    with pytest.raises(CDPError, match="no current attached tab"):
        rt.close_session_tab(sess)

    sess.current_target_id = "target-1"
    fake._sessions["target-1"] = "sid-target-1"
    fake.responses["BrowserwrightDaemon.closeTab"] = CDPError(
        method="BrowserwrightDaemon.closeTab", cdp_message="no backend"
    )
    with pytest.raises(CDPError, match="close tab failed: no backend"):
        rt.close_session_tab(sess)

    fake.responses["BrowserwrightDaemon.closeTab"] = {}
    with pytest.raises(CDPError, match="empty close-tab payload"):
        rt.close_session_tab(sess, target_id="target-1")

    fake.responses["BrowserwrightDaemon.closeTab"] = {"ok": False, "tabId": 55}
    fake._events["sid-target-1"] = deque([{"method": "Network.requestWillBeSent"}])
    assert rt.close_session_tab(sess) == {"ok": False, "tabId": 55}
    assert "target-1" not in fake._sessions
    assert "sid-target-1" not in fake._events
    assert sess.current_target_id is None


def test_eval_js_value_exception_and_no_tab_paths(fake_session, monkeypatch):
    from browserwright import session_runtime as rt

    sess, fake = fake_session
    fake.responses["Runtime.evaluate"] = {"result": {"value": "Title!"}}
    assert rt.eval_js(sess, "document.title") == "Title!"
    method, params = fake.calls[-1]
    assert method == "Runtime.evaluate"
    assert params["expression"] == "document.title"
    assert params["returnByValue"] is True

    fake.responses["Runtime.evaluate"] = {
        "exceptionDetails": {
            "text": "Uncaught",
            "exception": {"description": "ReferenceError: missing"},
        }
    }
    with pytest.raises(CDPError, match="ReferenceError: missing"):
        rt.eval_js(sess, "missing")

    sess.current_target_id = None
    monkeypatch.setattr(rt, "ensure_session_target", lambda s: None)
    with pytest.raises(CDPError, match="no current tab"):
        rt.eval_js(sess, "1 + 1")


def test_wait_for_ready_completes_and_times_out(fake_session, monkeypatch):
    from browserwright import session_runtime as rt

    sess, fake = fake_session
    fake.responses["Runtime.evaluate"] = {"result": {"value": "complete"}}
    assert rt.wait_for_ready(sess, timeout=1.0) is True

    fake.responses["Runtime.evaluate"] = {"result": {"value": "loading"}}
    monkeypatch.setattr(rt.time, "sleep", lambda _s: None)
    ticks = iter([0.0, 0.0, 99.0])
    monkeypatch.setattr(rt.time, "monotonic", lambda: next(ticks))
    assert rt.wait_for_ready(sess, timeout=0.01) is False


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
    from browserwright.primitives import site
    from browserwright.errors import NeedsUserConfirm

    sess, _ = fake_session
    assert site._resolve_host("https://sub.example.co.uk/path") == "example.co.uk"

    # Current-tab inference goes through session_runtime.session_tabs (the
    # fake CDP's Target.getTargets has target-1 → https://example.test/start).
    sess.current_target_id = "target-1"
    assert site._resolve_host(None) == "example.test"

    sess.current_target_id = None
    with pytest.raises(ValueError, match="no host given"):
        site._resolve_host(None)
    with pytest.raises(NeedsUserConfirm) as exc:
        site.remember_preference("daemon.preferred_backend", "rdp")
    assert exc.value.proposal == {"key": "daemon.preferred_backend", "value": "rdp"}
    assert "commit=True" in exc.value.fix
    assert site.remember_preference("daemon.preferred_backend", "rdp", confirm=False) == {
        "key": "daemon.preferred_backend",
        "value": "rdp",
        "previous": None,
    }
    before_body = site.memory_read()["global"]["body"]
    assert site.remember_preference("ui.theme", "dark", commit=True) == {
        "key": "ui.theme",
        "value": "dark",
        "previous": None,
    }
    assert site.memory_read()["global"]["body"] == before_body
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
