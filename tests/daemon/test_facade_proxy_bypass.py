"""Regression: the facade bridge must not route the upstream CDP connection
through the *daemon's* ambient web proxy (issues #20, #136).

`websockets` 15.x honors ``http_proxy`` / ``https_proxy`` / ``all_proxy`` by
default. The bridge's only proxy is the one the session pinned (resolved by
the CLI that opened it); the daemon's own environment must never apply —
otherwise a non-loopback upstream (e.g. a CloakBrowser profile reached over
Tailscale) fails the ws handshake with ``InvalidProxyMessage``.

This test points every daemon-side proxy var at a dead port and asserts a
client driven through the facade still reaches the (mock) upstream — which
only holds if the bridge connection ignored that environment.
"""
from __future__ import annotations

import asyncio
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
