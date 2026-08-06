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

import asyncio
import json
import os
from types import SimpleNamespace

from browserwright.daemon import _ipc, _stale, cli
from browserwright.daemon.config import Config
from browserwright.daemon.probe import DaemonProbe
from browserwright.daemon.server import listener


def _ns(**kw):
    kw.setdefault("json", True)
    return SimpleNamespace(**kw)


class _StubProbe(DaemonProbe):
    """A daemon that never answers, with scriptable ports and socket file."""

    retry_window = 0.05  # the retry loop is real; just don't dawdle in it

    def __init__(self, *, socket_present, ports=(), holder=None):
        super().__init__(Config())
        self._socket_present = socket_present
        self._ports = list(ports)
        self._holder = holder
        self.probed_ports = []

    def ping(self, timeout):
        return (None, None)

    def socket_present(self):
        return self._socket_present

    def daemon_ports(self):
        return list(self._ports)

    def listening_ports(self, ports):
        self.probed_ports.extend(ports)
        return list(ports)

    def confirmed_stale_holder(self, ports):
        return self._holder

    def endpoint(self):
        return {"schema_version": 1, "transport": "unix", "path": "/dev/null"}

    def facade(self):
        return (None, None)

    def sleep(self, seconds):
        pass  # skip the retry backoff


# ---- 2.1: status reports the port-held zombie -------------------------------


def test_cmd_status_reports_port_held_zombie(capsys):
    # Socket file present (a half-alive daemon left it) but nothing answers,
    # and lsof names a confirmed browserwright process on the ports.
    probe = _StubProbe(socket_present=True, ports=[29971, 29972], holder=4242)

    rc = cli._cmd_status(_ns(json=True), Config(), probe=probe)

    out = json.loads(capsys.readouterr().out)
    assert out["probe_state"] == "port_held_by_unresponsive_process"
    assert out["port_holder_pid"] == 4242
    assert out["alive"] is False
    assert rc == 2


def test_cmd_status_not_running_when_no_socket_file(capsys):
    # No socket file → the port-held probe must NOT fire (don't pick up an
    # unrelated daemon holding the default ports).
    probe = _StubProbe(socket_present=False, ports=[29971])

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

    probe = _Blind(socket_present=True, ports=[29971], holder=None)

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
    def __init__(self, *, ports, held=None, holder=None):
        super().__init__(Config())
        self._ports = list(ports)
        self._held = list(ports if held is None else held)
        self._holder = holder
        self.holder_lookups = []

    def daemon_ports(self):
        return list(self._ports)

    def listening_ports(self, ports):
        return [p for p in ports if p in self._held]

    def confirmed_stale_holder(self, ports):
        self.holder_lookups.append(list(ports))
        return self._holder


def test_reclaim_kills_confirmed_stale_daemon(monkeypatch):
    reclaimed = []
    monkeypatch.setattr(_stale, "reclaim_ports",
                        lambda pid, ports: reclaimed.append((pid, ports)) or True)

    listener._reclaim_stale_daemon_ports(
        Config(), probe=_PortProbe(ports=[29971, 29972], holder=999))

    assert reclaimed == [(999, [29971, 29972])]


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


# ---- 2.4: control-socket watchdog self-exits --------------------------------


async def test_watchdog_self_exits_on_socket_removal(monkeypatch, tmp_path):
    sock = tmp_path / "d.sock"
    sock.write_text("")
    monkeypatch.setattr(_ipc, "sock_path", lambda: sock)
    st = sock.stat()
    stop = asyncio.Event()
    task = asyncio.create_task(
        listener._control_socket_watchdog(
            (st.st_dev, st.st_ino), stop, interval=0.05))

    await asyncio.sleep(0.15)
    assert not stop.is_set()  # socket present → still serving

    sock.unlink()
    await asyncio.wait_for(task, timeout=2)
    assert stop.is_set()  # gone → self-exit requested


async def test_watchdog_self_exits_on_socket_replacement(monkeypatch, tmp_path):
    sock = tmp_path / "d.sock"
    sock.write_text("")
    monkeypatch.setattr(_ipc, "sock_path", lambda: sock)
    st = sock.stat()
    stop = asyncio.Event()
    task = asyncio.create_task(
        listener._control_socket_watchdog(
            (st.st_dev, st.st_ino), stop, interval=0.05))

    await asyncio.sleep(0.15)
    assert not stop.is_set()

    # Atomically replace with a different inode (another daemon rebound it).
    other = tmp_path / "other.sock"
    other.write_text("new")
    os.replace(other, sock)
    await asyncio.wait_for(task, timeout=2)
    assert stop.is_set()


async def test_watchdog_stays_quiet_while_socket_stable(monkeypatch, tmp_path):
    sock = tmp_path / "d.sock"
    sock.write_text("")
    monkeypatch.setattr(_ipc, "sock_path", lambda: sock)
    st = sock.stat()
    stop = asyncio.Event()
    task = asyncio.create_task(
        listener._control_socket_watchdog(
            (st.st_dev, st.st_ino), stop, interval=0.05))
    await asyncio.sleep(0.25)  # several poll cycles, socket unchanged
    assert not stop.is_set()
    stop.set()  # normal shutdown path
    await asyncio.wait_for(task, timeout=2)
