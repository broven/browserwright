"""Centralized daemon state (§8.5).

v0.3 expansion of the v0.2 single-client model:

- `client` (singular) → `clients: dict[id, ClientState]`
- per-client `sessions: dict[local_session_id, SessionBinding]`
- `upstream_to_local: dict[upstream_session_id, list[SessionBinding]]`
- `attachers: dict[target_id, AttachOwnership]` — the single-attacher rule
- `pending_requests: dict[upstream_id, PendingRequest]` — id translation for
  CDP response routing (CDP responses correlate by id, not by sessionId, so
  ids must be unique across clients on the upstream wire)
"""
from __future__ import annotations

import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


# Per-client buffer size for frames received while upstream is still
# opening. Spec §10 open question — "buffer with limit 100, error past that"
# was the resolution. Keep here as a module constant so tests can override.
PRE_OPEN_BUFFER_LIMIT = 100


# ---- enums -----------------------------------------------------------------


class UpstreamPhase(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING   = "CONNECTING"
    CONNECTED    = "CONNECTED"
    CLOSING      = "CLOSING"


CloseReason = Literal[
    "chrome_exit", "backend_lost", "idle_close",
    "daemon_shutdown", "skill_disconnect",
]


# ---- session / attach data classes ----------------------------------------


@dataclass
class SessionBinding:
    """One local sessionId, owned by a specific client, mapped to one upstream
    sessionId.
    """
    client_id: int
    local_session_id: str       # what THIS client sees
    upstream_session_id: str    # what Chrome sees
    target_id: str              # known from the attach response onward


@dataclass
class AttachOwnership:
    """Per-targetId ownership record. The primary client has full read+write."""
    target_id: str
    primary_client_id: int
    primary_local_session: str
    upstream_session_id: str


@dataclass
class PendingRequest:
    """A client request awaiting its upstream response. We translate ids
    because CDP responses correlate by id, and multiple clients can otherwise
    pick the same numeric id.
    """
    client_id: int
    client_request_id: int      # the id the client originally sent
    method: str                 # raw method (used by attach interceptor)
    # For Target.attachToTarget we need to remember which targetId the client
    # asked for so we can fill the attachers table when the response arrives.
    attach_target_id: str | None = None
    # When the daemon put this request on the upstream wire. `time.monotonic`,
    # not `time.time`: this exists to answer "how long has this been waiting",
    # and a wall clock that steps backwards would answer it wrong. Without it a
    # 50ms request and a 5-minute one are the same row in `pending_requests`,
    # which is exactly what made a hung daemon indistinguishable from an idle
    # one. Read by `status.snapshot` / `browserwright-daemon ps`.
    started_at: float = field(default_factory=time.monotonic)

    def elapsed_s(self, *, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, now - self.started_at)
    # Sessionless-vs-sessioned: if the original request carried a sessionId,
    # the response must carry the *local* sessionId back. CDP responses on
    # session-scoped requests echo the session-id in some daemon-mediated
    # synthetic events; for plain {"id","result"} responses CDP itself doesn't
    # echo sessionId so we don't need this for vanilla responses.


# ---- ClientState -----------------------------------------------------------


@dataclass
class ClientState:
    """One connected ws client. v0.3: N of these exist at a time."""
    client_id: int
    label: str
    # The browserwright session this client is bound to (ledger id) + its name.
    # Set from the ws ``?session=<id>`` query at connect. On the shared
    # extension context these scope browser-level enumeration (Target.getTargets)
    # to THIS session's tab group so sessions are mutually invisible. None for
    # the bare REPL client / single-context unit tests.
    session_id: str | None = None
    session_name: str | None = None
    # Opaque daemon lease used to revoke this exact control transport when its
    # Browserwright session ends.  None for sessionless/unit-test clients.
    connection_token: object | None = None
    sessions: dict[str, SessionBinding] = field(default_factory=dict)
    """local_session_id → SessionBinding owned by this client."""
    # Wall-clock, deliberately: these two are rendered as "connected 4m ago" /
    # "last command 12s ago" by `browserwright-daemon ps`, which is a human
    # question about a human clock. Both are written on every decoded client
    # frame (`Router.route_from_client`) and read by `status.snapshot` — until
    # that reader existed, `last_command_at` was write-only.
    connected_at: float = field(default_factory=time.time)
    last_command_at: float = field(default_factory=time.time)
    # Spec §10 open question: when a client sends a frame while upstream is
    # still in DISCONNECTED / CONNECTING phase, the daemon buffers the frame
    # per-client (FIFO, capacity 100) and drains it once upstream is OPEN.
    # The 101st frame is rejected with CDP error -32603. Without this, the
    # frame is silently dropped and the client times out at the 30s CDP
    # boundary (Task #76).
    pre_open_buffer: deque[str] = field(default_factory=deque)


# ---- DaemonState -----------------------------------------------------------


@dataclass
class DaemonState:
    """Whole-process mutable state. ONE instance per daemon."""
    backend_name: str
    upstream_phase: UpstreamPhase = UpstreamPhase.DISCONNECTED
    upstream_ws_url: str | None = None
    last_close_reason: CloseReason | None = None

    # v0.3: many clients keyed by client_id (monotonic).
    clients: dict[int, ClientState] = field(default_factory=dict)
    _next_client_id: itertools.count = field(
        default_factory=lambda: itertools.count(1))

    # Local→upstream session lookup is on ClientState. Upstream→[locals] lives here
    # for fast event fan-out (sessionId-carrying events look up here).
    upstream_to_locals: dict[str, list[SessionBinding]] = field(default_factory=dict)

    # Single-attacher table: targetId → AttachOwnership.
    attachers: dict[str, AttachOwnership] = field(default_factory=dict)

    # Pending request map keyed by the *upstream* (translated) id.
    pending_requests: dict[int, PendingRequest] = field(default_factory=dict)
    # Allocator for upstream ids. Stays positive — daemon-internal ids on
    # UpstreamConnection.send_command live in big negatives.
    _next_upstream_id: itertools.count = field(
        default_factory=lambda: itertools.count(1))

    # Target visibility table (targetId → {type, url, title}).
    targets: dict[str, dict[str, Any]] = field(default_factory=dict)

    last_activity_at: float = field(default_factory=time.time)

    # ---- client lifecycle -------------------------------------------------

    def allocate_client(self, label: str, *, client_id: int | None = None,
                        session_id: str | None = None,
                        session_name: str | None = None) -> ClientState:
        # Phase 2: the Daemon passes a globally-unique client_id (unique across
        # all UpstreamContexts) so daemon logs never show two clients sharing a
        # number. When omitted (single-context callers / tests), fall back to
        # this state's own monotonic counter.
        cid = client_id if client_id is not None else next(self._next_client_id)
        c = ClientState(client_id=cid, label=label or "anonymous",
                        session_id=session_id, session_name=session_name)
        self.clients[cid] = c
        return c

    def release_client(self, client_id: int) -> ClientState | None:
        """Drop a client + clean up all its sessions and owned attachments.

        Returns the released ClientState (so the caller can iterate owned
        sessions for synthesizing detach events to send before closing the
        ws). The caller MUST handle those side effects — state.release_client
        only mutates state.
        """
        client = self.clients.pop(client_id, None)
        if client is None:
            return None
        # Walk sessions; for each, pull from upstream_to_locals and drop or
        # transfer attacher ownership.
        for local_sid, binding in list(client.sessions.items()):
            self._unbind_session(binding)
        return client

    def _unbind_session(self, binding: SessionBinding) -> None:
        """Internal — remove a SessionBinding from the upstream→local table
        and update attacher ownership accordingly."""
        # Pop from upstream_to_locals.
        bindings = self.upstream_to_locals.get(binding.upstream_session_id, [])
        bindings = [b for b in bindings if not (
            b.client_id == binding.client_id
            and b.local_session_id == binding.local_session_id)]
        if bindings:
            self.upstream_to_locals[binding.upstream_session_id] = bindings
        else:
            self.upstream_to_locals.pop(binding.upstream_session_id, None)
        # Attacher cleanup.
        own = self.attachers.get(binding.target_id)
        if own is None:
            return
        if (own.primary_client_id == binding.client_id
                and own.primary_local_session == binding.local_session_id):
            # Primary owner is leaving — drop the attachment and let the
            # upstream session die.
            self.attachers.pop(binding.target_id, None)

    # ---- session table ----------------------------------------------------

    def bind_session(
        self,
        client_id: int,
        local_session_id: str,
        upstream_session_id: str,
        target_id: str,
    ) -> SessionBinding:
        client = self.clients[client_id]
        binding = SessionBinding(
            client_id=client_id,
            local_session_id=local_session_id,
            upstream_session_id=upstream_session_id,
            target_id=target_id,
        )
        client.sessions[local_session_id] = binding
        self.upstream_to_locals.setdefault(upstream_session_id, []).append(binding)
        return binding

    def unbind_session_by_local(
        self, client_id: int, local_session_id: str
    ) -> SessionBinding | None:
        """Used on Target.detachFromTarget. Returns the binding removed, or None."""
        client = self.clients.get(client_id)
        if client is None:
            return None
        binding = client.sessions.pop(local_session_id, None)
        if binding is not None:
            self._unbind_session(binding)
        return binding

    # ---- attacher table ---------------------------------------------------

    def claim_attacher(
        self,
        target_id: str,
        client_id: int,
        local_session_id: str,
        upstream_session_id: str,
    ) -> None:
        """Record that `client_id` is the primary owner of `target_id`. The
        single-attacher check happened earlier in the router; this just
        commits the bookkeeping after the upstream attach succeeded."""
        self.attachers[target_id] = AttachOwnership(
            target_id=target_id,
            primary_client_id=client_id,
            primary_local_session=local_session_id,
            upstream_session_id=upstream_session_id,
        )

    # ---- pending request map ---------------------------------------------

    def allocate_upstream_id(self) -> int:
        return next(self._next_upstream_id)

    def remember_request(
        self,
        upstream_id: int,
        client_id: int,
        client_request_id: int,
        method: str,
        *,
        attach_target_id: str | None = None,
    ) -> None:
        self.pending_requests[upstream_id] = PendingRequest(
            client_id=client_id,
            client_request_id=client_request_id,
            method=method,
            attach_target_id=attach_target_id,
        )

    def take_pending(self, upstream_id: int) -> PendingRequest | None:
        return self.pending_requests.pop(upstream_id, None)

    # ---- phase transitions ------------------------------------------------

    async def begin_connecting(self, backend_name: str) -> None:
        self.upstream_phase = UpstreamPhase.CONNECTING
        self.backend_name = backend_name

    async def set_connected(self, ws_url: str) -> None:
        self.upstream_phase = UpstreamPhase.CONNECTED
        self.upstream_ws_url = ws_url

    async def begin_closing(self, reason: CloseReason) -> None:
        self.upstream_phase = UpstreamPhase.CLOSING
        self.last_close_reason = reason

    async def set_disconnected(self) -> None:
        self.upstream_phase = UpstreamPhase.DISCONNECTED
        self.upstream_ws_url = None
        # Wipe v0.3-only tables but keep client list; the client handlers will
        # individually release. attachers / pending_requests are upstream-tied.
        self.attachers.clear()
        self.pending_requests.clear()
        self.upstream_to_locals.clear()
        for c in self.clients.values():
            c.sessions.clear()

    # ---- target visibility table ------------------------------------------

    def note_target_info(self, info: dict) -> None:
        tid = info.get("targetId")
        if not isinstance(tid, str):
            return
        self.targets[tid] = {
            "type": info.get("type"),
            "url": info.get("url", ""),
            "title": info.get("title", ""),
        }

    def note_target_destroyed(self, target_id: str) -> None:
        self.targets.pop(target_id, None)
        # Also drop any attacher record (the upstream session is gone with it).
        self.attachers.pop(target_id, None)
