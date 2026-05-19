"""v0.4 extension relay tests.

These exercise `server/relay.py` in isolation: a tiny mock-extension ws
client connects to the relay, performs the hello handshake, and drives the
ghost-target / attach / sendCDP / event paths the real Chrome extension
will follow.

The real Chrome extension lives in `chrome-extension/` and is exercised only
via this mock — installing it in a real Chrome would re-trigger the popup
storm we're explicitly avoiding (see RTK.md + the CLAUDE.md "Chrome popup
test policy" note).
"""
from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import pytest
import websockets

from browser_daemon.server.relay import (
    ATTACH_RETRY_LIMIT, RelayServer, _CommandError,
)


# ---- mock-extension scaffolding ------------------------------------------


@asynccontextmanager
async def _relay_running() -> AsyncIterator[RelayServer]:
    """Start a relay on an ephemeral port for the duration of the test."""
    relay = RelayServer(port=0)
    await relay.start()
    try:
        yield relay
    finally:
        await relay.stop()


class _MockExtension:
    """Drives the extension side of the protocol from the test process.

    Stateful: tracks pending command ids the relay sent us, exposes coros
    to respond to them. Each test wires up the responder it wants.
    """

    def __init__(self):
        self.ws: websockets.ClientConnection | None = None
        self.received: list[dict] = []
        self.recv_task: asyncio.Task | None = None
        self._inbox: asyncio.Queue[dict] = asyncio.Queue()

    async def connect(self, port: int, *, install_id: str = "test-ext-1") -> None:
        self.ws = await websockets.connect(
            f"ws://127.0.0.1:{port}/", compression=None)
        # Send hello so the relay considers us ready.
        await self.ws.send(json.dumps({
            "type": "hello",
            "installId": install_id,
            "browser": "chrome",
            "version": "120.0.0.0",
        }))
        self.recv_task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        assert self.ws is not None
        try:
            async for raw in self.ws:
                if isinstance(raw, (str, bytes)):
                    text = raw if isinstance(raw, str) else raw.decode()
                    msg = json.loads(text)
                    self.received.append(msg)
                    await self._inbox.put(msg)
        except websockets.exceptions.ConnectionClosed:
            pass

    async def next_command(self, *, timeout: float = 2.0) -> dict:
        return await asyncio.wait_for(self._inbox.get(), timeout=timeout)

    async def respond(self, cmd_id: int, *, result: dict | None = None,
                      error: dict | None = None) -> None:
        assert self.ws is not None
        msg: dict = {"type": "response", "id": cmd_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result or {}
        await self.ws.send(json.dumps(msg))

    async def push_event(self, *, tab_id: int, method: str,
                         params: dict | None = None) -> None:
        assert self.ws is not None
        await self.ws.send(json.dumps({
            "type": "event", "tabId": tab_id,
            "method": method, "params": params or {},
        }))

    async def announce_attached(self, *, tab_id: int, url: str = "https://x/",
                                title: str = "x") -> None:
        assert self.ws is not None
        await self.ws.send(json.dumps({
            "type": "attached", "tabId": tab_id,
            "targetInfo": {"url": url, "title": title},
        }))

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        if self.recv_task is not None:
            self.recv_task.cancel()


# ---- §8.4 lifecycle: hello + ready -----------------------------------------


@pytest.mark.asyncio
async def test_relay_starts_and_accepts_extension_hello():
    async with _relay_running() as relay:
        ext = _MockExtension()
        await ext.connect(relay.port)
        # wait_ready should return promptly once hello hits.
        await asyncio.wait_for(relay.wait_ready(timeout=2.0), timeout=2.0)
        assert relay.is_ready
        await ext.close()


@pytest.mark.asyncio
async def test_relay_status_endpoint_reports_connected_extensions():
    """Spec §5.2 doctor hook: GET /__status__ returns running + extension count."""
    async with _relay_running() as relay:
        # Before any extension connects.
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"http://127.0.0.1:{relay.port}/__status__")
        assert r.status_code == 200
        data = r.json()
        assert data["running"] is True
        assert data["extensions"] == 0

        ext = _MockExtension()
        await ext.connect(relay.port, install_id="abc")
        await relay.wait_ready(timeout=2.0)

        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"http://127.0.0.1:{relay.port}/__status__")
        data = r.json()
        assert data["extensions"] == 1
        assert "abc" in data["install_ids"]
        await ext.close()


# ---- §A.4 anti-CSRF: web-page Origin → 403, chrome-extension:// → allowed --


@pytest.mark.asyncio
async def test_relay_rejects_ws_upgrade_with_web_origin():
    """Spec §A.4 OpenCLI borrow: drive-by ws upgrade from a malicious page
    always carries a web-page `Origin` header. The relay must 403."""
    async with _relay_running() as relay:
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc_info:
            await websockets.connect(
                f"ws://127.0.0.1:{relay.port}/",
                additional_headers={"Origin": "https://evil.example/"},
                compression=None,
                open_timeout=2.0,
            )
        assert exc_info.value.response.status_code == 403


@pytest.mark.asyncio
async def test_relay_accepts_ws_upgrade_with_chrome_extension_origin():
    """Chrome 144+ MV3 service workers emit `Origin: chrome-extension://<id>`
    on ws upgrades. The relay must let those through; the original anti-CSRF
    rule incorrectly assumed no Origin would be set."""
    async with _relay_running() as relay:
        conn = await websockets.connect(
            f"ws://127.0.0.1:{relay.port}/",
            additional_headers={
                "Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
            },
            compression=None,
            open_timeout=2.0,
        )
        try:
            assert conn.state.name == "OPEN"
        finally:
            await conn.close()


# ---- ghost target table ----------------------------------------------------


@pytest.mark.asyncio
async def test_attach_tab_registers_ghost_target():
    async with _relay_running() as relay:
        ext = _MockExtension()
        await ext.connect(relay.port)
        await relay.wait_ready(timeout=2.0)

        # Drive the daemon → extension → response handshake for attach_tab.
        async def respond_to_attach():
            cmd = await ext.next_command()
            assert cmd["type"] == "attach"
            assert cmd["tabId"] == 42
            await ext.respond(cmd["id"], result={
                "targetInfo": {"url": "https://example.com/", "title": "Example"},
            })

        responder = asyncio.create_task(respond_to_attach())
        gt = await relay.attach_tab(42)
        await responder

        assert gt.target_id == "ext-tab-42"
        assert gt.tab_id == 42
        assert gt.url == "https://example.com/"
        assert gt.title == "Example"
        # list_ghost_targets surfaces it.
        ghosts = relay.list_ghost_targets()
        assert any(g.target_id == "ext-tab-42" for g in ghosts)
        await ext.close()


# ---- 3-retry chrome.debugger conflict handling -----------------------------


@pytest.mark.asyncio
async def test_attach_retries_on_already_attached_error():
    """Spec §A.4 OpenCLI borrow: `chrome.debugger` collides with DevTools or
    a second extension → "Another debugger is already attached". We retry
    ATTACH_RETRY_LIMIT times before giving up."""
    async with _relay_running() as relay:
        ext = _MockExtension()
        await ext.connect(relay.port)
        await relay.wait_ready(timeout=2.0)

        attempts = 0

        async def responder():
            nonlocal attempts
            for _ in range(ATTACH_RETRY_LIMIT):
                cmd = await ext.next_command()
                attempts += 1
                if attempts < ATTACH_RETRY_LIMIT:
                    await ext.respond(cmd["id"], error={
                        "code": -32000,
                        "message": "Another debugger is already attached",
                    })
                else:
                    # Final attempt succeeds.
                    await ext.respond(cmd["id"], result={
                        "targetInfo": {"url": "https://ok/", "title": "ok"},
                    })

        r = asyncio.create_task(responder())
        # Default backoff is short (0.1+0.3+0.8s) so 2s headroom is plenty.
        gt = await asyncio.wait_for(relay.attach_tab(7), timeout=3.0)
        await r
        assert attempts == ATTACH_RETRY_LIMIT
        assert gt.url == "https://ok/"
        await ext.close()


@pytest.mark.asyncio
async def test_attach_does_not_retry_on_non_conflict_error():
    """Only "already attached" gets retried — other errors are surfaced as-is."""
    async with _relay_running() as relay:
        ext = _MockExtension()
        await ext.connect(relay.port)
        await relay.wait_ready(timeout=2.0)

        async def responder():
            cmd = await ext.next_command()
            await ext.respond(cmd["id"], error={
                "code": -32000, "message": "No tab with id 99",
            })

        r = asyncio.create_task(responder())
        with pytest.raises(_CommandError) as exc:
            await relay.attach_tab(99)
        await r
        assert "no tab" in exc.value.message.lower()
        await ext.close()


# ---- send_cdp passthrough -------------------------------------------------


@pytest.mark.asyncio
async def test_send_cdp_forwards_method_and_returns_result():
    async with _relay_running() as relay:
        ext = _MockExtension()
        await ext.connect(relay.port)
        await relay.wait_ready(timeout=2.0)

        # Register a tab so the relay knows which extension owns it.
        await ext.announce_attached(tab_id=11)
        await asyncio.sleep(0.05)  # let relay ingest

        async def responder():
            cmd = await ext.next_command()
            assert cmd["type"] == "command"
            assert cmd["tabId"] == 11
            assert cmd["method"] == "Page.navigate"
            assert cmd["params"]["url"] == "https://x/"
            await ext.respond(cmd["id"], result={"frameId": "F1"})

        r = asyncio.create_task(responder())
        result = await relay.send_cdp(11, "Page.navigate", {"url": "https://x/"})
        await r
        assert result == {"frameId": "F1"}
        await ext.close()


# ---- Phase B: create_background_tab + close_tab ---------------------------


@pytest.mark.asyncio
async def test_create_background_tab_returns_ghost_target():
    """Phase B Feature 1: relay sends createTab, the mock extension responds
    with tabId + group metadata, relay surfaces a GhostTarget that carries
    the group_id."""
    async with _relay_running() as relay:
        ext = _MockExtension()
        await ext.connect(relay.port)
        await relay.wait_ready(timeout=2.0)

        async def respond():
            cmd = await ext.next_command()
            assert cmd["type"] == "createTab"
            assert cmd["url"] == "https://example.com/"
            assert cmd["groupName"] == "Agent"
            await ext.respond(cmd["id"], result={
                "tabId": 77,
                "url": "https://example.com/",
                "title": "Example",
                "groupId": 9,
            })

        r = asyncio.create_task(respond())
        gt = await relay.create_background_tab(
            "https://example.com/", group_name="Agent")
        await r

        assert gt.target_id == "ext-tab-77"
        assert gt.tab_id == 77
        assert gt.url == "https://example.com/"
        assert gt.title == "Example"
        assert getattr(gt, "group_id", None) == 9
        # Ghost-target table picked it up.
        assert any(g.target_id == "ext-tab-77"
                   for g in relay.list_ghost_targets())
        await ext.close()


@pytest.mark.asyncio
async def test_create_background_tab_without_group_name_omits_group_field():
    """When group_name=None we skip the groupName param entirely; the result's
    group_id falls back to -1 if the extension didn't populate it."""
    async with _relay_running() as relay:
        ext = _MockExtension()
        await ext.connect(relay.port)
        await relay.wait_ready(timeout=2.0)

        async def respond():
            cmd = await ext.next_command()
            assert cmd["type"] == "createTab"
            assert "groupName" not in cmd
            await ext.respond(cmd["id"], result={
                "tabId": 8, "url": "https://x/", "title": "x",
            })

        r = asyncio.create_task(respond())
        gt = await relay.create_background_tab("https://x/", group_name=None)
        await r

        assert gt.tab_id == 8
        assert getattr(gt, "group_id", None) == -1
        await ext.close()


@pytest.mark.asyncio
async def test_close_tab_sends_close_message():
    """Phase B Feature 2: relay sends a closeTab ws frame and pops the ghost
    target from the table when the extension acks."""
    async with _relay_running() as relay:
        ext = _MockExtension()
        await ext.connect(relay.port)
        await relay.wait_ready(timeout=2.0)
        # Register a ghost via attached so close_tab has something to clean up.
        await ext.announce_attached(tab_id=33)
        await asyncio.sleep(0.05)
        assert any(g.tab_id == 33 for g in relay.list_ghost_targets())

        async def respond():
            cmd = await ext.next_command()
            assert cmd["type"] == "closeTab"
            assert cmd["tabId"] == 33
            await ext.respond(cmd["id"], result={"ok": True, "tabId": 33})

        r = asyncio.create_task(respond())
        await relay.close_tab(33)
        await r

        # Ghost-target table no longer carries tab 33 — relay cleaned it.
        assert not any(g.tab_id == 33 for g in relay.list_ghost_targets())
        await ext.close()


@pytest.mark.asyncio
async def test_close_tab_unknown_tab_returns_success():
    """Extension responds 'No tab with id' (already-closed race). The
    extension itself surfaces this as success; the relay then pops the
    ghost target with no error logged for the caller."""
    async with _relay_running() as relay:
        ext = _MockExtension()
        await ext.connect(relay.port)
        await relay.wait_ready(timeout=2.0)
        await ext.announce_attached(tab_id=51)
        await asyncio.sleep(0.05)

        async def respond():
            cmd = await ext.next_command()
            assert cmd["type"] == "closeTab"
            # Real extension would map this case to a success result; we
            # mirror that here (see background.js doCloseTab).
            await ext.respond(cmd["id"], result={"ok": True, "tabId": 51})

        r = asyncio.create_task(respond())
        await relay.close_tab(51)
        await r
        assert not any(g.tab_id == 51 for g in relay.list_ghost_targets())
        await ext.close()


@pytest.mark.asyncio
async def test_close_tab_raises_when_no_extension_connected():
    """Reviewer H1: previously close_tab silently returned success when no
    extension was connected at all — callers got `{"ok": True}` for a close
    that never went over the wire. The relay must surface this as a raised
    exception so upstream layers can map it to -32603."""
    relay = RelayServer(port=0)
    await relay.start()
    try:
        with pytest.raises(Exception):  # RuntimeError today; accept any
            await relay.close_tab(tab_id=42)
    finally:
        await relay.stop()


# ---- event push-back from extension ----------------------------------------


@pytest.mark.asyncio
async def test_attach_active_tab_returns_ghost_target():
    """v0.5.4: attach_active_tab() asks the extension for the focused-window
    active tab and registers the resulting ghost target identically to a
    popup-driven attach."""
    async with _relay_running() as relay:
        ext = _MockExtension()
        await ext.connect(relay.port)
        await relay.wait_ready(timeout=2.0)

        async def respond_attach_active():
            cmd = await ext.next_command()
            assert cmd["type"] == "attachActive"
            await ext.respond(cmd["id"], result={
                "tabId": 314,
                "url": "https://focused.example/",
                "title": "Focused",
            })

        r = asyncio.create_task(respond_attach_active())
        gt = await relay.attach_active_tab(timeout=3.0)
        await r

        assert gt.target_id == "ext-tab-314"
        assert gt.tab_id == 314
        assert gt.url == "https://focused.example/"
        assert gt.title == "Focused"
        # Ghost target is reachable via list_ghost_targets too.
        ghosts = relay.list_ghost_targets()
        assert any(g.target_id == "ext-tab-314" for g in ghosts)
        await ext.close()


@pytest.mark.asyncio
async def test_attach_active_tab_retries_on_already_attached():
    """attach_active_tab inherits the same 3-retry behaviour as attach_tab —
    Chrome occasionally reports the debugger as already attached during a
    quick reattach race."""
    async with _relay_running() as relay:
        ext = _MockExtension()
        await ext.connect(relay.port)
        await relay.wait_ready(timeout=2.0)

        attempts = 0

        async def responder():
            nonlocal attempts
            for _ in range(ATTACH_RETRY_LIMIT):
                cmd = await ext.next_command()
                attempts += 1
                if attempts < ATTACH_RETRY_LIMIT:
                    await ext.respond(cmd["id"], error={
                        "code": -32000,
                        "message": "Another debugger is already attached",
                    })
                else:
                    await ext.respond(cmd["id"], result={
                        "tabId": 8, "url": "https://retried/", "title": "ok",
                    })

        r = asyncio.create_task(responder())
        gt = await asyncio.wait_for(relay.attach_active_tab(timeout=3.0),
                                    timeout=4.0)
        await r
        assert attempts == ATTACH_RETRY_LIMIT
        assert gt.tab_id == 8
        await ext.close()


@pytest.mark.asyncio
async def test_attach_active_tab_surfaces_no_active_tab_error():
    """If the extension says there's no active tab (e.g., zero windows),
    attach_active_tab surfaces a _CommandError so the caller can map to a
    CDP -32000 reply."""
    async with _relay_running() as relay:
        ext = _MockExtension()
        await ext.connect(relay.port)
        await relay.wait_ready(timeout=2.0)

        async def responder():
            cmd = await ext.next_command()
            await ext.respond(cmd["id"], error={
                "code": -32000, "message": "no active tab in focused window",
            })

        r = asyncio.create_task(responder())
        with pytest.raises(_CommandError) as exc:
            await relay.attach_active_tab(timeout=3.0)
        await r
        assert "no active tab" in exc.value.message.lower()
        await ext.close()


@pytest.mark.asyncio
async def test_event_handler_invoked_on_extension_event():
    captured: list[dict] = []

    async def handler(msg: dict) -> None:
        captured.append(msg)

    async with _relay_running() as relay:
        relay.set_event_handler(handler)
        ext = _MockExtension()
        await ext.connect(relay.port)
        await relay.wait_ready(timeout=2.0)

        await ext.push_event(
            tab_id=3, method="Page.frameNavigated",
            params={"frame": {"url": "https://nav/"}})
        # Give the relay a tick to dispatch.
        for _ in range(20):
            if captured:
                break
            await asyncio.sleep(0.02)

        assert len(captured) == 1
        assert captured[0]["method"] == "Page.frameNavigated"
        assert captured[0]["tabId"] == 3
        await ext.close()
