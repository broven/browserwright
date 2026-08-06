"""Shared fixtures.

We isolate every test from the developer's real environment:
- proxy env vars are cleared
- BD_* / BU_* env vars are cleared
- HOME is **not** redirected (the platforms.profile_paths() table reads $HOME)
  — individual tests that touch the profile scan use the `tmp_home` fixture.
"""
from __future__ import annotations


import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Pop everything daemon-relevant. Some tests re-set what they need."""
    for var in [
        "BD_BACKEND", "BD_CONFIG", "BD_NAME",
        "BD_TIMEOUT", "BD_CHROME_BINARY", "BD_SESSION_IDLE_PRUNE",
        # Retired (#38) but still popped: a stale value in a developer's shell
        # must not make the "these are inert now" tests pass for the wrong
        # reason, nor fail for one.
        "BD_CDP_WS", "BD_CDP_URL", "BU_CDP_WS", "BU_CDP_URL",
        # Port knobs. `BD_RDP_PORT` was never in this list, so a developer with
        # it exported saw different config results than CI did — the kind of
        # gap that only shows up as "works on my machine".
        "BD_CDP_PORT", "BD_PORT", "BD_PORT_QUIET",
        "BD_EXTENSION_PORT", "BD_FACADE_PORT", "BD_FACADE_HOST",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    ]:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
async def mock_browser_cdp():
    """A minimal browser-level CDP ws that answers any command with a sentinel.

    The repo's only real websocket mock. It was private to
    `test_facade_proxy_bypass.py`; per-session endpoints need it too, since the
    point of an endpoint is that something answers at the other end.

    Yields a factory: `url = await mock_browser_cdp()`, servers closed on exit.
    """
    import json

    import websockets

    servers = []

    async def _make(host: str = "127.0.0.1") -> str:
        async def handler(conn):
            async for raw in conn:
                msg = json.loads(raw)
                await conn.send(json.dumps({
                    "id": msg.get("id"),
                    "result": {"product": "MockChrome/99.0"},
                }))
        srv = await websockets.serve(handler, host, 0)
        servers.append(srv)
        port = srv.sockets[0].getsockname()[1]
        return f"ws://{host}:{port}/devtools/browser/mock"

    yield _make
    for srv in servers:
        srv.close()
        await srv.wait_closed()


@pytest.fixture
def tmp_home(monkeypatch, tmp_path):
    """Redirect Path.home() to a temp dir. Used by tests that scan profiles."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() reads HOME on POSIX, USERPROFILE on Windows.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path
