"""Dense offline coverage for Mode B client/session transport edges."""
from __future__ import annotations

import json
import socket
import subprocess
import threading
from collections import deque
from types import SimpleNamespace

import pytest


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_discover_normalizes_status_shapes_and_windows_port_fallback(tmp_path, monkeypatch):
    from browserwright.mode_b_client import ModeBClient
    import browserwright.mode_b_client as mb

    nested = {
        "alive": True,
        "endpoint": {"transport": "unix", "path": "/tmp/bw.sock", "extra": "drop"},
        "version": "ignored",
    }
    monkeypatch.setattr(mb.subprocess, "run", lambda *a, **k: _Proc(stdout=json.dumps(nested)))
    assert ModeBClient().discover() == {"transport": "unix", "path": "/tmp/bw.sock"}

    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "browserwright-daemon.port").write_text(
        json.dumps({"port": "8765", "token": "secret"}), encoding="utf-8"
    )
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing-runtime"))
    monkeypatch.setenv("TEMP", str(temp))
    monkeypatch.setattr(mb.subprocess, "run", lambda *a, **k: _Proc(stdout=json.dumps({"alive": False})))
    assert ModeBClient().discover() == {
        "transport": "tcp",
        "host": "127.0.0.1",
        "port": 8765,
        "token": "secret",
    }


def test_discover_ignores_bad_status_json_and_raises_on_bad_port_file(tmp_path, monkeypatch):
    from browserwright.errors import DaemonUnavailable
    from browserwright.mode_b_client import ModeBClient
    import browserwright.mode_b_client as mb

    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "browserwright-daemon.port").write_text(
        json.dumps({"port": "not-an-int", "token": "secret"}), encoding="utf-8"
    )
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing-runtime"))
    monkeypatch.setenv("TEMP", str(temp))
    monkeypatch.setattr(mb.subprocess, "run", lambda *a, **k: _Proc(stdout="{bad json"))

    with pytest.raises(DaemonUnavailable, match="no Mode B endpoint"):
        ModeBClient().discover()


def test_ping_socket_branches_close_on_failure(monkeypatch):
    from browserwright.mode_b_client import ModeBClient
    import browserwright.mode_b_client as mb

    instances = []

    class _FakeSocket:
        fail_unix = True

        def __init__(self, family, kind):
            self.family = family
            self.kind = kind
            self.timeout = None
            self.connected_to = None
            self.closed = False
            instances.append(self)

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            self.connected_to = address
            if self.family == socket.AF_UNIX and self.fail_unix:
                raise OSError("socket gone")

        def close(self):
            self.closed = True

    monkeypatch.setattr(mb.socket, "socket", _FakeSocket)
    client = ModeBClient()

    assert client._ping({"transport": "unix", "path": "/tmp/missing.sock"}) is False
    assert instances[-1].timeout == 1.5
    assert instances[-1].closed is True

    _FakeSocket.fail_unix = False
    assert client._ping({"transport": "tcp", "port": "9222"}) is True
    assert instances[-1].connected_to == ("127.0.0.1", 9222)
    assert instances[-1].closed is True


def test_ws_url_caches_until_invalidated_and_carries_session_query(monkeypatch):
    from browserwright.mode_b_client import ModeBClient

    client = ModeBClient()
    client._session_id = "s-42"
    endpoints = [
        {"transport": "unix", "path": "/tmp/first.sock"},
        {"transport": "tcp", "host": "127.0.0.9", "port": 9001, "token": "tok"},
    ]
    calls = []

    def discover():
        calls.append("discover")
        return endpoints.pop(0)

    monkeypatch.setattr(client, "discover", discover)

    assert client.ws_url(client_label="first") == "ws+unix:///tmp/first.sock?client=first&session=s-42"
    assert client.ws_url(client_label="second") == "ws+unix:///tmp/first.sock?client=first&session=s-42"
    assert calls == ["discover"]

    client.invalidate()
    assert client.ws_url(client_label="second") == (
        "ws://127.0.0.9:9001?token=tok&client=second&session=s-42"
    )
    assert calls == ["discover", "discover"]
    assert client._endpoint == "127.0.0.9:9001"
    assert client._transport == "tcp"
    assert client._token == "tok"


def test_cli_info_methods_parse_defaults_and_command_shapes(monkeypatch):
    from browserwright.mode_b_client import ModeBClient
    import browserwright.mode_b_client as mb

    outputs = deque(
        [
            _Proc(stdout=json.dumps({"backend": "rdp"})),
            _Proc(stdout=json.dumps({"targetId": "t1", "url": "https://e.test"})),
            _Proc(stdout=json.dumps({"sessionId": "sid", "targetId": "t1"})),
            _Proc(returncode=0),
            _Proc(stdout=json.dumps({"version": "1.2.3"})),
            _Proc(stdout="browserwright-daemon 9.8.7\n"),
        ]
    )
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append((cmd, kwargs))
        return outputs.popleft()

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    client = ModeBClient()
    client._session_id = "s-1"

    assert client.get_backend_info() == {"backend": "rdp"}
    assert client.active_tab() == {
        "targetId": "t1",
        "url": "https://e.test",
        "title": "",
        "accuracy": "unknown",
        "since_seconds": None,
    }
    assert client.attach_active() == {"sessionId": "sid", "targetId": "t1"}
    assert client.disconnect_upstream("done") is True
    assert client.running_daemon_version() == "1.2.3"
    assert client.installed_daemon_version() == "9.8.7"
    assert commands[0][0] == [
        "browserwright-daemon", "backend-info", "--json", "--session", "s-1",
    ]
    assert commands[3][0] == [
        "browserwright-daemon", "disconnect", "--reason", "done",
        "--session", "s-1",
    ]
    assert all(call[1]["capture_output"] and call[1]["text"] for call in commands)


def test_cli_info_methods_tolerate_bad_outputs_and_timeouts(monkeypatch):
    from browserwright.mode_b_client import ModeBClient
    import browserwright.mode_b_client as mb

    outputs = deque(
        [
            _Proc(returncode=1, stdout="{}"),
            _Proc(stdout="{not json"),
            _Proc(stdout=""),
            _Proc(stdout=json.dumps({"version": ""})),
            _Proc(returncode=1, stdout="browserwright-daemon 1.0.0"),
        ]
    )

    def fake_run(cmd, **kwargs):
        if cmd[1] == "disconnect":
            raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout", 0))
        return outputs.popleft()

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    client = ModeBClient()

    assert client.get_backend_info() is None
    assert client.active_tab() is None
    assert client.attach_active() is None
    assert client.disconnect_upstream() is False
    assert client.running_daemon_version() is None
    assert client.installed_daemon_version() is None


def test_open_background_and_close_tab_success_and_error_details(monkeypatch):
    from browserwright.mode_b_client import ModeBClient
    import browserwright.mode_b_client as mb

    outputs = deque(
        [
            _Proc(stdout=json.dumps({"targetId": "t-bg", "url": "https://e.test"})),
            _Proc(returncode=7, stdout="", stderr="wrong backend"),
            _Proc(stdout=json.dumps({"ok": True, "tabId": 10})),
            _Proc(stdout="not-json"),
        ]
    )
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return outputs.popleft()

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    client = ModeBClient()
    client._session_id = "s-1"

    assert client.open_background("https://e.test", group="Research") == {
        "targetId": "t-bg",
        "url": "https://e.test",
    }
    assert commands[-1] == [
        "browserwright-daemon",
        "open-background",
        "--url",
        "https://e.test",
        "--group",
        "Research",
        "--session",
        "s-1",
    ]

    assert client.close_tab() is None
    assert client.close_tab(session_id="sid") is None
    assert "wrong backend" in client.last_cli_error

    assert client.close_tab(session_id="sid", target_id="t-bg") == {"ok": True, "tabId": 10}
    assert commands[-1] == [
        "browserwright-daemon",
        "close-tab",
        "--session",
        "s-1",
        "--target-id",
        "t-bg",
        "--session-id",
        "sid",
    ]

    assert client.open_background("https://bad.test") is None
    assert "non-JSON stdout" in client.last_cli_error


def test_doctor_returns_real_json_or_synthetic_failure(monkeypatch):
    from browserwright.mode_b_client import ModeBClient
    import browserwright.mode_b_client as mb

    outputs = deque(
        [
            _Proc(stdout=json.dumps({"schema_version": 1, "backends": [{"name": "rdp"}]})),
            _Proc(returncode=2, stdout="", stderr="daemon down"),
            _Proc(stdout="{bad"),
        ]
    )
    monkeypatch.setattr(mb.subprocess, "run", lambda *a, **k: outputs.popleft())

    client = ModeBClient()
    assert client.doctor()["backends"] == [{"name": "rdp"}]
    assert client.doctor() == {
        "schema_version": 1,
        "backends": [],
        "error": "daemon down",
        "skill_synthetic": True,
    }
    assert client.doctor()["error"] == "doctor output was not JSON"

    def missing(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(mb.subprocess, "run", missing)
    assert client.doctor()["skill_synthetic"] is True


def test_ensure_version_coherent_restarts_legacy_alive_daemon_with_backend(monkeypatch):
    from browserwright.mode_b_client import ModeBClient

    client = ModeBClient()
    actions = []
    monkeypatch.setattr(client, "installed_daemon_version", lambda: "2.0.0")
    monkeypatch.setattr(client, "running_daemon_version", lambda: None)
    monkeypatch.setattr(client, "is_alive", lambda: True)
    monkeypatch.setattr(client, "get_backend_info", lambda: {"backend": "extension"})
    monkeypatch.setattr(client, "_stop_daemon", lambda: actions.append(("stop", None)))
    monkeypatch.setattr(client, "_spawn_daemon", lambda backend=None: actions.append(("spawn", backend)))
    monkeypatch.setattr(client, "invalidate", lambda: actions.append(("invalidate", None)))

    assert client.ensure_version_coherent() is True
    assert actions == [("stop", None), ("spawn", "extension"), ("invalidate", None)]


def test_client_for_session_is_lazy_but_runs_coherence_when_alive(monkeypatch):
    import browserwright.mode_b_client as mb

    original = mb.ModeBClient
    made = []

    class _FakeClient(original):
        def __init__(self):
            super().__init__()
            self.waited = False
            made.append(self)

        def is_alive(self):
            return True

        def ensure_version_coherent(self):
            return True

        def wait_until_alive(self, timeout=8.0, interval=0.2):
            self.waited = True
            return True

    monkeypatch.setattr(mb, "ModeBClient", _FakeClient)
    client = mb.client_for_session({"id": 123})

    assert client is made[0]
    assert client._client_label == "skill-s123"
    assert client._session_id == "123"
    assert client.waited is True
    assert client._cached_ws is None


def test_session_resolve_retries_and_lazy_cdp_reuses_until_closed(monkeypatch):
    from browserwright.errors import DaemonUnavailable
    import browserwright.session as session_mod

    class _Daemon:
        def __init__(self):
            self.calls = 0
            self.invalidated = 0

        def resolve_ws_url(self):
            self.calls += 1
            if self.calls == 1:
                raise DaemonUnavailable("cold")
            return f"ws://ok/{self.calls}"

        def invalidate(self):
            self.invalidated += 1

    class _FakeCDP:
        def __init__(self, url):
            self.ws_url = url
            self._closed = False
            self.closed_count = 0

        def close(self):
            self.closed_count += 1
            self._closed = True

    monkeypatch.setattr(session_mod, "CDPSession", _FakeCDP)
    daemon = _Daemon()
    sess = session_mod.Session(daemon=daemon)

    first = sess.cdp
    assert first.ws_url == "ws://ok/2"
    assert daemon.invalidated == 1
    assert sess.cdp is first

    first._closed = True
    second = sess.cdp
    assert second is not first
    assert second.ws_url == "ws://ok/3"
    sess.close()
    assert second.closed_count == 1
    assert sess._cdp is None


def test_session_backend_name_caches_and_swallows_known_getter_errors():
    from browserwright.session import Session

    class _NamedDaemon:
        def __init__(self):
            self.calls = 0

        def get_backend_info(self):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("backend_name should be cached")
            return {"name": "extension"}

    named = _NamedDaemon()
    sess = Session(daemon=named)
    assert sess.backend_name == "extension"
    assert sess.backend_name == "extension"
    assert named.calls == 1

    class _BrokenDaemon:
        def get_backend_info(self):
            raise OSError("temporary pipe failure")

    assert Session(daemon=_BrokenDaemon()).backend_name == ""


def test_resolve_session_empty_explicit_arg_refuses_even_with_env(tmp_bs_home, monkeypatch):
    from browserwright import session_ctx
    from browserwright import session_registry as reg
    from browserwright.errors import NoSession

    sid = reg.allocate(backend="rdp", owner="create")
    monkeypatch.setenv("BD_SESSION", sid)

    with pytest.raises(NoSession):
        session_ctx.resolve_session("")


def test_resolve_session_requires_explicit_arg_even_with_env(tmp_bs_home, monkeypatch):
    from browserwright import session_ctx
    from browserwright import session_registry as reg
    from browserwright.errors import NoSession

    sid = reg.allocate(backend="rdp", owner="create")
    monkeypatch.setenv("BD_SESSION", sid)

    with pytest.raises(NoSession):
        session_ctx.resolve_session()


def test_open_unix_websocket_uses_synthetic_http_upgrade(monkeypatch):
    import importlib

    cdp = importlib.import_module("browserwright.cdp")

    raw = SimpleNamespace(
        timeout=None,
        connected_to=None,
        settimeout=lambda timeout: setattr(raw, "timeout", timeout),
        connect=lambda path: setattr(raw, "connected_to", path),
        setsockopt=lambda *args: None,
    )
    ws_connect_calls = []

    monkeypatch.setattr(cdp._sock if hasattr(cdp, "_sock") else socket, "socket", lambda *a, **k: raw)

    def fake_ws_connect(url, **kwargs):
        ws_connect_calls.append((url, kwargs))
        return "ws-object"

    monkeypatch.setattr(cdp, "ws_connect", fake_ws_connect)

    assert cdp._open_unix_websocket(
        "ws+unix:///tmp/browserwright.sock?client=skill-s1&session=1",
        connect_timeout=0.25,
    ) == "ws-object"
    # GH #18: the connect-phase timeout must be CLEARED before handing the
    # socket to websockets — otherwise it leaks into steady state as a per-recv
    # read timeout and a slow RPC reply (> connect_timeout) drops the transport
    # with "no close frame received or sent". Liveness is ws keepalive's job.
    assert raw.timeout is None
    assert raw.connected_to == "/tmp/browserwright.sock"
    assert ws_connect_calls[0][0] == "ws://browserwright/?client=skill-s1&session=1"
    assert isinstance(ws_connect_calls[0][1]["sock"], cdp._UnixSocketAdapter)
    assert ws_connect_calls[0][1]["proxy"] is None
    assert ws_connect_calls[0][1]["compression"] is None


def test_cdp_send_serializes_session_returns_result_and_rewrites_stale_errors():
    from browserwright.cdp import CDPSession
    from browserwright.errors import CDPError

    cdp = CDPSession.__new__(CDPSession)
    cdp._lock = threading.Lock()
    cdp._inflight_cv = threading.Condition(cdp._lock)
    cdp._next_id = 1
    cdp._inflight = {}
    cdp._events = {None: deque(maxlen=2)}
    cdp._closed = False
    cdp._closed_reason = None
    sent = []

    class _FakeWS:
        def __init__(self, response):
            self.response = response

        def send(self, payload):
            frame = json.loads(payload)
            sent.append(frame)
            with cdp._inflight_cv:
                cdp._inflight[frame["id"]] = self.response(frame)
                cdp._inflight_cv.notify_all()

    cdp._ws = _FakeWS(lambda frame: {"id": frame["id"], "result": {"ok": True}})
    assert CDPSession.send(cdp, "Runtime.evaluate", session="sid", expression="1") == {"ok": True}
    assert sent[-1] == {
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {"expression": "1"},
        "sessionId": "sid",
    }

    cdp._ws = _FakeWS(
        lambda frame: {
            "id": frame["id"],
            "error": {"code": -32601, "message": "Method not found"},
        }
    )
    with pytest.raises(CDPError) as exc:
        CDPSession.send(cdp, "BrowserwrightDaemon.newerMethod")
    assert "stale" in exc.value.fix
    assert "BrowserwrightDaemon.newerMethod" in exc.value.fix

    cdp._closed = True
    cdp._closed_reason = "bye"
    with pytest.raises(CDPError, match="ws closed: bye"):
        CDPSession.send(cdp, "Target.getTargets")
