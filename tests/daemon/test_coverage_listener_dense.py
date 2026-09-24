from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from browserwright.daemon import _ipc
from browserwright.daemon.config import Config
from browserwright.daemon.errors import Unavailable
from browserwright.daemon.server import listener as listener_mod
from browserwright.daemon.server import relay as relay_mod
from browserwright.daemon.server import upstream as upstream_mod
from browserwright.daemon.server.extension_upstream import ExtensionUpstream
from browserwright.daemon.server.relay import GhostTarget, RelayServer, _ExtensionConn
from browserwright.daemon.server.state import DaemonState, UpstreamPhase
from browserwright.daemon.server.upstream import CdpUpstream
from browserwright.daemon.server.upstream_context import UpstreamHolder


class _FakeResponse:
    def __init__(self, status, body: str):
        self.status = status
        self.body = body
        self.headers: dict[str, str] = {}


class _FakeHttpConn:
    def __init__(self):
        self.responses: list[_FakeResponse] = []

    def respond(self, status, body: str):
        resp = _FakeResponse(status, body)
        self.responses.append(resp)
        return resp


class _FakeRouter:
    def __init__(self):
        self.sent: list[tuple[int, dict]] = []
        self.upstream_senders: list[object] = []
        self.drained = 0
        self.forwarded: list[str] = []
        self.daemon = None
        self.upstream = None

    async def _send_to_client(self, cid: int, text: str) -> None:
        self.sent.append((cid, json.loads(text)))

    async def drain_pre_open_buffers(self):
        self.drained += 1

    async def forward_from_upstream(self, text: str) -> None:
        self.forwarded.append(text)


@pytest.mark.asyncio
async def test_upstream_holder_extension_no_extension_emits_connecting_then_fails():
    state = DaemonState(backend_name="extension")
    client = state.allocate_client("listener-dense")
    router = _FakeRouter()

    class NoExtensionRelay:
        async def wait_ready(self, timeout):
            raise asyncio.TimeoutError

        def set_event_handler(self, handler):
            raise AssertionError("an unopened adapter must not take events")

    holder = UpstreamHolder(state, router, lambda h: ExtensionUpstream(
        NoExtensionRelay(), router.forward_from_upstream,
        h.on_upstream_closed, open_timeout=0.01))

    with pytest.raises(Unavailable):
        await holder.ensure_open()

    assert state.upstream_phase == UpstreamPhase.DISCONNECTED
    assert state.last_close_reason == "backend_lost"
    assert router.sent == [
        (
            client.client_id,
            {
                "method": "BrowserwrightDaemon.upstreamConnecting",
                "params": {"backend": "extension"},
            },
        )
    ]
    assert router.upstream is None
    assert holder.is_open is False


@pytest.mark.asyncio
async def test_upstream_holder_open_chrome_success_wires_router_and_internal_command(monkeypatch):
    state = DaemonState(backend_name="env")
    router = _FakeRouter()
    opened: dict[str, object] = {}

    class FakeWS:
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

    ws = FakeWS()

    async def fake_connect(url, **kwargs):
        opened["ws_url"] = url
        return ws

    async def fake_resolve(cfg):
        assert cfg.backend == "env"
        return SimpleNamespace(ws_url="ws://127.0.0.1/devtools/browser/fake")

    monkeypatch.setattr("browserwright.daemon.resolver.resolve", fake_resolve)
    monkeypatch.setattr(upstream_mod.websockets, "connect", fake_connect)
    holder = UpstreamHolder(state, router, lambda h: CdpUpstream(
        on_frame=router.forward_from_upstream, on_close=h.on_upstream_closed,
        state=state, cfg=Config(backend="env", timeout=0.01)))

    try:
        await holder.ensure_open()

        assert opened["ws_url"] == "ws://127.0.0.1/devtools/browser/fake"
        assert [(m["method"], m.get("params")) for m in ws.sent] == [
            ("Target.setDiscoverTargets", {"discover": True})]
        assert state.upstream_phase == UpstreamPhase.CONNECTED
        assert state.upstream_ws_url == "ws://127.0.0.1/devtools/browser/fake"
        assert router.upstream is holder.upstream
        assert router.drained == 1
    finally:
        await holder.trigger_close("daemon_shutdown")


@pytest.mark.asyncio
async def test_trigger_close_sends_detach_and_closed_events_then_clears_callbacks():
    state = DaemonState(backend_name="cdp")
    client = state.allocate_client("c1")
    state.bind_session(client.client_id, "local-1", "up-1", "target-1")
    await state.set_connected("ws://chrome")
    router = _FakeRouter()
    closed: list[tuple[int, str]] = []
    detached_phases: list[UpstreamPhase] = []

    class FakeUpstream:
        is_open = True

        async def close(self, *, code=1000, reason=""):
            closed.append((code, reason))

        def detach(self, router):
            detached_phases.append(state.upstream_phase)
            if router.upstream is self:
                router.upstream = None

    holder = UpstreamHolder(state, router, lambda _h: FakeUpstream())
    router.upstream = holder.upstream

    await holder.trigger_close("skill_disconnect")

    assert [m["method"] for _, m in router.sent] == [
        "Target.detachedFromTarget",
        "BrowserwrightDaemon.upstreamClosed",
    ]
    assert router.sent[0][1]["params"] == {"sessionId": "local-1", "targetId": "target-1"}
    assert router.sent[1][1]["params"] == {"reason": "skill_disconnect"}
    assert router.upstream is None
    # The adapter is closed exactly once; whatever it owns (a create-owned
    # Chrome) ends inside its own close.
    assert closed == [(1000, "skill_disconnect")]
    assert detached_phases == [UpstreamPhase.DISCONNECTED]
    assert state.upstream_phase == UpstreamPhase.DISCONNECTED


@pytest.mark.asyncio
async def test_idle_watchdog_closes_idle_cdp_context_and_drops_it(monkeypatch):
    state = DaemonState(backend_name="cdp")
    await state.set_connected("ws://cdp")
    state.last_activity_at -= 99
    calls: list[str] = []

    async def trigger_close(reason):
        calls.append(reason)
        await state.set_disconnected()

    ctx = SimpleNamespace(
        backend="cdp",
        session_id="s-id",
        state=state,
        holder=SimpleNamespace(trigger_close=trigger_close),
    )
    daemon = SimpleNamespace(
        all_contexts=lambda: [ctx],
        dropped=[],
        drop_context=lambda sid: daemon.dropped.append(sid),
    )
    sleeps = 0

    async def fake_sleep(delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(listener_mod.asyncio, "sleep", fake_sleep)

    await listener_mod._idle_watchdog(daemon, idle_after=0.01)

    assert calls == ["idle_close"]
    assert daemon.dropped == ["s-id"]


@pytest.mark.asyncio
async def test_upstream_reader_intercepts_internal_ids_forwards_binary_and_notifies_close():
    frames: list[str] = []
    closes: list[str] = []
    conn = upstream_mod.UpstreamConnection(
        on_frame=lambda text: frames.append(text) or asyncio.sleep(0),
        on_close=lambda reason: closes.append(reason) or asyncio.sleep(0),
    )
    fut = asyncio.get_running_loop().create_future()
    conn._pending_internal[-2_000_000_000] = fut

    class FakeWs:
        def __aiter__(self):
            self._it = iter([
                json.dumps({"id": -2_000_000_000, "result": {"product": "Fake"}}),
                b'{"method":"Page.loadEventFired"}',
                123,
            ])
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

    conn._ws = FakeWs()

    await conn._reader_loop()

    assert fut.result()["result"]["product"] == "Fake"
    assert frames == ['{"method":"Page.loadEventFired"}']
    assert closes == ["upstream-eof"]


@pytest.mark.asyncio
async def test_upstream_close_cancels_tasks_rejects_pending_and_resets_ws_url():
    conn = upstream_mod.UpstreamConnection(lambda text: asyncio.sleep(0), lambda reason: asyncio.sleep(0))
    fut = asyncio.get_running_loop().create_future()
    conn._pending_internal[1] = fut
    closed: list[tuple[int, str]] = []

    async def sleeper():
        await asyncio.sleep(100)

    class FakeWs:
        async def close(self, *, code=1000, reason=""):
            closed.append((code, reason))

    conn._ws = FakeWs()
    conn._ws_url = "ws://localhost/devtools/browser/x"
    conn._reader_task = asyncio.create_task(sleeper())
    conn._heartbeat_task = asyncio.create_task(sleeper())

    await conn.close(code=1001, reason="going away")

    assert conn._ws is None
    assert conn.ws_url is None
    assert closed == [(1001, "going away")]
    assert isinstance(fut.exception(), ConnectionError)
    assert conn._pending_internal == {}
    await asyncio.sleep(0)
    assert conn._reader_task.cancelled()
    assert conn._heartbeat_task.cancelled()


def test_localhost_bypass_proxy_augments_and_restores(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "example.com")
    with upstream_mod._localhost_bypass_proxy("ws://localhost:9222/devtools/browser/x"):
        no_proxy = upstream_mod.os.environ["NO_PROXY"]
        assert "example.com" in no_proxy
        assert "127.0.0.1" in no_proxy
        assert "localhost" in no_proxy
        assert "::1" in no_proxy
    assert upstream_mod.os.environ["NO_PROXY"] == "example.com"

    monkeypatch.delenv("NO_PROXY", raising=False)
    with upstream_mod._localhost_bypass_proxy("wss://remote.example/ws"):
        assert "NO_PROXY" not in upstream_mod.os.environ


def test_relay_process_request_status_and_origin_filter():
    relay = RelayServer()
    relay._extensions["ready"] = SimpleNamespace(
        install_id="install-1",
        hello_received=SimpleNamespace(is_set=lambda: True),
        tabs={1: object(), 2: object()},
    )
    conn = _FakeHttpConn()

    status = relay._process_request(conn, SimpleNamespace(path="/__status__", headers={}))
    assert status.status.value == 200
    body = json.loads(status.body)
    assert body["running"] is True
    assert body["extensions"] == 1
    assert body["install_ids"] == ["install-1"]
    assert body["tab_count"] == 2

    blocked = relay._process_request(
        conn,
        SimpleNamespace(path="/", headers={"Origin": "https://evil.example"}),
    )
    assert blocked.status.value == 403
    assert "anti-CSRF" in blocked.body
    assert relay._process_request(
        conn,
        SimpleNamespace(path="/", headers={"Origin": "chrome-extension://abc"}),
    ) is None


@pytest.mark.asyncio
async def test_relay_dispatch_handles_protocol_matrix_and_pending_errors():
    relay = RelayServer()
    sent: list[dict] = []
    events: list[dict] = []

    class FakeConn:
        async def send(self, text: str):
            sent.append(json.loads(text))

    ext = _ExtensionConn(conn=FakeConn())
    relay._extensions["tmp"] = ext
    relay.set_event_handler(lambda msg: events.append(msg) or asyncio.sleep(0))

    await relay._dispatch_from_extension(
        ext,
        "tmp",
        {"type": "hello", "installId": "install-2", "browser": "chrome", "version": "1"},
    )
    assert "install-2" in relay._extensions
    assert ext.hello_received.is_set()
    assert relay.is_ready
    assert sent[0]["type"] == "helloAck"

    await relay._dispatch_from_extension(ext, "tmp", {"type": "ping", "ts": 123})
    assert sent[-1] == {"type": "pong", "ts": 123}

    await relay._dispatch_from_extension(
        ext,
        "tmp",
        {"type": "attached", "tabId": 7, "targetInfo": {"url": "https://x/", "title": "X"}},
    )
    assert ext.tabs[7].target_id == "ext-tab-7"

    fut = asyncio.get_running_loop().create_future()
    ext.pending[9] = fut
    await relay._dispatch_from_extension(
        ext,
        "tmp",
        {"type": "response", "id": 9, "error": {"code": -32001, "message": "nope"}},
    )
    assert isinstance(fut.exception(), relay_mod._CommandError)
    assert fut.exception().code == -32001

    await relay._dispatch_from_extension(
        ext,
        "tmp",
        {"type": "event", "tabId": 7, "method": "Page.frameStoppedLoading", "params": {}},
    )
    assert events[0]["method"] == "Page.frameStoppedLoading"

    await relay._dispatch_from_extension(ext, "tmp", {"type": "detached", "tabId": 7})
    assert 7 not in ext.tabs


@pytest.mark.asyncio
async def test_relay_handler_cleanup_does_not_remove_replacement_connection():
    relay = RelayServer()
    new_ext = _ExtensionConn(conn=SimpleNamespace())
    new_ext.install_id = "same-install"
    new_ext.hello_received.set()
    captured_old: dict[str, _ExtensionConn] = {}

    class FakeConn:
        def __init__(self):
            self.frames = [
                json.dumps({
                    "type": "hello",
                    "installId": "same-install",
                    "browser": "chrome",
                    "version": "1",
                }),
            ]
            self.sent: list[str] = []

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.frames:
                old_ext = relay._extensions["same-install"]
                captured_old["ext"] = old_ext
                fut = asyncio.get_running_loop().create_future()
                old_ext.pending[1] = fut
                relay._extensions["same-install"] = new_ext
                raise StopAsyncIteration
            return self.frames.pop(0)

        async def send(self, text: str):
            self.sent.append(text)

    await relay._handler(FakeConn())

    assert relay._extensions["same-install"] is new_ext
    old_ext = captured_old["ext"]
    assert isinstance(old_ext.pending[1].exception(), ConnectionError)


@pytest.mark.asyncio
async def test_relay_request_cleanup_and_public_helpers_without_socket(monkeypatch):
    relay = RelayServer()
    sent: list[dict] = []
    auto_results: list[dict] = []

    class FakeConn:
        async def send(self, text: str):
            msg = json.loads(text)
            sent.append(msg)
            if auto_results:
                ext.pending[msg["id"]].set_result(auto_results.pop(0))

    ext = _ExtensionConn(conn=FakeConn(), install_id="install-ready")
    ext.hello_received.set()
    ext.tabs[5] = GhostTarget(target_id="ext-tab-5", tab_id=5, install_id="install-ready")
    relay._extensions[ext.install_id] = ext

    async def complete_request():
        while not sent:
            await asyncio.sleep(0)
        cmd_id = sent[-1]["id"]
        ext.pending[cmd_id].set_result({"tabId": 8, "url": "https://new/", "groupId": "44"})

    task = asyncio.create_task(complete_request())
    gt = await relay.attach_active_tab(group_name="Agent", timeout=1)
    await task
    assert sent[0]["type"] == "attachActive"
    assert sent[0]["groupName"] == "Agent"
    assert sent[0]["groupName"] == "Agent"  # ADR-0009: title, not id
    assert gt.tab_id == 8
    assert gt.group_id == 44
    assert 8 in ext.tabs
    assert ext.pending == {}

    auto_results.append({"groupId": 44, "tabs": []})
    assert await relay.query_group_tabs("Agent", timeout=1) == {"groupId": 44, "tabs": []}

    auto_results.append({"result": {"value": 1}})
    assert await relay.send_cdp(5, "Runtime.evaluate", {"expression": "1"}, timeout=1) == {
        "result": {"value": 1}
    }


def test_ipc_edges_cover_pid_read_write(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _ipc.write_pid(321)
    assert _ipc.read_pid() == 321
    _ipc.pid_path().write_text("-1\n")
    assert _ipc.read_pid() is None
    _ipc.pid_path().write_text("not an int\n")
    assert _ipc.read_pid() is None
