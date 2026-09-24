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


def test_diagnose_reads_pid_and_version_off_the_pong(monkeypatch):
    """The one "is the daemon up / which version" answer is the `/__ping__`
    pong, classified by `daemon_lifecycle.diagnose`."""
    from browserwright import daemon_lifecycle as lifecycle
    from browserwright.daemon._ipc import EndpointProbe

    seen = []

    def fake_probe(host, port, timeout=1.5):
        seen.append(timeout)
        return EndpointProbe(kind="ours", host=host, port=port, pid=4242,
                             version="9.9.9")

    monkeypatch.setattr(lifecycle, "probe", fake_probe)
    verdict = lifecycle.diagnose(expected_version="9.9.9")
    assert verdict.up and verdict.healthy
    assert (verdict.pid, verdict.version) == (4242, "9.9.9")
    assert seen  # the probe actually ran

    monkeypatch.setattr(lifecycle, "probe", lambda h, p, timeout=1.5: EndpointProbe(
        kind="refused", host=h, port=p))
    verdict = lifecycle.diagnose()
    assert verdict.up is False
    assert (verdict.pid, verdict.version) == (None, None)


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


def test_run_verb_parses_json_and_command_shapes(monkeypatch):
    from browserwright import daemon_lifecycle as lifecycle

    outputs = deque([_Proc(stdout=json.dumps({"backend": "cdp"}))])
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append((cmd, kwargs))
        return outputs.popleft()

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)
    monkeypatch.setenv("BW_DAEMON_URL", "http://10.0.0.7:19990")

    result = lifecycle.run_verb(["backend-info", "--json", "--session", "s-1"])
    assert result.json() == {"backend": "cdp"}
    cmd, kwargs = commands[0]
    assert cmd == [
        "browserwright-daemon", "backend-info", "--json", "--session", "s-1",
    ]
    assert kwargs["capture_output"] and kwargs["text"]
    # The child asks the SAME daemon this process is addressed at.
    assert kwargs["env"]["BW_DAEMON_URL"] == "http://10.0.0.7:19990"


def test_run_verb_tolerates_bad_outputs_and_a_missing_binary(monkeypatch):
    from browserwright import daemon_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle.subprocess, "run",
                        lambda cmd, **kw: _Proc(returncode=1, stdout="not json"))
    result = lifecycle.run_verb(["backend-info", "--json"])
    assert result.returncode == 1 and result.json() is None

    def missing(cmd, **kw):
        raise FileNotFoundError("browserwright-daemon")

    monkeypatch.setattr(lifecycle.subprocess, "run", missing)
    result = lifecycle.run_verb(["version"])
    assert result.missing and result.returncode == 1


def test_a_daemon_too_old_to_advertise_a_version_is_stale(monkeypatch):
    """A legacy daemon answers the ping without a version: stale, and — on
    the default endpoint — replaceable, never mistaken for "no daemon"."""
    from browserwright import daemon_lifecycle as lifecycle
    from browserwright.daemon._ipc import EndpointProbe

    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.setattr(lifecycle, "probe", lambda h, p, timeout=1.5: EndpointProbe(
        kind="ours", host=h, port=p, pid=9, version=None))
    verdict = lifecycle.diagnose(confirm=True)
    assert verdict.state == lifecycle.STALE
    assert verdict.replaceable


def test_client_for_session_has_no_lifecycle_side_effects(monkeypatch):
    """Constructing a Session's client must not probe, stop or spawn."""
    import browserwright.mode_b_client as mb
    from browserwright import daemon_lifecycle as lifecycle

    for name in ("probe", "ensure", "run_verb", "_spawn_detached"):
        monkeypatch.setattr(lifecycle, name, lambda *a, _n=name, **k: pytest.fail(
            f"client_for_session called daemon_lifecycle.{_n}"))
    client = mb.client_for_session({"id": 123})

    assert client._client_label == "skill-s123"
    assert client._session_id == "123"
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
