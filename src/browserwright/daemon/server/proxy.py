"""CDP proxy + BrowserwrightDaemon.* namespace router (v0.3 multi-client).

Three translation tables make v0.3 work:

1. **Request id** — every client-bound request id is replaced with a fresh
   upstream id. The PendingRequest lookup carries the (client_id,
   original_id) back when the upstream response arrives. Two reasons:
   (a) different clients otherwise pick colliding ids; (b) the Target.attach
   response needs to be intercepted server-side without the client knowing.

2. **sessionId** (local ↔ upstream) — each client gets its own sessionId
   namespace. Daemon allocates a UUID-like local sessionId when it first
   sees an upstream attach response, and translates in both directions on
   every subsequent message. Two routes get this:
     - command path: client → upstream rewrites params.sessionId
     - event path:   upstream → client picks owner(s) from upstream_to_locals

3. **attachers** (single-owner rule) — first attach to a targetId wins.
   Second attach without `allowSecondaryReadOnly` gets `-32602`. Second
   attach with the flag becomes a read-only reader sharing the existing
   upstream session.

`BrowserwrightDaemon.*` self-answer + heuristic active-tab table behave the same
as v0.2.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

from ..observability import metrics
from .state import (
    AttachOwnership, ClientState, DaemonState, PendingRequest, SessionBinding,
    UpstreamPhase, PRE_OPEN_BUFFER_LIMIT,
)
# The BrowserwrightDaemon.* verb handlers live in verbs.py (Batch 4a split);
# the shared response helpers are re-exported here so existing importers
# (`from .proxy import _cmd_result`, `proxy._error_response`, …) keep working.
from .verbs import (  # noqa: F401 - re-exports are part of proxy's surface
    SessionVerbsMixin, _cmd_result, _error_response, _new_local_session_id,
    _result_response,
)

logger = logging.getLogger(__name__)


# ---- helpers --------------------------------------------------------------


def _json_safe(text: str) -> dict | None:
    try:
        v = json.loads(text)
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, dict) else None


def _event(method: str, params: dict, session_id: str | None = None) -> str:
    msg: dict = {"method": method, "params": params}
    if session_id is not None:
        msg["sessionId"] = session_id
    return json.dumps(msg)


# ---- the router -----------------------------------------------------------


class Router(SessionVerbsMixin):
    """Multi-client v0.3 router.

    Bindings change shape from v0.2:
    - `client_send` becomes a `dict[client_id, send_fn]` registry, so the
      router can fan out events to the right subset of clients.
    - `upstream_send` remains a single callable (only one upstream conn).
    """

    def __init__(self, state: DaemonState):
        self.state = state
        # Phase 2: back-reference to the global Daemon, set by Daemon.__init__
        # / _ensure_rdp_context. Lets the session-verb handlers (ensureSession /
        # endSession) create or drop an rdp UpstreamContext. None in unit tests
        # that build a bare Router — those handlers degrade gracefully.
        self.daemon: object | None = None
        self._upstream_send: Callable[[str], Awaitable[None]] | None = None
        self._client_sends: dict[int, Callable[[str], Awaitable[None]]] = {}
        self._ensure_upstream: Callable[[], Awaitable[None]] | None = None
        self._trigger_disconnect: Callable[[str], Awaitable[None]] | None = None
        # Extension-backend-only verbs. listener.py sets these only when
        # backend=extension; other backends leave them None and the proxy
        # handlers respond -32601. `_close_tab_by_target_id` is the fallback
        # close-path used when the original opener disconnected and the
        # per-client session binding was reaped.
        self._attach_active_tab: Callable[[], Awaitable[dict]] | None = None
        self._open_background_tab: (
            Callable[[str, str | None], Awaitable[dict]] | None) = None
        self._close_tab: Callable[[str], Awaitable[dict]] | None = None
        self._close_tab_by_target_id: (
            Callable[[str], Awaitable[dict]] | None) = None
        # P5: per-session teardown (extension backend only). Closes the
        # session's owned tabs, keeps borrowed ones.
        self._end_session: Callable[[str], Awaitable[dict]] | None = None
        # Session-reconnect-recovery (extension backend only). Rebuilds a
        # session's tab bindings from the durable tab group, found by its
        # persisted numeric groupId (not the title). Signature:
        # (bs_session | None, *, group_id) -> dict.
        self._recover_session: (
            Callable[..., Awaitable[dict]] | None) = None
        self._wait_session_announce: (
            Callable[[str, float], Awaitable[bool]] | None) = None
        self._userscript_request: (
            Callable[[str, dict], Awaitable[dict | None]] | None) = None
        self._reload_extensions: (
            Callable[..., Awaitable[dict]] | None) = None
        # Extension-backend-only: scope Target.getTargets to a session's tab
        # group so sessions sharing the one Chrome are mutually invisible.
        # listener wires this to ExtensionUpstream.scoped_target_infos.
        # Signature: (session_id) -> list[targetInfo dict]; scopes by the
        # session's bound groupId.
        self._scoped_targets: (
            Callable[[str | None], Awaitable[list[dict]]] | None) = None
        # Phase 3 (docs/refactor-single-daemon.md): rdp raw-CDP command channel.
        # Set by listener._open_chrome_upstream to the UpstreamConnection's
        # daemon-internal `send_command` when this is an rdp (or env)
        # context. The unified session verbs (openBackgroundTab / closeTab /
        # userscript) dispatch to a CDP implementation through this when the
        # context's backend is rdp, instead of the extension callbacks (which
        # stay None on an rdp context). Signature mirrors
        # UpstreamConnection.send_command: (method, params?, session_id?) -> result.
        self._upstream_command: (
            Callable[..., Awaitable[dict]] | None) = None
        # Background tasks fired off when a client frame triggers lazy
        # upstream open. We keep references so they don't get GC'd mid-await
        # (asyncio warning), and so we can cancel them on shutdown.
        self._open_tasks: set[asyncio.Task] = set()

    # ---- listener wiring -------------------------------------------------

    def register_client(self, client_id: int,
                        send_fn: Callable[[str], Awaitable[None]]) -> None:
        self._client_sends[client_id] = send_fn

    def unregister_client(self, client_id: int) -> None:
        self._client_sends.pop(client_id, None)

    async def release_client(self, client_id: int) -> ClientState | None:
        """Release a downstream client and close its primary upstream sessions.

        ``DaemonState.release_client`` only mutates bookkeeping. The router owns
        the wire side effects, so a client websocket disappearing still sends
        real ``Target.detachFromTarget`` frames for sessions where that client
        was the primary owner. Read-only secondary sessions are local views and
        need no upstream detach.
        """
        client = self.state.clients.get(client_id)
        if client is None:
            return None
        for binding in list(client.sessions.values()):
            if binding.readonly:
                continue
            await self._detach_upstream_best_effort(binding.upstream_session_id)
        return self.state.release_client(client_id)

    async def _detach_upstream_best_effort(self, upstream_session_id: str) -> None:
        """Send an upstream detach without expecting a client response."""
        if self._upstream_send is None:
            return
        upstream_id = self.state.allocate_upstream_id()
        msg = {
            "id": upstream_id,
            "method": "Target.detachFromTarget",
            "params": {"sessionId": upstream_session_id},
        }
        try:
            await self._upstream_send(json.dumps(msg))
        except Exception as e:  # noqa: BLE001 - disconnect cleanup is best-effort.
            logger.warning("best-effort upstream detach failed: %r", e)

    def update_upstream_send(self, fn: Callable[[str], Awaitable[None]] | None) -> None:
        self._upstream_send = fn

    def bind_lifecycle(
        self,
        ensure_upstream: Callable[[], Awaitable[None]],
        trigger_disconnect: Callable[[str], Awaitable[None]],
    ) -> None:
        self._ensure_upstream = ensure_upstream
        self._trigger_disconnect = trigger_disconnect

    # ---- downstream → upstream ------------------------------------------

    async def route_from_client(self, client: ClientState, text: str) -> None:
        msg = _json_safe(text)
        if msg is None:
            # Garbage frame — best-effort forward, upstream will error if it
            # cares. We still gate on upstream readiness so the frame doesn't
            # vanish during the lazy-open window.
            if client.session_id is None:
                await self._send_to_client(client.client_id, _error_response(
                    None, -32602,
                    "browser CDP forwarding requires websocket ?session=<id>"))
                return
            if not await self._gate_upstream_ready(client, text, msg=None):
                return
            await self._forward_raw(text)
            return
        client.last_command_at = time.time()
        self.state.last_activity_at = time.time()

        method = msg.get("method")
        req_id = msg.get("id") if isinstance(msg.get("id"), int) else None
        params = msg.get("params") or {}
        local_sid = msg.get("sessionId") if isinstance(msg.get("sessionId"), str) else None

        # --- BrowserwrightDaemon.* namespace ---
        # Self-answered: doesn't need upstream, so no gate.
        if isinstance(method, str) and method.startswith("BrowserwrightDaemon."):
            await self._handle_browserdaemon(client, msg)
            return

        if client.session_id is None:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602,
                "browser CDP forwarding requires websocket ?session=<id>"))
            return

        # --- pre-open gate (Task #76) ---
        # Everything below this point sends to upstream. If upstream isn't
        # OPEN yet, buffer the raw frame and replay once it is — silently
        # dropping (the v0.3 bug) caused 30s CDP timeouts on the client side
        # when two clients raced lazy-open.
        if not await self._gate_upstream_ready(client, text, msg=msg):
            return

        # --- Target.getTargets scoping (extension: this session's group only) ---
        # The skill's list_tabs / current_page enumerate via Target.getTargets.
        # On the shared extension upstream the raw handler returns EVERY ghost
        # across all sessions; scope it to the requesting client's tab group so
        # sessions stay mutually invisible. rdp keeps the normal forward (its
        # Chrome is already private to the session).
        if (method == "Target.getTargets"
                and self.state.backend_name == "extension"
                and self._scoped_targets is not None
                and client.session_id):
            try:
                infos = await self._scoped_targets(client.session_id)
            except Exception as e:  # noqa: BLE001
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32603, f"getTargets scoping failed: {e!r}"))
                return
            await self._send_to_client(client.client_id, _result_response(
                req_id, {"targetInfos": infos}))
            return

        # --- Target.attachToTarget interceptor ---
        # Server-side single-attacher decision is made BEFORE forwarding.
        if method == "Target.attachToTarget":
            await self._handle_attach(client, msg, req_id, params)
            return

        # --- Target.detachFromTarget interceptor ---
        # We unbind locally and forward an upstream detach when this client
        # is the primary owner; readers just disappear locally.
        if method == "Target.detachFromTarget":
            await self._handle_detach(client, msg, req_id, params)
            return

        # --- Target.activateTarget side-effect (update last-activated table) ---
        if method == "Target.activateTarget":
            tid = params.get("targetId")
            if isinstance(tid, str):
                self.state.note_activate(tid)
                await self._maybe_push_focus(reason="activated", target_id=tid)
            # falls through to forward

        # --- sessionId translation for session-scoped commands ---
        upstream_sid: str | None = None
        if local_sid is not None:
            binding = client.sessions.get(local_sid)
            if binding is None:
                # Client invented a sessionId we don't know — refuse.
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32602, f"unknown sessionId {local_sid}"))
                return
            if binding.readonly:
                # Shared-read sessions can only receive events; commands are
                # daemon-side -32602.
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32602,
                    "session is read-only (allowSecondaryReadOnly); "
                    "another client is the primary attacher"))
                return
            upstream_sid = binding.upstream_session_id

        # --- forward to upstream with id + sessionId translation ---
        await self._forward_translated(
            client, msg, req_id=req_id, method=method or "",
            upstream_sid=upstream_sid)

    # ---- pre-open buffer (Task #76 race fix) ----------------------------

    async def _gate_upstream_ready(
        self, client: ClientState, text: str, *, msg: dict | None,
    ) -> bool:
        """Return True if upstream is OPEN and the caller may proceed to send.
        Return False if the frame was buffered (for replay on OPEN) or
        rejected (overflow → -32603 sent to client).

        We treat upstream as "ready" only when the daemon has a live
        `_upstream_send` callable AND DaemonState.upstream_phase is CONNECTED.
        Any other phase (DISCONNECTED / CONNECTING / CLOSING) → buffer.
        """
        if (self._upstream_send is not None
                and self.state.upstream_phase == UpstreamPhase.CONNECTED):
            return True

        # Trigger lazy upstream open. ensure_upstream() is idempotent + locked,
        # so concurrent callers all converge on the single connect attempt.
        # Fire-and-forget: we return the buffered ack to the client without
        # awaiting the open, so a slow Chrome handshake doesn't backpressure
        # the client read loop.
        if (self._ensure_upstream is not None
                and self.state.upstream_phase == UpstreamPhase.DISCONNECTED):
            self._spawn_ensure_open()

        # Overflow path: cap the buffer at PRE_OPEN_BUFFER_LIMIT. The 101st
        # frame gets a CDP error -32603. Older frames are preserved (FIFO).
        if len(client.pre_open_buffer) >= PRE_OPEN_BUFFER_LIMIT:
            req_id = None
            if isinstance(msg, dict) and isinstance(msg.get("id"), int):
                req_id = msg["id"]
            metrics().proxy_pre_open_overflow_total += 1
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603,
                f"upstream pre-open buffer overflow "
                f"({PRE_OPEN_BUFFER_LIMIT} frames pending)"))
            return False

        client.pre_open_buffer.append(text)
        metrics().proxy_pre_open_buffered_total += 1
        return False

    def _spawn_ensure_open(self) -> None:
        """Fire-and-forget the upstream lazy-open. Tracks the task so it's not
        GC'd mid-await. ensure_upstream() is idempotent — overlapping calls
        coalesce into one connect attempt via its internal lock.
        """
        if self._ensure_upstream is None:
            return
        coro = self._ensure_upstream()
        task = asyncio.create_task(coro)
        self._open_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._open_tasks.discard(t)
            exc = t.exception() if not t.cancelled() else None
            if exc is not None:
                logger.warning("lazy upstream open failed: %r", exc)
                # Open failed — surface to every client whose buffered frames
                # would otherwise sit forever. We schedule the drain because
                # _done is a sync callback.
                asyncio.create_task(self._fail_pre_open_buffers(str(exc)))
        task.add_done_callback(_done)

    async def drain_pre_open_buffers(self) -> None:
        """Called once upstream transitions to CONNECTED. For each client,
        re-process every buffered frame in FIFO order. The frames go through
        the normal route_from_client path — which now finds upstream OPEN
        and forwards them downstream without buffering.

        v0.3 race fix (Task #76): see proxy.py module docstring + state.py
        PRE_OPEN_BUFFER_LIMIT for context.
        """
        for client in list(self.state.clients.values()):
            while client.pre_open_buffer:
                text = client.pre_open_buffer.popleft()
                metrics().proxy_pre_open_drained_total += 1
                try:
                    await self.route_from_client(client, text)
                except Exception as e:
                    logger.warning(
                        "drain frame for client %d failed: %r",
                        client.client_id, e)

    async def _fail_pre_open_buffers(self, reason: str) -> None:
        """Best-effort: clear every buffered frame and surface a CDP error
        to its client. Used when the lazy upstream open task fails — the
        client would otherwise hang on the buffered request waiting for a
        reply that never comes.
        """
        for client in list(self.state.clients.values()):
            while client.pre_open_buffer:
                text = client.pre_open_buffer.popleft()
                msg = _json_safe(text)
                req_id = None
                if isinstance(msg, dict) and isinstance(msg.get("id"), int):
                    req_id = msg["id"]
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32603,
                    f"upstream open failed before frame could be sent: {reason}"))

    # ---- attach / detach handlers ---------------------------------------

    async def _handle_attach(
        self, client: ClientState, msg: dict, req_id: int | None,
        params: dict,
    ) -> None:
        """Intercept Target.attachToTarget per spec §3.4 H7."""
        target_id = params.get("targetId")
        if not isinstance(target_id, str):
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602, "Target.attachToTarget requires params.targetId"))
            return
        flags = params.get("flags") if isinstance(params.get("flags"), dict) else {}
        # Read both the v0.3 spec-listed flag AND CDP's standard `flatten`
        # — we don't change `flatten` semantics, just remember the shared-read
        # preference.
        allow_shared_read = bool(flags.get("allowSecondaryReadOnly", False))

        existing = self.state.attachers.get(target_id)
        if existing is None:
            # No prior owner — forward upstream + intercept response.
            await self._forward_translated(
                client, msg, req_id=req_id, method="Target.attachToTarget",
                upstream_sid=None,
                attach_target_id=target_id,
                attach_allow_shared_read=allow_shared_read,
            )
            return

        # Someone already owns this target.
        if existing.primary_client_id == client.client_id:
            # Same client re-attaching — re-issue the existing local session
            # without going to upstream (Chrome would return the same upstream
            # session anyway; this saves a roundtrip and avoids confusing the
            # primary's session table).
            await self._send_to_client(client.client_id, _result_response(
                req_id, {"sessionId": existing.primary_local_session}))
            return

        if not allow_shared_read:
            # Spec §3.4 H7: -32602 "target already owned by another client".
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602,
                f"target {target_id} already attached by another client; "
                f"set params.flags.allowSecondaryReadOnly=true for read-only access"))
            return

        # Shared-read path: allocate a local sessionId for this client that
        # maps to the existing upstream session, flagged readonly. No upstream
        # roundtrip — we synthesize the response.
        local_sid = _new_local_session_id(client.client_id)
        self.state.bind_session(
            client.client_id, local_sid, existing.upstream_session_id,
            target_id, readonly=True,
        )
        self.state.add_reader(target_id, client.client_id, local_sid)
        await self._send_to_client(client.client_id, _result_response(
            req_id, {"sessionId": local_sid}))

    async def _handle_detach(
        self, client: ClientState, msg: dict, req_id: int | None,
        params: dict,
    ) -> None:
        local_sid = params.get("sessionId")
        if not isinstance(local_sid, str):
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602, "Target.detachFromTarget requires params.sessionId"))
            return
        binding = client.sessions.get(local_sid)
        if binding is None:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602, f"unknown sessionId {local_sid}"))
            return

        if binding.readonly:
            # Reader detaches locally only — upstream session stays alive
            # because the primary owner still owns it.
            self.state.unbind_session_by_local(client.client_id, local_sid)
            await self._send_to_client(client.client_id, _result_response(
                req_id, {}))
            return

        # Primary owner: forward upstream so the session truly closes.
        # We hand-build the upstream message because the sessionId lives in
        # params (unlike most session-scoped commands where it's top-level).
        upstream_id = self.state.allocate_upstream_id()
        self.state.remember_request(
            upstream_id, client.client_id,
            req_id if req_id is not None else 0,
            method="Target.detachFromTarget",
        )
        upstream_msg = {
            "id": upstream_id,
            "method": "Target.detachFromTarget",
            "params": {"sessionId": binding.upstream_session_id},
        }
        # Unbind locally NOW so subsequent local commands on this session
        # fail fast instead of racing with the upstream detach response.
        self.state.unbind_session_by_local(client.client_id, local_sid)
        await self._forward_raw(json.dumps(upstream_msg))

    # ---- generic translated forward -------------------------------------

    async def _forward_translated(
        self,
        client: ClientState,
        original: dict,
        *,
        req_id: int | None,
        method: str,
        upstream_sid: str | None,
        attach_target_id: str | None = None,
        attach_allow_shared_read: bool = False,
    ) -> None:
        """Rewrite id (always) and sessionId (when present) on a copy of the
        message, remember the pending request, and send upstream."""
        upstream_id = self.state.allocate_upstream_id()
        self.state.remember_request(
            upstream_id,
            client.client_id,
            req_id if req_id is not None else 0,
            method=method,
            attach_target_id=attach_target_id,
            attach_allow_shared_read=attach_allow_shared_read,
        )
        # Build a fresh dict — never mutate the client's message in place.
        out: dict = {"id": upstream_id, "method": method}
        if "params" in original:
            out["params"] = original["params"]
        if upstream_sid is not None:
            out["sessionId"] = upstream_sid
        await self._forward_raw(json.dumps(out))

    async def _forward_raw(self, text: str) -> None:
        """Push to upstream verbatim.

        Callers MUST have passed `_gate_upstream_ready()` first (Task #76):
        the gate buffers frames while upstream is opening, so by the time we
        reach this point the upstream conn is live (or being torn down — in
        which case a dropped frame is acceptable, the client will see
        `upstreamClosed` shortly).
        """
        if self._upstream_send is None:
            # Defensive: this is only reachable if upstream torn down mid-call.
            logger.warning("dropped frame (no upstream): %s", text[:80])
            return
        await self._upstream_send(text)

    # ---- upstream → downstream -------------------------------------------

    async def forward_from_upstream(self, text: str) -> None:
        """Route an upstream frame to the right client(s)."""
        msg = _json_safe(text)
        if msg is None:
            # Malformed — broadcast as-is so any single curious client gets it.
            await self._broadcast(text)
            return

        # Response (id present, no method) — route by pending request map.
        if "id" in msg and "method" not in msg:
            await self._handle_upstream_response(msg)
            return

        # Event (method present, may or may not have sessionId).
        await self._handle_upstream_event(msg, text)

    async def _handle_upstream_response(self, msg: dict) -> None:
        upstream_id = msg.get("id")
        if not isinstance(upstream_id, int):
            return
        pending = self.state.take_pending(upstream_id)
        if pending is None:
            # Either a daemon-internal id (heartbeat — handled inside
            # UpstreamConnection before reaching us) or a stale id. Drop.
            return

        # Restore the client's original request id on the response.
        out = {**msg, "id": pending.client_request_id}
        # If a response happened to carry a sessionId, translate it back to
        # the local one (CDP standard responses don't, but Target.attach
        # does in its result).
        upstream_sid_in_result: str | None = None
        result = out.get("result") if isinstance(out.get("result"), dict) else None
        if result is not None and isinstance(result.get("sessionId"), str):
            upstream_sid_in_result = result["sessionId"]

        # --- Target.attachToTarget completion: bind sessions + attacher ---
        if (pending.method == "Target.attachToTarget"
                and pending.attach_target_id is not None
                and isinstance(upstream_sid_in_result, str)):
            target_id = pending.attach_target_id
            existing = self.state.attachers.get(target_id)
            if existing is None:
                # First attach — primary owner.
                local_sid = _new_local_session_id(pending.client_id)
                self.state.bind_session(
                    pending.client_id, local_sid, upstream_sid_in_result,
                    target_id, readonly=False,
                )
                self.state.claim_attacher(
                    target_id, pending.client_id, local_sid,
                    upstream_sid_in_result)
                # Rewrite the response's sessionId for the client.
                out["result"] = {**result, "sessionId": local_sid}  # type: ignore[index]
            else:
                # Edge: another client became primary between our attach and
                # response arriving. Treat as same-client re-attach if we are
                # primary, else flip to reader if allowed, else surface error.
                if existing.primary_client_id == pending.client_id:
                    out["result"] = {**result,
                                     "sessionId": existing.primary_local_session}  # type: ignore[index]
                elif pending.attach_allow_shared_read:
                    local_sid = _new_local_session_id(pending.client_id)
                    self.state.bind_session(
                        pending.client_id, local_sid,
                        existing.upstream_session_id, target_id, readonly=True)
                    self.state.add_reader(target_id, pending.client_id, local_sid)
                    out["result"] = {**result, "sessionId": local_sid}  # type: ignore[index]
                else:
                    # Race-loss: convert to error.
                    out = {
                        "id": pending.client_request_id,
                        "error": {"code": -32602, "message":
                                  "target already owned by another client (race)"},
                    }

        await self._send_to_client(pending.client_id, json.dumps(out))

    async def _handle_upstream_event(self, msg: dict, text: str) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        upstream_sid = msg.get("sessionId") if isinstance(msg.get("sessionId"), str) else None

        # Pre-route observations: update the target table and the focus push
        # decision uses the latest state.
        if method == "Target.targetCreated":
            info = params.get("targetInfo")
            if isinstance(info, dict):
                self.state.note_target_info(info)
        elif method == "Target.targetInfoChanged":
            info = params.get("targetInfo")
            if isinstance(info, dict):
                self.state.note_target_info(info)
                tid = info.get("targetId")
                if isinstance(tid, str) and info.get("type") == "page":
                    await self._maybe_push_focus(reason="navigated", target_id=tid)
        elif method == "Target.targetDestroyed":
            tid = params.get("targetId")
            if isinstance(tid, str):
                self.state.note_target_destroyed(tid)
                await self._maybe_push_focus(reason="closed", target_id=tid)
        elif method == "Target.attachedToTarget":
            # Update target table for getActiveTab observability.
            info = params.get("targetInfo")
            sid = params.get("sessionId")
            if isinstance(info, dict):
                self.state.note_target_info(info)
            # SessionId binding is handled by the explicit-attach response
            # path (_handle_upstream_response). We used to also bind here as
            # a fallback (auto-attach with active-client heuristic), but the
            # heuristic races with concurrent explicit attaches from multiple
            # clients — wrong client ends up owning the target. Cleaner: let
            # the response handler do all binding. If `Target.setAutoAttach`
            # is later supported as a session-scoped feature, sub-target
            # binding will go through that session's flatten flow, not here.
            if isinstance(sid, str) and isinstance(info, dict):
                tid = info.get("targetId") if isinstance(info.get("targetId"), str) else None
                # If there's a pending explicit attach for this target, drop
                # this event — the response handler owns the binding and will
                # surface attach completion to the requesting client.
                if tid is not None and any(
                    pr.method == "Target.attachToTarget"
                    and pr.attach_target_id == tid
                    for pr in self.state.pending_requests.values()
                ):
                    return  # drop; response handler will tell the client
                # No pending — this is an unsolicited auto-attach (e.g.
                # client previously enabled setAutoAttach). Fall through to
                # default routing: with no binding for `sid`, the
                # `upstream_to_locals` lookup at the bottom of this method
                # will find nothing and the event will be dropped silently.
                # That's acceptable for v0.3 — supporting full setAutoAttach
                # flattening over multi-client mux is v0.4 territory.
        elif method == "Target.detachedFromTarget":
            sid = params.get("sessionId")
            if isinstance(sid, str):
                # Drop the session from every client that had a binding for
                # this upstream sessionId.
                bindings = list(self.state.upstream_to_locals.get(sid, []))
                for binding in bindings:
                    self.state.unbind_session_by_local(
                        binding.client_id, binding.local_session_id)
                    # Rewrite the event per-client so they see THEIR sessionId.
                    rewritten = {
                        "method": method,
                        "params": {**params, "sessionId": binding.local_session_id},
                    }
                    await self._send_to_client(binding.client_id,
                                               json.dumps(rewritten))
                return
        elif method == "Inspector.detached":
            if self._trigger_disconnect is not None:
                await self._trigger_disconnect("chrome_exit")

        # --- routing decision ---
        if upstream_sid is not None:
            # Session-scoped event: route to all bindings of this upstream session
            # (primary + any shared-read readers). Each gets the event with their
            # local sessionId substituted.
            bindings = list(self.state.upstream_to_locals.get(upstream_sid, []))
            if not bindings:
                # Orphan event (session not bound to any client). Drop.
                return
            for binding in bindings:
                rewritten = {
                    "method": method,
                    "params": params,
                    "sessionId": binding.local_session_id,
                }
                await self._send_to_client(binding.client_id,
                                           json.dumps(rewritten))
            return

        # Browser-level event (no sessionId) → broadcast.
        await self._broadcast(text)

    def _pick_active_client(self) -> ClientState | None:
        if not self.state.clients:
            return None
        # Most-recent last_command_at wins.
        return max(self.state.clients.values(), key=lambda c: c.last_command_at)

    # ---- send primitives ------------------------------------------------

    async def _send_to_client(self, client_id: int, text: str) -> None:
        fn = self._client_sends.get(client_id)
        if fn is None:
            return
        try:
            await fn(text)
        except Exception as e:
            logger.warning("send to client %d failed: %r", client_id, e)

    async def _broadcast(self, text: str) -> None:
        for cid in list(self._client_sends.keys()):
            await self._send_to_client(cid, text)

    # ---- focus push -----------------------------------------------------

    async def _maybe_push_focus(self, *, reason: str, target_id: str) -> None:
        meta = self.state.targets.get(target_id) or {}
        params = {
            "targetId": target_id,
            "url": meta.get("url", ""),
            "title": meta.get("title", ""),
            "accuracy": "heuristic-recent-activate",
            "reason": reason,
        }
        for client in self.state.clients.values():
            if not client.subscribed_focus or not client.session_id:
                continue
            if (self.state.backend_name == "extension"
                    and self._scoped_targets is not None):
                try:
                    scoped = await self._scoped_targets(client.session_id)
                except Exception:
                    continue
                scoped_ids = {
                    info.get("targetId") for info in scoped
                    if isinstance(info, dict)
                }
                if target_id not in scoped_ids:
                    continue
            await self._send_to_client(
                client.client_id,
                _event("BrowserwrightDaemon.activeTabChanged", params))
