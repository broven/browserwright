"""The executor client speaks to the daemon's `/exec` relay (ADR-0011).

`tests/daemon/test_adr0011_endpoint.py` proves the daemon's half. This proves
the client's half against it — the synchronous `run_on_executor` path an agent
heredoc actually takes, over a real endpoint, with a real executor-protocol
peer at the far end. What is NOT covered here is the browser: the fake executor
answers the wire, not Playwright.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import struct
import tempfile
import threading
from pathlib import Path

import pytest

from browserwright._executor import client as exec_client
from browserwright._executor import protocol
from browserwright.daemon.config import Config
from browserwright.daemon.server.facade import PlaywrightFacade

_LEN = struct.Struct(">I")


class _Registry:
    def __init__(self, sock_path):
        self._sock = sock_path

    async def ensure(self, session_id):
        return self._sock


class _Harness:
    """An endpoint + a fake executor, both on a background event loop.

    The client under test is synchronous (it runs on the executor's worker
    thread in production, where no loop exists), so the server side has to live
    somewhere else — hence the thread.
    """

    def __init__(self):
        # Short base dir: AF_UNIX sun_path is 104 bytes on macOS.
        self.dir = Path(tempfile.mkdtemp(prefix="bw-adr11c-", dir="/tmp"))
        self.sock = str(self.dir / "e.sock")
        self.reply = protocol.ExecuteResponse(console="hello\n").to_dict()
        self.received = []
        self.port = None
        self._stopped = False
        self._loop = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        assert self._ready.wait(timeout=10), "harness never came up"
        return self

    def stop_endpoint(self):
        """Kill the daemon half only — the severing ADR-0011 accepts."""
        if self._stopped:
            return
        self._stopped = True
        asyncio.run_coroutine_threadsafe(
            self._endpoint.stop(), self._loop).result(timeout=10)

    def close(self):
        # Stop the endpoint before the loop, so its in-flight relay tasks are
        # cancelled by their owner rather than orphaned by a dead loop.
        self.stop_endpoint()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._setup())
        self._ready.set()
        self._loop.run_forever()

    async def _setup(self):
        await asyncio.start_unix_server(self._executor, self.sock)
        daemon = type("_D", (), {"executors": _Registry(self.sock)})()
        self._endpoint = PlaywrightFacade(
            cfg=Config(), port=0, host="127.0.0.1", daemon=daemon)
        self.port = await self._endpoint.start()

    async def _executor(self, reader, writer):
        try:
            while True:
                (length,) = _LEN.unpack(await reader.readexactly(4))
                self.received.append(json.loads(await reader.readexactly(length)))
                body = json.dumps(self.reply).encode()
                writer.write(_LEN.pack(len(body)) + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return


class _Sess:
    """A session whose control plane answers `ensureExecutor` in the new shape."""

    def __init__(self, sid="s-1"):
        self.session_record = {"id": sid}
        self.calls = []
        outer = self

        class _CDP:
            def send(self, method, **params):
                outer.calls.append((method, params))
                return {"ready": True, "executor_id": "exec-1"}

        self.cdp = _CDP()


@pytest.fixture
def harness(monkeypatch):
    h = _Harness().start()
    monkeypatch.setenv("BW_DAEMON_URL", f"http://127.0.0.1:{h.port}")
    monkeypatch.setattr(exec_client.reg, "touch", lambda sid: None)
    try:
        yield h
    finally:
        h.close()


def test_run_on_executor_round_trips_through_the_relay(harness):
    sess = _Sess()
    response = exec_client.run_on_executor(sess, "print('hi')", timeout_ms=5000)

    assert response.console == "hello\n"
    # Control plane first, over `/control`; then the data plane over `/exec`.
    assert harness.received[0]["code"] == "print('hi')"
    # The exact-instance identity still rides along, because reap targeting
    # still needs it — that is the half of the old lease that survived.
    assert harness.received[0]["executor_id"] == "exec-1"
    assert sess.calls[0][0] == "BrowserwrightDaemon.ensureExecutor"


def test_a_severed_relay_surfaces_as_executor_unavailable(harness):
    """A daemon restart now takes live data planes with it. The client must say
    so in words an agent can act on, not leak a websockets error."""
    sess = _Sess()
    harness.stop_endpoint()

    with pytest.raises(exec_client.ExecutorUnavailable) as ei:
        exec_client.run_on_executor(sess, "print('hi')", timeout_ms=5000)
    msg = str(ei.value)
    assert "/exec" in msg
    assert f"127.0.0.1:{harness.port}" in msg


def test_ensure_executor_without_readiness_is_refused(harness):
    """A daemon still answering the OLD `{exec_sock: ...}` shape is a hard
    error, not a silent fallback: the hard cut means its socket path is a lie
    from anywhere but that machine."""
    sess = _Sess()

    class _OldCDP:
        def send(self, method, **params):
            return {"exec_sock": "/tmp/bw-exec-x.sock", "executor_id": "e"}

    sess.cdp = _OldCDP()
    with pytest.raises(exec_client.ExecutorUnavailable, match="readiness"):
        exec_client.run_on_executor(sess, "print('hi')")
