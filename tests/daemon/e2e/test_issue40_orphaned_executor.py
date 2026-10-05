"""Issue #40 process-level repro: daemon death orphans the executor.

Real processes, no Chrome: an isolated daemon, a real ledger session, a real
executor subprocess — then the daemon is SIGKILLed. `session end` must recover
WITHOUT the daemon: reap the executor locally and drop the ledger row. The
same must hold for `session reset` (reap locally, keep the row).

Auto-marked `real_chrome` by the e2e conftest (opt-in via path or `-m
real_chrome`) — it spawns real daemon/executor processes, so it stays out of
the default gate.

Endpoint isolation: the CLI subprocesses must reach the private daemon, never
the machine-global one on 19990 (ADR-0012). `BW_DAEMON_URL` would do that but
also makes the endpoint *explicit*, which turns off the auto-start that
`session reset`'s recovery path relies on (ADR-0011). So the fixture starts
the private daemon itself and lets the CLI find it the non-explicit way: the
endpoint state file the daemon publishes in this test's `XDG_RUNTIME_DIR`. A
SIGKILL leaves that file behind, so after the kill the CLI still resolves the
private (now dead) port, and any auto-start it does binds the private ports
from `BD_*`. Every CLI call asserts this before it runs.
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
    # An inherited explicit endpoint would point every CLI call elsewhere and
    # disable the auto-start this test depends on (see module docstring).
    env.pop("BW_DAEMON_URL", None)
    env.update({
        "XDG_RUNTIME_DIR": runtime,
        "TMPDIR": runtime,
        "BS_HOME": home,
        "BD_CONFIG": "",
        # The private daemon (started by the fixture, or re-spawned by a CLI
        # auto-start) binds these, never the machine-global 19989/19990.
        "BD_EXTENSION_PORT": str(_free_port()),
        "BD_FACADE_PORT": str(_free_port()),
        "BD_RDP_PORT": str(_free_port()),
    })
    return env


def _published_url(runtime: str) -> str | None:
    """The endpoint URL the private daemon published in ``runtime``."""
    try:
        data = json.loads((Path(runtime) / "browserwright-daemon.endpoint")
                          .read_text())
    except (OSError, ValueError):
        return None
    return data.get("url") if isinstance(data, dict) else None


def _private_url(env: dict) -> str:
    return f"http://127.0.0.1:{env['BD_FACADE_PORT']}"


def _cli(args, env, timeout: float = 90) -> subprocess.CompletedProcess:
    # Guard: the CLI resolves its endpoint from the state file in this test's
    # runtime dir. If that ever stops naming the private port, the call would
    # fall through to the default 19990 — the machine-global daemon.
    published = _published_url(env["XDG_RUNTIME_DIR"])
    assert published == _private_url(env), (
        f"the CLI would not resolve the private daemon (state file names "
        f"{published!r}, expected {_private_url(env)!r}); refusing to run it "
        f"against the default endpoint")
    return subprocess.run(
        [sys.executable, "-m", "browserwright", *args],
        capture_output=True, text=True, env=env, timeout=timeout)


def _start_private_daemon(env: dict, timeout: float = 15.0) -> int:
    """Start the isolated daemon and return its pid once it serves the
    private port and has published that port in the state file."""
    subprocess.Popen(
        [sys.executable, "-m", "browserwright.daemon.cli", "serve"],
        env=env, start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port = int(env["BD_FACADE_PORT"])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pr = _ipc.probe_endpoint_sync("127.0.0.1", port, timeout=0.3)
        if (pr.kind == "ours" and pr.pid is not None
                and _published_url(env["XDG_RUNTIME_DIR"]) == _private_url(env)):
            return pr.pid
        time.sleep(0.05)
    raise AssertionError(f"the private daemon never came up on port {port}")


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


def _private_daemon_pid(env: dict) -> int | None:
    """The pid of whatever daemon answers on the private port, if any."""
    pr = _ipc.probe_endpoint_sync("127.0.0.1", int(env["BD_FACADE_PORT"]),
                                  timeout=0.3)
    return pr.pid if pr.kind == "ours" else None


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
    scenario = _Scenario(runtime=runtime, home=home, env=env)
    try:
        scenario.daemon_pid = _start_private_daemon(env)
        yield scenario
    finally:
        # Reap only what this test owns: a daemon answering on the private
        # port (e.g. the one `session reset` auto-started) and any executor
        # this test spawned. Then the private runtime dir.
        pid = _private_daemon_pid(env)
        if pid is not None:
            _kill_hard(pid)
        # An executor the test expected reaped but that survived (a failed
        # run) — signalled only if the pid still names the process we
        # spawned, so a recycled pid is never hit.
        from browserwright.daemon.platforms import proc_start_time
        for exec_pid, started in scenario.executors:
            if started is not None and proc_start_time(exec_pid) == started:
                _kill_hard(exec_pid)
        import shutil
        shutil.rmtree(runtime, ignore_errors=True)


class _Scenario:
    def __init__(self, *, runtime: str, home: str, env: dict) -> None:
        self.runtime = runtime
        self.home = home
        self.env = env
        self.daemon_pid: int | None = None
        #: (pid, start time) of every executor the test spawned.
        self.executors: list[tuple[int, object]] = []

    def track_executor(self, pid: int) -> None:
        from browserwright.daemon.platforms import proc_start_time
        self.executors.append((pid, proc_start_time(pid)))


def test_issue40_session_end_recovers_after_daemon_sigkill(scenario):
    """The issue's exact sequence: live session → daemon dies hard → `session
    end` must reap the executor locally and drop the ledger row."""
    env = scenario.env
    # 1. Create a session against the live private daemon (allocates the
    #    ledger row; `daemon_lifecycle.ensure` finds the daemon up).
    created = _cli(["session", "new", "--backend=cdp", "--name=issue40-e2e",
                    "--create"], env)
    assert created.returncode == 0, created.stderr
    sid = created.stdout.strip()
    assert _private_daemon_pid(env) == scenario.daemon_pid

    # 2. A real executor is live for the session (what `ensureExecutor`
    #    spawns; no Chrome needed — cold-start is lazy). Its parent is a
    #    throwaway launcher, so a daemon death orphans it to init exactly as
    #    in production.
    exec_pid = _spawn_executor(sid, env)
    scenario.track_executor(exec_pid)
    _wait_discovery(sid)
    assert not _pid_dead(exec_pid)

    # 3. The daemon dies hard — SIGKILL, no graceful teardown.
    _kill_hard(scenario.daemon_pid)
    assert _private_daemon_pid(env) is None

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

    assert _private_daemon_pid(env) == scenario.daemon_pid

    exec_pid = _spawn_executor(sid, env)
    scenario.track_executor(exec_pid)
    _wait_discovery(sid)
    assert not _pid_dead(exec_pid)

    _kill_hard(scenario.daemon_pid)
    assert _private_daemon_pid(env) is None

    reset = _cli(["session", "reset", sid], env)
    assert reset.returncode == 0, f"stderr: {reset.stderr}"
    assert sid in _ledger_sessions(scenario.home), \
        "reset recycles the executor; the ledger row must survive"
    assert _pid_dead(exec_pid), "the orphaned executor must be reaped"
