from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from browserwright.daemon import _ipc
from browserwright.daemon.config import Config
from browserwright.daemon.server import listener as listener_mod
from browserwright.daemon.server import upstream as upstream_mod
from browserwright.daemon.server.state import DaemonState, UpstreamPhase


class _Resp:
    def __init__(self, status, body: str):
        self.status = status
        self.body = body
        self.headers: dict[str, str] = {}


class _HttpConn:
    def respond(self, status, body: str):
        return _Resp(status, body)


class _Router:
    def __init__(self):
        self.sent: list[tuple[int, dict]] = []
        self.routes: list[tuple[int, str]] = []
        self.registered: list[int] = []
        self.unregistered: list[int] = []
        self.released: list[int] = []
        self.upstream_senders: list[object] = []
        self.lifecycle: tuple[object, object, object] | None = None
        self.drained = 0
        self.daemon = None
        self.upstream = None

    async def _send_to_client(self, cid: int, text: str) -> None:
        self.sent.append((cid, json.loads(text)))

    async def forward_from_upstream(self, text: str) -> None:
        self.routes.append((-1, text))

    def register_client(self, cid: int, send_fn) -> None:
        self.registered.append(cid)

    def unregister_client(self, cid: int) -> None:
        self.unregistered.append(cid)

    def bind_lifecycle(
        self, ensure_upstream, trigger_disconnect, prepare_executor=None,
    ) -> None:
        self.lifecycle = (ensure_upstream, trigger_disconnect, prepare_executor)

    async def route_from_client(self, client, text: str) -> None:
        self.routes.append((client.client_id, text))

    async def release_client(self, cid: int) -> None:
        self.released.append(cid)

    async def drain_pre_open_buffers(self) -> None:
        self.drained += 1


def test_process_request_ping_and_query_parse(monkeypatch):
    handler = SimpleNamespace()
    process_request = listener_mod._make_process_request(handler)
    conn = _HttpConn()

    monkeypatch.setattr(listener_mod.os, "getpid", lambda: 2468)
    ping = process_request(conn, SimpleNamespace(path="/__ping__?x=1", headers={}))
    assert ping.status.value == 200
    assert ping.headers["Content-Type"] == "application/json"
    assert _ipc.parse_pong(ping.body.encode()) == (2468, listener_mod.__version__)

    allowed = process_request(
        conn,
        SimpleNamespace(
            path="/ws?client=ok",
            headers={"Origin": "https://web.example"},
        ),
    )
    assert allowed is None
    assert listener_mod._parse_query("/ws?client=a&client=b&empty=&q=x%20y") == {
        "client": "a",
        "q": "x y",
    }


@pytest.mark.asyncio
async def test_open_server_unix_bind(monkeypatch, tmp_path):
    calls: list[tuple[str, dict]] = []
    handler = SimpleNamespace(serve_one=object())

    async def fake_unix_serve(*args, **kwargs):
        calls.append(("unix", kwargs))
        return SimpleNamespace(kind="unix")

    monkeypatch.setattr(listener_mod, "unix_serve", fake_unix_serve)
    monkeypatch.setattr(listener_mod._ipc, "make_unix_socket", lambda: "unix-sock")
    monkeypatch.setattr(listener_mod._ipc, "sock_path", lambda: tmp_path / "daemon.sock")
    monkeypatch.setattr(listener_mod.os, "stat", lambda path: SimpleNamespace(st_mode=0o100600))

    unix = await listener_mod._open_server(handler)

    assert unix.kind == "unix"
    assert calls[0][0] == "unix"
    assert calls[0][1]["sock"] == "unix-sock"
    assert calls[0][1]["max_size"] == 100 * 1024 * 1024
    assert calls[0][1]["process_request"] is not None


@pytest.mark.asyncio
async def test_run_serve_existing_pid_and_extension_relay_bind_failure(monkeypatch, capsys):
    cleanup_calls: list[str] = []
    monkeypatch.setattr(
        listener_mod._ipc,
        "ping_status_async",
        lambda timeout: asyncio.sleep(0, result=(999, listener_mod.__version__)),
    )
    assert await listener_mod.run_serve(Config(backend="env")) == 1
    err = capsys.readouterr().err
    assert "already running (pid 999)" in err
    assert "browserwright-daemon status" in err

    class FakeServer:
        def __init__(self):
            self.closed = False
            self.waited = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            self.waited = True

    class BadRelay:
        def __init__(self, *, host, port):
            cleanup_calls.append(f"relay:{host}:{port}")

        async def start(self):
            raise OSError("busy")

    server = FakeServer()
    monkeypatch.setattr(
        listener_mod._ipc,
        "ping_status_async",
        lambda timeout: asyncio.sleep(0, result=(None, None)),
    )
    monkeypatch.setattr(listener_mod._ipc, "cleanup_endpoint", lambda: cleanup_calls.append("cleanup"))
    monkeypatch.setattr(listener_mod._ipc, "write_pid", lambda pid: cleanup_calls.append(f"pid:{pid}"))
    monkeypatch.setattr(listener_mod, "_cleanup_orphan_rdp_chrome", lambda: cleanup_calls.append("orphans"))
    monkeypatch.setattr(listener_mod, "_wire_logging", lambda: None)
    monkeypatch.setattr(listener_mod, "install_json_logging_if_requested", lambda: None)
    monkeypatch.setattr(listener_mod, "_open_server", lambda handler: asyncio.sleep(0, result=server))
    monkeypatch.setattr(listener_mod, "RelayServer", BadRelay)

    cfg = Config(backend="extension")
    cfg.backends.extension.port = 22345

    assert await listener_mod.run_serve(cfg) == 2
    assert server.closed is True
    assert server.waited is True
    assert cleanup_calls == ["cleanup", "orphans", f"pid:{listener_mod.os.getpid()}", "relay:127.0.0.1:22345", "cleanup"]
    assert "failed to bind extension relay" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_client_handler_routes_session_frames_and_releases(monkeypatch):
    state = DaemonState("env")
    router = _Router()

    class Holder:
        is_open = True
        upstream = object()

        async def send_text(self, text: str) -> None:
            return None

        async def ensure_open(self) -> None:
            return None

        async def trigger_close(self, reason: str) -> None:
            return None

        async def prepare_executor(self, session_id: str) -> None:
            return None

    class Conn:
        request = SimpleNamespace(path="/ws?client=alice&session=s-1")

        def __aiter__(self):
            self.items = iter(['{"id":1}', b'{"id":2}', object()])
            return self

        async def __anext__(self):
            try:
                return next(self.items)
            except StopIteration:
                raise StopAsyncIteration

        async def send(self, text: str) -> None:
            raise AssertionError("send should not be used by this test")

    holder = Holder()
    router.upstream = holder.upstream
    ctx = SimpleNamespace(state=state, router=router, holder=holder, backend="env")
    daemon = SimpleNamespace(context_for_required=lambda session_id: ctx, _next_client_id=iter([77]))
    monkeypatch.setattr("browserwright.session_registry.get", lambda sid: {"name": "Session One"})

    await listener_mod._ClientHandler(daemon, Config(backend="env")).serve_one(Conn())

    assert router.registered == [77]
    assert router.unregistered == [77]
    assert router.released == [77]
    assert router.lifecycle == (
        holder.ensure_open, holder.trigger_close, holder.prepare_executor)
    assert router.upstream is holder.upstream
    assert [(cid, json.loads(text)) for cid, text in router.routes] == [
        (77, {"id": 1}),
        (77, {"id": 2}),
    ]
    client = state.clients[77]
    assert client.label == "alice"
    assert client.session_id == "s-1"
    assert client.session_name == "Session One"


@pytest.mark.asyncio
async def test_extension_upstream_success_wires_callbacks_and_ready_events(monkeypatch):
    state = DaemonState("extension")
    client = state.allocate_client("client")
    router = _Router()
    holder = listener_mod._UpstreamHolder(state, router, Config(backend="extension", timeout=0.01))
    holder.relay = object()
    opened: list[float] = []

    class FakeExtensionUpstream:
        is_open = True
        ws_url = "ext://ready"

        def __init__(self, *, relay, on_frame, on_close):
            assert relay is holder.relay
            self._relay = relay

        async def open(self, *, timeout):
            opened.append(timeout)

        def attach(self, router):
            router.upstream = self

        def detach(self, router):
            if router.upstream is self:
                router.upstream = None

    monkeypatch.setattr(listener_mod, "ExtensionUpstream", FakeExtensionUpstream)

    await holder.ensure_open()

    assert opened == [60.0]
    assert state.upstream_phase == UpstreamPhase.CONNECTED
    assert state.upstream_ws_url == "ext://ready"
    assert router.drained == 1
    assert [msg["method"] for _, msg in router.sent] == [
        "BrowserwrightDaemon.upstreamConnecting",
        "BrowserwrightDaemon.upstreamReady",
    ]
    assert router.sent[0] == (
        client.client_id,
        {"method": "BrowserwrightDaemon.upstreamConnecting", "params": {"backend": "extension"}},
    )
    assert router.sent[1][1]["params"] == {"backend": "extension", "ws_url": "ext://ready"}
    assert router.upstream is holder.upstream


@pytest.mark.asyncio
async def test_rdp_launch_kill_and_upstream_closed_drop_context(monkeypatch):
    cfg = Config(backend="rdp")
    cfg.backends.rdp.port = 0
    launched: list[dict] = []

    async def fake_launch_chrome(cfg, *, profile, persistent, port, timeout):
        launched.append({"profile": profile, "persistent": persistent, "port": port, "timeout": timeout})
        return {"extras": {"pid": 111, "profile_path": "/tmp/profile"}}

    monkeypatch.setattr("browserwright.daemon.launch_chrome.launch_chrome", fake_launch_chrome)
    holder = listener_mod._UpstreamHolder(DaemonState("rdp"), _Router(), cfg, session_id="abc")
    await holder._launch_rdp_chrome(cfg)
    assert launched == [
        {"profile": "bs-sabc", "persistent": True, "port": holder.rdp_port, "timeout": 30.0}
    ]
    assert holder.rdp_pid == 111
    assert holder.rdp_profile_dir == "/tmp/profile"
    assert holder._cfg.backends.rdp.port == holder.rdp_port

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(listener_mod.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    holder._kill_rdp_chrome()
    assert holder.rdp_pid is None
    assert killed == [(111, listener_mod.signal.SIGTERM)]

    state = DaemonState("rdp")
    await state.set_connected("ws://rdp")
    router = _Router()
    dropped: list[str] = []
    router.daemon = SimpleNamespace(drop_rdp_context=lambda sid: dropped.append(sid))
    closing_holder = listener_mod._UpstreamHolder(state, router, Config(backend="rdp"), session_id="abc")
    class ClosingUpstream:
        is_open = True

        async def close(self, **kwargs):
            return None

        def detach(self, bound_router):
            if bound_router.upstream is self:
                bound_router.upstream = None

    closing_holder.upstream = ClosingUpstream()
    router.upstream = closing_holder.upstream

    await closing_holder._on_upstream_closed("upstream-eof")

    assert dropped == ["abc"]
    assert state.upstream_phase == UpstreamPhase.DISCONNECTED
    assert router.upstream is None


@pytest.mark.asyncio
async def test_rdp_attach_session_ensure_open_does_not_launch(monkeypatch):
    cfg = Config(backend="rdp")
    cfg.backends.rdp.port = 9444
    state = DaemonState("rdp")
    router = _Router()
    holder = listener_mod._UpstreamHolder(state, router, cfg, session_id="attach")
    holder.rdp_owns_browser = False
    launched: list[str] = []

    async def fake_launch(_cfg):
        launched.append("launch")

    async def fake_resolve(_cfg):
        assert _cfg.backends.rdp.port == 9444
        return SimpleNamespace(ws_url="ws://127.0.0.1:9444/devtools/browser/x")

    class FakeConn:
        is_open = True

        def __init__(self, *, on_frame, on_close, state=None,
                     on_end_session=None):
            self.send_command = self._send_command

        async def open(self, ws_url, **kwargs):
            assert ws_url == "ws://127.0.0.1:9444/devtools/browser/x"

        async def _send_command(self, method, params):
            return {}

        def attach(self, bound_router):
            bound_router.upstream = self

        def detach(self, bound_router):
            if bound_router.upstream is self:
                bound_router.upstream = None

    monkeypatch.setattr(holder, "_launch_rdp_chrome", fake_launch)
    monkeypatch.setattr(listener_mod, "resolve", fake_resolve)
    monkeypatch.setattr(listener_mod, "CdpUpstream", FakeConn)

    await holder.ensure_open()

    assert launched == []
    assert state.upstream_phase == UpstreamPhase.CONNECTED


@pytest.mark.asyncio
async def test_graceful_shutdown_closes_every_context_despite_errors(caplog):
    calls: list[str] = []

    async def close_ok(reason):
        calls.append(f"ok:{reason}")

    async def close_bad(reason):
        calls.append(f"bad:{reason}")
        raise RuntimeError("boom")

    daemon = SimpleNamespace(
        all_contexts=lambda: [
            SimpleNamespace(backend="env", holder=SimpleNamespace(trigger_close=close_bad)),
            SimpleNamespace(backend="rdp", holder=SimpleNamespace(trigger_close=close_ok)),
        ]
    )

    await listener_mod._graceful_shutdown(daemon)

    assert calls == ["bad:daemon_shutdown", "ok:daemon_shutdown"]
    assert "shutdown close failed for env" in caplog.text


@pytest.mark.asyncio
async def test_upstream_open_and_heartbeat_failure(monkeypatch):
    connect_calls: list[tuple[str, dict]] = []
    created: list[object] = []

    class FakeWs:
        async def send(self, text: str) -> None:
            return None

    async def fake_connect(url, **kwargs):
        connect_calls.append((url, kwargs))
        return FakeWs()

    def fake_create_task(coro):
        coro.close()
        task = SimpleNamespace(cancel=lambda: None)
        created.append(task)
        return task

    monkeypatch.setenv("NO_PROXY", "example.com")
    monkeypatch.setattr(upstream_mod.websockets, "connect", fake_connect)
    monkeypatch.setattr(upstream_mod.asyncio, "create_task", fake_create_task)

    conn = upstream_mod.UpstreamConnection(lambda text: asyncio.sleep(0), lambda reason: asyncio.sleep(0))
    await conn.open("ws://localhost:9222/devtools/browser/x", timeout=0.5)

    assert conn.is_open is True
    assert conn.ws_url == "ws://localhost:9222/devtools/browser/x"
    assert len(created) == 2
    assert connect_calls[0][0] == "ws://localhost:9222/devtools/browser/x"
    kwargs = connect_calls[0][1]
    assert kwargs["compression"] is None
    assert kwargs["max_size"] == 100 * 1024 * 1024
    assert upstream_mod.os.environ["NO_PROXY"] == "example.com"

    heartbeat = upstream_mod.UpstreamConnection(lambda text: asyncio.sleep(0), lambda reason: asyncio.sleep(0))
    heartbeat._ws = object()
    attempts = 0

    async def fake_sleep(delay):
        return None

    async def fake_send_command(method, **kwargs):
        nonlocal attempts
        attempts += 1
        raise asyncio.TimeoutError

    monkeypatch.setattr(upstream_mod.asyncio, "sleep", fake_sleep)
    heartbeat.send_command = fake_send_command  # type: ignore[method-assign]

    await heartbeat._heartbeat_loop()

    assert attempts == 1


@pytest.mark.asyncio
async def test_upstream_reader_handles_bad_frames_callback_errors_and_close_callback_errors():
    frames: list[str] = []
    closed: list[str] = []

    async def on_frame(text: str) -> None:
        frames.append(text)
        if "raise" in text:
            raise RuntimeError("client send failed")

    async def on_close(reason: str) -> None:
        closed.append(reason)
        raise RuntimeError("ignored")

    class FakeWs:
        def __aiter__(self):
            self.items = iter([{"not": "wire"}, b"\xff", '{"method":"raise"}'])
            return self

        async def __anext__(self):
            try:
                return next(self.items)
            except StopIteration:
                raise StopAsyncIteration

    conn = upstream_mod.UpstreamConnection(on_frame, on_close)
    conn._ws = FakeWs()

    await conn._reader_loop()

    assert frames == ["�", '{"method":"raise"}']
    assert closed == ["upstream-eof"]
