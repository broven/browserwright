"""Wiring coverage for the issue #15 P2 fixes: status port-held reporting (2.1),
serve reclaim (2.2), and the control-socket self-exit watchdog (2.4).

Both halves drive the real code through a stub `probe.DaemonProbe` — the same
object production uses — so what these tests exercise is the actual state
machine, not a monkeypatched reassembly of it. The one surviving patch is
`_stale.reclaim_ports`, the single destructive step, which is deliberately not
on the probe.

Still verified by hand, not here: an actual SIGTERM reaching an actual wedged
daemon and the ports coming free. That is `_stale.reclaim_ports`' own contract
(unit-tested in `test_stale_daemon.py` with `os.kill` stubbed); nothing in CI
starts a real half-alive daemon.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from browserwright.daemon import _ipc, _stale, cli
from browserwright.daemon._ipc import EndpointProbe
from browserwright.daemon.config import Config
from browserwright.daemon.probe import DaemonProbe
from browserwright.daemon.server import listener


def _ns(**kw):
    kw.setdefault("json", True)
    return SimpleNamespace(**kw)


class _StubProbe(DaemonProbe):
    """A daemon that never answers, with scriptable ports and crash traces."""

    retry_window = 0.05  # the retry loop is real; just don't dawdle in it

    def __init__(self, *, daemon_traces, ports=(), holder=None):
        super().__init__(Config())
        self._daemon_traces = daemon_traces
        self._ports = list(ports)
        self._holder = holder
        self.probed_ports = []

    def ping(self, timeout):
        return _ipc.NO_PONG

    def daemon_traces(self):
        return self._daemon_traces

    def daemon_ports(self):
        return list(self._ports)

    def listening_ports(self, ports):
        self.probed_ports.extend(ports)
        return list(ports)

    def confirmed_stale_holder(self, ports):
        return self._holder

    def endpoint(self):
        return {"schema_version": 1, "transport": "tcp",
                "url": "http://127.0.0.1:19990", "explicit": False,
                "source": "default"}

    def sleep(self, seconds):
        pass  # skip the retry backoff


# ---- 2.1: status reports the port-held zombie -------------------------------


def test_cmd_status_reports_port_held_zombie(capsys):
    # Pid file present (a half-alive daemon left it) but nothing answers, and
    # lsof names a confirmed browserwright process on the ports.
    probe = _StubProbe(daemon_traces=True, ports=[29971, 29972], holder=4242)

    rc = cli._cmd_status(_ns(json=True), Config(), probe=probe)

    out = json.loads(capsys.readouterr().out)
    assert out["probe_state"] == "port_held_by_unresponsive_process"
    assert out["port_holder_pid"] == 4242
    assert out["alive"] is False
    assert rc == 2


def test_cmd_status_not_running_when_no_daemon_traces(capsys):
    # No pid file → the port-held probe must NOT fire (don't pick up an
    # unrelated daemon holding the default ports).
    probe = _StubProbe(daemon_traces=False, ports=[29971])

    rc = cli._cmd_status(_ns(json=True), Config(), probe=probe)

    out = json.loads(capsys.readouterr().out)
    assert out["probe_state"] == "not_running"
    assert out["port_holder_pid"] is None
    assert probe.probed_ports == []  # never probed ports
    assert rc == 2


def test_cmd_status_falls_back_to_live_pid_file_when_lsof_blind(capsys):
    # lsof unavailable / holder unidentifiable → surface the pid-file pid as a
    # best-effort hint rather than reporting no holder at all.
    class _Blind(_StubProbe):
        def live_pid_file_pid(self):
            return 777

    probe = _Blind(daemon_traces=True, ports=[29971], holder=None)

    rc = cli._cmd_status(_ns(json=True), Config(), probe=probe)

    out = json.loads(capsys.readouterr().out)
    assert out["probe_state"] == "port_held_by_unresponsive_process"
    assert out["port_holder_pid"] == 777
    assert rc == 2


# ---- 2.2: serve reclaims only a CONFIRMED stale daemon ----------------------
#
# `serve` reads the world through the same `DaemonProbe` as `status`, so these
# drive the real `_reclaim_stale_daemon_ports` with a stub probe. The single
# remaining patch is `_stale.reclaim_ports` — the one destructive step, which
# deliberately does NOT live on the probe.


class _PortProbe(DaemonProbe):
    def __init__(self, *, ports, held=None, holder=None, escalated=None):
        super().__init__(Config())
        self._ports = list(ports)
        self._held = list(ports if held is None else held)
        self._holder = holder
        #: What the escalated re-probe observes. The default is a holder that
        #: stayed unanswered for the whole window — the only shape besides
        #: "nothing is listening" that authorizes a reclaim.
        self._escalated = ([_silent()] if escalated is None else list(escalated))
        self.holder_lookups = []
        self.escalations = []

    def daemon_ports(self):
        return list(self._ports)

    def listening_ports(self, ports):
        return [p for p in ports if p in self._held]

    def confirmed_stale_holder(self, ports):
        self.holder_lookups.append(list(ports))
        return self._holder

    def escalated_probes(self, host, port):
        self.escalations.append((host, port))
        return list(self._escalated)


def _silent(latency_ms=2000.0, kind="timeout"):
    return EndpointProbe(kind=kind, host="127.0.0.1", port=39990,
                         latency_ms=latency_ms)


def _answered(pid=999, version="9.9.9"):
    return EndpointProbe(kind="ours", host="127.0.0.1", port=39990, pid=pid,
                         version=version, latency_ms=41.0)


def test_reclaim_kills_confirmed_stale_daemon(monkeypatch):
    reclaimed = []
    monkeypatch.setattr(_stale, "reclaim_ports",
                        lambda pid, ports: reclaimed.append((pid, ports)) or True)
    # The holder must be in OUR runtime dir — a stale daemon is one that
    # crashed on the same control socket we are about to bind.
    monkeypatch.setattr(_stale, "same_runtime_dir_as_us", lambda pid: True)

    listener._reclaim_stale_daemon_ports(
        Config(), probe=_PortProbe(ports=[29971, 29972], holder=999))

    assert reclaimed == [(999, [29971, 29972])]


def test_reclaim_refuses_holder_from_different_runtime_dir(monkeypatch):
    """issue #44 B: a confirmed browserwright daemon from a DIFFERENT runtime
    dir is someone else's live daemon (the machine-global one, or a sibling
    worktree's e2e daemon) — never SIGTERM it, however stale it looks."""
    signalled = []
    monkeypatch.setattr(_stale, "reclaim_ports",
                        lambda pid, ports: signalled.append(pid) or True)
    monkeypatch.setattr(_stale, "same_runtime_dir_as_us", lambda pid: False)
    monkeypatch.setattr(_stale, "pid_runtime_dir", lambda pid: "/tmp")

    listener._reclaim_stale_daemon_ports(
        Config(), probe=_PortProbe(ports=[29971, 29972], holder=999))

    assert signalled == []  # nothing was ever signalled


def test_reclaim_never_kills_unconfirmed_holder(monkeypatch):
    # Ports held, but no confirmed browserwright daemon owns them → must NOT
    # signal anyone (safety: never kill a stranger).
    reclaimed = []
    monkeypatch.setattr(_stale, "reclaim_ports",
                        lambda pid, ports: reclaimed.append(pid))

    listener._reclaim_stale_daemon_ports(
        Config(), probe=_PortProbe(ports=[29971], holder=None))

    assert reclaimed == []


def test_reclaim_only_considers_ports_actually_held():
    # One of the two ports is free: the holder lookup — and any later signal —
    # must be scoped to the held one, never to the whole configured set.
    probe = _PortProbe(ports=[29971, 29972], held=[29972], holder=None)

    listener._reclaim_stale_daemon_ports(Config(), probe=probe)

    assert probe.holder_lookups == [[29972]]


def test_reclaim_noop_when_ports_free():
    probe = _PortProbe(ports=[29971], held=[])

    listener._reclaim_stale_daemon_ports(Config(), probe=probe)

    assert probe.holder_lookups == []  # never even looked for a holder


# ---- 2.2b: a negative short ping is not enough to SIGTERM a holder --------
#
# The 2026-10-28 incident: a machine-global daemon that was alive (still
# accepting ws clients, shutting down gracefully on the signal) got SIGTERMed
# 2s after two 1.5s `/__ping__` probes timed out. The reclaim's only staleness
# evidence was the caller's own single 1.0s ping. These pin the escalating
# evidence it now needs, and the record that outlives a /dev/null stderr.


def test_reclaim_refuses_a_holder_that_answers_the_escalated_probe(monkeypatch):
    """The incident, replayed: short ping silent, daemon answers on re-probe.

    A slow-but-live daemon must never be SIGTERMed, however little its first
    ping said.
    """
    signalled = []
    monkeypatch.setattr(_stale, "reclaim_ports",
                        lambda pid, ports: signalled.append(pid) or True)
    monkeypatch.setattr(_stale, "same_runtime_dir_as_us", lambda pid: True)

    listener._reclaim_stale_daemon_ports(
        Config(), probe=_PortProbe(ports=[29971], holder=999,
                                   escalated=[_silent(1500.0), _answered()]))

    assert signalled == []


def test_reclaim_refuses_an_incumbent_that_answered_non_http(monkeypatch):
    """Bytes that are not HTTP are an ANSWER, not silence — never a kill."""
    signalled = []
    monkeypatch.setattr(_stale, "reclaim_ports",
                        lambda pid, ports: signalled.append(pid) or True)
    monkeypatch.setattr(_stale, "same_runtime_dir_as_us", lambda pid: True)

    listener._reclaim_stale_daemon_ports(
        Config(), probe=_PortProbe(ports=[29971], holder=999,
                                   escalated=[_silent(kind="garbage")]))

    assert signalled == []


def test_reclaim_still_reclaims_a_half_alive_holder(monkeypatch):
    """The shape issue #15 exists for: the endpoint listener is gone (refused)
    while the relay port is still bound by a provably stale daemon.

    A live process cannot refuse a connection to a socket it holds, so this is
    the half-alive case, not the slow one — reclaiming is correct and must stay
    working.
    """
    reclaimed = []
    monkeypatch.setattr(_stale, "reclaim_ports",
                        lambda pid, ports: reclaimed.append((pid, ports)) or True)
    monkeypatch.setattr(_stale, "same_runtime_dir_as_us", lambda pid: True)
    monkeypatch.setattr(_stale, "pid_runtime_dir", lambda pid: "/tmp")

    listener._reclaim_stale_daemon_ports(
        Config(), probe=_PortProbe(
            ports=[29971, 29972], holder=999,
            escalated=[EndpointProbe(kind="refused", host="127.0.0.1",
                                     port=29972, latency_ms=1.0)]))

    assert reclaimed == [(999, [29971, 29972])]


def test_reclaim_names_the_endpoint_it_rechallenged(monkeypatch):
    """The re-probe targets the port and local client address THIS serve binds
    (ADR-0012 rule 6), not whatever endpoint the shell resolves."""
    monkeypatch.setattr(_stale, "reclaim_ports", lambda pid, ports: True)
    monkeypatch.setattr(_stale, "same_runtime_dir_as_us", lambda pid: True)
    monkeypatch.setattr(_stale, "pid_runtime_dir", lambda pid: "/tmp")
    cfg = Config()
    cfg.facade_host = "100.72.20.32"
    cfg.facade_port = 39990
    probe = _PortProbe(ports=[39990], holder=999)

    listener._reclaim_stale_daemon_ports(cfg, probe=probe)

    # The facade co-binds loopback, so that is what a local client sees.
    assert probe.escalations == [("127.0.0.1", 39990)]


def test_reclaim_records_its_decision_even_with_the_logger_unwired(
        monkeypatch, tmp_path):
    """The record must exist without a logging handler.

    An ensure-spawned `serve` gets stdout/stderr = DEVNULL, and the reclaim runs
    before logging is wired; before this, an automatic SIGTERM left no trace
    anywhere. The decision carries the pid, the probe kinds with their measured
    latencies, and which way it went.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(_stale, "reclaim_ports", lambda pid, ports: True)
    monkeypatch.setattr(_stale, "same_runtime_dir_as_us", lambda pid: True)

    listener._reclaim_stale_daemon_ports(
        Config(), probe=_PortProbe(ports=[29971], holder=999,
                                   escalated=[_silent(2001.0),
                                              _silent(1999.0)]))

    line = _reclaim_lines(tmp_path)[-1]
    assert "decision=reclaimed" in line
    assert "pid=999" in line
    assert "probes=timeout@2001ms,timeout@1999ms" in line
    assert "reason=silent-window" in line
    assert "ports=29971" in line


def test_reclaim_records_a_refusal_with_the_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(_stale, "reclaim_ports",
                        lambda pid, ports: pytest.fail("must not signal"))
    monkeypatch.setattr(_stale, "same_runtime_dir_as_us", lambda pid: True)

    listener._reclaim_stale_daemon_ports(
        Config(), probe=_PortProbe(ports=[29971], holder=999,
                                   escalated=[_answered()]))

    line = _reclaim_lines(tmp_path)[-1]
    assert "decision=refused" in line
    assert "reason=holder-answered" in line
    assert "probes=ours@41ms" in line


def test_reclaim_records_the_different_runtime_dir_refusal(
        monkeypatch, tmp_path):
    """The strict identity gate is untouched, and its refusal is now on disk."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(_stale, "same_runtime_dir_as_us", lambda pid: False)
    monkeypatch.setattr(_stale, "pid_runtime_dir", lambda pid: "/tmp")
    monkeypatch.setattr(_stale, "reclaim_ports",
                        lambda pid, ports: pytest.fail("must not signal"))

    listener._reclaim_stale_daemon_ports(
        Config(), probe=_PortProbe(ports=[29971], holder=999))

    line = _reclaim_lines(tmp_path)[-1]
    assert "decision=refused" in line
    assert "reason=different-runtime-dir" in line


def test_reclaim_records_a_no_holder_decision(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    listener._reclaim_stale_daemon_ports(
        Config(), probe=_PortProbe(ports=[29971], holder=None))

    line = _reclaim_lines(tmp_path)[-1]
    assert "decision=no-holder" in line


def _reclaim_lines(tmp_path):
    text = (tmp_path / "browserwright-daemon.log").read_text()
    lines = [ln for ln in text.splitlines() if " LIFECYCLE reclaim " in ln]
    assert lines, text
    for line in lines:
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} LIFECYCLE reclaim ",
            line), line
    return lines


@pytest.mark.asyncio
async def test_serve_wires_logging_before_it_can_reclaim(monkeypatch):
    """Ordering, through the real `run_serve`.

    The reclaim must not run before logging is wired: with only
    `logging.lastResort` its warnings are undated, unlabelled and — on an
    ensure-spawned serve, whose stderr is DEVNULL — lost. The LIFECYCLE line
    survives either way, but the two are meant to agree in one file.
    """
    order: list[str] = []

    async def fake_ping(timeout=1.0, *, host=None, port=None):
        return _ipc.NO_PONG

    class _RefusedEndpoint:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            raise OSError("address already in use")

    monkeypatch.setattr(listener._ipc, "ping_status_async", fake_ping)
    monkeypatch.setattr(listener._ipc, "cleanup_endpoint", lambda: None)
    monkeypatch.setattr(listener, "_wire_logging",
                        lambda: order.append("logging"))
    monkeypatch.setattr(listener, "install_json_logging_if_requested",
                        lambda: order.append("json"))
    monkeypatch.setattr(listener, "_reclaim_stale_daemon_ports",
                        lambda cfg: order.append("reclaim"))
    monkeypatch.setattr(listener, "_cleanup_orphan_cdp_chrome", lambda: None)
    monkeypatch.setattr(listener, "PlaywrightFacade", _RefusedEndpoint)

    await listener.run_serve(Config(backend="extension"))

    assert order[:3] == ["logging", "json", "reclaim"]


# ---- 2.4: the control-socket watchdog is gone (ADR-0011) --------------------
#
# It self-exited the daemon when its socket file was removed or replaced, so a
# superseded daemon released the relay/endpoint ports instead of squatting on
# them (issue #15 2.4). With the socket deleted there is nothing to watch, and
# nothing to fix: two daemons can no longer coexist at all, because the second
# one's endpoint bind fails with EADDRINUSE. The mutual exclusion moved from a
# file to the port itself.


def test_the_control_socket_watchdog_is_gone():
    assert not hasattr(listener, "_control_socket_watchdog")
    assert not hasattr(_ipc, "sock_path")
    assert not hasattr(_ipc, "make_unix_socket")
