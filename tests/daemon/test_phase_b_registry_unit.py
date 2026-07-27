"""Unit coverage for the Phase B PR1 daemon control plane (no real subprocess):

  - ExecutorRegistry single-flight: a live handle short-circuits; concurrent
    `ensure()` calls for the same session spawn EXACTLY once (the double-spawn
    race guard, Fork 1); a dead handle cold-respawns.
  - BrowserwrightDaemon.ensureExecutor verb dispatch: returns the registry's
    socket path, requires a ws session, and surfaces registry failure as -32603
    (never -32601 — it's a known verb).
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from browserwright.daemon.server.executor_registry import (
    ExecutorHandle,
    ExecutorRegistry,
    _discovery_alive,
)


class _FakeProc:
    def __init__(self, alive: bool = True):
        self._alive = alive
        self.pid = 4242

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False


def _handle(
    session_id: str,
    sock: str,
    alive: bool = True,
    executor_id: str | None = None,
) -> ExecutorHandle:
    kwargs = {}
    if executor_id is not None:
        kwargs["executor_id"] = executor_id
    return ExecutorHandle(
        session_id=session_id, proc=_FakeProc(alive), sock_path=sock, **kwargs
    )


# ---- registry single-flight ------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_short_circuits_live_handle(monkeypatch):
    reg = ExecutorRegistry()
    spawned = {"n": 0}

    async def fake_spawn(session_id):
        spawned["n"] += 1
        return _handle(session_id, f"/tmp/bw-exec-{session_id}.sock")

    monkeypatch.setattr(reg, "_spawn", fake_spawn)

    p1 = await reg.ensure("sess-a")
    p2 = await reg.ensure("sess-a")
    assert p1 == p2
    assert spawned["n"] == 1  # second call reused the live handle


@pytest.mark.asyncio
async def test_ensure_single_flight_under_concurrency(monkeypatch):
    reg = ExecutorRegistry()
    spawned = {"n": 0}

    async def fake_spawn(session_id):
        spawned["n"] += 1
        await asyncio.sleep(0.01)  # widen the race window
        return _handle(session_id, f"/tmp/bw-exec-{session_id}.sock")

    monkeypatch.setattr(reg, "_spawn", fake_spawn)

    # Two concurrent first-heredocs for the SAME session must spawn once.
    results = await asyncio.gather(
        reg.ensure("sess-b"), reg.ensure("sess-b"), reg.ensure("sess-b"))
    assert len(set(results)) == 1
    assert spawned["n"] == 1


@pytest.mark.asyncio
async def test_ensure_respawns_dead_handle(monkeypatch):
    reg = ExecutorRegistry()
    spawned = {"n": 0}

    async def fake_spawn(session_id):
        spawned["n"] += 1
        return _handle(session_id, f"/tmp/bw-exec-{session_id}-{spawned['n']}.sock")

    monkeypatch.setattr(reg, "_spawn", fake_spawn)

    p1 = await reg.ensure("sess-c")
    # Mark the handle dead (crashed); next ensure must cold-respawn.
    reg.get("sess-c").proc._alive = False
    p2 = await reg.ensure("sess-c")
    assert spawned["n"] == 2
    assert p1 != p2


@pytest.mark.asyncio
async def test_ensure_isolates_per_session(monkeypatch):
    reg = ExecutorRegistry()

    async def fake_spawn(session_id):
        return _handle(session_id, f"/tmp/bw-exec-{session_id}.sock")

    monkeypatch.setattr(reg, "_spawn", fake_spawn)
    pa = await reg.ensure("sess-x")
    pb = await reg.ensure("sess-y")
    assert pa != pb
    assert {h.session_id for h in reg.all_handles()} == {"sess-x", "sess-y"}


@pytest.mark.asyncio
async def test_kill_and_wait_does_not_kill_newer_executor_instance(monkeypatch):
    reg = ExecutorRegistry()
    current = _handle("sess-race", "/tmp/current.sock", executor_id="executor-new")
    reg._handles["sess-race"] = current

    result = await reg.kill_and_wait("sess-race", executor_id="executor-old")

    assert result["reaped"] is True
    assert result["matched"] is False
    assert current.is_alive()
    assert reg.get("sess-race") is current


@pytest.mark.asyncio
async def test_kill_and_wait_confirms_matching_process_death(monkeypatch):
    reg = ExecutorRegistry()
    current = _handle("sess-reap", "/tmp/current.sock", executor_id="executor-current")
    reg._handles["sess-reap"] = current

    result = await reg.kill_and_wait("sess-reap", executor_id="executor-current")

    assert result == {
        "killed": True,
        "reaped": True,
        "matched": True,
        "executor_id": "executor-current",
    }
    assert current.is_alive() is False
    assert reg.get("sess-reap") is None


# ---- stale discovery-file robustness (Fork 4 / daemon restart) -------------


@pytest.mark.asyncio
async def test_ensure_purges_stale_dead_discovery_file_before_spawn(monkeypatch, tmp_path):
    """A just-restarted daemon (empty in-memory registry) facing a leftover
    `bw-exec-*.json` from a DEAD pre-restart executor must treat it as absent:
    purge it, then cold-spawn fresh — never hand the thin client the stale
    socket. (Failure 4 robustness.)"""
    from browserwright.daemon import _ipc

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    # Seed a stale discovery file naming a DEAD pid.
    dead_pid = 2 ** 30  # almost certainly not a live process
    _ipc.write_executor_file("sess-stale", "/tmp/old-dead.sock", dead_pid)
    assert _ipc.executor_file_path("sess-stale").exists()

    reg = ExecutorRegistry()
    cleaned = {"called": False}
    real_cleanup = _ipc.cleanup_executor

    def spy_cleanup(session_id):
        if session_id == "sess-stale":
            cleaned["called"] = True
        real_cleanup(session_id)

    monkeypatch.setattr(
        "browserwright.daemon.server.executor_registry._ipc.cleanup_executor",
        spy_cleanup)

    async def fake_spawn(session_id):
        return _handle(session_id, "/tmp/fresh.sock")

    monkeypatch.setattr(reg, "_spawn", fake_spawn)

    sock = await reg.ensure("sess-stale")
    assert sock == "/tmp/fresh.sock"  # the FRESH socket, not the stale one
    assert cleaned["called"] is True  # the dead file was purged


def test_discovery_alive_false_for_missing_and_dead(monkeypatch, tmp_path):
    from browserwright.daemon import _ipc

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    # Absent file → not alive.
    assert _discovery_alive("nope") is False
    # File naming a dead pid → not alive.
    _ipc.write_executor_file("dead", "/tmp/x.sock", 2 ** 30)
    assert _discovery_alive("dead") is False
    # File naming OUR (live) pid → alive.
    import os
    _ipc.write_executor_file("live", "/tmp/y.sock", os.getpid())
    assert _discovery_alive("live") is True


# ---- readiness decoupling: ready == socket listening, NOT cold-started ------


@pytest.mark.asyncio
async def test_await_ready_returns_once_discovery_file_exists(monkeypatch, tmp_path):
    """The control plane is decoupled from cold-start: `_await_ready` returns as
    soon as the executor publishes its discovery file (socket LISTENING) — it
    does NOT wait for the slow facade connect+bind. Here the discovery file
    appears immediately (the child is alive but cold-start is still pending);
    `_await_ready` must return the socket path fast, without any cold-start
    signal."""
    from browserwright.daemon import _ipc

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    reg = ExecutorRegistry()
    proc = _FakeProc(alive=True)

    # Simulate the executor binding its socket + writing discovery BEFORE any
    # cold-start (the new startup ordering): the file is present immediately.
    _ipc.write_executor_file("sess-ready", "/tmp/bw-exec-sess-ready.sock", proc.pid)

    t0 = time.monotonic()
    sock = await reg._await_ready("sess-ready", proc)
    elapsed = time.monotonic() - t0

    assert sock == "/tmp/bw-exec-sess-ready.sock"
    # Returned promptly — NOT after a multi-second cold-start window.
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_await_ready_does_not_wait_for_cold_start(monkeypatch, tmp_path):
    """Even if cold-start would take a long time, `_await_ready` returns the
    moment the discovery file appears. We model a slow-cold-start executor by
    having the discovery file appear after a brief socket-bind delay, while the
    child stays alive — `_await_ready` returns on the file, not on connectedness
    (there is no connectedness signal in the file at all)."""
    from browserwright.daemon import _ipc

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    reg = ExecutorRegistry()
    proc = _FakeProc(alive=True)

    async def _publish_after_bind():
        await asyncio.sleep(0.1)  # process start + socket bind
        _ipc.write_executor_file(
            "sess-slowcold", "/tmp/bw-exec-sess-slowcold.sock", proc.pid)

    publisher = asyncio.create_task(_publish_after_bind())
    sock = await reg._await_ready("sess-slowcold", proc)
    await publisher
    assert sock == "/tmp/bw-exec-sess-slowcold.sock"


@pytest.mark.asyncio
async def test_await_ready_requires_matching_executor_instance(
    monkeypatch,
    tmp_path,
):
    from browserwright.daemon import _ipc

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    reg = ExecutorRegistry()
    proc = _FakeProc(alive=True)
    _ipc.write_executor_file(
        "sess-instance",
        "/tmp/stale.sock",
        proc.pid,
        executor_id="executor-old",
    )

    async def _replace_stale_record():
        await asyncio.sleep(0.1)
        _ipc.write_executor_file(
            "sess-instance",
            "/tmp/current.sock",
            proc.pid,
            executor_id="executor-current",
        )

    publisher = asyncio.create_task(_replace_stale_record())
    sock = await reg._await_ready("sess-instance", proc, executor_id="executor-current")
    await publisher

    assert sock == "/tmp/current.sock"


# ---- ensureExecutor verb dispatch ------------------------------------------


def _router_with_client():
    """Minimal Router + one registered client, exercised through the real
    `route_from_client` dispatch path (no daemon process / upstream)."""
    from browserwright.daemon.server.proxy import Router
    from browserwright.daemon.server.state import DaemonState, UpstreamPhase

    captured: dict[int, list] = {}
    state = DaemonState(backend_name="rdp")
    state.upstream_phase = UpstreamPhase.CONNECTED
    router = Router(state)

    async def _ensure():
        return None

    async def _disc(_reason):
        return None

    router.bind_lifecycle(_ensure, _disc)
    client = state.allocate_client("c")
    client.session_id = "sess"
    captured[client.client_id] = []

    async def _send(text: str) -> None:
        captured[client.client_id].append(json.loads(text))

    router.register_client(client.client_id, _send)
    return router, client, captured


@pytest.mark.asyncio
async def test_ensure_executor_verb_returns_socket():
    router, client, captured = _router_with_client()

    class _Daemon:
        class _Reg:
            async def ensure(self, session_id):
                assert session_id == "sess"
                return "/tmp/bw-exec-sess.sock"

            def get(self, session_id):
                assert session_id == "sess"
                return type("Handle", (), {"executor_id": "executor-sess"})()

        executors = _Reg()

    router.daemon = _Daemon()
    await router.route_from_client(
        client,
        json.dumps(
            {
                "id": 1,
                "method": "BrowserwrightDaemon.ensureExecutor",
                "params": {"session": "sess"},
            }
        ),
    )
    assert captured[client.client_id][-1]["result"] == {
        "exec_sock": "/tmp/bw-exec-sess.sock",
        "executor_id": "executor-sess",
    }


@pytest.mark.asyncio
async def test_ensure_executor_rejects_mismatched_browserwright_session_param():
    router, client, captured = _router_with_client()

    class _Daemon:
        class _Reg:
            async def ensure(self, session_id):
                raise AssertionError("registry must not be reached")

        executors = _Reg()

    router.daemon = _Daemon()
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.ensureExecutor",
        "params": {"bsSession": "other"},
    }))
    err = captured[client.client_id][-1]["error"]
    assert err["code"] == -32602
    assert "session mismatch" in err["message"]


@pytest.mark.asyncio
async def test_ensure_executor_verb_requires_ws_session():
    router, client, captured = _router_with_client()
    client.session_id = None  # no ws ?session=

    router.daemon = object()
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.ensureExecutor",
        "params": {},
    }))
    # Missing session → -32602 (NOT -32601: the verb is known/registered).
    assert captured[client.client_id][-1]["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_ensure_executor_verb_surfaces_registry_failure():
    router, client, captured = _router_with_client()

    class _Daemon:
        class _Reg:
            async def ensure(self, session_id):
                raise RuntimeError("spawn boom")

        executors = _Reg()

    router.daemon = _Daemon()
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.ensureExecutor",
        "params": {"session": "sess"},
    }))
    assert captured[client.client_id][-1]["error"]["code"] == -32603


# ---- Failure #4: ensureExecutor launches the upstream BEFORE spawning -------


def _router_disconnected():
    """Like `_router_with_client` but upstream is DISCONNECTED, with an
    `_ensure_upstream` callback that records its call ordering vs the registry
    and (mimicking the rdp holder) flips the phase to CONNECTED."""
    from browserwright.daemon.server.proxy import Router
    from browserwright.daemon.server.state import DaemonState, UpstreamPhase

    captured: dict[int, list] = {}
    order: list[str] = []
    state = DaemonState(backend_name="rdp")
    state.upstream_phase = UpstreamPhase.DISCONNECTED
    router = Router(state)

    async def _ensure():
        order.append("ensure_upstream")
        # The rdp holder's ensure_open launches Chrome + marks connected.
        state.upstream_phase = UpstreamPhase.CONNECTED

    async def _disc(_reason):
        return None

    router.bind_lifecycle(_ensure, _disc)
    client = state.allocate_client("c")
    client.session_id = "sess"
    captured[client.client_id] = []

    async def _send(text: str) -> None:
        captured[client.client_id].append(json.loads(text))

    router.register_client(client.client_id, _send)
    return router, client, captured, order


@pytest.mark.asyncio
async def test_ensure_executor_launches_upstream_before_registry():
    """Failure #4: the executor's cold-start `connect_over_cdp(facade)` needs a
    LIVE rdp Chrome (its dynamic port pinned). So `ensureExecutor` must call
    `_ensure_upstream` (→ `_launch_rdp_chrome`) BEFORE `registry.ensure`, or the
    facade resolves the stale default port and the executor exits during
    cold-start. Assert the ordering."""
    router, client, captured, order = _router_disconnected()

    class _Daemon:
        class _Reg:
            async def ensure(self, session_id):
                order.append("registry.ensure")
                return "/tmp/bw-exec-sess.sock"

        executors = _Reg()

    router.daemon = _Daemon()
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.ensureExecutor",
        "params": {"session": "sess"},
    }))
    assert order == ["ensure_upstream", "registry.ensure"], (
        "ensureExecutor must launch the upstream (rdp Chrome) BEFORE spawning "
        "the executor")
    assert captured[client.client_id][-1]["result"] == {
        "exec_sock": "/tmp/bw-exec-sess.sock"}


@pytest.mark.asyncio
async def test_ensure_executor_skips_upstream_when_already_connected():
    """When the upstream is already CONNECTED (warm), `ensureExecutor` does NOT
    re-trigger `_ensure_upstream` — it goes straight to the registry."""
    from browserwright.daemon.server.proxy import Router
    from browserwright.daemon.server.state import DaemonState, UpstreamPhase

    captured: dict[int, list] = {}
    order: list[str] = []
    state = DaemonState(backend_name="rdp")
    state.upstream_phase = UpstreamPhase.CONNECTED
    router = Router(state)

    async def _ensure():
        order.append("ensure_upstream")  # must NOT be called

    router.bind_lifecycle(_ensure, lambda _r: None)
    client = state.allocate_client("c")
    client.session_id = "sess"
    captured[client.client_id] = []
    router.register_client(
        client.client_id,
        lambda text: captured[client.client_id].append(json.loads(text)))

    class _Daemon:
        class _Reg:
            async def ensure(self, session_id):
                order.append("registry.ensure")
                return "/tmp/bw-exec-sess.sock"

        executors = _Reg()

    router.daemon = _Daemon()
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.ensureExecutor",
        "params": {"session": "sess"},
    }))
    assert order == ["registry.ensure"]  # ensure_upstream skipped


@pytest.mark.asyncio
async def test_ensure_executor_upstream_failure_returns_error_envelope():
    """A Chrome-launch failure during the pre-spawn upstream open surfaces as a
    proper -32603 error envelope (never crashes the client ws), and the
    registry is NOT consulted."""
    router, client, captured, order = _router_disconnected()

    async def _boom():
        raise RuntimeError("chrome launch boom")

    router._ensure_upstream = _boom  # type: ignore[attr-defined]

    class _Daemon:
        class _Reg:
            async def ensure(self, session_id):
                order.append("registry.ensure")  # must NOT be reached
                return "/tmp/x.sock"

        executors = _Reg()

    router.daemon = _Daemon()
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.ensureExecutor",
        "params": {"session": "sess"},
    }))
    assert "registry.ensure" not in order
    err = captured[client.client_id][-1]["error"]
    assert err["code"] == -32603
    assert "upstream open" in err["message"]


# ---- killExecutor verb (Phase B / Failure #3 hardening) --------------------


@pytest.mark.asyncio
async def test_kill_executor_verb_reaps_and_acks():
    """`killExecutor` reaps ONLY the executor (no browser teardown) and acks
    `{ok, killed}` — used by `session_create.end()` for attach sessions."""
    router, client, captured = _router_with_client()
    killed: list[str] = []

    class _Daemon:
        class _Reg:
            def kill(self, session_id):
                killed.append(session_id)
                return True

        executors = _Reg()

    router.daemon = _Daemon()
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.killExecutor",
        "params": {"session": "sess"},
    }))
    assert killed == ["sess"]
    assert captured[client.client_id][-1]["result"] == {
        "ok": True, "killed": True}


@pytest.mark.asyncio
async def test_kill_executor_waits_for_exact_instance_reap():
    router, client, captured = _router_with_client()
    calls = []

    class _Daemon:
        class _Reg:
            async def kill_and_wait(self, session_id, *, executor_id=None):
                calls.append((session_id, executor_id))
                return {
                    "killed": True,
                    "reaped": True,
                    "matched": True,
                }

        executors = _Reg()

    router.daemon = _Daemon()
    await router.route_from_client(
        client,
        json.dumps(
            {
                "id": 1,
                "method": "BrowserwrightDaemon.killExecutor",
                "params": {
                    "session": "sess",
                    "executorId": "executor-current",
                    "wait": True,
                },
            }
        ),
    )

    assert calls == [("sess", "executor-current")]
    assert captured[client.client_id][-1]["result"] == {
        "ok": True,
        "killed": True,
        "reaped": True,
        "matched": True,
    }


@pytest.mark.asyncio
async def test_kill_executor_verb_idempotent_without_registry():
    """A daemon with no executor registry still answers a clean (non-`-32601`)
    `{ok, killed: False}` so a stale-daemon caller never errors on `session
    end`."""
    router, client, captured = _router_with_client()
    router.daemon = object()  # no `executors` attr
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.killExecutor",
        "params": {"session": "sess"},
    }))
    assert captured[client.client_id][-1]["result"] == {
        "ok": True, "killed": False}


@pytest.mark.asyncio
async def test_kill_executor_verb_requires_session():
    router, client, captured = _router_with_client()
    client.session_id = None
    router.daemon = object()
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.killExecutor",
        "params": {},
    }))
    assert captured[client.client_id][-1]["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_kill_executor_rejects_mismatched_browserwright_session_param():
    router, client, captured = _router_with_client()
    killed: list[str] = []

    class _Daemon:
        class _Reg:
            def kill(self, session_id):
                killed.append(session_id)
                return True

        executors = _Reg()

    router.daemon = _Daemon()
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.killExecutor",
        "params": {"session": "other"},
    }))
    assert killed == []
    err = captured[client.client_id][-1]["error"]
    assert err["code"] == -32602
    assert "session mismatch" in err["message"]
