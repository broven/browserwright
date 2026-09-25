from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from browserwright.daemon import _ipc
from browserwright.daemon.config import Config
from browserwright.daemon.server import listener as listener_mod
from browserwright.daemon.errors import Unavailable
from browserwright.daemon.server import upstream as upstream_mod
from browserwright.daemon.server import upstream_context as upstream_context_mod
from browserwright.daemon.server.state import DaemonState, UpstreamPhase
from browserwright.daemon.server.upstream import CdpUpstream
from browserwright.daemon.server.upstream_context import UpstreamHolder, build_context


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
        self.lifecycle: tuple[object, object] | None = None
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
        self, ensure_upstream, trigger_disconnect,
    ) -> None:
        self.lifecycle = (ensure_upstream, trigger_disconnect)

    async def route_from_client(self, client, text: str) -> None:
        self.routes.append((client.client_id, text))

    async def release_client(self, cid: int) -> None:
        self.released.append(cid)

    async def drain_pre_open_buffers(self) -> None:
        self.drained += 1


def test_endpoint_process_request_ping_origin_and_unknown_path(monkeypatch):
    """ADR-0011 moved `/__ping__`, Origin validation and path dispatch onto the
    one endpoint server. All three are decided before any ws upgrade."""
    from browserwright.daemon.server import facade as facade_mod

    endpoint = facade_mod.PlaywrightFacade(cfg=Config(), port=0)
    conn = _HttpConn()

    monkeypatch.setattr(facade_mod.os, "getpid", lambda: 2468)
    ping = endpoint._process_request(
        conn, SimpleNamespace(path="/__ping__?x=1", headers={}))
    assert ping.status.value == 200
    assert ping.headers["Content-Type"] == "application/json"
    pong = _ipc.parse_pong(ping.body.encode())
    assert (pong.pid, pong.version) == (2468, listener_mod.__version__)

    # Each ws surface is allowed through when no Origin is present...
    for path in ("/cdp", "/control", "/exec"):
        assert endpoint._process_request(
            conn, SimpleNamespace(path=f"{path}?client=ok", headers={})) is None

    # ...and refused outright when one is, on every surface. Nothing that
    # legitimately reaches this endpoint runs in a browser.
    for path in ("/cdp", "/control", "/exec"):
        denied = endpoint._process_request(
            conn,
            SimpleNamespace(path=path,
                            headers={"Origin": "https://web.example"}))
        assert denied.status.value == 403

    unknown = endpoint._process_request(
        conn, SimpleNamespace(path="/nope", headers={}))
    assert unknown.status.value == 404


def test_parse_query_keeps_first_value_and_drops_empties():
    assert listener_mod._parse_query("/ws?client=a&client=b&empty=&q=x%20y") == {
        "client": "a",
        "q": "x y",
    }


class _AnsweringWS:
    """A browser-level CDP websocket that answers every command with `{}`."""

    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []

    async def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        await self.inbox.put(json.dumps({"id": msg["id"], "result": {}}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.inbox.get()

    async def close(self, **_kw):
        return None


async def _noop_frame(_value: str) -> None:
    return None



@pytest.mark.asyncio
async def test_run_serve_existing_pid_and_extension_relay_bind_failure(monkeypatch, capsys):
    cleanup_calls: list[str] = []
    monkeypatch.setattr(
        listener_mod._ipc,
        "ping_status_async",
        lambda timeout, **kw: asyncio.sleep(
            0, result=_ipc.PongInfo(pid=999, version=listener_mod.__version__)),
    )
    assert await listener_mod.run_serve(Config(backend="env")) == 1
    err = capsys.readouterr().err
    assert "already running (pid 999)" in err
    assert "browserwright-daemon status" in err

    class FakeEndpoint:
        """Stands in for the one TCP endpoint, which binds before the relay."""

        def __init__(self, **kwargs):
            cleanup_calls.append("endpoint")
            self.stopped = False

        async def start(self):
            return 19990

        async def stop(self):
            self.stopped = True
            cleanup_calls.append("endpoint-stop")

    class BadRelay:
        on_extension_hello = None
        on_extension_closed = None

        def __init__(self, *, host, port):
            cleanup_calls.append(f"relay:{host}:{port}")

        def add_event_listener(self, handler):
            return None

        async def start(self):
            cleanup_calls.append("relay-start")
            raise OSError("busy")

    monkeypatch.setattr(
        listener_mod._ipc,
        "ping_status_async",
        lambda timeout, **kw: asyncio.sleep(0, result=_ipc.NO_PONG),
    )
    monkeypatch.setattr(listener_mod._ipc, "cleanup_endpoint", lambda: cleanup_calls.append("cleanup"))
    monkeypatch.setattr(listener_mod._ipc, "write_pid", lambda pid: cleanup_calls.append(f"pid:{pid}"))
    monkeypatch.setattr(listener_mod._ipc, "write_endpoint_state", lambda url: cleanup_calls.append(f"state:{url}"))
    monkeypatch.setattr(listener_mod, "_cleanup_orphan_cdp_chrome", lambda: cleanup_calls.append("orphans"))
    monkeypatch.setattr(listener_mod, "_wire_logging", lambda: None)
    monkeypatch.setattr(listener_mod, "install_json_logging_if_requested", lambda: None)
    monkeypatch.setattr(listener_mod, "PlaywrightFacade", FakeEndpoint)
    monkeypatch.setattr(upstream_context_mod, "RelayServer", BadRelay)

    cfg = Config(backend="extension")
    cfg.backends.extension.port = 22345

    assert await listener_mod.run_serve(cfg) == 2
    # The relay object is built with the shared context, but it BINDS only
    # after the endpoint (whose bind is the mutual exclusion between daemons)
    # has bound and published its URL; the endpoint is stopped again when the
    # relay cannot come up — a daemon with no relay would serve an extension
    # backend that can never connect.
    assert cleanup_calls == [
        "cleanup", "orphans", "relay:127.0.0.1:22345",
        f"pid:{listener_mod.os.getpid()}",
        "endpoint", "state:http://127.0.0.1:19990",
        "relay-start", "endpoint-stop", "cleanup",
    ]
    assert "failed to bind extension relay" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_run_serve_endpoint_bind_failure_is_fatal(monkeypatch, capsys):
    """The endpoint is the only client-facing door: no bind, no daemon.

    Contrast the pre-ADR-0011 facade, whose bind failure was non-fatal because
    the unix control socket kept serving the agent path. There is no second
    path left to fall back to.
    """
    monkeypatch.setattr(
        listener_mod._ipc, "ping_status_async",
        lambda timeout, **kw: asyncio.sleep(0, result=_ipc.NO_PONG))
    monkeypatch.setattr(listener_mod._ipc, "cleanup_endpoint", lambda: None)
    monkeypatch.setattr(listener_mod._ipc, "write_pid", lambda pid: None)
    monkeypatch.setattr(listener_mod, "_cleanup_orphan_cdp_chrome", lambda: None)
    monkeypatch.setattr(listener_mod, "_wire_logging", lambda: None)
    monkeypatch.setattr(listener_mod, "install_json_logging_if_requested", lambda: None)

    class RefusedEndpoint:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            raise OSError("address already in use")

    monkeypatch.setattr(listener_mod, "PlaywrightFacade", RefusedEndpoint)

    assert await listener_mod.run_serve(Config(backend="extension")) == 2
    err = capsys.readouterr().err
    assert "failed to bind endpoint" in err
    assert "lsof -nP -iTCP:19990" in err


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
    # Lifecycle slots are bound once, when the context is built — never
    # re-bound per client connection.
    assert router.lifecycle is None
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
    opened: list[float] = []

    class ReadyRelay:
        port = 19989
        on_extension_hello = None
        on_extension_closed = None

        def __init__(self, *, host, port):
            self.listeners: list[object] = []
            self.handler = None

        def add_event_listener(self, handler):
            self.listeners.append(handler)

        async def wait_ready(self, timeout):
            opened.append(timeout)

        def set_event_handler(self, handler):
            self.handler = handler

    monkeypatch.setattr(upstream_context_mod, "RelayServer", ReadyRelay)
    ctx = build_context(backend="extension",
                        cfg=Config(backend="extension", timeout=0.01))
    relay = ctx.upstream.relay
    # The factory hands the relay's lifecycle events to the adapter before
    # the relay can accept a connection.
    assert relay.on_extension_hello is not None
    assert relay.on_extension_closed is not None
    assert len(relay.listeners) == 1

    state = ctx.state
    client = state.allocate_client("client")
    sent: list[dict] = []

    async def send(text: str) -> None:
        sent.append(json.loads(text))

    ctx.router.register_client(client.client_id, send)

    await ctx.holder.ensure_open()

    # Generous open budget: the user may still have to load the extension.
    assert opened == [60.0]
    assert relay.handler is not None
    assert state.upstream_phase == UpstreamPhase.CONNECTED
    ws_url = "ws://127.0.0.1:19989/__extension_relay__"
    assert state.upstream_ws_url == ws_url
    assert [msg["method"] for msg in sent] == [
        "BrowserwrightDaemon.upstreamConnecting",
        "BrowserwrightDaemon.upstreamReady",
    ]
    assert sent[0]["params"] == {"backend": "extension"}
    assert sent[1]["params"] == {"backend": "extension", "ws_url": ws_url}
    assert ctx.router.upstream is ctx.upstream


@pytest.mark.asyncio
async def test_cdp_launch_kill_and_upstream_closed_drop_context(monkeypatch):
    cfg = Config(backend="cdp")
    cfg.backends.cdp.port = 0
    launched: list[dict] = []

    async def fake_launch_chrome(cfg, *, profile, persistent, port, timeout):
        launched.append({"profile": profile, "persistent": persistent, "port": port, "timeout": timeout})
        return {"extras": {"pid": 111, "profile_path": "/tmp/profile"}}

    async def fail_resolve(_cfg):
        raise Unavailable("not listening yet")

    monkeypatch.setattr("browserwright.daemon.launch_chrome.launch_chrome", fake_launch_chrome)
    monkeypatch.setattr("browserwright.daemon.resolver.resolve", fail_resolve)
    up = CdpUpstream(_noop_frame, _noop_frame, cfg=cfg, session_id="abc",
                     owns_browser=True)
    with pytest.raises(Unavailable):
        await up.open()
    pinned = up.cfg.backends.cdp.port
    assert pinned
    assert launched == [
        {"profile": "bs-sabc", "persistent": True, "port": pinned, "timeout": 30.0}
    ]
    assert up.browser_pid == 111

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(upstream_mod.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    await up.close()
    assert up.browser_pid is None
    assert killed == [(111, listener_mod.signal.SIGTERM)]

    state = DaemonState("cdp")
    await state.set_connected("ws://cdp")
    router = _Router()
    dropped: list[str] = []

    class ClosingUpstream:
        is_open = True

        async def close(self, **kwargs):
            return None

        def detach(self, bound_router):
            if bound_router.upstream is self:
                bound_router.upstream = None

    closing_holder = UpstreamHolder(state, router, lambda _h: ClosingUpstream())
    closing_holder.on_upstream_lost = lambda: dropped.append("abc")
    router.upstream = closing_holder.upstream

    await closing_holder.on_upstream_closed("upstream-eof")

    assert dropped == ["abc"]
    assert state.upstream_phase == UpstreamPhase.DISCONNECTED
    assert router.upstream is None


@pytest.mark.asyncio
async def test_cdp_attach_session_ensure_open_does_not_launch(monkeypatch):
    cfg = Config(backend="cdp")
    cfg.backends.cdp.port = 9444
    state = DaemonState("cdp")
    router = _Router()
    launched: list[str] = []

    async def fake_launch(*_a, **_kw):
        launched.append("launch")

    async def fake_resolve(_cfg):
        assert _cfg.backends.cdp.port == 9444
        return SimpleNamespace(ws_url="ws://127.0.0.1:9444/devtools/browser/x")

    async def fake_connect(url, **_kw):
        assert url == "ws://127.0.0.1:9444/devtools/browser/x"
        return _AnsweringWS()

    monkeypatch.setattr("browserwright.daemon.launch_chrome.launch_chrome", fake_launch)
    monkeypatch.setattr("browserwright.daemon.resolver.resolve", fake_resolve)
    monkeypatch.setattr(upstream_mod.websockets, "connect", fake_connect)
    holder = UpstreamHolder(state, router, lambda h: CdpUpstream(
        router.forward_from_upstream, h.on_upstream_closed, state=state,
        cfg=cfg, session_id="attach", owns_browser=False))

    try:
        await holder.ensure_open()

        assert launched == []
        assert state.upstream_phase == UpstreamPhase.CONNECTED
    finally:
        await holder.trigger_close("daemon_shutdown")


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
            SimpleNamespace(backend="cdp", holder=SimpleNamespace(trigger_close=close_ok)),
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
