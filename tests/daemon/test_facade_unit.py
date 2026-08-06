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

import asyncio
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


async def test_json_version_payload_preserves_session_query(facade):
    port = facade.port
    body = await _get_json(f"http://127.0.0.1:{port}/json/version?session=rdp%207")
    assert body["webSocketDebuggerUrl"] == (
        f"ws://127.0.0.1:{port}{FACADE_WS_PATH}?session=rdp%207"
    )


async def test_json_list_payload(facade):
    port = facade.port
    for path in ("/json", "/json/list"):
        body = await _get_json(f"http://127.0.0.1:{port}{path}")
        assert isinstance(body, list) and len(body) == 1
        assert body[0]["type"] == "browser"
        assert body[0]["webSocketDebuggerUrl"] == (
            f"ws://127.0.0.1:{port}{FACADE_WS_PATH}")


async def test_json_version_uses_request_host_header(facade):
    # Feature 2: a client that reached us over Tailscale/LAN gets a ws URL that
    # points back at the authority IT used (the Host header), not the bound
    # loopback host. Mirrors CloakBrowser's discovery rewrite.
    port = facade.port
    tailnet = "100.72.20.32:29990"
    body = await _get_json(
        f"http://127.0.0.1:{port}/json/version", host=tailnet)
    assert body["webSocketDebuggerUrl"] == f"ws://{tailnet}{FACADE_WS_PATH}"


async def test_json_list_uses_request_host_header(facade):
    port = facade.port
    tailnet = "100.72.20.32:29990"
    for path in ("/json", "/json/list"):
        body = await _get_json(f"http://127.0.0.1:{port}{path}", host=tailnet)
        assert body[0]["webSocketDebuggerUrl"] == f"ws://{tailnet}{FACADE_WS_PATH}"


async def test_json_version_tolerates_trailing_slash(facade):
    # Playwright's `connect_over_cdp("http://host:port")` probes
    # `/json/version/` (trailing slash) — the bootstrap must still answer it.
    port = facade.port
    body = await _get_json(f"http://127.0.0.1:{port}/json/version/")
    assert body["webSocketDebuggerUrl"] == f"ws://127.0.0.1:{port}{FACADE_WS_PATH}"


def test_ws_url_falls_back_to_configured_host_when_no_authority():
    # No Host header (e.g. a hand-rolled probe) → fall back to the configured
    # bind host:port so the advertised URL is still well-formed.
    f = PlaywrightFacade(cfg=Config(backend="rdp"), port=29990, host="0.0.0.0")
    assert f._ws_url() == f"ws://0.0.0.0:29990{FACADE_WS_PATH}"
    assert f._version_payload()["webSocketDebuggerUrl"] == (
        f"ws://0.0.0.0:29990{FACADE_WS_PATH}")
    # An explicit authority (from the Host header) always wins.
    assert f._ws_url(authority="host.example:1234") == (
        f"ws://host.example:1234{FACADE_WS_PATH}")


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
        def context_for_required(self, session_id):
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


async def test_facade_unknown_session_fails_closed():
    from browserwright.daemon.server.daemon import UnknownSessionError

    class _Daemon:
        def context_for_required(self, session_id):
            raise UnknownSessionError(session_id)

    f = PlaywrightFacade(cfg=Config(backend="extension"), port=0, daemon=_Daemon())

    class _Conn:
        request = SimpleNamespace(path="/cdp?session=missing")
        closed = []

        async def close(self, *, code=1000, reason=""):
            self.closed.append((code, reason))

    conn = _Conn()
    await f._handle_client(conn)
    assert conn.closed == [(1008, "unknown browserwright session")]


@pytest.mark.asyncio
async def test_sessionless_facade_client_is_no_longer_refused(monkeypatch):
    """The `env`-era 1008 refusal is gone (#38).

    A sessionless client used to be closed with "env facade requires
    browserwright session" whenever the shared context was `env`: that context
    had no session identity, and the ledger allowed only one env session per
    daemon, so "whose browser is this?" genuinely had no answer.

    With endpoints carried per session, a sessionless client cannot reach any
    session's browser at all — it gets the operator-configured default port. The
    ambiguity is gone, so the refusal is too.
    """
    daemon = SimpleNamespace(shared_context=SimpleNamespace(backend="rdp"))
    f = PlaywrightFacade(cfg=Config(backend="rdp"), port=0, daemon=daemon)

    handled = []

    async def _fake_rdp(conn, ctx=None):
        handled.append(ctx)

    monkeypatch.setattr(f, "_handle_rdp_client", _fake_rdp)

    class _Conn:
        request = SimpleNamespace(path="/cdp")
        closed = []

        async def close(self, *, code=1000, reason=""):
            self.closed.append((code, reason))

    conn = _Conn()
    await f._handle_client(conn)

    assert conn.closed == []
    assert handled == [None]  # served by the shared context, not refused


@pytest.mark.asyncio
async def test_ending_session_revokes_parked_facade_client(monkeypatch):
    from browserwright.daemon.server.daemon import Daemon, UpstreamContext
    from browserwright.daemon.server.state import DaemonState

    class _Router:
        daemon = None

    class _Registry:
        async def terminate_session(self, session_id, teardown, *, budget=None):
            return {"reaped": True}, await teardown()

    shared = UpstreamContext(
        backend="extension", state=DaemonState("extension"),
        router=_Router(), holder=object())
    daemon = Daemon(
        cfg=Config(), shared_context=shared,
        make_context=lambda **_kw: pytest.fail("should not create"))
    daemon.executors = _Registry()
    monkeypatch.setattr(
        "browserwright.daemon.server.daemon.session_registry.get",
        lambda sid: {"id": sid, "backend": "extension"},
    )
    facade = PlaywrightFacade(
        cfg=Config(backend="extension"), port=0, daemon=daemon)
    parked = asyncio.Event()

    async def park(_conn, _ctx):
        parked.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(facade, "_handle_extension_client", park)

    class _Conn:
        request = SimpleNamespace(path="/cdp?session=session-a")

        def __init__(self):
            self.closed = []

        async def close(self, *, code=1000, reason=""):
            self.closed.append((code, reason))

    conn = _Conn()
    client_task = asyncio.create_task(facade._handle_client(conn))
    await parked.wait()

    async def teardown():
        return {"ok": True, "backend": "extension"}

    _reap, result = await daemon.terminate_session("session-a", teardown)

    assert result["ok"] is True
    assert conn.closed == [(1008, "browserwright session ended")]
    assert client_task.done()
    assert daemon._session_leases.get("session-a") in (None, {})


async def _get_json(url: str, host: str | None = None):
    import asyncio

    def _fetch():
        # An explicit `host` overrides the Host header the facade reads to
        # rewrite the advertised ws URL (simulates a remote/Tailscale client).
        req = urllib.request.Request(url)
        if host is not None:
            req.add_header("Host", host)
        with urllib.request.urlopen(req, timeout=2) as resp:
            assert resp.status == 200
            return json.loads(resp.read().decode("utf-8"))

    return await asyncio.to_thread(_fetch)
