"""BrowserwrightDaemon.* session-verb handlers (split from proxy.py).

The ``SessionVerbsMixin`` carries the ``BrowserwrightDaemon.*`` RPC dispatcher
and its validation / JSON-RPC response policy. Browser operations live behind
the router's single ``Upstream`` reference; raw-CDP tab and userscript behavior
belongs to ``CdpUpstream``, not to this dispatcher.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from functools import partial
from typing import Any, Awaitable, Callable, Protocol

from ... import session_registry
from .state import ClientState, UpstreamPhase

logger = logging.getLogger(__name__)

_END_SESSION_BUDGET_S = 8.0


def _teardown_budget_result(backend: str) -> dict:
    return {
        "ok": False,
        "partial": True,
        "timedOut": True,
        "closed": [],
        "failed": [],
        "unknown": ["workspace"],
        "kept": [],
        "backend": backend,
    }


class Handler(Protocol):
    """Uniform call shape for one ``BrowserwrightDaemon.*`` verb."""

    async def __call__(
        self,
        router: "SessionVerbsMixin",
        client: ClientState,
        params: dict,
        req_id: int | None,
    ) -> None: ...


# ---- helpers shared with the translation engine (re-exported by proxy) ----


def _error_response(req_id: int | None, code: int, message: str) -> str:
    return json.dumps({"id": req_id, "error": {"code": code, "message": message}})


def _result_response(req_id: int | None, result: dict) -> str:
    return json.dumps({"id": req_id, "result": result})


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


# ---- the session-verb mixin ------------------------------------------------


class SessionVerbsMixin:
    """BrowserwrightDaemon.* verb handlers, mixed into ``proxy.Router``.

    All state lives on the Router (``self.state``, ``self.daemon``, and the
    single attached ``self.upstream`` adapter); this class only groups the verb
    methods. Never instantiated on its own.
    """

    def _session_group_name(
        self, client: ClientState, session_id: str,
        explicit: str | None = None,
    ) -> str:
        """Extension-only human-visible tab group title for a session."""
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

    async def _ready_upstream(
        self, client: ClientState, req_id: int | None, fail_prefix: str,
    ):
        """Return the single attached adapter, lazy-opening it when cold."""
        if (self.upstream is None and self._ensure_upstream is not None
                and self.state.upstream_phase != UpstreamPhase.CONNECTED):
            try:
                await self._ensure_upstream()
            except Exception as e:
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32603,
                    f"{fail_prefix} (upstream open): {e!r}"))
                return None
        if self.upstream is None:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, f"{fail_prefix}: upstream not attached"))
            return None
        return self.upstream

    async def _invoke_upstream(
        self,
        client: ClientState,
        req_id: int | None,
        fail_prefix: str,
        invoke: Callable[[Any], Awaitable[Any]],
        *,
        error_code: int = -32603,
        value_error_code: int | None = None,
    ) -> Any | None:
        """One readiness → invoke → error-response skeleton for verb handlers."""
        upstream = await self._ready_upstream(client, req_id, fail_prefix)
        if upstream is None:
            return None
        try:
            return await invoke(upstream)
        except ValueError as e:
            code = value_error_code or error_code
            detail = str(e) if value_error_code is not None else repr(e)
        except Exception as e:  # noqa: BLE001 - surface adapter failures
            code = error_code
            detail = repr(e)
        await self._send_to_client(client.client_id, _error_response(
            req_id, code, f"{fail_prefix}: {detail}"))
        return None

    async def _register_and_respond(
        self, client: ClientState, req_id: int | None, result: dict, *,
        malformed_msg: str, extra: dict | None = None,
        reuse_existing: bool = False,
    ) -> None:
        """Validate a tab-verb result and register its session binding.

        Mirrors the binding shape Target.attachToTarget would produce:
        allocate a local sessionId visible to the client, bind it to the
        upstream session, claim the attacher slot, and stash target metadata
        in the visibility table — so subsequent CDP commands routed by the
        client through this sessionId are translated upstream the same way.
        Then answer ``req_id`` with the standard tab payload plus ``extra``.

        ``reuse_existing`` (attachActiveTab): when the target already has an
        attacher, reuse the same client's existing local sessionId rather
        than minting a new one, and refuse another client's target.
        """
        upstream_sid = result.get("sessionId")
        target_id = result.get("targetId")
        if not isinstance(upstream_sid, str) or not isinstance(target_id, str):
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, f"{malformed_msg}: {result!r}"))
            return
        existing = self.state.attachers.get(target_id) if reuse_existing else None
        if existing is not None:
            if existing.primary_client_id != client.client_id:
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32602,
                    f"target {target_id} already attached by another client"))
                return
            local_sid = existing.primary_local_session
        else:
            local_sid = _new_local_session_id(client.client_id)
            self.state.bind_session(
                client.client_id, local_sid, upstream_sid, target_id,
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
        payload = {
            "sessionId": local_sid,
            "targetId": target_id,
            "tabId": result.get("tabId"),
            "url": result.get("url", ""),
            "title": result.get("title", ""),
        }
        if extra:
            payload.update(extra)
        await self._send_to_client(
            client.client_id, _result_response(req_id, payload))

    def _forget_tab_binding(
        self, target_id: str, owner_client_id: int | None = None,
        owner_local_sid: str | None = None,
    ) -> None:
        """Drop the router bookkeeping for one tab exactly once."""
        if owner_client_id is not None and owner_local_sid is not None:
            self.state.unbind_session_by_local(
                owner_client_id, owner_local_sid)
        self.state.note_target_destroyed(target_id)

    # ---- BrowserwrightDaemon.* (per-client RPC) -------------------------------

    async def _handle_browserdaemon(self, client: ClientState, msg: dict) -> None:
        method = msg["method"]
        req_id = msg.get("id") if isinstance(msg.get("id"), int) else None
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        handler = VERBS.get(method)
        if handler is None:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32601, f"unknown BrowserwrightDaemon method: {method}"))
            return
        await handler(self, client, params, req_id)

    async def _handle_get_backend_info(
        self, client: ClientState, params: dict, req_id: int | None,
    ) -> None:
        from ..backends import kind_for
        kind = kind_for(self.state.backend_name) or "UPSTREAM_WS"
        await self._send_to_client(client.client_id, _result_response(req_id, {
            "name": self.state.backend_name,
            "kind": kind,
            "ux_warnings": [],
            "schema_version": 1,
        }))

    async def _handle_status(
        self, client: ClientState, params: dict, req_id: int | None,
    ) -> None:
        # Status is deliberately whole-daemon and upstream-independent: it must
        # remain answerable precisely when browser work is wedged.
        from .status import snapshot
        await self._send_to_client(
            client.client_id,
            _result_response(req_id, snapshot(self.daemon, state=self.state)))

    async def _handle_wait_for_session_announce(
        self, client: ClientState, params: dict, req_id: int | None,
    ) -> None:
        session_id = await self._require_browser_session(
            client, req_id, "BrowserwrightDaemon.waitForSessionAnnounce", params)
        if session_id is None:
            return
        timeout = params.get("timeout")
        timeout = float(timeout) if isinstance(timeout, (int, float)) else 2.0
        announced = await self._invoke_upstream(
            client, req_id, "waitForSessionAnnounce failed",
            lambda upstream: upstream.wait_session_announce(
                session_id, timeout))
        if announced is None:
            return
        await self._send_to_client(client.client_id, _result_response(
            req_id, {"announced": bool(announced)}))

    async def _handle_extension_reload(
        self, client: ClientState, params: dict, req_id: int | None,
    ) -> None:
        result = await self._invoke_upstream(
            client, req_id, "extension reload failed",
            lambda upstream: upstream.reload_extensions(
                reason=str(params.get("reason") or "manual"),
                expected_version=(str(params["expectedVersion"])
                                  if params.get("expectedVersion") else None)),
            error_code=-32000)
        if result is None:
            return
        await self._send_to_client(
            client.client_id, _result_response(req_id, result or {}))

    async def _handle_attach_active_tab(
        self, client: ClientState, params: dict, req_id: int | None,
    ) -> None:
        session_id = await self._require_browser_session(
            client, req_id, "BrowserwrightDaemon.attachActiveTab", params)
        if session_id is None:
            return
        info = await self._invoke_upstream(
            client, req_id, "attach active failed",
            lambda upstream: upstream.attach_active(
                session_id=session_id,
                group_name=self._session_group_name(client, session_id)),
            error_code=-32000)
        if info is None:
            return
        await self._register_and_respond(
            client, req_id, info,
            malformed_msg="attach active returned malformed payload",
            reuse_existing=True)

    async def _handle_userscript(
        self, client: ClientState, params: dict, req_id: int | None, verb: str,
    ) -> None:
        method = f"BrowserwrightDaemon.userscript.{verb}"
        session_id = await self._require_browser_session(
            client, req_id, method, params)
        if session_id is None:
            return
        result = await self._invoke_upstream(
            client, req_id, f"userscript {verb} failed",
            lambda upstream: upstream.userscript_request(
                verb, params,
                session_ids=[b.upstream_session_id
                             for b in client.sessions.values()]),
            error_code=-32000)
        if result is None:
            return
        await self._send_to_client(
            client.client_id, _result_response(req_id, result or {}))

    # ---- Phase B: BrowserwrightDaemon.openBackgroundTab / closeTab ----------

    async def _handle_open_background_tab(
        self, client: ClientState, params: dict, req_id: int | None,
    ) -> None:
        """Spec Phase B Feature 1.

        Extension calls the extension upstream's open_background_tab inside the
        session tab group. RDP handles the same public verb with raw CDP against
        the session's isolated browser. Both paths register the returned
        (target_id, upstream_session_id) as a regular client-side binding so
        subsequent CDP commands work through the same session-id translation
        path as Target.attachToTarget.
        """
        # Param validation runs FIRST so an empty-params call answers -32602
        # ("bad params"), never -32601 ("unknown method") — checking backend
        # wiring first would make a missing param indistinguishable from an
        # unimplemented verb. Enforced by
        # tests/daemon/test_verb_schema_lock.py::
        # test_param_validation_runs_before_backend_wiring.
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
        # Extension-only: the tab-group title comes from the session label in
        # the ledger unless explicitly overridden. The durable identity is the
        # numeric groupId returned by the extension path, not this title.
        group_name = self._session_group_name(client, session, group_name)
        # `background` (default True) protects the user's focus on the
        # extension backend; background=False opens the tab in the foreground.
        background = params.get("background")
        background = background if isinstance(background, bool) else True
        skip_post_attach_commands = params.get("skipPostAttachCommands") is True
        result = await self._invoke_upstream(
            client, req_id, "openBackgroundTab failed",
            lambda upstream: upstream.open_tab(
                url, group_name=group_name, session_id=session,
                background=background,
                skip_post_attach_commands=skip_post_attach_commands))
        if result is None:
            return
        # groupId is just metadata for the caller.
        await self._register_and_respond(
            client, req_id, result,
            malformed_msg="openBackgroundTab returned malformed result",
            extra={"groupId": result.get("groupId", -1)})

    async def _handle_recover_session(
        self, client: ClientState, params: dict, req_id: int | None,
    ) -> None:
        """Session-reconnect-recovery.

        After a reconnect / daemon restart the in-memory session→tab bindings
        are gone, but the Chrome tab group id persisted in the session ledger
        may still identify a live group.
        Recover the tabs from that group, re-attach, and register a regular
        client-side binding for the representative tab so subsequent CDP
        commands route through the normal sessionId translation path (mirrors
        openBackgroundTab). The adapter decides whether recovery means durable
        group reconstruction or raw-CDP current-page rebinding."""
        group_id = params.get("groupId")
        if group_id is not None and (
                not isinstance(group_id, int) or group_id < 0):
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602,
                "BrowserwrightDaemon.recoverSession params.groupId must be "
                "a non-negative integer"))
            return
        session_id = await self._require_browser_session(
            client, req_id, "BrowserwrightDaemon.recoverSession", params)
        if session_id is None:
            return
        result = await self._invoke_upstream(
            client, req_id, "recoverSession failed",
            lambda upstream: upstream.recover(
                session_id,
                group_id=group_id if isinstance(group_id, int) else None),
            value_error_code=-32602)
        if result is None:
            return
        await self._register_and_respond(
            client, req_id, result,
            malformed_msg="recoverSession returned malformed result",
            extra={"groupId": result.get("groupId", -1),
                   "recovered": result.get("recovered", [])},
            reuse_existing=True)

    async def _handle_end_session(
        self, client: ClientState, params: dict, req_id: int | None,
    ) -> None:
        """P5.4 / Phase 2: tear down a browserwright session.

        Issue #32 initiate contract: the response is sent after the bounded
        fast phase (clients revoked, executor reaped, phase=terminating); the
        unbounded workspace teardown keeps running daemon-side under the
        session lock. A retried ``endSession`` (the CLI's poll) joins the
        in-flight teardown and returns the FINAL result, so the caller can
        never time out mid-teardown and never mistakes slow for hung.

        extension: close every tab in the session's adapter-owned tab group.

        rdp: the per-session context owns a dedicated Chrome. Close that Chrome
        (SIGTERM the launched pid), close the upstream, and drop the context —
        the uniform, non-`-32601` success shape (docs §RPCs)."""
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

        daemon = self.daemon
        registry = getattr(daemon, "executors", None) if daemon is not None else None

        group_id = params.get("groupId")
        group_id = group_id if isinstance(group_id, int) and group_id >= 0 else None
        # Adapters stop cooperatively at this deadline, after committing every
        # confirmed mutation. The registry deliberately never hard-cancels the
        # callback between a browser write and its durable checkpoint.
        teardown_deadline = time.monotonic() + _END_SESSION_BUDGET_S - 0.5

        async def teardown_workspace() -> dict:
            # This readiness + teardown runs under ExecutorRegistry's lifecycle
            # gate in production. A queued ensure therefore cannot reopen the
            # workspace between executor reap and terminal teardown.
            record = session_registry.get(session)
            record_backend = (
                record.get("backend") if isinstance(record, dict) else None)
            attach_owned_raw = (
                isinstance(record, dict)
                and record.get("owner") == "attach"
                and record_backend in ("rdp", "env")
            )
            if attach_owned_raw and self.upstream is None:
                ended: bool | None = None
                if record_backend == "rdp":
                    ended = False
                    teardown_rdp = getattr(
                        daemon, "teardown_rdp_context", None)
                    if callable(teardown_rdp):
                        ended = await teardown_rdp(
                            session, deadline=teardown_deadline)
                ok = ended is not False
                return {
                    "ok": ok,
                    "partial": not ok,
                    "timedOut": (
                        not ok and time.monotonic() >= teardown_deadline),
                    "closed": [],
                    "failed": [] if ok else ["workspace"],
                    "unknown": [] if ok else ["workspace"],
                    "kept": [],
                    "backend": record_backend,
                }
            if (self.upstream is None and self._ensure_upstream is not None
                    and self.state.upstream_phase != UpstreamPhase.CONNECTED):
                remaining = max(0.0, teardown_deadline - time.monotonic())
                if remaining <= 0:
                    return _teardown_budget_result(self.state.backend_name)
                try:
                    await asyncio.wait_for(
                        self._ensure_upstream(), timeout=remaining)
                except asyncio.TimeoutError:
                    # ensure_open may have been cancelled *after* publishing an
                    # adapter — assigned and attached — but before
                    # set_connected. Keying the reset on `upstream is None`
                    # left exactly that case in CONNECTING permanently, where
                    # _gate_upstream_ready neither forwards frames nor starts
                    # another open, so client traffic piles up unanswered in
                    # the pre-open buffer. Reset whenever the open did not
                    # finish, and take the half-published adapter down with it.
                    # No workspace mutation follows this result.
                    if self.state.upstream_phase != UpstreamPhase.CONNECTED:
                        partial = self.upstream
                        self.upstream = None
                        if partial is not None:
                            with contextlib.suppress(Exception):
                                partial.detach(self)
                            with contextlib.suppress(Exception):
                                await partial.close(reason="open_timeout")
                        await self.state.set_disconnected()
                    return _teardown_budget_result(self.state.backend_name)
            upstream = self.upstream
            if upstream is None:
                raise RuntimeError("upstream not attached")
            # Declared on the protocol, so called directly — see the
            # target_belongs_to_session note below on why probing is wrong.
            return await upstream.end_session_before(
                session, group_id, deadline=teardown_deadline)

        daemon_terminate = getattr(daemon, "terminate_session", None)
        terminate = (
            daemon_terminate if callable(daemon_terminate)
            else getattr(registry, "terminate_session", None))
        if callable(terminate):
            try:
                if callable(daemon_terminate):
                    reap, result = await terminate(
                        session, teardown_workspace,
                        caller_token=client.connection_token,
                        budget=_END_SESSION_BUDGET_S,
                        # Issue #32: return at the initiate boundary. The
                        # bounded fast phase (revoke + reap) completed; the
                        # unbounded workspace teardown continues daemon-side
                        # and the caller polls/joins for the final result — a
                        # slow teardown can no longer outlive this RPC.
                        wait=False)
                else:
                    reap, result = await terminate(
                        session, teardown_workspace,
                        budget=_END_SESSION_BUDGET_S)
                if reap.get("reaped") is not True:
                    await self._send_to_client(
                        client.client_id,
                        _error_response(
                            req_id, -32603,
                            "endSession could not confirm executor death: "
                            f"{reap!r}"),
                    )
                    return
                if isinstance(result, dict):
                    result.setdefault("backend", self.state.backend_name)
            except Exception as e:  # noqa: BLE001 - executor kill is best-effort
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32603,
                    f"endSession failed: {e!r}"))
                return
        else:
            # Lightweight test doubles and old embedders retain the prior
            # sequence; the real daemon registry always takes the atomic path.
            if registry is not None:
                try:
                    reap = await registry.kill_current_and_wait(session)
                    if reap.get("reaped") is not True:
                        await self._send_to_client(
                            client.client_id,
                            _error_response(
                                req_id, -32603,
                                "endSession could not confirm executor death: "
                                f"{reap!r}"),
                        )
                        return
                except Exception as e:  # noqa: BLE001
                    await self._send_to_client(client.client_id, _error_response(
                        req_id, -32603,
                        f"endSession executor reap failed: {e!r}"))
                    return
            result = await self._invoke_upstream(
                client, req_id, "endSession failed",
                lambda upstream: upstream.end_session(session, group_id))
            if result is None:
                return
        await self._send_to_client(client.client_id, _result_response(req_id, result))
        if (isinstance(result, dict) and result.get("ok") is True
                and client.connection_token is not None
                and daemon is not None):
            revoke = getattr(daemon, "revoke_session_lease", None)
            if callable(revoke):
                await revoke(client.connection_token)

    async def _handle_ensure_executor(
        self, client: ClientState, params: dict, req_id: int | None,
    ) -> None:
        """Phase B (Fork 2 control plane): lazily spawn the session's persistent
        executor and return its data-plane socket path.

        The daemon OWNS the executor lifecycle (Fork 1a): it spawns the
        subprocess if absent (single-flight per session — no double-spawn),
        waits for it to bind + write its `_ipc` discovery file, and returns
        ``{exec_sock}``. The thin heredoc client then connects DIRECTLY to that
        socket to ship code (bulk data never touches this event loop)."""
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
        async def preflight() -> None:
            if (self._prepare_executor is not None
                    and self.state.upstream_phase != UpstreamPhase.CONNECTED):
                try:
                    await self._prepare_executor(session)
                except Exception as e:  # noqa: BLE001
                    raise RuntimeError(
                        f"upstream readiness: {e}") from e
            if (self._ensure_upstream is not None
                    and self.state.upstream_phase != UpstreamPhase.CONNECTED):
                try:
                    await self._ensure_upstream()
                except Exception as e:  # noqa: BLE001
                    raise RuntimeError(f"upstream open: {e!r}") from e
        try:
            ensure_with_preflight = getattr(
                registry, "ensure_with_preflight", None)
            if callable(ensure_with_preflight):
                sock_path = await ensure_with_preflight(session, preflight)
            else:
                await preflight()
                sock_path = await registry.ensure(session)
        except Exception as e:  # noqa: BLE001
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, f"ensureExecutor failed: {e!r}"))
            return
        result = {"exec_sock": sock_path}
        get_handle = getattr(registry, "get", None)
        handle = get_handle(session) if callable(get_handle) else None
        executor_id = getattr(handle, "executor_id", None)
        if isinstance(executor_id, str) and executor_id:
            result["executor_id"] = executor_id
        await self._send_to_client(
            client.client_id, _result_response(req_id, result))

    async def _handle_kill_executor(
        self, client: ClientState, params: dict, req_id: int | None,
    ) -> None:
        """Reap ONLY this session's persistent executor — no browser teardown.

        Used by `session_create.end()` to reap an attach-owned session's
        resident executor (the full `endSession` path is create-only and would
        also tear down the browser, which an attach session must leave running).
        Idempotent: a no-op `{ok: True, killed: False}` when no executor exists.
        Best-effort — a missing registry still answers a clean (non-`-32601`)
        result so a stale-daemon caller never errors on `session end`."""
        session = await self._require_browser_session(
            client, req_id, "BrowserwrightDaemon.killExecutor", params)
        if session is None:
            return
        daemon = self.daemon
        registry = getattr(daemon, "executors", None) if daemon is not None else None
        killed = False
        reaped = False
        matched = True
        executor_id = params.get("executorId")
        if not isinstance(executor_id, str) or not executor_id:
            executor_id = None
        wait = params.get("wait") is True
        if registry is not None:
            try:
                result = await registry.kill_and_wait(
                    session, executor_id=executor_id)
                killed = bool(result.get("killed"))
                reaped = bool(result.get("reaped"))
                matched = bool(result.get("matched", True))
            except Exception as e:  # noqa: BLE001 - executor kill is best-effort
                logger.warning("killExecutor: kill for %s failed: %r", session, e)
        response = {"ok": True, "killed": killed}
        if wait:
            response.update({"reaped": reaped, "matched": matched})
        await self._send_to_client(
            client.client_id, _result_response(req_id, response))

    async def _handle_close_tab(
        self, client: ClientState, params: dict, req_id: int | None,
    ) -> None:
        """Spec Phase B Feature 2.

        Maps the client-facing LOCAL sessionId to the upstream sessionId
        (mirroring _handle_detach's translation), invokes upstream.close_tab,
        and tears down local state bindings only after upstream confirms the
        close. On failure the binding remains available for retry.
        """
        # Param validation runs FIRST (same rationale as openBackgroundTab:
        # empty params must answer -32602, never -32601).
        # Accept either `sessionId` (per-client; for persistent-ws callers like
        # Skill REPL) or `targetId` (global; for CLI subcommands whose
        # transient ws can't share per-client session state).
        local_sid = params.get("sessionId")
        target_id_param = params.get("targetId")
        has_sid = isinstance(local_sid, str) and local_sid
        has_tid = isinstance(target_id_param, str) and target_id_param
        if not has_sid and not has_tid:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602,
                "BrowserwrightDaemon.closeTab requires params.sessionId or params.targetId"))
            return
        browser_session = await self._require_browser_session(
            client, req_id, "BrowserwrightDaemon.closeTab", params,
        )
        if browser_session is None:
            return
        upstream = await self._ready_upstream(client, req_id, "closeTab failed")
        if upstream is None:
            return
        # Resolve to (target_id, owner_client_id, owner_local_sid).
        # sessionId path = per-client lookup; targetId path = state.attachers
        # global lookup, valid even across different ws clients.
        target_id: str | None = None
        owner_client_id: int | None = None
        owner_local_sid: str | None = None
        if has_sid:
            binding = client.sessions.get(local_sid)
            if binding is not None:
                target_id = binding.target_id
                owner_client_id = client.client_id
                owner_local_sid = local_sid
        if target_id is None and has_tid:
            target_id = target_id_param
            attacher = self.state.attachers.get(target_id)
            if attacher is not None:
                owner_client_id = attacher.primary_client_id
                owner_local_sid = attacher.primary_local_session
        if target_id is None:
            ident = local_sid if has_sid else target_id_param
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602, f"unknown sessionId/targetId {ident}"))
            return

        if has_sid and has_tid and target_id_param != target_id:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602,
                "sessionId and targetId refer to different tabs"))
            return

        # A local sessionId is only an address, not continuing authority. Both
        # forms are canonicalized to targetId before checking live group
        # membership, so a tab dragged to another session fails closed.
        # Called directly, never probed for. Wrapping an authorization check in
        # `if callable(getattr(...))` fails OPEN — an adapter missing the
        # method skips the check silently. Every adapter declares it.
        try:
            ownership = await upstream.target_belongs_to_session(
                browser_session, target_id)
        except Exception as e:  # noqa: BLE001 - unknown must fail closed
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603,
                f"closeTab ownership check failed: {e!r}"))
            return
        if ownership is None:
            if not self._raw_cdp_backend:
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32603,
                    "closeTab target ownership is unavailable for the shared "
                    "extension workspace"))
                return
            # Raw-CDP authorization is the browser-instance routing boundary.
            # The operation below can still fail if a forwarding-only adapter
            # does not implement close_session_tab; that is a capability error,
            # not an ownership denial.
        elif ownership is False:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32602,
                f"target {target_id} does not belong to session "
                f"{browser_session!r}"))
            return
        elif ownership is not True:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603,
                f"closeTab ownership check returned invalid value "
                f"{ownership!r}"))
            return
        try:
            # Every adapter declares close_session_tab, so this is one call and
            # not a hasattr probe: dispatching on whether a method exists is
            # the backend fork the Upstream protocol removed, and it hides the
            # behaviour change when an adapter later grows the method. No
            # attacher is required for the one-shot CLI path either — the live
            # ownership check above is the authorization boundary.
            result = await upstream.close_session_tab(browser_session, target_id)
        except Exception as e:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, f"closeTab failed: {e!r}"))
            return
        self._forget_tab_binding(
            target_id, owner_client_id, owner_local_sid)
        await self._send_to_client(client.client_id, _result_response(req_id, {
            "ok": True,
            "tabId": result.get("tabId"),
        }))


# The complete declared daemon-verb surface. Keep this explicit: recognition is
# data, while each handler shares the same four-argument skeleton above.
VERBS: dict[str, Handler] = {
    "BrowserwrightDaemon.getBackendInfo": SessionVerbsMixin._handle_get_backend_info,
    "BrowserwrightDaemon.status": SessionVerbsMixin._handle_status,
    "BrowserwrightDaemon.waitForSessionAnnounce": SessionVerbsMixin._handle_wait_for_session_announce,
    "BrowserwrightDaemon.attachActiveTab": SessionVerbsMixin._handle_attach_active_tab,
    "BrowserwrightDaemon.openBackgroundTab": SessionVerbsMixin._handle_open_background_tab,
    "BrowserwrightDaemon.closeTab": SessionVerbsMixin._handle_close_tab,
    "BrowserwrightDaemon.endSession": SessionVerbsMixin._handle_end_session,
    "BrowserwrightDaemon.ensureExecutor": SessionVerbsMixin._handle_ensure_executor,
    "BrowserwrightDaemon.killExecutor": SessionVerbsMixin._handle_kill_executor,
    "BrowserwrightDaemon.recoverSession": SessionVerbsMixin._handle_recover_session,
    "BrowserwrightDaemon.extension.reload": SessionVerbsMixin._handle_extension_reload,
    "BrowserwrightDaemon.userscript.install": partial(
        SessionVerbsMixin._handle_userscript, verb="install"),
    "BrowserwrightDaemon.userscript.list": partial(
        SessionVerbsMixin._handle_userscript, verb="list"),
    "BrowserwrightDaemon.userscript.remove": partial(
        SessionVerbsMixin._handle_userscript, verb="remove"),
    "BrowserwrightDaemon.userscript.toggle": partial(
        SessionVerbsMixin._handle_userscript, verb="toggle"),
    "BrowserwrightDaemon.userscript.logs": partial(
        SessionVerbsMixin._handle_userscript, verb="logs"),
}
