"""Relay reconnect + app-level-keepalive paths — the "does it return, or hang?" net.

These are the relay paths that decide whether an in-flight command **comes back
with something** or **hangs forever**. They were at 0% coverage
(`_retry_request_on_replacement`, both retry-dispatch branches in `_request`,
and `_app_ping_loop` — the only half-open-socket detector), which is why the
user-visible symptom was a random freeze rather than an error.

Layer contract (why these survive the C2/C3 rewrites)
-----------------------------------------------------
Every test here drives a **real** ``RelayServer`` over a **real** websocket with
a fake Chrome extension client speaking the documented wire protocol
(``hello`` / ``response`` / ``ping`` / ``pong``). The only production surface
touched is:

  * ``RelayServer(port=0)`` / ``start()`` / ``stop()`` / ``port`` / ``is_ready``
    / ``wait_ready()``                                     — public lifecycle
  * ``query_group_tabs()`` / ``send_cdp()``                — public command API
  * the extension↔daemon JSON wire protocol itself         — the relay's contract

No private method is called and no private attribute is read. C2/C3 rewrite
``proxy.py`` / ``verbs.py`` / ``listener.py`` / ``extension_upstream.py``; none
of those are imported here, and ``relay.py`` is under a diagnostic freeze.

The three module-level *tuning constants* (``STALE_FRAME_AFTER``,
``RECONNECT_WAIT_TIMEOUT``, ``APP_PING_INTERVAL``) are shrunk with
``monkeypatch.setattr`` so a 35-second production timeout becomes a
sub-second test. They are public module attributes, not behaviour stubs — the
code under test is unmodified, and this is the idiom already used by
``test_extension_upstream.py``. Nothing else is patched.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest
import websockets

from browserwright.daemon.server import relay as relay_mod
from browserwright.daemon.server.relay import RelayServer

# Wall-clock budget for "this must come back with *something*". Generous enough
# for a loaded CI box, tight enough that a genuine hang (35s+ / forever) fails.
HANG_BUDGET_S = 5.0


@asynccontextmanager
async def _relay_running() -> AsyncIterator[RelayServer]:
    relay = RelayServer(port=0)
    await relay.start()
    try:
        yield relay
    finally:
        await relay.stop()


class _FakeExtension:
    """A minimal Chrome extension speaking the relay's documented wire protocol.

    ``auto_pong=False`` models the MV3 failure mode this suite exists for: the
    TCP socket stays ESTABLISHED (Chrome's network process holds it) while the
    service worker is suspended, so **no application frames ever come back**.
    """

    def __init__(self, *, auto_pong: bool = True) -> None:
        self.ws: websockets.ClientConnection | None = None
        self._auto_pong = auto_pong
        self._commands: asyncio.Queue[dict] = asyncio.Queue()
        self._pings: asyncio.Queue[dict] = asyncio.Queue()
        self._reader: asyncio.Task | None = None

    async def connect(self, port: int, *, install_id: str = "ext-A") -> None:
        self.ws = await websockets.connect(
            f"ws://127.0.0.1:{port}/", compression=None)
        await self.ws.send(json.dumps({
            "type": "hello",
            "installId": install_id,
            "browser": "chrome",
            "version": "120.0.0.0",
        }))
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self.ws is not None
        try:
            async for raw in self.ws:
                if not isinstance(raw, (str, bytes)):
                    continue
                text = raw if isinstance(raw, str) else raw.decode()
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "helloAck":
                    continue
                if kind == "ping":
                    await self._pings.put(msg)
                    if self._auto_pong and self.ws is not None:
                        await self.ws.send(json.dumps(
                            {"type": "pong", "ts": msg.get("ts")}))
                    continue
                await self._commands.put(msg)
        except (websockets.exceptions.ConnectionClosed, RuntimeError):
            pass

    async def next_command(self, *, timeout: float = 2.0) -> dict:
        return await asyncio.wait_for(self._commands.get(), timeout=timeout)

    async def next_ping(self, *, timeout: float = 3.0) -> dict:
        return await asyncio.wait_for(self._pings.get(), timeout=timeout)

    async def respond(self, cmd_id: int, *, result: dict) -> None:
        assert self.ws is not None
        await self.ws.send(json.dumps(
            {"type": "response", "id": cmd_id, "result": result}))

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        if self._reader is not None:
            self._reader.cancel()

    async def wait_closed(self, *, timeout: float = 3.0) -> None:
        assert self.ws is not None
        await asyncio.wait_for(self.ws.wait_closed(), timeout=timeout)

    @property
    def close_code(self) -> int | None:
        return None if self.ws is None else self.ws.close_code


# ---- _retry_request_on_replacement: the in-flight-reconnect recovery -------


@pytest.mark.asyncio
async def test_inflight_request_retries_on_the_reconnected_extension():
    """The extension's websocket dies while a command is in flight; a fresh
    service worker reconnects with the same installId; the relay re-issues the
    command on the replacement and the ORIGINAL caller gets its result.

    This is the whole point of `_retry_request_on_replacement` and it had never
    executed under test. Note the retry carries a *fresh* command id — the old
    id belonged to a socket that no longer exists.
    """
    async with _relay_running() as relay:
        ext1 = _FakeExtension()
        await ext1.connect(relay.port, install_id="ext-A")
        await relay.wait_ready(timeout=HANG_BUDGET_S)

        call = asyncio.create_task(relay.query_group_tabs(group_id=5, timeout=HANG_BUDGET_S))
        first = await ext1.next_command()
        assert first["type"] == "queryGroup"

        # Service worker dies mid-command: socket gone, no response ever sent.
        await ext1.close()
        await ext1.wait_closed()

        ext2 = _FakeExtension()
        await ext2.connect(relay.port, install_id="ext-A")
        try:
            retry = await ext2.next_command(timeout=HANG_BUDGET_S)
            assert retry["type"] == "queryGroup"
            assert retry["groupId"] == 5
            assert retry["id"] != first["id"]
            await ext2.respond(retry["id"], result={"groupId": 5, "tabs": []})

            result = await asyncio.wait_for(call, timeout=HANG_BUDGET_S)
            assert result == {"groupId": 5, "tabs": []}
        finally:
            await ext2.close()


@pytest.mark.asyncio
async def test_inflight_request_raises_when_no_extension_ever_reconnects(monkeypatch):
    """Same failure, but nothing reconnects. The caller must get a
    ``ConnectionError`` — the one raise inside `_retry_request_on_replacement`
    that had never executed — and must get it WITHIN the reconnect budget
    rather than hanging.
    """
    monkeypatch.setattr(relay_mod, "RECONNECT_WAIT_TIMEOUT", 0.4)
    async with _relay_running() as relay:
        ext = _FakeExtension()
        await ext.connect(relay.port, install_id="ext-lonely")
        await relay.wait_ready(timeout=HANG_BUDGET_S)

        call = asyncio.create_task(relay.query_group_tabs(group_id=1, timeout=HANG_BUDGET_S))
        assert (await ext.next_command())["type"] == "queryGroup"
        await ext.close()
        await ext.wait_closed()

        started = time.monotonic()
        with pytest.raises(ConnectionError,
                           match="did not reconnect after request failure"):
            await asyncio.wait_for(call, timeout=HANG_BUDGET_S)
        assert time.monotonic() - started < HANG_BUDGET_S


@pytest.mark.asyncio
async def test_extension_without_install_id_still_recovers_via_any_replacement(monkeypatch):
    """An extension that omits ``installId`` (the wire protocol allows it) can
    still be replaced — the relay falls back to "any other ready connection".

    The cost is visible here: with no installId to match on there is no early
    exit, so recovery only happens after the FULL reconnect timeout has elapsed
    (35s in production). Locking the behaviour keeps the fallback from being
    dropped silently; the latency cliff is recorded as a known gap.
    """
    monkeypatch.setattr(relay_mod, "RECONNECT_WAIT_TIMEOUT", 0.3)
    async with _relay_running() as relay:
        ext1 = _FakeExtension()
        await ext1.connect(relay.port, install_id="")
        await relay.wait_ready(timeout=HANG_BUDGET_S)

        call = asyncio.create_task(relay.query_group_tabs(group_id=7, timeout=HANG_BUDGET_S))
        assert (await ext1.next_command())["type"] == "queryGroup"
        await ext1.close()
        await ext1.wait_closed()

        ext2 = _FakeExtension()
        await ext2.connect(relay.port, install_id="")
        try:
            retry = await ext2.next_command(timeout=HANG_BUDGET_S)
            await ext2.respond(retry["id"], result={"groupId": 7, "tabs": []})
            assert await asyncio.wait_for(call, timeout=HANG_BUDGET_S) == {
                "groupId": 7, "tabs": []}
        finally:
            await ext2.close()


@pytest.mark.asyncio
async def test_timed_out_request_on_a_ghost_socket_takes_the_reconnect_path(monkeypatch):
    """The other retry-dispatch branch: the socket is still ESTABLISHED but no
    application frames have arrived for longer than ``STALE_FRAME_AFTER``, so a
    request timeout is treated as a ghost connection (tear down + retry) instead
    of being re-raised as a plain timeout.

    Contrast with ``test_live_extension_request_timeout_does_not_retry`` in
    ``test_extension_upstream.py``: a *live* extension's timeout must NOT retry.
    Together they pin both sides of the branch.
    """
    monkeypatch.setattr(relay_mod, "STALE_FRAME_AFTER", 0.3)
    monkeypatch.setattr(relay_mod, "RECONNECT_WAIT_TIMEOUT", 0.4)
    async with _relay_running() as relay:
        ext = _FakeExtension(auto_pong=False)
        await ext.connect(relay.port, install_id="ext-ghost")
        await relay.wait_ready(timeout=HANG_BUDGET_S)

        started = time.monotonic()
        # 0.6s request timeout > 0.3s staleness threshold, and the connection is
        # fresh at dispatch time, so the *timeout* is what discovers the ghost.
        with pytest.raises(ConnectionError,
                           match="did not reconnect after request failure"):
            await relay.query_group_tabs(group_id=1, timeout=0.6)
        elapsed = time.monotonic() - started
        assert elapsed < HANG_BUDGET_S
        # The ghost socket was torn down rather than left to poison later calls.
        await ext.wait_closed()
        await ext.close()


@pytest.mark.asyncio
async def test_command_on_an_already_stale_socket_raises_fast(monkeypatch):
    """Pre-flight freshness check: when the connection is *already* stale before
    the command is even dispatched, the caller gets a named RuntimeError inside
    the reconnect budget — never an open-ended wait on a dead socket.
    """
    monkeypatch.setattr(relay_mod, "STALE_FRAME_AFTER", 0.05)
    monkeypatch.setattr(relay_mod, "RECONNECT_WAIT_TIMEOUT", 0.4)
    async with _relay_running() as relay:
        ext = _FakeExtension(auto_pong=False)
        await ext.connect(relay.port, install_id="ext-dead")
        await relay.wait_ready(timeout=HANG_BUDGET_S)
        await asyncio.sleep(0.15)  # let it go stale

        started = time.monotonic()
        with pytest.raises(RuntimeError, match="stale"):
            await relay.send_cdp(1, "Page.navigate", {"url": "https://x/"})
        assert time.monotonic() - started < HANG_BUDGET_S
        await ext.close()


# ---- _app_ping_loop: the only half-open-socket detector --------------------


@pytest.mark.asyncio
async def test_relay_sends_app_level_keepalive_pings(monkeypatch):
    """The relay drives an application-level ping. Protocol-level websocket
    PINGs are answered by Chrome's network process even when the MV3 service
    worker is dead, so this app frame is the only liveness signal that actually
    proves the extension is running.
    """
    monkeypatch.setattr(relay_mod, "APP_PING_INTERVAL", 0.05)
    async with _relay_running() as relay:
        ext = _FakeExtension(auto_pong=False)
        await ext.connect(relay.port, install_id="ext-ping")
        await relay.wait_ready(timeout=HANG_BUDGET_S)
        try:
            ping = await ext.next_ping(timeout=HANG_BUDGET_S)
            assert ping["type"] == "ping"
            assert isinstance(ping["ts"], int)
        finally:
            await ext.close()


@pytest.mark.asyncio
async def test_keepalive_loop_force_closes_a_socket_that_stopped_answering(monkeypatch):
    """The half-open detector, end to end: a socket that keeps the TCP
    connection but stops producing application frames is force-closed by the
    relay (ws code 1011) and stops counting as a ready extension.

    Without this the daemon believes an extension is attached forever and every
    subsequent command waits on a corpse — the original "random hang".
    """
    monkeypatch.setattr(relay_mod, "APP_PING_INTERVAL", 0.05)
    monkeypatch.setattr(relay_mod, "STALE_FRAME_AFTER", 0.1)
    async with _relay_running() as relay:
        ext = _FakeExtension(auto_pong=False)
        await ext.connect(relay.port, install_id="ext-halfopen")
        await relay.wait_ready(timeout=HANG_BUDGET_S)

        await ext.wait_closed(timeout=HANG_BUDGET_S)
        assert ext.close_code == 1011

        deadline = time.monotonic() + HANG_BUDGET_S
        while relay.is_ready and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert relay.is_ready is False
        await ext.close()


@pytest.mark.asyncio
async def test_healthy_extension_is_never_force_closed(monkeypatch):
    """Guard rail for the detector above: an extension that answers the app-level
    ping keeps its socket. A half-open detector that also kills healthy sockets
    would be worse than none.
    """
    monkeypatch.setattr(relay_mod, "APP_PING_INTERVAL", 0.05)
    monkeypatch.setattr(relay_mod, "STALE_FRAME_AFTER", 0.3)
    async with _relay_running() as relay:
        ext = _FakeExtension(auto_pong=True)
        await ext.connect(relay.port, install_id="ext-healthy")
        await relay.wait_ready(timeout=HANG_BUDGET_S)
        try:
            # Several ping/pong rounds, well past STALE_FRAME_AFTER in wall time.
            for _ in range(3):
                await ext.next_ping(timeout=HANG_BUDGET_S)
            await asyncio.sleep(0.35)
            assert relay.is_ready is True
            assert ext.close_code is None
        finally:
            await ext.close()


# ---- shutdown must not strand an in-flight caller -------------------------


@pytest.mark.asyncio
async def test_relay_shutdown_unblocks_inflight_callers(monkeypatch):
    """``RelayServer.stop()`` (daemon shutdown) with a command in flight must
    raise in the caller, not leave it awaiting a socket that will never answer.

    NOTE the observed shape: ``stop()`` fails the pending future with
    "relay shutting down", which `_request` classifies as a reconnect-worthy
    ConnectionError, so the caller actually waits out ``RECONNECT_WAIT_TIMEOUT``
    (35s in production) before surfacing "did not reconnect". Shrinking the
    constant here keeps the test fast; the 35s shutdown stall is recorded as a
    known gap rather than asserted as desirable.
    """
    monkeypatch.setattr(relay_mod, "RECONNECT_WAIT_TIMEOUT", 0.4)
    relay = RelayServer(port=0)
    await relay.start()
    ext = _FakeExtension()
    await ext.connect(relay.port, install_id="ext-bye")
    await relay.wait_ready(timeout=HANG_BUDGET_S)

    call = asyncio.create_task(relay.query_group_tabs(group_id=1, timeout=30.0))
    assert (await ext.next_command())["type"] == "queryGroup"

    started = time.monotonic()
    await relay.stop()
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(call, timeout=HANG_BUDGET_S)
    assert time.monotonic() - started < HANG_BUDGET_S
    await ext.close()


@pytest.mark.asyncio
async def test_wait_ready_honours_its_own_deadline():
    """A relay with no extension must fail ``wait_ready`` on schedule. This is
    the budget every caller (doctor probe, ensureExecutor fast-fail) relies on.
    """
    async with _relay_running() as relay:
        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await relay.wait_ready(timeout=0.2)
        elapsed = time.monotonic() - started
        assert 0.15 <= elapsed < HANG_BUDGET_S
        assert relay.is_ready is False
