"""Playwright facade ↔ extension backend bridge (Task #tab-handle-model, PR2).

PR1's `facade.py` is a byte-for-byte CDP passthrough to a resolved upstream ws.
That works for the cdp backend because the daemon-owned Chrome speaks real
browser-level CDP natively (it emits `Target.attachedToTarget`/`targetCreated`,
handles `Target.*`/`Browser.*`). The **extension** backend has no resolvable
upstream ws — the daemon IS the relay, and `extension_upstream.py` only *acks*
`Target.setAutoAttach`/`setDiscoverTargets` (it never emits the unsolicited
target-lifecycle EVENTS that Playwright's `connect_over_cdp` handshake depends
on to discover tabs). So a Playwright client connects but `context.pages()` is
empty.

This module is the **extension-specific synthesis layer** the facade switches to
when the resolved backend is `extension`. The design (and the reason synthesis
lives HERE, not inside `extension_upstream.py`):

  - The agent client path (`BrowserwrightDaemon.*` over the unix socket) relies
    on `Target.setAutoAttach` being a *silent ack* — it drives discovery via the
    daemon's own RPCs, not via Chrome's auto-attach event stream. Emitting
    synthetic `attachedToTarget` into THAT path would be a regression. Only a
    raw Playwright client wants the events, and the facade is the only place
    that knows the consumer is a raw Playwright client.

  - We REUSE (not duplicate) the existing emulation in `ExtensionUpstream`:
    `Target.getTargets` / `Target.attachToTarget` / `Target.detachFromTarget` /
    `Browser.getVersion` and most session-scoped `chrome.debugger` forwarding
    already live there. This bridge constructs a *dedicated* `ExtensionUpstream`
    over the SAME shared `RelayServer` (so all those methods work unchanged),
    drives its shared synthesis kernels (`make_target_info`, `attach_target`,
    `register_session`, `resolve_tab_id`, `session_for_tab`,
    `evict_tab_sessions`, `close_tab_by_target_id`) rather than re-implementing
    them, and only ADDS:
      * A2 — `Target.setAutoAttach`/`setDiscoverTargets`: ack + replay
        `Target.targetCreated` + `Target.attachedToTarget` for every connected
        tab (mirrors playwriter's relay replay). Also pushes these when a tab is
        opened/attached later (via a relay fan-out listener).
      * A3 — `Target.createTarget` → `RelayServer.create_background_tab`
        (the extension can't open browser-level targets), then synthesizes the
        created/attached events for the new tab.
      * A4 — `Runtime.enable` execution-context barrier: discover real contexts
        from native document handles without subscribing to console previews;
        native Runtime events remain available via `BW_CAPTURE_CONSOLE=1`.

Page-session `Target.setAutoAttach` is forwarded because Playwright expects the
page-session auto-attach command to reach Chrome. The extension service worker
pre-arms `Target.setDiscoverTargets` + auto-attach and owns resuming child
sessions, while this facade keeps child Target.* lifecycle events hidden from
Playwright until full child-session routing exists.

`getTargets` scope policy: session-bound facade connections scope discovery to
that session's tab group, while genuinely sessionless raw CDP clients keep the
historical unscoped enumeration.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
from collections import deque
from typing import Any

from websockets.asyncio.server import ServerConnection

from ... import session_registry
from . import call_deadline
from .extension_contexts import UnsupportedContextHandleError, discover_default_context
from .extension_upstream import (
    ExtensionUpstream,
    _tab_id_from_target_id,
    make_target_info,
)
from .relay import RelayServer, _CommandError

logger = logging.getLogger(__name__)


# Bounded wait for the main-frame execution context after `Runtime.enable`
# (A4 / PR3). playwriter waits ~3s; we match that order of magnitude. On timeout
# we still return the enable result — the barrier is best-effort robustness, not
# a correctness gate.
_RUNTIME_ENABLE_BARRIER_TIMEOUT = 3.0

# After `Runtime.disable`, pause briefly before `Runtime.enable` so Chrome
# treats the re-enable as a fresh subscription and re-emits
# `executionContextCreated` for the existing default context to this
# late-joining client (playwriter's relay does the same disable→sleep→enable
# dance: cdp-relay.ts:792-829).
_RUNTIME_REENABLE_PAUSE = 0.05

# Cold-session announce retry (KNOWN BUG, issue #30): the extension announces
# a freshly-created tab (`attached`) BEFORE the createTab response lands, and
# the session→group binding is written from that response. For a session's
# FIRST tab the facade's visibility check therefore races the binding and can
# answer "not in my group" even though the tab was created for this session.
# Instead of skipping the announce permanently we re-check for a short window
# (the binding lands milliseconds later); a tab that never becomes visible
# (a foreign tab) expires without an announce — the scope check still gates.
#
# BUG B: this window used to be 5 x 0.1s = 0.5s, which was the real ceiling on
# a cold bind. It has to outlast an extension MV3 service-worker wake-up (the
# `scoped_target_infos` round trip goes to the SW), and it must not expire
# before the CLIENT's bind budget — otherwise the daemon quietly stops trying
# while the client is still waiting, and the tab is never announced for the
# life of the bridge, which is exactly what surfaced as a 100%-reproducible
# `PageBindTimeout` with a leaked tab per attempt. 30 x 0.2s = 6s sits under
# the 10s client budget (`repl.playwright_handle._PAGE_BIND_TIMEOUT_S`) with
# room for the round trip. Cost on the happy path is zero: the loop returns on
# the first successful check, and a foreign tab is idle waiting, not work.
_VISIBILITY_RETRY_COUNT = 30
_VISIBILITY_RETRY_INTERVAL = 0.2


# Synthetic browserContextId for synthesized page targets. The extension backend
# has no real CDP browser contexts (P4 — sessions isolate via tab groups), but
# Playwright requires a truthy browserContextId on attachedToTarget; a stable
# value routes every page into Playwright's single default context.
_SYNTHETIC_BROWSER_CONTEXT_ID = "browserwright-ext-default"

# Synthetic sessionId for the browser target itself (Playwright's
# `new_browser_cdp_session` → Target.attachToBrowserTarget). Commands arriving
# on this session are browser-level (Target.*/Browser.*), not page-scoped, so we
# strip the session and run them through the same session-less emulation.
_BROWSER_SESSION_ID = "browserwright-ext-browser-session"


# Browser-level CDP methods Playwright's `connect_over_cdp` handshake (and some
# context setup) issues that the extension backend cannot honor — but which are
# safe to ACK with an empty result so the handshake proceeds. `extension_upstream
# .py` returns -32601 for several of these (it serves the agent path, which never
# sends them); a raw Playwright client treats a -32601 during bootstrap as fatal.
# Mirrors playwriter's relay, which synthesizes benign successes for the
# browser-level methods it doesn't forward. SCOPED TO THE FACADE — the agent
# path's -32601 behavior is unchanged.
_BENIGN_BROWSER_NOOPS = frozenset({
    "Browser.setDownloadBehavior",
    "Storage.setStorageBucketTracking",
    "Target.autoAttachRelated",
    "Target.setRemoteLocations",
})


class ExtensionFacadeBridge:
    """One Playwright `connect_over_cdp` client bridged to the extension
    backend via the shared relay.

    Lifetime == one ws connection. `run()` pumps frames from the Playwright
    client through interception/synthesis until either side closes; `aclose()`
    detaches the relay listener and tears down state.
    """

    def __init__(
        self, *, client: ServerConnection, relay: RelayServer,
        session_id: str | None = None, session_name: str | None = None,
        binding_owner: ExtensionUpstream | None = None,
        label: str | None = None,
        auto_registry: set[str] | None = None,
    ):
        self._client = client
        self._relay = relay
        self._is_auto = session_id is None
        self._auto_registry = auto_registry
        if self._is_auto:
            # AUTO mode (ADR-0010): a sessionless raw CDP client still gets a
            # private tab group — ``<label>-BWauto-<hex>`` — so every
            # extension consumer owns exactly one group and can only see/act
            # on its own tabs ("no exceptions"). The synthetic sid keeps every
            # existing group-scoped path (`scoped_target_infos`,
            # `target_belongs_to_session`, `_record_group_binding`) working
            # unchanged; the title override teaches group resolution the
            # auto title without touching the persistent ledger.
            auto_sid = f"auto-{secrets.token_hex(4)}"
            session_id = auto_sid
            label = label or "anon"
            session_name = label
        self._session_id = session_id
        loaded_name = self._load_session_scope(session_id)
        self._session_name = session_name or loaded_name or session_id
        # A dedicated ExtensionUpstream over the SAME relay. on_frame routes
        # synthesized/forwarded frames back to THIS Playwright client. on_close
        # is a no-op — the facade owns connection teardown, not the upstream.
        self._ext = ExtensionUpstream(
            relay=relay,
            on_frame=self._send_to_client,
            on_close=self._noop_close,
            group_owner=binding_owner,
        )
        self._binding_owner = binding_owner or self._ext
        if self._is_auto:
            assert session_id is not None  # auto sid assigned above
            self._auto_sid = session_id
            self._auto_title = f"{label}-BW{session_id}"
            self._ext.bind_group_title(session_id, self._auto_title)
            if self._auto_registry is not None:
                self._auto_registry.add(session_id)
        # Ledger group ids are recovery candidates, not proof of live
        # ownership: Chrome can recycle them after restart. The shared
        # ExtensionUpstream validates title + known-tab membership before it
        # promotes one into the live binding map.
        # tab_id → synthetic flat sessionId we've handed Playwright for it. One
        # entry per tab we've announced via attachedToTarget, so a later
        # `targetDestroyed`/`detachedFromTarget` references the same session and
        # forwarded extension events can be tagged with the right sessionId.
        self._tab_sessions: dict[int, str] = {}
        # Explicit CDP attachments are independent handles over the shared
        # debugger. Reusing a primary session crashes Playwright's CDP router.
        self._aux_sessions: dict[str, int] = {}
        self._closed = False
        # Guard concurrent synthesis (autoAttach replay vs fan-out attach) so we
        # never announce the same tab twice.
        self._lock = asyncio.Lock()
        # >0 while a Target.createTarget is in flight: the fan-out `attached`
        # observer defers announcing so the createTarget RESPONSE is sent before
        # the target's attachedToTarget event (CDP ordering Playwright needs).
        self._creating = 0
        # Per-frame: the sessionId to echo on responses when the current command
        # arrived on the synthetic browser CDP session (Target.attachToBrowser-
        # Target). None for ordinary frames.
        self._echo_sid: str | None = None
        # PR3: last-known top-frame url per tab, fed from `Page.frameNavigated`
        # (and seeded from the relay ghost). Used to keep synthesized targetInfo
        # url fresh so Playwright isn't stranded on a stale value. A freshly-
        # created, not-yet-navigated tab is normalized to ":" (Chrome's initial
        # empty document) so CRPage's `isInitialEmptyPage` heuristic matches.
        self._tab_url: dict[int, str] = {}
        # PR3: tabs we created via Target.createTarget that have NOT yet seen a
        # real navigation — their targetInfo.url is reported as ":" (the initial
        # empty document) until the first frameNavigated lands. This is what
        # flips Playwright's `crPage.ts` init onto the benign initial-empty-page
        # branch instead of the "already navigated" one (research delta #2).
        self._fresh_blank_tabs: set[int] = set()
        # PR3: tab_id → the REAL Chrome main-frame id (from Page.getFrameTree).
        # Real Chrome makes a page's top-level frame id === its targetId, and
        # Playwright's CRPage keys its frame→session map on the targetId
        # (`_sessions.set(targetId, mainFrameSession)`), then looks the main
        # frame up by `frame.id` (`_sessionForFrame`). The extension backend's
        # targetId is the SYNTHETIC `ext-tab-<tabid>`, which never equals
        # Chrome's internal main-frame id — so the lookup throws "Frame has been
        # detached" and init rejects. We bridge by rewriting the main frame's id
        # to the synthetic targetId in everything we hand Playwright (frame tree
        # + page-domain events), and rewriting it back to the real id on
        # commands Playwright sends scoped to that frame.
        self._tab_main_frame: dict[int, str] = {}
        # PR3: per-(tab) futures awaiting the main-frame default
        # `Runtime.executionContextCreated` event, resolved by `_on_relay_event`
        # so `_handle_runtime_enable` can gate its response on the real event
        # rather than a blind sleep.
        self._ctx_waiters: dict[int, list[asyncio.Future]] = {}
        # Runtime.enable subscribes Chrome to console previews, including
        # synchronous table serialization. Native Document handles expose real
        # context IDs without enabling either Runtime events or Debugger.
        self._capture_console = os.environ.get("BW_CAPTURE_CONSOLE", "0") == "1"
        self._quiet_runtime_tabs: set[int] = set()
        self._native_runtime_tabs: set[int] = set()
        self._execution_contexts: dict[int, dict[int, dict]] = {}
        self._pending_console_text: dict[int, deque[dict]] = {}
        self._world_names: dict[int, set[str]] = {}
        self._frame_generations: dict[tuple[int, str], int] = {}
        self._frame_discovery_serial = 0
        self._context_refresh_tasks: dict[tuple[int, str], asyncio.Task] = {}
        self._context_refresh_pending: set[asyncio.Task] = set()

    @property
    def auto_title(self) -> str | None:
        """The tab group title of an auto (sessionless) connection, or None.

        Deterministic per connection (``<label>-BWauto-<hex>``); exposed so
        callers/tests can place tabs into the same group the scoping resolves.
        """
        return self._auto_title if self._is_auto else None

    @staticmethod
    def _load_session_scope(session_id: str | None) -> str | None:
        if not session_id:
            return None, None
        rec = session_registry.get(session_id)
        if not isinstance(rec, dict):
            return None
        name = rec.get("name")
        return name if isinstance(name, str) and name else None

    def _record_group_binding(self, group_id: int) -> None:
        """Record the live in-memory binding only.

        ADR-0009 removed the durable `runtime.group_id` mirror that used to be
        written here: the group is found again by its title, so a cached numeric
        id is a second source of truth with nothing to add.
        """
        if not self._session_id or group_id < 0:
            return
        self._binding_owner._bind_group(self._session_id, group_id)  # noqa: SLF001

    # ---- lifecycle -------------------------------------------------------

    async def run(self) -> None:
        """Pump the Playwright client until it (or the relay) closes."""
        self._relay.add_event_listener(self._on_relay_event)
        try:
            async for raw in self._client:
                if self._closed:
                    break
                if not isinstance(raw, (str, bytes)):
                    continue
                text = raw if isinstance(raw, str) else raw.decode(
                    "utf-8", errors="replace")
                await self._handle_client_frame(text)
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._relay.remove_event_listener(self._on_relay_event)
        # Native commands already on the shared relay must finish; subsequent
        # refresh steps observe _closed and stop without issuing more work.
        if self._context_refresh_pending:
            await asyncio.gather(*self._context_refresh_pending, return_exceptions=True)
        self._context_refresh_tasks.clear()
        self._context_refresh_pending.clear()
        self._frame_generations.clear()
        self._pending_console_text.clear()
        # NOTE: we deliberately do NOT call self._ext.close() — ExtensionUpstream
        # .close() calls relay.set_event_handler(None), which would clobber the
        # AGENT path's primary event handler. Our dedicated ExtensionUpstream
        # never called open() (so it never set the handler); it only lent us its
        # send_text emulation + session table. Tearing down our relay fan-out
        # listener above is the only relay-side cleanup we own.
        self._ext._open = False  # noqa: SLF001 — mark our adapter inert
        # Cancel any in-flight Runtime.enable barriers so a closing connection
        # never leaves awaiters hanging (CancelledError is BaseException; the
        # awaiters catch TimeoutError/CancelledError and proceed).
        for tab_id in list(self._ctx_waiters.keys()):
            for fut in self._ctx_waiters.pop(tab_id, []):
                if not fut.done():
                    fut.cancel()
        # AUTO teardown: close this connection's own group (its tabs), drop
        # the title override, and unregister from the reaper's live set. This
        # is what keeps a reconnecting consumer from accumulating orphaned
        # tab groups in the user's real Chrome.
        if self._is_auto and self._session_id:
            if self._auto_registry is not None:
                self._auto_registry.discard(self._session_id)
            try:
                await self._ext.close_auto_group(self._session_id)
            except Exception as e:  # noqa: BLE001 - teardown is best-effort
                logger.warning(
                    "facade(ext): auto-group teardown failed for %s: %r",
                    self._session_id, e)
            self._session_id = None

    # ---- client frame handling ------------------------------------------

    async def _handle_client_frame(self, frame: str) -> None:
        try:
            msg = json.loads(frame)
        except (ValueError, TypeError):
            logger.warning("facade(ext) got non-JSON: %s", frame[:80])
            return
        if not isinstance(msg, dict):
            return

        method = msg.get("method")
        req_id = msg.get("id") if isinstance(msg.get("id"), int) else None
        params = msg.get("params") or {}
        session_id = (msg.get("sessionId")
                      if isinstance(msg.get("sessionId"), str) else None)
        # The browser CDP session carries browser-level commands — treat them as
        # session-less (the emulation below keys on method name) but echo the
        # sessionId back so Playwright's CDPSession routing stays consistent.
        browser_session = session_id == _BROWSER_SESSION_ID
        if browser_session:
            # Echo the browser sessionId on every response/event for this frame
            # so Playwright's CDPSession routing matches; the browser-level
            # handlers below stay session-agnostic (run as session-less).
            self._echo_sid = session_id
            session_id = None
        else:
            self._echo_sid = None

        # --- Benign browser-level no-ops the handshake needs acked ---
        if (isinstance(method, str) and method in _BENIGN_BROWSER_NOOPS
                and session_id is None):
            await self._respond(req_id, {})
            return

        # --- Target.attachToBrowserTarget → synthesize a browser session. ---
        # Playwright's `new_browser_cdp_session()` uses this; the extension can't
        # provide a real browser-level session, so we hand back a stable
        # synthetic sessionId. Browser-level commands on it are then handled by
        # the same session-less emulation (the facade keys browser methods on
        # method name, not session).
        if method == "Target.attachToBrowserTarget" and session_id is None:
            await self._respond(req_id, {"sessionId": _BROWSER_SESSION_ID})
            return

        # --- Target.getBrowserContexts → synthesize one context. ---
        # puppeteer-core's `connect()` calls this during its browser bootstrap
        # (Playwright's connect_over_cdp does not). The extension backend has no
        # real browser contexts (P4 — isolation is tab groups), so we advertise
        # the single synthetic context the page targets already carry. puppeteer
        # then creates pages via Target.createTarget with that browserContextId,
        # which the auto/session group path ignores safely.
        if method == "Target.getBrowserContexts" and session_id is None:
            await self._respond(req_id, {
                "browserContextIds": [_SYNTHETIC_BROWSER_CONTEXT_ID]})
            return

        # --- Target.getTargetInfo: the handshake asks for the browser target ---
        # (no/unknown targetId) — synthesize it; a tab targetId returns that
        # tab's info. ExtensionUpstream errors here (it expects a sessionId), so
        # the facade answers directly.
        if method == "Target.getTargetInfo" and session_id is None:
            tid = params.get("targetId")
            tab_id = (_tab_id_from_target_id(tid)
                      if isinstance(tid, str) else None)
            if tab_id is not None:
                if not await self._authorize_target(req_id, tid):
                    return
                await self._respond(req_id, {"targetInfo": self._target_info(tab_id)})
            else:
                await self._respond(req_id, {"targetInfo": self._browser_target_info()})
            return

        # --- A2: discovery handshake → ack + replay target events ---
        if method in ("Target.setAutoAttach", "Target.setDiscoverTargets"):
            # The browser-level (session-less) form is the discovery handshake;
            # ack and replay every known tab. A *session-scoped* setAutoAttach
            # (auto-attach for a target's children) is forwarded like any other
            # session command below.
            if session_id is None:
                await self._respond(req_id, {})
                await self._replay_all_targets()
                return

        # --- A3: createTarget → open a background tab via the extension ---
        if method == "Target.createTarget" and session_id is None:
            await self._handle_create_target(req_id, params)
            return

        # --- Target.closeTarget (browser-level, {targetId}) → close the tab. ---
        # Playwright issues this during page teardown; ExtensionUpstream returns
        # -32601 for the session-less form, which aborts Playwright's page setup.
        # Map it to the relay's close-by-target-id (no session lookup needed).
        if method == "Target.closeTarget" and session_id is None:
            await self._handle_close_target(req_id, params)
            return

        # --- A4: Runtime.enable barrier (session-scoped) ---
        if method == "Runtime.enable" and session_id is not None:
            if session_id in self._aux_sessions:
                await self._forward_session_command(req_id, session_id, method, params)
                return
            await self._handle_runtime_enable(req_id, session_id, params)
            return

        # --- Browser-level (session-less) Target/Browser emulation: reuse
        # ExtensionUpstream (getTargets / attachToTarget / detachFromTarget /
        # Browser.getVersion). Its responses carry no sessionId, which is
        # correct for these browser-level methods. ---
        if session_id is None:
            if method == "Target.detachFromTarget" and params.get("sessionId") in self._aux_sessions:
                detached_sid = params["sessionId"]
                self._aux_sessions.pop(detached_sid)
                await self._respond(req_id, {})
                await self._send_to_client(json.dumps({
                    "method": "Target.detachedFromTarget",
                    "params": {"sessionId": detached_sid},
                }))
                return
            if method == "Target.getTargets" and self._session_id is not None:
                try:
                    envelope = await self._ext.get_targets(
                        params, self._session_id)
                except Exception as e:  # noqa: BLE001
                    await self._error(
                        req_id, -32603, f"getTargets scoping failed: {e!r}")
                    return
                if isinstance(envelope.get("error"), dict):
                    error = envelope["error"]
                    await self._error(
                        req_id, int(error.get("code", -32603)),
                        str(error.get("message", "getTargets failed")))
                else:
                    await self._respond(req_id, envelope.get("result") or {})
                return
            if method == "Target.attachToTarget":
                await self._handle_attach_to_target(req_id, params)
                return
            await self._ext.send_text(frame)
            return

        # --- Session-scoped commands: forward to the tab's chrome.debugger and
        # echo the response WITH the sessionId. This is the critical difference
        # from the agent path: Playwright drives FLAT sessions and routes every
        # response by (sessionId, id) — a response missing the sessionId lands
        # on the root session, whose id space doesn't know it, tripping
        # Playwright's `_onMessage` assert and dropping the connection. The
        # daemon Router re-adds sessionId for the agent path; the facade must do
        # it here because it talks raw flat-session CDP to Playwright. ---
        await self._forward_session_command(req_id, session_id, method, params)

    def _command_wait_s(self) -> float | None:
        """How long a forwarded command may wait in the relay: derived from
        the deadline of the agent call currently running on this session
        (ADR-0014; see `call_deadline.command_wait_s`), or ``None`` — the
        relay's own default — when no call is bound (a raw Playwright client,
        or Playwright traffic between calls)."""
        return call_deadline.command_wait_s(self._session_id)

    async def _forward_session_command(self, req_id: int | None,
                                       session_id: str, method: str | None,
                                       params: dict) -> None:
        """Forward a session-scoped command to the tab's chrome.debugger and
        echo `{id, sessionId, result|error}`."""
        tab_id = self._tab_id_for_session(session_id)
        if tab_id is None:
            await self._error(req_id, -32602,
                              f"unknown sessionId {session_id!r}",
                              session_id=session_id)
            return
        # PR3: a command scoped to the synthetic main-frame id (which equals the
        # targetId we handed Playwright) must target the REAL Chrome frame id.
        self._rewrite_command_frame_id(tab_id, params)
        if method in ("Page.createIsolatedWorld", "Page.addScriptToEvaluateOnNewDocument"):
            name = params.get("worldName")
            if isinstance(name, str) and name:
                self._world_names.setdefault(tab_id, set()).add(name)
        try:
            result = await self._relay.send_cdp(
                tab_id, method or "", params, timeout=self._command_wait_s())
            if method == "Runtime.enable":
                self._native_runtime_tabs.add(tab_id)
            elif method == "Runtime.disable":
                self._native_runtime_tabs.discard(tab_id)
            if (method == "Page.createIsolatedWorld"
                    and tab_id in self._quiet_runtime_tabs
                    and isinstance(result, dict)
                    and isinstance(result.get("executionContextId"), int)):
                await self._publish_execution_context(tab_id, {
                    "id": result["executionContextId"], "origin": "",
                    "name": params.get("worldName", ""),
                    "auxData": {"isDefault": False, "type": "isolated",
                                "frameId": params.get("frameId")},
                })
            # PR3: real Chrome makes a page's TOP frame id === its targetId, and
            # CRPage keys its frame→session map on the targetId
            # (`_sessions.set(targetId, mainFrameSession)`) then resolves the
            # main frame by `frame.id` (`_sessionForFrame`). Our targetId is the
            # SYNTHETIC `ext-tab-<tabid>`, never Chrome's internal main-frame id,
            # so that lookup throws "Frame has been detached" and `new_page()`
            # init rejects. Remember the real↔synthetic mapping and present the
            # synthetic targetId as the main frame id in the frame tree (the same
            # rewrite is mirrored on forwarded events + inbound commands), making
            # the page look exactly like a real-Chrome top-level target.
            #
            # NOTE: we deliberately do NOT rewrite the frame url to ":" here.
            # CRPage init computes `isInitialEmptyPage = mainFrame().url() === ":"`;
            # when TRUE it withholds `_firstNonInitialNavigationCommittedFulfill()`
            # (waiting for a real navigation), and since init awaits
            # `_firstNonInitialNavigationCommittedPromise`, a fresh `new_page()`
            # that never navigates would hang. Leaving the real `about:blank` url
            # makes `isInitialEmptyPage` FALSE → init fulfills immediately, which
            # is exactly how a real-Chrome `context.new_page()` settles.
            if (method == "Page.getFrameTree"
                    and isinstance(result, dict)):
                frame = (result.get("frameTree") or {}).get("frame")
                if isinstance(frame, dict):
                    real_id = frame.get("id")
                    if isinstance(real_id, str) and real_id:
                        self._tab_main_frame[tab_id] = real_id
                        frame["id"] = f"ext-tab-{tab_id}"
                        # Existing child frames are replayed at cold bind too.
                        # Their own ids stay native; only the root parent id
                        # must agree with Playwright's synthetic main frame.
                        pending = list((result.get("frameTree") or {}).get("childFrames") or [])
                        while pending:
                            child = pending.pop()
                            child_frame = child.get("frame") or {}
                            if child_frame.get("parentId") == real_id:
                                child_frame["parentId"] = f"ext-tab-{tab_id}"
                            pending.extend(child.get("childFrames") or [])
            await self._respond(req_id, result, session_id=session_id)
        except _CommandError as e:
            await self._error(req_id, e.code, e.message, session_id=session_id)
        except Exception as e:  # noqa: BLE001
            await self._error(req_id, -32603, f"relay send failed: {e!r}",
                              session_id=session_id)

    async def _handle_attach_to_target(self, req_id: int | None,
                                       params: dict) -> None:
        """Forward to ExtensionUpstream but capture the fabricated sessionId so
        our tab↔session table stays consistent with what Playwright holds (so
        forwarded async events get the right sessionId tag)."""
        target_id = params.get("targetId")
        tab_id = (_tab_id_from_target_id(target_id)
                  if isinstance(target_id, str) else None)
        if (isinstance(target_id, str)
                and not await self._authorize_target(req_id, target_id)):
            return
        # An explicit attachment to an announced Page is an auxiliary handle;
        # never reuse or re-announce its primary auto-attachment session.
        if tab_id is not None and tab_id in self._tab_sessions:
            sid = f"ext-aux-{secrets.token_hex(12)}"
            self._aux_sessions[sid] = tab_id
            await self._respond(req_id, {"sessionId": sid})
            return
        # Unknown tab → let ExtensionUpstream do the attach + sid fabrication,
        # then snoop its table to learn the sid it handed back.
        await self._ext.send_text(json.dumps({
            "id": req_id, "method": "Target.attachToTarget", "params": params,
        }))
        if tab_id is not None:
            sid = self._ext.session_for_tab(tab_id)
            if sid is not None:
                self._tab_sessions[tab_id] = sid

    async def _handle_create_target(self, req_id: int | None,
                                    params: dict) -> None:
        """A3: map browser-level Target.createTarget to a real background tab.

        Playwright calls this for `context.new_page()`. The extension can't
        issue browser-level CDP, so we open a tab via the relay's
        `create_background_tab` (the same primitive `open_background` uses) and
        return its `ext-tab-<id>` targetId. The subsequent attach + page events
        are synthesized so Playwright wires up the new Page object."""
        url = params.get("url") or "about:blank"
        # CDP ORDERING (critical): Playwright's `doCreateNewPage` does
        # `const {targetId} = await createTarget(...); return
        # this._crPages.get(targetId)._page` — it expects the target's
        # `Target.attachedToTarget` to have ALREADY been delivered (and the
        # CRPage registered) by the time the createTarget RESPONSE arrives. Real
        # Chrome emits attachedToTarget before the response. So we suppress the
        # extension's own `attached` fan-out for this tab (in-flight guard),
        # announce attachedToTarget OURSELVES first, THEN send the response.
        self._creating += 1
        try:
            group_name = self._ext._group_title_for(self._session_id) or "Agent"
            gt = await self._ext.open_background_tab(
                url, group_name=group_name,
                session_id=self._session_id,
                background=True,
                skip_post_attach_commands=True)
            tab_id = int(gt["tabId"])
            created_group = gt.get("groupId")
            if isinstance(created_group, int) and created_group >= 0:
                self._record_group_binding(created_group)
            # PR3 (research delta #2): a brand-new, not-yet-navigated tab must be
            # reported to Playwright with the initial-empty-document url ":" (NOT
            # "about:blank"), so CRPage's `isInitialEmptyPage = mainFrame().url()
            # === ":"` heuristic takes the benign branch instead of treating the
            # page as already-navigated (which flips init onto the path that
            # rejects → close). Real Chrome reports ":" for createTarget targets;
            # the extension's tab has already committed about:blank by attach
            # time, so we normalize it here until the first real frameNavigated.
            if url in ("", "about:blank"):
                self._fresh_blank_tabs.add(tab_id)
            else:
                self._tab_url[tab_id] = url
            # Announce BEFORE the response (Chrome ordering Playwright relies on).
            await self._announce_target(
                tab_id, sid=gt.get("sessionId"), send_created=True)
        except Exception as e:  # noqa: BLE001
            await self._error(req_id, -32603,
                              f"createTarget→createTab failed: {e!r}")
            return
        finally:
            self._creating -= 1
        await self._respond(req_id, {"targetId": gt["targetId"]})

    async def _handle_close_target(self, req_id: int | None,
                                   params: dict) -> None:
        """Browser-level Target.closeTarget → close the tab via the shared
        close-by-target-id path (derive tabId from the ext-tab-<id> targetId;
        no session needed)."""
        target_id = params.get("targetId")
        tab_id = (_tab_id_from_target_id(target_id)
                  if isinstance(target_id, str) else None)
        if tab_id is None:
            # CDP returns success:false for an unknown target rather than erroring.
            await self._respond(req_id, {"success": False})
            return
        if not await self._authorize_target(
                req_id, f"ext-tab-{tab_id}", close_response=True):
            return
        validated_generation = self._relay.connection_generation
        sid = self._tab_sessions.get(tab_id)
        try:
            # Same core as the agent path's closeTab-by-targetId verb: evicts
            # the upstream's fabricated sessions for the tab + relay close.
            await self._ext.close_tab_by_target_id(
                f"ext-tab-{tab_id}",
                expected_generation=validated_generation)
        except Exception as e:  # noqa: BLE001
            logger.warning("facade(ext) closeTarget tab %s failed: %r", tab_id, e)
            await self._respond(req_id, {"success": False})
            return
        # Evict the facade-local per-tab state too.
        await self._detach_aux_sessions(tab_id)
        self._evict_tab(tab_id)
        await self._respond(req_id, {"success": True})
        # Real Chrome ALWAYS emits detachedFromTarget + targetDestroyed after a
        # successful closeTarget; Playwright's page-creation/teardown path AWAITS
        # targetDestroyed to settle the CRPage. The relay only surfaces a
        # `detached` event for a USER-driven tab close, not for this
        # daemon-initiated `close_tab`, so the events must be synthesized here —
        # otherwise (e.g. when CRPage `_initialize` rejects and Playwright closes
        # the freshly-created target) `new_page()` hangs forever waiting for the
        # destroy that never arrives. Mirrors the `detached` relay-event path.
        await self._emit_target_detached(tab_id, sid)

    async def _handle_runtime_enable(self, req_id: int | None,
                                     session_id: str, params: dict) -> None:
        """A4 / PR3: event-gated `Runtime.enable` barrier — the single most
        important CRPage-init fidelity fix per playwriter.

        CRPage `_initialize` issues `Runtime.enable` and expects the default
        execution context to materialize before init completes; if the
        late-joining Playwright client never sees the main-frame
        `executionContextCreated`, init's promise chain settles as an error and
        Playwright closes the freshly-created target. The extension's
        `chrome.debugger` session is shared/long-lived, so a plain
        `Runtime.enable` may NOT re-emit `executionContextCreated` for a context
        that already existed before this client subscribed.

        We mirror playwriter's relay dance (cdp-relay.ts:792-829):
          1. `Runtime.disable` → short pause → `Runtime.enable` so Chrome treats
             it as a fresh subscription and re-emits `executionContextCreated`
             for the existing default context.
          2. HOLD this `Runtime.enable` response until we OBSERVE the default
             (`auxData.isDefault == true`) `executionContextCreated` for this
             tab (forwarded via `_on_relay_event`), bounded by ~3s. On timeout
             we still return the enable result (best-effort, not a hard gate).

        The architectural choice: the disable/enable round-trip is issued HERE
        in the bridge over its relay upstream (not pushed down to the
        extension), because the bridge owns the Playwright-facing flat session
        AND already observes the extension event fan-out via `_on_relay_event` —
        so it is the one place that can both drive the re-subscribe and watch
        for the resulting event without a second transport hop."""
        tab_id = self._tab_id_for_session(session_id)
        if tab_id is None:
            await self._error(req_id, -32602,
                              f"unknown sessionId {session_id!r}",
                              session_id=session_id)
            return
        if not self._capture_console:
            await self._enable_quiet_runtime(req_id, session_id, tab_id)
            return
        # Arm the waiter BEFORE issuing enable so we can't miss the event
        # between the enable round-trip and registering the future.
        waiter = self._arm_context_waiter(tab_id)
        try:
            # Force re-emission of executionContextCreated for the existing
            # default context: disable → pause → enable.
            with contextlib.suppress(_CommandError, Exception):
                await self._relay.send_cdp(
                    tab_id, "Runtime.disable", {},
                    timeout=self._command_wait_s())
            await asyncio.sleep(_RUNTIME_REENABLE_PAUSE)
            result = await self._relay.send_cdp(
                tab_id, "Runtime.enable", params,
                timeout=self._command_wait_s())
        except _CommandError as e:
            self._disarm_context_waiter(tab_id, waiter)
            await self._error(req_id, e.code, e.message, session_id=session_id)
            return
        except Exception as e:  # noqa: BLE001
            self._disarm_context_waiter(tab_id, waiter)
            await self._error(req_id, -32603, f"Runtime.enable failed: {e!r}",
                              session_id=session_id)
            return
        # Gate the response on the real default-context event (bounded).
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    waiter, timeout=_RUNTIME_ENABLE_BARRIER_TIMEOUT)
        finally:
            self._disarm_context_waiter(tab_id, waiter)
        await self._respond(req_id, result, session_id=session_id)

    async def _enable_quiet_runtime(self, req_id: int | None,
                                    session_id: str, tab_id: int) -> None:
        """Supply real context IDs without previews or debugger machinery."""
        self._quiet_runtime_tabs.add(tab_id)
        self._native_runtime_tabs.discard(tab_id)
        self._forget_tab_discoveries(tab_id)
        self._execution_contexts.pop(tab_id, None)
        waiter = self._arm_context_waiter(tab_id)
        try:
            await self._relay.send_cdp(tab_id, "Runtime.disable", {},
                                       timeout=self._command_wait_s())
            await self._relay.send_cdp(tab_id, "Console.enable", {},
                                       timeout=self._command_wait_s())
            await self._relay.send_cdp(tab_id, "Debugger.disable", {},
                                       timeout=self._command_wait_s())
            tree = await self._relay.send_cdp(tab_id, "Page.getFrameTree", {},
                                              timeout=self._command_wait_s())
            root = tree.get("frameTree") or {}
            main_id = (root.get("frame") or {}).get("id")
            if not isinstance(main_id, str):
                raise ValueError("Chrome returned no main frame for context discovery")
            self._tab_main_frame[tab_id] = main_id
            # A rebind discovers every existing local frame; it does not rely
            # on an event subscription replay or a page-script preload.
            pending = [root]
            while pending:
                node = pending.pop()
                frame_id = (node.get("frame") or {}).get("id")
                if isinstance(frame_id, str):
                    generation = self._invalidate_frame_discovery(tab_id, frame_id)
                    await self._refresh_frame_worlds(tab_id, frame_id, generation)
                pending.extend(node.get("childFrames") or [])
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(waiter, _RUNTIME_ENABLE_BARRIER_TIMEOUT)
            await self._respond(req_id, {}, session_id=session_id)
        except _CommandError as exc:
            await self._error(req_id, exc.code, exc.message, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            await self._error(req_id, -32603, f"context discovery failed: {exc!r}",
                              session_id=session_id)
        finally:
            self._disarm_context_waiter(tab_id, waiter)

    def _invalidate_frame_discovery(self, tab_id: int, frame_id: str) -> int:
        key = (tab_id, frame_id)
        self._frame_discovery_serial += 1
        generation = self._frame_discovery_serial
        self._frame_generations[key] = generation
        # Let in-flight native commands finish. Cancelling a relay command can
        # invalidate the shared extension transport; generation checks discard
        # the result after it completes instead.
        self._context_refresh_tasks.pop(key, None)
        return generation

    def _schedule_frame_refresh(self, tab_id: int, frame_id: str,
                                *, rediscover: bool = False) -> None:
        key = (tab_id, frame_id)
        aliases = {frame_id}
        if frame_id == self._tab_main_frame.get(tab_id):
            aliases.add(f"ext-tab-{tab_id}")
        if not rediscover and any(
                (ctx.get("auxData") or {}).get("isDefault")
                and (ctx.get("auxData") or {}).get("frameId") in aliases
                for ctx in self._execution_contexts.get(tab_id, {}).values()):
            return
        pending = self._context_refresh_tasks.get(key)
        if pending is not None and not pending.done():
            return
        generation = self._frame_generations.get(key)
        if generation is None:
            generation = self._invalidate_frame_discovery(tab_id, frame_id)
        task = asyncio.create_task(self._refresh_frame_worlds(tab_id, frame_id, generation))
        self._context_refresh_tasks[key] = task
        self._context_refresh_pending.add(task)
        task.add_done_callback(self._frame_refresh_done)

    def _frame_refresh_done(self, task: asyncio.Task) -> None:
        self._context_refresh_pending.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("facade(ext): context discovery failed: %r", task.exception())

    def _frame_discovery_current(self, tab_id: int, frame_id: str,
                                 generation: int) -> bool:
        return (not self._closed and tab_id in self._quiet_runtime_tabs
                and self._frame_generations.get((tab_id, frame_id)) == generation)

    async def _publish_execution_context(self, tab_id: int, ctx: dict) -> None:
        context_id = ctx.get("id")
        if not isinstance(context_id, int):
            return
        contexts = self._execution_contexts.setdefault(tab_id, {})
        previous = contexts.get(context_id)
        if previous is not None:
            old_aux = previous.get("auxData") or {}
            new_aux = ctx.get("auxData") or {}
            if (old_aux.get("frameId") == new_aux.get("frameId")
                    and old_aux.get("isDefault") == new_aux.get("isDefault")
                    and previous.get("name") == ctx.get("name")
                    and previous.get("_native_identity") == ctx.get("_native_identity")):
                return
            contexts.pop(context_id)
            await self._on_relay_event({
                "type": "event", "tabId": tab_id,
                "method": "Runtime.executionContextDestroyed",
                "params": {"executionContextId": context_id},
            })
        ctx['_console_ready'] = False
        contexts[context_id] = ctx
        payload = {key: value for key, value in ctx.items() if not key.startswith('_')}
        if isinstance(payload.get("auxData"), dict):
            payload["auxData"] = dict(payload["auxData"])
        await self._on_relay_event({
            "type": "event", "tabId": tab_id,
            "method": "Runtime.executionContextCreated", "params": {"context": payload},
        })
        main_ids = {self._tab_main_frame.get(tab_id), f"ext-tab-{tab_id}"}
        aux = ctx.get('auxData') or {}
        if aux.get('isDefault') and aux.get('frameId') in main_ids:
            # Publish the realm before text events, and preserve their order
            # while more native messages arrive during the flush.
            while (self._execution_contexts.get(tab_id, {}).get(context_id) is ctx
                   and self._pending_console_text.get(tab_id)):
                message = self._pending_console_text[tab_id].popleft()
                if tab_id not in self._native_runtime_tabs:
                    await self._emit_console_text(tab_id, context_id, message)
            if not self._pending_console_text.get(tab_id):
                self._pending_console_text.pop(tab_id, None)
        ctx['_console_ready'] = True

    def _console_context_id(self, tab_id: int) -> int | None:
        main_ids = {self._tab_main_frame.get(tab_id), f"ext-tab-{tab_id}"}
        return next((context_id for context_id, context in
                     self._execution_contexts.get(tab_id, {}).items()
                     if (context.get("auxData") or {}).get("isDefault")
                     and context.get('_console_ready')
                     and (context.get("auxData") or {}).get("frameId") in main_ids), None)

    async def _emit_console_text(self, tab_id: int, context_id: int, message: dict) -> None:
        await self._on_relay_event({
            "type": "event", "tabId": tab_id,
            "method": "Runtime.consoleAPICalled", "params": {
                "type": message.get("level", "log"),
                "args": [{"type": "string", "value": message.get("text", "")}],
                "executionContextId": context_id, "timestamp": 0,
            },
        })

    async def _refresh_frame_worlds(self, tab_id: int, frame_id: str,
                                    generation: int) -> None:
        async def send(method: str, params: dict) -> dict:
            return await self._relay.send_cdp(
                tab_id, method, params, timeout=self._command_wait_s())

        # frameNavigated arrives at WillCommitLoad, before the new Document
        # necessarily exists. Retry only while this exact document generation
        # remains current, and let later lifecycle events retry an early miss.
        for attempt in range(6):
            if not self._frame_discovery_current(tab_id, frame_id, generation):
                return
            try:
                context = await discover_default_context(
                    send, frame_id, main=frame_id == self._tab_main_frame.get(tab_id))
                if not self._frame_discovery_current(tab_id, frame_id, generation):
                    return
                aliases = {frame_id}
                if frame_id == self._tab_main_frame.get(tab_id):
                    aliases.add(f"ext-tab-{tab_id}")
                if any((ctx.get("auxData") or {}).get("isDefault")
                       and (ctx.get("auxData") or {}).get("frameId") in aliases
                       and ctx.get("_native_identity") != context["_native_identity"]
                       for ctx in self._execution_contexts.get(tab_id, {}).values()):
                    await self._retire_frame_contexts(tab_id, frame_id)
                await self._publish_execution_context(tab_id, context)
                break
            except UnsupportedContextHandleError:
                raise
            except Exception as exc:  # noqa: BLE001 - a document may be provisional
                if attempt == 5:
                    logger.debug("facade(ext): context discovery for frame %s: %r",
                                 frame_id, exc)
                    return
                await asyncio.sleep(0.025 * 2 ** attempt)
        for name in self._world_names.get(tab_id, set()).copy():
            if not self._frame_discovery_current(tab_id, frame_id, generation):
                return
            with contextlib.suppress(_CommandError, Exception):
                result = await send("Page.createIsolatedWorld", {
                    "frameId": frame_id, "worldName": name,
                    "grantUniveralAccess": True,
                })
                if not self._frame_discovery_current(tab_id, frame_id, generation):
                    return
                await self._publish_execution_context(tab_id, {
                    "id": result.get("executionContextId"), "origin": "", "name": name,
                    "auxData": {"isDefault": False, "type": "isolated", "frameId": frame_id},
                })

    async def _retire_frame_contexts(self, tab_id: int, frame_id: str) -> None:
        contexts = self._execution_contexts.get(tab_id, {})
        for context_id, ctx in list(contexts.items()):
            frame = (ctx.get("auxData") or {}).get("frameId")
            if frame in (frame_id, f"ext-tab-{tab_id}" if frame_id == self._tab_main_frame.get(tab_id) else frame_id):
                contexts.pop(context_id, None)
                await self._on_relay_event({
                    "type": "event", "tabId": tab_id,
                    "method": "Runtime.executionContextDestroyed",
                    "params": {"executionContextId": context_id},
                })

    def _forget_tab_discoveries(self, tab_id: int) -> None:
        for key in [key for key in self._frame_generations if key[0] == tab_id]:
            self._frame_generations.pop(key, None)
            self._context_refresh_tasks.pop(key, None)

    async def _retire_tab_contexts(self, tab_id: int) -> None:
        contexts = self._execution_contexts.pop(tab_id, {})
        for context_id in contexts:
            await self._on_relay_event({
                "type": "event", "tabId": tab_id,
                "method": "Runtime.executionContextDestroyed",
                "params": {"executionContextId": context_id},
            })

    def _arm_context_waiter(self, tab_id: int) -> asyncio.Future:
        """Register a future resolved when the next default
        `Runtime.executionContextCreated` for `tab_id` is observed."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._ctx_waiters.setdefault(tab_id, []).append(fut)
        return fut

    def _tab_id_for_session(self, session_id: str) -> int | None:
        """Resolve only sids this facade connection actually handed out.

        ``ExtensionUpstream.resolve_tab_id`` intentionally understands the
        synthetic sid's text format for daemon-internal recovery. It is not an
        authorization boundary: a facade client could otherwise invent
        ``ext-sid-<foreign-tab>-anything`` and bypass attachToTarget entirely.
        """
        if session_id in self._aux_sessions:
            return self._aux_sessions[session_id]
        return next(
            (tab_id for tab_id, sid in self._tab_sessions.items()
             if sid == session_id),
            None,
        )

    async def _authorize_target(
        self, req_id: int | None, target_id: str, *, close_response: bool = False,
    ) -> bool:
        if self._session_id is None:
            return True
        try:
            ownership = await self._binding_owner.target_belongs_to_session(
                self._session_id, target_id)
        except Exception as e:  # noqa: BLE001 - unknown ownership fails closed
            if close_response:
                await self._respond(req_id, {"success": False})
            else:
                await self._error(
                    req_id, -32603, f"target ownership check failed: {e!r}")
            return False
        if ownership is True:
            return True
        if ownership is None:
            if close_response:
                await self._respond(req_id, {"success": False})
            else:
                await self._error(
                    req_id, -32603,
                    "target ownership is unavailable for the shared extension "
                    "workspace")
            return False
        if ownership is not False:
            if close_response:
                await self._respond(req_id, {"success": False})
            else:
                await self._error(
                    req_id, -32603,
                    f"target ownership check returned invalid value "
                    f"{ownership!r}")
            return False
        if close_response:
            await self._respond(req_id, {"success": False})
        else:
            await self._error(
                req_id, -32602,
                f"target {target_id} does not belong to session "
                f"{self._session_id!r}")
        return False

    def _disarm_context_waiter(self, tab_id: int,
                               fut: asyncio.Future) -> None:
        waiters = self._ctx_waiters.get(tab_id)
        if waiters and fut in waiters:
            waiters.remove(fut)
            if not waiters:
                self._ctx_waiters.pop(tab_id, None)
        if not fut.done():
            fut.cancel()

    def _resolve_context_waiters(self, tab_id: int) -> None:
        """Wake every waiter for `tab_id` (the default execution context just
        landed)."""
        for fut in self._ctx_waiters.pop(tab_id, []):
            if not fut.done():
                fut.set_result(None)

    # ---- target event synthesis (A2) ------------------------------------

    async def _replay_all_targets(self) -> None:
        """Replay targetCreated + attachedToTarget for visible tabs.

        Session-bound facade connections see only the tab group recorded for
        that Browserwright session. Sessionless raw CDP clients keep the legacy
        unscoped view across all attached tabs.
        """
        if self._session_id is not None:
            try:
                infos = await self._ext.scoped_target_infos(self._session_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("facade(ext) scoped replay failed: %r", e)
                infos = []
            logger.debug("facade(ext): replaying %d target(s) for session %s",
                         len(infos), self._session_id)
            for info in infos:
                target_id = info.get("targetId") if isinstance(info, dict) else None
                tab_id = _tab_id_from_target_id(target_id) if isinstance(target_id, str) else None
                if tab_id is not None:
                    await self._announce_target(tab_id, send_created=True)
            return
        for g in self._relay.list_ghost_targets():
            tab_id = _tab_id_from_target_id(g.target_id)
            if tab_id is None:
                continue
            await self._announce_target(tab_id, send_created=True)

    async def _announce_target(self, tab_id: int, *, sid: str | None = None,
                               send_created: bool) -> None:
        """Synthesize the target-lifecycle frames Playwright needs to attach a
        Page to `tab_id`. Idempotent per tab: if we've already announced this
        tab (it has a session), we don't re-emit attachedToTarget (which would
        make Playwright create a duplicate Page)."""
        async with self._lock:
            already = tab_id in self._tab_sessions
            if sid is None:
                sid = self._tab_sessions.get(tab_id)
            # Shared sid fabrication + upstream session-table registration, so
            # session-scoped commands Playwright sends for this sid resolve to
            # the tab.
            sid = self._ext.register_session(tab_id, sid)
            self._tab_sessions[tab_id] = sid
            if already:
                return
            # Make sure the extension actually has chrome.debugger attached so
            # subsequent session-scoped commands work (idempotent in the relay).
            with contextlib.suppress(Exception):
                await self._relay.attach_tab(tab_id, timeout=10.0)
            info = self._target_info(tab_id)
            if send_created:
                await self._send_to_client(json.dumps({
                    "method": "Target.targetCreated",
                    "params": {"targetInfo": info},
                }))
            await self._send_to_client(json.dumps({
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": sid,
                    "targetInfo": info,
                    "waitingForDebugger": False,
                },
            }))
            self._relay.set_session_announce(self._session_id)

    async def _retry_visibility_announce(self, tab_id: int) -> None:
        """Re-check session visibility for a tab whose `attached` raced the
        createTab response (cold-session first tab, issue #30). Announces as
        soon as the tab is a member of this session's group; expires silently.
        Idempotent — a concurrent announce (replay, later fan-out) marks the
        tab announced and stops the retries."""
        for _ in range(_VISIBILITY_RETRY_COUNT):
            await asyncio.sleep(_VISIBILITY_RETRY_INTERVAL)
            if self._closed:
                return
            if tab_id in self._tab_sessions:
                return  # already announced by another path
            if await self._tab_visible_to_session(tab_id):
                await self._announce_target(tab_id, send_created=True)
                return

    async def _tab_visible_to_session(self, tab_id: int) -> bool:
        if self._session_id is None:
            return True
        try:
            infos = await self._ext.scoped_target_infos(self._session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("facade(ext) scoped visibility check failed: %r", e)
            return False
        target_id = f"ext-tab-{tab_id}"
        return any(
            isinstance(info, dict) and info.get("targetId") == target_id
            for info in infos
        )

    def _browser_target_info(self) -> dict:
        """Synthetic targetInfo for the browser itself (type=browser). Some
        handshake steps query it before any page target exists."""
        return make_target_info(
            target_id="browserwright-extension-browser",
            type="browser",
            title="Browserwright",
        )

    def _target_info(self, tab_id: int) -> dict:
        """Build a CDP targetInfo from the relay's current ghost view, kept
        fresh from `Page.frameNavigated` (PR3)."""
        url = ""
        title = ""
        for g in self._relay.list_ghost_targets():
            if _tab_id_from_target_id(g.target_id) == tab_id:
                url = g.url
                title = g.title
                break
        # PR3: prefer the live top-frame url we track from frameNavigated so
        # getTargetInfo/attachedToTarget never strand Playwright on a stale
        # value; a freshly-created blank tab is reported as the initial empty
        # document ":" (research delta #2 / #3).
        if tab_id in self._fresh_blank_tabs:
            url = ":"
        elif tab_id in self._tab_url:
            url = self._tab_url[tab_id]
        return make_target_info(
            target_id=f"ext-tab-{tab_id}",
            type="page",
            title=title,
            url=url,
            # Playwright's `_onAttachedToTarget` asserts a TRUTHY browserContextId
            # and looks it up in its known contexts, falling back to the default
            # context when not found. A stable synthetic id satisfies the assert
            # and routes the page into Playwright's default context (the
            # extension backend has no real browser contexts — P4).
            browser_context_id=_SYNTHETIC_BROWSER_CONTEXT_ID,
        )

    # ---- relay fan-out (new tabs + async page events) -------------------

    async def _on_relay_event(self, ext_msg: dict) -> None:
        """Called by the relay for every extension `attached`/`detached`/`event`
        message (fan-out observer). Translates them into the frames a live
        Playwright client expects."""
        if self._closed:
            return
        kind = ext_msg.get("type")
        tab_id = ext_msg.get("tabId")
        if not isinstance(tab_id, int):
            return

        if kind == "attached":
            # A newly-attached tab (popup click, daemon-driven adopt, or our own
            # createTarget). Announce it so Playwright spawns a Page — UNLESS a
            # createTarget is in flight: that path announces explicitly AFTER it
            # sends the createTarget response (CDP ordering), so deferring here
            # avoids emitting attachedToTarget before the response.
            if self._creating > 0:
                return
            visible = await self._tab_visible_to_session(tab_id)
            if visible:
                await self._announce_target(tab_id, send_created=True)
                return
            # Not visible (yet). For the session's FIRST tab this is normally
            # the announce-before-createTab-response race: the extension emits
            # `attached` before the response that carries the group binding, so
            # the check above queried a session with no group at all. Defer a
            # bounded re-check instead of skipping permanently — the binding
            # lands milliseconds later and the retry announces the tab. A tab
            # that never becomes a member of this session's group expires with
            # nothing announced (same outcome as the old unconditional skip).
            asyncio.get_running_loop().create_task(
                self._retry_visibility_announce(tab_id))
            return

        if kind == "detached":
            sid = self._tab_sessions.get(tab_id)
            if self._session_id is not None and sid is None:
                return
            await self._detach_aux_sessions(tab_id)
            self._evict_tab(tab_id)
            await self._emit_target_detached(tab_id, sid)
            return

        if kind == "event":
            method = ext_msg.get("method")
            params = ext_msg.get("params") or {}
            if not isinstance(method, str):
                return
            if method in ("Target.attachedToTarget", "Target.detachedFromTarget"):
                return
            if method == "Console.messageAdded" and tab_id in self._quiet_runtime_tabs:
                if tab_id in self._native_runtime_tabs:
                    return
                message = params.get("message") or {}
                if message.get("source") == "console-api":
                    context_id = self._console_context_id(tab_id)
                    if context_id is None:
                        # Keep native text from early page scripts while a new
                        # default realm is being discovered. No object preview
                        # subscription or page-side console hook is involved.
                        self._pending_console_text.setdefault(tab_id, deque(maxlen=1000)).append(message)
                    else:
                        await self._emit_console_text(tab_id, context_id, message)
                return
            # PR3: keep the live top-frame url fresh and release the fresh-blank
            # normalization once the page actually navigates, so getTargetInfo
            # stops reporting ":" after the first real navigation.
            if method == "Page.frameNavigated":
                frame = params.get("frame") or {}
                if tab_id in self._quiet_runtime_tabs and isinstance(frame.get("id"), str):
                    if not frame.get("parentId"):
                        # A replacement root document destroys all child realms,
                        # even if Chrome omits child frameDetached events during
                        # a renderer swap and reuses numeric context IDs.
                        self._forget_tab_discoveries(tab_id)
                        await self._retire_tab_contexts(tab_id)
                        self._tab_main_frame[tab_id] = frame["id"]
                    else:
                        await self._retire_frame_contexts(tab_id, frame["id"])
                    self._invalidate_frame_discovery(tab_id, frame["id"])
                    self._schedule_frame_refresh(tab_id, frame["id"])
                # Top frame only: no parentId.
                if isinstance(frame, dict) and not frame.get("parentId"):
                    new_url = frame.get("url")
                    if isinstance(new_url, str) and new_url and new_url != ":":
                        self._tab_url[tab_id] = new_url
                        self._fresh_blank_tabs.discard(tab_id)
            elif method == "Page.frameDetached" and tab_id in self._quiet_runtime_tabs:
                frame_id = params.get("frameId")
                if isinstance(frame_id, str):
                    key = (tab_id, frame_id)
                    self._frame_generations.pop(key, None)
                    self._context_refresh_tasks.pop(key, None)
                    await self._retire_frame_contexts(tab_id, frame_id)
            elif (method == "Page.lifecycleEvent" and tab_id in self._quiet_runtime_tabs
                  and params.get("name") in ("init", "DOMContentLoaded")):
                frame_id = params.get("frameId")
                if isinstance(frame_id, str):
                    self._schedule_frame_refresh(tab_id, frame_id, rediscover=True)
            elif method == "DOM.documentUpdated" and tab_id in self._quiet_runtime_tabs:
                frame_id = self._tab_main_frame.get(tab_id)
                if frame_id is not None:
                    self._schedule_frame_refresh(tab_id, frame_id, rediscover=True)
            # PR3: a default-context creation releases the Runtime.enable barrier.
            elif method == "Runtime.executionContextCreated":
                ctx = params.get("context") or {}
                aux = ctx.get("auxData") or {} if isinstance(ctx, dict) else {}
                if isinstance(aux, dict) and aux.get("isDefault"):
                    self._resolve_context_waiters(tab_id)
            # PR3: rewrite the REAL Chrome main-frame id → the synthetic
            # targetId in events we forward, matching the rewrite applied to the
            # getFrameTree response (so Playwright's frame→session map stays
            # consistent and never throws "Frame has been detached").
            self._rewrite_event_frame_id(tab_id, method, params)
            sid = self._tab_sessions.get(tab_id)
            if self._session_id is not None and sid is None:
                return
            out: dict[str, Any] = {"method": method, "params": params}
            if sid is not None:
                out["sessionId"] = sid
            await self._send_to_client(json.dumps(out))
            for aux_sid, target_tab in list(self._aux_sessions.items()):
                if target_tab == tab_id:
                    await self._send_to_client(json.dumps({
                        "method": method, "params": params, "sessionId": aux_sid,
                    }))
            return

    # ---- helpers ---------------------------------------------------------

    async def _detach_aux_sessions(self, tab_id: int) -> None:
        for aux_sid, target_tab in list(self._aux_sessions.items()):
            if target_tab == tab_id:
                self._aux_sessions.pop(aux_sid, None)
                await self._send_to_client(json.dumps({
                    "method": "Target.detachedFromTarget",
                    "params": {"sessionId": aux_sid,
                               "targetId": f"ext-tab-{tab_id}"},
                }))

    async def _emit_target_detached(self, tab_id: int,
                                    sid: str | None) -> None:
        """Synthesize the CDP teardown pair for a gone tab —
        `Target.detachedFromTarget` then `Target.targetDestroyed` — exactly as
        real Chrome orders them. Used by both the relay `detached` fan-out and
        the daemon-initiated `Target.closeTarget` path."""
        await self._send_to_client(json.dumps({
            "method": "Target.detachedFromTarget",
            "params": {
                "sessionId": sid or "",
                "targetId": f"ext-tab-{tab_id}",
            },
        }))
        await self._send_to_client(json.dumps({
            "method": "Target.targetDestroyed",
            "params": {"targetId": f"ext-tab-{tab_id}"},
        }))

    def _rewrite_event_frame_id(self, tab_id: int, method: str,
                                params: dict) -> None:
        """In-place: swap the REAL Chrome main-frame id for the synthetic
        targetId (`ext-tab-<tab_id>`) in a forwarded page-domain event, so it
        agrees with the frame id we presented in `Page.getFrameTree`. Only the
        TOP frame is remapped; child/OOPIF frames keep their real ids."""
        real = self._tab_main_frame.get(tab_id)
        if not real:
            return
        synthetic = f"ext-tab-{tab_id}"
        # `params.frameId` (lifecycleEvent, frameStartedLoading, navigatedWithin
        # Document, …) and `params.frame.id`/`parentId` (frameNavigated,
        # frameAttached) and `params.context.auxData.frameId`
        # (executionContextCreated) are the carriers of the top-frame id.
        if params.get("frameId") == real:
            params["frameId"] = synthetic
        if params.get("parentFrameId") == real:
            params["parentFrameId"] = synthetic
        frame = params.get("frame")
        if isinstance(frame, dict):
            if frame.get("id") == real:
                frame["id"] = synthetic
            if frame.get("parentId") == real:
                frame["parentId"] = synthetic
        ctx = params.get("context")
        if isinstance(ctx, dict):
            aux = ctx.get("auxData")
            if isinstance(aux, dict) and aux.get("frameId") == real:
                aux["frameId"] = synthetic

    def _rewrite_command_frame_id(self, tab_id: int, params: dict) -> None:
        """In-place inverse of `_rewrite_event_frame_id`: a command Playwright
        sends scoped to the (synthetic) main frame id must be rewritten back to
        the REAL Chrome frame id before forwarding to chrome.debugger (e.g.
        `Page.createIsolatedWorld {frameId}`)."""
        real = self._tab_main_frame.get(tab_id)
        if not real:
            return
        synthetic = f"ext-tab-{tab_id}"
        if params.get("frameId") == synthetic:
            params["frameId"] = real

    def _evict_tab(self, tab_id: int) -> None:
        """Drop all per-tab state for a closed/detached tab and wake any
        outstanding Runtime.enable barrier so it doesn't hang on a dead tab."""
        self._tab_sessions.pop(tab_id, None)
        self._quiet_runtime_tabs.discard(tab_id)
        self._native_runtime_tabs.discard(tab_id)
        self._execution_contexts.pop(tab_id, None)
        self._pending_console_text.pop(tab_id, None)
        self._world_names.pop(tab_id, None)
        self._forget_tab_discoveries(tab_id)
        for sid in [sid for sid, target in self._aux_sessions.items() if target == tab_id]:
            self._aux_sessions.pop(sid, None)
        self._ext.evict_tab_sessions(tab_id)
        self._tab_url.pop(tab_id, None)
        self._fresh_blank_tabs.discard(tab_id)
        self._tab_main_frame.pop(tab_id, None)
        for fut in self._ctx_waiters.pop(tab_id, []):
            if not fut.done():
                fut.cancel()

    async def _send_to_client(self, frame: str) -> None:
        if self._closed:
            return
        with contextlib.suppress(Exception):
            await self._client.send(frame)

    async def _respond(self, req_id: int | None, result: dict,
                       *, session_id: str | None = None) -> None:
        frame: dict[str, Any] = {"id": req_id, "result": result}
        sid = session_id if session_id is not None else self._echo_sid
        if sid is not None:
            frame["sessionId"] = sid
        await self._send_to_client(json.dumps(frame))

    async def _error(self, req_id: int | None, code: int, msg: str,
                     *, session_id: str | None = None) -> None:
        frame: dict[str, Any] = {
            "id": req_id, "error": {"code": code, "message": msg},
        }
        sid = session_id if session_id is not None else self._echo_sid
        if sid is not None:
            frame["sessionId"] = sid
        await self._send_to_client(json.dumps(frame))

    async def _noop_close(self, reason: str) -> None:
        return
