from __future__ import annotations

import json
from typing import Any

import pytest

from browserwright.daemon.server.proxy import Router, _cmd_result
from browserwright.daemon.server.state import DaemonState, UpstreamPhase
from browserwright.daemon.server.upstream import CdpUpstream


class Capture:
    def __init__(self) -> None:
        self.upstream: list[Any] = []
        self.per_client: dict[int, list[Any]] = {}
        self.ensure_calls = 0
        self.disconnects: list[str] = []

    async def upstream_send(self, text: str) -> None:
        try:
            self.upstream.append(json.loads(text))
        except ValueError:
            self.upstream.append(text)

    def client_send_for(self, client_id: int):
        self.per_client[client_id] = []

        async def send(text: str) -> None:
            try:
                self.per_client[client_id].append(json.loads(text))
            except ValueError:
                self.per_client[client_id].append(text)

        return send

    async def ensure_upstream(self) -> None:
        self.ensure_calls += 1

    async def trigger_disconnect(self, reason: str) -> None:
        self.disconnects.append(reason)


def setup_router(*labels: str, backend: str = "rdp",
                 phase: UpstreamPhase = UpstreamPhase.CONNECTED,
                 wire_upstream: bool = True):
    state = DaemonState(backend_name=backend)
    state.upstream_phase = phase
    router = Router(state)
    cap = Capture()
    router.bind_lifecycle(cap.ensure_upstream, cap.trigger_disconnect)
    if wire_upstream:
        router.update_upstream_send(cap.upstream_send)
    clients = []
    for label in labels or ("client",):
        client = state.allocate_client(label)
        client.session_id = f"s-{label}"
        client.session_name = label
        router.register_client(client.client_id, cap.client_send_for(client.client_id))
        clients.append(client)
    return state, router, cap, clients


async def attach_primary(router: Router, cap: Capture, client, target_id: str = "T",
                         upstream_sid: str = "U"):
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "Target.attachToTarget",
        "params": {"targetId": target_id},
    }))
    upstream_id = cap.upstream[-1]["id"]
    await router.forward_from_upstream(json.dumps({
        "id": upstream_id,
        "result": {"sessionId": upstream_sid},
    }))
    return cap.per_client[client.client_id][-1]["result"]["sessionId"]


def last_error(cap: Capture, client) -> dict:
    return cap.per_client[client.client_id][-1]["error"]


def attach_cdp(router: Router, cap: Capture, command, *, on_end_session=None):
    async def on_close(_reason: str) -> None:
        return None

    upstream = CdpUpstream(
        router.forward_from_upstream, on_close, state=router.state,
        on_end_session=on_end_session)
    upstream.send_command = command
    upstream.send_cdp = cap.upstream_send
    router.upstream = None
    upstream.attach(router)
    return upstream


@pytest.mark.asyncio
async def test_raw_frames_lifecycle_failure_and_broadcast_edges():
    state, router, cap, (client,) = setup_router()
    await router.route_from_client(client, "{not json")
    assert cap.upstream == ["{not json"]

    await router.forward_from_upstream("not-json either")
    assert cap.per_client[client.client_id][-1] == "not-json either"

    dropped = []

    async def broken_send(text: str) -> None:
        dropped.append(text)
        raise RuntimeError("client vanished")

    router.register_client(999, broken_send)
    await router.forward_from_upstream(json.dumps({
        "method": "Target.targetCreated",
        "params": {"targetInfo": {"targetId": "T", "type": "page"}},
    }))
    assert dropped

    router.unregister_client(999)
    router.update_upstream_send(None)
    await router._forward_raw(json.dumps({"id": 123}))

    state.upstream_phase = UpstreamPhase.DISCONNECTED
    await router._fail_pre_open_buffers("boom")
    client.pre_open_buffer.append(json.dumps({"id": 77, "method": "Browser.getVersion"}))
    client.pre_open_buffer.append("still raw")
    await router._fail_pre_open_buffers("boom")
    ids = [msg["id"] for msg in cap.per_client[client.client_id][-2:]]
    assert ids == [77, None]
    assert client.pre_open_buffer == type(client.pre_open_buffer)()


@pytest.mark.asyncio
async def test_self_answer_backend_info_and_target_table():
    state, router, cap, (alice, bob) = setup_router("alice", "bob")
    await router.forward_from_upstream(json.dumps({
        "method": "Target.targetInfoChanged",
        "params": {"targetInfo": {
            "targetId": "T", "type": "page",
            "url": "https://focus/", "title": "Focus",
        }},
    }))
    assert state.targets["T"]["url"] == "https://focus/"

    await router.route_from_client(alice, json.dumps({
        "id": 6, "method": "BrowserwrightDaemon.getBackendInfo",
    }))
    assert cap.per_client[alice.client_id][-1]["result"]["kind"] == "UPSTREAM_WS"
    assert cap.per_client[alice.client_id][-1]["result"]["schema_version"] == 1


@pytest.mark.asyncio
async def test_wait_session_announce_is_extension_only_noop_for_rdp():
    state, router, cap, (client,) = setup_router("rdp-session", backend="rdp")
    called = False

    async def wait_session_announce(session_id: str, timeout: float) -> bool:
        nonlocal called
        called = True
        return False

    router.upstream.wait_session_announce = wait_session_announce
    await router.route_from_client(client, json.dumps({
        "id": 7,
        "method": "BrowserwrightDaemon.waitForSessionAnnounce",
        "params": {"timeout": 0.01},
    }))

    assert cap.per_client[client.client_id][-1]["result"] == {"announced": True}
    assert called is False


@pytest.mark.asyncio
async def test_extension_scoped_get_targets_success_error_and_fallback():
    state, router, cap, (scoped, sessionless) = setup_router(
        "scoped", "sessionless", backend="extension")
    scoped.session_id = "sess-1"
    seen: list[str] = []

    async def scoped_targets(session_id: str):
        seen.append(session_id)
        return [{"targetId": "only-mine", "type": "page"}]

    router.upstream.list_tabs = scoped_targets
    await router.route_from_client(scoped, json.dumps({
        "id": 10, "method": "Target.getTargets",
    }))
    assert seen == ["sess-1"]
    assert cap.per_client[scoped.client_id][-1]["result"]["targetInfos"][0]["targetId"] == "only-mine"
    assert cap.upstream == []

    async def broken_scoped(session_id: str):
        raise RuntimeError(f"bad scope {session_id}")

    router.upstream.list_tabs = broken_scoped
    await router.route_from_client(scoped, json.dumps({
        "id": 11, "method": "Target.getTargets",
    }))
    assert last_error(cap, scoped)["code"] == -32603

    state.backend_name = "rdp"
    await router.route_from_client(sessionless, json.dumps({
        "id": 12, "method": "Target.getTargets",
    }))
    assert cap.upstream[-1]["method"] == "Target.getTargets"
    assert state.pending_requests[cap.upstream[-1]["id"]].client_request_id == 12


@pytest.mark.asyncio
async def test_attach_response_race_variants_and_validation():
    state, router, cap, (alice, bob, carol) = setup_router("alice", "bob", "carol")

    await router.route_from_client(alice, json.dumps({
        "id": 10, "method": "Target.attachToTarget", "params": {},
    }))
    assert last_error(cap, alice)["code"] == -32602

    await router.route_from_client(alice, json.dumps({
        "id": 11,
        "method": "Target.attachToTarget",
        "params": {"targetId": "RACE"},
    }))
    alice_upstream_id = cap.upstream[-1]["id"]
    bob_local = state.bind_session(
        bob.client_id, "bob-local", "BOB-UP", "RACE")
    state.claim_attacher("RACE", bob.client_id, bob_local.local_session_id, "BOB-UP")
    await router.forward_from_upstream(json.dumps({
        "id": alice_upstream_id, "result": {"sessionId": "ALICE-UP"},
    }))
    assert cap.per_client[alice.client_id][-1]["error"]["code"] == -32602

    await router.route_from_client(carol, json.dumps({
        "id": 12,
        "method": "Target.attachToTarget",
        "params": {"targetId": "SHARE"},
    }))
    carol_upstream_id = cap.upstream[-1]["id"]
    state.bind_session(bob.client_id, "owner-local", "OWNER-UP", "SHARE")
    state.claim_attacher("SHARE", bob.client_id, "owner-local", "OWNER-UP")
    await router.forward_from_upstream(json.dumps({
        "id": carol_upstream_id, "result": {"sessionId": "CAROL-UP"},
    }))
    # Race-loss: carol's attach converts to an error because bob became
    # primary between her attach and the response.
    assert cap.per_client[carol.client_id][-1]["error"]["code"] == -32602

    cap.upstream.clear()
    await router.route_from_client(bob, json.dumps({
        "id": 13,
        "method": "Target.attachToTarget",
        "params": {"targetId": "SHARE"},
    }))
    assert cap.upstream == []
    assert cap.per_client[bob.client_id][-1]["result"]["sessionId"] == "owner-local"


@pytest.mark.asyncio
async def test_detach_paths_and_session_scoped_event_unbinds():
    state, router, cap, (alice, bob) = setup_router("alice", "bob")
    alice_local = await attach_primary(router, cap, alice, "T", "UP-T")

    await router.route_from_client(alice, json.dumps({
        "id": 2, "method": "Target.detachFromTarget", "params": {},
    }))
    assert last_error(cap, alice)["code"] == -32602
    await router.route_from_client(alice, json.dumps({
        "id": 3, "method": "Target.detachFromTarget",
        "params": {"sessionId": "unknown"},
    }))
    assert last_error(cap, alice)["code"] == -32602

    # A second attach from another client is refused (single-attacher rule).
    await router.route_from_client(bob, json.dumps({
        "id": 4,
        "method": "Target.attachToTarget",
        "params": {"targetId": "T"},
    }))
    assert last_error(cap, bob)["code"] == -32602

    await router.route_from_client(alice, json.dumps({
        "id": 6, "method": "Target.detachFromTarget",
        "params": {"sessionId": alice_local},
    }))
    detach_frame = cap.upstream[-1]
    assert detach_frame["params"]["sessionId"] == "UP-T"
    assert alice_local not in alice.sessions
    await router.forward_from_upstream(json.dumps({
        "id": detach_frame["id"], "result": {},
    }))
    assert cap.per_client[alice.client_id][-1]["id"] == 6

    state.bind_session(alice.client_id, "a2", "UP-2", "T2")
    state.claim_attacher("T2", alice.client_id, "a2", "UP-2")
    await router.forward_from_upstream(json.dumps({
        "method": "Target.detachedFromTarget",
        "params": {"sessionId": "UP-2", "reason": "gone"},
    }))
    assert "a2" not in alice.sessions
    assert cap.per_client[alice.client_id][-1]["params"]["sessionId"] == "a2"


@pytest.mark.asyncio
async def test_upstream_event_table_orphan_and_pending_auto_attach_edges():
    state, router, cap, (client,) = setup_router()
    await router.route_from_client(client, json.dumps({
        "id": 20,
        "method": "Target.attachToTarget",
        "params": {"targetId": "PENDING"},
    }))
    await router.forward_from_upstream(json.dumps({
        "method": "Target.attachedToTarget",
        "params": {
            "sessionId": "AUTO-SID",
            "targetInfo": {
                "targetId": "PENDING", "type": "page",
                "url": "https://pending/", "title": "Pending",
            },
        },
    }))
    assert cap.per_client[client.client_id] == []
    assert state.targets["PENDING"]["url"] == "https://pending/"

    await router.forward_from_upstream(json.dumps({
        "method": "Runtime.consoleAPICalled",
        "sessionId": "ORPHAN",
        "params": {},
    }))
    assert cap.per_client[client.client_id] == []

    await router.forward_from_upstream(json.dumps({"id": 999, "result": {}}))
    await router.forward_from_upstream(json.dumps({"id": "not-int", "result": {}}))
    assert cap.per_client[client.client_id] == []

    await router.forward_from_upstream(json.dumps({
        "method": "Target.targetDestroyed",
        "params": {"targetId": "PENDING"},
    }))
    assert "PENDING" not in state.targets


@pytest.mark.asyncio
async def test_attach_active_extension_lazy_malformed_reuse_and_conflict():
    state, router, cap, (alice, bob) = setup_router(
        "alice", "bob", backend="extension", phase=UpstreamPhase.DISCONNECTED,
        wire_upstream=False)

    async def failing_ensure() -> None:
        raise RuntimeError("offline")

    router.bind_lifecycle(failing_ensure, cap.trigger_disconnect)
    await router.route_from_client(alice, json.dumps({
        "id": 1, "method": "BrowserwrightDaemon.attachActiveTab",
    }))
    assert last_error(cap, alice)["code"] == -32603

    state.upstream_phase = UpstreamPhase.CONNECTED
    router.update_upstream_send(cap.upstream_send)

    async def malformed_attach(**kwargs):
        return {"sessionId": 123, "targetId": None}

    router.upstream.attach_active = malformed_attach
    await router.route_from_client(alice, json.dumps({
        "id": 2, "method": "BrowserwrightDaemon.attachActiveTab",
    }))
    assert last_error(cap, alice)["code"] == -32603

    async def attach(**kwargs):
        return {
            "sessionId": "UP-ACTIVE", "targetId": "ACTIVE",
            "tabId": 7, "url": "https://active/", "title": "Active",
        }

    router.upstream.attach_active = attach
    await router.route_from_client(alice, json.dumps({
        "id": 3, "method": "BrowserwrightDaemon.attachActiveTab",
    }))
    first_local = cap.per_client[alice.client_id][-1]["result"]["sessionId"]
    await router.route_from_client(alice, json.dumps({
        "id": 4, "method": "BrowserwrightDaemon.attachActiveTab",
    }))
    assert cap.per_client[alice.client_id][-1]["result"]["sessionId"] == first_local

    await router.route_from_client(bob, json.dumps({
        "id": 5, "method": "BrowserwrightDaemon.attachActiveTab",
    }))
    assert last_error(cap, bob)["code"] == -32602
    assert state.targets["ACTIVE"]["title"] == "Active"


@pytest.mark.asyncio
async def test_open_recover_close_and_end_error_translation_branches():
    state, router, cap, (client,) = setup_router("client", backend="extension")
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/", "groupName": 123},
    }))
    assert last_error(cap, client)["code"] == -32602

    async def bad_open(*args, **kwargs):
        return {"sessionId": "UP"}

    router.upstream.open_tab = bad_open
    await router.route_from_client(client, json.dumps({
        "id": 2,
        "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/"},
    }))
    assert last_error(cap, client)["code"] == -32603

    async def ok_open(*args, **kwargs):
        return {"sessionId": "UP", "targetId": "T", "tabId": 1}

    router.upstream.open_tab = ok_open
    await router.route_from_client(client, json.dumps({
        "id": 3,
        "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/", "background": "not-bool"},
    }))
    local_sid = cap.per_client[client.client_id][-1]["result"]["sessionId"]

    async def failing_close(upstream_sid: str):
        raise RuntimeError(f"nope {upstream_sid}")

    router.upstream.close_tab = failing_close
    await router.route_from_client(client, json.dumps({
        "id": 4,
        "method": "BrowserwrightDaemon.closeTab",
        "params": {"sessionId": local_sid},
    }))
    assert last_error(cap, client)["code"] == -32603
    assert local_sid not in client.sessions
    assert "T" not in state.attachers

    state.note_target_info({"targetId": "ghost", "type": "page"})

    async def failing_close_by_target(target_id: str):
        raise RuntimeError(target_id)

    router.upstream.close_tab = failing_close_by_target
    await router.route_from_client(client, json.dumps({
        "id": 5,
        "method": "BrowserwrightDaemon.closeTab",
        "params": {"targetId": "ghost"},
    }))
    assert last_error(cap, client)["code"] == -32603
    assert "ghost" not in state.targets

    async def malformed_recover(*args, **kwargs):
        return {"sessionId": "UP"}

    router.upstream.recover = malformed_recover
    await router.route_from_client(client, json.dumps({
        "id": 6,
        "method": "BrowserwrightDaemon.recoverSession",
        "params": {"groupId": 2},
    }))
    assert last_error(cap, client)["code"] == -32603

    async def end_with_group(session: str, group_id: int):
        return {"session": session, "groupId": group_id}

    router.upstream.end_session = end_with_group
    await router.route_from_client(client, json.dumps({
        "id": 7,
        "method": "BrowserwrightDaemon.endSession",
        "params": {"session": client.session_id, "groupId": 12},
    }))
    assert cap.per_client[client.client_id][-1]["result"] == {
        "session": client.session_id, "groupId": 12,
    }


@pytest.mark.asyncio
async def test_end_session_daemon_edge_cases():
    state, router, cap, (client,) = setup_router()
    client.session_id = "sess"

    class TeardownDaemon:
        def __init__(self, fail: bool = False) -> None:
            self.contexts = {"sess": object()}
            self.fail = fail
            self.calls: list[str] = []

        async def teardown_rdp_context(self, session: str) -> None:
            self.calls.append(session)
            if self.fail:
                raise RuntimeError("teardown failed")

    good = TeardownDaemon()
    router.daemon = good
    async def unused_command(*args, **kwargs):
        raise AssertionError("endSession must not send CDP")
    attach_cdp(
        router, cap, unused_command,
        on_end_session=good.teardown_rdp_context)
    await router.route_from_client(client, json.dumps({
        "id": 2,
        "method": "BrowserwrightDaemon.endSession",
        "params": {"session": "sess"},
    }))
    assert good.calls == ["sess"]
    assert cap.per_client[client.client_id][-1]["result"]["backend"] == "rdp"

    bad = TeardownDaemon(fail=True)
    router.daemon = bad
    router.upstream._on_end_session = bad.teardown_rdp_context
    await router.route_from_client(client, json.dumps({
        "id": 3,
        "method": "BrowserwrightDaemon.endSession",
        "params": {"session": "sess"},
    }))
    assert last_error(cap, client)["code"] == -32603


@pytest.mark.asyncio
async def test_rdp_open_attach_close_error_translation_and_cleanup():
    state, router, cap, (client,) = setup_router(backend="rdp")

    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/"},
    }))
    assert last_error(cap, client)["code"] == -32603

    async def malformed_create(method: str, params=None, session_id=None):
        if method == "Target.createTarget":
            return {"id": -1, "result": {}}
        raise AssertionError(method)

    attach_cdp(router, cap, malformed_create)
    await router.route_from_client(client, json.dumps({
        "id": 2,
        "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/"},
    }))
    assert "createTarget returned" in last_error(cap, client)["message"]

    async def malformed_attach(method: str, params=None, session_id=None):
        if method == "Target.createTarget":
            return {"id": -1, "result": {"targetId": "T"}}
        if method == "Target.attachToTarget":
            return {"id": -1, "result": {}}
        raise AssertionError(method)

    attach_cdp(router, cap, malformed_attach)
    await router.route_from_client(client, json.dumps({
        "id": 3,
        "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/"},
    }))
    assert "Target.attachToTarget returned" in last_error(cap, client)["message"]

    calls: list[tuple[str, Any, Any]] = []

    async def rdp_cmd(method: str, params=None, session_id=None):
        calls.append((method, params, session_id))
        if method == "Target.createTarget":
            return {"id": -1, "result": {"targetId": "T"}}
        if method == "Target.attachToTarget":
            return {"id": -1, "result": {"sessionId": "UP"}}
        if method == "Target.closeTarget":
            raise RuntimeError("close boom")
        raise AssertionError(method)

    attach_cdp(router, cap, rdp_cmd)
    await router.route_from_client(client, json.dumps({
        "id": 4,
        "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/"},
    }))
    local_sid = cap.per_client[client.client_id][-1]["result"]["sessionId"]
    await router.route_from_client(client, json.dumps({
        "id": 5,
        "method": "BrowserwrightDaemon.closeTab",
        "params": {"sessionId": local_sid},
    }))
    assert calls[-1][0] == "Target.closeTarget"
    assert last_error(cap, client)["code"] == -32603
    assert local_sid not in client.sessions
    assert "T" not in state.targets

    await router.route_from_client(client, json.dumps({
        "id": 6,
        "method": "BrowserwrightDaemon.closeTab",
        "params": {"sessionId": "missing"},
    }))
    assert last_error(cap, client)["code"] == -32602


@pytest.mark.asyncio
async def test_env_backend_dispatches_tab_verbs_to_raw_cdp():
    """issue #20: an env session speaks real browser-level CDP (like rdp), so
    the unified tab verbs must dispatch to raw CDP via `_upstream_command` —
    NOT return -32601 "requires the extension backend". Locks in
    `Router._raw_cdp_backend` covering env, not only rdp."""
    state, router, cap, (client,) = setup_router(backend="env")

    calls: list[str] = []

    async def env_cmd(method: str, params=None, session_id=None):
        calls.append(method)
        if method == "Target.createTarget":
            return {"id": -1, "result": {"targetId": "T"}}
        if method == "Target.attachToTarget":
            return {"id": -1, "result": {"sessionId": "UP"}}
        if method == "Target.closeTarget":
            return {"id": -1, "result": {}}
        raise AssertionError(method)

    attach_cdp(router, cap, env_cmd)
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/"},
    }))
    result = cap.per_client[client.client_id][-1]["result"]
    assert calls[0] == "Target.createTarget"  # raw CDP, not the relay callback
    local_sid = result["sessionId"]

    await router.route_from_client(client, json.dumps({
        "id": 2,
        "method": "BrowserwrightDaemon.closeTab",
        "params": {"sessionId": local_sid},
    }))
    assert "Target.closeTarget" in calls  # closeTab also raw CDP for env
    assert local_sid not in client.sessions


@pytest.mark.asyncio
async def test_rdp_attach_active_reuses_local_page_and_falls_back_after_bad_targets():
    state, router, cap, (client,) = setup_router(backend="rdp")
    state.bind_session(client.client_id, "local", "UP", "T")
    state.claim_attacher("T", client.client_id, "local", "UP")
    state.note_target_info({
        "targetId": "T", "type": "page", "url": "https://front/", "title": "Front",
    })

    calls: list[str] = []

    async def should_not_call(method: str, params=None, session_id=None):
        calls.append(method)
        raise AssertionError(method)

    attach_cdp(router, cap, should_not_call)
    await router.route_from_client(client, json.dumps({
        "id": 1, "method": "BrowserwrightDaemon.attachActiveTab",
    }))
    assert calls == []
    assert cap.per_client[client.client_id][-1]["result"]["sessionId"] == "local"

    state.unbind_session_by_local(client.client_id, "local")

    async def fallback_cmd(method: str, params=None, session_id=None):
        calls.append(method)
        if method == "Target.getTargets":
            return {"id": -1, "error": {"message": "bad"}}
        if method == "Target.createTarget":
            return {"id": -1, "result": {"targetId": "NEW"}}
        if method == "Target.attachToTarget":
            return {"id": -1, "result": {"sessionId": "UP-NEW"}}
        raise AssertionError(method)

    attach_cdp(router, cap, fallback_cmd)
    await router.route_from_client(client, json.dumps({
        "id": 2, "method": "BrowserwrightDaemon.attachActiveTab",
    }))
    assert calls[-3:] == ["Target.getTargets", "Target.createTarget", "Target.attachToTarget"]
    assert cap.per_client[client.client_id][-1]["result"]["targetId"] == "NEW"


@pytest.mark.asyncio
async def test_rdp_userscript_registry_success_error_and_unsupported_paths():
    state, router, cap, (client,) = setup_router(backend="rdp")
    await router.route_from_client(client, json.dumps({
        "id": 1,
        "method": "BrowserwrightDaemon.userscript.install",
        "params": {"script": {"source": "1"}},
    }))
    assert last_error(cap, client)["code"] == -32000

    state.bind_session(client.client_id, "l1", "UP1", "T1")
    state.bind_session(client.client_id, "l2", "UP1", "T1")
    calls: list[tuple[str, Any, Any]] = []

    async def us_cmd(method: str, params=None, session_id=None):
        calls.append((method, params, session_id))
        if method == "Page.addScriptToEvaluateOnNewDocument":
            return {"id": -1, "result": {"identifier": f"id-{session_id}"}}
        if method == "Page.removeScriptToEvaluateOnNewDocument":
            raise RuntimeError("ignored")
        raise AssertionError(method)

    attach_cdp(router, cap, us_cmd)
    await router.route_from_client(client, json.dumps({
        "id": 2,
        "method": "BrowserwrightDaemon.userscript.install",
        "params": {"script": {"identity": "u", "source": "console.log(1)"}},
    }))
    assert cap.per_client[client.client_id][-1]["result"]["identity"] == "u"
    add_calls = [c for c in calls if c[0] == "Page.addScriptToEvaluateOnNewDocument"]
    assert [c[2] for c in add_calls] == ["UP1"]

    await router.route_from_client(client, json.dumps({
        "id": 3, "method": "BrowserwrightDaemon.userscript.list",
    }))
    assert cap.per_client[client.client_id][-1]["result"]["scripts"][0]["identity"] == "u"

    await router.route_from_client(client, json.dumps({
        "id": 4,
        "method": "BrowserwrightDaemon.userscript.toggle",
        "params": {"key": "missing"},
    }))
    assert cap.per_client[client.client_id][-1]["result"]["ok"] is False

    await router.route_from_client(client, json.dumps({
        "id": 5,
        "method": "BrowserwrightDaemon.userscript.toggle",
        "params": {"key": "u", "enabled": False},
    }))
    assert cap.per_client[client.client_id][-1]["result"]["ok"] is True

    await router.route_from_client(client, json.dumps({
        "id": 6,
        "method": "BrowserwrightDaemon.userscript.logs",
    }))
    assert cap.per_client[client.client_id][-1]["result"] == {
        "logs": [], "backend": "rdp",
    }

    await router.route_from_client(client, json.dumps({
        "id": 7,
        "method": "BrowserwrightDaemon.userscript.mystery",
    }))
    assert cap.per_client[client.client_id][-1]["result"]["ok"] is False

    assert _cmd_result({"id": -1, "result": {"ok": True}}) == {"ok": True}
    with pytest.raises(RuntimeError):
        _cmd_result({"id": -1, "error": {"message": "bad"}})
