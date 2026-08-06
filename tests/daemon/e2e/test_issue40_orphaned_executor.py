"""Issue #40 process-level repro: daemon death orphans the executor.

Real processes, no Chrome: an isolated daemon, a real ledger session, a real
executor subprocess — then the daemon is SIGKILLed. `session end` must recover
WITHOUT the daemon: reap the executor locally and drop the ledger row. The
same must hold for `session reset` (reap locally, keep the row).

Auto-marked `real_chrome` by the e2e conftest (opt-in via path or `-m
real_chrome`) — it spawns real daemon/executor processes, so it stays out of
the default gate.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from browserwright.daemon import _ipc


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _env(runtime: str, home: str) -> dict:
    env = os.environ.copy()
    env.update({
        "XDG_RUNTIME_DIR": runtime,
        "TMPDIR": runtime,
        "BS_HOME": home,
        "BD_CONFIG": "",
        # The isolated daemon auto-spawned by `session new` inherits these, so
        # it never touches the machine-global 19989/19990 ports.
        "BD_EXTENSION_PORT": str(_free_port()),
        "BD_FACADE_PORT": str(_free_port()),
        "BD_RDP_PORT": str(_free_port()),
    })
    return env


def _cli(args, env, timeout: float = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "browserwright", *args],
        capture_output=True, text=True, env=env, timeout=timeout)


def _spawn_executor(session_id: str, env: dict) -> int:
    """Spawn a real executor and return its pid, the way production does:
    the DAEMON is the executor's parent, so the executor is orphaned to init
    when the daemon dies (init reaps the zombie — the CLI's local reap then
    sees the pid go dead). Spawn via a throwaway launcher that exits at once
    so this pytest process is NOT the parent."""
    launcher = (
        "import os, subprocess, sys; "
        "p = subprocess.Popen("
        "[sys.executable, '-m', 'browserwright._executor',"
        "'--session', %r, '--executor-id', %r],"
        "env=os.environ.copy(), start_new_session=True,"
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print(p.pid, flush=True); os._exit(0)"
    ) % (session_id, f"e2e-{session_id}")
    out = subprocess.run([sys.executable, "-c", launcher], env=env,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip())


def _wait_discovery(session_id: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _ipc.read_executor_record(session_id) is not None:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"executor discovery record for session {session_id!r} never appeared")


def _daemon_pid(runtime: str, timeout: float = 10.0) -> int:
    """The daemon pid once it is fully serving. The spawn is detached + async
    (`session new` returns before `serve` binds), and spawning an executor
    before the daemon finishes its startup orphan sweep races the sweep — so
    wait for the control socket to answer, then return the pid."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _ipc.ping_sync(timeout=0.3) is not None:
            pid = _ipc_pid(runtime)
            if pid is not None:
                return pid
        time.sleep(0.05)
    raise AssertionError("daemon never came up on the isolated socket")


def _ipc_pid(runtime: str) -> int | None:
    p = Path(runtime) / "browserwright-daemon.pid"
    try:
        return int(p.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _kill_hard(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # Give the OS a moment to close the daemon's sockets, so the CLI's
    # connect-refused is deterministic (not a half-open socket hang).
    time.sleep(0.5)


def _pid_dead(pid: int) -> bool:
    """True when no process (not even a zombie) answers for ``pid``."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    # A zombie answers kill(0) — but init reaps orphans promptly, so after
    # the reap grace a lingering zombie means "not our executor anymore".
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        return reaped == pid
    except (ChildProcessError, OSError):
        return False


def _ledger_sessions(home: str) -> dict:
    p = Path(home) / "sessions" / "ledger.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text()).get("sessions", {})


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    """Isolated runtime dir + BS_HOME, env for CLI subprocesses, and the
    in-process IPC path pointed at the same runtime dir."""
    runtime = tempfile.mkdtemp(prefix="bw-issue40-e2e-", dir="/tmp")
    home = str(tmp_path / "bs-home")
    Path(home).mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
    monkeypatch.setenv("BS_HOME", home)
    env = _env(runtime, home)
    try:
        yield _Scenario(runtime=runtime, home=home, env=env)
    finally:
        import shutil
        shutil.rmtree(runtime, ignore_errors=True)


class _Scenario:
    def __init__(self, *, runtime: str, home: str, env: dict) -> None:
        self.runtime = runtime
        self.home = home
        self.env = env


def test_issue40_session_end_recovers_after_daemon_sigkill(scenario):
    """The issue's exact sequence: live session → daemon dies hard → `session
    end` must reap the executor locally and drop the ledger row."""
    env = scenario.env
    # 1. Create a session (allocates the ledger row; auto-spawns the isolated
    #    daemon via `_ensure_daemon_running`).
    created = _cli(["session", "new", "--backend=cdp", "--name=issue40-e2e",
                    "--create"], env)
    assert created.returncode == 0, created.stderr
    sid = created.stdout.strip()
    assert _daemon_pid(scenario.runtime) is not None

    # 2. A real executor is live for the session (what `ensureExecutor`
    #    spawns; no Chrome needed — cold-start is lazy). Its parent is a
    #    throwaway launcher, so a daemon death orphans it to init exactly as
    #    in production.
    exec_pid = _spawn_executor(sid, env)
    _wait_discovery(sid)
    assert not _pid_dead(exec_pid)

    # 3. The daemon dies hard — SIGKILL, no graceful teardown.
    _kill_hard(_daemon_pid(scenario.runtime))

    # 4. `session end` must now recover WITHOUT the daemon: exit 0, executor
    #    reaped, ledger row dropped.
    ended = _cli(["session", "end", "--session", sid], env)
    assert ended.returncode == 0, f"stderr: {ended.stderr}"
    assert sid not in _ledger_sessions(scenario.home), \
        "the ledger row must not leak after the executor is provably gone"
    assert _pid_dead(exec_pid), "the orphaned executor must be reaped"


def test_issue40_session_reset_recovers_after_daemon_sigkill(scenario):
    """`session reset` recovers the same way — and keeps the ledger row
    (reset recycles only the executor)."""
    env = scenario.env
    created = _cli(["session", "new", "--backend=cdp", "--name=issue40-reset",
                    "--create"], env)
    assert created.returncode == 0, created.stderr
    sid = created.stdout.strip()

    exec_pid = _spawn_executor(sid, env)
    _wait_discovery(sid)
    assert not _pid_dead(exec_pid)

    _kill_hard(_daemon_pid(scenario.runtime))

    reset = _cli(["session", "reset", sid], env)
    assert reset.returncode == 0, f"stderr: {reset.stderr}"
    assert sid in _ledger_sessions(scenario.home), \
        "reset recycles the executor; the ledger row must survive"
    assert _pid_dead(exec_pid), "the orphaned executor must be reaped"
