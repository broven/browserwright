"""ExtensionUpstream adapter tests (v0.4).

Verifies CDP→relay translation, Target.* interception, unsupported
Browser.* surfacing as -32601, and event fan-in from the relay back through
`on_frame`.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest
import websockets

from browserwright.daemon.server.extension_upstream import (
    ExtensionUpstream, _tab_id_from_session_id, _tab_id_from_target_id,
)
from browserwright.daemon.server.relay import RelayServer

@asynccontextmanager
async def _relay_running() -> AsyncIterator[RelayServer]:
    relay = RelayServer(port=0)
    await relay.start()
    try:
        yield relay
    finally:
        await relay.stop()


class _MockExtension:
    def __init__(self):
        self.ws: websockets.ClientConnection | None = None
        self.received: list[dict] = []
        self.recv_task: asyncio.Task | None = None
        self._inbox: asyncio.Queue[dict] = asyncio.Queue()

    async def connect(self, port: int, *, install_id: str = "test-ext-1") -> None:
        self.ws = await websockets.connect(
            f"ws://127.0.0.1:{port}/", compression=None)
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
                    if msg.get("type") == "ping":
                        await self.ws.send(json.dumps({
                            "type": "pong",
                            "ts": msg.get("ts"),
                        }))
                        continue
                    if msg.get("type") == "helloAck":
                        continue
                    self.received.append(msg)
                    await self._inbox.put(msg)
        except websockets.exceptions.ConnectionClosed:
            pass

    async def next_command(self, *, timeout: float = 2.0) -> dict:
        return await asyncio.wait_for(self._inbox.get(), timeout=timeout)

    async def respond(
        self, cmd_id: int, *, result: dict | None = None,
        error: dict | None = None,
    ) -> None:
        assert self.ws is not None
        msg: dict = {"type": "response", "id": cmd_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result or {}
        await self.ws.send(json.dumps(msg))

    async def push_event(
        self, *, tab_id: int, method: str, params: dict | None = None,
    ) -> None:
        assert self.ws is not None
        await self.ws.send(json.dumps({
            "type": "event",
            "tabId": tab_id,
            "method": method,
            "params": params or {},
        }))

    async def announce_attached(
        self, *, tab_id: int, url: str = "https://x/", title: str = "x",
    ) -> None:
        assert self.ws is not None
        await self.ws.send(json.dumps({
            "type": "attached",
            "tabId": tab_id,
            "targetInfo": {"url": url, "title": title},
        }))

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        if self.recv_task is not None:
            self.recv_task.cancel()

    async def wait_closed(self, *, timeout: float = 2.0) -> None:
        assert self.ws is not None
        await asyncio.wait_for(self.ws.wait_closed(), timeout=timeout)


# ---- helper: build a wired-up ExtensionUpstream -------------------------


@asynccontextmanager
async def _ext_upstream(
) -> AsyncIterator[tuple[RelayServer, ExtensionUpstream, list[dict], _MockExtension]]:
    """Yields (relay, upstream, captured_downstream_frames, mock_extension).

    `captured_downstream_frames` is what the upstream's `on_frame` callback
    received — i.e., everything the daemon would relay back to a client.
    """
    captured: list[dict] = []

    async def on_frame(text: str) -> None:
        captured.append(json.loads(text))

    async def on_close(reason: str) -> None:
        pass

    async with _relay_running() as relay:
        ext = _MockExtension()
        await ext.connect(relay.port)
        await relay.wait_ready(timeout=2.0)
        upstream = ExtensionUpstream(relay, on_frame, on_close)
        await upstream.open(timeout=2.0)
        try:
            yield relay, upstream, captured, ext
        finally:
            await upstream.close()
            await ext.close()


# ---- naming helpers (low-level) ------------------------------------------


def test_tab_id_extraction_helpers():
    assert _tab_id_from_target_id("ext-tab-42") == 42
    assert _tab_id_from_target_id("ext-tab-bad") is None
    assert _tab_id_from_target_id("FAKE-T") is None
    assert _tab_id_from_session_id("ext-sid-99-AABBCC") == 99
    assert _tab_id_from_session_id("regular-session") is None


# ---- Target.* interception ------------------------------------------------


@pytest.mark.asyncio
async def test_target_get_targets_returns_ghosts_from_relay():
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await ext.announce_attached(tab_id=1, url="https://a/", title="A")
        await ext.announce_attached(tab_id=2, url="https://b/", title="B")
        # Give the relay a tick to ingest the announcements.
        await asyncio.sleep(0.05)

        await upstream.send_text(json.dumps({
            "id": 100, "method": "Target.getTargets",
        }))
        # The interception produces an immediate downstream frame.
        assert len(captured) == 1
        resp = captured[0]
        assert resp["id"] == 100
        target_ids = {ti["targetId"] for ti in resp["result"]["targetInfos"]}
        assert target_ids == {"ext-tab-1", "ext-tab-2"}


@pytest.mark.asyncio
async def test_stale_extension_connection_is_closed_and_request_retries(monkeypatch):
    async with _relay_running() as relay:
        monkeypatch.setattr("browserwright.daemon.server.relay.STALE_FRAME_AFTER", 0.01)
        monkeypatch.setattr("browserwright.daemon.server.relay.RECONNECT_WAIT_TIMEOUT", 2.0)
        ext1 = _MockExtension()
        await ext1.connect(relay.port, install_id="same-ext")
        await relay.wait_ready(timeout=2.0)
        await asyncio.sleep(0.03)

        async def reconnect():
            await ext1.wait_closed()
            ext2 = _MockExtension()
            await ext2.connect(relay.port, install_id="same-ext")
            cmd = await ext2.next_command()
            assert cmd["type"] == "queryActiveTab"
            await ext2.respond(cmd["id"], result={"tabId": 9, "url": "https://fresh/"})
            return ext2

        reconnect_task = asyncio.create_task(reconnect())
        result = await relay.query_active_tab(timeout=1.0)
        ext2 = await reconnect_task
        try:
            assert result == {"tabId": 9, "url": "https://fresh/"}
            assert ext1.ws is not None and ext1.ws.close_code is not None
            active = relay._pick_active_extension()
            assert active is not None
            assert active.install_id == "same-ext"
        finally:
            await ext2.close()
            await ext1.close()


@pytest.mark.asyncio
async def test_live_extension_request_timeout_does_not_retry(monkeypatch):
    async with _relay_running() as relay:
        monkeypatch.setattr("browserwright.daemon.server.relay.RECONNECT_WAIT_TIMEOUT", 0.2)
        ext = _MockExtension()
        await ext.connect(relay.port, install_id="live-ext")
        await relay.wait_ready(timeout=2.0)

        try:
            with pytest.raises(asyncio.TimeoutError):
                await relay.query_active_tab(timeout=0.01)
            await asyncio.sleep(0.05)
            assert len([
                msg for msg in ext.received
                if msg.get("type") == "queryActiveTab"
            ]) == 1
            active = relay._pick_active_extension()
            assert active is not None
            assert active.install_id == "live-ext"
        finally:
            await ext.close()


@pytest.mark.asyncio
async def test_target_attach_via_extension_returns_synthetic_session_id():
    async with _ext_upstream() as (relay, upstream, captured, ext):
        async def respond_attach():
            cmd = await ext.next_command()
            assert cmd["type"] == "attach"
            assert cmd["tabId"] == 7
            await ext.respond(cmd["id"], result={
                "targetInfo": {"url": "https://x/", "title": "X"},
            })

        responder = asyncio.create_task(respond_attach())
        await upstream.send_text(json.dumps({
            "id": 1, "method": "Target.attachToTarget",
            "params": {"targetId": "ext-tab-7"},
        }))
        await responder

        # Find the response (might not be at [0] if other things happened).
        resps = [m for m in captured if m.get("id") == 1]
        assert len(resps) == 1
        sid = resps[0]["result"]["sessionId"]
        assert sid.startswith("ext-sid-7-")


@pytest.mark.asyncio
async def test_target_attach_unknown_target_id_returns_invalid_params():
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await upstream.send_text(json.dumps({
            "id": 1, "method": "Target.attachToTarget",
            "params": {"targetId": "ext-tab-not-a-number"},
        }))
        err = [m for m in captured if m.get("id") == 1][0]
        assert err["error"]["code"] == -32602


# ---- Browser.* unsupported -----------------------------------------------


@pytest.mark.asyncio
async def test_browser_crash_returns_method_not_found():
    """Spec §8.4: unsupported browser-level commands → -32601 with
    'method not implemented in extension backend'."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await upstream.send_text(json.dumps({
            "id": 1, "method": "Browser.crash",
        }))
        err = [m for m in captured if m.get("id") == 1][0]
        assert err["error"]["code"] == -32601
        assert "extension backend" in err["error"]["message"]


@pytest.mark.asyncio
async def test_target_set_discover_targets_is_silent_ack():
    """The daemon's own setDiscoverTargets call during ensure_open must not
    crash on extension backend — we silently ack since the extension pushes
    ghost targets via its own `attached`/`detached` event stream."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await upstream.send_text(json.dumps({
            "id": 1, "method": "Target.setDiscoverTargets",
            "params": {"discover": True},
        }))
        resp = [m for m in captured if m.get("id") == 1][0]
        assert resp["result"] == {}


# ---- session-scoped CDP forwarding ---------------------------------------


@pytest.mark.asyncio
async def test_session_scoped_command_forwarded_via_relay():
    """After Target.attachToTarget gives us a synthetic sessionId, sending a
    Page.navigate on that session translates to relay.send_cdp(tabId, ...)."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        # Step 1: attach via the upstream so we have a registered sessionId.
        async def respond_attach():
            cmd = await ext.next_command()
            await ext.respond(cmd["id"], result={
                "targetInfo": {"url": "https://x/", "title": "X"},
            })

        a = asyncio.create_task(respond_attach())
        await upstream.send_text(json.dumps({
            "id": 1, "method": "Target.attachToTarget",
            "params": {"targetId": "ext-tab-5"},
        }))
        await a
        attach_resp = [m for m in captured if m.get("id") == 1][0]
        sid = attach_resp["result"]["sessionId"]

        # Step 2: send a session-scoped command and expect the relay to
        # see a "command" frame for tab 5.
        async def respond_navigate():
            cmd = await ext.next_command()
            assert cmd["type"] == "command"
            assert cmd["tabId"] == 5
            assert cmd["method"] == "Page.navigate"
            await ext.respond(cmd["id"], result={"frameId": "F-100"})

        r = asyncio.create_task(respond_navigate())
        await upstream.send_text(json.dumps({
            "id": 2, "method": "Page.navigate",
            "sessionId": sid,
            "params": {"url": "https://destination/"},
        }))
        await r

        nav_resp = [m for m in captured if m.get("id") == 2][0]
        assert nav_resp["result"] == {"frameId": "F-100"}


@pytest.mark.asyncio
async def test_session_scoped_command_with_unknown_session_id_errors():
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await upstream.send_text(json.dumps({
            "id": 1, "method": "Page.navigate",
            "sessionId": "totally-unknown",
            "params": {"url": "https://x/"},
        }))
        err = [m for m in captured if m.get("id") == 1][0]
        assert err["error"]["code"] == -32602


# ---- attach_active_tab (v0.5.4) ------------------------------------------


@pytest.mark.asyncio
async def test_attach_active_tab_returns_fabricated_session_id():
    """ExtensionUpstream.attach_active_tab() drives the relay's
    attach_active_tab and synthesises a sessionId in the same shape as
    Target.attachToTarget would. The session is registered in
    upstream._sessions so subsequent session-scoped commands route through
    the relay for the same tab."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        async def respond_attach_active():
            cmd = await ext.next_command()
            assert cmd["type"] == "attachActive"
            await ext.respond(cmd["id"], result={
                "tabId": 99,
                "url": "https://active.example/",
                "title": "Active",
            })

        r = asyncio.create_task(respond_attach_active())
        info = await upstream.attach_active_tab()
        await r

        assert info["tabId"] == 99
        assert info["url"] == "https://active.example/"
        assert info["title"] == "Active"
        assert info["targetId"] == "ext-tab-99"
        sid = info["sessionId"]
        assert sid.startswith("ext-sid-99-")
        # The session is registered so a follow-up Page.navigate routes via
        # the relay to tabId=99 (matches the standard attach behaviour).
        assert upstream._sessions.get(sid) == 99


# ---- event push-back through on_frame ------------------------------------


@pytest.mark.asyncio
async def test_extension_event_translated_to_cdp_frame_with_session_id():
    """When the extension pushes a `{"type":"event",...}` for a tab we have
    an open session on, ExtensionUpstream surfaces it as a CDP event with
    our synthetic sessionId substituted in.
    """
    async with _ext_upstream() as (relay, upstream, captured, ext):
        # Attach session.
        async def respond_attach():
            cmd = await ext.next_command()
            await ext.respond(cmd["id"], result={
                "targetInfo": {"url": "https://x/", "title": "X"},
            })

        a = asyncio.create_task(respond_attach())
        await upstream.send_text(json.dumps({
            "id": 1, "method": "Target.attachToTarget",
            "params": {"targetId": "ext-tab-9"},
        }))
        await a
        sid = [m for m in captured if m.get("id") == 1][0]["result"]["sessionId"]
        captured.clear()

        # Push an event.
        await ext.push_event(
            tab_id=9, method="Page.loadEventFired",
            params={"timestamp": 12.34})
        # Tick.
        for _ in range(20):
            if captured:
                break
            await asyncio.sleep(0.02)

        assert len(captured) == 1
        evt = captured[0]
        assert evt["method"] == "Page.loadEventFired"
        assert evt["sessionId"] == sid
        assert evt["params"]["timestamp"] == 12.34


# ---- Phase B: open_background_tab + close_tab ------------------------------


@pytest.mark.asyncio
async def test_open_background_tab_returns_fabricated_session():
    """upstream.open_background_tab fans out to relay.create_background_tab
    and surfaces the synthetic sessionId + tabId + groupId."""
    async with _ext_upstream() as (relay, upstream, captured, ext):

        async def respond():
            cmd = await ext.next_command()
            assert cmd["type"] == "createTab"
            assert cmd["url"] == "https://example.com/"
            assert cmd["groupName"] == "Agent"
            assert "skipPostAttachCommands" not in cmd
            await ext.respond(cmd["id"], result={
                "tabId": 12,
                "url": "https://example.com/",
                "title": "Example",
                "groupId": 4,
            })

        r = asyncio.create_task(respond())
        result = await upstream.open_background_tab(
            "https://example.com/", group_name="Agent")
        await r

        assert result["targetId"] == "ext-tab-12"
        assert result["tabId"] == 12
        assert result["url"] == "https://example.com/"
        assert result["title"] == "Example"
        assert result["groupId"] == 4
        sid = result["sessionId"]
        assert sid.startswith("ext-sid-12-")
        # Session is registered for future relay routing.
        assert upstream._sessions[sid] == 12


@pytest.mark.asyncio
async def test_open_background_tab_can_skip_post_attach_commands():
    async with _ext_upstream() as (_relay, upstream, _captured, ext):

        async def respond():
            cmd = await ext.next_command()
            assert cmd["type"] == "createTab"
            assert cmd["skipPostAttachCommands"] is True
            await ext.respond(cmd["id"], result={
                "tabId": 12,
                "url": "https://example.com/",
                "title": "Example",
                "groupId": 4,
            })

        r = asyncio.create_task(respond())
        await upstream.open_background_tab(
            "https://example.com/",
            group_name="Agent",
            skip_post_attach_commands=True,
        )
        await r


@pytest.mark.asyncio
async def test_close_tab_removes_session():
    """upstream.close_tab pops the upstream sessionId mapping after the relay
    has confirmed the chrome.tabs.remove call."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        # First open a background tab so we have a sessionId to close.
        async def respond_create():
            cmd = await ext.next_command()
            await ext.respond(cmd["id"], result={
                "tabId": 21, "url": "https://x/", "title": "x", "groupId": 1,
            })

        c = asyncio.create_task(respond_create())
        opened = await upstream.open_background_tab("https://x/", group_name="Agent")
        await c
        sid = opened["sessionId"]
        assert sid in upstream._sessions

        async def respond_close():
            cmd = await ext.next_command()
            assert cmd["type"] == "closeTab"
            assert cmd["tabId"] == 21
            await ext.respond(cmd["id"], result={"ok": True, "tabId": 21})

        cl = asyncio.create_task(respond_close())
        result = await upstream.close_tab(sid)
        await cl

        assert result == {"ok": True, "tabId": 21}
        assert sid not in upstream._sessions


@pytest.mark.asyncio
async def test_close_tab_unknown_session_raises_value_error():
    async with _ext_upstream() as (relay, upstream, captured, ext):
        with pytest.raises(ValueError):
            await upstream.close_tab("not-a-real-session-id")


# ---- tab-group = browser: groupId binding + close-whole-group -------------


async def _open_in_group(upstream, ext, url, tab_id, group_id, session_id):
    async def respond():
        cmd = await ext.next_command()
        await ext.respond(cmd["id"], result={
            "tabId": tab_id, "url": url, "title": "t", "groupId": group_id,
        })
    r = asyncio.create_task(respond())
    out = await upstream.open_background_tab(url, group_name="Agent",
                                            session_id=session_id)
    await r
    return out


@pytest.mark.asyncio
async def test_sessions_bind_distinct_groups():
    """Each session binds to its own durable groupId (the session = group);
    there is no _owned/_borrowed bookkeeping anymore."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await _open_in_group(upstream, ext, "https://a/", 10, 100, "A")
        await _open_in_group(upstream, ext, "https://b/", 20, 200, "B")
        assert upstream._groups["A"] == 100
        assert upstream._groups["B"] == 200
        # owned/borrowed sets are gone entirely.
        assert not hasattr(upstream, "_owned")
        assert not hasattr(upstream, "_borrowed")


@pytest.mark.asyncio
async def test_open_background_passes_bound_group_id():
    """Once a session has a bound groupId, the next open passes it back to the
    extension as the durable key (so the same group is reused)."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await _open_in_group(upstream, ext, "https://a/", 10, 100, "A")

        async def respond():
            cmd = await ext.next_command()
            assert cmd["type"] == "createTab"
            assert cmd["groupId"] == 100  # the bound id is threaded through
            await ext.respond(cmd["id"], result={
                "tabId": 11, "url": "https://a2/", "title": "t", "groupId": 100})
        r = asyncio.create_task(respond())
        out = await upstream.open_background_tab(
            "https://a2/", group_name="Agent", session_id="A")
        await r
        assert out["groupId"] == 100


@pytest.mark.asyncio
async def test_attach_active_adopts_into_group():
    """attach_active = adopt: the focused tab is moved into the session's group
    and the returned groupId is bound to the session (no borrowed flag)."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        async def respond_attach():
            cmd = await ext.next_command()
            assert cmd["type"] == "attachActive"
            await ext.respond(cmd["id"], result={
                "tabId": 77, "url": "https://focused/", "title": "F",
                "groupId": 55})
        r = asyncio.create_task(respond_attach())
        info = await upstream.attach_active_tab(session_id="A", group_name="A")
        await r
        assert info["tabId"] == 77
        assert info["groupId"] == 55
        assert upstream._groups["A"] == 55


@pytest.mark.asyncio
async def test_attach_active_passes_bound_group():
    """When the session already has a bound groupId, attach_active threads it
    to the extension so the focused tab joins the same group."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await _open_in_group(upstream, ext, "https://a/", 10, 100, "A")

        async def respond_attach():
            cmd = await ext.next_command()
            assert cmd["type"] == "attachActive"
            assert cmd["groupId"] == 100
            await ext.respond(cmd["id"], result={
                "tabId": 12, "url": "https://f/", "title": "f", "groupId": 100})
        r = asyncio.create_task(respond_attach())
        await upstream.attach_active_tab(session_id="A", group_name="A")
        await r


@pytest.mark.asyncio
async def test_end_session_closes_whole_group():
    """DECIDED: end_session closes the WHOLE group — every member tab resolved
    from live membership — and `kept` is always empty (no borrowed)."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await _open_in_group(upstream, ext, "https://a/", 30, 300, "A")

        async def responder():
            # end_session re-resolves live membership via queryGroup, then
            # closes each member.
            q = await ext.next_command()
            assert q["type"] == "queryGroup"
            assert q["groupId"] == 300  # bound id is the primary key
            await ext.respond(q["id"], result={
                "groupId": 300,
                "tabs": [
                    {"tabId": 30, "url": "https://a/", "title": "t",
                     "active": True, "lastAccessed": 2},
                    {"tabId": 31, "url": "https://b/", "title": "t",
                     "active": False, "lastAccessed": 1},
                ],
            })
            for _ in range(2):
                c = await ext.next_command()
                assert c["type"] == "closeTab"
                await ext.respond(c["id"], result={"ok": True, "tabId": c["tabId"]})

        r = asyncio.create_task(responder())
        result = await upstream.end_session("A")  # group 300 is bound to "A"
        await r

        assert sorted(result["closed"]) == [30, 31]
        assert result["kept"] == []
        assert "A" not in upstream._groups


# ---- session-reconnect-recovery ------------------------------------------


@pytest.mark.asyncio
async def test_recover_session_reattaches_tabs_and_rebuilds_state():
    """recover_session queries the group, re-attaches each tab, rebuilds
    _sessions/_owned/_groups, and returns the representative target."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        async def responder():
            # 1) queryGroup
            q = await ext.next_command()
            assert q["type"] == "queryGroup"
            assert q["groupId"] == 9  # recover by the persisted groupId, not the title
            await ext.respond(q["id"], result={
                "groupId": 9,
                "tabs": [
                    {"tabId": 50, "url": "https://a/", "title": "A",
                     "active": False, "lastAccessed": 100},
                    {"tabId": 51, "url": "https://b/", "title": "B",
                     "active": True, "lastAccessed": 300},
                ],
            })
            # 2) attach for each tab (order may vary; just ack twice).
            for _ in range(2):
                a = await ext.next_command()
                assert a["type"] == "attach"
                await ext.respond(a["id"], result={
                    "targetInfo": {"url": "", "title": ""}})

        r = asyncio.create_task(responder())
        result = await upstream.recover_session("bs-session-1", group_id=9)
        await r

        assert sorted(result["recovered"]) == [50, 51]
        assert result["groupId"] == 9
        # representative = max lastAccessed = tab 51
        assert result["tabId"] == 51
        assert result["targetId"] == "ext-tab-51"
        assert result["sessionId"].startswith("ext-sid-51-")
        # state rebuilt: the session is re-bound to the recovered groupId and
        # each tab gets a fabricated CDP session (membership is the truth).
        assert upstream._groups["bs-session-1"] == 9
        assert set(upstream._sessions.values()) >= {50, 51}


@pytest.mark.asyncio
async def test_recover_session_empty_group_raises():
    async with _ext_upstream() as (relay, upstream, captured, ext):
        async def responder():
            q = await ext.next_command()
            await ext.respond(q["id"], result={"groupId": -1, "tabs": []})

        r = asyncio.create_task(responder())
        with pytest.raises((RuntimeError, ValueError)):
            await upstream.recover_session("bs-x", group_id=999)
        await r


@pytest.mark.asyncio
async def test_end_session_resolves_group_by_passed_id_when_unbound():
    """end_session always resolves membership from the live group; when the
    session has no bound groupId in memory (e.g. after a daemon restart), the
    persisted numeric groupId passed in is the key used to find + close the tabs
    — never the title (names aren't unique)."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        async def responder():
            q = await ext.next_command()
            assert q["type"] == "queryGroup"
            assert q["groupId"] == 3  # the passed persisted id, not a title
            await ext.respond(q["id"], result={
                "groupId": 3,
                "tabs": [
                    {"tabId": 60, "url": "https://x/", "title": "x",
                     "active": True, "lastAccessed": 1},
                ],
            })
            c = await ext.next_command()
            assert c["type"] == "closeTab"
            assert c["tabId"] == 60
            await ext.respond(c["id"], result={"ok": True, "tabId": 60})

        r = asyncio.create_task(responder())
        result = await upstream.end_session("never-tracked", group_id=3)
        await r
        assert result["closed"] == [60]


@pytest.mark.asyncio
async def test_session_info_live_fields():
    """session_info reports the session's bound group id + a sample url. The
    authoritative tab membership comes from list_tabs (live group query); this
    synchronous view is best-effort for whoami diagnostics."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await _open_in_group(upstream, ext, "https://owned/", 40, 400, "A")
        info = upstream.session_info("A")
        assert info["group_id"] == 400
        assert info["sample_url"] == "https://owned/"
        assert "owned_tabs" not in info
        assert "borrowed_tabs" not in info


@pytest.mark.asyncio
async def test_list_tabs_resolves_from_live_group_membership():
    """list_tabs is the single source of truth: it returns whatever the live
    group currently contains (groupId-keyed), including tabs the user dragged
    in — not an in-memory owned/borrowed set."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await _open_in_group(upstream, ext, "https://a/", 70, 700, "A")

        async def responder():
            q = await ext.next_command()
            assert q["type"] == "queryGroup"
            assert q["groupId"] == 700
            await ext.respond(q["id"], result={
                "groupId": 700,
                "tabs": [
                    {"tabId": 70, "url": "https://a/", "title": "A",
                     "active": True, "lastAccessed": 5},
                    # A tab the user dragged into the group — visible despite
                    # never being opened by the agent.
                    {"tabId": 71, "url": "https://dragged-in/", "title": "D",
                     "active": False, "lastAccessed": 4},
                ],
            })

        r = asyncio.create_task(responder())
        out = await upstream.list_tabs(session_id="A")
        await r
        assert out["groupId"] == 700
        ids = {t["tabId"] for t in out["tabs"]}
        assert ids == {70, 71}


@pytest.mark.asyncio
async def test_scoped_target_infos_filters_ghosts_to_session_group():
    """#2: scoped_target_infos returns CDP target infos only for ghosts whose
    tab is a live member of the session's group — another session's attached
    tab (ext-tab-2) is excluded. This is the source-of-truth filter that makes
    sessions mutually invisible at enumeration."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await ext.announce_attached(tab_id=1, url="https://a/", title="A")
        await ext.announce_attached(tab_id=2, url="https://b/", title="B")
        await asyncio.sleep(0.05)

        upstream._bind_group("A", 100)  # session A's browser = groupId 100

        async def respond_query():
            cmd = await ext.next_command()
            assert cmd["type"] == "queryGroup"
            assert cmd.get("groupId") == 100  # scoped by groupId, no title
            await ext.respond(cmd["id"], result={
                "groupId": 100,
                "tabs": [{"tabId": 1, "url": "https://a/", "title": "A",
                          "active": True, "lastAccessed": 1}],
            })

        r = asyncio.create_task(respond_query())
        infos = await upstream.scoped_target_infos(session_id="A")
        await r
        assert {ti["targetId"] for ti in infos} == {"ext-tab-1"}
