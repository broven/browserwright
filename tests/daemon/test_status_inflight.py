"""C1: the daemon's pulse — `BrowserwrightDaemon.status` + `ps` + SIGUSR1.

What these lock down is one property, from four angles: **a request that is
waiting must be able to say so, and say for how long.** The bug that motivated
the work was not a crash — it was a daemon that answered every health question
correctly while one future sat unresolved forever.

So the interesting assertions here are the *elapsed* ones. "The verb returns a
dict" is table stakes; "the row reports an age that grows" is the thing that
would have turned a silent 10-second hang into a one-line diagnosis.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from browserwright.daemon import _ipc
from browserwright.daemon.server import status as status_mod
from browserwright.daemon.server.proxy import Router
from browserwright.daemon.server.state import DaemonState, UpstreamPhase


STATUS = "BrowserwrightDaemon.status"


# ---- harness ---------------------------------------------------------------


async def ask_status(state: DaemonState, *, daemon: object | None = None) -> dict:
    """Drive the real verb through the real router with one JSON-RPC frame."""
    router = Router(state)
    if daemon is not None:
        router.daemon = daemon
    replies: list[dict] = []

    async def send_to_client(text: str) -> None:
        replies.append(json.loads(text))

    client = state.allocate_client("agent", session_id="bs-1", session_name="agent")
    router.register_client(client.client_id, send_to_client)
    await asyncio.wait_for(
        router.route_from_client(client, json.dumps({"id": 1, "method": STATUS})),
        timeout=5.0)
    assert replies, "status produced no reply"
    return replies[-1]


def fresh_state(backend: str = "extension") -> DaemonState:
    state = DaemonState(backend_name=backend)
    state.upstream_phase = UpstreamPhase.CONNECTED
    return state


# ---- the verb --------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_answers_on_a_bare_router_with_no_daemon():
    """The verb must work on the sickest possible daemon.

    A `Router` with no `Daemon` back-reference is the shape a unit test builds —
    and also the shape a half-initialised daemon has. Status is the one verb
    that has to answer there, because it is what you reach for when nothing else
    is answering.
    """
    reply = await ask_status(fresh_state())
    result = reply["result"]
    assert "error" not in reply
    assert result["schema_version"] == status_mod.SCHEMA_VERSION
    assert result["daemon"]["wired"] is False
    # Even with no Daemon, the caller's own context is reported.
    assert [c["backend"] for c in result["contexts"]] == ["extension"]


@pytest.mark.asyncio
async def test_status_reports_the_connected_client_and_its_command_clock():
    """`ClientState.last_command_at` was written on every frame and read by
    nothing. This is the reader — and it must show the *status call itself* as
    recent activity, which is what proves the field is live rather than frozen
    at connect time."""
    state = fresh_state()
    reply = await ask_status(state)
    clients = reply["result"]["contexts"][0]["clients"]
    assert len(clients) == 1
    c = clients[0]
    assert c["label"] == "agent"
    assert c["session_id"] == "bs-1"
    assert c["last_command_age_s"] is not None
    assert c["last_command_age_s"] < 5.0


@pytest.mark.asyncio
async def test_a_stuck_pending_request_is_visible_with_its_age():
    """The exact shape of the original hang, reconstructed.

    One entry in `pending_requests` that nobody will ever resolve. Before C1
    this was invisible: no log line, no `/__status__` delta. Now it is a row
    that names the method and how long it has been waiting.
    """
    state = fresh_state()
    state.remember_request(
        upstream_id=7, client_id=1, client_request_id=3, method="Page.navigate")
    # Backdate it: the point of the row is that a 5-minute wait does not look
    # like a 50ms one.
    state.pending_requests[7].started_at -= 300.0

    reply = await ask_status(state)
    rows = reply["result"]["contexts"][0]["pending_requests"]
    assert len(rows) == 1
    assert rows[0]["method"] == "Page.navigate"
    assert rows[0]["hop"] == "router"
    assert rows[0]["elapsed_s"] > 299.0


@pytest.mark.asyncio
async def test_pending_requests_are_reported_oldest_first():
    """In a hang the operator wants one row: the oldest. Put it on top."""
    state = fresh_state()
    for upstream_id, method, age in (
        (1, "Runtime.evaluate", 1.0),
        (2, "Page.navigate", 120.0),
        (3, "DOM.getDocument", 30.0),
    ):
        state.remember_request(
            upstream_id=upstream_id, client_id=1,
            client_request_id=upstream_id, method=method)
        state.pending_requests[upstream_id].started_at -= age

    reply = await ask_status(state)
    rows = reply["result"]["contexts"][0]["pending_requests"]
    assert [r["method"] for r in rows] == [
        "Page.navigate", "DOM.getDocument", "Runtime.evaluate"]


@pytest.mark.asyncio
async def test_status_needs_no_upstream_and_no_websocket_session():
    """A wedged daemon has a wedged upstream by definition, so status must not
    consult one. Disconnected phase, no session binding — still an answer."""
    state = DaemonState(backend_name="extension")  # phase stays DISCONNECTED
    router = Router(state)
    replies: list[dict] = []

    async def send_to_client(text: str) -> None:
        replies.append(json.loads(text))

    client = state.allocate_client("anonymous")  # no ?session=
    router.register_client(client.client_id, send_to_client)
    await asyncio.wait_for(
        router.route_from_client(client, json.dumps({"id": 4, "method": STATUS})),
        timeout=5.0)
    assert replies[-1]["id"] == 4
    assert "result" in replies[-1]
    assert replies[-1]["result"]["contexts"][0]["upstream_phase"] == "DISCONNECTED"


# ---- the executor hop ------------------------------------------------------


class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class _FakeHandle:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.proc = _FakeProc(4242)
        self.sock_path = f"/tmp/{session_id}.sock"
        self.executor_id = "deadbeef"
        self.spawned_at = time.monotonic() - 12.0

    def is_alive(self) -> bool:
        return True

    def idle_seconds(self, *, now: float | None = None) -> float:
        return 3.5


class _FakeRegistry:
    def __init__(self, handles) -> None:
        self._handles = handles

    def all_handles(self):
        return list(self._handles)


class _FakeDaemon:
    def __init__(self, contexts, executors) -> None:
        self._contexts = contexts
        self.executors = executors

    def all_contexts(self):
        return list(self._contexts)


@pytest.mark.asyncio
async def test_executor_handles_reach_the_wire(tmp_path, monkeypatch):
    """`all_handles()` already knew pid / socket / idle. Its only callers were
    two test files. This asserts the out-line exists."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    state = fresh_state()
    ctx = status_mod._BareContext(state)
    daemon = _FakeDaemon([ctx], _FakeRegistry([_FakeHandle("bs-abc")]))

    reply = await ask_status(state, daemon=daemon)
    rows = reply["result"]["executors"]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "bs-abc"
    assert rows[0]["pid"] == 4242
    assert rows[0]["alive"] is True
    assert rows[0]["idle_s"] == 3.5
    assert rows[0]["age_s"] >= 12.0
    assert rows[0]["inflight"] is None  # nothing published → honestly idle
    assert reply["result"]["daemon"]["wired"] is True


@pytest.mark.asyncio
async def test_executor_inflight_sidecar_is_surfaced_with_its_age(
        tmp_path, monkeypatch):
    """The worker publishes what it is running to a file precisely because a
    wedged worker cannot answer an RPC. `status` reads it back."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _ipc.write_executor_inflight("bs-abc", {
        "session": "bs-abc", "what": "code", "code_sha": "abc123def456",
        "code_chars": 91, "timeout_ms": 30000,
        "started_wall": time.time() - 47.0, "connected": True,
    })
    state = fresh_state()
    daemon = _FakeDaemon([status_mod._BareContext(state)],
                         _FakeRegistry([_FakeHandle("bs-abc")]))

    reply = await ask_status(state, daemon=daemon)
    fl = reply["result"]["executors"][0]["inflight"]
    assert fl["what"] == "code"
    assert fl["code_sha"] == "abc123def456"
    assert fl["elapsed_s"] >= 46.0

    _ipc.write_executor_inflight("bs-abc", None)
    reply = await ask_status(state, daemon=daemon)
    assert reply["result"]["executors"][0]["inflight"] is None


def test_the_inflight_sidecar_never_carries_code(tmp_path, monkeypatch):
    """Heredocs routinely carry credentials and the runtime dir is shared. The
    sidecar records a digest of the code, never the code."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    from browserwright._executor.process import _Worker
    from browserwright._executor.protocol import ExecuteRequest

    secret = "page.fill('#pw', 'hunter2-super-secret')"
    worker = _Worker("bs-secret", executor_id="e1")
    worker._begin_inflight(ExecuteRequest(code=secret))
    try:
        raw = _ipc.executor_inflight_path("bs-secret").read_text()
        assert "hunter2" not in raw
        assert "super-secret" not in raw
        published = json.loads(raw)
        assert published["code_chars"] == len(secret)
        assert len(published["code_sha"]) == 12
        # The monotonic clock is meaningless across processes; only its
        # wall-clock twin is published.
        assert "started_at" not in published
        assert "started_wall" in published

        snap = worker.inflight_snapshot()
        assert snap["what"] == "code"
        assert snap["elapsed_s"] >= 0.0
    finally:
        worker._end_inflight()
    assert not _ipc.executor_inflight_path("bs-secret").exists()
    assert worker.inflight_snapshot() is None


def test_cleanup_executor_removes_the_inflight_sidecar(tmp_path, monkeypatch):
    """A sidecar outliving its executor would make `ps` report a call that ended
    when the process died."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _ipc.write_executor_inflight("bs-gone", {"what": "code"})
    assert _ipc.executor_inflight_path("bs-gone").exists()
    _ipc.cleanup_executor("bs-gone")
    assert not _ipc.executor_inflight_path("bs-gone").exists()


def test_the_sidecar_is_not_mistaken_for_a_discovery_file(tmp_path, monkeypatch):
    """`cleanup_orphan_executors` globs `bw-exec-*.json` and SIGTERMs whatever
    `pid` it finds inside. The sidecar must not match that glob."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _ipc.write_executor_inflight("bs-x", {"what": "code", "pid": 999999})
    path = _ipc.executor_inflight_path("bs-x")
    assert path.exists()
    assert path not in list(path.parent.glob("bw-exec-*.json"))


# ---- the relay hop ---------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_inflight_names_the_stuck_call():
    """`_ExtensionConn.pending` was a bare `dict[int, Future]`. The C1 question
    — which call, how long — is now answerable per entry."""
    from browserwright.daemon.server.relay import RelayServer, _ExtensionConn

    relay = RelayServer(port=0)
    ext = _ExtensionConn(conn=None, install_id="ext-1")
    relay._extensions["ext-1"] = ext

    loop = asyncio.get_running_loop()
    ext.pending[11] = loop.create_future()
    from browserwright.daemon.server.relay import _inflight_from_body
    ext.pending_meta[11] = _inflight_from_body(
        {"type": "command", "method": "Page.navigate", "tabId": 7}, 11)
    ext.pending_meta[11].started_at -= 47.2

    rows = relay.inflight_snapshot()
    assert len(rows) == 1
    assert rows[0]["method"] == "Page.navigate"
    assert rows[0]["kind"] == "command"
    assert rows[0]["tab_id"] == 7
    assert rows[0]["install_id"] == "ext-1"
    assert rows[0]["elapsed_s"] >= 47.0
    assert rows[0]["done"] is False

    ext.pending[11].cancel()


@pytest.mark.asyncio
async def test_relay_inflight_tolerates_a_future_with_no_metadata():
    """Several tests insert futures into `pending` directly. Those rows must
    still appear (the count is the honest part) with an unknown age rather than
    a fabricated zero."""
    from browserwright.daemon.server.relay import RelayServer, _ExtensionConn

    relay = RelayServer(port=0)
    ext = _ExtensionConn(conn=None, install_id="ext-1")
    relay._extensions["ext-1"] = ext
    loop = asyncio.get_running_loop()
    ext.pending[3] = loop.create_future()

    rows = relay.inflight_snapshot()
    assert len(rows) == 1
    assert rows[0]["elapsed_s"] is None
    assert rows[0]["kind"] == "?"
    ext.pending[3].cancel()


# ---- SIGUSR1 ---------------------------------------------------------------


def test_sigusr1_handler_is_installable_and_writes_to_the_daemon_log(
        tmp_path, monkeypatch):
    """The reason this exists: the executor's SIGTERM handler ends in
    `os._exit(0)`, so signalling a wedged executor destroys the evidence.
    SIGUSR1 is the read-only look-before-you-kill channel."""
    import faulthandler
    import signal

    from browserwright.daemon.observability import install_sigusr1_traceback

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    try:
        assert install_sigusr1_traceback("test-role") is True
        # It really is faulthandler on that signal, not our own stub: only
        # faulthandler's registration is removable by `faulthandler.unregister`.
        assert faulthandler.unregister(signal.SIGUSR1) is True
        assert install_sigusr1_traceback("test-role") is True
        log = _ipc.log_path()
        assert "armed SIGUSR1 stack dump" in log.read_text()
    finally:
        faulthandler.unregister(signal.SIGUSR1)
