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
