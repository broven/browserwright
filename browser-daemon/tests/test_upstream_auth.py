"""Mode B upstream-auth integration tests (v0.5).

Verifies that `UpstreamConnection.open(..., additional_headers=..., ssl_context=...)`:
  - Passes `additional_headers=` through to `websockets.connect` as a list
    of (name, value) tuples (the websockets v15 API contract)
  - Passes `ssl=` through when an SSLContext is supplied (mTLS)
  - Default-omits both kwargs when called the v0.1-v0.4 way

Plus an end-to-end with a fake ws server that requires a bearer header to
upgrade — proves the auth path actually reaches the upstream handshake.
"""
from __future__ import annotations

import asyncio
import http
import ssl
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import pytest
import websockets
from websockets.asyncio.server import serve as ws_serve

from browser_daemon.server.upstream import UpstreamConnection


# ---- unit: kwargs passthrough --------------------------------------------


@pytest.mark.asyncio
async def test_open_passes_additional_headers_to_websockets_connect(monkeypatch):
    captured: dict[str, Any] = {}

    class _FakeWs:
        async def send(self, *a, **kw): pass
        async def close(self, *a, **kw): pass
        def __aiter__(self):
            async def gen():
                if False:
                    yield  # pragma: no cover
            return gen()

    async def fake_connect(url, **kw):
        captured["url"] = url
        captured["kw"] = kw
        return _FakeWs()

    monkeypatch.setattr("browser_daemon.server.upstream.websockets.connect",
                        fake_connect)
    conn = UpstreamConnection(
        on_frame=lambda _t: asyncio.sleep(0),
        on_close=lambda _r: asyncio.sleep(0),
    )
    await conn.open(
        "wss://example.com/cdp",
        timeout=2.0,
        additional_headers={"Authorization": "Bearer abc",
                            "X-Trace": "id-42"},
    )
    # The websockets API takes the list-of-tuples form, not a dict.
    hdrs = captured["kw"]["additional_headers"]
    assert ("Authorization", "Bearer abc") in hdrs
    assert ("X-Trace", "id-42") in hdrs


@pytest.mark.asyncio
async def test_open_passes_ssl_context_for_mtls(monkeypatch):
    captured: dict[str, Any] = {}

    class _FakeWs:
        async def send(self, *a, **kw): pass
        async def close(self, *a, **kw): pass
        def __aiter__(self):
            async def gen():
                if False:
                    yield  # pragma: no cover
            return gen()

    async def fake_connect(url, **kw):
        captured["kw"] = kw
        return _FakeWs()

    monkeypatch.setattr("browser_daemon.server.upstream.websockets.connect",
                        fake_connect)
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    conn = UpstreamConnection(
        on_frame=lambda _t: asyncio.sleep(0),
        on_close=lambda _r: asyncio.sleep(0),
    )
    await conn.open("wss://x/", timeout=2.0, ssl_context=ctx)
    assert captured["kw"]["ssl"] is ctx


@pytest.mark.asyncio
async def test_open_default_omits_auth_kwargs(monkeypatch):
    """Backward compat: callers that don't pass auth must not see
    `additional_headers` / `ssl` keys at all (would break older websockets
    builds that don't know them)."""
    captured: dict[str, Any] = {}

    class _FakeWs:
        async def send(self, *a, **kw): pass
        async def close(self, *a, **kw): pass
        def __aiter__(self):
            async def gen():
                if False:
                    yield  # pragma: no cover
            return gen()

    async def fake_connect(url, **kw):
        captured["kw"] = kw
        return _FakeWs()

    monkeypatch.setattr("browser_daemon.server.upstream.websockets.connect",
                        fake_connect)
    conn = UpstreamConnection(
        on_frame=lambda _t: asyncio.sleep(0),
        on_close=lambda _r: asyncio.sleep(0),
    )
    await conn.open("ws://127.0.0.1:9222/", timeout=2.0)
    assert "additional_headers" not in captured["kw"]
    assert "ssl" not in captured["kw"]


# ---- E2E: bearer-required ws server -------------------------------------


@asynccontextmanager
async def _bearer_required_server(expected_token: str) -> AsyncIterator[str]:
    """A minimal ws server that 401s any upgrade without the right bearer
    header. Yields the ws URL clients should connect to.
    """
    received_token = {"value": None}

    def process_request(conn, request) -> Any:
        auth = (request.headers.get("Authorization")
                or request.headers.get("authorization") or "")
        if not auth.startswith("Bearer "):
            return conn.respond(
                http.HTTPStatus.UNAUTHORIZED, "missing bearer\n")
        received_token["value"] = auth[len("Bearer "):]
        if received_token["value"] != expected_token:
            return conn.respond(
                http.HTTPStatus.UNAUTHORIZED, "bad token\n")
        return None  # allow upgrade

    async def handler(ws):
        try:
            async for raw in ws:
                # echo CDP-shaped acks so the daemon's startup probe
                # (Target.setDiscoverTargets) doesn't hang
                import json
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if "id" in msg:
                    await ws.send(json.dumps({"id": msg["id"], "result": {}}))
        except websockets.exceptions.ConnectionClosed:
            pass

    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = await ws_serve(
        handler, "127.0.0.1", port,
        process_request=process_request, compression=None,
    )
    try:
        yield f"ws://127.0.0.1:{port}/"
        # Expose the received token via an attribute for assertions.
        server.received_token = received_token["value"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_upstream_connect_with_bearer_header_succeeds():
    """End-to-end: the daemon's UpstreamConnection successfully completes
    a ws handshake against a server that enforces `Authorization: Bearer`."""
    async with _bearer_required_server("super-secret-99") as ws_url:
        frames: list[str] = []

        async def on_frame(text: str) -> None:
            frames.append(text)

        async def on_close(reason: str) -> None:
            pass

        conn = UpstreamConnection(on_frame=on_frame, on_close=on_close)
        await conn.open(
            ws_url, timeout=3.0,
            additional_headers={"Authorization": "Bearer super-secret-99"},
        )
        # Sanity-roundtrip: send any command, expect a response frame.
        result = await conn.send_command("Browser.getVersion", timeout=2.0)
        assert "result" in result or result == {}
        await conn.close()


@pytest.mark.asyncio
async def test_upstream_connect_with_wrong_bearer_token_raises():
    async with _bearer_required_server("right-token") as ws_url:
        conn = UpstreamConnection(
            on_frame=lambda _t: asyncio.sleep(0),
            on_close=lambda _r: asyncio.sleep(0),
        )
        with pytest.raises(Exception):  # InvalidStatus or similar
            await conn.open(
                ws_url, timeout=3.0,
                additional_headers={"Authorization": "Bearer wrong-token"},
            )


@pytest.mark.asyncio
async def test_upstream_connect_no_header_to_bearer_server_raises():
    async with _bearer_required_server("right") as ws_url:
        conn = UpstreamConnection(
            on_frame=lambda _t: asyncio.sleep(0),
            on_close=lambda _r: asyncio.sleep(0),
        )
        with pytest.raises(Exception):
            await conn.open(ws_url, timeout=3.0)
