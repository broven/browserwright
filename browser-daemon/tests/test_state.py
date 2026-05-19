"""DaemonState transitions + heuristic active-tab table.

These are pure-Python unit tests — no ws, no Chrome, no asyncio. The state
object exists exactly because spec §8.5 says it should be testable without
any of those things.
"""
from __future__ import annotations

import time

import pytest

from browser_daemon.server.state import DaemonState, UpstreamPhase


def _state() -> DaemonState:
    return DaemonState(name="t", backend_name="rdp")


# ---- upstream phase transitions -------------------------------------------


@pytest.mark.asyncio
async def test_initial_phase_is_disconnected():
    s = _state()
    assert s.upstream_phase == UpstreamPhase.DISCONNECTED
    assert s.upstream_ws_url is None


@pytest.mark.asyncio
async def test_connecting_to_connected_records_ws_url():
    s = _state()
    await s.begin_connecting("rdp")
    assert s.upstream_phase == UpstreamPhase.CONNECTING

    await s.set_connected("ws://chrome/x", was_popup=False)
    assert s.upstream_phase == UpstreamPhase.CONNECTED
    assert s.upstream_ws_url == "ws://chrome/x"
    assert s.last_popup_resolved_at is None  # was_popup=False


@pytest.mark.asyncio
async def test_set_connected_with_popup_records_resolved_at():
    s = _state()
    await s.begin_connecting("rdp")
    before = time.time()
    await s.set_connected("ws://chrome/x", was_popup=True)
    assert s.last_popup_resolved_at is not None
    assert s.last_popup_resolved_at >= before


@pytest.mark.asyncio
async def test_close_cycle_resets_to_disconnected():
    s = _state()
    await s.begin_connecting("rdp")
    await s.set_connected("ws://x", was_popup=False)
    await s.begin_closing("skill_disconnect")
    assert s.upstream_phase == UpstreamPhase.CLOSING
    assert s.last_close_reason == "skill_disconnect"
    await s.set_disconnected()
    assert s.upstream_phase == UpstreamPhase.DISCONNECTED
    assert s.upstream_ws_url is None


# ---- observer notifications ------------------------------------------------


@pytest.mark.asyncio
async def test_subscribers_receive_transitions():
    s = _state()
    events = []

    async def observer(name, payload):
        events.append((name, dict(payload)))

    s.subscribe(observer)
    await s.begin_connecting("rdp")
    await s.set_connected("ws://x", was_popup=False)
    await s.begin_closing("daemon_shutdown")
    await s.set_disconnected()
    names = [e[0] for e in events]
    assert names == ["upstream.connecting", "upstream.ready",
                     "upstream.closing", "upstream.disconnected"]


@pytest.mark.asyncio
async def test_crashing_observer_does_not_corrupt_state():
    """A bug in one observer must not stop transitions or other observers."""
    s = _state()
    good_events = []

    async def crashing(name, payload):
        raise RuntimeError("boom")

    async def good(name, payload):
        good_events.append(name)

    s.subscribe(crashing)
    s.subscribe(good)
    await s.begin_connecting("rdp")
    assert s.upstream_phase == UpstreamPhase.CONNECTING
    assert good_events == ["upstream.connecting"]


# ---- active-tab heuristic --------------------------------------------------


def test_best_active_tab_picks_most_recently_activated_page():
    s = _state()
    s.note_target_info({"targetId": "OLD", "type": "page", "url": "https://a/", "title": "A"})
    s.note_target_info({"targetId": "NEW", "type": "page", "url": "https://b/", "title": "B"})
    s.last_activated_at["OLD"] = time.time() - 60
    s.last_activated_at["NEW"] = time.time() - 1
    pick = s.best_active_tab()
    assert pick is not None
    assert pick["targetId"] == "NEW"
    assert pick["accuracy"] == "heuristic-recent-activate"
    assert pick["since_seconds"] is not None and 0 < pick["since_seconds"] < 60


def test_best_active_tab_filters_internal_urls():
    s = _state()
    s.note_target_info({"targetId": "X", "type": "page",
                        "url": "chrome://newtab/", "title": ""})
    s.last_activated_at["X"] = time.time()
    assert s.best_active_tab() is None


def test_best_active_tab_filters_non_page_types():
    s = _state()
    s.note_target_info({"targetId": "EXT", "type": "background_page",
                        "url": "https://example.com/", "title": "ext"})
    s.last_activated_at["EXT"] = time.time()
    assert s.best_active_tab() is None


def test_best_active_tab_returns_none_when_nothing():
    s = _state()
    assert s.best_active_tab() is None


def test_note_target_destroyed_removes_from_table():
    s = _state()
    s.note_target_info({"targetId": "T", "type": "page", "url": "https://a/", "title": ""})
    s.last_activated_at["T"] = time.time()
    assert s.best_active_tab() is not None
    s.note_target_destroyed("T")
    assert s.best_active_tab() is None


# ---- client allocation -----------------------------------------------------


def test_allocate_and_release_client():
    """v0.3: many clients in `state.clients`. The legacy `state.client`
    property still works when exactly one is connected."""
    s = _state()
    assert s.client is None and s.clients == {}
    c1 = s.allocate_client("skill-repl")
    assert s.clients[c1.client_id] is c1
    assert s.client is c1  # legacy convenience: 1 client → exposed
    assert c1.client_id == 1
    released = s.release_client(c1.client_id)
    assert released is c1
    assert s.clients == {}
    c2 = s.allocate_client("skill-repl")
    assert c2.client_id == 2  # monotonic across release


def test_multi_client_state_client_returns_none():
    """When >1 client connected, the legacy `state.client` is None
    (it would otherwise be ambiguous which to return)."""
    s = _state()
    s.allocate_client("a")
    s.allocate_client("b")
    assert s.client is None  # ambiguous
    assert len(s.clients) == 2


def test_release_only_drops_specified_client():
    s = _state()
    a = s.allocate_client("a")
    b = s.allocate_client("b")
    s.release_client(a.client_id)
    assert a.client_id not in s.clients
    assert s.clients[b.client_id] is b
