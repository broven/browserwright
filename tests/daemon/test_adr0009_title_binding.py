"""ADR-0009: the tab group title is the session's only binding.

These lock the invariants that replaced issue #29's per-tab ownership markers.
The deleted `test_issue29_*` files asserted the inverse of several of these on
purpose — if you find yourself re-adding a second anchor, read the ADR first.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from browserwright.daemon.server.extension_upstream import (
    ExtensionUpstream,
    session_group_title,
)


async def _noop(_value):
    return None


def _ledger(monkeypatch, session_id, name, runtime=None):
    record = {"id": session_id, "name": name, "runtime": dict(runtime or {})}
    monkeypatch.setattr(
        "browserwright.session_registry.get",
        lambda sid, _r=record: _r if sid == session_id else None)
    monkeypatch.setattr(
        "browserwright.session_registry.update",
        lambda _sid, **f: (record.update(f), record)[1])
    monkeypatch.setattr("browserwright.session_registry.list_all", lambda: [])
    return record


class _TitleRelay:
    """A relay whose group lookup behaves like the extension: title-keyed."""

    port = 19989
    connection_generation = 1

    def __init__(self, groups: dict[str, tuple[int, list[int]]] | None = None):
        self.groups = dict(groups or {})
        self.asked: list[str | None] = []
        self.closed: list[int] = []

    def reset_session_announce(self, _session_id):
        return None

    async def query_group_tabs(self, group_name=None, *, timeout=10.0):
        self.asked.append(group_name)
        gid, members = self.groups.get(group_name, (-1, []))
        return {
            "groupId": gid,
            "groupTitle": group_name or "",
            "tabs": [{"tabId": t} for t in members],
        }

    async def close_tab(self, tab_id, *, expected_generation=None,
                        timeout=5.0):
        self.closed.append(tab_id)


def test_title_is_name_then_bw_token_then_session_id(monkeypatch):
    _ledger(monkeypatch, "12", "fetch")
    assert session_group_title("12") == "fetch-BW12"


def test_title_falls_back_to_a_placeholder_name_but_never_to_a_bare_token(
    monkeypatch,
):
    """A row with no name still gets a well-formed, session-scoped title.

    `-BW12` alone would be a title no human wrote and no session can claim
    twice, but it reads like a bug in Chrome's UI. The placeholder keeps the
    shape `<something>-BW<sid>` intact.
    """
    _ledger(monkeypatch, "12", "")
    assert session_group_title("12") == "session-BW12"


def test_no_ledger_row_means_no_title_to_look_for(monkeypatch):
    monkeypatch.setattr("browserwright.session_registry.get", lambda _sid: None)
    assert session_group_title("12") is None
    assert session_group_title(None) is None


@pytest.mark.asyncio
async def test_group_is_reclaimed_after_a_browser_restart_reassigns_its_id(
    monkeypatch,
):
    """The case issue #53 was filed for, and the reason the anchor changed.

    Chrome hands the restored group a NEW numeric id and wipes the per-tab
    markers, so neither of the old anchors survives. The title does, so
    teardown finds the group and closes it.
    """
    _ledger(monkeypatch, "12", "fetch",
            runtime={"current_target_id": "ext-tab-4"})
    relay = _TitleRelay({"fetch-BW12": (901, [4, 5])})  # id 901 != pre-restart
    upstream = ExtensionUpstream(relay, _noop, _noop)

    result = await upstream.end_session("12")

    assert relay.asked == ["fetch-BW12"]
    assert sorted(relay.closed) == [4, 5]
    assert result["ok"] is True
    assert result["failed"] == []


@pytest.mark.asyncio
async def test_a_renamed_group_is_reported_gone_not_retried(monkeypatch):
    """The accepted cost of a single anchor (ADR-0009 §前提).

    The user renamed the group, so we no longer find it. Teardown reports a
    clean end rather than failing forever — the group is theirs now.
    """
    _ledger(monkeypatch, "12", "fetch")
    relay = _TitleRelay({"something the user typed": (901, [4])})
    upstream = ExtensionUpstream(relay, _noop, _noop)

    result = await upstream.end_session("12")

    assert relay.closed == [], "must not close tabs in a group we can't name"
    assert result["ok"] is True
    assert result["failed"] == []


@pytest.mark.asyncio
async def test_a_group_id_that_is_not_ours_by_title_is_never_touched(
    monkeypatch,
):
    """The safety direction #29 exists to protect, now closed by construction.

    A user-created group cannot collide: `-BW<sid>` is not a shape anyone types
    by hand, and `sid` comes from the ledger's monotonic allocator.
    """
    _ledger(monkeypatch, "12", "fetch")
    relay = _TitleRelay({"Shopping": (901, [4, 5])})  # the user's own group
    upstream = ExtensionUpstream(relay, _noop, _noop)

    await upstream.end_session("12")

    assert relay.closed == []


@pytest.mark.asyncio
async def test_open_derives_the_title_and_ignores_a_caller_supplied_name(
    monkeypatch,
):
    """A caller cannot rename a session's group by passing `group_name`.

    Honouring it would let the agent path and the facade put one session in two
    groups — exactly the split the single anchor exists to prevent.
    """
    _ledger(monkeypatch, "12", "fetch")
    sent: list[str | None] = []

    class _Relay(_TitleRelay):
        async def create_background_tab(self, url, *, group_name, **kwargs):
            sent.append(group_name)
            return SimpleNamespace(
                tab_id=7, target_id="ext-tab-7", url=url, title="t",
                group_id=901)

    upstream = ExtensionUpstream(_Relay(), _noop, _noop)
    await upstream.open_background_tab(
        "https://x/", group_name="something else", session_id="12")

    assert sent == ["fetch-BW12"]


@pytest.mark.asyncio
async def test_teardown_writes_no_group_id_or_retry_anchors_to_the_ledger(
    monkeypatch,
):
    """The durable state ADR-0009 removed stays removed.

    A cached numeric id is a second source of truth, and the retry anchors made
    a ledger write able to abort a browser teardown midway. Retry re-queries by
    title instead.
    """
    record = _ledger(monkeypatch, "12", "fetch")
    relay = _TitleRelay({"fetch-BW12": (901, [4])})
    upstream = ExtensionUpstream(relay, _noop, _noop)

    await upstream.end_session("12")

    runtime = record.get("runtime") or {}
    assert "group_id" not in runtime
    assert "retry_target_ids" not in runtime
    assert "owned_tab_ids" not in runtime


@pytest.mark.asyncio
async def test_a_live_id_that_disagrees_with_the_cache_replaces_it(
    monkeypatch,
):
    """Disagreement is the restart case, not corruption.

    Under the old id-keyed lookup this could only mean something was wrong, so
    it raised. Raising now would fail exactly the recovery ADR-0009 enables.
    """
    _ledger(monkeypatch, "12", "fetch")
    relay = _TitleRelay({"fetch-BW12": (901, [4])})
    upstream = ExtensionUpstream(relay, _noop, _noop)
    upstream._bind_group("12", 100)          # cached before the restart
    relay.connection_generation = 2          # ...which this reconnect retires

    gid, members = await upstream._group_member_tabs("12")

    assert (gid, members) == (901, [4])
    assert upstream._groups["12"] == 901


@pytest.mark.asyncio
async def test_concurrent_opens_of_one_session_share_a_single_group(
    monkeypatch,
):
    """Two adapters, one title, one group — enforced by the extension itself.

    Before ADR-0009 the shared lock had to thread a numeric id between adapters
    to stop a second group appearing. Now they simply agree on the title.
    """
    _ledger(monkeypatch, "12", "fetch")
    minted: dict[str, int] = {}

    class _Relay(_TitleRelay):
        async def create_background_tab(self, url, *, group_name, **kwargs):
            await asyncio.sleep(0)
            gid = minted.setdefault(group_name, 900 + len(minted))
            return SimpleNamespace(
                tab_id=10 + len(minted), target_id="ext-tab-x", url=url,
                title="t", group_id=gid)

    owner = ExtensionUpstream(_Relay(), _noop, _noop)
    facade = ExtensionUpstream(_Relay(), _noop, _noop, group_owner=owner)
    first, second = await asyncio.gather(
        owner.open_background_tab("https://a/", session_id="12"),
        facade.open_background_tab("https://b/", session_id="12"),
    )

    assert list(minted) == ["fetch-BW12"]
    assert first["groupId"] == second["groupId"]
