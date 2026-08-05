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
from types import SimpleNamespace
from typing import AsyncIterator

import pytest
import websockets

from browserwright import session_registry as reg
from browserwright.daemon.server.extension_upstream import ExtensionUpstream
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
        self.group_titles: dict[int, str] = {}
        self._next_created_tab = 100
        self.create_tab_messages: list[dict] = []
        # Every chrome.debugger.sendCommand the relay forwarded, as
        # (tabId, method) tuples — lets tests assert what reached the extension.
        self.commands_seen: list[tuple] = []

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
                if msg.get("type") == "helloAck":
                    continue
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
            self.create_tab_messages.append(msg)
            tab = self._next_created_tab
            self._next_created_tab += 1
            url = msg.get("url", "about:blank")
            group_id = msg.get("groupId")
            if not isinstance(group_id, int) or group_id < 0:
                group_id = 700 if msg.get("groupName") else -1
            if group_id >= 0 and msg.get("groupName"):
                self.group_titles[group_id] = str(msg["groupName"])
            self.tabs_meta[tab] = {
                "url": url, "title": "new", "groupId": group_id}
            # The extension announces the attach (the relay turns it into a
            # ghost + fan-out) just like the real one.
            await self.ws.send(json.dumps({
                "type": "attached", "tabId": tab,
                "targetInfo": {"url": url, "title": "new"},
            }))
            await self._respond(cmd_id, {"tabId": tab, "url": url,
                                         "title": "new", "groupId": group_id})
            return
        if kind == "queryGroup":
            gid = msg.get("groupId")
            tabs = []
            if isinstance(gid, int) and gid >= 0:
                tabs = [
                    {
                        "tabId": tab,
                        "url": meta.get("url", ""),
                        "title": meta.get("title", ""),
                    }
                    for tab, meta in sorted(self.tabs_meta.items())
                    if meta.get("groupId") == gid
                ]
            await self._respond(cmd_id, {
                "groupId": gid if isinstance(gid, int) else -1,
                "groupTitle": self.group_titles.get(gid, ""),
                "tabs": tabs,
            })
            return
        if kind == "command":
            method = msg.get("method")
            tab = msg.get("tabId")
            self.commands_seen.append((tab, method))
            # chrome.debugger.sendCommand — answer with an empty result, then
            # (like real Chrome) emit Runtime.executionContextCreated for the
            # default context after a Runtime.enable so the facade's event-gated
            # barrier (PR3) releases.
            await self._respond(cmd_id, {})
            if method == "Runtime.enable" and isinstance(tab, int):
                await self.push_event(
                    tab_id=tab, method="Runtime.executionContextCreated",
                    params={"context": {"id": 1, "auxData": {
                        "isDefault": True, "type": "default"}}})
            return
        if kind == "closeTab":
            tab = int(msg.get("tabId", -1))
            self.tabs_meta.pop(tab, None)
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
                                title: str = "t", group_id: int | None = None,
                                group_title: str | None = None) -> None:
        assert self.ws is not None
        self.tabs_meta[tab_id] = {"url": url, "title": title}
        if group_id is not None:
            self.tabs_meta[tab_id]["groupId"] = group_id
            if group_title is not None:
                self.group_titles[group_id] = group_title
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


async def test_session_bound_replay_only_announces_session_group(tmp_home):
    sid = reg.allocate(backend="extension", owner="create", name="Research")
    reg.update(sid, runtime={
        "group_id": 44,
        "current_target_id": "ext-tab-10",
    })
    relay = RelayServer(port=0)
    port = await relay.start()
    ext = _MockExtension()
    await ext.connect(port)
    await relay.wait_ready(timeout=2.0)
    await ext.announce_attached(
        tab_id=10, url="https://mine/", group_id=44,
        group_title="Research")
    await ext.announce_attached(tab_id=11, url="https://other/", group_id=55)
    await asyncio.sleep(0.05)
    client = _FakeClient()
    bridge = ExtensionFacadeBridge(client=client, relay=relay, session_id=sid)
    run_task = asyncio.create_task(bridge.run())
    try:
        client.feed({"id": 1, "method": "Target.setAutoAttach",
                     "params": {"autoAttach": True}})

        assert await client.wait_for(lambda f: f.get("id") == 1
                                     and "result" in f)
        assert await client.wait_for(
            lambda f: f.get("method") == "Target.attachedToTarget"
            and f["params"]["targetInfo"]["targetId"] == "ext-tab-10")
        await asyncio.sleep(0.1)
        assert not any(
            f.get("method") == "Target.attachedToTarget"
            and f["params"]["targetInfo"]["targetId"] == "ext-tab-11"
            for f in client.sent
        )
    finally:
        client.eof()
        with contextlib_suppress():
            await asyncio.wait_for(run_task, timeout=2.0)
        await ext.close()
        await relay.stop()


async def test_session_bound_browser_methods_reject_foreign_targets(tmp_home):
    sid = reg.allocate(backend="extension", owner="create", name="Research")
    reg.update(sid, runtime={
        "group_id": 44,
        "current_target_id": "ext-tab-10",
    })
    relay = RelayServer(port=0)
    port = await relay.start()
    ext = _MockExtension()
    await ext.connect(port)
    await relay.wait_ready(timeout=2.0)
    await ext.announce_attached(
        tab_id=10, url="https://mine/", group_id=44,
        group_title="Research")
    await ext.announce_attached(
        tab_id=11, url="https://secret/", group_id=55,
        group_title="Other")
    await asyncio.sleep(0.05)
    client = _FakeClient()
    bridge = ExtensionFacadeBridge(client=client, relay=relay, session_id=sid)
    run_task = asyncio.create_task(bridge.run())
    try:
        client.feed({"id": 1, "method": "Target.getTargets", "params": {
            "filter": [{"type": "page", "exclude": False}],
        }})
        listed = await client.wait_for(lambda f: f.get("id") == 1)
        assert {
            info["targetId"] for info in listed["result"]["targetInfos"]
        } == {"ext-tab-10"}

        client.feed({"id": 2, "method": "Target.attachToTarget",
                     "params": {"targetId": "ext-tab-11"}})
        attached = await client.wait_for(lambda f: f.get("id") == 2)
        assert attached["error"]["code"] == -32602

        client.feed({"id": 3, "method": "Target.getTargetInfo",
                     "params": {"targetId": "ext-tab-11"}})
        info = await client.wait_for(lambda f: f.get("id") == 3)
        assert info["error"]["code"] == -32602

        client.feed({"id": 4, "method": "Target.closeTarget",
                     "params": {"targetId": "ext-tab-11"}})
        closed = await client.wait_for(lambda f: f.get("id") == 4)
        assert closed["result"]["success"] is False
        assert 11 in ext.tabs_meta

        before = list(ext.commands_seen)
        client.feed({
            "id": 5,
            "sessionId": "ext-sid-11-FORGED",
            "method": "Runtime.evaluate",
            "params": {"expression": "document.cookie"},
        })
        forged = await client.wait_for(lambda f: f.get("id") == 5)
        assert forged["error"]["code"] == -32602
        assert ext.commands_seen == before
    finally:
        client.eof()
        with contextlib_suppress():
            await asyncio.wait_for(run_task, timeout=2.0)
        await ext.close()
        await relay.stop()


async def test_session_scope_is_enforced_without_transport():
    class _Relay:
        port = 19989
        connection_generation = 1

        def __init__(self):
            self.closed: list[int] = []
            self.commands: list[tuple[int, str]] = []

        def list_ghost_targets(self):
            return [
                SimpleNamespace(
                    target_id="ext-tab-10", type="page",
                    url="https://mine/", title="Mine"),
                SimpleNamespace(
                    target_id="ext-tab-11", type="page",
                    url="https://secret/", title="Secret"),
            ]

        async def query_group_tabs(self, *, group_id, timeout=10.0):
            members = {44: [10], 55: [11]}.get(group_id, [])
            return {"groupId": group_id, "tabs": [
                {"tabId": tab_id} for tab_id in members
            ]}

        async def close_tab(self, tab_id):
            self.closed.append(tab_id)

        async def attach_tab(
            self, tab_id, *, expected_generation=None, timeout=10.0,
        ):
            return None

        async def send_cdp(self, tab_id, method, params):
            self.commands.append((tab_id, method))
            return {}

        def set_session_announce(self, _session_id):
            return None

        def reset_session_announce(self, _session_id):
            return None

    async def _noop(_value):
        return None

    relay = _Relay()
    owner = ExtensionUpstream(relay, _noop, _noop)
    owner._bind_group("session-A", 44)
    client = _FakeClient()
    bridge = ExtensionFacadeBridge(
        client=client,
        relay=relay,
        session_id="session-A",
        session_name="A",
        binding_owner=owner,
    )

    await bridge._handle_client_frame(json.dumps({
        "id": 1, "method": "Target.getTargets", "params": {},
    }))
    assert {
        info["targetId"] for info in client.result_for(1)["targetInfos"]
    } == {"ext-tab-10"}

    await bridge._handle_client_frame(json.dumps({
        "id": 2, "method": "Target.attachToTarget",
        "params": {"targetId": "ext-tab-11"},
    }))
    assert next(f for f in client.sent if f.get("id") == 2)["error"]["code"] == -32602

    await bridge._handle_client_frame(json.dumps({
        "id": 3, "method": "Target.getTargetInfo",
        "params": {"targetId": "ext-tab-11"},
    }))
    assert next(f for f in client.sent if f.get("id") == 3)["error"]["code"] == -32602

    await bridge._handle_client_frame(json.dumps({
        "id": 4, "method": "Target.closeTarget",
        "params": {"targetId": "ext-tab-11"},
    }))
    assert client.result_for(4)["success"] is False
    assert relay.closed == []

    await bridge._handle_client_frame(json.dumps({
        "id": 5, "sessionId": "ext-sid-11-FORGED",
        "method": "Runtime.evaluate", "params": {"expression": "secret"},
    }))
    assert next(f for f in client.sent if f.get("id") == 5)["error"]["code"] == -32602
    assert relay.commands == []


# ---- A3: createTarget maps to a background tab -----------------------------


async def test_create_target_opens_background_tab():
    async with _wired() as (relay, ext, client, bridge):
        client.feed({"id": 3, "method": "Target.createTarget",
                     "params": {"url": "https://new/"}})
        # Result carries an ext-tab-* targetId.
        res = await client.wait_for(lambda f: f.get("id") == 3 and "result" in f)
        tid = res["result"]["targetId"]
        assert tid.startswith("ext-tab-")
        assert ext.create_tab_messages[-1]["skipPostAttachCommands"] is True
        # The new target is announced so Playwright attaches a Page.
        await client.wait_for(
            lambda f: f.get("method") == "Target.attachedToTarget"
            and f["params"]["targetInfo"]["targetId"] == tid)


async def test_session_bound_create_target_uses_and_persists_group(tmp_home):
    sid = reg.allocate(backend="extension", owner="create", name="Research")
    reg.update(sid, runtime={
        "group_id": 44,
        "current_target_id": "ext-tab-10",
    })
    relay = RelayServer(port=0)
    port = await relay.start()
    ext = _MockExtension()
    await ext.connect(port)
    await relay.wait_ready(timeout=2.0)
    await ext.announce_attached(
        tab_id=10, url="https://existing/", group_id=44,
        group_title="Research")
    client = _FakeClient()
    async def _noop_frame(_text):
        return None

    async def _noop_close(_reason):
        return None

    binding_owner = ExtensionUpstream(relay, _noop_frame, _noop_close)
    bridge = ExtensionFacadeBridge(
        client=client, relay=relay, session_id=sid,
        binding_owner=binding_owner)
    run_task = asyncio.create_task(bridge.run())
    try:
        client.feed({"id": 3, "method": "Target.createTarget",
                     "params": {"url": "https://new/"}})
        res = await client.wait_for(lambda f: f.get("id") == 3 and "result" in f)
        tid = res["result"]["targetId"]
        tab_id = int(tid.rsplit("-", 1)[1])
        assert ext.tabs_meta[tab_id]["groupId"] == 44
        assert (reg.get(sid).get("runtime") or {})["group_id"] == 44
    finally:
        client.eof()
        with contextlib_suppress():
            await asyncio.wait_for(run_task, timeout=2.0)
        await ext.close()
        await relay.stop()


async def test_session_scoped_create_target_joins_session_group():
    relay = RelayServer(port=0)
    port = await relay.start()
    ext = _MockExtension()
    await ext.connect(port)
    await relay.wait_ready(timeout=2.0)
    client = _FakeClient()
    bridge = ExtensionFacadeBridge(
        client=client, relay=relay,
        session_id="bw-s", session_name="Scoped Session")
    run_task = asyncio.create_task(bridge.run())
    try:
        client.feed({"id": 3, "method": "Target.createTarget",
                     "params": {"url": "https://scoped/"}})
        res = await client.wait_for(
            lambda f: f.get("id") == 3 and "result" in f)
        tid = res["result"]["targetId"]
        await client.wait_for(
            lambda f: f.get("method") == "Target.attachedToTarget"
            and f["params"]["targetInfo"]["targetId"] == tid)

        assert ext.create_tab_messages
        created = ext.create_tab_messages[-1]
        assert created["groupName"] == "Scoped Session"
        assert "groupId" not in created
        assert bridge._ext._groups["bw-s"] == 700  # noqa: SLF001
    finally:
        client.eof()
        with contextlib_suppress():
            await asyncio.wait_for(run_task, timeout=2.0)
        await ext.close()
        await relay.stop()


async def test_session_bound_create_target_refreshes_agent_bound_group(tmp_home):
    sid = reg.allocate(backend="extension", owner="create", name="Research")
    relay = RelayServer(port=0)
    port = await relay.start()
    ext = _MockExtension()
    await ext.connect(port)
    await relay.wait_ready(timeout=2.0)
    client = _FakeClient()
    async def _noop_frame(_text):
        return None

    async def _noop_close(_reason):
        return None

    binding_owner = ExtensionUpstream(relay, _noop_frame, _noop_close)
    bridge = ExtensionFacadeBridge(
        client=client, relay=relay, session_id=sid,
        binding_owner=binding_owner)
    run_task = asyncio.create_task(bridge.run())
    try:
        # Simulate the agent path winning the race after the facade bridge was
        # constructed: the adapter-owned live binding is immediately visible
        # to the facade; neither the relay nor bridge keeps a duplicate cache.
        binding_owner._bind_group(sid, 88)  # noqa: SLF001

        client.feed({"id": 3, "method": "Target.createTarget",
                     "params": {"url": "https://new/"}})
        res = await client.wait_for(lambda f: f.get("id") == 3 and "result" in f)
        tid = res["result"]["targetId"]
        tab_id = int(tid.rsplit("-", 1)[1])
        assert ext.tabs_meta[tab_id]["groupId"] == 88
        assert binding_owner.group_for_session(sid) == 88
        assert (reg.get(sid).get("runtime") or {})["group_id"] == 88
    finally:
        client.eof()
        with contextlib_suppress():
            await asyncio.wait_for(run_task, timeout=2.0)
        await ext.close()
        await relay.stop()


async def test_session_scoped_create_target_persists_group_id(monkeypatch):
    updates: list[tuple[str, dict]] = []

    class _Registry:
        @staticmethod
        def get(_session_id):
            return {"runtime": {"current_target_id": "ext-tab-old"}}

        @staticmethod
        def update(session_id, **fields):
            updates.append((session_id, fields))

    import browserwright.session_registry as session_registry

    monkeypatch.setattr(session_registry, "get", _Registry.get)
    monkeypatch.setattr(session_registry, "update", _Registry.update)

    async with _wired() as (relay, ext, client, bridge):
        bridge = ExtensionFacadeBridge(
            client=client, relay=relay,
            session_id="bw-s", session_name="Scoped Session")
        await bridge._handle_create_target(3, {"url": "https://scoped/"})  # noqa: SLF001

    assert updates
    sid, fields = updates[-1]
    assert sid == "bw-s"
    assert fields["runtime"]["current_target_id"] == "ext-tab-old"
    assert fields["runtime"]["group_id"] == 700
    assert isinstance(fields["runtime"]["updated_at"], float)


async def test_session_scoped_create_target_does_not_trust_bare_group_id():
    async with _wired() as (relay, ext, client, bridge):
        bridge = ExtensionFacadeBridge(
            client=client, relay=relay,
            session_id="bw-s", session_name="Scoped Session",
            session_group_id=42)
        await bridge._handle_create_target(3, {"url": "https://scoped/"})  # noqa: SLF001

        assert ext.create_tab_messages
        created = ext.create_tab_messages[-1]
        assert created["groupName"] == "Scoped Session"
        assert "groupId" not in created
        assert bridge._ext._groups["bw-s"] == 700  # noqa: SLF001


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


# ---- PR3: CRPage high-level fidelity ---------------------------------------


async def _attach_one(ext: _MockExtension, client: _FakeClient, *,
                      tab_id: int) -> str:
    """Announce a tab and run the discovery handshake; return its sessionId."""
    await ext.announce_attached(tab_id=tab_id)
    await asyncio.sleep(0.05)
    client.feed({"id": 1, "method": "Target.setAutoAttach",
                 "params": {"autoAttach": True}})
    attached = await client.wait_for(
        lambda f: f.get("method") == "Target.attachedToTarget"
        and f["params"]["targetInfo"]["targetId"] == f"ext-tab-{tab_id}")
    return attached["params"]["sessionId"]


async def test_attached_target_carries_browser_context_id():
    """PR3 fix #1: Playwright's crBrowser asserts a truthy browserContextId on
    attachedToTarget before building CRPage. The synthesized targetInfo must
    carry a stable non-empty id + type=page + waitingForDebugger:false."""
    async with _wired() as (relay, ext, client, bridge):
        await _attach_one(ext, client, tab_id=42)
        att = client.result_for  # noqa: F841 — readability only
        attached = next(f for f in client.sent
                        if f.get("method") == "Target.attachedToTarget")
        ti = attached["params"]["targetInfo"]
        assert ti["browserContextId"], "browserContextId must be truthy"
        assert ti["type"] == "page"
        assert attached["params"]["waitingForDebugger"] is False


async def test_page_session_set_auto_attach_is_forwarded():
    """ZER-123: page-session Target.setAutoAttach is required to make Chrome
    surface OOPIF child targets to chrome.debugger. The extension handles the
    child sessions and the facade keeps their lifecycle events hidden."""
    async with _wired() as (relay, ext, client, bridge):
        sid = await _attach_one(ext, client, tab_id=8)
        ext.commands_seen.clear()
        client.feed({"id": 30, "sessionId": sid,
                     "method": "Target.setAutoAttach",
                     "params": {"autoAttach": True, "flatten": True,
                                "waitForDebuggerOnStart": True}})
        await client.wait_for(lambda f: f.get("id") == 30
                              and f.get("sessionId") == sid
                              and "result" in f)
        assert (8, "Target.setAutoAttach") in ext.commands_seen


async def test_child_target_lifecycle_events_are_not_forwarded_to_playwright():
    """ZER-123: if the extension ever relays child Target.* events, the facade
    must not expose them as page-session events until child-session routing is
    implemented."""
    async with _wired() as (relay, ext, client, bridge):
        sid = await _attach_one(ext, client, tab_id=8)
        assert sid
        before = len(client.sent)
        await ext.push_event(
            tab_id=8,
            method="Target.attachedToTarget",
            params={
                "sessionId": "child-session",
                "targetInfo": {
                    "targetId": "child-target",
                    "type": "iframe",
                    "url": "https://child.example/",
                },
                "waitingForDebugger": True,
            },
        )
        await ext.push_event(
            tab_id=8,
            method="Target.detachedFromTarget",
            params={"sessionId": "child-session", "targetId": "child-target"},
        )
        await asyncio.sleep(0.1)
        new_frames = client.sent[before:]
        assert not any(
            f.get("method") in (
                "Target.attachedToTarget", "Target.detachedFromTarget")
            for f in new_frames
        ), new_frames


async def test_runtime_enable_does_disable_enable_dance_and_gates_on_event():
    """PR3 fix #2: Runtime.enable issues Runtime.disable→enable to force a
    re-emit, and gates its response on the default executionContextCreated."""
    async with _wired() as (relay, ext, client, bridge):
        sid = await _attach_one(ext, client, tab_id=8)
        ext.commands_seen.clear()
        client.feed({"id": 20, "sessionId": sid, "method": "Runtime.enable",
                     "params": {}})
        res = await client.wait_for(
            lambda f: f.get("id") == 20 and "result" in f)
        assert res["result"] == {}
        methods = [m for (t, m) in ext.commands_seen if t == 8]
        assert "Runtime.disable" in methods, f"no disable in {methods}"
        assert "Runtime.enable" in methods, f"no enable in {methods}"
        assert methods.index("Runtime.disable") < methods.index(
            "Runtime.enable"), "disable must precede enable"


async def test_created_blank_target_reports_initial_empty_url():
    """PR3 fix #3: a freshly-created about:blank target is reported with the
    initial-empty-document url ':' (so CRPage's isInitialEmptyPage heuristic
    matches real Chrome); a real frameNavigated then refreshes it."""
    async with _wired() as (relay, ext, client, bridge):
        client.feed({"id": 3, "method": "Target.createTarget",
                     "params": {"url": "about:blank"}})
        res = await client.wait_for(
            lambda f: f.get("id") == 3 and "result" in f)
        tid = res["result"]["targetId"]
        created = await client.wait_for(
            lambda f: f.get("method") == "Target.targetCreated"
            and f["params"]["targetInfo"]["targetId"] == tid)
        assert created["params"]["targetInfo"]["url"] == ":", (
            "fresh blank tab must report ':' (initial empty document)")
        # A real navigation refreshes the tracked url.
        tab_id = int(tid[len("ext-tab-"):])
        await ext.push_event(tab_id=tab_id, method="Page.frameNavigated",
                             params={"frame": {"id": "F", "url": "https://z/"}})
        await asyncio.sleep(0.05)
        info = bridge._target_info(tab_id)  # noqa: SLF001
        assert info["url"] == "https://z/", (
            f"frameNavigated did not refresh url; got {info['url']!r}")


async def test_close_target_emits_detach_and_destroy_events():
    """PR3 follow-up: a successful browser-level Target.closeTarget must emit
    Target.detachedFromTarget + Target.targetDestroyed AFTER the success
    response (real Chrome always does; Playwright's page teardown AWAITS the
    destroy — without it new_page() cleanup hangs forever)."""
    async with _wired() as (relay, ext, client, bridge):
        sid = await _attach_one(ext, client, tab_id=8)
        assert sid
        client.feed({"id": 50, "method": "Target.closeTarget",
                     "params": {"targetId": "ext-tab-8"}})
        res = await client.wait_for(
            lambda f: f.get("id") == 50 and "result" in f)
        assert res["result"]["success"] is True
        detached = await client.wait_for(
            lambda f: f.get("method") == "Target.detachedFromTarget"
            and f["params"]["targetId"] == "ext-tab-8")
        assert detached["params"]["sessionId"] == sid
        destroyed = await client.wait_for(
            lambda f: f.get("method") == "Target.targetDestroyed"
            and f["params"]["targetId"] == "ext-tab-8")
        assert destroyed
        # Order: success response precedes the destroy event.
        ids = [i for i, f in enumerate(client.sent)
               if f.get("id") == 50 and "result" in f]
        destroy_idx = [i for i, f in enumerate(client.sent)
                       if f.get("method") == "Target.targetDestroyed"]
        assert ids and destroy_idx and ids[0] < destroy_idx[0], (
            "success response must precede targetDestroyed")
        # Per-tab state is evicted on close (no leak).
        assert 8 not in bridge._tab_sessions  # noqa: SLF001
        assert 8 not in bridge._tab_main_frame  # noqa: SLF001


async def test_main_frame_id_rewrite_round_trip_and_agent_path_isolation():
    """PR3 follow-up (highest-scrutiny): the bridge rewrites the REAL Chrome
    main-frame id to the synthetic targetId (ext-tab-<id>) in the getFrameTree
    response AND in forwarded page events, and rewrites it BACK on inbound
    commands. The rewrite must NOT corrupt the shared relay event dict that the
    agent path's primary _on_event also consumes."""
    async with _wired() as (relay, ext, client, bridge):
        sid = await _attach_one(ext, client, tab_id=8)
        # Teach the mock to answer Page.getFrameTree with a REAL main-frame id.
        real_frame_id = "REALFRAME123"

        async def _frame_tree_handler(msg: dict) -> None:
            if msg.get("type") == "command" and (
                    msg.get("method") == "Page.getFrameTree"):
                await ext._respond(msg.get("id"), {"frameTree": {"frame": {
                    "id": real_frame_id, "url": "about:blank"}}})

        # Drive getFrameTree through the bridge; intercept via a one-shot patch.
        orig_handle = ext._handle

        async def _patched(msg: dict) -> None:
            if (msg.get("type") == "command"
                    and msg.get("method") == "Page.getFrameTree"):
                await _frame_tree_handler(msg)
                return
            await orig_handle(msg)

        ext._handle = _patched  # type: ignore[method-assign]
        client.feed({"id": 60, "sessionId": sid,
                     "method": "Page.getFrameTree", "params": {}})
        res = await client.wait_for(
            lambda f: f.get("id") == 60 and "result" in f)
        # Response carries the SYNTHETIC main-frame id, not the real one.
        assert res["result"]["frameTree"]["frame"]["id"] == "ext-tab-8"
        assert bridge._tab_main_frame[8] == real_frame_id  # noqa: SLF001

        # A forwarded event with the REAL frame id is rewritten to synthetic.
        ext._handle = orig_handle  # type: ignore[method-assign]
        agent_seen: list[dict] = []

        async def _agent_handler(ext_msg: dict) -> None:
            # Mimic the agent path: capture what it would serialize.
            agent_seen.append(json.loads(json.dumps(ext_msg)))

        relay.set_event_handler(_agent_handler)
        await ext.push_event(
            tab_id=8, method="Page.lifecycleEvent",
            params={"frameId": real_frame_id, "name": "load"})
        evt = await client.wait_for(
            lambda f: f.get("method") == "Page.lifecycleEvent")
        # Facade rewrote the top-frame id to synthetic for Playwright.
        assert evt["params"]["frameId"] == "ext-tab-8"
        # The agent path saw the ORIGINAL real frame id (ordering: _on_event
        # runs+serializes before the facade fan-out mutates) — no corruption.
        assert agent_seen, "agent handler must have observed the event"
        assert agent_seen[-1]["params"]["frameId"] == real_frame_id, (
            "agent path's event was corrupted by the facade rewrite")

        # Inbound command scoped to the synthetic frame id is rewritten back.
        ext.commands_seen.clear()
        captured: list[dict] = []
        orig_handle2 = ext._handle

        async def _capture(msg: dict) -> None:
            if (msg.get("type") == "command"
                    and msg.get("method") == "Page.createIsolatedWorld"):
                captured.append(msg.get("params") or {})
                await ext._respond(msg.get("id"), {})
                return
            await orig_handle2(msg)

        ext._handle = _capture  # type: ignore[method-assign]
        client.feed({"id": 61, "sessionId": sid,
                     "method": "Page.createIsolatedWorld",
                     "params": {"frameId": "ext-tab-8"}})
        await client.wait_for(lambda f: f.get("id") == 61 and "result" in f)
        assert captured and captured[0]["frameId"] == real_frame_id, (
            f"inbound command frameId not rewritten back; got {captured}")


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN BUG (unfixed): the session's first tab is never announced to "
           "a session-bound facade bridge. See the docstring for the exact "
           "ordering. This is a real, deterministic defect found while "
           "diagnosing PageBindTimeout; it is NOT the whole cause of the "
           "extension e2e PageBindTimeout failures (fixing it alone leaves "
           "context.pages empty because Playwright's CRPage init still never "
           "completes), so it is recorded here rather than papered over.",
)
async def test_agent_first_tab_of_groupless_session_is_announced(tmp_home):
    """Cold-bind regression: the session's FIRST tab must reach Playwright.

    When the AGENT path opens the first tab of a session that has no tab group
    yet, the extension emits ``attached`` BEFORE the createTab response, so the
    relay fan-out reaches this bridge while the session→group binding (written
    from that response) still does not exist. The visibility check then reports
    "not in my group" and the announce is skipped -- permanently, because no
    later event ever re-announces a tab. Playwright's ``context.pages`` stays
    empty and ``bind_current_page`` raises ``PageBindTimeout`` no matter how
    long it waits.
    """
    from browserwright.daemon.server.extension_upstream import ExtensionUpstream

    sid = reg.allocate(backend="extension", owner="create", name="Cold")
    # NOTE: deliberately NO runtime={"group_id": ...} -- this is a cold session.
    relay = RelayServer(port=0)
    port = await relay.start()
    ext = _MockExtension()
    await ext.connect(port)
    await relay.wait_ready(timeout=2.0)

    client = _FakeClient()
    bridge = ExtensionFacadeBridge(client=client, relay=relay, session_id=sid)
    run_task = asyncio.create_task(bridge.run())
    try:
        # Playwright completes its discovery handshake FIRST: the executor
        # cold-start connects the facade before it resolves the session target.
        client.feed({"id": 1, "method": "Target.setDiscoverTargets",
                     "params": {"discover": True}})
        assert await client.wait_for(lambda f: f.get("id") == 1
                                     and "result" in f)

        # Now the AGENT path opens the session's first tab.
        async def _noop(_):
            return None

        agent = ExtensionUpstream(relay, _noop, _noop)
        opened = await agent.open_background_tab(
            "about:blank", group_name="Cold", session_id=sid,
            skip_post_attach_commands=True)
        target_id = opened["targetId"]

        # The authoritative target MUST be announced to Playwright.
        await client.wait_for(
            lambda f: f.get("method") == "Target.attachedToTarget"
            and f["params"]["targetInfo"]["targetId"] == target_id,
            timeout=3.0)
    finally:
        client.eof()
        with contextlib_suppress():
            await asyncio.wait_for(run_task, timeout=2.0)
        await ext.close()
        await relay.stop()
