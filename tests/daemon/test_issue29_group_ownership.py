"""Issue #29 — extension group ownership has no durable anchor.

Spec-shaped repro tests for the two failure directions of the old
title+groupId heuristic. Written as the post-fix contract, so they are RED
against the current code:

1. **False negative** — a user renaming a legitimate group must not wedge
   recovery or teardown forever. The current code rejects the group because
   the title no longer matches the session name.
2. **False positive** — after a browser restart group/tab ids are recycled;
   a stale ledger groupId can name the user's own unrelated group (or another
   session's). The current code adopts it, attaches its tabs, and
   `endSession` closes every member. Recovery must fail explicitly instead,
   and never adopt or close unproven tabs.

The fix (see docs/plans/issue-29-group-ownership.md) anchors ownership in
extension-maintained per-tab markers (`chrome.storage.session`, keyed by
tabId, value = owning sessionId) written when the extension places a tab in a
session group. Mock relays below answer with that post-fix shape
(`ownedSessionId` per member tab), which is exactly the evidence the fixed
daemon validates and the current daemon ignores.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from browserwright.daemon.server.extension_upstream import ExtensionUpstream


async def _noop(_value):
    return None


def _ledger(session_id: str, *, name: str = "Agent", group_id: int = 100,
            tab_id: int = 10) -> dict:
    return {
        "id": session_id,
        "name": name,
        "runtime": {
            "group_id": group_id,
            "current_target_id": f"ext-tab-{tab_id}",
        },
    }


def _patch_registry(monkeypatch, record: dict, others: list[dict] | None = None):
    monkeypatch.setattr(
        "browserwright.session_registry.get", lambda _sid: record)
    monkeypatch.setattr(
        "browserwright.session_registry.list_all", lambda: others or [])

    def update(_session_id, **fields):
        record.update(fields)
        return record

    monkeypatch.setattr("browserwright.session_registry.update", update)
    return record


# ---------------------------------------------------------------------------
# 1. False negative: user rename wedges recovery and teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_rename_does_not_wedge_recovery(monkeypatch):
    """Daemon restarts, Chrome still running; the user renamed the session's
    group ("Agent" → "My Research"). The groupId is still live and the group
    is still the session's (its tabs carry the session's marker), so recovery
    must reuse it — the title is a user-editable label, never an anchor."""
    calls: list[str] = []

    class _RenamedGroupRelay:
        port = 19989

        def reset_session_announce(self, _session_id):
            return None

        async def query_group_tabs(self, *, group_id, timeout=10.0):
            calls.append("query")
            return {
                "groupId": group_id,
                "groupTitle": "My Research",  # user renamed it
                "tabs": [
                    {"tabId": 10, "url": "https://a/", "title": "t",
                     "ownedSessionId": "A"},
                ],
            }

        async def create_background_tab(self, url, *, group_name, group_id,
                                        background,
                                        skip_post_attach_commands,
                                        expected_generation=None,
                                        session_id=None):
            calls.append(f"create:{group_id}")
            return SimpleNamespace(
                tab_id=11, target_id="ext-tab-11", url=url, title="new",
                group_id=group_id)

    record = _patch_registry(monkeypatch, _ledger("A"))
    upstream = ExtensionUpstream(_RenamedGroupRelay(), _noop, _noop)

    result = await upstream.open_background_tab(
        "https://new/", group_name="Agent", session_id="A")

    # Reused the renamed-but-owned group; did not split into a second one.
    assert calls == ["query", "create:100"]
    assert result["groupId"] == 100
    assert upstream.group_for_session("A") == 100
    assert record["runtime"]["group_id"] == 100


@pytest.mark.asyncio
async def test_user_rename_does_not_wedge_teardown(monkeypatch):
    """endSession after a rename must still find the group via its markers and
    close exactly its members — not fail forever."""
    closed: list[int] = []

    class _RenamedGroupRelay:
        port = 19989

        def reset_session_announce(self, _session_id):
            return None

        async def query_group_tabs(self, *, group_id, timeout=10.0):
            return {
                "groupId": group_id,
                "groupTitle": "My Research",
                "tabs": [
                    {"tabId": 30, "url": "https://a/", "title": "t",
                     "active": True, "lastAccessed": 2,
                     "ownedSessionId": "A"},
                    {"tabId": 31, "url": "https://b/", "title": "t",
                     "active": False, "lastAccessed": 1,
                     "ownedSessionId": "A"},
                ],
            }

        async def close_tab(self, tab_id, *args, **kwargs):
            closed.append(tab_id)
            return {"ok": True, "tabId": tab_id}

    _patch_registry(monkeypatch, _ledger("A"))
    upstream = ExtensionUpstream(_RenamedGroupRelay(), _noop, _noop)

    result = await upstream.end_session("A")

    assert result["ok"] is True
    assert sorted(closed) == [30, 31]
    assert result["closed"] == [30, 31]


# ---------------------------------------------------------------------------
# 2. False positive: recycled ids must never adopt an unrelated group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recycled_group_id_does_not_adopt_user_group(monkeypatch):
    """Browser restarted; group/tab ids recycled. The stale ledger groupId now
    names the user's OWN unrelated group — same default title "Agent", and a
    member tab whose recycled id equals the session's last-known tab id. Every
    old heuristic (title, known tab, cross-session ledger claim) passes, so
    the current code adopts the group and later closes its tabs. The fixed
    code must fail explicitly instead."""
    created = False

    class _UserGroupRelay:
        port = 19989

        def reset_session_announce(self, _session_id):
            return None

        async def query_group_tabs(self, *, group_id, timeout=10.0):
            return {
                "groupId": group_id,
                "groupTitle": "Agent",  # recycled id landed on a user group
                "tabs": [
                    # recycled tab id coincidentally equals the last-known one
                    {"tabId": 10, "url": "https://user/", "title": "user",
                     "ownedSessionId": None},
                ],
            }

        async def create_background_tab(self, *args, **kwargs):
            nonlocal created
            created = True
            raise AssertionError("must never create in an unproven group")

    _patch_registry(monkeypatch, _ledger("A"))
    upstream = ExtensionUpstream(_UserGroupRelay(), _noop, _noop)

    with pytest.raises(RuntimeError, match="re-adopt"):
        await upstream.open_background_tab(
            "https://new/", group_name="Agent", session_id="A")
    assert created is False
    assert upstream.group_for_session("A") is None


@pytest.mark.asyncio
async def test_end_session_never_closes_tabs_of_unproven_group(monkeypatch):
    """The data-loss half of the false positive: endSession must refuse to
    close a group it cannot prove, instead of silently closing the user's
    tabs (current code resolves the recycled group via title+known-tab and
    closes every member)."""
    closed: list[int] = []

    class _UserGroupRelay:
        port = 19989

        def reset_session_announce(self, _session_id):
            return None

        async def query_group_tabs(self, *, group_id, timeout=10.0):
            return {
                "groupId": group_id,
                "groupTitle": "Agent",
                "tabs": [
                    {"tabId": 10, "url": "https://user/", "title": "user",
                     "ownedSessionId": None},
                    {"tabId": 11, "url": "https://user/2", "title": "user",
                     "ownedSessionId": None},
                ],
            }

        async def close_tab(self, tab_id, *args, **kwargs):
            closed.append(tab_id)
            return {"ok": True, "tabId": tab_id}

    _patch_registry(monkeypatch, _ledger("A"))
    upstream = ExtensionUpstream(_UserGroupRelay(), _noop, _noop)

    with pytest.raises(RuntimeError, match="re-adopt"):
        await upstream.end_session("A")
    assert closed == []


@pytest.mark.asyncio
async def test_stale_group_id_does_not_steal_another_sessions_group(
    monkeypatch,
):
    """Browser restarted; session B re-adopted and Chrome recycled the old
    group id to B's NEW live group. B belongs to a different browserwright
    install (separate ledger) sharing the same Chrome profile, so B's claim
    is invisible to this daemon's ledger — only B's per-tab markers prove
    the group. A must refuse: never adopt B's group (cross-session control),
    never close B's tabs."""

    # B's session is NOT in this daemon's ledger — the current code's
    # cross-session ledger check cannot see it, and the title/known-tab
    # heuristic adopts the group (the data-loss path).
    created = False

    class _RecycledCrossSessionRelay:
        port = 19989

        def reset_session_announce(self, _session_id):
            return None

        async def query_group_tabs(self, *, group_id, timeout=10.0):
            return {
                "groupId": group_id,
                "groupTitle": "Agent",
                "tabs": [
                    {"tabId": 10, "url": "https://b/", "title": "b",
                     "ownedSessionId": "B"},
                ],
            }

        async def create_background_tab(self, *args, **kwargs):
            nonlocal created
            created = True
            raise AssertionError("must never adopt another session's group")

    _patch_registry(monkeypatch, _ledger("A"), others=[])
    upstream = ExtensionUpstream(_RecycledCrossSessionRelay(), _noop, _noop)

    with pytest.raises(RuntimeError, match="re-adopt|session"):
        await upstream.open_background_tab(
            "https://new/", group_name="Agent", session_id="A")
    assert created is False
    assert upstream.group_for_session("A") is None


# ---------------------------------------------------------------------------
# 3. The escape hatch: explicit re-adoption, and the self-heal that survives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_active_is_the_explicit_readoption_escape(monkeypatch):
    """When the stale binding is unproven (browser restarted), the explicit
    adopt verb must still work: it falls back to a FRESH group on the focused
    tab instead of joining the unproven (possibly user-owned) group."""
    calls: list[int | None] = []

    class _EscapeRelay:
        port = 19989

        def reset_session_announce(self, _session_id):
            return None

        async def query_group_tabs(self, *, group_id, timeout=10.0):
            return {
                "groupId": group_id,
                "groupTitle": "Agent",
                "tabs": [
                    {"tabId": 10, "url": "https://user/", "title": "user",
                     "ownedSessionId": None},
                ],
            }

        async def attach_active_tab(self, *, group_name, group_id, timeout,
                                    expected_generation=None, session_id=None):
            calls.append(group_id)
            return SimpleNamespace(
                tab_id=11, target_id="ext-tab-11", url="https://active/",
                title="active", group_id=101)

    _patch_registry(monkeypatch, _ledger("A"))
    upstream = ExtensionUpstream(_EscapeRelay(), _noop, _noop)

    result = await upstream.attach_active_tab(session_id="A")

    # group_id=None: a fresh group, never the unproven recycled id.
    assert calls == [None]
    assert result["groupId"] == 101
    assert upstream.group_for_session("A") == 101


@pytest.mark.asyncio
async def test_restart_without_restore_self_heals_with_fresh_group(
    monkeypatch,
):
    """A browser restart with no surviving group (no session restore, no user
    group recycled onto the stale id) must keep self-healing: the stale id is
    gone, so the next open creates a fresh group — never a wedge."""
    calls: list[tuple[str, int | None]] = []

    class _GoneRelay:
        port = 19989

        def reset_session_announce(self, _session_id):
            return None

        async def query_group_tabs(self, *, group_id, timeout=10.0):
            calls.append(("query", group_id))
            return {"groupId": -1, "tabs": []}

        async def create_background_tab(self, url, *, group_name, group_id,
                                        background,
                                        skip_post_attach_commands,
                                        expected_generation=None,
                                        session_id=None):
            calls.append(("create", group_id))
            return SimpleNamespace(
                tab_id=12, target_id="ext-tab-12", url=url, title="new",
                group_id=101)

    _patch_registry(monkeypatch, _ledger("A"))
    upstream = ExtensionUpstream(_GoneRelay(), _noop, _noop)

    result = await upstream.open_background_tab(
        "https://new/", group_name="Agent", session_id="A")

    assert calls == [("query", 100), ("create", None)]
    assert result["groupId"] == 101
    assert upstream.group_for_session("A") == 101
