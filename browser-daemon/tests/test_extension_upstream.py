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

from browser_daemon.server.extension_upstream import (
    ExtensionUpstream, _tab_id_from_session_id, _tab_id_from_target_id,
)
from browser_daemon.server.relay import RelayServer

from tests.test_relay import _MockExtension, _relay_running  # type: ignore


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


# ---- P5: per-session ownership + end_session cleanup ----------------------


async def _open_owned(upstream, ext, url, tab_id, group_id, session_id):
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
async def test_sessions_own_distinct_groups_and_tabs():
    """P5.1/5.2: two sessions get distinct groups; each owns its own tab."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await _open_owned(upstream, ext, "https://a/", 10, 100, "A")
        await _open_owned(upstream, ext, "https://b/", 20, 200, "B")
        assert upstream._owned["A"] == {10}
        assert upstream._owned["B"] == {20}
        assert upstream._groups["A"] == 100
        assert upstream._groups["B"] == 200
        # session B can't see session A's tab
        assert 10 not in upstream._owned["B"]


@pytest.mark.asyncio
async def test_attach_active_records_borrowed_not_owned():
    """P5.3: an attached focused tab is borrowed, not owned."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        async def respond_attach():
            cmd = await ext.next_command()
            await ext.respond(cmd["id"], result={
                "tabId": 77, "url": "https://focused/", "title": "F"})
        r = asyncio.create_task(respond_attach())
        await upstream.attach_active_tab(session_id="A")
        await r
        assert upstream._borrowed["A"] == {77}
        assert 77 not in upstream._owned.get("A", set())


@pytest.mark.asyncio
async def test_end_session_closes_owned_keeps_borrowed():
    """P5.4: end_session closes owned tabs, keeps borrowed ones."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await _open_owned(upstream, ext, "https://owned/", 30, 300, "A")

        async def respond_attach():
            cmd = await ext.next_command()
            await ext.respond(cmd["id"], result={
                "tabId": 88, "url": "https://borrowed/", "title": "B"})
        r = asyncio.create_task(respond_attach())
        await upstream.attach_active_tab(session_id="A")
        await r

        async def respond_close():
            cmd = await ext.next_command()
            assert cmd["type"] == "closeTab"
            assert cmd["tabId"] == 30
            await ext.respond(cmd["id"], result={"ok": True, "tabId": 30})
        c = asyncio.create_task(respond_close())
        result = await upstream.end_session("A")
        await c

        assert result == {"closed": [30], "kept": [88]}
        # tracking cleared
        assert "A" not in upstream._owned
        assert "A" not in upstream._borrowed


@pytest.mark.asyncio
async def test_session_info_live_fields():
    """P5.5: session_info reports group id, owned/borrowed counts, sample url."""
    async with _ext_upstream() as (relay, upstream, captured, ext):
        await _open_owned(upstream, ext, "https://owned/", 40, 400, "A")
        info = upstream.session_info("A")
        assert info["group_id"] == 400
        assert info["owned_tabs"] == 1
        assert info["borrowed_tabs"] == 0
        assert info["sample_url"] == "https://owned/"
