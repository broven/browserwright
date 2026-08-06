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
from typing import Any

import pytest

from browserwright.daemon.config import Config
from browserwright.daemon.errors import Unavailable
from browserwright.daemon.server import listener as listener_mod
from browserwright.daemon.server.proxy import Router
from browserwright.daemon.server.state import DaemonState, UpstreamPhase


class _Cap:
    def __init__(self, relay=None) -> None:
        self.sent: dict[int, list[Any]] = {}
        self.ensure_calls = 0
        self.prepare_calls = 0
        self.relay = relay

    def send_for(self, cid: int):
        self.sent[cid] = []

        async def send(text: str) -> None:
            self.sent[cid].append(json.loads(text))

        return send

    async def ensure_upstream(self) -> None:
        self.ensure_calls += 1

    async def prepare_executor(self, session_id: str) -> None:
        self.prepare_calls += 1
        if self.relay is None or self.relay.is_ready:
            return
        try:
            await self.relay.wait_ready(timeout=0.05)
        except asyncio.TimeoutError:
            pass
        if not self.relay.is_ready:
            raise RuntimeError(
                "no browserwright extension is connected; reload at "
                "chrome://extensions")

    async def trigger_disconnect(self, reason: str) -> None:  # pragma: no cover
        pass


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
        raise asyncio.TimeoutError


class _FakeHolder:
    def __init__(self, relay: _FakeRelay | None) -> None:
        self.relay = relay


class _FakeSharedCtx:
    def __init__(self, holder: _FakeHolder) -> None:
        self.holder = holder


class _FakeRegistry:
    def __init__(self) -> None:
        self.ensure_calls = 0

    async def ensure(self, session_id: str) -> str:
        self.ensure_calls += 1
        return f"/tmp/bw-exec-{session_id}.sock"


class _FakeDaemon:
    def __init__(self, *, relay: _FakeRelay | None, registry: _FakeRegistry) -> None:
        self.shared_context = _FakeSharedCtx(_FakeHolder(relay))
        self.executors = registry


def _build(backend: str, *, relay: _FakeRelay | None,
           phase: UpstreamPhase = UpstreamPhase.DISCONNECTED):
    state = DaemonState(backend_name=backend)
    state.upstream_phase = phase
    router = Router(state)
    cap = _Cap(relay)
    router.bind_lifecycle(
        cap.ensure_upstream, cap.trigger_disconnect, cap.prepare_executor)
    registry = _FakeRegistry()
    router.daemon = _FakeDaemon(relay=relay, registry=registry)
    client = state.allocate_client("agent")
    client.session_id = "246"
    router.register_client(client.client_id, cap.send_for(client.client_id))
    return state, router, cap, client, registry


async def _ensure_executor(router: Router, client) -> dict:
    await router.route_from_client(client, json.dumps({
        "id": 7,
        "method": "BrowserwrightDaemon.ensureExecutor",
        "params": {"bsSession": "246"},
    }))


@pytest.mark.asyncio
async def test_extension_holder_owns_executor_readiness_fastfail(monkeypatch):
    monkeypatch.setattr(listener_mod, "_EXECUTOR_READY_BUDGET_S", 0.05)
    relay = _FakeRelay(ready=False)
    state = DaemonState(backend_name="renamed-extension-wrapper")
    holder = listener_mod._UpstreamHolder(
        state, Router(state), Config(backend="renamed-extension-wrapper"))
    holder.relay = relay

    with pytest.raises(Unavailable, match="chrome://extensions"):
        await holder.prepare_executor("246")

    assert relay.wait_calls == 1
    assert state.upstream_phase == UpstreamPhase.DISCONNECTED


@pytest.mark.asyncio
async def test_extension_no_extension_connected_fast_actionable_error():
    relay = _FakeRelay(ready=False)
    state, router, cap, client, registry = _build("extension", relay=relay)

    await _ensure_executor(router, client)

    reply = cap.sent[client.client_id][-1]
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
    assert cap.prepare_calls == 1
    assert cap.ensure_calls == 0
    assert registry.ensure_calls == 0
    # Upstream state is untouched — a later reconnect can still open it.
    assert state.upstream_phase == UpstreamPhase.DISCONNECTED


@pytest.mark.asyncio
async def test_extension_ready_proceeds_to_spawn():
    relay = _FakeRelay(ready=True)
    state, router, cap, client, registry = _build("extension", relay=relay)

    await _ensure_executor(router, client)

    reply = cap.sent[client.client_id][-1]
    assert reply["id"] == 7
    assert "result" in reply, f"expected a result, got {reply!r}"
    assert reply["result"]["exec_sock"] == "/tmp/bw-exec-246.sock"
    # Relay was already ready → no readiness wait; normal open + spawn ran.
    assert relay.wait_calls == 0
    assert cap.prepare_calls == 1
    assert cap.ensure_calls == 1
    assert registry.ensure_calls == 1


@pytest.mark.asyncio
async def test_cdp_skips_extension_fastfail():
    """The fast-fail is extension-only: an cdp session never consults the relay
    (it has none) and proceeds through the normal upstream-open + spawn path."""
    state, router, cap, client, registry = _build("cdp", relay=None)

    await _ensure_executor(router, client)

    reply = cap.sent[client.client_id][-1]
    assert "result" in reply, f"expected a result, got {reply!r}"
    assert reply["result"]["exec_sock"] == "/tmp/bw-exec-246.sock"
    assert cap.ensure_calls == 1
    assert cap.prepare_calls == 1
    assert registry.ensure_calls == 1
