"""Unit coverage for the Phase B PR2 executor supervision (no real subprocess):

  - idle-reap: a stale executor (discovery-file mtime older than the threshold)
    is SIGTERMed + popped; a fresh one is left alone.
  - endSession kill: the endSession verb kills + pops the session's executor for
    BOTH rdp and extension backends (symmetric).
  - crash-reap: a dead child is dropped so the next `ensure()` cold-respawns.
  - shutdown kill-all: every registered executor is signalled + dropped.
  - orphan-sweep: stale `bw-exec-*` sockets/discovery files are unlinked on
    startup (and a still-alive orphan pid is signalled).
  - isolation: two concurrent sessions get distinct executors with no cross-talk
    on kill.

All process signalling is stubbed (`_terminate` monkeypatched) so the suite is
hermetic — no daemon, no real Chrome, no real subprocess. A separate test
exercises the real `_terminate` signal discipline against a short-lived child.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from browserwright.daemon import _ipc
from browserwright.daemon.server import executor_registry as er
from browserwright.daemon.server.executor_registry import (
    ExecutorHandle,
    ExecutorRegistry,
)


class _FakeProc:
    def __init__(self, alive: bool = True, pid: int = 4242):
        self._alive = alive
        self.pid = pid
        self.returncode = None if alive else 0

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False
        self.returncode = 0


def _handle(session_id: str, alive: bool = True) -> ExecutorHandle:
    return ExecutorHandle(
        session_id=session_id,
        proc=_FakeProc(alive),
        sock_path=f"/tmp/bw-exec-{session_id}.sock",
    )


@pytest.fixture
def stub_terminate(monkeypatch):
    """Record kills instead of actually signalling a (fake) pid."""
    killed: list[str] = []

    def _fake(handle):
        handle.proc.terminate()
        killed.append(handle.session_id)

    monkeypatch.setattr(er, "_terminate", _fake)
    # Don't touch the real runtime dir's discovery files on cleanup.
    monkeypatch.setattr(_ipc, "cleanup_executor", lambda *_a, **_k: None)
    return killed


# ---- idle reap -------------------------------------------------------------


def test_reap_idle_signals_and_pops_stale(stub_terminate, monkeypatch):
    reg = ExecutorRegistry()
    stale = _handle("sess-stale")
    fresh = _handle("sess-fresh")
    reg._handles[stale.session_id] = stale
    reg._handles[fresh.session_id] = fresh

    # idle_seconds: stale is old, fresh is new.
    def _idle(self, *, now=None):
        return 999.0 if self.session_id == "sess-stale" else 0.0

    monkeypatch.setattr(ExecutorHandle, "idle_seconds", _idle)

    reaped = reg.reap_idle(idle_after=30.0)
    assert reaped == ["sess-stale"]
    assert stub_terminate == ["sess-stale"]
    assert reg.get("sess-stale") is None
    assert reg.get("sess-fresh") is fresh  # untouched


def test_reap_idle_skips_dead_handles(stub_terminate, monkeypatch):
    reg = ExecutorRegistry()
    dead = _handle("sess-dead", alive=False)
    reg._handles[dead.session_id] = dead
    monkeypatch.setattr(ExecutorHandle, "idle_seconds",
                        lambda self, *, now=None: 999.0)
    # A dead handle is the crash-reaper's job, not idle-reap — don't double-kill.
    assert reg.reap_idle(idle_after=1.0) == []
    assert stub_terminate == []


# ---- crash reap ------------------------------------------------------------


def test_reap_dead_drops_exited_child(stub_terminate):
    reg = ExecutorRegistry()
    dead = _handle("sess-x", alive=False)
    live = _handle("sess-y", alive=True)
    reg._handles[dead.session_id] = dead
    reg._handles[live.session_id] = live
    assert reg.reap_dead() == ["sess-x"]
    assert reg.get("sess-x") is None
    assert reg.get("sess-y") is live


@pytest.mark.asyncio
async def test_reaped_dead_handle_cold_respawns(stub_terminate, monkeypatch):
    reg = ExecutorRegistry()
    spawned = {"n": 0}

    async def fake_spawn(session_id):
        spawned["n"] += 1
        return _handle(f"{session_id}")

    monkeypatch.setattr(reg, "_spawn", fake_spawn)
    await reg.ensure("sess-c")
    spawned["n"] = 0  # reset count after the initial spawn

    # Child dies on its own (Fork 4 self-exit) → crash-reaper drops it.
    reg.get("sess-c").proc._alive = False
    reg.get("sess-c").proc.returncode = 0
    assert reg.reap_dead() == ["sess-c"]
    # Next ensure cold-respawns.
    await reg.ensure("sess-c")
    assert spawned["n"] == 1


# ---- kill / kill_all -------------------------------------------------------


def test_kill_signals_and_pops(stub_terminate):
    reg = ExecutorRegistry()
    reg._handles["sess"] = _handle("sess")
    assert reg.kill("sess") is True
    assert stub_terminate == ["sess"]
    assert reg.get("sess") is None
    # Idempotent: killing again is a no-op (already popped).
    assert reg.kill("sess") is False


def test_kill_all_signals_every_executor(stub_terminate):
    reg = ExecutorRegistry()
    for sid in ("a", "b", "c"):
        reg._handles[sid] = _handle(sid)
    reg.kill_all()
    assert sorted(stub_terminate) == ["a", "b", "c"]
    assert reg.all_handles() == []


def test_kill_unlinks_discovery_file(monkeypatch):
    """Failure 3: endSession SIGTERMs the executor AND removes its discovery
    file/socket so the test (and the next daemon's discovery) doesn't latch onto
    a stale `bw-exec-*.json`. Assert `kill` invokes `_ipc.cleanup_executor`."""
    monkeypatch.setattr(er, "_terminate", lambda handle: handle.proc.terminate())
    cleaned: list[str] = []
    monkeypatch.setattr(_ipc, "cleanup_executor",
                        lambda sid: cleaned.append(sid))
    reg = ExecutorRegistry()
    reg._handles["sess"] = _handle("sess")
    assert reg.kill("sess") is True
    assert cleaned == ["sess"]


def test_kill_isolated_per_session(stub_terminate):
    reg = ExecutorRegistry()
    reg._handles["x"] = _handle("x")
    reg._handles["y"] = _handle("y")
    reg.kill("x")
    assert stub_terminate == ["x"]
    assert reg.get("x") is None
    assert reg.get("y") is not None  # no cross-talk


# ---- idle_seconds uses the discovery-file mtime ----------------------------


def test_idle_seconds_reads_discovery_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(_ipc, "_runtime_dir", lambda: tmp_path)
    sid = "sess-mtime"
    fp = _ipc.executor_file_path(sid)
    fp.write_text(json.dumps({"sock": "x", "pid": 1, "session": sid}))
    # Backdate the mtime by 120s.
    past = time.time() - 120.0
    os.utime(fp, (past, past))
    h = ExecutorHandle(session_id=sid, proc=_FakeProc(),
                       sock_path="x", spawned_wall=past)
    assert h.idle_seconds() >= 110.0


def test_idle_seconds_floors_on_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(_ipc, "_runtime_dir", lambda: tmp_path)
    # No discovery file → fall back to spawn time (just spawned → ~0 idle).
    h = ExecutorHandle(session_id="nope", proc=_FakeProc(), sock_path="x")
    assert h.idle_seconds() < 5.0


# ---- orphan sweep ----------------------------------------------------------


def test_cleanup_orphan_executors_unlinks_stale_files(tmp_path, monkeypatch):
    monkeypatch.setattr(_ipc, "_runtime_dir", lambda: tmp_path)
    # A stale discovery file + socket from a prior daemon crash, pid that no
    # longer exists.
    sock = tmp_path / "bw-exec-deadbeef.sock"
    sock.write_bytes(b"")
    disc = tmp_path / "bw-exec-deadbeef.json"
    # pid 1 is init — never ours; use a definitely-dead pid that getpgid raises
    # on. We just assert the files are removed regardless.
    disc.write_text(json.dumps({"sock": str(sock), "pid": 2 ** 30,
                                "session": "old"}))
    er.cleanup_orphan_executors()
    assert not disc.exists()
    assert not sock.exists()


def test_cleanup_orphan_executors_no_runtime_dir(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(_ipc, "_runtime_dir", lambda: missing)
    er.cleanup_orphan_executors()  # must not raise


# ---- endSession kills the executor (both backends) -------------------------


def _router_with_client(backend: str):
    """Minimal Router + one registered client through the real dispatch path."""
    from browserwright.daemon.server.proxy import Router
    from browserwright.daemon.server.state import DaemonState, UpstreamPhase

    captured: dict[int, list] = {}
    state = DaemonState(backend_name=backend)
    state.upstream_phase = UpstreamPhase.CONNECTED
    router = Router(state)

    async def _ensure():
        return None

    async def _disc(_reason):
        return None

    router.bind_lifecycle(_ensure, _disc)
    async def _upstream_send(_text):
        return None
    router.update_upstream_send(_upstream_send)
    client = state.allocate_client("c")
    client.session_id = "sess"
    captured[client.client_id] = []

    async def _send(text):
        captured[client.client_id].append(json.loads(text))

    router.register_client(client.client_id, _send)
    return router, client, captured


class _RecordingRegistry:
    def __init__(self):
        self.killed: list[str] = []

    def kill(self, session_id):
        self.killed.append(session_id)
        return True


@pytest.mark.asyncio
async def test_end_session_kills_executor_extension():
    router, client, captured = _router_with_client("extension")
    reg = _RecordingRegistry()

    class _Daemon:
        executors = reg
        contexts: dict = {}

    async def _end_session(session, *a):
        return {"ok": True, "closed": [], "kept": []}

    router.daemon = _Daemon()
    router.upstream.end_session = _end_session
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.endSession",
        "params": {"session": "sess"},
    }))
    assert reg.killed == ["sess"], "extension endSession did not kill executor"
    assert captured[client.client_id][-1]["result"]["ok"] is True


@pytest.mark.asyncio
async def test_end_session_kills_executor_rdp():
    router, client, captured = _router_with_client("rdp")
    reg = _RecordingRegistry()

    class _Daemon:
        executors = reg
        contexts = {"sess": object()}

        async def teardown_rdp_context(self, session):
            return True

    router.daemon = _Daemon()
    async def _end_session(session, *args):
        await router.daemon.teardown_rdp_context(session)
        return {"ok": True, "closed": [], "kept": [], "backend": "rdp"}
    router.upstream.end_session = _end_session
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.endSession",
        "params": {"session": "sess"},
    }))
    assert reg.killed == ["sess"], "rdp endSession did not kill executor"
    assert captured[client.client_id][-1]["result"]["backend"] == "rdp"


# ---- idle-watchdog drives executor supervision -----------------------------


@pytest.mark.asyncio
async def test_idle_watchdog_crash_reaps_even_when_idle_off(monkeypatch):
    """With idle-close OFF (idle_after=None) the watchdog still crash-reaps dead
    executors — corpses must never accumulate."""
    import asyncio

    from browserwright.daemon.server import listener

    calls = {"reap_dead": 0, "reap_idle": 0}

    class _Reg:
        def reap_dead(self):
            calls["reap_dead"] += 1
            return []

        def reap_idle(self, idle_after):
            calls["reap_idle"] += 1
            return []

    class _Daemon:
        executors = _Reg()

        def all_contexts(self):
            return []

    # Speed up the poll so the first tick lands fast (keep the real sleep).
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(listener.asyncio, "sleep",
                        lambda _s: _real_sleep(0.001))
    task = asyncio.create_task(listener._idle_watchdog(_Daemon(), None))
    await _real_sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert calls["reap_dead"] >= 1
    assert calls["reap_idle"] == 0  # idle-reap gated off


@pytest.mark.asyncio
async def test_idle_watchdog_idle_reaps_when_configured(monkeypatch):
    import asyncio

    from browserwright.daemon.server import listener

    calls = {"reap_dead": 0, "reap_idle": 0}

    class _Reg:
        def reap_dead(self):
            calls["reap_dead"] += 1
            return []

        def reap_idle(self, idle_after):
            calls["reap_idle"] += 1
            assert idle_after == 30.0
            return []

    class _Daemon:
        executors = _Reg()

        def all_contexts(self):
            return []

    _real_sleep = asyncio.sleep
    monkeypatch.setattr(listener.asyncio, "sleep",
                        lambda _s: _real_sleep(0.001))
    task = asyncio.create_task(listener._idle_watchdog(_Daemon(), 30.0))
    await _real_sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert calls["reap_dead"] >= 1
    assert calls["reap_idle"] >= 1


@pytest.mark.asyncio
async def test_auto_prune_sessions_uses_configured_threshold(tmp_path, monkeypatch):
    from browserwright import session_registry as reg
    from browserwright.daemon.config import Config
    from browserwright.daemon.server import listener

    monkeypatch.setenv("BS_HOME", str(tmp_path))
    sid = reg.allocate(backend="rdp", owner="create", name="old")
    reg._with_entry(sid, lambda e: e.update(last_seen=0.0))

    class _Executors:
        def __init__(self):
            self.killed: list[str] = []

        def kill(self, session_id):
            self.killed.append(session_id)

    class _Daemon:
        cfg = Config(session_idle_prune=12.5)
        executors = _Executors()
        shared_context = None

        def __init__(self):
            self.torn_down: list[str] = []

        async def teardown_rdp_context(self, session_id):
            assert reg.get(session_id) is not None
            self.torn_down.append(session_id)
            return True

    daemon = _Daemon()

    pruned = await listener._auto_prune_sessions(daemon, reason="test")
    assert len(pruned) == 1
    assert pruned[0]["id"] == sid
    assert pruned[0]["backend"] == "rdp"
    assert pruned[0]["owner"] == "create"
    assert daemon.executors.killed == [sid]
    assert daemon.torn_down == [sid]
    assert reg.get(sid) is None

    daemon.cfg = Config(session_idle_prune=None)
    assert await listener._auto_prune_sessions(daemon, reason="off") == []


@pytest.mark.asyncio
async def test_auto_prune_sessions_closes_open_extension_workspace(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from browserwright import session_registry as reg
    from browserwright.daemon.config import Config
    from browserwright.daemon.server import listener

    monkeypatch.setenv("BS_HOME", str(tmp_path))
    sid = reg.allocate(backend="extension", owner="attach", name="old-ext")
    reg._with_entry(
        sid,
        lambda e: e.update(
            last_seen=0.0,
            runtime={"group_id": 17},
        ),
    )

    class _Executors:
        def __init__(self):
            self.killed: list[str] = []

        def kill(self, session_id):
            self.killed.append(session_id)

    class _Upstream:
        def __init__(self):
            self.ended: list[tuple[str, int | None]] = []

        async def end_session(self, session_id, group_id=None):
            assert reg.get(session_id) is not None
            self.ended.append((session_id, group_id))
            return {"closed": [], "kept": []}

    upstream = _Upstream()
    daemon = SimpleNamespace(
        cfg=Config(session_idle_prune=1.0),
        executors=_Executors(),
        shared_context=SimpleNamespace(
            holder=SimpleNamespace(upstream=upstream),
        ),
    )

    pruned = await listener._auto_prune_sessions(daemon, reason="test")

    assert [rec["id"] for rec in pruned] == [sid]
    assert daemon.executors.killed == [sid]
    assert upstream.ended == [(sid, 17)]
    assert reg.get(sid) is None


@pytest.mark.asyncio
async def test_idle_watchdog_periodically_prunes_sessions(monkeypatch):
    import asyncio

    from browserwright.daemon.config import Config
    from browserwright.daemon.server import listener

    calls = {"reap_dead": 0, "session_prune": 0}

    class _Reg:
        def reap_dead(self):
            calls["reap_dead"] += 1
            return []

        def reap_idle(self, idle_after):
            return []

    class _Daemon:
        cfg = Config(session_idle_prune=24.0)
        executors = _Reg()

        def all_contexts(self):
            return []

    times = iter([100.0, 3701.0])
    monkeypatch.setattr(listener.time, "time", lambda: next(times, 3701.0))
    async def fake_prune(daemon, *, reason):
        calls["session_prune"] += 1
        return []

    monkeypatch.setattr(listener, "_auto_prune_sessions", fake_prune)

    sleeps = 0

    async def fake_sleep(delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(listener.asyncio, "sleep", fake_sleep)

    await listener._idle_watchdog(
        _Daemon(), None, session_idle_prune=24.0)

    assert calls["reap_dead"] == 1
    assert calls["session_prune"] == 1


# ---- real signal discipline (one child, no stub) ---------------------------


def test_terminate_kills_real_child():
    """Exercise the real `_terminate` against a short-lived detached child to
    prove SIGTERM (escalating to SIGKILL) actually reaps it."""
    if sys.platform == "win32":
        pytest.skip("POSIX process-group signalling")
    # A child that ignores SIGTERM forces the SIGKILL escalation path.
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
         "time.sleep(60)"],
        start_new_session=True,
    )
    handle = ExecutorHandle(session_id="real", proc=proc, sock_path="x")
    # Shrink the grace window so the test is fast.
    import browserwright.daemon.server.executor_registry as mod
    orig = mod._KILL_GRACE_S
    mod._KILL_GRACE_S = 0.3
    try:
        er._terminate(handle)
    finally:
        mod._KILL_GRACE_S = orig
    # Within a moment the child is gone (SIGKILL won after the ignored SIGTERM).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.05)
    assert proc.poll() is not None, "child survived _terminate"
