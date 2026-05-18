"""active-tab — H8/US1 Mode A path: pick most-recent eligible page target."""
from __future__ import annotations

import time

import pytest

import browser_daemon.active_tab as at_mod
from browser_daemon.config import load


async def _stub_resolve(cfg):
    from browser_daemon.backends.base import ResolveResult
    return ResolveResult(ws_url="ws://stub/", backend="rdp")


@pytest.mark.asyncio
async def test_picks_most_recent_eligible_page(monkeypatch):
    now_ms = time.time() * 1000.0
    targets = [
        {"type": "page", "url": "https://old.example/",  "title": "old",
         "targetId": "OLD", "lastAccessed": now_ms - 60_000},
        {"type": "page", "url": "https://new.example/",  "title": "new",
         "targetId": "NEW", "lastAccessed": now_ms - 2_000},
        {"type": "background_page", "url": "chrome-extension://abc/bg.html",
         "title": "ext", "targetId": "EXT", "lastAccessed": now_ms},
        {"type": "page", "url": "chrome://newtab/", "title": "new tab",
         "targetId": "CHROME", "lastAccessed": now_ms - 1_000},
    ]

    async def fake_fetch(ws_url, timeout): return targets

    monkeypatch.setattr(at_mod, "resolve", _stub_resolve)
    monkeypatch.setattr(at_mod, "_fetch_targets", fake_fetch)

    info = await at_mod.active_tab(load(env={}))
    assert info is not None
    assert info["targetId"] == "NEW"
    assert info["accuracy"] == "heuristic-recent-activate"
    assert info["since_seconds"] is not None
    assert 0 < info["since_seconds"] < 60


@pytest.mark.asyncio
async def test_no_eligible_page_returns_none(monkeypatch):
    """Only chrome:// + extensions — no real page open. spec §5.4: empty
    output + exit 2 is the contract; the function-level signal is None."""
    targets = [
        {"type": "page", "url": "chrome://newtab/", "title": "newtab",
         "targetId": "N", "lastAccessed": 1},
        {"type": "service_worker", "url": "https://example.com/sw.js",
         "title": "", "targetId": "SW", "lastAccessed": 2},
    ]

    async def fake_fetch(ws_url, timeout): return targets

    monkeypatch.setattr(at_mod, "resolve", _stub_resolve)
    monkeypatch.setattr(at_mod, "_fetch_targets", fake_fetch)

    info = await at_mod.active_tab(load(env={}))
    assert info is None


@pytest.mark.asyncio
async def test_missing_lastaccessed_falls_back_to_registry_order(monkeypatch):
    """Older Chrome builds omit `lastAccessed`. We sort missing-to-the-bottom;
    when ALL miss it, registry order wins."""
    targets = [
        {"type": "page", "url": "https://a.example/", "title": "a", "targetId": "A"},
        {"type": "page", "url": "https://b.example/", "title": "b", "targetId": "B"},
    ]

    async def fake_fetch(ws_url, timeout): return targets

    monkeypatch.setattr(at_mod, "resolve", _stub_resolve)
    monkeypatch.setattr(at_mod, "_fetch_targets", fake_fetch)

    info = await at_mod.active_tab(load(env={}))
    assert info["targetId"] == "A"
    assert info["since_seconds"] is None
