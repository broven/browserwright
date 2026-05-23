"""PID-reuse guard on daemon stop.

``_cmd_stop`` pings to learn the live daemon's pid, then signals it. Between the
ping and the kill, the daemon could die and the OS could recycle the pid for an
unrelated process — SIGTERM/SIGKILL would then hit an innocent bystander.

Borrowed from browser-use/browser-harness: fingerprint the pid's process
start-time and re-verify it just before each signal. When the platform can't
report a start-time, we degrade to the old behaviour (don't block the stop).
"""
import os
import signal

import pytest

from browserwright.daemon import platforms


# ---- proc_start_time -------------------------------------------------------

def test_start_time_for_self_is_stable_and_nonempty():
    st1 = platforms.proc_start_time(os.getpid())
    assert st1  # truthy on Linux + macOS
    st2 = platforms.proc_start_time(os.getpid())
    assert st1 == st2  # stable across calls for the same process


def test_start_time_for_dead_pid_is_none():
    # pid 99999999 is well above any real pid → no such process.
    assert platforms.proc_start_time(99_999_999) is None


# ---- _cmd_stop guard -------------------------------------------------------

class _Args:
    timeout = 0.2


def _patch_common(monkeypatch, killed: list, pid=4242):
    """Make ping report a live daemon at ``pid`` and record os.kill targets."""
    from browserwright.daemon import cli, _ipc

    monkeypatch.setattr(_ipc, "ping_sync", lambda name, timeout=1.0: pid)
    monkeypatch.setattr(_ipc, "cleanup_endpoint", lambda name: None)

    def fake_kill(target_pid, sig):
        killed.append((target_pid, sig))

    monkeypatch.setattr(cli.os, "kill", fake_kill)
    return cli


def _patch_start_time(monkeypatch, fn):
    # _cmd_stop does `from . import platforms`, so patch the module object.
    from browserwright.daemon import platforms
    monkeypatch.setattr(platforms, "proc_start_time", fn)


def test_stop_kills_when_start_time_matches(monkeypatch):
    killed = []
    cli = _patch_common(monkeypatch, killed)
    # start-time never changes → daemon identity stable → kill proceeds.
    _patch_start_time(monkeypatch, lambda pid: "STABLE")
    # ping keeps returning the pid so SIGTERM doesn't "succeed" early → SIGKILL.
    rc = cli._cmd_stop(_Args(), _cfg())
    assert rc == 0
    assert (4242, signal.SIGTERM) in killed


def test_stop_refuses_when_pid_was_reused(monkeypatch):
    killed = []
    cli = _patch_common(monkeypatch, killed)
    # First read (at ping time) differs from the read just before kill →
    # the pid was recycled → must NOT signal it.
    seq = iter(["ORIGINAL", "REUSED", "REUSED", "REUSED"])
    _patch_start_time(monkeypatch, lambda pid: next(seq))
    rc = cli._cmd_stop(_Args(), _cfg())
    assert rc == 0
    assert killed == []  # innocent bystander spared


def test_stop_degrades_when_start_time_unavailable(monkeypatch):
    killed = []
    cli = _patch_common(monkeypatch, killed)
    # Platform can't report start-time → guard must not block the stop.
    _patch_start_time(monkeypatch, lambda pid: None)
    rc = cli._cmd_stop(_Args(), _cfg())
    assert rc == 0
    assert (4242, signal.SIGTERM) in killed


def _cfg():
    from browserwright.daemon.config import Config
    return Config(name="default")
