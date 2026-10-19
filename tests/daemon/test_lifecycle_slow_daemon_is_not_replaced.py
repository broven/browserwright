"""A slow daemon is not a dead daemon, and is never replaced on a timeout.

The 2026-10-28 incident: a machine-global `browserwright-daemon` that was alive
— still accepting ws clients, shutting down gracefully on the signal — was
replaced because two consecutive 1.5s `/__ping__` probes timed out. It had been
idle for ~53 minutes and was momentarily slow. Two agreeing timeouts were read
as "someone else holds the port" (`FOREIGN`), which `ensure()` treats as
"spawn a replacement".

The verdict states are the contract:

- ``foreign``   — something answered HTTP that is not our pong. Never replaced.
- ``down``      — nothing is listening (`refused`). Replaced.
- ``stale``     — our daemon, wrong version, answered. Replaced.
- ``unresponsive`` — the socket opened and nothing came back, for the whole
  escalated window. **Nothing is started or signalled**, the same early return
  `undecided` gets. A merely-slow daemon answers inside that window and is
  diagnosed `up` / `stale` instead.

`ensure` is the only code allowed to start or replace a daemon, so these drive
the genuine one with its side-effecting adapters (`_spawn_detached`, `run_verb`,
`_ipc.log_lifecycle`, `_ipc.request_executor_handoff`) substituted — the shape
`tests/skill/test_adr0011_daemon_url.py` established for the same reason: the
repo-wide wall in `tests/conftest.py` replaces `ensure` itself.
"""
from __future__ import annotations

import json
import socket
import threading

import pytest

from browserwright import daemon_lifecycle as lifecycle
from browserwright.daemon import _ipc
from browserwright.daemon._ipc import EndpointProbe, probe_endpoint_sync

#: The genuine `ensure`, captured before the wall stubs it out.
_REAL_ENSURE = lifecycle.ensure

#: A budget that is short enough to keep the suite fast and wide enough that a
#: hand-built fake listener must genuinely answer *late*.
_ESCALATED_WINDOW_S = 1.0
_SLOW_ANSWER_AFTER_S = 0.25


@pytest.fixture(autouse=True)
def _fast_escalation(monkeypatch):
    """Shrink the escalated window. The real 6s is a latency budget, not a
    contract; the behaviour under test is \"answers inside the window or not\"."""
    monkeypatch.setattr(_ipc, "ESCALATED_PROBE_WINDOW_S", _ESCALATED_WINDOW_S,
                        raising=False)


@pytest.fixture(autouse=True)
def _record_adapters(monkeypatch):
    """Substitute `ensure`'s side-effecting adapters; return what they saw."""
    actions: list = []
    monkeypatch.setattr(lifecycle, "_spawn_detached",
                        lambda args, *, initiator: actions.append(
                            ("spawn", args, initiator)))
    monkeypatch.setattr(lifecycle, "run_verb",
                        lambda args, **kw: actions.append(
                            ("run", args, kw.get("initiator"))))
    monkeypatch.setattr(_ipc, "request_executor_handoff",
                        lambda pid: actions.append(("handoff", pid)))
    monkeypatch.setattr(_ipc, "log_lifecycle",
                        lambda event, **fields: actions.append(
                            ("log", event, fields)))
    return actions


def _timeout_probe(host, port, timeout=1.5):
    return EndpointProbe(kind="timeout", host=host, port=port,
                         latency_ms=timeout * 1000.0)


def _refused_probe(host, port, timeout=1.5):
    return EndpointProbe(kind="refused", host=host, port=port, latency_ms=1.0)


def _silent_then(monkeypatch, answer: EndpointProbe):
    """Script `probe` so the first two calls time out and the escalated re-probe
    finds `answer`."""
    calls = {"n": 0}

    def fake_probe(host, port, timeout=1.5):
        calls["n"] += 1
        if calls["n"] <= 2:
            return EndpointProbe(kind="timeout", host=host, port=port,
                                 latency_ms=timeout * 1000.0)
        return answer

    monkeypatch.setattr(lifecycle, "probe", fake_probe)


# ---- (a) a live-but-slow endpoint is not replaceable -----------------------


def test_a_silent_endpoint_is_unresponsive_not_foreign(monkeypatch):
    """The defect, at the classifier: silence used to be folded into FOREIGN."""
    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.setattr(lifecycle, "probe", _timeout_probe)

    verdict = lifecycle.diagnose(confirm=True)

    assert verdict.state == lifecycle.UNRESPONSIVE
    assert verdict.state != lifecycle.FOREIGN
    assert verdict.replaceable is False
    assert verdict.up is False
    # The detail is evidence: the endpoint, every probe kind, and its latency.
    assert verdict.endpoint.authority in verdict.detail
    assert verdict.detail.count("timeout@") == len(verdict.probes)
    assert len(verdict.probes) > 2  # the short pair, then the escalated re-asks
    assert "1500ms" in verdict.detail
    assert set(verdict.probes) == {"timeout"}


def test_ensure_starts_nothing_for_an_unresponsive_endpoint(monkeypatch,
                                                            _record_adapters):
    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.setattr(lifecycle, "probe", _timeout_probe)

    verdict = _REAL_ENSURE("session new")

    assert verdict.state == lifecycle.UNRESPONSIVE
    assert _record_adapters == []  # no spawn, no stop, no LIFECYCLE line


def test_ensure_starts_nothing_for_an_endpoint_answering_garbage(monkeypatch,
                                                                _record_adapters):
    """Bytes that are not HTTP are an answer too — never a replacement cue."""
    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.setattr(lifecycle, "probe", lambda h, p, timeout=1.5: EndpointProbe(
        kind="garbage", host=h, port=p, detail="SSH-2.0-OpenSSH"))

    verdict = _REAL_ENSURE("session new")

    assert verdict.state == lifecycle.UNRESPONSIVE
    assert _record_adapters == []


# ---- (b) escalation rescues a slow-but-answering daemon -------------------


@pytest.mark.parametrize(("reported_version", "state"), [
    (None, lifecycle.UP),      # `None` → the installed version
    ("0.0.1-old", lifecycle.STALE),
])
def test_escalation_diagnoses_a_slow_daemon_up(monkeypatch, reported_version,
                                               state):
    from browserwright.version import package_version

    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    version = package_version() if reported_version is None else reported_version
    _silent_then(monkeypatch, EndpointProbe(
        kind="ours", host="127.0.0.1", port=1, pid=4321, version=version,
        latency_ms=612.0))

    verdict = lifecycle.diagnose(confirm=True)

    assert verdict.state == state
    assert verdict.up is True
    assert verdict.pid == 4321
    assert "timed out" in verdict.detail and "before it answered" in verdict.detail


def test_escalation_saves_a_slow_daemon_from_a_version_skew_replacement(
        monkeypatch, _record_adapters):
    """The incident's shape exactly: version skew is a *replace* trigger, so a
    timeout misread as `foreign`/`stale` replaces a daemon that answers late."""
    from browserwright.version import package_version

    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    _silent_then(monkeypatch, EndpointProbe(
        kind="ours", host="127.0.0.1", port=1, pid=4321,
        version=package_version(), latency_ms=900.0))

    verdict = _REAL_ENSURE("session new")

    assert verdict.healthy is True
    assert _record_adapters == []


def test_a_real_late_answering_listener_is_diagnosed_up(monkeypatch,
                                                        tmp_path):
    """Unstubbed: a real socket that accepts and answers only after the short
    probe's budget has already expired. The first probe must time out and the
    escalated one must find the pong — otherwise this is not testing anything."""
    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    # Own runtime dir: the endpoint state file this publishes is how `diagnose`
    # finds the server WITHOUT making the endpoint explicit (an explicit one is
    # never started or replaced, which is the opposite of the case here).
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    host, port, thread = _late_pong_server(_SLOW_ANSWER_AFTER_S)
    try:
        _ipc.write_endpoint_state(f"http://{host}:{port}")
        monkeypatch.setattr(lifecycle, "probe", probe_endpoint_sync)

        first = probe_endpoint_sync(host, port, timeout=0.05)
        assert first.kind == "timeout", first

        verdict = lifecycle.diagnose(confirm=True, expected_version="1.2.3")
        assert verdict.state == lifecycle.UP
        assert verdict.pid == 987
    finally:
        thread.join(2.0)


def _late_pong_server(delay: float):
    """A loopback server that accepts and then answers the pong ``delay`` later.

    One connection per probe attempt, so the escalated re-probe is a genuinely
    separate request."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(5.0)
    host, port = srv.getsockname()

    def serve_one(conn):
        with conn:
            conn.settimeout(2.0)
            try:
                conn.recv(4096)
            except OSError:
                pass
            threading.Event().wait(delay)
            body = json.dumps({"pong": True, "pid": 987, "version": "1.2.3"}).encode()
            try:
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
                    + body)
            except OSError:
                pass

    def run():
        try:
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=serve_one, args=(conn,),
                                 daemon=True).start()
        finally:
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return host, port, t



def test_probes_until_answered_stops_at_the_first_answer():
    observed = [
        EndpointProbe(kind="timeout", host="127.0.0.1", port=1, latency_ms=1000.0),
        EndpointProbe(kind="ours", host="127.0.0.1", port=1, pid=7,
                      version="1.0.0", latency_ms=12.0),
    ]
    calls = []

    def fake_probe(host, port, *, timeout):
        calls.append(timeout)
        return observed[len(calls) - 1]

    result = _ipc.probes_until_answered("127.0.0.1", 1, probe=fake_probe)

    assert [pr.kind for pr in result] == ["timeout", "ours"]
    assert len(calls) == 2


def test_probes_until_answered_is_bounded_and_never_asks_garbage_twice():
    garbage = EndpointProbe(kind="garbage", host="127.0.0.1", port=1,
                            latency_ms=3.0)
    calls = []

    def fake_probe(host, port, *, timeout):
        calls.append(timeout)
        return garbage

    result = _ipc.probes_until_answered("127.0.0.1", 1, probe=fake_probe)

    assert len(result) == 1 and len(calls) == 1

    silent = EndpointProbe(kind="timeout", host="127.0.0.1", port=1,
                           latency_ms=1.0)
    calls.clear()
    result = _ipc.probes_until_answered("127.0.0.1", 1, probe=(
        lambda h, p, *, timeout: calls.append(timeout) or silent))
    assert len(calls) == _ipc.ESCALATED_PROBE_ATTEMPTS
    assert len(result) == _ipc.ESCALATED_PROBE_ATTEMPTS


# ---- (d) the legitimate paths are untouched -------------------------------


def test_a_refused_endpoint_is_still_down_and_spawns(monkeypatch,
                                                    _record_adapters):
    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.setattr(lifecycle, "probe", _refused_probe)

    verdict = _REAL_ENSURE("session new")

    assert verdict.state == lifecycle.DOWN
    assert [a[:2] for a in _record_adapters] == [
        ("log", "spawn"), ("spawn", ["serve"])]


def test_a_version_skewed_daemon_is_still_replaced(monkeypatch,
                                                   _record_adapters):
    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.setattr(lifecycle, "probe", lambda h, p, timeout=1.5: EndpointProbe(
        kind="ours", host=h, port=p, pid=31, version="0.0.1-old"))

    _REAL_ENSURE("recover session=7")

    assert [a[:2] for a in _record_adapters] == [
        ("log", "replace"), ("handoff", 31), ("run", ["stop"]),
        ("spawn", ["serve"])]


# ---- (e) a real HTTP answer is still FOREIGN, which is never replaced -----


def test_a_real_http_answer_is_still_foreign_and_never_replaced(
        monkeypatch, tmp_path, _record_adapters):
    """A proxy answering 503 on our port is still FOREIGN — diagnosed, and never
    a stop or a replacement target.

    ``ensure`` does still try a ``serve`` for it, exactly as before: the new
    process is what politely refuses beside a live daemon, and `serve`'s own
    reclaim will not signal an unproven holder (`_reclaim_decision` in
    `listener.py`). What must never happen — and is what `recover` gates on via
    ``replaceable`` — is a stop or a version replacement.
    """
    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    host, port, thread = _http_503_server()
    try:
        _ipc.write_endpoint_state(f"http://{host}:{port}")
        monkeypatch.setattr(lifecycle, "probe", probe_endpoint_sync)

        verdict = _REAL_ENSURE("session new", wait=0.0)
        assert verdict.state == lifecycle.FOREIGN
        assert verdict.replaceable is False
        assert "503" in verdict.detail
        assert set(verdict.probes) == {"foreign"}
        assert [a for a in _record_adapters if a[0] in ("run", "handoff")] == []
    finally:
        thread.join(2.0)


def _http_503_server():
    """A real one-shot server that answers an HTTP 503 to every request."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(5.0)
    host, port = srv.getsockname()

    def run():
        try:
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    conn.settimeout(2.0)
                    try:
                        conn.recv(4096)
                    except OSError:
                        pass
                    try:
                        conn.sendall(
                            b"HTTP/1.1 503 Service Unavailable\r\n"
                            b"Server: Surge/5.0\r\nContent-Length: 0\r\n\r\n")
                    except OSError:
                        pass
        finally:
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return host, port, t
