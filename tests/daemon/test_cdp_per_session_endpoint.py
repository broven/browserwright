"""How a per-session CDP endpoint reaches the code that dials it (#38).

The endpoint travels in the session's `Config`, not as an attribute on the
holder, and that is load-bearing rather than incidental: `facade._resolve_cdp_ws`
reads `ctx.holder._cfg`, so the Config is the one channel that already reached
both the agent path and the Playwright facade. Anything that moves the endpoint
elsewhere has to teach the facade a second way to find it — which is what the
last test here exists to notice.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from browserwright.daemon.config import Config
from browserwright.daemon.server.daemon import Daemon, _endpoint_from_workspace
from browserwright.daemon.server.state import DaemonState


class _Router:
    daemon = None


def _daemon() -> Daemon:
    shared = _ctx("extension")

    def make_context(*, backend, cfg, session_id=None):
        return _ctx(backend, cfg=cfg, session_id=session_id)

    return Daemon(cfg=Config(backend="extension"), shared_context=shared,
                  make_context=make_context)


def _ctx(backend, *, cfg=None, session_id=None):
    from browserwright.daemon.server.daemon import UpstreamContext

    async def _ensure_open():
        return None

    return UpstreamContext(
        backend=backend, state=DaemonState(backend), router=_Router(),
        holder=SimpleNamespace(_cfg=cfg, ensure_open=_ensure_open),
        session_id=session_id)


# ---- the single ledger reader ----------------------------------------------


@pytest.mark.parametrize(("workspace", "expected"), [
    ({"port": 9222}, (9222, None)),
    ({"url": "ws://a/b"}, (None, "ws://a/b")),
    ({"url": "http://a:1/"}, (None, "http://a:1/")),
])
def test_well_formed_workspaces(workspace, expected):
    assert _endpoint_from_workspace(workspace) == expected


@pytest.mark.parametrize("workspace", [
    None,
    {},
    [],
    "9222",
    {"port": "9222"},          # a string port, e.g. hand-edited
    {"url": 5},
    {"port": True},            # bool is an int subclass — must not read as 1
    {"url": ""},
])
def test_malformed_workspaces_fall_back_to_the_default_port(workspace):
    """Fail *safe*, not fail open.

    The ledger is a JSON file a user can edit. Falling back to the
    operator-configured default port can never reach a browser they did not
    configure; half-parsing a value could.
    """
    assert _endpoint_from_workspace(workspace) == (None, None)


def test_url_wins_when_a_record_somehow_has_both():
    """Not reachable through `session new`, but the reader must still be total."""
    assert _endpoint_from_workspace(
        {"port": 9222, "url": "ws://a/b"}) == (None, "ws://a/b")


# ---- Config plumbing --------------------------------------------------------


def test_port_record_pins_the_port_only():
    cfg = _daemon()._cdp_cfg_for({"workspace": {"port": 9444}})

    assert cfg.backend == "cdp"
    assert cfg.backends.cdp.port == 9444
    assert cfg.backends.cdp.endpoint is None


def test_url_record_pins_the_endpoint():
    cfg = _daemon()._cdp_cfg_for({"workspace": {"url": "wss://cloud/x?t=1"}})

    assert cfg.backends.cdp.endpoint == "wss://cloud/x?t=1"


def test_per_session_pinning_never_mutates_the_shared_config():
    """The nested `dataclasses.replace` chain exists for exactly this.

    `replace` shares the nested BackendsConfig, so pinning in place would leak
    one session's browser into every other session's config.
    """
    daemon = _daemon()

    daemon._cdp_cfg_for({"workspace": {"port": 9444}})
    daemon._cdp_cfg_for({"workspace": {"url": "ws://cloud/x"}})

    assert daemon.cfg.backends.cdp.port == 9222
    assert daemon.cfg.backends.cdp.endpoint is None


def test_two_sessions_do_not_cross_talk():
    daemon = _daemon()

    a = daemon._cdp_cfg_for({"workspace": {"url": "ws://a.example/cdp"}})
    b = daemon._cdp_cfg_for({"workspace": {"url": "ws://b.example/cdp"}})

    assert a.backends.cdp.endpoint == "ws://a.example/cdp"
    assert b.backends.cdp.endpoint == "ws://b.example/cdp"
    assert a.backends.cdp is not b.backends.cdp


def test_workspaceless_record_uses_the_daemon_default_port():
    """Still legal, and the e2e harness relies on it."""
    cfg = _daemon()._cdp_cfg_for({"workspace": None})

    assert cfg.backends.cdp.port == 9222
    assert cfg.backends.cdp.endpoint is None


# ---- the facade actually reaches the endpoint ------------------------------


@pytest.mark.asyncio
async def test_facade_bridges_a_client_to_the_sessions_own_endpoint(
    mock_browser_cdp,
):
    """The proof that "the endpoint lives in the Config" is sufficient.

    A Playwright client on the facade never sees the ledger; it gets here only
    because `_resolve_cdp_ws` reads the holder's Config. This drives a real
    frame through a real websocket to a per-session endpoint and back.
    """
    import websockets

    from browserwright.daemon.server.facade import PlaywrightFacade

    upstream_url = await mock_browser_cdp()
    daemon = _daemon()
    ctx = _ctx("cdp",
               cfg=daemon._cdp_cfg_for({"workspace": {"url": upstream_url}}))

    # A daemon stub that resolves any session to that context — the ledger
    # lookup itself is covered elsewhere; what is under test is whether the
    # endpoint survives the trip from the Config into a live socket.
    facade = PlaywrightFacade(
        cfg=Config(backend="cdp"), port=0,
        daemon=SimpleNamespace(context_for_required=lambda sid: ctx))
    port = await facade.start()
    try:
        assert await facade._resolve_cdp_ws(ctx) == upstream_url

        async with websockets.connect(
            f"ws://127.0.0.1:{port}/cdp?session=s1"
        ) as client:
            await client.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
            reply = json.loads(await client.recv())

        assert reply["result"]["product"] == "MockChrome/99.0"
    finally:
        await facade.stop()
