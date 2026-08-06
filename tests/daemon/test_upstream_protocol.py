from __future__ import annotations

import pytest

from browserwright.daemon.server.extension_upstream import ExtensionUpstream
from browserwright.daemon.server.proxy import Router
from browserwright.daemon.server.state import DaemonState
from browserwright.daemon.server.upstream import CdpUpstream, Upstream


async def _noop(_value: str) -> None:
    return None


class _Relay:
    port = 19989

    def __init__(self):
        self.closed: list[int] = []

    async def close_tab(self, tab_id: int) -> None:
        self.closed.append(tab_id)


def test_both_adapters_satisfy_declared_upstream_protocol():
    cdp = CdpUpstream(_noop, _noop)
    extension = ExtensionUpstream(_Relay(), _noop, _noop)

    assert isinstance(cdp, Upstream)
    assert isinstance(extension, Upstream)


def test_attach_and_detach_publish_one_atomic_router_reference():
    router = Router(DaemonState("cdp"))
    upstream = CdpUpstream(_noop, _noop)

    upstream.attach(router)
    assert router.upstream is upstream
    upstream.detach(router)
    assert router.upstream is None


@pytest.mark.asyncio
async def test_cdp_upstream_owns_raw_tab_lifecycle():
    calls: list[tuple[str, dict | None, str | None]] = []

    async def command(method, params=None, session_id=None, timeout=10.0):
        calls.append((method, params, session_id))
        if method == "Target.createTarget":
            return {"result": {"targetId": "target-1"}}
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "upstream-1"}}
        if method == "Target.closeTarget":
            return {"result": {"success": True}}
        raise AssertionError(method)

    upstream = CdpUpstream(_noop, _noop)
    upstream.send_command = command

    opened = await upstream.open_tab("https://example.test/")
    assert opened == {
        "sessionId": "upstream-1",
        "targetId": "target-1",
        "tabId": None,
        "url": "https://example.test/",
        "title": "",
        "groupId": -1,
    }
    assert await upstream.attach_active() == opened
    assert await upstream.close_tab("upstream-1") == {
        "ok": True,
        "tabId": None,
    }
    assert [call[0] for call in calls] == [
        "Target.createTarget",
        "Target.attachToTarget",
        "Target.closeTarget",
    ]


def test_facade_adapter_shares_extension_binding_owner():
    owner = ExtensionUpstream(_Relay(), _noop, _noop)
    facade_adapter = ExtensionUpstream(
        _Relay(), _noop, _noop, group_owner=owner)

    owner._bind_group("session-1", 42)
    assert facade_adapter.group_for_session("session-1") == 42
    assert facade_adapter._groups is owner._groups


@pytest.mark.asyncio
async def test_extension_close_tab_accepts_target_id_fallback():
    relay = _Relay()
    upstream = ExtensionUpstream(relay, _noop, _noop)
    upstream.register_session(7, "ext-sid-7-known")

    assert await upstream.close_tab("ext-tab-7") == {"ok": True, "tabId": 7}
    assert relay.closed == [7]
    assert upstream.session_for_tab(7) is None


@pytest.mark.asyncio
async def test_extension_current_page_preserves_zero_group_id():
    upstream = ExtensionUpstream(_Relay(), _noop, _noop)
    upstream._bind_group("session-0", 0)
    upstream.register_session(8, "ext-sid-8-known")

    async def scoped(_session_id):
        return [{
            "targetId": "ext-tab-8", "url": "https://example.test/",
            "title": "Example", "attached": True,
        }]

    upstream.scoped_target_infos = scoped
    result = await upstream.current_page("session-0")
    assert result["groupId"] == 0
