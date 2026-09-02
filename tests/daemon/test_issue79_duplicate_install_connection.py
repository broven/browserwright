"""Issue #79 -- one live relay connection per `install_id`, enforced by the daemon.

`chrome-extension/background.js` could dial the relay twice from a single
service worker (see `test_issue79_ws_socket_identity.py`): a superseded socket's
late `onclose` cleared the module-level `ws`, and `maintainLoop` opened a
duplicate one tick later. Both sockets then sent `hello` with the *same*
`install_id`, ~1s apart -- which is the "the extension disconnects and reconnects
between commands" signature in `daemon.log`.

The extension-side fix cannot reach a user until a new build ships to the Chrome
Web Store, so the daemon enforces the same invariant itself: when a `hello`
arrives for an `install_id` that already has a live connection, the *older*
connection is superseded and closed. `self._extensions` is keyed by `install_id`,
so without this the older socket is silently evicted from the table while its
TCP connection stays open forever -- a ghost the daemon still app-pings and can
never route to.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest
import websockets

from browserwright.daemon.server.relay import RelayServer


@asynccontextmanager
async def _relay_running() -> AsyncIterator[RelayServer]:
    relay = RelayServer(port=0)
    await relay.start()
    try:
        yield relay
    finally:
        await relay.stop()


async def _hello(port: int, install_id: str) -> websockets.ClientConnection:
    ws = await websockets.connect(f"ws://127.0.0.1:{port}/", compression=None)
    await ws.send(json.dumps({
        "type": "hello",
        "installId": install_id,
        "browser": "chrome",
        "version": "120.0.0.0",
    }))
    # Wait for helloAck so the relay has finished processing this hello.
    async with asyncio.timeout(3.0):
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == "helloAck":
                return ws


@pytest.mark.asyncio
async def test_second_hello_for_same_install_closes_the_first_connection():
    async with _relay_running() as relay:
        first = await _hello(relay.port, "bd-same")
        second = await _hello(relay.port, "bd-same")
        try:
            await asyncio.wait_for(first.wait_closed(), timeout=3.0)
        except asyncio.TimeoutError:  # pragma: no cover - failure path
            pytest.fail(
                "the superseded connection stayed open: the daemon now has two "
                "live sockets for one install_id and can route to only one")
        # The winner is untouched and still the one the relay will use.
        assert second.state is websockets.protocol.State.OPEN
        snap = relay.status_payload()
        assert snap["install_ids"] == ["bd-same"], snap
        assert snap["extensions"] == 1, snap
        await second.close()


@pytest.mark.asyncio
async def test_a_different_install_is_never_superseded():
    """Two genuinely different extensions must coexist (multi-profile support)."""
    async with _relay_running() as relay:
        a = await _hello(relay.port, "bd-A")
        b = await _hello(relay.port, "bd-B")
        await asyncio.sleep(0.3)
        assert a.state is websockets.protocol.State.OPEN
        assert b.state is websockets.protocol.State.OPEN
        snap = relay.status_payload()
        assert sorted(snap["install_ids"]) == ["bd-A", "bd-B"], snap
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_reconnect_after_a_clean_close_is_not_treated_as_a_duplicate():
    """The ordinary reconnect path must stay a no-op for the supersede logic."""
    async with _relay_running() as relay:
        first = await _hello(relay.port, "bd-same")
        await first.close()
        await asyncio.sleep(0.1)
        second = await _hello(relay.port, "bd-same")
        await asyncio.sleep(0.3)
        assert second.state is websockets.protocol.State.OPEN
        snap = relay.status_payload()
        assert snap["install_ids"] == ["bd-same"], snap
        await second.close()


@pytest.mark.asyncio
async def test_hello_log_distinguishes_a_first_connect_from_a_reconnect(caplog):
    """The diagnostic that issue #79 was misread from.

    `install_id` is persisted in `chrome.storage.local`, so it survives service
    worker restarts: a *new* id means a new browser profile or a reinstall, not
    a churning SW. The daemon must say which it saw, or a session-scoped log
    (one fresh Chrome per e2e test) reads as one extension reconnecting over
    and over with a new identity.
    """
    caplog.set_level("INFO", logger="browserwright.daemon.server.relay")
    async with _relay_running() as relay:
        first = await _hello(relay.port, "bd-logtest")
        await first.close()
        await asyncio.sleep(0.1)
        second = await _hello(relay.port, "bd-logtest")
        await asyncio.sleep(0.1)
        await second.close()

    hellos = [r.getMessage() for r in caplog.records
              if "extension hello" in r.getMessage()]
    assert len(hellos) == 2, hellos
    assert "first connect" in hellos[0], hellos
    assert "reconnect" in hellos[1], hellos


@pytest.mark.asyncio
async def test_a_brand_new_install_id_is_never_logged_as_a_reconnect(caplog):
    caplog.set_level("INFO", logger="browserwright.daemon.server.relay")
    async with _relay_running() as relay:
        a = await _hello(relay.port, "bd-one")
        b = await _hello(relay.port, "bd-two")
        await asyncio.sleep(0.1)
        await a.close()
        await b.close()

    hellos = [r.getMessage() for r in caplog.records
              if "extension hello" in r.getMessage()]
    assert len(hellos) == 2, hellos
    assert all("first connect" in m for m in hellos), hellos
