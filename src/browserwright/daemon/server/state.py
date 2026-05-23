"""Centralized daemon state (§8.5).

v0.3 expansion of the v0.2 single-client model:

- `client` (singular) → `clients: dict[id, ClientState]`
- per-client `sessions: dict[local_session_id, SessionBinding]`
- `upstream_to_local: dict[upstream_session_id, list[SessionBinding]]`
  (list because one upstream session can serve N local sessions via shared-read)
- `attachers: dict[target_id, AttachOwnership]` — the single-attacher rule
- `pending_requests: dict[upstream_id, PendingRequest]` — id translation for
  CDP response routing (CDP responses correlate by id, not by sessionId, so
  ids must be unique across clients on the upstream wire)

The transitions still go through the same observer pattern as v0.2.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Literal


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
    sessionId. Multiple SessionBindings can point at the same upstream session
    when shared-read is active.
    """
    client_id: int
    local_session_id: str       # what THIS client sees
    upstream_session_id: str    # what Chrome sees
    target_id: str              # known from the attach response onward
    readonly: bool = False      # True ⇒ shared-read; commands rejected -32602


@dataclass
class AttachOwnership:
    """Per-targetId ownership record. The primary client has full read+write;
    additional readers (shared-read) get read-only sessions backed by the
    same upstream session.
    """
    target_id: str
    primary_client_id: int
    primary_local_session: str
    upstream_session_id: str
    readers: list[tuple[int, str]] = field(default_factory=list)
    """(client_id, local_session_id) tuples for read-only attachers."""

    def all_local_sessions(self) -> list[tuple[int, str]]:
        """Primary first, then readers — useful for event fan-out within a session."""
        return [(self.primary_client_id, self.primary_local_session), *self.readers]


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
    # Whether the client passed `flags.allowSecondaryReadOnly=true` in the
    # attach. Daemon doesn't actually forward this flag — the routing decision
    # is made locally — but we remember it for the rare case where the primary
    # owner is the SAME client (then we keep regular write semantics).
    attach_allow_shared_read: bool = False
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
    sessions: dict[str, SessionBinding] = field(default_factory=dict)
    """local_session_id → SessionBinding owned by this client."""
    subscribed_focus: bool = False
    connected_at: float = field(default_factory=time.time)
    last_command_at: float = field(default_factory=time.time)
    # Spec §10 open question: when a client sends a frame while upstream is
    # still in DISCONNECTED / CONNECTING phase, the daemon buffers the frame
    # per-client (FIFO, capacity 100) and drains it once upstream is OPEN.
    # The 101st frame is rejected with CDP error -32603. Without this, the
    # frame is silently dropped and the client times out at the 30s CDP
    # boundary (Task #76).
    pre_open_buffer: deque[str] = field(default_factory=deque)

    def owns_session(self, local_session_id: str) -> bool:
        return local_session_id in self.sessions


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

    # Heuristic active-tab table (unchanged from v0.2).
    last_activated_at: dict[str, float] = field(default_factory=dict)
    targets: dict[str, dict[str, Any]] = field(default_factory=dict)

    last_activity_at: float = field(default_factory=time.time)
    last_popup_resolved_at: float | None = None

    _subscribers: list[Callable[[str, dict], Awaitable[None]]] = field(default_factory=list)

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
            # Primary owner is leaving. If there's a reader, promote them;
            # otherwise drop the attachment. NOTE: actually transferring write
            # ownership without consent is unusual — for v0.3 we just drop and
            # let the upstream session die. spec doesn't mandate promotion.
            self.attachers.pop(binding.target_id, None)
        else:
            # Reader leaving.
            own.readers = [
                (cid, lsid) for (cid, lsid) in own.readers
                if not (cid == binding.client_id and lsid == binding.local_session_id)
            ]

    # ---- session table ----------------------------------------------------

    def bind_session(
        self,
        client_id: int,
        local_session_id: str,
        upstream_session_id: str,
        target_id: str,
        *,
        readonly: bool,
    ) -> SessionBinding:
        client = self.clients[client_id]
        binding = SessionBinding(
            client_id=client_id,
            local_session_id=local_session_id,
            upstream_session_id=upstream_session_id,
            target_id=target_id,
            readonly=readonly,
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

    def add_reader(
        self,
        target_id: str,
        client_id: int,
        local_session_id: str,
    ) -> AttachOwnership | None:
        own = self.attachers.get(target_id)
        if own is None:
            return None
        own.readers.append((client_id, local_session_id))
        return own

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
        attach_allow_shared_read: bool = False,
    ) -> None:
        self.pending_requests[upstream_id] = PendingRequest(
            client_id=client_id,
            client_request_id=client_request_id,
            method=method,
            attach_target_id=attach_target_id,
            attach_allow_shared_read=attach_allow_shared_read,
        )

    def take_pending(self, upstream_id: int) -> PendingRequest | None:
        return self.pending_requests.pop(upstream_id, None)

    # ---- subscriptions / transitions (unchanged) -------------------------

    def subscribe(self, fn: Callable[[str, dict], Awaitable[None]]) -> None:
        self._subscribers.append(fn)

    async def _emit(self, event: str, payload: dict) -> None:
        for fn in list(self._subscribers):
            try:
                await fn(event, payload)
            except Exception:
                pass

    async def begin_connecting(self, backend_name: str) -> None:
        self.upstream_phase = UpstreamPhase.CONNECTING
        self.backend_name = backend_name
        await self._emit("upstream.connecting", {"backend": backend_name})

    async def set_connected(self, ws_url: str, *, was_popup: bool) -> None:
        self.upstream_phase = UpstreamPhase.CONNECTED
        self.upstream_ws_url = ws_url
        if was_popup:
            self.last_popup_resolved_at = time.time()
        await self._emit("upstream.ready", {"ws_url": ws_url})

    async def begin_closing(self, reason: CloseReason) -> None:
        self.upstream_phase = UpstreamPhase.CLOSING
        self.last_close_reason = reason
        await self._emit("upstream.closing", {"reason": reason})

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
        await self._emit("upstream.disconnected", {"reason": self.last_close_reason})

    # ---- heuristic active-tab table (unchanged) --------------------------

    def note_activate(self, target_id: str) -> None:
        self.last_activated_at[target_id] = time.time()
        self.last_activity_at = time.time()

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
        self.last_activated_at.pop(target_id, None)
        # Also drop any attacher record (the upstream session is gone with it).
        self.attachers.pop(target_id, None)

    def best_active_tab(self) -> dict | None:
        internals = (
            "chrome://", "chrome-untrusted://", "devtools://", "edge://",
            "chrome-extension://", "about:", "view-source:",
        )
        eligible: list[tuple[float, str, dict]] = []
        for tid, meta in self.targets.items():
            if meta.get("type") != "page":
                continue
            url = meta.get("url") or ""
            if url.startswith(internals):
                continue
            score = self.last_activated_at.get(tid, 0.0)
            eligible.append((score, tid, meta))
        if not eligible:
            return None
        eligible.sort(key=lambda r: r[0], reverse=True)
        score, tid, meta = eligible[0]
        since = (time.time() - score) if score > 0 else None
        return {
            "targetId": tid,
            "url": meta.get("url", ""),
            "title": meta.get("title", ""),
            "accuracy": "heuristic-recent-activate",
            "since_seconds": since,
        }

    # ---- v0.2 compat: legacy `client` accessor ---------------------------

    @property
    def client(self) -> ClientState | None:
        """v0.2 callers used `state.client` (singular). v0.3 supports many,
        but keeping this convenient when there happens to be exactly one
        client connected makes the close-etiquette path simpler in single-
        client deployments. None when 0 or >1 clients."""
        if len(self.clients) == 1:
            return next(iter(self.clients.values()))
        return None
