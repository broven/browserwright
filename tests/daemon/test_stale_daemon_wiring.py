"""Wiring coverage for the issue #15 P2 fixes: status port-held reporting (2.1),
serve reclaim (2.2), and the control-socket self-exit watchdog (2.4).

These mock `_stale`'s side-effecting probes so no daemon/process is touched.
The real signal-driven takeover is verified by hand against a live daemon.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

from browserwright.daemon import _ipc, _stale, cli
from browserwright.daemon.config import Config
from browserwright.daemon.server import listener


def _ns(**kw):
    kw.setdefault("json", True)
    return SimpleNamespace(**kw)


# ---- 2.1: status reports the port-held zombie -------------------------------


def test_cmd_status_reports_port_held_zombie(monkeypatch, capsys, tmp_path):
    sock = tmp_path / "d.sock"
    sock.write_text("")  # sock_path().exists() → True (half-alive left its file)
    monkeypatch.setattr(_ipc, "sock_path", lambda: sock)
    monkeypatch.setattr(_ipc, "ping_status_sync", lambda timeout: (None, None))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)  # skip retry backoff
    monkeypatch.setattr(_stale, "daemon_tcp_ports", lambda cfg: [29971, 29972])
    monkeypatch.setattr(_stale, "port_is_listening", lambda h, p: True)
    monkeypatch.setattr(_stale, "confirmed_stale_holder", lambda ports: 4242)

    rc = cli._cmd_status(_ns(json=True), Config())

    out = json.loads(capsys.readouterr().out)
    assert out["probe_state"] == "port_held_by_unresponsive_process"
    assert out["port_holder_pid"] == 4242
    assert out["alive"] is False
    assert rc == 2


def test_cmd_status_not_running_when_no_socket_file(monkeypatch, capsys, tmp_path):
    # No socket file → the port-held probe must NOT fire (don't pick up an
    # unrelated daemon holding the default ports).
    monkeypatch.setattr(_ipc, "sock_path", lambda: tmp_path / "absent.sock")
    monkeypatch.setattr(_ipc, "ping_status_sync", lambda timeout: (None, None))
    held = []
    monkeypatch.setattr(_stale, "port_is_listening",
                        lambda h, p: held.append(p) or True)

    rc = cli._cmd_status(_ns(json=True), Config())

    out = json.loads(capsys.readouterr().out)
    assert out["probe_state"] == "not_running"
    assert out["port_holder_pid"] is None
    assert held == []  # never probed ports
    assert rc == 2


# ---- 2.2: serve reclaims only a CONFIRMED stale daemon ----------------------


def test_reclaim_kills_confirmed_stale_daemon(monkeypatch):
    monkeypatch.setattr(_stale, "daemon_tcp_ports", lambda cfg: [29971, 29972])
    monkeypatch.setattr(_stale, "port_is_listening", lambda h, p: True)
    monkeypatch.setattr(_stale, "confirmed_stale_holder", lambda ports: 999)
    reclaimed = []
    monkeypatch.setattr(_stale, "reclaim_ports",
                        lambda pid, ports: reclaimed.append((pid, ports)) or True)

    listener._reclaim_stale_daemon_ports(Config())

    assert reclaimed == [(999, [29971, 29972])]


def test_reclaim_never_kills_unconfirmed_holder(monkeypatch):
    # Ports held, but no confirmed browserwright daemon owns them → must NOT
    # signal anyone (safety: never kill a stranger).
    monkeypatch.setattr(_stale, "daemon_tcp_ports", lambda cfg: [29971])
    monkeypatch.setattr(_stale, "port_is_listening", lambda h, p: True)
    monkeypatch.setattr(_stale, "confirmed_stale_holder", lambda ports: None)
    reclaimed = []
    monkeypatch.setattr(_stale, "reclaim_ports",
                        lambda pid, ports: reclaimed.append(pid))

    listener._reclaim_stale_daemon_ports(Config())

    assert reclaimed == []


def test_reclaim_noop_when_ports_free(monkeypatch):
    monkeypatch.setattr(_stale, "daemon_tcp_ports", lambda cfg: [29971])
    monkeypatch.setattr(_stale, "port_is_listening", lambda h, p: False)
    called = []
    monkeypatch.setattr(_stale, "confirmed_stale_holder",
                        lambda ports: called.append(1))

    listener._reclaim_stale_daemon_ports(Config())

    assert called == []  # never even looked for a holder


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
