"""Unit coverage for stale/half-alive daemon detection + reclaim (issue #15 P2).

These exercise the pure logic with lsof/ps/os.kill mocked, plus one real bind
for `port_is_listening`. No daemon process is spawned.
"""
from __future__ import annotations

import socket
from contextlib import closing
from types import SimpleNamespace

import pytest

from browserwright.daemon import _stale, supervise


def test_port_is_listening_true_and_false():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        assert _stale.port_is_listening("127.0.0.1", port) is True
    # After the `with` closes the socket, the port is free again.
    assert _stale.port_is_listening("127.0.0.1", port) is False


def test_port_holder_pids_parses_lsof(monkeypatch):
    monkeypatch.setattr(_stale.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout="123\n456\n123\n"))
    assert _stale.port_holder_pids(19989) == [123, 456]


def test_port_holder_pids_lsof_missing_returns_empty(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("lsof")
    monkeypatch.setattr(_stale.subprocess, "run", _boom)
    assert _stale.port_holder_pids(19989) == []


@pytest.mark.parametrize("cmd,expected", [
    ("/Users/x/.local/bin/browserwright-daemon serve", True),
    ("python -m browserwright.daemon.cli serve --backend env", True),
    ("/usr/bin/vim notes.md", False),
    ("browserwright -s 3 -e print(1)", False),  # the CLI, not the daemon
    ("", False),
])
def test_pid_is_browserwright_daemon(monkeypatch, cmd, expected):
    monkeypatch.setattr(_stale.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=cmd + "\n"))
    assert _stale.pid_is_browserwright_daemon(4321) is expected


def test_confirmed_stale_holder_requires_daemon_cmdline(monkeypatch):
    monkeypatch.setattr(_stale, "port_holder_pids", lambda port: [999])
    monkeypatch.setattr(_stale.os, "getpid", lambda: 1)
    # Holder is NOT a browserwright daemon → refuse to nominate it for a kill.
    monkeypatch.setattr(_stale, "pid_is_browserwright_daemon", lambda pid: False)
    assert _stale.confirmed_stale_holder([19989]) is None
    # Holder IS a browserwright daemon → return it.
    monkeypatch.setattr(_stale, "pid_is_browserwright_daemon", lambda pid: True)
    assert _stale.confirmed_stale_holder([19989]) == 999


def test_confirmed_stale_holder_skips_self(monkeypatch):
    monkeypatch.setattr(_stale.os, "getpid", lambda: 999)
    monkeypatch.setattr(_stale, "port_holder_pids", lambda port: [999])
    monkeypatch.setattr(_stale, "pid_is_browserwright_daemon", lambda pid: True)
    assert _stale.confirmed_stale_holder([19989]) is None


# ---- pid_env / runtime-dir identity (issue #44 B) --------------------------
#
# The reclaim must never SIGTERM a browserwright daemon from a DIFFERENT
# runtime dir: that is someone else's live daemon (the machine-global one, or a
# sibling worktree's isolated e2e daemon), not a stale daemon of ours.


def test_pid_env_reads_proc_environ(monkeypatch, tmp_path):
    environ = tmp_path / "environ"
    environ.write_bytes(b"PATH=/usr/bin\0XDG_RUNTIME_DIR=/tmp/bd-e2e-x\0FOO=1\0")
    real_path = _stale.Path

    def _fake_path(*a):
        return environ if a == ("/proc/42/environ",) else real_path(*a)

    monkeypatch.setattr(_stale, "Path", _fake_path)
    assert _stale.pid_env(42, "XDG_RUNTIME_DIR") == "/tmp/bd-e2e-x"
    assert _stale.pid_env(42, "PATH") == "/usr/bin"
    # A var that is not set in the file → None (unset, not "").
    assert _stale.pid_env(42, "HOME") is None


def test_pid_env_parses_bsd_ps_eww(monkeypatch):
    env_col = "sleep 5 XDG_RUNTIME_DIR=/tmp/bd-e2e-y FOO=bar"
    monkeypatch.setattr(_stale.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=env_col + "\n"))
    assert _stale.pid_env(7, "XDG_RUNTIME_DIR") == "/tmp/bd-e2e-y"
    assert _stale.pid_env(7, "FOO") == "bar"
    assert _stale.pid_env(7, "MISSING") is None


def test_pid_env_unreadable_returns_none(monkeypatch):
    monkeypatch.setattr(_stale.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError))
    assert _stale.pid_env(7, "XDG_RUNTIME_DIR") is None


def test_pid_runtime_dir_defaults_to_tmp_when_unset(monkeypatch):
    monkeypatch.setattr(_stale, "pid_env", lambda pid, name: None)
    assert _stale.pid_runtime_dir(123) == "/tmp"


def test_pid_runtime_dir_echoes_set_value(monkeypatch):
    monkeypatch.setattr(_stale, "pid_env",
                        lambda pid, name: "/tmp/bd-e2e-z")
    assert _stale.pid_runtime_dir(123) == "/tmp/bd-e2e-z"


def test_same_runtime_dir_matches_only_our_dir(monkeypatch):
    # Ours: XDG_RUNTIME_DIR unset → /tmp. A holder WITHOUT the var (the
    # machine-global daemon) lives at /tmp too → same dir → reclaimable.
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(_stale, "pid_env", lambda pid, name: None)
    assert _stale.same_runtime_dir_as_us(1) is True
    # A holder with a DIFFERENT XDG_RUNTIME_DIR (an isolated e2e daemon) is
    # someone else's → must refuse.
    monkeypatch.setattr(_stale, "pid_env",
                        lambda pid, name: "/tmp/bd-e2e-other")
    assert _stale.same_runtime_dir_as_us(1) is False


def test_same_runtime_dir_compares_resolved_paths(monkeypatch, tmp_path):
    # Same dir reachable through a symlink still matches (macOS /tmp →
    # /private/tmp is the real-world case).
    real = tmp_path / "runtime"
    real.mkdir()
    link = tmp_path / "runtime-link"
    link.symlink_to(real)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(link))
    monkeypatch.setattr(_stale, "pid_env", lambda pid, name: str(real))
    assert _stale.same_runtime_dir_as_us(1) is True


def test_reclaim_ports_sigterm_frees(monkeypatch):
    killed = []
    monkeypatch.setattr(supervise.os, "kill",
                        lambda pid, sig: killed.append((pid, sig)))
    # Port frees immediately after SIGTERM.
    monkeypatch.setattr(_stale, "port_is_listening", lambda h, p: False)
    assert _stale.reclaim_ports(999, [19989, 19990]) is True
    assert killed == [(999, supervise.signal.SIGTERM)]


def test_reclaim_ports_escalates_to_sigkill(monkeypatch):
    killed = []
    monkeypatch.setattr(supervise.os, "kill",
                        lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(supervise.time, "sleep", lambda s: None)
    # Never frees → SIGTERM wait times out → SIGKILL, still held.
    monkeypatch.setattr(_stale, "port_is_listening", lambda h, p: True)
    monkeypatch.setattr(supervise.time, "monotonic",
                        _fake_monotonic([0, 6, 6, 9]))
    assert _stale.reclaim_ports(999, [19989]) is False
    assert (999, supervise.signal.SIGKILL) in killed


def _fake_monotonic(values):
    it = iter(values)
    last = [0.0]

    def _now():
        try:
            last[0] = next(it)
        except StopIteration:
            last[0] += 10
        return last[0]
    return _now
