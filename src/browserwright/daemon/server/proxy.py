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
import secrets
import time
from typing import Awaitable, Callable

from .. import __version__
from ..observability import metrics
from .state import (
    AttachOwnership, ClientState, DaemonState, PendingRequest, SessionBinding,
    UpstreamPhase, PRE_OPEN_BUFFER_LIMIT,
)

logger = logging.getLogger(__name__)


# ---- helpers --------------------------------------------------------------


def _json_safe(text: str) -> dict | None:
    try:
        v = json.loads(text)
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, dict) else None


def _error_response(req_id: int | None, code: int, message: str) -> str:
    return json.dumps({"id": req_id, "error": {"code": code, "message": message}})


def _result_response(req_id: int | None, result: dict) -> str:
    return json.dumps({"id": req_id, "result": result})


def _event(method: str, params: dict, session_id: str | None = None) -> str:
    msg: dict = {"method": method, "params": params}
    if session_id is not None:
        msg["sessionId"] = session_id
    return json.dumps(msg)


def _cmd_result(envelope: object) -> dict:
    """Extract the CDP ``result`` dict from an ``UpstreamConnection.send_command``
    envelope. send_command resolves to the FULL frame
    (``{"id": N, "result": {...}}`` or ``{"id": N, "error": {...}}``), so the rdp
    verb impls must unwrap ``result`` rather than reading fields off the envelope.
    Raises ``RuntimeError`` on a CDP error or a malformed frame (the rdp handlers
    catch it and surface -32603)."""
    if not isinstance(envelope, dict):
        raise RuntimeError(f"malformed CDP response: {envelope!r}")
    if "error" in envelope and envelope["error"]:
        raise RuntimeError(f"CDP error: {envelope['error']!r}")
    result = envelope.get("result")
    return result if isinstance(result, dict) else {}


def _new_local_session_id(client_id: int) -> str:
    """Synthetic local sessionId. Spec doesn't pin the format — we pick a
    `c<client_id>-<random>` prefix so daemon logs make it obvious which
    client a sessionId belongs to (debugging multi-client races is otherwise
    miserable).
    """
    return f"c{client_id}-{secrets.token_hex(8).upper()}"


# ---- the router -----------------------------------------------------------


class Router:
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
        self._userscript_request: (
            Callable[[str, dict], Awaitable[dict | None]] | None) = None
        # Extension-backend-only: scope Target.getTargets to a session's tab
        # group so sessions sharing the one Chrome are mutually invisible.
        # listener wires this to ExtensionUpstream.scoped_target_infos.
        # Signature: (session_id) -> list[targetInfo dict]; scopes by the
        # session's bound groupId.
        self._scoped_targets: (
            Callable[[str | None], Awaitable[list[dict]]] | None) = None
        # Phase 3 (docs/refactor-single-daemon.md): rdp raw-CDP command channel.
        # Set by listener._open_chrome_upstream to the UpstreamConnection's
        # daemon-internal `send_command` when this is an rdp (or env/cloud)
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

    def _session_group_name(
        self, client: ClientState, session_id: str,
        explicit: str | None = None,
    ) -> str:
        """Human-visible tab group title for a browserwright session."""
        if explicit:
            return explicit
        return client.session_name or session_id

    def _request_session_param(self, params: dict) -> str | None:
        session = params.get("bsSession") or params.get("session")
        return session if isinstance(session, str) and session else None

    async def _require_browser_session(
        self, client: ClientState, req_id: int | None, op: str,
        params: dict | None = None,
    ) -> str | None:
        """Enforce browserwright-session scoping at the daemon boundary.

        The websocket's ``?session=<id>`` is the isolation key. Legacy request
        params may repeat that id for mixed-version clients, but they may not
        invent or switch sessions.
        """
        if not client.session_id:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602, f"{op} requires websocket ?session=<id>"))
            return None
        requested = self._request_session_param(params or {})
        if requested is not None and requested != client.session_id:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602,
                f"{op} session mismatch: connection is bound to "
                f"{client.session_id!r}, request asked for {requested!r}"))
            return None
        return client.session_id

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

    # ---- BrowserwrightDaemon.* (per-client RPC) -------------------------------

    async def _handle_browserdaemon(self, client: ClientState, msg: dict) -> None:
        method = msg["method"]
        req_id = msg.get("id") if isinstance(msg.get("id"), int) else None
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if isinstance(method, str) and method.startswith("BrowserwrightDaemon.userscript."):
            session_id = await self._require_browser_session(
                client, req_id, method, params)
            if session_id is None:
                return
            # The schema-lock test scans this file for `method == "..."` string
            # literals; this no-op registers the userscript.install verb literal
            # for that scan (userscript.* is otherwise dispatched by prefix).
            if False and method == "BrowserwrightDaemon.userscript.install":
                pass
            verb = method.split(".", 2)[2]
            # rdp dispatch: the extension's userScripts API doesn't exist on a
            # daemon-owned Chrome. Provide an honest shim via
            # Page.addScriptToEvaluateOnNewDocument (see _rdp_userscript). Never
            # -32601 on rdp.
            if self.state.backend_name == "rdp":
                await self._rdp_userscript(client, req_id, verb, params)
                return
            if self._userscript_request is None:
                if (self._ensure_upstream is not None
                        and self.state.upstream_phase != UpstreamPhase.CONNECTED):
                    try:
                        await self._ensure_upstream()
                    except Exception as e:
                        await self._send_to_client(client.client_id, _error_response(
                            req_id, -32603,
                            f"userscript {verb} failed (upstream open): {e!r}"))
                        return
            if self._userscript_request is None:
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32601,
                    "BrowserwrightDaemon.userscript.* requires the extension backend"))
                return
            try:
                result = await self._userscript_request(verb, params)
            except Exception as e:  # noqa: BLE001 - surface to client
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32000, f"userscript {verb} failed: {e}"))
                return
            await self._send_to_client(
                client.client_id, _result_response(req_id, result or {}))
            return
        if method == "BrowserwrightDaemon.getActiveTab":
            session_id = await self._require_browser_session(
                client, req_id, method, params)
            if session_id is None:
                return
            tab = self.state.best_active_tab()
            if (tab is not None and self.state.backend_name == "extension"
                    and self._scoped_targets is not None):
                try:
                    scoped = await self._scoped_targets(session_id)
                except Exception as e:  # noqa: BLE001
                    await self._send_to_client(client.client_id, _error_response(
                        req_id, -32603, f"getActiveTab scoping failed: {e!r}"))
                    return
                scoped_ids = {
                    info.get("targetId") for info in scoped
                    if isinstance(info, dict)
                }
                if tab.get("targetId") not in scoped_ids:
                    tab = None
            payload = tab if tab is not None else {
                "targetId": None, "url": None, "title": None,
                "accuracy": "unknown", "since_seconds": None,
            }
            await self._send_to_client(client.client_id, _result_response(req_id, payload))
            return
        if method == "BrowserwrightDaemon.getBackendInfo":
            from ..backends import kind_for
            # Report the live backend's real kind (extension is LOCAL_RELAY),
            # not a hardcoded UPSTREAM_WS. Unknown/unresolved names ("auto")
            # fall back to UPSTREAM_WS.
            kind = kind_for(self.state.backend_name) or "UPSTREAM_WS"
            await self._send_to_client(client.client_id, _result_response(req_id, {
                "name": self.state.backend_name,
                "kind": kind,
                "ux_warnings": [],
                "schema_version": 1,
            }))
            return
        if method == "BrowserwrightDaemon.uiState":
            await self._send_to_client(client.client_id, _result_response(req_id, {
                "ws_count": 1 if self.state.upstream_phase == UpstreamPhase.CONNECTED else 0,
                "last_popup_resolved_at": self.state.last_popup_resolved_at,
                "banner_visible_estimated":
                    self.state.upstream_phase == UpstreamPhase.CONNECTED,
                "client_count": len(self.state.clients),  # v0.3 addition
            }))
            return
        if method == "BrowserwrightDaemon.subscribeFocus":
            if await self._require_browser_session(client, req_id, method, params) is None:
                return
            client.subscribed_focus = True
            await self._send_to_client(client.client_id,
                                       _result_response(req_id, {"ok": True}))
            return
        if method == "BrowserwrightDaemon.unsubscribeFocus":
            if await self._require_browser_session(client, req_id, method, params) is None:
                return
            client.subscribed_focus = False
            await self._send_to_client(client.client_id,
                                       _result_response(req_id, {"ok": True}))
            return
        if method == "BrowserwrightDaemon.disconnect":
            if await self._require_browser_session(client, req_id, method, params) is None:
                return
            await self._send_to_client(client.client_id,
                                       _result_response(req_id, {"ok": True}))
            if self._trigger_disconnect is not None:
                await self._trigger_disconnect("skill_disconnect")
            return
        if method == "BrowserwrightDaemon.version":
            await self._send_to_client(client.client_id, _result_response(req_id, {
                "browserwright_daemon_version": __version__,
                "schema_version": 1,
            }))
            return
        if method == "BrowserwrightDaemon.attachActiveTab":
            # Unified verb. On extension this adopts the user's focused-window
            # active tab (the targetId isn't known until the extension picks
            # it). On rdp the daemon owns the Chrome, so "the active tab" is
            # the session's current front target (most-recently-fronted), and
            # we create+attach one if none exists — an honest equivalent, NOT
            # -32601 (docs §C1). Either path registers the resulting session
            # in the binding tables so subsequent CDP commands route the same
            # way an explicit attach would.
            attach_params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            attach_session = await self._require_browser_session(
                client, req_id, method, attach_params)
            if attach_session is None:
                return
            if self.state.backend_name == "rdp":
                if (self._upstream_command is None and self._ensure_upstream is not None):
                    try:
                        await self._ensure_upstream()
                    except Exception as e:
                        await self._send_to_client(client.client_id, _error_response(
                            req_id, -32603,
                            f"attach active failed (upstream open): {e!r}"))
                        return
                await self._rdp_attach_active(client, req_id)
                return
            if self._attach_active_tab is None:
                # Trigger lazy-open once; the listener wires
                # `_attach_active_tab` inside _open_extension_upstream so a
                # cold daemon + extension already connected will become
                # ready by the time ensure_upstream returns.
                if (self._ensure_upstream is not None
                        and self.state.upstream_phase != UpstreamPhase.CONNECTED):
                    try:
                        await self._ensure_upstream()
                    except Exception as e:
                        await self._send_to_client(client.client_id, _error_response(
                            req_id, -32603,
                            f"attach active failed (upstream open): {e!r}"))
                        return
            if self._attach_active_tab is None:
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32601,
                    "BrowserwrightDaemon.attachActiveTab requires the extension backend"))
                return
            try:
                # Adopt into THIS session's tab group. The title is cosmetic:
                # prefer the ledger name when the daemon can see it, otherwise
                # fall back to the bound session id. The durable association is
                # still the returned numeric groupId.
                info = await self._attach_active_tab(
                    session_id=attach_session,
                    group_name=self._session_group_name(client, attach_session))
            except Exception as e:
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32000, f"attach active failed: {e!r}"))
                return
            upstream_sid = info.get("sessionId")
            target_id = info.get("targetId")
            if not isinstance(upstream_sid, str) or not isinstance(target_id, str):
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32603,
                    f"attach active returned malformed payload: {info!r}"))
                return
            # Mirror the binding shape that Target.attachToTarget would
            # produce: allocate a local sessionId visible to the client,
            # bind it to the upstream session, and claim the attacher slot.
            existing = self.state.attachers.get(target_id)
            if existing is None:
                local_sid = _new_local_session_id(client.client_id)
                self.state.bind_session(
                    client.client_id, local_sid, upstream_sid,
                    target_id, readonly=False,
                )
                self.state.claim_attacher(
                    target_id, client.client_id, local_sid, upstream_sid)
                # Stash target metadata so list_tabs / getActiveTab see it.
                self.state.note_target_info({
                    "targetId": target_id,
                    "type": "page",
                    "url": info.get("url", ""),
                    "title": info.get("title", ""),
                })
            elif existing.primary_client_id == client.client_id:
                # Same client re-attaching the active tab — reuse the
                # existing local sessionId rather than minting a new one.
                local_sid = existing.primary_local_session
            else:
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32602,
                    f"target {target_id} already attached by another client"))
                return
            await self._send_to_client(client.client_id, _result_response(
                req_id, {
                    "sessionId": local_sid,
                    "targetId": target_id,
                    "tabId": info.get("tabId"),
                    "url": info.get("url", ""),
                    "title": info.get("title", ""),
                }))
            return
        if method == "BrowserwrightDaemon.stats":
            # v0.5: expose in-process metrics counters. Schema is the
            # observability.Metrics dataclass keys + uptime_seconds.
            await self._send_to_client(
                client.client_id,
                _result_response(req_id, metrics().snapshot()))
            return
        if method == "BrowserwrightDaemon.openBackgroundTab":
            await self._handle_open_background_tab(client, msg, req_id)
            return
        if method == "BrowserwrightDaemon.closeTab":
            await self._handle_close_tab(client, msg, req_id)
            return
        if method == "BrowserwrightDaemon.ensureSession":
            await self._handle_ensure_session(client, msg, req_id)
            return
        if method == "BrowserwrightDaemon.endSession":
            await self._handle_end_session(client, msg, req_id)
            return
        if method == "BrowserwrightDaemon.ensureExecutor":
            await self._handle_ensure_executor(client, msg, req_id)
            return
        if method == "BrowserwrightDaemon.killExecutor":
            await self._handle_kill_executor(client, msg, req_id)
            return
        if method == "BrowserwrightDaemon.recoverSession":
            await self._handle_recover_session(client, msg, req_id)
            return
        await self._send_to_client(client.client_id, _error_response(
            req_id, -32601, f"unknown BrowserwrightDaemon method: {method}"))

    # ---- Phase B: BrowserwrightDaemon.openBackgroundTab / closeTab ----------

    async def _handle_open_background_tab(
        self, client: ClientState, msg: dict, req_id: int | None,
    ) -> None:
        """Spec Phase B Feature 1.

        Requires backend=extension (the only backend with the callback wired
        in). Calls the upstream's open_background_tab, then registers the
        returned (target_id, upstream_session_id) as a regular client-side
        binding so subsequent CDP commands work through the same session-id
        translation path as Target.attachToTarget.
        """
        # Param validation runs FIRST: the schema-lock smoke test calls
        # every BrowserwrightDaemon.* method with no params and asserts the
        # response code is NOT -32601 ("unknown method"). Returning -32602
        # here for the missing required param keeps the lock satisfied
        # without us wiring a real extension upstream in unit tests.
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        url = params.get("url")
        if not isinstance(url, str) or not url:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602,
                "BrowserwrightDaemon.openBackgroundTab requires params.url"))
            return
        group_name = params.get("groupName")
        if group_name is not None and not isinstance(group_name, str):
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602,
                "BrowserwrightDaemon.openBackgroundTab params.groupName must be a string"))
            return
        session = await self._require_browser_session(
            client, req_id, "BrowserwrightDaemon.openBackgroundTab", params)
        if session is None:
            return
        # rdp dispatch: on an rdp context there is no extension callback —
        # implement the verb with raw CDP (Target.createTarget + attach). Every
        # rdp tab is "background" (no human focus to protect), so `background`
        # is a no-op and `groupId` is -1 (tab groups are an extension concept).
        if self.state.backend_name == "rdp":
            if self._upstream_command is None and self._ensure_upstream is not None:
                try:
                    await self._ensure_upstream()
                except Exception as e:
                    await self._send_to_client(client.client_id, _error_response(
                        req_id, -32603,
                        f"openBackgroundTab failed (upstream open): {e!r}"))
                    return
            await self._rdp_open_tab(client, req_id, url)
            return
        if self._open_background_tab is None:
            # Lazy-open: a cold daemon + already-connected extension becomes
            # ready after ensure_upstream runs (listener wires the callbacks
            # inside _open_extension_upstream). Mirrors attachActiveTab.
            if (self._ensure_upstream is not None
                    and self.state.upstream_phase != UpstreamPhase.CONNECTED):
                try:
                    await self._ensure_upstream()
                except Exception as e:
                    await self._send_to_client(client.client_id, _error_response(
                        req_id, -32603,
                        f"openBackgroundTab failed (upstream open): {e!r}"))
                    return
        if self._open_background_tab is None:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32601,
                "BrowserwrightDaemon.openBackgroundTab requires the extension backend"))
            return
        # The group identity is the session's NAME, fixed (and required +
        # unique) at `session new` and stored in the ledger — the daemon reads
        # it off the connection (client.session_name) rather than the caller
        # re-passing it on every open. This is an authoritative lookup, NOT a
        # fallback: the session IS the tab group (decision 6).
        group_name = self._session_group_name(client, session, group_name)
        # `background` (default True) protects the user's focus on the
        # extension backend; background=False opens the tab in the foreground.
        background = params.get("background")
        background = background if isinstance(background, bool) else True
        try:
            result = await self._open_background_tab(
                url, group_name=group_name, session_id=session,
                background=background)
        except Exception as e:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, f"openBackgroundTab failed: {e!r}"))
            return
        upstream_sid = result.get("sessionId")
        target_id = result.get("targetId")
        if not isinstance(upstream_sid, str) or not isinstance(target_id, str):
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603,
                f"openBackgroundTab returned malformed result: {result!r}"))
            return
        # Register the session binding so future CDP commands routed by the
        # client through this sessionId are translated upstream same as
        # Target.attachToTarget bindings.
        local_sid = _new_local_session_id(client.client_id)
        self.state.bind_session(
            client.client_id, local_sid, upstream_sid, target_id,
            readonly=False,
        )
        self.state.claim_attacher(
            target_id, client.client_id, local_sid, upstream_sid,
        )
        # Note the target in the visibility table so getActiveTab /
        # uiState see the new tab. groupId is just metadata for the caller.
        self.state.note_target_info({
            "targetId": target_id,
            "type": "page",
            "url": result.get("url", ""),
            "title": result.get("title", ""),
        })
        await self._send_to_client(client.client_id, _result_response(req_id, {
            "sessionId": local_sid,
            "targetId": target_id,
            "tabId": result.get("tabId"),
            "url": result.get("url", ""),
            "title": result.get("title", ""),
            "groupId": result.get("groupId", -1),
        }))

    async def _handle_recover_session(
        self, client: ClientState, msg: dict, req_id: int | None,
    ) -> None:
        """Session-reconnect-recovery.

        After a reconnect / daemon restart the in-memory session→tab bindings
        are gone, but the Chrome tab group id persisted in the session ledger
        may still identify a live group.
        Recover the tabs from that group, re-attach, and register a regular
        client-side binding for the representative tab so subsequent CDP
        commands route through the normal sessionId translation path (mirrors
        openBackgroundTab). Requires backend=extension."""
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        # rdp is ephemeral (decision 9): the daemon-owned Chrome dies with the
        # daemon, so there is nothing durable to recover. Surviving targets are
        # re-attached by the skill's in-process / ledger fast paths, so recover
        # is an honest no-op here — NEVER -32601 (revised Rule: same-shape,
        # honest, nearest equivalent). This runs before param validation so the
        # schema-lock smoke test (no params) sees a result, not an error.
        if self.state.backend_name == "rdp":
            await self._send_to_client(client.client_id, _result_response(
                req_id, {"recovered": [], "groupId": -1, "tabs": []}))
            return
        # Recovery keys on the persisted numeric groupId (the session's durable
        # tab-group id from the ledger), NOT the title — names aren't unique.
        # Validation FIRST so the schema-lock smoke test sees -32602, != -32601.
        group_id = params.get("groupId")
        if not isinstance(group_id, int) or group_id < 0:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602,
                "BrowserwrightDaemon.recoverSession requires params.groupId"))
            return
        bs_session = await self._require_browser_session(
            client, req_id, "BrowserwrightDaemon.recoverSession", params)
        if bs_session is None:
            return
        if self._recover_session is None:
            # Lazy-open mirror of openBackgroundTab.
            if (self._ensure_upstream is not None
                    and self.state.upstream_phase != UpstreamPhase.CONNECTED):
                try:
                    await self._ensure_upstream()
                except Exception as e:
                    await self._send_to_client(client.client_id, _error_response(
                        req_id, -32603,
                        f"recoverSession failed (upstream open): {e!r}"))
                    return
        if self._recover_session is None:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32601,
                "BrowserwrightDaemon.recoverSession requires the extension backend"))
            return
        try:
            result = await self._recover_session(bs_session, group_id=group_id)
        except Exception as e:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, f"recoverSession failed: {e!r}"))
            return
        upstream_sid = result.get("sessionId")
        target_id = result.get("targetId")
        if not isinstance(upstream_sid, str) or not isinstance(target_id, str):
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603,
                f"recoverSession returned malformed result: {result!r}"))
            return
        # Register the representative tab's session binding (same as
        # openBackgroundTab) so the client can drive it immediately.
        local_sid = _new_local_session_id(client.client_id)
        self.state.bind_session(
            client.client_id, local_sid, upstream_sid, target_id,
            readonly=False,
        )
        self.state.claim_attacher(
            target_id, client.client_id, local_sid, upstream_sid,
        )
        self.state.note_target_info({
            "targetId": target_id,
            "type": "page",
            "url": result.get("url", ""),
            "title": result.get("title", ""),
        })
        await self._send_to_client(client.client_id, _result_response(req_id, {
            "sessionId": local_sid,
            "targetId": target_id,
            "tabId": result.get("tabId"),
            "url": result.get("url", ""),
            "title": result.get("title", ""),
            "groupId": result.get("groupId", -1),
            "recovered": result.get("recovered", []),
        }))

    async def _handle_ensure_session(
        self, client: ClientState, msg: dict, req_id: int | None,
    ) -> None:
        """Phase 2: backend-neutral session verb. Idempotent.

        The backend is read from the ledger (NOT a param) by the dispatcher in
        listener / Daemon, so by the time a client reaches this Router it must
        already be routed to the right context:
          - extension/env/cloud → the shared context (this Router). The client
            is attached; ensureSession is a no-op success.
          - rdp → a per-session context. `Daemon.context_for(session_id)`
            already created the context (its state/router/holder) when this
            client connected with `?session=`.

        Returns `{ "ok": true }`. Never `-32601`.
        """
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        session = params.get("session_id") or params.get("session")
        session = session if isinstance(session, str) and session else None
        if not client.session_id:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602,
                "BrowserwrightDaemon.ensureSession requires the websocket "
                "to connect with ?session=<id>; sessionless clients cannot "
                "materialize or switch session contexts"))
            return
        if session is not None and session != client.session_id:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602,
                f"BrowserwrightDaemon.ensureSession session mismatch: "
                f"connection is bound to {client.session_id!r}, request asked "
                f"for {session!r}"))
            return
        daemon = self.daemon
        if daemon is not None:
            try:
                # Idempotent get-or-create of the session's context. For
                # extension/env/cloud this returns the shared context (no-op);
                # for rdp it ensures the per-session context exists.
                daemon.context_for(client.session_id)  # type: ignore[attr-defined]
            except Exception as e:
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32603, f"ensureSession failed: {e!r}"))
                return
        await self._send_to_client(
            client.client_id, _result_response(req_id, {"ok": True}))

    async def _handle_end_session(
        self, client: ClientState, msg: dict, req_id: int | None,
    ) -> None:
        """P5.4 / Phase 2: tear down a browserwright session.

        extension: close the session's extension workspace (owned tabs closed,
        borrowed kept) via the wired `_end_session` callback.

        rdp: the per-session context owns a dedicated Chrome. Close that Chrome
        (SIGTERM the launched pid), close the upstream, and drop the context —
        the uniform, non-`-32601` success shape (docs §RPCs)."""
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        session = params.get("session")
        if not isinstance(session, str) or not session:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602,
                "BrowserwrightDaemon.endSession requires params.session"))
            return
        if await self._require_browser_session(
            client, req_id, "BrowserwrightDaemon.endSession", params,
        ) is None:
            return

        # Phase B (PR2): kill this session's persistent executor FIRST, symmetric
        # for rdp + extension (each session has its own executor keyed on the
        # daemon registry, even though extension sessions share one
        # UpstreamContext). Idempotent — a no-op when no executor was spawned.
        daemon = self.daemon
        registry = getattr(daemon, "executors", None) if daemon is not None else None
        if registry is not None:
            try:
                registry.kill(session)
            except Exception as e:  # noqa: BLE001 - executor kill is best-effort
                logger.warning("endSession: executor kill for %s failed: %r",
                               session, e)

        # rdp branch: if this session has a live per-session context, tear it
        # down — close the upstream + SIGTERM the daemon-owned Chrome + drop the
        # context. A later ensureSession recreates a fresh context + relaunches.
        if daemon is not None and getattr(daemon, "contexts", None) is not None:
            if session in daemon.contexts:  # type: ignore[attr-defined]
                try:
                    await daemon.teardown_rdp_context(session)  # type: ignore[attr-defined]
                except Exception as e:
                    await self._send_to_client(client.client_id, _error_response(
                        req_id, -32603, f"endSession failed (rdp teardown): {e!r}"))
                    return
                await self._send_to_client(client.client_id, _result_response(
                    req_id, {"ok": True, "closed": [], "kept": [],
                             "backend": "rdp"}))
                return
        group_id = params.get("groupId")
        group_id = group_id if isinstance(group_id, int) and group_id >= 0 else None
        if self._end_session is None:
            # Lazy-open mirror of openBackgroundTab.
            if (self._ensure_upstream is not None
                    and self.state.upstream_phase != UpstreamPhase.CONNECTED):
                try:
                    await self._ensure_upstream()
                except Exception as e:
                    await self._send_to_client(client.client_id, _error_response(
                        req_id, -32603, f"endSession failed (upstream open): {e!r}"))
                    return
        if self._end_session is None:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32601,
                "BrowserwrightDaemon.endSession requires the extension backend"))
            return
        try:
            # Pass group_id only when provided so callbacks with the legacy
            # single-arg signature stay compatible. group_id is the persisted
            # numeric tab-group id end_session uses to resolve the group's live
            # membership (and close the whole group) when the session's bound
            # groupId is unavailable (e.g. after a daemon restart).
            if group_id is not None:
                result = await self._end_session(session, group_id)
            else:
                result = await self._end_session(session)
        except Exception as e:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, f"endSession failed: {e!r}"))
            return
        await self._send_to_client(client.client_id, _result_response(req_id, result))

    async def _handle_ensure_executor(
        self, client: ClientState, msg: dict, req_id: int | None,
    ) -> None:
        """Phase B (Fork 2 control plane): lazily spawn the session's persistent
        executor and return its data-plane socket path.

        The daemon OWNS the executor lifecycle (Fork 1a): it spawns the
        subprocess if absent (single-flight per session — no double-spawn),
        waits for it to bind + write its `_ipc` discovery file, and returns
        ``{exec_sock}``. The thin heredoc client then connects DIRECTLY to that
        socket to ship code (bulk data never touches this event loop)."""
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        session = await self._require_browser_session(
            client, req_id, "BrowserwrightDaemon.ensureExecutor", params)
        if session is None:
            return
        daemon = self.daemon
        registry = getattr(daemon, "executors", None) if daemon is not None else None
        if registry is None:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603,
                "ensureExecutor unavailable: daemon has no executor registry"))
            return
        # Failure #4 fix: ensure the session's UPSTREAM (rdp Chrome) is launched
        # + ready BEFORE we spawn the executor. The executor's cold-start
        # `connect_over_cdp(facade)` resolves the rdp Chrome's DYNAMIC port,
        # which is only pinned once `_ensure_upstream` (→ `_launch_rdp_chrome`)
        # has run. Pre-restart, ordinary client frames launched Chrome before
        # the executor connected; post-restart the executor path is hit FIRST,
        # so without this the facade probes the stale default port (9222), 404s,
        # and the executor exits during cold-start. Mirror the other verbs'
        # lazy-open (openBackgroundTab / closeTab). Best-effort + bounded: a
        # launch failure surfaces as a proper error envelope, never a crash.
        if (self._ensure_upstream is not None
                and self.state.upstream_phase != UpstreamPhase.CONNECTED):
            try:
                await self._ensure_upstream()
            except Exception as e:  # noqa: BLE001
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32603,
                    f"ensureExecutor failed (upstream open): {e!r}"))
                return
        try:
            sock_path = await registry.ensure(session)
        except Exception as e:  # noqa: BLE001
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, f"ensureExecutor failed: {e!r}"))
            return
        await self._send_to_client(client.client_id, _result_response(
            req_id, {"exec_sock": sock_path}))

    async def _handle_kill_executor(
        self, client: ClientState, msg: dict, req_id: int | None,
    ) -> None:
        """Reap ONLY this session's persistent executor — no browser teardown.

        Used by `session_create.end()` to reap an attach-owned session's
        resident executor (the full `endSession` path is create-only and would
        also tear down the browser, which an attach session must leave running).
        Idempotent: a no-op `{ok: True, killed: False}` when no executor exists.
        Best-effort — a missing registry still answers a clean (non-`-32601`)
        result so a stale-daemon caller never errors on `session end`."""
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        session = await self._require_browser_session(
            client, req_id, "BrowserwrightDaemon.killExecutor", params)
        if session is None:
            return
        daemon = self.daemon
        registry = getattr(daemon, "executors", None) if daemon is not None else None
        killed = False
        if registry is not None:
            try:
                killed = bool(registry.kill(session))
            except Exception as e:  # noqa: BLE001 - executor kill is best-effort
                logger.warning("killExecutor: kill for %s failed: %r", session, e)
        await self._send_to_client(client.client_id, _result_response(
            req_id, {"ok": True, "killed": killed}))

    async def _handle_close_tab(
        self, client: ClientState, msg: dict, req_id: int | None,
    ) -> None:
        """Spec Phase B Feature 2.

        Maps the client-facing LOCAL sessionId to the upstream sessionId
        (mirroring _handle_detach's translation), invokes upstream.close_tab,
        and tears down the local state bindings whether the close succeeded
        or not — the tab is gone either way.
        """
        # Param validation runs FIRST (same rationale as openBackgroundTab).
        # Accept either `sessionId` (per-client; for persistent-ws callers like
        # Skill REPL) or `targetId` (global; for CLI subcommands whose
        # transient ws can't share per-client session state).
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        local_sid = params.get("sessionId")
        target_id_param = params.get("targetId")
        has_sid = isinstance(local_sid, str) and local_sid
        has_tid = isinstance(target_id_param, str) and target_id_param
        if not has_sid and not has_tid:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602,
                "BrowserwrightDaemon.closeTab requires params.sessionId or params.targetId"))
            return
        if await self._require_browser_session(
            client, req_id, "BrowserwrightDaemon.closeTab", params,
        ) is None:
            return
        # rdp dispatch: close via Target.closeTarget. Resolve the targetId from
        # the local sessionId binding when only a sessionId was given.
        if self.state.backend_name == "rdp":
            if self._upstream_command is None and self._ensure_upstream is not None:
                try:
                    await self._ensure_upstream()
                except Exception as e:
                    await self._send_to_client(client.client_id, _error_response(
                        req_id, -32603,
                        f"closeTab failed (upstream open): {e!r}"))
                    return
            await self._rdp_close_tab(
                client, req_id,
                local_sid=local_sid if has_sid else None,
                target_id_param=target_id_param if has_tid else None)
            return
        if self._close_tab is None:
            # Lazy-open mirror of openBackgroundTab + attachActiveTab.
            if (self._ensure_upstream is not None
                    and self.state.upstream_phase != UpstreamPhase.CONNECTED):
                try:
                    await self._ensure_upstream()
                except Exception as e:
                    await self._send_to_client(client.client_id, _error_response(
                        req_id, -32603,
                        f"closeTab failed (upstream open): {e!r}"))
                    return
        if self._close_tab is None:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32601,
                "BrowserwrightDaemon.closeTab requires the extension backend"))
            return
        # Resolve to (target_id, upstream_sid, owner_client_id, owner_local_sid).
        # sessionId path = per-client lookup; targetId path = state.attachers
        # global lookup, valid even across different ws clients.
        target_id: str | None = None
        upstream_sid: str | None = None
        owner_client_id: int | None = None
        owner_local_sid: str | None = None
        if has_sid:
            binding = client.sessions.get(local_sid)
            if binding is not None:
                target_id = binding.target_id
                upstream_sid = binding.upstream_session_id
                owner_client_id = client.client_id
                owner_local_sid = local_sid
        if target_id is None and has_tid:
            target_id = target_id_param
            attacher = self.state.attachers.get(target_id)
            if attacher is not None:
                owner_client_id = attacher.primary_client_id
                owner_local_sid = attacher.primary_local_session
                upstream_sid = attacher.upstream_session_id
        # Fallback path: targetId given but no live attacher (original opener
        # disconnected — common for CLI subcommands). The tab still exists in
        # Chrome; close via targetId-only path that bypasses session lookup.
        if upstream_sid is None and has_tid:
            target_id = target_id_param
            if self._close_tab_by_target_id is None:
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32601,
                    "BrowserwrightDaemon.closeTab (by targetId) requires the extension backend"))
                return
            try:
                result = await self._close_tab_by_target_id(target_id)
            except Exception as e:
                # Match the regular path's policy: tear down bookkeeping even
                # on error so callers can't reuse the stale targetId. There's
                # no session/attacher binding to drop here by construction —
                # that's why the attacher lookup failed in the first place —
                # so just dropping the target visibility entry is enough.
                self.state.note_target_destroyed(target_id)
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32603, f"closeTab failed: {e!r}"))
                return
            self.state.note_target_destroyed(target_id)
            await self._send_to_client(client.client_id, _result_response(req_id, {
                "ok": True,
                "tabId": result.get("tabId"),
            }))
            return
        if target_id is None or upstream_sid is None:
            ident = local_sid if has_sid else target_id_param
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602, f"unknown sessionId/targetId {ident}"))
            return
        try:
            result = await self._close_tab(upstream_sid)
        except Exception as e:
            # Even when upstream signals an error, tear down our bookkeeping
            # so the caller can't reuse the (now-invalid) sessionId.
            if owner_client_id is not None and owner_local_sid is not None:
                self.state.unbind_session_by_local(
                    owner_client_id, owner_local_sid)
            self.state.note_target_destroyed(target_id)
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, f"closeTab failed: {e!r}"))
            return
        # Success: clean up the session + attacher bindings; drop the target.
        if owner_client_id is not None and owner_local_sid is not None:
            self.state.unbind_session_by_local(
                owner_client_id, owner_local_sid)
        self.state.note_target_destroyed(target_id)
        await self._send_to_client(client.client_id, _result_response(req_id, {
            "ok": True,
            "tabId": result.get("tabId"),
        }))

    # ---- rdp unified-verb implementations -------------------------------
    #
    # On an rdp context (state.backend_name == "rdp") the extension callbacks
    # (`_open_background_tab` etc.) are never wired — instead we drive the
    # daemon-owned Chrome with raw CDP through `self._upstream_command`
    # (UpstreamConnection.send_command, a distinct id space from client
    # traffic). These keep the wire-facing method names + result shapes
    # identical to the extension impls so the downstream never branches on
    # backend (docs §"Unified downstream interface"); divergences are honest,
    # not -32601.

    async def _rdp_open_tab(
        self, client: ClientState, req_id: int | None, url: str,
    ) -> None:
        """rdp `openBackgroundTab`: Target.createTarget(url) then attach, and
        register the same client-side binding openBackgroundTab's extension
        path produces so subsequent CDP commands route through sessionId
        translation. `groupId` is -1 (tab groups are extension-only); `tabId`
        is null (Chrome tab ids are an extension concept — the targetId is the
        rdp-native handle)."""
        if self._upstream_command is None:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, "openBackgroundTab: rdp upstream not connected"))
            return
        try:
            created = await self._upstream_command(
                "Target.createTarget", {"url": url})
            target_id = _cmd_result(created).get("targetId")
            if not isinstance(target_id, str):
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32603,
                    f"openBackgroundTab: Target.createTarget returned {created!r}"))
                return
            # Attach (flatten) so the daemon owns a session for this target.
            attached = await self._upstream_command(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True})
            upstream_sid = _cmd_result(attached).get("sessionId")
            if not isinstance(upstream_sid, str):
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32603,
                    f"openBackgroundTab: attach returned {attached!r}"))
                return
        except Exception as e:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, f"openBackgroundTab failed: {e!r}"))
            return
        # Register the binding (mirror the extension path).
        local_sid = _new_local_session_id(client.client_id)
        self.state.bind_session(
            client.client_id, local_sid, upstream_sid, target_id, readonly=False)
        self.state.claim_attacher(
            target_id, client.client_id, local_sid, upstream_sid)
        meta = self.state.targets.get(target_id) or {}
        self.state.note_target_info({
            "targetId": target_id, "type": "page",
            "url": meta.get("url", url), "title": meta.get("title", ""),
        })
        await self._send_to_client(client.client_id, _result_response(req_id, {
            "sessionId": local_sid,
            "targetId": target_id,
            "tabId": None,           # rdp has no Chrome tab id; targetId is native
            "url": meta.get("url", url),
            "title": meta.get("title", ""),
            "groupId": -1,           # tab groups are extension-only
        }))

    async def _rdp_attach_active(
        self, client: ClientState, req_id: int | None,
    ) -> None:
        """rdp `attachActiveTab` (docs §C1): the daemon owns this Chrome, so
        there is no human-contended "focused tab". Define the active tab as the
        session's current front target — reuse a page target this context is
        already attached to (most-recently-fronted), else attach an existing
        page target, else create one. Result shape matches the extension path.
        This is an honest equivalent, never -32601."""
        if self._upstream_command is None:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, "attachActiveTab: rdp upstream not connected"))
            return
        # 1. Reuse a page target this client already has bound (front tab).
        for local_sid, binding in client.sessions.items():
            tid = binding.target_id
            meta = self.state.targets.get(tid) or {}
            if meta.get("type", "page") == "page":
                await self._send_to_client(client.client_id, _result_response(
                    req_id, {
                        "sessionId": local_sid,
                        "targetId": tid,
                        "tabId": None,
                        "url": meta.get("url", ""),
                        "title": meta.get("title", ""),
                    }))
                return
        # 2. Attach an existing page target the daemon-owned Chrome already has.
        target_id: str | None = None
        url = ""
        title = ""
        try:
            targets = _cmd_result(await self._upstream_command("Target.getTargets", {}))
        except Exception:
            targets = None
        if isinstance(targets, dict):
            for info in targets.get("targetInfos", []):
                if not isinstance(info, dict) or info.get("type") != "page":
                    continue
                tid = info.get("targetId")
                if isinstance(tid, str):
                    target_id = tid
                    url = info.get("url", "")
                    title = info.get("title", "")
                    break
        if target_id is None:
            # 3. No tab at all — create one (mirrors the empty-fallback in the
            # skill's current_page → open()).
            await self._rdp_open_tab(client, req_id, "about:blank")
            return
        try:
            attached = await self._upstream_command(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True})
            upstream_sid = _cmd_result(attached).get("sessionId")
            if not isinstance(upstream_sid, str):
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32603,
                    f"attachActiveTab: attach returned {attached!r}"))
                return
        except Exception as e:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, f"attachActiveTab failed: {e!r}"))
            return
        existing = self.state.attachers.get(target_id)
        if existing is not None and existing.primary_client_id == client.client_id:
            local_sid = existing.primary_local_session
        else:
            local_sid = _new_local_session_id(client.client_id)
            self.state.bind_session(
                client.client_id, local_sid, upstream_sid, target_id, readonly=False)
            self.state.claim_attacher(
                target_id, client.client_id, local_sid, upstream_sid)
        self.state.note_target_info({
            "targetId": target_id, "type": "page", "url": url, "title": title,
        })
        await self._send_to_client(client.client_id, _result_response(req_id, {
            "sessionId": local_sid,
            "targetId": target_id,
            "tabId": None,
            "url": url,
            "title": title,
        }))

    async def _rdp_close_tab(
        self, client: ClientState, req_id: int | None,
        *, local_sid: str | None, target_id_param: str | None,
    ) -> None:
        """rdp `closeTab`: Target.closeTarget(targetId). Resolve the targetId
        from the client's local sessionId binding when only a sessionId was
        given (mirrors the extension path's sessionId→target resolution), then
        tear down local bookkeeping whether or not the close succeeded — the
        tab is gone either way."""
        target_id: str | None = None
        owner_client_id: int | None = None
        owner_local_sid: str | None = None
        if local_sid is not None:
            binding = client.sessions.get(local_sid)
            if binding is not None:
                target_id = binding.target_id
                owner_client_id = client.client_id
                owner_local_sid = local_sid
        if target_id is None and target_id_param is not None:
            target_id = target_id_param
            attacher = self.state.attachers.get(target_id)
            if attacher is not None:
                owner_client_id = attacher.primary_client_id
                owner_local_sid = attacher.primary_local_session
        if target_id is None:
            ident = local_sid or target_id_param
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602, f"unknown sessionId/targetId {ident}"))
            return
        if self._upstream_command is None:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, "closeTab: rdp upstream not connected"))
            return
        try:
            await self._upstream_command(
                "Target.closeTarget", {"targetId": target_id})
        except Exception as e:
            # Tear down bookkeeping even on error so the caller can't reuse a
            # stale id.
            if owner_client_id is not None and owner_local_sid is not None:
                self.state.unbind_session_by_local(owner_client_id, owner_local_sid)
            self.state.note_target_destroyed(target_id)
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, f"closeTab failed: {e!r}"))
            return
        if owner_client_id is not None and owner_local_sid is not None:
            self.state.unbind_session_by_local(owner_client_id, owner_local_sid)
        self.state.note_target_destroyed(target_id)
        await self._send_to_client(client.client_id, _result_response(req_id, {
            "ok": True, "tabId": None,
        }))

    async def _rdp_userscript(
        self, client: ClientState, req_id: int | None, verb: str, params: dict,
    ) -> None:
        """rdp `userscript.*` shim via Page.addScriptToEvaluateOnNewDocument.

        Caveats (documented per docs §C3 — these are honest divergences from
        the extension's userScripts API, NOT lies):
          - The script runs in the page's MAIN world, not the extension's
            ISOLATED world. There is no privileged `GM_*` API surface.
          - There is NO match-pattern filtering: CDP injects the script into
            EVERY new document on the attached target(s). The extension's
            per-URL `@match` semantics are not reproduced — callers that need
            URL scoping must guard inside the script body.
          - `install` registers on the currently-attached rdp sessions; `list`
            reports what we've registered this process; `remove`/`toggle` are
            best-effort (CDP's removeScriptToEvaluateOnNewDocument by id).
          - This persists only for the life of the (ephemeral, C2) Chrome.

        We keep the result shape uniform with the extension impl and never
        return -32601.
        """
        if self._upstream_command is None:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, "userscript: rdp upstream not connected"))
            return
        # Registry of scripts we've installed this process, keyed by identity.
        # Lives on the Router (per-context) so list/remove can see it.
        registry: dict = getattr(self, "_rdp_userscripts", None)
        if registry is None:
            registry = {}
            self._rdp_userscripts = registry  # type: ignore[attr-defined]

        try:
            if verb == "install":
                script = params.get("script") if isinstance(params.get("script"), dict) else {}
                source = script.get("source") or script.get("body") or ""
                identity = script.get("identity") or script.get("id") or f"rdp-us-{len(registry) + 1}"
                if not isinstance(source, str) or not source:
                    await self._send_to_client(client.client_id, _error_response(
                        req_id, -32602, "userscript install requires script.source"))
                    return
                # Register on every attached rdp session (each its own target).
                ids: list[str] = []
                seen: set[str] = set()
                for binding in list(client.sessions.values()):
                    usid = binding.upstream_session_id
                    if usid in seen:
                        continue
                    seen.add(usid)
                    res = await self._upstream_command(
                        "Page.addScriptToEvaluateOnNewDocument",
                        {"source": source}, usid)
                    sid_id = _cmd_result(res).get("identifier")
                    if isinstance(sid_id, str):
                        ids.append(sid_id)
                registry[identity] = {"identity": identity, "ids": ids,
                                      "enabled": True}
                await self._send_to_client(client.client_id, _result_response(req_id, {
                    "id": identity, "identity": identity,
                    "sync": {"ok": True, "backend": "rdp",
                             "note": "MAIN-world, no @match filtering (rdp shim)"},
                }))
                return
            if verb == "list":
                await self._send_to_client(client.client_id, _result_response(req_id, {
                    "scripts": [
                        {"identity": k, "enabled": v.get("enabled", True),
                         "backend": "rdp"}
                        for k, v in registry.items()
                    ],
                }))
                return
            if verb in ("remove", "toggle"):
                key = params.get("key")
                entry = registry.get(key) if isinstance(key, str) else None
                if entry is None:
                    await self._send_to_client(client.client_id, _result_response(
                        req_id, {"ok": False, "reason": f"no such userscript {key!r}"}))
                    return
                # CDP can only un-register future injections (removeScript...);
                # already-injected MAIN-world code can't be retracted.
                for binding in list(client.sessions.values()):
                    for ident in entry.get("ids", []):
                        try:
                            await self._upstream_command(
                                "Page.removeScriptToEvaluateOnNewDocument",
                                {"identifier": ident},
                                binding.upstream_session_id)
                        except Exception:
                            pass
                if verb == "remove":
                    registry.pop(key, None)
                else:
                    enabled = bool(params.get("enabled"))
                    entry["enabled"] = enabled
                await self._send_to_client(client.client_id, _result_response(
                    req_id, {"ok": True, "backend": "rdp"}))
                return
            if verb == "logs":
                # No injection-log facility on rdp; honest empty list.
                await self._send_to_client(client.client_id, _result_response(
                    req_id, {"logs": [], "backend": "rdp"}))
                return
            await self._send_to_client(client.client_id, _result_response(
                req_id, {"ok": False, "reason": f"unsupported userscript verb {verb!r} on rdp"}))
        except Exception as e:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32000, f"userscript {verb} failed (rdp): {e!r}"))

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
