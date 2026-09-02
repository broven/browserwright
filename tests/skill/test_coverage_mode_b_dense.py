"""Dense offline coverage for Mode B client/session transport edges."""
from __future__ import annotations

import json
import threading
from collections import deque

import pytest


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_discover_reports_the_resolved_endpoint(monkeypatch):
    """ADR-0011: discovery is URL resolution, not a socket-file hunt."""
    from browserwright.mode_b_client import ModeBClient

    monkeypatch.setenv("BW_DAEMON_URL", "http://10.0.0.7:19990")
    client = ModeBClient()
    assert client.discover() == {"transport": "tcp",
                                 "url": "http://10.0.0.7:19990"}
    assert client.explicit is True


def test_discover_default_endpoint_is_not_explicit(monkeypatch, tmp_path):
    """No configured source => the local default, and auto-start stays on."""
    from browserwright.mode_b_client import ModeBClient

    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.delenv("BD_CONFIG", raising=False)
    # An empty runtime dir has no endpoint state file to fall back to.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    client = ModeBClient()
    assert client.discover() == {"transport": "tcp",
                                 "url": "http://127.0.0.1:19990"}
    assert client.explicit is False


def test_ping_is_the_http_pong_probe(monkeypatch):
    from browserwright.daemon import _ipc
    from browserwright.mode_b_client import ModeBClient

    seen = []

    def fake_ping(timeout=1.0):
        seen.append(timeout)
        return _ipc.PongInfo(pid=4242, version="9.9.9")

    monkeypatch.setattr(_ipc, "ping_status_sync", fake_ping)
    client = ModeBClient()
    assert client.is_alive() is True
    assert client.running_daemon_version() == "9.9.9"
    assert seen  # the probe actually ran

    monkeypatch.setattr(_ipc, "ping_status_sync", lambda timeout=1.0: _ipc.NO_PONG)
    assert client.is_alive() is False
    assert client.running_daemon_version() is None


def test_explicit_endpoint_never_spawns_or_restarts(monkeypatch, capsys):
    """The ADR-0011 rule: a daemon someone named is not ours to manage."""
    from browserwright.mode_b_client import ModeBClient

    monkeypatch.setenv("BW_DAEMON_URL", "http://10.0.0.7:19990")
    client = ModeBClient()
    spawned = []
    monkeypatch.setattr(client, "installed_daemon_version", lambda: "2.0.0")
    monkeypatch.setattr(client, "running_daemon_version", lambda: "1.0.0")
    monkeypatch.setattr(
        client, "_stop_daemon", lambda: spawned.append("stop"))

    assert client.ensure_version_coherent() is False
    assert spawned == []
    # The skew is still reported — silently driving a mismatched daemon is
    # exactly the pothole `ensure_version_coherent` exists to prevent.
    assert "will not restart it" in capsys.readouterr().err


def test_ws_url_caches_until_invalidated_and_carries_session_query(monkeypatch):
    from browserwright.mode_b_client import ModeBClient

    monkeypatch.setenv("BW_DAEMON_URL", "http://127.0.0.1:19990")
    client = ModeBClient()
    client._session_id = "s-42"

    assert client.ws_url(client_label="first") == (
        "ws://127.0.0.1:19990/control?client=first&session=s-42")
    # Cached: the label of the second call is ignored until invalidate().
    assert client.ws_url(client_label="second") == (
        "ws://127.0.0.1:19990/control?client=first&session=s-42")

    client.invalidate()
    monkeypatch.setenv("BW_DAEMON_URL", "http://127.0.0.1:29990")
    assert client.ws_url(client_label="second") == (
        "ws://127.0.0.1:29990/control?client=second&session=s-42")
    assert client._endpoint == "http://127.0.0.1:29990"
    assert client._transport == "tcp"


def test_cli_info_methods_parse_defaults_and_command_shapes(monkeypatch):
    from browserwright.mode_b_client import ModeBClient
    import browserwright.mode_b_client as mb

    outputs = deque(
        [
            _Proc(stdout=json.dumps({"backend": "cdp"})),
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

    assert client.get_backend_info() == {"backend": "cdp"}
    assert client.installed_daemon_version() == "9.8.7"
    assert commands[0][0] == [
        "browserwright-daemon", "backend-info", "--json", "--session", "s-1",
    ]
    assert all(call[1]["capture_output"] and call[1]["text"] for call in commands)


def test_cli_info_methods_tolerate_bad_outputs_and_timeouts(monkeypatch):
    from browserwright.mode_b_client import ModeBClient
    import browserwright.mode_b_client as mb

    outputs = deque(
        [
            _Proc(returncode=1, stdout="{}"),
            _Proc(returncode=1, stdout="browserwright-daemon 1.0.0"),
        ]
    )

    def fake_run(cmd, **kwargs):
        return outputs.popleft()

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    client = ModeBClient()

    assert client.get_backend_info() is None
    assert client.installed_daemon_version() is None


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


def test_resolve_session_empty_explicit_arg_refuses_even_with_env(tmp_bs_home, monkeypatch):
    from browserwright import session_ctx
    from browserwright import session_registry as reg
    from browserwright.errors import NoSession

    sid = reg.allocate(backend="cdp", owner="create")
    monkeypatch.setenv("BD_SESSION", sid)

    with pytest.raises(NoSession):
        session_ctx.resolve_session("")


def test_resolve_session_requires_explicit_arg_even_with_env(tmp_bs_home, monkeypatch):
    from browserwright import session_ctx
    from browserwright import session_registry as reg
    from browserwright.errors import NoSession

    sid = reg.allocate(backend="cdp", owner="create")
    monkeypatch.setenv("BD_SESSION", sid)

    with pytest.raises(NoSession):
        session_ctx.resolve_session()


def test_cdp_session_opens_one_plain_ws_transport(monkeypatch):
    """ADR-0011 deleted the `ws+unix://` sentinel and its AF_UNIX adapter."""
    import importlib

    cdp = importlib.import_module("browserwright.cdp")

    assert not hasattr(cdp, "_open_unix_websocket")
    assert not hasattr(cdp, "_UnixSocketAdapter")

    calls = []

    class _FakeWS:
        def __iter__(self):
            return iter(())

    def fake_ws_connect(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeWS()

    monkeypatch.setattr(cdp, "ws_connect", fake_ws_connect)
    sess = cdp.CDPSession(
        "ws://127.0.0.1:19990/control?client=skill-s1&session=1",
        connect_timeout=0.25)
    try:
        url, kwargs = calls[0]
        assert url == "ws://127.0.0.1:19990/control?client=skill-s1&session=1"
        assert kwargs["proxy"] is None
        assert kwargs["compression"] is None
        assert kwargs["open_timeout"] == 0.25
    finally:
        sess.close()


def test_cdp_send_serializes_session_returns_result_and_rewrites_stale_errors():
    from browserwright.cdp import CDPSession
    from browserwright.errors import CDPError

    cdp = CDPSession.__new__(CDPSession)
    cdp._lock = threading.Lock()
    cdp._inflight_cv = threading.Condition(cdp._lock)
    cdp._next_id = 1
    cdp._inflight = {}
    cdp._inflight_meta = {}
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

    # Issue #40: an attach conflict against the session's own orphaned
    # executor must point at the reap recovery, not at the generic -32601
    # stale-daemon hint (which does not apply).
    cdp._ws = _FakeWS(
        lambda frame: {
            "id": frame["id"],
            "error": {
                "code": -32602,
                "message": "target ext-tab-32688709 already attached by "
                           "another client",
            },
        }
    )
    with pytest.raises(CDPError) as exc:
        CDPSession.send(cdp, "Target.attachToTarget", targetId="ext-tab-1")
    assert "orphaned" in exc.value.fix
    assert "browserwright recover --session" in exc.value.fix

    cdp._closed = True
    cdp._closed_reason = "bye"
    with pytest.raises(CDPError, match="ws closed: bye"):
        CDPSession.send(cdp, "Target.getTargets")
