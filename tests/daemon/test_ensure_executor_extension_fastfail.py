"""GH #18: `BrowserwrightDaemon.ensureExecutor` on an extension session with no
extension connected must fail FAST with an actionable error.

Before the fix, the handler awaited the full ~60s extension-open grace, which
outlived the client's CDP reply deadline (cdp.py ~30s) AND a stray socket
read-timeout (~8s) — so the agent saw `ws closed: no close frame received or
sent` instead of the real cause. The handler now does a short, bounded relay
readiness wait and, if still no extension, returns the actionable message
WITHOUT touching the upstream state machine (a later reconnect still works).
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from browserwright import session_registry
from browserwright.daemon.config import Config
from browserwright.daemon.errors import Unavailable
from browserwright.daemon.server import executor_registry as registry_mod
from browserwright.daemon.server import extension_upstream as ext_mod
from browserwright.daemon.server.daemon import Daemon
from browserwright.daemon.server.extension_upstream import ExtensionUpstream
from browserwright.daemon.server.relay import RelayServer
from browserwright.daemon.server.state import UpstreamPhase
from browserwright.daemon.server.upstream import CdpUpstream
from browserwright.daemon.server.upstream_context import build_context


class _FakeRelay:
    def __init__(self, *, ready: bool) -> None:
        self._ready = ready
        self.wait_calls = 0

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def wait_ready(self, timeout: float = 30.0) -> None:
        self.wait_calls += 1
        # No extension will ever connect in this test → behave like the real
        # relay and time out (the handler catches it and re-checks is_ready).
        if not self._ready:
            raise asyncio.TimeoutError

    def set_event_handler(self, handler) -> None:
        return None


def _build(monkeypatch, tmp_path, backend: str, *, relay: _FakeRelay | None):
    """A real daemon — context factory, adapters, drivable path, executor
    registry — whose browser side (the relay, a cdp Chrome) and executor
    subprocess are the only stand-ins."""
    monkeypatch.setattr(ext_mod, "_EXECUTOR_READY_BUDGET_S", 0.05)
    monkeypatch.setenv("BS_HOME", str(tmp_path))
    sid = session_registry.allocate(backend=backend, owner="attach", name="t")
    if relay is not None:
        monkeypatch.setattr(RelayServer, "is_ready",
                            property(lambda _self: relay.is_ready))
        monkeypatch.setattr(RelayServer, "wait_ready",
                            lambda _self, timeout=30.0: relay.wait_ready(timeout))
    opens: list[str] = []

    async def _cdp_open(self, ws_url=None, *, timeout=None):
        opens.append("cdp")

    monkeypatch.setattr(CdpUpstream, "open", _cdp_open)
    cfg = Config()
    daemon = Daemon(cfg=cfg, shared_context=build_context(
        backend="extension", cfg=cfg))
    spawned: list[str] = []

    async def _spawn(session_id):
        spawned.append(session_id)
        return registry_mod.ExecutorHandle(
            session_id=session_id, proc=None,
            sock_path=f"/tmp/bw-exec-{session_id}.sock", pid=os.getpid())

    monkeypatch.setattr(daemon.executors, "_spawn", _spawn)
    ctx = daemon.context_for_required(sid)
    sent: list[dict] = []

    async def send(text: str) -> None:
        sent.append(json.loads(text))

    client = ctx.state.allocate_client("agent")
    client.session_id = sid
    ctx.router.register_client(client.client_id, send)
    return ctx, client, sent, spawned, opens


async def _ensure_executor(ctx, client) -> dict:
    await ctx.router.route_from_client(client, json.dumps({
        "id": 7,
        "method": "BrowserwrightDaemon.ensureExecutor",
        "params": {},
    }))


@pytest.mark.asyncio
async def test_extension_adapter_owns_executor_readiness_fastfail(monkeypatch):
    monkeypatch.setattr(ext_mod, "_EXECUTOR_READY_BUDGET_S", 0.05)
    relay = _FakeRelay(ready=False)

    async def _noop(_value: str) -> None:
        return None

    adapter = ExtensionUpstream(relay, _noop, _noop)

    with pytest.raises(Unavailable, match="chrome://extensions"):
        await adapter.await_browser("246")

    assert relay.wait_calls == 1
    # The probe never opens the adapter: a later reconnect stays recoverable.
    assert adapter.is_open is False


@pytest.mark.asyncio
async def test_extension_no_extension_connected_fast_actionable_error(
        monkeypatch, tmp_path):
    relay = _FakeRelay(ready=False)
    ctx, client, sent, spawned, _ = _build(
        monkeypatch, tmp_path, "extension", relay=relay)

    await _ensure_executor(ctx, client)

    reply = sent[-1]
    assert reply["id"] == 7
    assert "error" in reply, f"expected an error, got {reply!r}"
    msg = reply["error"]["message"]
    # Actionable: names the extension + the concrete recovery action.
    assert "no browserwright extension is connected" in msg
    assert "chrome://extensions" in msg
    # It must NOT be the confusing ws-closed symptom.
    assert "ws closed" not in msg
    # Fast-fail path: we gave the relay a chance, then bailed WITHOUT opening
    # the upstream (no state-machine mutation) and WITHOUT spawning an executor.
    assert relay.wait_calls == 1
    assert spawned == []
    # Upstream state is untouched — a later reconnect can still open it.
    assert ctx.state.upstream_phase == UpstreamPhase.DISCONNECTED


@pytest.mark.asyncio
async def test_extension_ready_proceeds_to_spawn(monkeypatch, tmp_path):
    relay = _FakeRelay(ready=True)
    ctx, client, sent, spawned, _ = _build(
        monkeypatch, tmp_path, "extension", relay=relay)
    ext = ctx.upstream
    converged: list[str] = []

    async def _recover_session(sid):
        converged.append(sid)
        return {"targetId": "ext-tab-1"}

    ext.recover_session = _recover_session

    await _ensure_executor(ctx, client)

    reply = sent[-1]
    assert reply["id"] == 7
    assert "result" in reply, f"expected a result, got {reply!r}"
    assert reply["result"]["ready"] is True
    # Relay was already ready → no readiness grace; the open's own wait
    # returns at once, then converge + spawn ran.
    assert relay.wait_calls == 1
    assert ctx.state.upstream_phase == UpstreamPhase.CONNECTED
    assert converged == [client.session_id]
    assert spawned == [client.session_id]


@pytest.mark.asyncio
async def test_cdp_skips_extension_fastfail(monkeypatch, tmp_path):
    """The fast-fail is extension-only: an cdp session never consults the relay
    (it has none) and proceeds through the normal upstream-open + spawn path."""
    ctx, client, sent, spawned, opens = _build(
        monkeypatch, tmp_path, "cdp", relay=None)

    await _ensure_executor(ctx, client)

    reply = sent[-1]
    assert "result" in reply, f"expected a result, got {reply!r}"
    assert reply["result"]["ready"] is True
    assert opens == ["cdp"]
    assert spawned == [client.session_id]
