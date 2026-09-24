"""ADR-0011: the daemon serves one TCP endpoint with three ws sub-surfaces.

These drive a REAL endpoint server on an ephemeral port — no browser, no
executor subprocess, but real websockets, real HTTP and real path dispatch.
That matters here more than usual: the whole change is about what a socket on
the wire does, and a mocked dispatcher would pass no matter which path it
served.
"""
from __future__ import annotations

import asyncio
import json
import struct

import pytest
import websockets

from browserwright.daemon import _ipc
from browserwright.daemon.config import Config
from browserwright.daemon.server.facade import (
    CONTROL_PATH,
    EXEC_PATH,
    PING_PATH,
    PlaywrightFacade,
)

_LEN = struct.Struct(">I")


class _FakeExecutorServer:
    """A stand-in executor: one length-prefixed request in, one response out.

    Speaks the executor's real wire format, because the relay's whole job is to
    translate between that framing and websocket messages — a fake that spoke
    JSON directly would test nothing.
    """

    def __init__(self):
        self.received: list[dict] = []
        self.reply: dict = {"console": "ok\n"}
        self._server = None
        self.path = None

    async def start(self, tmp_path):
        # NOT pytest's tmp_path: AF_UNIX sun_path is 104 bytes on macOS and
        # `/private/var/folders/...` blows it, same reason `_ipc.runtime_dir`
        # hardcodes /tmp.
        self.path = str(tmp_path / "e.sock")
        self._server = await asyncio.start_unix_server(self._handle, self.path)
        return self.path

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            while True:
                header = await reader.readexactly(4)
                (length,) = _LEN.unpack(header)
                payload = await reader.readexactly(length)
                self.received.append(json.loads(payload))
                body = json.dumps(self.reply).encode()
                writer.write(_LEN.pack(len(body)) + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return


class _FakeRegistry:
    def __init__(self, sock_path: str | None):
        self._sock = sock_path
        self.ensured: list[str] = []

    async def ensure(self, session_id: str) -> str:
        self.ensured.append(session_id)
        if self._sock is None:
            raise RuntimeError("no executor for you")
        return self._sock


class _FakeDaemon:
    """The daemon's side of the relay: its one drivable path (here, just the
    registry's ensure) and its recovery state machine."""

    executors: _FakeRegistry | None = None
    recovery = None

    async def ensure_executor(self, session_id: str) -> str:
        if self.executors is None:
            raise RuntimeError("daemon has no executor registry")
        return await self.executors.ensure(session_id)


@pytest.fixture
def short_tmp(tmp_path_factory):
    """A short-path dir for AF_UNIX sockets (see `_FakeExecutorServer.start`)."""
    import pathlib
    import shutil
    import tempfile

    path = pathlib.Path(tempfile.mkdtemp(prefix="bw-adr11-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def endpoint():
    """A bound endpoint, plus hooks to observe what reached each surface."""
    seen_control: list[str] = []

    async def control_handler(conn):
        seen_control.append(conn.request.path)
        async for raw in conn:
            await conn.send(json.dumps({"echo": json.loads(raw)}))

    daemon = _FakeDaemon()
    server = PlaywrightFacade(cfg=Config(), port=0, host="127.0.0.1",
                              daemon=daemon, control_handler=control_handler)
    port = await server.start()
    server.seen_control = seen_control
    server.fake_daemon = daemon
    server.base = f"127.0.0.1:{port}"
    try:
        yield server
    finally:
        await server.stop()


async def _http_get(base: str, path: str) -> tuple[int, bytes]:
    host, port = base.split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: {base}\r\n"
                     f"Connection: close\r\n\r\n".encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(65536), timeout=5.0)
    finally:
        writer.close()
    status = int(data.split(b" ", 2)[1])
    body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
    return status, body


# ---- HTTP surfaces ---------------------------------------------------------


async def test_ping_answers_the_pong_that_replaced_the_socket_file(endpoint):
    status, body = await _http_get(endpoint.base, PING_PATH)
    assert status == 200
    pong = _ipc.parse_pong(body)
    assert pong.pid is not None
    assert pong.version


async def test_cdp_bootstrap_routes_still_answer(endpoint):
    status, body = await _http_get(endpoint.base, "/json/version")
    assert status == 200
    payload = json.loads(body)
    # The advertised ws points back at the authority the client used, on /cdp.
    assert payload["webSocketDebuggerUrl"] == f"ws://{endpoint.base}/cdp"


async def test_unknown_path_is_a_4xx_not_a_ws_upgrade(endpoint):
    status, body = await _http_get(endpoint.base, "/wat")
    assert status == 404
    assert b"/control" in body  # the error names the surfaces that do exist


# ---- Origin validation on every ws surface ---------------------------------


@pytest.mark.parametrize("path", ["/cdp", "/control", "/exec"])
async def test_browser_origin_is_refused_on_every_ws_surface(endpoint, path):
    """The endpoint grants arbitrary code execution and has no auth by design
    (ADR-0011), so a page-originated upgrade must never be admitted."""
    with pytest.raises(websockets.exceptions.InvalidStatus) as ei:
        async with websockets.connect(
            f"ws://{endpoint.base}{path}",
            additional_headers={"Origin": "https://evil.example"},
        ):
            pass
    assert ei.value.response.status_code == 403


@pytest.mark.parametrize("path", ["/cdp", "/control", "/exec"])
async def test_no_origin_is_admitted(endpoint, path):
    """The CLI, the skill client and Playwright all send no Origin."""
    try:
        async with websockets.connect(f"ws://{endpoint.base}{path}?session=x"):
            pass
    except websockets.exceptions.InvalidStatus as e:  # pragma: no cover
        pytest.fail(f"{path} refused a headless client: {e}")
    except websockets.exceptions.ConnectionClosed:
        # /exec and /cdp close after the upgrade when they cannot resolve a
        # session — that is a *post*-upgrade decision, which is the point.
        pass


# ---- control surface -------------------------------------------------------


async def test_control_surface_reaches_the_client_handler_with_its_query(endpoint):
    url = f"ws://{endpoint.base}{CONTROL_PATH}?session=s-7&client=cli"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"id": 1}))
        assert json.loads(await ws.recv()) == {"echo": {"id": 1}}
    # The `?session=`/`?client=` query maps 1:1 onto the old unix query — the
    # daemon's dispatcher routes on it, so losing it would silently land every
    # session on the shared context.
    assert endpoint.seen_control == [f"{CONTROL_PATH}?session=s-7&client=cli"]


async def test_cdp_surface_is_not_the_control_surface(endpoint):
    """Two protocols share the port; only the path separates them."""
    async with websockets.connect(f"ws://{endpoint.base}/cdp") as ws:
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), timeout=5.0)
    assert endpoint.seen_control == []


# ---- exec relay ------------------------------------------------------------


async def test_exec_relay_round_trip(endpoint, short_tmp):
    executor = _FakeExecutorServer()
    sock = await executor.start(short_tmp)
    endpoint.fake_daemon.executors = _FakeRegistry(sock)
    try:
        url = f"ws://{endpoint.base}{EXEC_PATH}?session=s-1"
        async with websockets.connect(url, max_size=None) as ws:
            await ws.send(json.dumps({"code": "page.title()"}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert reply == {"console": "ok\n"}
        assert executor.received == [{"code": "page.title()"}]
        assert endpoint.fake_daemon.executors.ensured == ["s-1"]
    finally:
        await executor.stop()


async def test_exec_relay_carries_a_frame_above_the_old_100mib_ceiling(
        endpoint, short_tmp):
    """The endpoint's ws `max_size` must clear the executor's own frame cap.

    The pre-ADR-0011 server allowed 100 MiB, which was fine while executor
    frames never crossed it; now they do, and a legal response above that would
    be dropped as oversized.
    """
    executor = _FakeExecutorServer()
    big = "x" * (110 * 1024 * 1024)
    executor.reply = {"console": big}
    sock = await executor.start(short_tmp)
    endpoint.fake_daemon.executors = _FakeRegistry(sock)
    try:
        url = f"ws://{endpoint.base}{EXEC_PATH}?session=s-big"
        async with websockets.connect(url, max_size=None) as ws:
            await ws.send(json.dumps({"code": "screenshot()"}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=60.0))
        assert reply["console"] == big
    finally:
        await executor.stop()


async def test_exec_relay_without_a_session_closes_with_a_reason(endpoint):
    async with websockets.connect(f"ws://{endpoint.base}{EXEC_PATH}") as ws:
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), timeout=5.0)
        assert "requires ?session=" in (ws.close_reason or "")


async def test_exec_relay_surfaces_an_unavailable_executor(endpoint):
    endpoint.fake_daemon.executors = _FakeRegistry(None)
    url = f"ws://{endpoint.base}{EXEC_PATH}?session=s-2"
    async with websockets.connect(url) as ws:
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), timeout=5.0)
        assert "could not ensure executor" in (ws.close_reason or "")


async def test_a_dead_daemon_severs_the_exec_plane(endpoint, short_tmp):
    """ADR-0011's accepted consequence: the daemon owns both ends of the relay,
    so its death takes the data plane with it (it used to survive a restart).
    The client must see a close, not a hang."""
    executor = _FakeExecutorServer()
    sock = await executor.start(short_tmp)
    endpoint.fake_daemon.executors = _FakeRegistry(sock)
    try:
        url = f"ws://{endpoint.base}{EXEC_PATH}?session=s-3"
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"code": "x"}))
            await asyncio.wait_for(ws.recv(), timeout=5.0)
            await endpoint.stop()
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=5.0)
    finally:
        await executor.stop()
