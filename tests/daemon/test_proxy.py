"""CDP proxy + BrowserwrightDaemon.* namespace (v0.3 multi-client).

The Router uses async callables for upstream + per-client sends; we capture
them in test scaffolding and assert on what the router would have done
without spinning up a real ws.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any

import pytest

from browserwright.daemon.server.proxy import Router
from browserwright.daemon.server.state import (
    AttachOwnership, ClientState, DaemonState, SessionBinding, UpstreamPhase,
)


# ---- shared scaffolding ---------------------------------------------------


def _state(phase=UpstreamPhase.CONNECTED, backend: str = "rdp") -> DaemonState:
    s = DaemonState(backend_name=backend)
    s.upstream_phase = phase
    return s


class _Capture:
    """Records everything the router 'sends'. Per-client outboxes."""

    def __init__(self):
        self.upstream: list[dict] = []
        self.per_client: dict[int, list[dict]] = {}
        self.ensure_calls: int = 0
        self.disconnect_calls: list[str] = []

    async def upstream_send(self, text: str) -> None:
        self.upstream.append(json.loads(text))

    def client_send_for(self, client_id: int):
        self.per_client[client_id] = []
        async def send(text: str) -> None:
            self.per_client[client_id].append(json.loads(text))
        return send

    async def ensure_upstream(self) -> None:
        self.ensure_calls += 1

    async def trigger_disconnect(self, reason: str) -> None:
        self.disconnect_calls.append(reason)


def _setup(*labels: str, phase=UpstreamPhase.CONNECTED, wire_upstream: bool = True,
           backend: str = "rdp"):
    """Build a Router + state + one or more clients ready to receive sends."""
    state = _state(phase=phase, backend=backend)
    cap = _Capture()
    router = Router(state)
    router.bind_lifecycle(cap.ensure_upstream, cap.trigger_disconnect)
    if wire_upstream:
        router.update_upstream_send(cap.upstream_send)
    clients = []
    for label in labels or ("skill-repl",):
        c = state.allocate_client(label)
        c.session_id = f"bw-{label}"
        c.session_name = f"bw-{label}"
        router.register_client(c.client_id, cap.client_send_for(c.client_id))
        clients.append(c)
    return state, router, cap, clients


# ---- standard CDP forwarding ---------------------------------------------


@pytest.mark.asyncio
async def test_standard_cdp_command_forwarded_with_id_translation():
    """Client request id != upstream request id (v0.3 mux requires it).
    Upstream method/params are preserved verbatim."""
    state, router, cap, (client,) = _setup()
    await router.route_from_client(client, json.dumps({
        "id": 7, "method": "Browser.getVersion",
    }))
    assert len(cap.upstream) == 1
    sent = cap.upstream[0]
    assert sent["method"] == "Browser.getVersion"
    # Client sent id=7; daemon translated to its own counter (starts at 1).
    assert sent["id"] != 7
    # The pending map remembers our original id.
    pending = state.pending_requests[sent["id"]]
    assert pending.client_request_id == 7
    assert pending.method == "Browser.getVersion"


@pytest.mark.asyncio
async def test_response_id_translated_back_to_client():
    """Upstream answers with the daemon-allocated id; daemon must rewrite
    back to the client's original id before forwarding."""
    state, router, cap, (client,) = _setup()
    await router.route_from_client(client, json.dumps({
        "id": 7, "method": "Browser.getVersion",
    }))
    upstream_id = cap.upstream[0]["id"]
    # Upstream replies.
    await router.forward_from_upstream(json.dumps({
        "id": upstream_id,
        "result": {"product": "FakeChrome/1.0"},
    }))
    assert cap.per_client[client.client_id][-1]["id"] == 7


@pytest.mark.asyncio
async def test_target_activate_target_updates_last_activated_table():
    state, router, cap, (client,) = _setup()
    await router.route_from_client(client, json.dumps({
        "id": 5, "method": "Target.activateTarget",
        "params": {"targetId": "ABC"},
    }))
    assert "ABC" in state.last_activated_at
    # Forwarded too.
    assert cap.upstream[-1]["method"] == "Target.activateTarget"


@pytest.mark.asyncio
async def test_lazy_upstream_open_when_disconnected_calls_ensure():
    """Task #76: frame buffered + lazy-open fired as a background task.

    The pre-fix v0.3 code synchronously awaited ensure_upstream() inside
    route_from_client. After the fix, the frame is buffered and ensure is
    spawned via asyncio.create_task; the buffer is drained once upstream
    transitions to CONNECTED (here we don't drive that — we only verify the
    buffer hold + background-spawn invariants).
    """
    state, router, cap, (client,) = _setup(
        phase=UpstreamPhase.DISCONNECTED, wire_upstream=False)
    frame = json.dumps({"id": 1, "method": "Browser.getVersion"})
    await router.route_from_client(client, frame)
    # The frame is held in the client's pre-open buffer, NOT sent upstream.
    assert cap.upstream == []
    assert len(client.pre_open_buffer) == 1
    assert json.loads(client.pre_open_buffer[0])["id"] == 1
    # The background ensure_upstream() task is scheduled; yield once so it
    # gets a chance to run before we check the counter.
    await asyncio.sleep(0)
    assert cap.ensure_calls == 1


# ---- BrowserwrightDaemon.* self-answer (per-client) ----------------------------


@pytest.mark.asyncio
async def test_browserdaemon_get_active_tab_no_tabs_returns_unknown():
    state, router, cap, (client,) = _setup()
    await router.route_from_client(client, json.dumps({
        "id": 1, "method": "BrowserwrightDaemon.getActiveTab",
    }))
    assert cap.upstream == []
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 1
    assert resp["result"]["accuracy"] == "unknown"


@pytest.mark.asyncio
async def test_browserdaemon_get_active_tab_picks_recent():
    state, router, cap, (client,) = _setup()
    state.note_target_info({"targetId": "T", "type": "page",
                            "url": "https://x/", "title": "X"})
    state.last_activated_at["T"] = time.time() - 1
    await router.route_from_client(client, json.dumps({
        "id": 1, "method": "BrowserwrightDaemon.getActiveTab",
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["result"]["targetId"] == "T"


@pytest.mark.asyncio
async def test_browserdaemon_get_backend_info_self_answered():
    state, router, cap, (client,) = _setup()
    state.backend_name = "rdp"
    await router.route_from_client(client, json.dumps({
        "id": 2, "method": "BrowserwrightDaemon.getBackendInfo",
    }))
    assert cap.upstream == []
    result = cap.per_client[client.client_id][-1]["result"]
    assert result["name"] == "rdp"
    assert result["kind"] == "UPSTREAM_WS"  # rdp is an upstream-ws backend


@pytest.mark.asyncio
async def test_browserdaemon_get_backend_info_reports_extension_kind():
    """4a: under the extension backend, kind must be LOCAL_RELAY, not the
    old hardcoded UPSTREAM_WS."""
    state, router, cap, (client,) = _setup()
    state.backend_name = "extension"
    await router.route_from_client(client, json.dumps({
        "id": 2, "method": "BrowserwrightDaemon.getBackendInfo",
    }))
    result = cap.per_client[client.client_id][-1]["result"]
    assert result["name"] == "extension"
    assert result["kind"] == "LOCAL_RELAY"


@pytest.mark.asyncio
async def test_browserdaemon_ui_state_reports_client_count():
    """v0.3 added `client_count` to uiState — checks both fields shape."""
    state, router, cap, clients = _setup("a", "b")
    await router.route_from_client(clients[0], json.dumps({
        "id": 3, "method": "BrowserwrightDaemon.uiState",
    }))
    result = cap.per_client[clients[0].client_id][-1]["result"]
    assert result["ws_count"] == 1
    assert result["banner_visible_estimated"] is True
    assert result["client_count"] == 2


@pytest.mark.asyncio
async def test_browserdaemon_disconnect_triggers_close():
    state, router, cap, (client,) = _setup()
    await router.route_from_client(client, json.dumps({
        "id": 6, "method": "BrowserwrightDaemon.disconnect",
    }))
    assert cap.per_client[client.client_id][0]["result"] == {"ok": True}
    assert cap.disconnect_calls == ["skill_disconnect"]


@pytest.mark.asyncio
async def test_unknown_browserdaemon_method_returns_method_not_found():
    state, router, cap, (client,) = _setup()
    await router.route_from_client(client, json.dumps({
        "id": 99, "method": "BrowserwrightDaemon.totallyMadeUp",
    }))
    err = cap.per_client[client.client_id][-1]["error"]
    assert err["code"] == -32601


# ---- v0.5.4 attach-active (extension backend) ----------------------------


@pytest.mark.asyncio
async def test_attach_active_without_callback_returns_method_not_found():
    """Without an extension backend wired, BrowserwrightDaemon.attachActiveTab
    must surface -32601 instead of silently doing nothing."""
    state, router, cap, (client,) = _setup(backend="extension")
    await router.route_from_client(client, json.dumps({
        "id": 33, "method": "BrowserwrightDaemon.attachActiveTab",
    }))
    err = cap.per_client[client.client_id][-1]["error"]
    assert err["code"] == -32601
    assert "extension backend" in err["message"]


@pytest.mark.asyncio
async def test_attach_active_with_callback_binds_session_and_attacher():
    """When the extension callback is wired, calling attachActiveTab must
    bind a local session + claim_attacher so subsequent CDP commands route
    correctly under the returned sessionId."""
    state, router, cap, (client,) = _setup(backend="extension")

    async def fake_attach_active(*, session_id=None, group_name=None):
        return {
            "sessionId": "UPSTREAM-XSID-1",
            "targetId": "ext-tab-77",
            "tabId": 77,
            "url": "https://x.example/",
            "title": "X",
        }

    router._attach_active_tab = fake_attach_active
    await router.route_from_client(client, json.dumps({
        "id": 10, "method": "BrowserwrightDaemon.attachActiveTab",
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 10
    local_sid = resp["result"]["sessionId"]
    assert local_sid != "UPSTREAM-XSID-1"  # daemon-allocated local id
    assert resp["result"]["targetId"] == "ext-tab-77"
    assert resp["result"]["tabId"] == 77

    # Attacher + session bindings updated to mirror Target.attachToTarget.
    own = state.attachers["ext-tab-77"]
    assert own.primary_client_id == client.client_id
    assert own.upstream_session_id == "UPSTREAM-XSID-1"
    binding = client.sessions[local_sid]
    assert binding.upstream_session_id == "UPSTREAM-XSID-1"
    assert binding.target_id == "ext-tab-77"
    assert binding.readonly is False


@pytest.mark.asyncio
async def test_attach_active_callback_failure_returns_error():
    """If the callback raises, the client sees a CDP -32000 error rather
    than a hung request."""
    state, router, cap, (client,) = _setup(backend="extension")

    async def boom(*, session_id=None, group_name=None):
        raise RuntimeError("relay says no")

    router._attach_active_tab = boom
    await router.route_from_client(client, json.dumps({
        "id": 11, "method": "BrowserwrightDaemon.attachActiveTab",
    }))
    err = cap.per_client[client.client_id][-1]["error"]
    assert err["code"] == -32000
    assert "relay says no" in err["message"]


# ---- unified attachActiveTab on the rdp backend (docs §C1) ---------------


@pytest.mark.asyncio
async def test_attach_active_on_rdp_attaches_existing_front_target():
    """On an rdp context attachActiveTab must NOT be -32601 (no extension
    callback wired). The daemon owns the Chrome, so it attaches an existing
    page target (the session's current front tab) via raw CDP, returning the
    same {sessionId,targetId,...} shape as the extension path."""
    state, router, cap, (client,) = _setup(backend="rdp")

    async def fake_cmd(method, params=None, session_id=None):
        # send_command resolves to the FULL CDP envelope ({"id","result"}).
        if method == "Target.getTargets":
            return {"id": -1, "result": {"targetInfos": [{
                "targetId": "rdp-T1", "type": "page",
                "url": "https://front.example/", "title": "Front",
            }]}}
        if method == "Target.attachToTarget":
            return {"id": -1, "result": {"sessionId": "UP-RDP-1"}}
        raise AssertionError(f"unexpected upstream cmd {method}")

    router._upstream_command = fake_cmd
    await router.route_from_client(client, json.dumps({
        "id": 21, "method": "BrowserwrightDaemon.attachActiveTab",
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 21
    assert "error" not in resp, resp
    result = resp["result"]
    assert result["targetId"] == "rdp-T1"
    assert result["url"] == "https://front.example/"
    # rdp has no Chrome tab id — uniform shape, honest null.
    assert result["tabId"] is None
    # Binding + attacher registered like the extension path.
    own = state.attachers["rdp-T1"]
    assert own.primary_client_id == client.client_id
    assert own.upstream_session_id == "UP-RDP-1"
    assert client.sessions[result["sessionId"]].target_id == "rdp-T1"


@pytest.mark.asyncio
async def test_attach_active_on_rdp_creates_tab_when_none_exists():
    """When the daemon-owned Chrome has no page target, rdp attachActiveTab
    creates one (mirrors the skill's current_page → open() empty fallback)."""
    state, router, cap, (client,) = _setup(backend="rdp")

    async def fake_cmd(method, params=None, session_id=None):
        # send_command resolves to the FULL CDP envelope ({"id","result"}).
        if method == "Target.getTargets":
            return {"id": -1, "result": {"targetInfos": []}}
        if method == "Target.createTarget":
            return {"id": -1, "result": {"targetId": "rdp-new"}}
        if method == "Target.attachToTarget":
            return {"id": -1, "result": {"sessionId": "UP-RDP-NEW"}}
        raise AssertionError(f"unexpected upstream cmd {method}")

    router._upstream_command = fake_cmd
    await router.route_from_client(client, json.dumps({
        "id": 22, "method": "BrowserwrightDaemon.attachActiveTab",
    }))
    resp = cap.per_client[client.client_id][-1]
    assert "error" not in resp, resp
    assert resp["result"]["targetId"] == "rdp-new"
    assert resp["result"]["tabId"] is None
    assert resp["result"]["groupId"] == -1  # created via the open path


@pytest.mark.asyncio
async def test_attach_active_on_rdp_ensures_upstream_before_raw_cdp():
    state, router, cap, (client,) = _setup(backend="rdp", wire_upstream=False)
    client.session_id = "rdp-session"
    state.upstream_phase = UpstreamPhase.DISCONNECTED
    calls: list[str] = []

    async def ensure() -> None:
        cap.ensure_calls += 1
        state.upstream_phase = UpstreamPhase.CONNECTED

        async def rdp_cmd(method: str, params=None, session_id=None):
            calls.append(method)
            if method == "Target.getTargets":
                return {"id": -1, "result": {"targetInfos": []}}
            if method == "Target.createTarget":
                return {"id": -1, "result": {"targetId": "rdp-new"}}
            if method == "Target.attachToTarget":
                return {"id": -1, "result": {"sessionId": "UP-RDP"}}
            raise AssertionError(method)

        router._upstream_command = rdp_cmd

    router.bind_lifecycle(ensure, cap.trigger_disconnect)
    await router.route_from_client(client, json.dumps({
        "id": 23, "method": "BrowserwrightDaemon.attachActiveTab",
    }))

    assert cap.ensure_calls == 1
    assert calls == ["Target.getTargets", "Target.createTarget", "Target.attachToTarget"]
    assert cap.per_client[client.client_id][-1]["result"]["targetId"] == "rdp-new"


# ---- v0.3 multi-client: single-attacher rule -----------------------------


@pytest.mark.asyncio
async def test_attach_first_attacher_becomes_primary():
    state, router, cap, (alice,) = _setup("alice")
    # Alice attaches.
    await router.route_from_client(alice, json.dumps({
        "id": 1, "method": "Target.attachToTarget",
        "params": {"targetId": "TARGET-A"},
    }))
    # Forwarded upstream (no shortcut).
    sent = cap.upstream[-1]
    upstream_id = sent["id"]
    # Simulate upstream success.
    await router.forward_from_upstream(json.dumps({
        "id": upstream_id,
        "result": {"sessionId": "UPSTREAM-SID-1"},
    }))
    # Alice now sees a daemon-allocated local sessionId.
    resp = cap.per_client[alice.client_id][-1]
    assert resp["id"] == 1
    local_sid = resp["result"]["sessionId"]
    assert local_sid != "UPSTREAM-SID-1"
    # Attacher table updated.
    own = state.attachers["TARGET-A"]
    assert own.primary_client_id == alice.client_id
    assert own.upstream_session_id == "UPSTREAM-SID-1"


@pytest.mark.asyncio
async def test_second_attacher_gets_minus_32602():
    """spec §3.4 H7: second attach without allowSecondaryReadOnly → error."""
    state, router, cap, (alice, bob) = _setup("alice", "bob")
    # Alice attaches first.
    await router.route_from_client(alice, json.dumps({
        "id": 1, "method": "Target.attachToTarget",
        "params": {"targetId": "T"},
    }))
    await router.forward_from_upstream(json.dumps({
        "id": cap.upstream[-1]["id"],
        "result": {"sessionId": "U-SID-1"},
    }))
    # Bob tries to attach the same target.
    cap.upstream.clear()
    await router.route_from_client(bob, json.dumps({
        "id": 50, "method": "Target.attachToTarget",
        "params": {"targetId": "T"},
    }))
    # Daemon must short-circuit — NO upstream forward.
    assert cap.upstream == []
    resp = cap.per_client[bob.client_id][-1]
    assert resp["id"] == 50
    assert resp["error"]["code"] == -32602
    assert "already" in resp["error"]["message"].lower()


@pytest.mark.asyncio
async def test_shared_read_grants_readonly_session():
    """spec §3.4 H7 + v0.3 opt-in: with allowSecondaryReadOnly=true the
    second attacher gets a read-only session backed by the same upstream sid."""
    state, router, cap, (alice, bob) = _setup("alice", "bob")
    # Alice attaches.
    await router.route_from_client(alice, json.dumps({
        "id": 1, "method": "Target.attachToTarget",
        "params": {"targetId": "T"},
    }))
    await router.forward_from_upstream(json.dumps({
        "id": cap.upstream[-1]["id"],
        "result": {"sessionId": "U-SID-7"},
    }))
    alice_local = state.attachers["T"].primary_local_session

    # Bob attaches with the shared-read flag.
    cap.upstream.clear()
    await router.route_from_client(bob, json.dumps({
        "id": 99, "method": "Target.attachToTarget",
        "params": {
            "targetId": "T",
            "flags": {"allowSecondaryReadOnly": True},
        },
    }))
    # No upstream roundtrip — daemon synthesized the response.
    assert cap.upstream == []
    resp = cap.per_client[bob.client_id][-1]
    assert resp["id"] == 99
    bob_local = resp["result"]["sessionId"]
    assert bob_local != alice_local
    # Bob's binding is readonly.
    bob_binding = bob.sessions[bob_local]
    assert bob_binding.readonly is True
    assert bob_binding.upstream_session_id == "U-SID-7"
    # Attacher record knows Bob as reader.
    assert (bob.client_id, bob_local) in state.attachers["T"].readers


@pytest.mark.asyncio
async def test_readonly_session_rejects_commands_locally():
    """Commands on a shared-read session must NOT reach upstream."""
    state, router, cap, (alice, bob) = _setup("alice", "bob")
    # Alice attaches.
    await router.route_from_client(alice, json.dumps({
        "id": 1, "method": "Target.attachToTarget",
        "params": {"targetId": "T"},
    }))
    await router.forward_from_upstream(json.dumps({
        "id": cap.upstream[-1]["id"], "result": {"sessionId": "U"},
    }))
    # Bob shared-reads.
    await router.route_from_client(bob, json.dumps({
        "id": 99, "method": "Target.attachToTarget",
        "params": {"targetId": "T", "flags": {"allowSecondaryReadOnly": True}},
    }))
    bob_local = cap.per_client[bob.client_id][-1]["result"]["sessionId"]

    cap.upstream.clear()
    # Bob tries to issue a command on his read-only session.
    await router.route_from_client(bob, json.dumps({
        "id": 100, "method": "Page.navigate",
        "sessionId": bob_local,
        "params": {"url": "https://evil.example/"},
    }))
    assert cap.upstream == [], "read-only command must NOT reach upstream"
    err = cap.per_client[bob.client_id][-1]
    assert err["error"]["code"] == -32602


# ---- v0.3 multi-client: sessionId translation ----------------------------


@pytest.mark.asyncio
async def test_session_scoped_command_translates_local_to_upstream():
    """Outgoing command's sessionId is rewritten from local → upstream."""
    state, router, cap, (alice,) = _setup()
    # Alice attaches.
    await router.route_from_client(alice, json.dumps({
        "id": 1, "method": "Target.attachToTarget",
        "params": {"targetId": "T"},
    }))
    await router.forward_from_upstream(json.dumps({
        "id": cap.upstream[-1]["id"],
        "result": {"sessionId": "UPSTREAM-SID"},
    }))
    alice_local = cap.per_client[alice.client_id][-1]["result"]["sessionId"]

    cap.upstream.clear()
    await router.route_from_client(alice, json.dumps({
        "id": 50, "method": "Page.navigate",
        "sessionId": alice_local,
        "params": {"url": "https://example/"},
    }))
    sent = cap.upstream[-1]
    assert sent["sessionId"] == "UPSTREAM-SID"
    assert sent["sessionId"] != alice_local


@pytest.mark.asyncio
async def test_session_event_routes_only_to_owner():
    """spec §9.3: client A attached target X, client B attached target Y.
    An event with X's sessionId goes ONLY to A.
    """
    state, router, cap, (alice, bob) = _setup("alice", "bob")
    # Alice attaches X.
    await router.route_from_client(alice, json.dumps({
        "id": 1, "method": "Target.attachToTarget",
        "params": {"targetId": "X"},
    }))
    await router.forward_from_upstream(json.dumps({
        "id": cap.upstream[-1]["id"], "result": {"sessionId": "U-X"},
    }))
    # Bob attaches Y.
    await router.route_from_client(bob, json.dumps({
        "id": 2, "method": "Target.attachToTarget",
        "params": {"targetId": "Y"},
    }))
    await router.forward_from_upstream(json.dumps({
        "id": cap.upstream[-1]["id"], "result": {"sessionId": "U-Y"},
    }))
    alice_local = state.attachers["X"].primary_local_session
    bob_local = state.attachers["Y"].primary_local_session

    cap.per_client[alice.client_id].clear()
    cap.per_client[bob.client_id].clear()

    # Upstream sends a Network.responseReceived on session U-X.
    await router.forward_from_upstream(json.dumps({
        "method": "Network.responseReceived",
        "sessionId": "U-X",
        "params": {"requestId": "REQ-1"},
    }))
    assert len(cap.per_client[alice.client_id]) == 1
    assert cap.per_client[alice.client_id][0]["sessionId"] == alice_local
    assert cap.per_client[bob.client_id] == []  # bob does NOT receive


@pytest.mark.asyncio
async def test_browser_level_event_broadcasts_to_all_clients():
    """No sessionId → broadcast (spec §9.3 multi-client event isolation)."""
    state, router, cap, (alice, bob) = _setup("alice", "bob")
    await router.forward_from_upstream(json.dumps({
        "method": "Target.targetCreated",
        "params": {"targetInfo": {"targetId": "NEW", "type": "page",
                                  "url": "https://new/", "title": "n"}},
    }))
    # Both received.
    assert len(cap.per_client[alice.client_id]) == 1
    assert len(cap.per_client[bob.client_id]) == 1


# ---- session-scoped event observability stays correct --------------------


@pytest.mark.asyncio
async def test_target_destroyed_drops_target_from_table_and_broadcasts():
    state, router, cap, (client,) = _setup()
    state.note_target_info({"targetId": "T", "type": "page",
                            "url": "https://x/", "title": ""})
    state.last_activated_at["T"] = time.time()
    await router.forward_from_upstream(json.dumps({
        "method": "Target.targetDestroyed", "params": {"targetId": "T"},
    }))
    assert "T" not in state.targets


@pytest.mark.asyncio
async def test_inspector_detached_triggers_disconnect():
    state, router, cap, (client,) = _setup()
    await router.forward_from_upstream(json.dumps({
        "method": "Inspector.detached", "params": {"reason": "target_closed"},
    }))
    assert cap.disconnect_calls == ["chrome_exit"]


# ---- Task #76: pre-open buffer + race fix --------------------------------


@pytest.mark.asyncio
async def test_pre_open_buffer_holds_frame_until_upstream_ready():
    """v0.3 race fix (Task #76).

    Reproduction shape: a client sends a CDP frame while upstream is still
    CONNECTING (or DISCONNECTED). The pre-fix daemon dropped the frame with
    a WARNING log; the client then sat at the 30s CDP timeout. The fix
    buffers the frame and replays it via drain_pre_open_buffers() once
    upstream transitions to CONNECTED.
    """
    state, router, cap, (client,) = _setup(
        phase=UpstreamPhase.DISCONNECTED, wire_upstream=False)
    await router.route_from_client(client, json.dumps({
        "id": 1, "method": "Browser.getVersion",
    }))
    assert cap.upstream == [], "frame must NOT be sent while upstream is closed"
    assert len(client.pre_open_buffer) == 1

    # Now simulate the upstream coming online.
    state.upstream_phase = UpstreamPhase.CONNECTED
    router.update_upstream_send(cap.upstream_send)
    await router.drain_pre_open_buffers()

    # The buffered frame is replayed verbatim (id-translated for upstream).
    assert len(cap.upstream) == 1
    assert cap.upstream[0]["method"] == "Browser.getVersion"
    assert client.pre_open_buffer == deque()


@pytest.mark.asyncio
async def test_pre_open_buffer_overflow_returns_cdp_error_32603():
    """The 101st frame while upstream isn't open gets -32603 immediately.
    Older frames are preserved so they replay in order on drain.
    """
    from browserwright.daemon.server.state import PRE_OPEN_BUFFER_LIMIT

    state, router, cap, (client,) = _setup(
        phase=UpstreamPhase.DISCONNECTED, wire_upstream=False)
    # Fill the buffer to capacity.
    for i in range(PRE_OPEN_BUFFER_LIMIT):
        await router.route_from_client(client, json.dumps({
            "id": i, "method": "Browser.getVersion",
        }))
    assert len(client.pre_open_buffer) == PRE_OPEN_BUFFER_LIMIT
    assert cap.per_client[client.client_id] == []

    # The 101st frame: error reply, buffer NOT extended.
    await router.route_from_client(client, json.dumps({
        "id": 999, "method": "Browser.getVersion",
    }))
    assert len(client.pre_open_buffer) == PRE_OPEN_BUFFER_LIMIT
    err = cap.per_client[client.client_id][-1]
    assert err["id"] == 999
    assert err["error"]["code"] == -32603
    assert "overflow" in err["error"]["message"].lower()


@pytest.mark.asyncio
async def test_pre_open_buffer_per_client_isolation():
    """Two clients buffering concurrently each have their own deque; on drain
    each client's frames replay in their own FIFO order — but no interleave
    guarantee across clients (which is fine, the upstream wire doesn't care).
    """
    state, router, cap, (alice, bob) = _setup(
        "alice", "bob", phase=UpstreamPhase.DISCONNECTED, wire_upstream=False)
    await router.route_from_client(alice, json.dumps({
        "id": 1, "method": "Browser.getVersion",
    }))
    await router.route_from_client(bob, json.dumps({
        "id": 99, "method": "Browser.getVersion",
    }))
    assert len(alice.pre_open_buffer) == 1
    assert len(bob.pre_open_buffer) == 1

    state.upstream_phase = UpstreamPhase.CONNECTED
    router.update_upstream_send(cap.upstream_send)
    await router.drain_pre_open_buffers()

    assert len(cap.upstream) == 2
    # Both replayed; method preserved.
    methods = {m["method"] for m in cap.upstream}
    assert methods == {"Browser.getVersion"}


# ---- Phase B: BrowserwrightDaemon.openBackgroundTab / closeTab ------------------


@pytest.mark.asyncio
async def test_open_background_without_callback_returns_method_not_found():
    """When backend != extension, the callback stays None and the handler
    must surface -32601 'requires extension backend' (NOT -32602 from
    missing-params)."""
    state, router, cap, (client,) = _setup(backend="extension")
    await router.route_from_client(client, json.dumps({
        "id": 1, "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/"},
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 1
    assert resp["error"]["code"] == -32601
    assert "extension backend" in resp["error"]["message"]


@pytest.mark.asyncio
async def test_open_background_with_callback_binds_session_and_attacher():
    """Happy path: callback wired, params valid → session binding + attacher
    ownership registered with the daemon-allocated LOCAL sessionId."""
    state, router, cap, (client,) = _setup(backend="extension")

    async def _fake_open(url: str, group_name: str | None = None, *, session_id: str | None = None, background: bool = True) -> dict:
        assert url == "https://example.com/"
        assert group_name == "Agent"
        return {
            "sessionId": "UPSTREAM-SID-42",
            "targetId": "ext-tab-42",
            "tabId": 42,
            "url": "https://example.com/",
            "title": "Example",
            "groupId": 7,
        }

    router._open_background_tab = _fake_open
    await router.route_from_client(client, json.dumps({
        "id": 5, "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://example.com/", "groupName": "Agent"},
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 5
    result = resp["result"]
    assert result["targetId"] == "ext-tab-42"
    assert result["tabId"] == 42
    assert result["groupId"] == 7
    local_sid = result["sessionId"]
    assert local_sid != "UPSTREAM-SID-42"
    # Session binding registered on the client + attacher table claimed.
    binding = client.sessions[local_sid]
    assert binding.upstream_session_id == "UPSTREAM-SID-42"
    assert binding.target_id == "ext-tab-42"
    assert binding.readonly is False
    own = state.attachers["ext-tab-42"]
    assert own.primary_client_id == client.client_id
    assert own.primary_local_session == local_sid


@pytest.mark.asyncio
async def test_open_background_on_rdp_ensures_upstream_before_raw_cdp():
    """RDP uses the same BrowserwrightDaemon.openBackgroundTab verb, but its
    raw-CDP command channel is only wired after the upstream has been opened."""
    state, router, cap, (client,) = _setup(backend="rdp", wire_upstream=False)
    client.session_id = "rdp-session"
    state.upstream_phase = UpstreamPhase.DISCONNECTED
    calls: list[str] = []

    async def ensure() -> None:
        cap.ensure_calls += 1
        state.upstream_phase = UpstreamPhase.CONNECTED

        async def rdp_cmd(method: str, params=None, session_id=None):
            calls.append(method)
            if method == "Target.createTarget":
                return {"id": -1, "result": {"targetId": "rdp-tab"}}
            if method == "Target.attachToTarget":
                return {"id": -1, "result": {"sessionId": "rdp-session"}}
            raise AssertionError(method)

        router._upstream_command = rdp_cmd

    router.bind_lifecycle(ensure, cap.trigger_disconnect)
    await router.route_from_client(client, json.dumps({
        "id": 6,
        "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://rdp.example/"},
    }))

    assert cap.ensure_calls == 1
    assert calls == ["Target.createTarget", "Target.attachToTarget"]
    result = cap.per_client[client.client_id][-1]["result"]
    assert result["targetId"] == "rdp-tab"
    assert result["groupId"] == -1


@pytest.mark.asyncio
async def test_open_background_missing_url_returns_invalid_params():
    """params.url is required; absence → -32602."""
    state, router, cap, (client,) = _setup(backend="extension")
    await router.route_from_client(client, json.dumps({
        "id": 9, "method": "BrowserwrightDaemon.openBackgroundTab",
    }))
    err = cap.per_client[client.client_id][-1]["error"]
    assert err["code"] == -32602
    assert "url" in err["message"]


@pytest.mark.asyncio
async def test_browser_operations_require_browserwright_session_id():
    state, router, cap, (client,) = _setup(backend="extension")
    client.session_id = None
    client.session_name = None
    await router.route_from_client(client, json.dumps({
        "id": 10,
        "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/"},
    }))
    assert cap.per_client[client.client_id][-1]["error"]["code"] == -32602

    await router.route_from_client(client, json.dumps({
        "id": 11,
        "method": "Browser.getVersion",
    }))
    msg = cap.per_client[client.client_id][-1]["error"]["message"]
    assert "requires websocket ?session" in msg


@pytest.mark.asyncio
async def test_browser_operation_rejects_session_mismatch():
    state, router, cap, (client,) = _setup(backend="extension")
    await router.route_from_client(client, json.dumps({
        "id": 12,
        "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/", "bsSession": "different-session"},
    }))
    err = cap.per_client[client.client_id][-1]["error"]
    assert err["code"] == -32602
    assert "session mismatch" in err["message"]


@pytest.mark.asyncio
async def test_close_tab_without_callback_returns_method_not_found():
    state, router, cap, (client,) = _setup(backend="extension")
    await router.route_from_client(client, json.dumps({
        "id": 1, "method": "BrowserwrightDaemon.closeTab",
        "params": {"sessionId": "anything"},
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["error"]["code"] == -32601
    assert "extension backend" in resp["error"]["message"]


@pytest.mark.asyncio
async def test_close_tab_with_callback_cleans_session_state():
    """Happy path: open a tab, then close it; session binding + attacher
    table cleared after the close completes."""
    state, router, cap, (client,) = _setup(backend="extension")
    upstream_sid = "UPSTREAM-SID-99"

    async def _fake_open(url: str, group_name: str | None = None, *, session_id: str | None = None, background: bool = True) -> dict:
        return {
            "sessionId": upstream_sid,
            "targetId": "ext-tab-99",
            "tabId": 99,
            "url": "https://x/",
            "title": "x",
            "groupId": 9,
        }

    captured_close: list[str] = []

    async def _fake_close(sid: str) -> dict:
        captured_close.append(sid)
        return {"ok": True, "tabId": 99}

    router._open_background_tab = _fake_open
    router._close_tab = _fake_close

    await router.route_from_client(client, json.dumps({
        "id": 1, "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/"},
    }))
    local_sid = cap.per_client[client.client_id][-1]["result"]["sessionId"]
    assert local_sid in client.sessions
    assert "ext-tab-99" in state.attachers

    await router.route_from_client(client, json.dumps({
        "id": 2, "method": "BrowserwrightDaemon.closeTab",
        "params": {"sessionId": local_sid},
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 2
    assert resp["result"] == {"ok": True, "tabId": 99}
    # Upstream got the UPSTREAM sid, not the local one.
    assert captured_close == [upstream_sid]
    # Session + attacher bindings are gone.
    assert local_sid not in client.sessions
    assert "ext-tab-99" not in state.attachers


@pytest.mark.asyncio
async def test_close_tab_unknown_local_session_returns_invalid_params():
    state, router, cap, (client,) = _setup(backend="extension")
    async def _fake_close(sid: str) -> dict:
        return {"ok": True, "tabId": 0}
    router._close_tab = _fake_close
    await router.route_from_client(client, json.dumps({
        "id": 1, "method": "BrowserwrightDaemon.closeTab",
        "params": {"sessionId": "no-such-local-sid"},
    }))
    err = cap.per_client[client.client_id][-1]["error"]
    assert err["code"] == -32602


@pytest.mark.asyncio
async def test_close_tab_by_target_id_works_across_client_boundary():
    """CLI use-case: one ws opens the tab (binding lives on that client),
    a SECOND (fresh) ws calls closeTab with targetId. Lookup must reach the
    global state.attachers table, not just the local client's sessions."""
    state, router, cap, (alice, bob) = _setup("alice", "bob", backend="extension")

    async def _fake_open(url: str, group_name: str | None = None, *, session_id: str | None = None, background: bool = True) -> dict:
        return {
            "sessionId": "UPSTREAM-SID-77",
            "targetId": "ext-tab-77",
            "tabId": 77,
            "url": "https://x/", "title": "x", "groupId": 9,
        }
    captured_close: list[str] = []
    async def _fake_close(sid: str) -> dict:
        captured_close.append(sid)
        return {"ok": True, "tabId": 77}
    router._open_background_tab = _fake_open
    router._close_tab = _fake_close

    # Alice opens; binding lands on alice.sessions only.
    await router.route_from_client(alice, json.dumps({
        "id": 1, "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/"},
    }))
    alice_local_sid = cap.per_client[alice.client_id][-1]["result"]["sessionId"]
    assert alice_local_sid in alice.sessions
    assert "ext-tab-77" in state.attachers

    # Bob (different client, no shared session state) closes by targetId.
    await router.route_from_client(bob, json.dumps({
        "id": 2, "method": "BrowserwrightDaemon.closeTab",
        "params": {"targetId": "ext-tab-77"},
    }))
    resp = cap.per_client[bob.client_id][-1]
    assert resp.get("id") == 2
    assert "result" in resp, resp
    assert captured_close == ["UPSTREAM-SID-77"]
    # Global cleanup: alice's binding gone, attacher gone.
    assert alice_local_sid not in alice.sessions
    assert "ext-tab-77" not in state.attachers


@pytest.mark.asyncio
async def test_close_tab_falls_back_to_by_target_id_when_opener_disconnected():
    """Opener (alice) opens the tab, then her transient ws disconnects —
    `release_client` reaps her sessions + the global attacher binding. A
    fresh ws (bob) then calls closeTab with targetId. The regular
    `_close_tab(upstream_sid)` path is unreachable (no attacher), so the
    handler must take the `_close_tab_by_target_id` fallback path that
    `test_close_tab_by_target_id_works_across_client_boundary` never
    exercises (alice stays attached in that test, so the attacher table
    still has an entry)."""
    state, router, cap, (alice, bob) = _setup("alice", "bob", backend="extension")

    async def _fake_open(url: str, group_name: str | None = None, *, session_id: str | None = None, background: bool = True) -> dict:
        return {
            "sessionId": "UPSTREAM-SID-88",
            "targetId": "ext-tab-88",
            "tabId": 88,
            "url": "https://y/", "title": "y", "groupId": 9,
        }
    by_sid_calls: list[str] = []
    by_tid_calls: list[str] = []

    async def _fake_close(sid: str) -> dict:
        by_sid_calls.append(sid)
        return {"ok": True, "tabId": 88}

    async def _fake_close_by_target(tid: str) -> dict:
        by_tid_calls.append(tid)
        return {"ok": True, "tabId": 88}

    router._open_background_tab = _fake_open
    router._close_tab = _fake_close
    router._close_tab_by_target_id = _fake_close_by_target

    # Alice opens the tab.
    await router.route_from_client(alice, json.dumps({
        "id": 1, "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://y/"},
    }))
    assert "ext-tab-88" in state.attachers

    # Simulate alice's transient ws dropping. release_client unbinds her
    # sessions and the global attacher binding via _unbind_session.
    state.release_client(alice.client_id)
    assert "ext-tab-88" not in state.attachers, \
        "release_client should drop the attacher binding"

    # Bob closes by targetId. No attacher → fallback path runs.
    await router.route_from_client(bob, json.dumps({
        "id": 2, "method": "BrowserwrightDaemon.closeTab",
        "params": {"targetId": "ext-tab-88"},
    }))
    resp = cap.per_client[bob.client_id][-1]
    assert resp.get("id") == 2
    assert "result" in resp, resp
    assert resp["result"]["ok"] is True
    assert resp["result"]["tabId"] == 88
    # Critical: it took the fallback path, NOT the regular one.
    assert by_tid_calls == ["ext-tab-88"]
    assert by_sid_calls == []


@pytest.mark.asyncio
async def test_router_release_client_detaches_primary_upstream_sessions():
    """A disappearing websocket must not leave chrome.debugger/CDP attached."""
    state, router, cap, (client,) = _setup("alice", backend="extension")
    state.bind_session(
        client.client_id, "local-primary", "UPSTREAM-PRIMARY",
        "ext-tab-1", readonly=False)
    state.claim_attacher(
        "ext-tab-1", client.client_id, "local-primary", "UPSTREAM-PRIMARY")
    state.bind_session(
        client.client_id, "local-reader", "UPSTREAM-READER",
        "ext-tab-2", readonly=True)

    released = await router.release_client(client.client_id)

    assert released is client
    assert client.client_id not in state.clients
    assert "ext-tab-1" not in state.attachers
    detach_frames = [
        m for m in cap.upstream
        if m.get("method") == "Target.detachFromTarget"
    ]
    assert [m["params"]["sessionId"] for m in detach_frames] == [
        "UPSTREAM-PRIMARY"
    ]


@pytest.mark.asyncio
async def test_close_tab_without_session_or_target_id_returns_invalid_params():
    state, router, cap, (client,) = _setup()
    async def _fake_close(sid: str) -> dict:
        return {"ok": True, "tabId": 0}
    router._close_tab = _fake_close
    await router.route_from_client(client, json.dumps({
        "id": 1, "method": "BrowserwrightDaemon.closeTab",
        "params": {},
    }))
    err = cap.per_client[client.client_id][-1]["error"]
    assert err["code"] == -32602
    assert "sessionId or params.targetId" in err["message"]


@pytest.mark.asyncio
async def test_pre_open_buffer_browserdaemon_namespace_bypasses_gate():
    """BrowserwrightDaemon.* commands self-answer without touching upstream — they
    must NOT get buffered when upstream is closed.
    """
    state, router, cap, (client,) = _setup(
        phase=UpstreamPhase.DISCONNECTED, wire_upstream=False)
    await router.route_from_client(client, json.dumps({
        "id": 1, "method": "BrowserwrightDaemon.getBackendInfo",
    }))
    assert client.pre_open_buffer == deque()
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 1
    assert "result" in resp




# ---- P5: BrowserwrightDaemon.endSession dispatch --------------------------------


@pytest.mark.asyncio
async def test_end_session_dispatch_invokes_callback():
    """P5: BrowserwrightDaemon.endSession routes to the wired callback with the
    session id and returns its {closed, kept} result."""
    state, router, cap, (client,) = _setup()
    seen: list[str] = []

    async def _fake_end(session_id: str) -> dict:
        seen.append(session_id)
        return {"closed": [30], "kept": [88]}

    router._end_session = _fake_end
    await router.route_from_client(client, json.dumps({
        "id": 9, "method": "BrowserwrightDaemon.endSession",
        "params": {"session": "A"},
    }))
    assert seen == ["A"]
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 9
    assert resp["result"] == {"closed": [30], "kept": [88]}


@pytest.mark.asyncio
async def test_recover_session_missing_group_id_returns_invalid_params():
    """No params on an extension context → -32602 (validation FIRST so the
    schema-lock smoke test sees code != -32601). Recovery keys on the persisted
    groupId now, not the title."""
    state, router, cap, (client,) = _setup(backend="extension")
    await router.route_from_client(client, json.dumps({
        "id": 1, "method": "BrowserwrightDaemon.recoverSession",
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 1
    assert resp["error"]["code"] == -32602
    assert "groupId" in resp["error"]["message"]


@pytest.mark.asyncio
async def test_recover_session_on_rdp_is_honest_noop_not_minus_32601():
    """#3: recoverSession must NEVER surface -32601 on rdp. An ephemeral rdp
    Chrome has nothing durable to recover (surviving targets are re-attached by
    the skill's in-process / ledger fast paths), so the honest answer is a
    no-op success with empty ``recovered`` — not 'requires the extension
    backend'. (rdp contexts never wire a _recover_session callback.)"""
    state, router, cap, (client,) = _setup(backend="rdp")
    router._recover_session = None
    await router.route_from_client(client, json.dumps({
        "id": 2, "method": "BrowserwrightDaemon.recoverSession",
        "params": {"groupId": 7, "bsSession": "5"},
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 2
    assert "error" not in resp, resp
    assert resp["result"]["recovered"] == []


@pytest.mark.asyncio
async def test_recover_session_with_callback_binds_and_returns_payload():
    """Happy path: callback wired + valid groupName → session binding +
    attacher claimed, payload (incl. recovered list) returned. The recover
    callback only exists on the EXTENSION context (rdp short-circuits to a
    no-op), so this exercises backend=extension."""
    state, router, cap, (client,) = _setup(backend="extension")
    seen: list[tuple] = []

    async def _fake_recover(bs_session, *, group_id) -> dict:
        seen.append((bs_session, group_id))
        return {
            "sessionId": "UPSTREAM-SID-7",
            "targetId": "ext-tab-7",
            "tabId": 7,
            "url": "https://recovered/",
            "title": "Recovered",
            "groupId": 4,
            "recovered": [7, 8],
        }

    router._recover_session = _fake_recover
    await router.route_from_client(client, json.dumps({
        "id": 3, "method": "BrowserwrightDaemon.recoverSession",
        "params": {"groupId": 4, "bsSession": "bs-42"},
    }))
    assert seen == [("bs-42", 4)]
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 3
    result = resp["result"]
    assert result["targetId"] == "ext-tab-7"
    assert result["tabId"] == 7
    assert result["groupId"] == 4
    assert result["recovered"] == [7, 8]
    local_sid = result["sessionId"]
    assert local_sid != "UPSTREAM-SID-7"
    binding = client.sessions[local_sid]
    assert binding.upstream_session_id == "UPSTREAM-SID-7"
    assert binding.target_id == "ext-tab-7"
    assert binding.readonly is False
    own = state.attachers["ext-tab-7"]
    assert own.primary_client_id == client.client_id
    assert own.primary_local_session == local_sid


@pytest.mark.asyncio
async def test_ensure_session_requires_session_bound_websocket():
    state, router, cap, (client,) = _setup()
    client.session_id = None
    client.session_name = None
    await router.route_from_client(client, json.dumps({
        "id": 10, "method": "BrowserwrightDaemon.ensureSession",
        "params": {"session": "7"},
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 10
    assert resp["error"]["code"] == -32602
    assert "?session=<id>" in resp["error"]["message"]


@pytest.mark.asyncio
async def test_ensure_session_rejects_session_mismatch():
    state, router, cap, (client,) = _setup()
    client.session_id = "7"
    await router.route_from_client(client, json.dumps({
        "id": 11, "method": "BrowserwrightDaemon.ensureSession",
        "params": {"session": "8"},
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 11
    assert resp["error"]["code"] == -32602
    assert "session mismatch" in resp["error"]["message"]


@pytest.mark.asyncio
async def test_ensure_session_uses_connection_bound_session():
    state, router, cap, (client,) = _setup()
    client.session_id = "7"
    calls: list[str | None] = []

    class _Daemon:
        def context_for(self, session_id):
            calls.append(session_id)

    router.daemon = _Daemon()
    await router.route_from_client(client, json.dumps({
        "id": 12, "method": "BrowserwrightDaemon.ensureSession",
        "params": {"session": "7"},
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 12
    assert resp["result"] == {"ok": True}
    assert calls == ["7"]


@pytest.mark.asyncio
async def test_recover_session_callback_failure_returns_error():
    # The recover callback only exists on the extension context.
    state, router, cap, (client,) = _setup(backend="extension")

    async def _fake_recover(bs_session, *, group_id) -> dict:
        raise RuntimeError("empty group")

    router._recover_session = _fake_recover
    await router.route_from_client(client, json.dumps({
        "id": 4, "method": "BrowserwrightDaemon.recoverSession",
        "params": {"groupId": 99},
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["error"]["code"] == -32603


@pytest.mark.asyncio
async def test_end_session_requires_session_param():
    state, router, cap, (client,) = _setup()
    router._end_session = None
    await router.route_from_client(client, json.dumps({
        "id": 9, "method": "BrowserwrightDaemon.endSession",
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_get_targets_scoped_to_client_session_on_extension():
    """#2: on the extension backend Target.getTargets must be scoped to the
    REQUESTING client's session tab group — never the global ghost list — so
    two sessions sharing one Chrome are mutually invisible. The daemon answers
    locally from the scoped query; it does NOT forward to the shared upstream."""
    state, router, cap, (client,) = _setup(backend="extension")
    client.session_id = "A"
    client.session_name = "grp-A"

    async def fake_scoped(session_id):
        assert session_id == "A"  # scoped by the session's bound groupId, no title
        return [{"targetId": "ext-tab-1", "type": "page",
                 "url": "https://a/", "title": "A", "attached": True}]

    router._scoped_targets = fake_scoped
    await router.route_from_client(client, json.dumps({
        "id": 5, "method": "Target.getTargets",
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 5
    assert {t["targetId"] for t in resp["result"]["targetInfos"]} == {"ext-tab-1"}
    assert not cap.upstream  # answered locally, not forwarded to the shared upstream


@pytest.mark.asyncio
async def test_open_background_uses_session_name_as_group_when_groupname_omitted():
    """#2: the skill's open() omits groupName, so the daemon resolves the tab
    group from the session's NAME (read off the connection, fixed at
    `session new`) — an authoritative lookup, not a fallback. Otherwise the tab
    is opened UNGROUPED and the session has no findable browser to enumerate."""
    state, router, cap, (client,) = _setup(backend="extension")
    client.session_id = "7"
    client.session_name = "my-sess"
    seen: dict = {}

    async def _fake_open(url, group_name=None, *, session_id=None, background=True):
        seen["group_name"] = group_name
        seen["session_id"] = session_id
        return {"sessionId": "U-1", "targetId": "ext-tab-1", "tabId": 1,
                "url": url, "title": "t", "groupId": 9}

    router._open_background_tab = _fake_open
    await router.route_from_client(client, json.dumps({
        "id": 4, "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/", "bsSession": "7"},
    }))
    assert seen["group_name"] == "my-sess"  # anchored on the session name
    assert seen["session_id"] == "7"


@pytest.mark.asyncio
async def test_open_background_falls_back_to_session_id_group_title():
    """The daemon may not share BS_HOME with the skill, so it may not know the
    ledger name. First open still needs a title so Chrome creates a group and
    returns a durable numeric groupId for later recovery."""
    state, router, cap, (client,) = _setup(backend="extension")
    client.session_id = "7"
    client.session_name = None
    seen: dict = {}

    async def _fake_open(url, group_name=None, *, session_id=None, background=True):
        seen["group_name"] = group_name
        seen["session_id"] = session_id
        return {"sessionId": "U-1", "targetId": "ext-tab-1", "tabId": 1,
                "url": url, "title": "t", "groupId": 9}

    router._open_background_tab = _fake_open
    await router.route_from_client(client, json.dumps({
        "id": 4, "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/", "bsSession": "7"},
    }))
    assert seen["group_name"] == "7"
    assert seen["session_id"] == "7"


@pytest.mark.asyncio
async def test_open_background_rejects_bssession_on_sessionless_client(tmp_home, monkeypatch):
    """The websocket session id is the isolation key. ``bsSession`` may only
    repeat the bound session; it cannot substitute for a missing ``?session``."""
    from browserwright import session_registry as reg

    monkeypatch.setenv("BS_HOME", str(tmp_home / "bs-home"))
    sid = reg.allocate(backend="extension", owner="attach", name="ledger-name")
    state, router, cap, (client,) = _setup(backend="extension")
    client.session_id = None
    client.session_name = None

    async def _fake_open(url, group_name=None, *, session_id=None, background=True):
        raise AssertionError("sessionless open must not reach extension upstream")
        return {"sessionId": "U-1", "targetId": "ext-tab-1", "tabId": 1,
                "url": url, "title": "t", "groupId": 9}

    router._open_background_tab = _fake_open
    await router.route_from_client(client, json.dumps({
        "id": 4,
        "method": "BrowserwrightDaemon.openBackgroundTab",
        "params": {"url": "https://x/", "bsSession": sid},
    }))
    msg = cap.per_client[client.client_id][-1]
    assert msg["error"]["code"] == -32602
    assert "requires websocket ?session" in msg["error"]["message"]
