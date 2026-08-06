"""Issue #40 regression tests: daemon death orphans the executor.

When the global daemon goes down while a session is live, neither `session
end` nor `session reset` can reap the executor (both need the daemon to
confirm), the executor stays attached to its Chrome tab, and the ledger entry
leaks "for retry" forever — so the next bind fails with a CDP attach conflict
against the session's own orphan.

The fix (issue #40): when the daemon is unreachable, Layer 2 reaps the
executor LOCALLY from its on-disk discovery record — pid + start-time
fingerprint, the same graded TERM→KILL discipline as the daemon's startup
orphan sweep — and `session end` force-drops the ledger entry when the
executor is provably gone instead of keeping it for a retry that can never
succeed.

These tests mock only the daemon-CLI boundary (`_run` fails,
`_daemon_is_running` says no, `_ensure_daemon_running` is a no-op). The
executor is a REAL subprocess and the discovery files are real, so the local
reap is exercised for real.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time

import pytest

from browserwright.daemon import _ipc


def _isolated_runtime(monkeypatch) -> str:
    """A short runtime dir for the AF_UNIX socket (macOS sun_path budget:
    pytest's tmp_path under /private/var/folders is too long)."""
    runtime = tempfile.mkdtemp(prefix="bw-issue40-", dir="/tmp")
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
    monkeypatch.setenv("TMPDIR", runtime)
    return runtime


def _spawn_executor(session_id: str, runtime: str, bs_home: str) -> subprocess.Popen:
    """A real executor subprocess, spawned the way the daemon spawns it
    (`start_new_session=True` → own process group, so a group TERM can never
    touch the test process)."""
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = runtime
    env["BS_HOME"] = bs_home
    return subprocess.Popen(
        [sys.executable, "-m", "browserwright._executor",
         "--session", session_id, "--executor-id", f"repro-{session_id}"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_discovery(session_id: str, timeout: float = 10.0) -> dict:
    """Wait for the executor's discovery file (socket bound) to appear."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = _ipc.read_executor_record(session_id)
        if record is not None:
            return record
        time.sleep(0.05)
    raise AssertionError(
        f"executor discovery record for session {session_id!r} never appeared")


def _dead_pid() -> int:
    """A pid that is guaranteed dead (the child was reaped)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    return proc.pid


@pytest.fixture
def daemon_down(monkeypatch):
    """The daemon-CLI boundary: every daemon call fails, the daemon is
    unreachable, and nothing tries to auto-start a real daemon."""
    from browserwright import session_create
    monkeypatch.setattr(session_create, "_run", lambda cmd, **kwargs: 3)
    monkeypatch.setattr(session_create, "_daemon_is_running", lambda: False)
    monkeypatch.setattr(session_create, "_ensure_daemon_running", lambda: None)
    yield session_create


def _alloc(monkeypatch) -> str:
    from browserwright import session_registry as reg
    monkeypatch.setenv("BS_HOME", str(tempfile.mkdtemp(prefix="bw-issue40-home-")))
    return reg.allocate(backend="cdp", owner="attach", name="issue40-repro")


def test_end_force_drops_ledger_row_when_executor_dead_and_daemon_down(
    tmp_path, monkeypatch, daemon_down,
):
    """A session whose executor is provably gone (dead pid in the discovery
    record) must not be kept "for retry" when the daemon is unreachable —
    the retry can never succeed, so the row is force-dropped."""
    from browserwright import session_registry as reg
    from browserwright import session_create
    _isolated_runtime(monkeypatch)
    sid = _alloc(monkeypatch)
    # A discovery record naming a dead pid → executor provably gone.
    _ipc.write_executor_file(
        sid, str(_ipc.executor_sock_path(sid)), _dead_pid())
    record = reg.get(sid)

    message = session_create.end(record)

    assert "ended" in message
    assert reg.get(sid) is None, \
        "the ledger row must be force-dropped once the executor is provably gone"


def test_end_locally_reaps_live_executor_when_daemon_down(
    tmp_path, monkeypatch, daemon_down,
):
    """A session whose executor is STILL ALIVE is recovered by a local
    fingerprint-guarded reap; the ledger row is dropped afterwards."""
    from browserwright import session_registry as reg
    from browserwright import session_create
    runtime = _isolated_runtime(monkeypatch)
    sid = _alloc(monkeypatch)
    proc = _spawn_executor(sid, runtime, str(tmp_path))
    _wait_discovery(sid)
    try:
        message = session_create.end(reg.get(sid))
    finally:
        if proc.poll() is None:  # belt-and-suspenders: never leak a child
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=10)
    assert "ended" in message
    assert proc.poll() is not None, "the executor must be reaped locally"
    assert _ipc.read_executor_record(sid) is None, \
        "the stale discovery record must be cleaned up"
    assert reg.get(sid) is None


def test_end_drops_row_without_any_executor_record_when_daemon_down(
    tmp_path, monkeypatch, daemon_down,
):
    """No discovery record at all = no executor ever existed (or it already
    cleaned up after itself) → provably gone → row dropped."""
    from browserwright import session_registry as reg
    from browserwright import session_create
    _isolated_runtime(monkeypatch)
    sid = _alloc(monkeypatch)
    record = reg.get(sid)

    message = session_create.end(record)

    assert "ended" in message
    assert reg.get(sid) is None


def test_reset_locally_reaps_live_executor_when_daemon_down(
    tmp_path, monkeypatch, daemon_down,
):
    """`session reset` reaps the orphan locally when the daemon is
    unreachable — but NEVER drops the ledger row (reset only recycles the
    executor; the session itself stays)."""
    from browserwright import session_registry as reg
    from browserwright import session_create
    runtime = _isolated_runtime(monkeypatch)
    sid = _alloc(monkeypatch)
    proc = _spawn_executor(sid, runtime, str(tmp_path))
    _wait_discovery(sid)
    try:
        message = session_create.reset_executor(reg.get(sid))
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=10)
    assert "reset" in message
    assert proc.poll() is not None, "the executor must be reaped locally"
    assert _ipc.read_executor_record(sid) is None
    assert reg.get(sid) is not None, \
        "reset recycles the executor; the ledger row must survive"


def test_reset_succeeds_when_executor_gone_and_daemon_down(
    tmp_path, monkeypatch, daemon_down,
):
    """`session reset` with no executor at all is trivially successful."""
    from browserwright import session_registry as reg
    from browserwright import session_create
    _isolated_runtime(monkeypatch)
    sid = _alloc(monkeypatch)

    message = session_create.reset_executor(reg.get(sid))

    assert "reset" in message
    assert reg.get(sid) is not None
