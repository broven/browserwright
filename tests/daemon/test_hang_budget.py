"""Wall-clock budgets: every failure path must *return*, not hang.

The reported symptom this suite exists for is a random freeze — the agent issues
a command and nothing ever comes back. That failure reaches a test runner as a
``subprocess.TimeoutExpired`` or a stalled CI job, which reads like flakiness
rather than "the daemon deadlocked". Nothing in the suite gave itself a time
budget before this file.

Every test below asserts the same shape:

    *a result or an error, either is fine — but within N seconds.*

Scope note: these lock the paths that are bounded **today**. Paths with no
deadline at all (a client frame forwarded to an upstream that never answers; a
pre-open buffer whose lazy open hangs instead of failing) are deliberately NOT
asserted here — a test that goes red on unmodified `main` is not a net, it is a
bug report. They are written up in the handoff's gap list instead.

Layer contract (why these survive the C2/C3 rewrites)
-----------------------------------------------------
Router paths use only ``Router(state)`` / ``bind_lifecycle`` /
``register_client`` / ``route_from_client`` and public ``DaemonState`` /
``PRE_OPEN_BUFFER_LIMIT``. Relay paths use only ``RelayServer``'s public command
API over a real websocket. No private attribute is assigned and no method under
test is patched.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest

from browserwright.daemon.server.proxy import Router
from browserwright.daemon.server.relay import RelayServer
from browserwright.daemon.server.state import (
    PRE_OPEN_BUFFER_LIMIT, DaemonState, UpstreamPhase,
)

from .test_relay_reconnect_paths import _FakeExtension

#: A failure verdict is bookkeeping plus, at worst, a bounded retry ladder.
#: Anything slower than this is waiting on something it should not be.
BUDGET_S = 5.0


@asynccontextmanager
async def within(budget: float, what: str) -> AsyncIterator[None]:
    """Fail with a hang-shaped message, not a bare timeout."""
    started = time.monotonic()
    yield
    elapsed = time.monotonic() - started
    assert elapsed < budget, (
        f"{what} took {elapsed:.2f}s (budget {budget}s) — this is the shape of "
        f"a hang, not a slow test"
    )


# ---- daemon side: the pre-open buffer ---------------------------------------


def _cold_router(backend: str, ensure_upstream):
    """A Router whose upstream is DISCONNECTED — every client frame gets
    buffered by the pre-open gate instead of forwarded."""
    state = DaemonState(backend_name=backend)
    state.upstream_phase = UpstreamPhase.DISCONNECTED
    router = Router(state)
    replies: list[dict] = []

    async def trigger_disconnect(reason: str) -> None:
        return None

    async def send_to_client(text: str) -> None:
        replies.append(json.loads(text))

    router.bind_lifecycle(ensure_upstream, trigger_disconnect)
    client = state.allocate_client(
        "agent", session_id="bs-session-1", session_name="agent")
    router.register_client(client.client_id, send_to_client)
    return router, client, replies


async def _wait_for_reply(replies: list[dict], *, budget: float = BUDGET_S) -> dict:
    deadline = time.monotonic() + budget
    while not replies and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert replies, (
        f"no reply within {budget}s — the request is buffered forever, which is "
        f"exactly the reported freeze"
    )
    return replies[-1]


@pytest.mark.asyncio
async def test_failed_upstream_open_unblocks_buffered_requests():
    """A client frame that arrives before the upstream is open gets buffered.
    If the lazy open then FAILS, the buffered frame must be answered with an
    error — otherwise the client waits on a reply that can never be produced.

    This is the difference between "Chrome failed to launch" and "the agent
    froze".
    """
    async def ensure_upstream() -> None:
        raise RuntimeError("chrome refused to launch")

    router, client, replies = _cold_router("cdp", ensure_upstream)

    async with within(BUDGET_S, "buffered request after a failed upstream open"):
        await router.route_from_client(client, json.dumps({
            "id": 1, "method": "Page.navigate",
            "params": {"url": "https://example.com/"},
        }))
        reply = await _wait_for_reply(replies)

    assert reply["id"] == 1
    assert "error" in reply
    assert "chrome refused to launch" in reply["error"]["message"]


@pytest.mark.asyncio
async def test_pre_open_buffer_overflow_answers_instead_of_growing():
    """The pre-open buffer is capped. Past the cap the client must be told, so a
    client that keeps writing into a daemon that never opens gets an error
    rather than unbounded silence (and the daemon does not grow without limit).
    """
    async def ensure_upstream() -> None:
        return None  # "opens" without ever becoming CONNECTED

    router, client, replies = _cold_router("cdp", ensure_upstream)

    async with within(BUDGET_S, f"{PRE_OPEN_BUFFER_LIMIT + 1} buffered frames"):
        for i in range(PRE_OPEN_BUFFER_LIMIT):
            await router.route_from_client(client, json.dumps({
                "id": i, "method": "Runtime.evaluate", "params": {},
            }))
        assert replies == [], "frames below the cap must buffer silently"

        await router.route_from_client(client, json.dumps({
            "id": 999, "method": "Runtime.evaluate", "params": {},
        }))

    assert len(replies) == 1
    assert replies[0]["id"] == 999
    assert replies[0]["error"]["code"] == -32603
    assert "buffer overflow" in replies[0]["error"]["message"]


@pytest.mark.asyncio
async def test_sessionless_client_is_refused_immediately_not_buffered():
    """A client that never named a browserwright session must be refused at
    once. Buffering it would be indistinguishable from a hang, because no
    upstream open can ever make an unscoped frame routable.
    """
    async def ensure_upstream() -> None:  # pragma: no cover - must not be called
        raise AssertionError("must not attempt an upstream open")

    state = DaemonState(backend_name="extension")
    state.upstream_phase = UpstreamPhase.DISCONNECTED
    router = Router(state)
    replies: list[dict] = []

    async def trigger_disconnect(reason: str) -> None:
        return None

    async def send_to_client(text: str) -> None:
        replies.append(json.loads(text))

    router.bind_lifecycle(ensure_upstream, trigger_disconnect)
    client = state.allocate_client("anonymous")
    router.register_client(client.client_id, send_to_client)

    async with within(BUDGET_S, "sessionless CDP frame"):
        await router.route_from_client(client, json.dumps({
            "id": 4, "method": "Page.navigate", "params": {},
        }))

    assert replies[-1]["error"]["code"] == -32602
    assert "?session=" in replies[-1]["error"]["message"]


# ---- relay side: bounded refusals -------------------------------------------


@asynccontextmanager
async def _relay() -> AsyncIterator[RelayServer]:
    relay = RelayServer(port=0)
    await relay.start()
    try:
        yield relay
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_relay_commands_refuse_immediately_when_no_extension_is_connected():
    """With no extension attached, every relay command must raise straight away.

    A daemon that instead waited "just in case one connects" would turn a
    trivially diagnosable state (extension not running) into the freeze.
    """
    async with _relay() as relay:
        async with within(BUDGET_S, "relay commands with no extension"):
            with pytest.raises(RuntimeError, match="no extension"):
                await relay.send_cdp(1, "Page.navigate", {})
            with pytest.raises(RuntimeError, match="no extension"):
                await relay.attach_tab(1)
            with pytest.raises(RuntimeError, match="no extension"):
                await relay.attach_active_tab()
            with pytest.raises(RuntimeError, match="no extension"):
                await relay.close_tab(1)
            with pytest.raises(RuntimeError, match="no extension"):
                await relay.userscript_request("list", {})
            with pytest.raises(RuntimeError, match="no extension"):
                await relay.create_background_tab("https://example.com/")
            # Two paths answer without raising: a detach of an unknown tab is a
            # no-op, and a membership query degrades to "can't tell" (None) so
            # the caller can fall back.
            assert await relay.detach_tab(1) is None
            assert await relay.query_group_tabs(group_name="g") is None


@pytest.mark.asyncio
async def test_attach_retry_ladder_terminates():
    """`chrome.debugger` conflicts ("Another debugger is already attached") are
    retried on a fixed 3-step backoff. The ladder must END: the caller gets the
    extension's error, it does not retry forever.
    """
    async with _relay() as relay:
        ext = _FakeExtension()
        await ext.connect(relay.port, install_id="ext-busy")
        await relay.wait_ready(timeout=BUDGET_S)

        attempts = 0

        async def refuse_every_attach() -> None:
            nonlocal attempts
            while True:
                cmd = await ext.next_command(timeout=BUDGET_S)
                attempts += 1
                assert ext.ws is not None
                await ext.ws.send(json.dumps({
                    "type": "response", "id": cmd["id"],
                    "error": {"code": -32000,
                              "message": "Another debugger is already attached"},
                }))

        refuser = asyncio.create_task(refuse_every_attach())
        try:
            async with within(BUDGET_S, "attach retry ladder"):
                with pytest.raises(Exception, match="already attached"):
                    await relay.attach_tab(42, timeout=BUDGET_S)
            assert attempts == 3, (
                f"expected exactly 3 attach attempts, saw {attempts}")
        finally:
            refuser.cancel()
            await ext.close()


@pytest.mark.asyncio
async def test_non_conflict_attach_error_is_not_retried():
    """The other half of the ladder: an error that is NOT an attach conflict
    surfaces on the first try. Retrying every failure would multiply the user's
    wait by three for errors that will never succeed.
    """
    async with _relay() as relay:
        ext = _FakeExtension()
        await ext.connect(relay.port, install_id="ext-gone")
        await relay.wait_ready(timeout=BUDGET_S)

        attempts = 0

        async def refuse_once() -> None:
            nonlocal attempts
            while True:
                cmd = await ext.next_command(timeout=BUDGET_S)
                attempts += 1
                assert ext.ws is not None
                await ext.ws.send(json.dumps({
                    "type": "response", "id": cmd["id"],
                    "error": {"code": -32000, "message": "No tab with given id"},
                }))

        refuser = asyncio.create_task(refuse_once())
        try:
            async with within(BUDGET_S, "non-conflict attach error"):
                with pytest.raises(Exception, match="No tab with given id"):
                    await relay.attach_tab(42, timeout=BUDGET_S)
            assert attempts == 1
        finally:
            refuser.cancel()
            await ext.close()
