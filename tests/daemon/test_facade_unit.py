"""Unit coverage for the Playwright CDP facade (phase A1).

These tests bind the facade on an ephemeral port (no real Chrome) and assert:
  - `/json/version` returns the CDP bootstrap shape with a webSocketDebuggerUrl
    that points back at the facade's own /cdp ws.
  - `/json` / `/json/list` return a single synthetic browser entry.
  - the facade starts/stops cleanly and reports its bound port.

The ws passthrough (which needs a live upstream Chrome) is exercised by the
e2e suite (`tests/daemon/e2e/test_l1_playwright_facade.py`).
"""
from __future__ import annotations

import json
import urllib.request
from types import SimpleNamespace

import pytest

from browserwright.daemon.config import Config
from browserwright.daemon.server.facade import PlaywrightFacade, FACADE_WS_PATH


@pytest.fixture
async def facade():
    f = PlaywrightFacade(cfg=Config(backend="rdp"), port=0)
    port = await f.start()
    assert port > 0
    assert f.port == port
    yield f
    await f.stop()


async def test_json_version_payload(facade):
    port = facade.port
    body = await _get_json(f"http://127.0.0.1:{port}/json/version")
    assert body["Protocol-Version"] == "1.3"
    assert body["Browser"].startswith("Browserwright/")
    assert body["webSocketDebuggerUrl"] == f"ws://127.0.0.1:{port}{FACADE_WS_PATH}"


async def test_json_list_payload(facade):
    port = facade.port
    for path in ("/json", "/json/list"):
        body = await _get_json(f"http://127.0.0.1:{port}{path}")
        assert isinstance(body, list) and len(body) == 1
        assert body[0]["type"] == "browser"
        assert body[0]["webSocketDebuggerUrl"] == (
            f"ws://127.0.0.1:{port}{FACADE_WS_PATH}")


async def test_stop_is_idempotent(facade):
    await facade.stop()
    await facade.stop()  # second stop is a no-op, must not raise


async def test_stop_cleans_up_inflight_session(facade):
    """A client connected (and parked in the handler) must not prevent stop()
    from clearing sessions and closing the listening socket.

    Regression: awaiting a cancelled session task re-raises CancelledError
    (a BaseException), which `suppress(Exception)` would NOT catch — that
    aborted shutdown midway and leaked the socket. We park a handler task in
    _sessions and assert stop() returns cleanly and clears it.
    """
    import asyncio

    parked = asyncio.Event()

    async def _never():
        parked.set()
        await asyncio.sleep(3600)  # block until cancelled by stop()

    task = asyncio.create_task(_never())
    facade._sessions.add(task)
    await parked.wait()

    await facade.stop()  # must not raise despite cancelling a live task

    assert not facade._sessions  # session set cleared
    assert facade.port  # port still reported
    # The listening socket is closed: re-binding the facade succeeds (would
    # raise OSError(EADDRINUSE) if the old server leaked the socket).
    again = PlaywrightFacade(cfg=Config(backend="rdp"), port=0)
    assert await again.start() > 0
    await again.stop()


async def test_session_query_routes_facade_to_session_context(monkeypatch):
    shared_cfg = Config(backend="extension")
    session_cfg = Config(backend="rdp")
    session_cfg.backends.rdp.port = 9444
    holder = SimpleNamespace(_cfg=session_cfg, relay=None)
    ctx = SimpleNamespace(backend="rdp", holder=holder)
    calls = []

    class _Daemon:
        def context_for(self, session_id):
            calls.append(session_id)
            return ctx

    f = PlaywrightFacade(cfg=shared_cfg, port=0, daemon=_Daemon())

    async def fake_resolve(cfg):
        assert cfg.backends.rdp.port == 9444
        return SimpleNamespace(ws_url="ws://127.0.0.1:9444/devtools/browser/session")

    monkeypatch.setattr(
        "browserwright.daemon.server.facade.resolve_upstream", fake_resolve)

    class _Conn:
        request = SimpleNamespace(path="/cdp?session=rdp-session")

    assert await f._resolve_rdp_ws(f._context_for_connection(_Conn())) == (
        "ws://127.0.0.1:9444/devtools/browser/session"
    )
    assert calls == ["rdp-session"]


async def test_facade_without_session_keeps_shared_backend():
    f = PlaywrightFacade(cfg=Config(backend="extension"), port=0)

    class _Conn:
        request = SimpleNamespace(path="/cdp")

    assert f._context_for_connection(_Conn()) is None


async def _get_json(url: str):
    import asyncio

    def _fetch():
        with urllib.request.urlopen(url, timeout=2) as resp:
            assert resp.status == 200
            return json.loads(resp.read().decode("utf-8"))

    return await asyncio.to_thread(_fetch)
