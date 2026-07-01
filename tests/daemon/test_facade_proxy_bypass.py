"""Regression: the facade bridge must not route the upstream CDP connection
through the user's ambient web proxy (issue #20).

`websockets` 15.x honors ``http_proxy`` / ``https_proxy`` / ``all_proxy`` by
default. But the daemon→browser CDP control channel is a direct connection to a
browser the user controls (loopback, LAN, or a Tailscale host), and must bypass
that proxy — otherwise any non-loopback upstream (e.g. a CloakBrowser profile
reached over Tailscale) fails the ws handshake with ``InvalidProxyMessage``.

The loopback-only ``_localhost_bypass_proxy`` shortcut can't cover a
non-loopback upstream, so ``_bridge`` passes ``proxy=None`` to disable proxying
outright (per-page proxying is applied downstream by Chrome itself).

This test simulates a non-loopback upstream by no-op'ing the loopback shortcut,
points a bogus proxy at a dead port, and asserts a client driven through the
facade still reaches the (mock) upstream — which only holds if the facade
disabled the proxy on its bridge connection.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest
import websockets

from browserwright.daemon.config import Config
from browserwright.daemon.server.facade import PlaywrightFacade


async def _mock_browser_cdp(host: str = "127.0.0.1"):
    """A minimal browser-level CDP ws that answers any command with a sentinel
    result, so a transparent bridge round-trips it back to the client."""
    async def handler(conn):
        async for raw in conn:
            msg = json.loads(raw)
            await conn.send(json.dumps({
                "id": msg.get("id"),
                "result": {"product": "MockChrome/99.0"},
            }))
    srv = await websockets.serve(handler, host, 0)
    port = srv.sockets[0].getsockname()[1]
    return srv, f"ws://{host}:{port}/devtools/browser/mock"


@pytest.fixture
def bogus_proxy(monkeypatch):
    """Point every proxy var at a dead port and clear NO_PROXY, so that — absent
    ``proxy=None`` — websockets would try (and fail) to tunnel through it."""
    for var in ("http_proxy", "https_proxy", "all_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://127.0.0.1:1")
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


async def test_bridge_bypasses_ambient_proxy(monkeypatch, bogus_proxy):
    srv, upstream_url = await _mock_browser_cdp()

    # Simulate a non-loopback upstream: the loopback bypass shortcut can't help,
    # so only the facade's proxy=None keeps the bridge connection direct.
    @contextlib.contextmanager
    def _no_bypass(_ws_url):
        yield
    monkeypatch.setattr(
        "browserwright.daemon.server.facade._localhost_bypass_proxy", _no_bypass)

    async def _fake_resolve(_cfg):
        return SimpleNamespace(ws_url=upstream_url)
    monkeypatch.setattr(
        "browserwright.daemon.server.facade.resolve_upstream", _fake_resolve)

    facade = PlaywrightFacade(cfg=Config(backend="env"), port=0)
    await facade.start()
    try:
        # Drive the facade like a raw CDP client (the bridge is transparent).
        # proxy=None on the *client* isolates the assertion to the facade's own
        # upstream connection, not this test client's.
        client = await websockets.connect(
            f"ws://127.0.0.1:{facade.port}/cdp", proxy=None, open_timeout=5)
        try:
            await client.send(json.dumps(
                {"id": 1, "method": "Browser.getVersion"}))
            reply = json.loads(await asyncio.wait_for(client.recv(), 5))
        finally:
            await client.close()
        assert reply["result"]["product"] == "MockChrome/99.0"
    finally:
        await facade.stop()
        srv.close()
        await srv.wait_closed()
