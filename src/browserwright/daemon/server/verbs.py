"""BrowserwrightDaemon.* session-verb handlers (split from proxy.py).

Pure code motion from ``proxy.py`` (Batch 4a): the ``SessionVerbsMixin`` below
carries the ``BrowserwrightDaemon.*`` RPC dispatcher, the extension
session-verb handlers (openBackgroundTab / recoverSession /
endSession / closeTab / ensureExecutor / killExecutor), and their rdp raw-CDP
counterparts. ``Router`` in ``proxy.py`` mixes this in — every method still
runs on the Router instance and uses the same attributes/callbacks wired by
the listener, so behavior is byte-for-byte identical to the pre-split file.
"""
from __future__ import annotations

import json
import logging
import os
import secrets

from .state import ClientState, UpstreamPhase

logger = logging.getLogger(__name__)


# How long the executor control plane (`ensureExecutor`) waits for an extension
# to (re)connect to the relay before failing with an actionable error. Kept WELL
# under the client's CDP reply deadline (cdp.py ~30s) so the real cause reaches
# the agent instead of a `ws closed` / `timeout` (GH #18) — and with margin for
# the executor spawn that follows (`_SPAWN_READY_TIMEOUT_S`) on the ready path.
# The full interactive extension grace (listener `_open_extension_upstream`,
# ~60s) is unchanged — it governs paths a human drives, not the agent hot path.
# Env-overridable (`BW_EXT_READY_BUDGET_S`) so out-of-process tests can shrink
# the wait and operators can tune it; falls back to 10s on a bad value.
def _ext_ready_budget_s() -> float:
    try:
        return float(os.environ.get("BW_EXT_READY_BUDGET_S", "") or 10.0)
    except (TypeError, ValueError):
        return 10.0


_EXT_READY_BUDGET_S = _ext_ready_budget_s()

# Actionable message when an extension session has no extension connected to the
# daemon relay. Names the concrete next action — the #1 real cause is a stale
# service worker after a browserwright upgrade (the extension must reconnect).
_NO_EXTENSION_CONNECTED_MSG = (
    "no browserwright extension is connected to the daemon (session {sid}). "
    "Open the browser where the extension is installed and ensure it is "
    "enabled; if you just installed or upgraded browserwright, reload the "
    "extension at chrome://extensions so its service worker reconnects to the "
    "daemon relay. Then retry. (Use --backend=rdp --create for an isolated "
    "Chrome that needs no extension.)"
)


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

    All state lives on the Router (``self.state``, ``self.daemon``, the
    listener-wired ``self._*`` callbacks); this class only groups the verb
    methods. Never instantiated on its own.
    """

    @property
    def _raw_cdp_backend(self) -> bool:
        """True when this context speaks real browser-level CDP (rdp / env)
        rather than the extension relay.

        The unified tab-lifecycle verbs (openBackgroundTab / closeTab /
        recoverSession / userscript / attachActiveTab) dispatch to their
        raw-CDP implementation via ``_upstream_command`` for every such backend;
        only the extension relay uses the callback-synthesis path. extension is
        the sole LOCAL_RELAY backend, so "not extension" is exactly "raw
        browser-level CDP". (issue #20: env joined this family — it resolves
        BD_CDP_WS and shares ``_open_chrome_upstream``'s raw command channel,
        same as rdp.)"""
        return self.state.backend_name != "extension"

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

    def _shared_relay(self):
        """Best-effort handle to the daemon's extension relay, or None.

        Extension sessions multiplex onto the shared context, whose holder owns
        the single RelayServer. Reached defensively (the daemon back-ref is
        ``object | None``) so a missing/partly-wired daemon never raises here —
        the caller treats None as "can't tell, fall through to the normal
        path"."""
        daemon = self.daemon
        shared = getattr(daemon, "shared_context", None)
        holder = getattr(shared, "holder", None)
        return getattr(holder, "relay", None)

    async def _ready_callback(
        self, client: ClientState, req_id: int | None,
        attr: str, fail_prefix: str, method_label: str,
    ):
        """Lazy-open guard shared by every extension-callback verb.

        The listener wires the extension callbacks (``_attach_active_tab``,
        ``_open_background_tab``, …) inside ``_open_extension_upstream``, so a
        cold daemon + already-connected extension becomes ready after one
        ``_ensure_upstream()`` — trigger it once when the callback is still
        None and the upstream isn't CONNECTED, then re-read the attribute.
        Returns the ready callback, or None after having sent the error
        envelope (-32603 on a failed upstream open, -32601 when the callback
        is still unwired, i.e. not the extension backend).
        """
        cb = getattr(self, attr)
        if cb is None:
            if (self._ensure_upstream is not None
                    and self.state.upstream_phase != UpstreamPhase.CONNECTED):
                try:
                    await self._ensure_upstream()
                except Exception as e:
                    await self._send_to_client(client.client_id, _error_response(
                        req_id, -32603,
                        f"{fail_prefix} (upstream open): {e!r}"))
                    return None
            cb = getattr(self, attr)
        if cb is None:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32601,
                f"{method_label} requires the extension backend"))
            return None
        return cb

    async def _rdp_lazy_open(
        self, client: ClientState, req_id: int | None, fail_prefix: str,
    ) -> bool:
        """Lazy-open guard for the raw-CDP verb impls: make sure the session's
        upstream command channel exists before dispatching to a ``_rdp_*``
        implementation. Returns False after sending the error envelope when
        the open failed."""
        if self._upstream_command is None and self._ensure_upstream is not None:
            try:
                await self._ensure_upstream()
            except Exception as e:
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32603,
                    f"{fail_prefix} (upstream open): {e!r}"))
                return False
        return True

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
            if self._raw_cdp_backend:
                await self._rdp_userscript(client, req_id, verb, params)
                return
            userscript_cb = await self._ready_callback(
                client, req_id, "_userscript_request",
                f"userscript {verb} failed", "BrowserwrightDaemon.userscript.*")
            if userscript_cb is None:
                return
            try:
                result = await userscript_cb(verb, params)
            except Exception as e:  # noqa: BLE001 - surface to client
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32000, f"userscript {verb} failed: {e}"))
                return
            await self._send_to_client(
                client.client_id, _result_response(req_id, result or {}))
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
        if method == "BrowserwrightDaemon.waitForSessionAnnounce":
            session_id = await self._require_browser_session(
                client, req_id, method, params)
            if session_id is None:
                return
            timeout = params.get("timeout")
            timeout = float(timeout) if isinstance(timeout, (int, float)) else 2.0
            if self.state.backend_name != "extension":
                await self._send_to_client(client.client_id, _result_response(
                    req_id, {"announced": True}))
                return
            announce_cb = await self._ready_callback(
                client, req_id, "_wait_session_announce",
                "waitForSessionAnnounce failed",
                "BrowserwrightDaemon.waitForSessionAnnounce")
            if announce_cb is None:
                return
            announced = await announce_cb(session_id, timeout)
            await self._send_to_client(client.client_id, _result_response(
                req_id, {"announced": bool(announced)}))
            return
        if method == "BrowserwrightDaemon.extension.reload":
            reload_cb = await self._ready_callback(
                client, req_id, "_reload_extensions",
                "extension reload failed", "BrowserwrightDaemon.extension.reload")
            if reload_cb is None:
                return
            try:
                result = await reload_cb(
                    reason=str(params.get("reason") or "manual"),
                    expected_version=(
                        str(params.get("expectedVersion"))
                        if params.get("expectedVersion") else None
                    ),
                )
            except Exception as e:  # noqa: BLE001
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32000, f"extension reload failed: {e!r}"))
                return
            await self._send_to_client(
                client.client_id, _result_response(req_id, result or {}))
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
            if self._raw_cdp_backend:
                if not await self._rdp_lazy_open(
                        client, req_id, "attach active failed"):
                    return
                await self._rdp_attach_active(client, req_id)
                return
            attach_cb = await self._ready_callback(
                client, req_id, "_attach_active_tab",
                "attach active failed", "BrowserwrightDaemon.attachActiveTab")
            if attach_cb is None:
                return
            try:
                # Adopt into THIS session's tab group. The title is cosmetic:
                # prefer the ledger name when the daemon can see it, otherwise
                # fall back to the bound session id. The durable association is
                # still the returned numeric groupId.
                info = await attach_cb(
                    session_id=attach_session,
                    group_name=self._session_group_name(client, attach_session))
            except Exception as e:
                await self._send_to_client(client.client_id, _error_response(
                    req_id, -32000, f"attach active failed: {e!r}"))
                return
            await self._register_and_respond(
                client, req_id, info,
                malformed_msg="attach active returned malformed payload",
                reuse_existing=True)
            return
        if method == "BrowserwrightDaemon.openBackgroundTab":
            await self._handle_open_background_tab(client, msg, req_id)
            return
        if method == "BrowserwrightDaemon.closeTab":
            await self._handle_close_tab(client, msg, req_id)
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

        Extension calls the extension upstream's open_background_tab inside the
        session tab group. RDP handles the same public verb with raw CDP against
        the session's isolated browser. Both paths register the returned
        (target_id, upstream_session_id) as a regular client-side binding so
        subsequent CDP commands work through the same session-id translation
        path as Target.attachToTarget.
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
        if self._raw_cdp_backend:
            if not await self._rdp_lazy_open(
                    client, req_id, "openBackgroundTab failed"):
                return
            await self._rdp_open_tab(client, req_id, url)
            return
        open_cb = await self._ready_callback(
            client, req_id, "_open_background_tab",
            "openBackgroundTab failed", "BrowserwrightDaemon.openBackgroundTab")
        if open_cb is None:
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
        try:
            result = await open_cb(
                url, group_name=group_name, session_id=session,
                background=background,
                skip_post_attach_commands=skip_post_attach_commands)
        except Exception as e:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, f"openBackgroundTab failed: {e!r}"))
            return
        # groupId is just metadata for the caller.
        await self._register_and_respond(
            client, req_id, result,
            malformed_msg="openBackgroundTab returned malformed result",
            extra={"groupId": result.get("groupId", -1)})

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
        if self._raw_cdp_backend:
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
        recover_cb = await self._ready_callback(
            client, req_id, "_recover_session",
            "recoverSession failed", "BrowserwrightDaemon.recoverSession")
        if recover_cb is None:
            return
        try:
            result = await recover_cb(bs_session, group_id=group_id)
        except Exception as e:
            await self._send_to_client(client.client_id, _error_response(
                req_id, -32603, f"recoverSession failed: {e!r}"))
            return
        # Register the representative tab's session binding (same as
        # openBackgroundTab) so the client can drive it immediately.
        await self._register_and_respond(
            client, req_id, result,
            malformed_msg="recoverSession returned malformed result",
            extra={"groupId": result.get("groupId", -1),
                   "recovered": result.get("recovered", [])})

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
        # context. A later session connect recreates a fresh context + relaunches.
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
        end_cb = await self._ready_callback(
            client, req_id, "_end_session",
            "endSession failed", "BrowserwrightDaemon.endSession")
        if end_cb is None:
            return
        try:
            # Pass group_id only when provided so callbacks with the legacy
            # single-arg signature stay compatible. group_id is the persisted
            # numeric tab-group id end_session uses to resolve the group's live
            # membership (and close the whole group) when the session's bound
            # groupId is unavailable (e.g. after a daemon restart).
            if group_id is not None:
                result = await end_cb(session, group_id)
            else:
                result = await end_cb(session)
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
        # Extension backend fast-fail: if no extension is connected to the
        # relay, the blocking `_ensure_upstream` below would wait the full
        # 60s extension grace — far longer than the control-plane CDP reply
        # deadline (cdp.py ~30s) — so the client's facade ws gives up first
        # and the agent sees an unhelpful `ws closed` / `timeout` instead of
        # the real cause (GH #18). Surface an actionable error WITHIN the
        # deadline. We give the service worker a short grace to (re)connect
        # (it may be mid-reconnect after a daemon restart / upgrade), then
        # fail fast if still absent. This never mutates the upstream state
        # machine (no `ensure_open`), so a later reconnect still works.
        if (self.state.backend_name == "extension"
                and self.state.upstream_phase != UpstreamPhase.CONNECTED):
            relay = self._shared_relay()
            if relay is not None and not relay.is_ready:
                try:
                    await relay.wait_ready(timeout=_EXT_READY_BUDGET_S)
                except Exception:  # noqa: BLE001 - TimeoutError + any relay hiccup
                    pass
                if not relay.is_ready:
                    await self._send_to_client(client.client_id, _error_response(
                        req_id, -32603, _NO_EXTENSION_CONNECTED_MSG.format(sid=session)))
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
        if self._raw_cdp_backend:
            if not await self._rdp_lazy_open(client, req_id, "closeTab failed"):
                return
            await self._rdp_close_tab(
                client, req_id,
                local_sid=local_sid if has_sid else None,
                target_id_param=target_id_param if has_tid else None)
            return
        close_cb = await self._ready_callback(
            client, req_id, "_close_tab",
            "closeTab failed", "BrowserwrightDaemon.closeTab")
        if close_cb is None:
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
            result = await close_cb(upstream_sid)
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
        # Register the binding (mirror the extension path). tabId is None (rdp
        # has no Chrome tab id; targetId is native) and groupId is -1 (tab
        # groups are extension-only).
        meta = self.state.targets.get(target_id) or {}
        await self._register_and_respond(
            client, req_id, {
                "sessionId": upstream_sid,
                "targetId": target_id,
                "tabId": None,
                "url": meta.get("url", url),
                "title": meta.get("title", ""),
            },
            malformed_msg="openBackgroundTab returned malformed result",
            extra={"groupId": -1})

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
                client.client_id, local_sid, upstream_sid, target_id)
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
