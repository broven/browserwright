"""Unit coverage for the Playwright facade ↔ extension bridge (PR2, phase A2/A3/A4).

These tests drive `ExtensionFacadeBridge` directly with:
  - a REAL `RelayServer` on an ephemeral port,
  - a mock Chrome extension ws client (announces attached tabs, answers
    chrome.debugger commands),
  - a fake Playwright client (a object capturing every frame the bridge sends).

We assert the synthesis the research delta calls for WITHOUT a real browser:
  A2  setAutoAttach / setDiscoverTargets → ack + Target.targetCreated +
      Target.attachedToTarget for every known tab; a tab attached LATER also
      triggers the events (relay fan-out).
  A3  Target.createTarget → relay create_background_tab + synthesized events.
  A4  Runtime.enable (session-scoped) → forwarded to the extension + acked.
  Regression: the bridge must not clobber the agent path's relay event handler.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

import websockets

from browserwright.daemon.server.facade_extension import ExtensionFacadeBridge
from browserwright.daemon.server.relay import RelayServer


# ---- mock extension (real ws to the relay) ---------------------------------


class _MockExtension:
    """A fake Chrome extension SW: connects to the relay, announces tabs,
    auto-answers a configurable set of chrome.debugger commands."""

    def __init__(self):
        self.ws: websockets.ClientConnection | None = None
        self.recv_task: asyncio.Task | None = None
        # method/type → callable(msg) -> result dict (None error). For
        # 'createTab' / 'attach' / 'command' we provide sane defaults.
        self.tabs_meta: dict[int, dict] = {}
        self._next_created_tab = 100

    async def connect(self, port: int) -> None:
        self.ws = await websockets.connect(
            f"ws://127.0.0.1:{port}/", compression=None)
        await self.ws.send(json.dumps({
            "type": "hello", "installId": "ext-1",
            "browser": "chrome", "version": "144.0",
        }))
        self.recv_task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        assert self.ws is not None
        try:
            async for raw in self.ws:
                if not isinstance(raw, (str, bytes)):
                    continue
                text = raw if isinstance(raw, str) else raw.decode()
                msg = json.loads(text)
                await self._handle(msg)
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _handle(self, msg: dict) -> None:
        assert self.ws is not None
        kind = msg.get("type")
        cmd_id = msg.get("id")
        if kind == "attach":
            tab = int(msg.get("tabId", -1))
            meta = self.tabs_meta.get(tab, {"url": "https://x/", "title": "x"})
            await self._respond(cmd_id, {"targetInfo": meta})
            return
        if kind == "createTab":
            tab = self._next_created_tab
            self._next_created_tab += 1
            url = msg.get("url", "about:blank")
            self.tabs_meta[tab] = {"url": url, "title": "new"}
            # The extension announces the attach (the relay turns it into a
            # ghost + fan-out) just like the real one.
            await self.ws.send(json.dumps({
                "type": "attached", "tabId": tab,
                "targetInfo": {"url": url, "title": "new"},
            }))
            await self._respond(cmd_id, {"tabId": tab, "url": url,
                                         "title": "new", "groupId": -1})
            return
        if kind == "command":
            # chrome.debugger.sendCommand — answer with an empty result.
            await self._respond(cmd_id, {})
            return
        if kind == "ping":
            await self.ws.send(json.dumps({"type": "pong", "ts": msg.get("ts")}))
            return

    async def _respond(self, cmd_id, result: dict) -> None:
        assert self.ws is not None
        await self.ws.send(json.dumps({
            "type": "response", "id": cmd_id, "result": result,
        }))

    async def announce_attached(self, *, tab_id: int, url: str = "https://t/",
                                title: str = "t") -> None:
        assert self.ws is not None
        self.tabs_meta[tab_id] = {"url": url, "title": title}
        await self.ws.send(json.dumps({
            "type": "attached", "tabId": tab_id,
            "targetInfo": {"url": url, "title": title},
        }))

    async def push_event(self, *, tab_id: int, method: str,
                         params: dict | None = None) -> None:
        assert self.ws is not None
        await self.ws.send(json.dumps({
            "type": "event", "tabId": tab_id,
            "method": method, "params": params or {},
        }))

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        if self.recv_task is not None:
            self.recv_task.cancel()


# ---- fake Playwright client (captures frames sent by the bridge) -----------


class _FakeClient:
    """Quacks like a websockets ServerConnection for the bridge: iterable of
    inbound frames + a `send` that captures outbound frames."""

    def __init__(self):
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()

    def feed(self, msg: dict) -> None:
        self._inbox.put_nowait(json.dumps(msg))

    def eof(self) -> None:
        self._inbox.put_nowait(None)

    async def send(self, frame: str) -> None:
        self.sent.append(json.loads(frame))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self._inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item

    # --- helpers for assertions ---
    async def wait_for(self, predicate, *, timeout: float = 2.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            for f in list(self.sent):
                if predicate(f):
                    return f
            await asyncio.sleep(0.02)
        raise AssertionError(
            f"frame not seen within {timeout}s; sent={self.sent}")

    def methods(self) -> list[str]:
        return [f.get("method") for f in self.sent if "method" in f]

    def result_for(self, req_id: int) -> dict | None:
        for f in self.sent:
            if f.get("id") == req_id and "result" in f:
                return f["result"]
        return None


@asynccontextmanager
async def _wired() -> AsyncIterator[tuple[RelayServer, _MockExtension, _FakeClient, ExtensionFacadeBridge]]:
    relay = RelayServer(port=0)
    port = await relay.start()
    ext = _MockExtension()
    await ext.connect(port)
    await relay.wait_ready(timeout=2.0)
    client = _FakeClient()
    bridge = ExtensionFacadeBridge(client=client, relay=relay)
    run_task = asyncio.create_task(bridge.run())
    try:
        yield relay, ext, client, bridge
    finally:
        client.eof()
        with contextlib_suppress():
            await asyncio.wait_for(run_task, timeout=2.0)
        await ext.close()
        await relay.stop()


def contextlib_suppress():
    import contextlib
    return contextlib.suppress(Exception, asyncio.CancelledError, asyncio.TimeoutError)


# ---- A2: discovery handshake replays target events -------------------------


async def test_set_auto_attach_replays_attached_for_known_tabs():
    async with _wired() as (relay, ext, client, bridge):
        # The extension already has one attached tab (popup-driven).
        await ext.announce_attached(tab_id=42, url="https://a/", title="A")
        # Let the relay register the ghost.
        await asyncio.sleep(0.05)

        client.feed({"id": 1, "method": "Target.setAutoAttach",
                     "params": {"autoAttach": True, "flatten": True,
                                "waitForDebuggerOnStart": False}})

        # ack
        assert await client.wait_for(lambda f: f.get("id") == 1
                                     and "result" in f)
        # targetCreated + attachedToTarget for tab 42
        created = await client.wait_for(
            lambda f: f.get("method") == "Target.targetCreated"
            and f["params"]["targetInfo"]["targetId"] == "ext-tab-42")
        assert created["params"]["targetInfo"]["url"] == "https://a/"
        attached = await client.wait_for(
            lambda f: f.get("method") == "Target.attachedToTarget"
            and f["params"]["targetInfo"]["targetId"] == "ext-tab-42")
        assert attached["params"]["waitingForDebugger"] is False
        assert attached["params"]["sessionId"]


async def test_tab_attached_after_handshake_triggers_events():
    async with _wired() as (relay, ext, client, bridge):
        client.feed({"id": 1, "method": "Target.setAutoAttach",
                     "params": {"autoAttach": True}})
        assert await client.wait_for(lambda f: f.get("id") == 1)

        # A new tab shows up LATER (e.g. user opened one) → fan-out → events.
        await ext.announce_attached(tab_id=77, url="https://late/", title="L")
        attached = await client.wait_for(
            lambda f: f.get("method") == "Target.attachedToTarget"
            and f["params"]["targetInfo"]["targetId"] == "ext-tab-77")
        assert attached["params"]["sessionId"]


async def test_set_discover_targets_acks_and_replays():
    async with _wired() as (relay, ext, client, bridge):
        await ext.announce_attached(tab_id=5)
        await asyncio.sleep(0.05)
        client.feed({"id": 9, "method": "Target.setDiscoverTargets",
                     "params": {"discover": True}})
        assert await client.wait_for(lambda f: f.get("id") == 9
                                     and "result" in f)
        assert await client.wait_for(
            lambda f: f.get("method") == "Target.targetCreated"
            and f["params"]["targetInfo"]["targetId"] == "ext-tab-5")


# ---- A3: createTarget maps to a background tab -----------------------------


async def test_create_target_opens_background_tab():
    async with _wired() as (relay, ext, client, bridge):
        client.feed({"id": 3, "method": "Target.createTarget",
                     "params": {"url": "https://new/"}})
        # Result carries an ext-tab-* targetId.
        res = await client.wait_for(lambda f: f.get("id") == 3 and "result" in f)
        tid = res["result"]["targetId"]
        assert tid.startswith("ext-tab-")
        # The new target is announced so Playwright attaches a Page.
        await client.wait_for(
            lambda f: f.get("method") == "Target.attachedToTarget"
            and f["params"]["targetInfo"]["targetId"] == tid)


# ---- A4: Runtime.enable barrier --------------------------------------------


async def test_runtime_enable_forwarded_and_acked():
    async with _wired() as (relay, ext, client, bridge):
        # First attach a tab so we have a session.
        await ext.announce_attached(tab_id=8)
        await asyncio.sleep(0.05)
        client.feed({"id": 1, "method": "Target.setAutoAttach",
                     "params": {"autoAttach": True}})
        attached = await client.wait_for(
            lambda f: f.get("method") == "Target.attachedToTarget"
            and f["params"]["targetInfo"]["targetId"] == "ext-tab-8")
        sid = attached["params"]["sessionId"]

        client.feed({"id": 20, "sessionId": sid, "method": "Runtime.enable",
                     "params": {}})
        # The bridge forwards to the extension (mock returns {}) and acks.
        res = await client.wait_for(lambda f: f.get("id") == 20 and "result" in f)
        assert res["result"] == {}


# ---- session-scoped events get tagged --------------------------------------


async def test_async_page_event_tagged_with_session():
    async with _wired() as (relay, ext, client, bridge):
        await ext.announce_attached(tab_id=8)
        await asyncio.sleep(0.05)
        client.feed({"id": 1, "method": "Target.setAutoAttach",
                     "params": {"autoAttach": True}})
        attached = await client.wait_for(
            lambda f: f.get("method") == "Target.attachedToTarget"
            and f["params"]["targetInfo"]["targetId"] == "ext-tab-8")
        sid = attached["params"]["sessionId"]

        await ext.push_event(tab_id=8, method="Page.frameNavigated",
                             params={"frame": {"id": "1"}})
        ev = await client.wait_for(
            lambda f: f.get("method") == "Page.frameNavigated")
        assert ev.get("sessionId") == sid


# ---- regression: agent event handler not clobbered -------------------------


async def test_bridge_does_not_clobber_agent_event_handler():
    relay = RelayServer(port=0)
    port = await relay.start()
    try:
        agent_seen: list[dict] = []

        async def agent_handler(msg: dict) -> None:
            agent_seen.append(msg)

        relay.set_event_handler(agent_handler)

        ext = _MockExtension()
        await ext.connect(port)
        await relay.wait_ready(timeout=2.0)

        client = _FakeClient()
        bridge = ExtensionFacadeBridge(client=client, relay=relay)
        run_task = asyncio.create_task(bridge.run())

        # An event reaches BOTH the agent primary handler and the facade fan-out.
        await ext.announce_attached(tab_id=8)
        await asyncio.sleep(0.05)
        await ext.push_event(tab_id=8, method="Page.loadEventFired")
        await asyncio.sleep(0.1)
        assert any(m.get("method") == "Page.loadEventFired" for m in agent_seen)

        # Bridge close must NOT null out the agent handler.
        client.eof()
        await asyncio.wait_for(run_task, timeout=2.0)
        assert relay._on_event is agent_handler  # noqa: SLF001

        await ext.close()
    finally:
        await relay.stop()
