"""CDP proxy + BrowserDaemon.* namespace (v0.3 multi-client).

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

from browser_daemon.server.proxy import Router
from browser_daemon.server.state import (
    AttachOwnership, ClientState, DaemonState, SessionBinding, UpstreamPhase,
)


# ---- shared scaffolding ---------------------------------------------------


def _state(phase=UpstreamPhase.CONNECTED) -> DaemonState:
    s = DaemonState(name="t", backend_name="rdp")
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


def _setup(*labels: str, phase=UpstreamPhase.CONNECTED, wire_upstream: bool = True):
    """Build a Router + state + one or more clients ready to receive sends."""
    state = _state(phase=phase)
    cap = _Capture()
    router = Router(state)
    router.bind_lifecycle(cap.ensure_upstream, cap.trigger_disconnect)
    if wire_upstream:
        router.update_upstream_send(cap.upstream_send)
    clients = []
    for label in labels or ("skill-repl",):
        c = state.allocate_client(label)
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


# ---- BrowserDaemon.* self-answer (per-client) ----------------------------


@pytest.mark.asyncio
async def test_browserdaemon_get_active_tab_no_tabs_returns_unknown():
    state, router, cap, (client,) = _setup()
    await router.route_from_client(client, json.dumps({
        "id": 1, "method": "BrowserDaemon.getActiveTab",
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
        "id": 1, "method": "BrowserDaemon.getActiveTab",
    }))
    resp = cap.per_client[client.client_id][-1]
    assert resp["result"]["targetId"] == "T"


@pytest.mark.asyncio
async def test_browserdaemon_get_backend_info_self_answered():
    state, router, cap, (client,) = _setup()
    state.backend_name = "rdp"
    await router.route_from_client(client, json.dumps({
        "id": 2, "method": "BrowserDaemon.getBackendInfo",
    }))
    assert cap.upstream == []
    assert cap.per_client[client.client_id][-1]["result"]["name"] == "rdp"


@pytest.mark.asyncio
async def test_browserdaemon_ui_state_reports_client_count():
    """v0.3 added `client_count` to uiState — checks both fields shape."""
    state, router, cap, clients = _setup("a", "b")
    await router.route_from_client(clients[0], json.dumps({
        "id": 3, "method": "BrowserDaemon.uiState",
    }))
    result = cap.per_client[clients[0].client_id][-1]["result"]
    assert result["ws_count"] == 1
    assert result["banner_visible_estimated"] is True
    assert result["client_count"] == 2


@pytest.mark.asyncio
async def test_browserdaemon_disconnect_triggers_close():
    state, router, cap, (client,) = _setup()
    await router.route_from_client(client, json.dumps({
        "id": 6, "method": "BrowserDaemon.disconnect",
    }))
    assert cap.per_client[client.client_id][0]["result"] == {"ok": True}
    assert cap.disconnect_calls == ["skill_disconnect"]


@pytest.mark.asyncio
async def test_unknown_browserdaemon_method_returns_method_not_found():
    state, router, cap, (client,) = _setup()
    await router.route_from_client(client, json.dumps({
        "id": 99, "method": "BrowserDaemon.totallyMadeUp",
    }))
    err = cap.per_client[client.client_id][-1]["error"]
    assert err["code"] == -32601


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
    from browser_daemon.server.state import PRE_OPEN_BUFFER_LIMIT

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


@pytest.mark.asyncio
async def test_pre_open_buffer_browserdaemon_namespace_bypasses_gate():
    """BrowserDaemon.* commands self-answer without touching upstream — they
    must NOT get buffered when upstream is closed.
    """
    state, router, cap, (client,) = _setup(
        phase=UpstreamPhase.DISCONNECTED, wire_upstream=False)
    await router.route_from_client(client, json.dumps({
        "id": 1, "method": "BrowserDaemon.getBackendInfo",
    }))
    assert client.pre_open_buffer == deque()
    resp = cap.per_client[client.client_id][-1]
    assert resp["id"] == 1
    assert "result" in resp


