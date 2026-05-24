"""Playwright facade ↔ extension backend bridge (Task #tab-handle-model, PR2).

PR1's `facade.py` is a byte-for-byte CDP passthrough to a resolved upstream ws.
That works for the rdp backend because the daemon-owned Chrome speaks real
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
    `Browser.getVersion` and the session-scoped `chrome.debugger` forwarding all
    already live there. This bridge constructs a *dedicated* `ExtensionUpstream`
    over the SAME shared `RelayServer` (so all those methods work unchanged) and
    only ADDS:
      * A2 — `Target.setAutoAttach`/`setDiscoverTargets`: ack + replay
        `Target.targetCreated` + `Target.attachedToTarget` for every connected
        tab (mirrors playwriter's relay replay). Also pushes these when a tab is
        opened/attached later (via a relay fan-out listener).
      * A3 — `Target.createTarget` → `RelayServer.create_background_tab`
        (the extension can't open browser-level targets), then synthesizes the
        created/attached events for the new tab.
      * A4 — `Runtime.enable` execution-context barrier: forward, then wait
        (bounded) for `Runtime.executionContextCreated` so Playwright doesn't
        race ahead of the main-frame default context.

`getTargets` scope policy: the agent path scopes `Target.getTargets` to a
session's tab group (so two sessions sharing one Chrome are mutually invisible).
The Playwright facade connection is session-LESS (a raw CDP client), so we use
the UNSCOPED `ExtensionUpstream.send_text` enumeration — Playwright should see
every attached tab, which is exactly what makes `context.pages()` enumerate.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from websockets.asyncio.server import ServerConnection

from .extension_upstream import (
    ExtensionUpstream,
    _new_upstream_session_id,
    _tab_id_from_target_id,
)
from .relay import RelayServer, _CommandError

logger = logging.getLogger(__name__)


# Bounded wait for the main-frame execution context after `Runtime.enable`
# (A4). playwriter waits ~3s; we match that order of magnitude. On timeout we
# still return the enable result — the barrier is best-effort robustness, not a
# correctness gate.
_RUNTIME_ENABLE_BARRIER_TIMEOUT = 3.0


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

    def __init__(self, *, client: ServerConnection, relay: RelayServer):
        self._client = client
        self._relay = relay
        # A dedicated ExtensionUpstream over the SAME relay. on_frame routes
        # synthesized/forwarded frames back to THIS Playwright client. on_close
        # is a no-op — the facade owns connection teardown, not the upstream.
        self._ext = ExtensionUpstream(
            relay=relay,
            on_frame=self._send_to_client,
            on_close=self._noop_close,
        )
        # tab_id → synthetic flat sessionId we've handed Playwright for it. One
        # entry per tab we've announced via attachedToTarget, so a later
        # `targetDestroyed`/`detachedFromTarget` references the same session and
        # forwarded extension events can be tagged with the right sessionId.
        self._tab_sessions: dict[int, str] = {}
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
        # NOTE: we deliberately do NOT call self._ext.close() — ExtensionUpstream
        # .close() calls relay.set_event_handler(None), which would clobber the
        # AGENT path's primary event handler. Our dedicated ExtensionUpstream
        # never called open() (so it never set the handler); it only lent us its
        # send_text emulation + session table. Tearing down our relay fan-out
        # listener above is the only relay-side cleanup we own.
        self._ext._open = False  # noqa: SLF001 — mark our adapter inert

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

        # --- Target.getTargetInfo: the handshake asks for the browser target ---
        # (no/unknown targetId) — synthesize it; a tab targetId returns that
        # tab's info. ExtensionUpstream errors here (it expects a sessionId), so
        # the facade answers directly.
        if method == "Target.getTargetInfo" and session_id is None:
            tid = params.get("targetId")
            tab_id = (_tab_id_from_target_id(tid)
                      if isinstance(tid, str) else None)
            if tab_id is not None:
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
            await self._handle_runtime_enable(req_id, session_id, params)
            return

        # --- Browser-level (session-less) Target/Browser emulation: reuse
        # ExtensionUpstream (getTargets / attachToTarget / detachFromTarget /
        # Browser.getVersion). Its responses carry no sessionId, which is
        # correct for these browser-level methods. ---
        if session_id is None:
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

    async def _forward_session_command(self, req_id: int | None,
                                       session_id: str, method: str | None,
                                       params: dict) -> None:
        """Forward a session-scoped command to the tab's chrome.debugger and
        echo `{id, sessionId, result|error}`."""
        tab_id = self._ext._sessions.get(session_id)  # noqa: SLF001
        if tab_id is None:
            from .extension_upstream import _tab_id_from_session_id
            tab_id = _tab_id_from_session_id(session_id)
        if tab_id is None:
            await self._error(req_id, -32602,
                              f"unknown sessionId {session_id!r}",
                              session_id=session_id)
            return
        # A few session-scoped browser-discovery methods are silent-acked the
        # same way the agent path does (the extension can't honor child
        # auto-attach), so Playwright's per-page setup doesn't stall.
        if method in ("Target.setAutoAttach", "Target.setDiscoverTargets"):
            await self._respond(req_id, {}, session_id=session_id)
            return
        try:
            result = await self._relay.send_cdp(tab_id, method or "", params)
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
        # Reuse an already-announced session for this tab if we have one, so
        # auto-attach replay + an explicit attachToTarget agree on one session.
        if tab_id is not None and tab_id in self._tab_sessions:
            sid = self._tab_sessions[tab_id]
            # Re-register the sid in the upstream's table (it may differ from
            # ours if this is the first explicit attach) so session-scoped
            # commands resolve the tab.
            self._ext._sessions[sid] = tab_id  # noqa: SLF001 — intra-package
            try:
                await self._relay.attach_tab(tab_id, timeout=10.0)
            except _CommandError as e:
                await self._error(req_id, e.code, e.message)
                return
            except Exception as e:  # noqa: BLE001
                await self._error(req_id, -32603, f"attach failed: {e!r}")
                return
            await self._respond(req_id, {"sessionId": sid})
            await self._announce_target(tab_id, sid=sid, send_created=False)
            return
        # Unknown tab → let ExtensionUpstream do the attach + sid fabrication,
        # then snoop its table to learn the sid it handed back.
        await self._ext.send_text(json.dumps({
            "id": req_id, "method": "Target.attachToTarget", "params": params,
        }))
        if tab_id is not None:
            sid = next((s for s, t in self._ext._sessions.items()  # noqa: SLF001
                        if t == tab_id), None)
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
            # Same primitive shape the agent `open_background` verb uses.
            gt = await self._relay.create_background_tab(
                url, group_name="Agent", group_id=None, background=True)
            # Announce BEFORE the response (Chrome ordering Playwright relies on).
            await self._announce_target(gt.tab_id, send_created=True)
        except Exception as e:  # noqa: BLE001
            await self._error(req_id, -32603,
                              f"createTarget→createTab failed: {e!r}")
            return
        finally:
            self._creating -= 1
        await self._respond(req_id, {"targetId": gt.target_id})

    async def _handle_close_target(self, req_id: int | None,
                                   params: dict) -> None:
        """Browser-level Target.closeTarget → close the tab via the relay
        (derive tabId from the ext-tab-<id> targetId; no session needed)."""
        target_id = params.get("targetId")
        tab_id = (_tab_id_from_target_id(target_id)
                  if isinstance(target_id, str) else None)
        if tab_id is None:
            # CDP returns success:false for an unknown target rather than erroring.
            await self._respond(req_id, {"success": False})
            return
        try:
            await self._relay.close_tab(tab_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("facade(ext) closeTarget tab %s failed: %r", tab_id, e)
        # Evict local + upstream session state for the closed tab.
        sid = self._tab_sessions.pop(tab_id, None)
        if sid:
            self._ext._sessions.pop(sid, None)  # noqa: SLF001
        await self._respond(req_id, {"success": True})

    async def _handle_runtime_enable(self, req_id: int | None,
                                     session_id: str, params: dict) -> None:
        """A4: forward Runtime.enable for the session's tab, then wait (bounded)
        for the main-frame `Runtime.executionContextCreated` before returning,
        so Playwright doesn't race ahead of the default execution context.

        We can't intercept the event stream cheaply here (events flow async via
        the relay fan-out → _on_relay_event), so we forward the command and add
        a small settle delay bounded by the barrier timeout. The extension's
        `Runtime.enable` itself replays `executionContextCreated` for existing
        contexts, which _on_relay_event forwards to the client; the bounded wait
        just gives that replay a beat to land before Playwright's next call."""
        tab_id = self._ext._sessions.get(session_id)  # noqa: SLF001
        if tab_id is None:
            from .extension_upstream import _tab_id_from_session_id
            tab_id = _tab_id_from_session_id(session_id)
        if tab_id is None:
            await self._error(req_id, -32602,
                              f"unknown sessionId {session_id!r}",
                              session_id=session_id)
            return
        try:
            result = await self._relay.send_cdp(
                tab_id, "Runtime.enable", params)
        except _CommandError as e:
            await self._error(req_id, e.code, e.message, session_id=session_id)
            return
        except Exception as e:  # noqa: BLE001
            await self._error(req_id, -32603, f"Runtime.enable failed: {e!r}",
                              session_id=session_id)
            return
        # Bounded settle so the extension's executionContextCreated replay
        # (forwarded via _on_relay_event) reaches the client before we ack.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                self._await_default_context(tab_id),
                timeout=_RUNTIME_ENABLE_BARRIER_TIMEOUT,
            )
        await self._respond(req_id, result, session_id=session_id)

    async def _await_default_context(self, tab_id: int) -> None:
        """Best-effort barrier: a short sleep that yields control so any
        in-flight executionContextCreated frames get pumped. Kept trivial — the
        true correctness comes from the extension replaying the context on
        Runtime.enable; this only avoids returning the ack on the very same
        tick the command was sent."""
        await asyncio.sleep(0.05)

    # ---- target event synthesis (A2) ------------------------------------

    async def _replay_all_targets(self) -> None:
        """Replay targetCreated + attachedToTarget for EVERY currently-known
        tab — the playwriter relay behavior that makes context.pages()
        enumerate. Unscoped on purpose (the facade connection is session-less;
        Playwright should see all attached tabs)."""
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
                sid = self._tab_sessions.get(tab_id) or _new_upstream_session_id(tab_id)
            self._tab_sessions[tab_id] = sid
            # Keep the upstream's session table in sync so session-scoped
            # commands Playwright sends for this sid resolve to the tab.
            self._ext._sessions[sid] = tab_id  # noqa: SLF001
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

    def _browser_target_info(self) -> dict:
        """Synthetic targetInfo for the browser itself (type=browser). Some
        handshake steps query it before any page target exists."""
        return {
            "targetId": "browserwright-extension-browser",
            "type": "browser",
            "title": "Browserwright",
            "url": "",
            "attached": True,
            "canAccessOpener": False,
            "browserContextId": "",
        }

    def _target_info(self, tab_id: int) -> dict:
        """Build a CDP targetInfo from the relay's current ghost view."""
        url = ""
        title = ""
        for g in self._relay.list_ghost_targets():
            if _tab_id_from_target_id(g.target_id) == tab_id:
                url = g.url
                title = g.title
                break
        return {
            "targetId": f"ext-tab-{tab_id}",
            "type": "page",
            "title": title,
            "url": url,
            "attached": True,
            "canAccessOpener": False,
            # Playwright's `_onAttachedToTarget` asserts a TRUTHY browserContextId
            # and looks it up in its known contexts, falling back to the default
            # context when not found. A stable synthetic id satisfies the assert
            # and routes the page into Playwright's default context (the
            # extension backend has no real browser contexts — P4).
            "browserContextId": _SYNTHETIC_BROWSER_CONTEXT_ID,
        }

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
            await self._announce_target(tab_id, send_created=True)
            return

        if kind == "detached":
            sid = self._tab_sessions.pop(tab_id, None)
            if sid:
                self._ext._sessions.pop(sid, None)  # noqa: SLF001
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
            return

        if kind == "event":
            method = ext_msg.get("method")
            params = ext_msg.get("params") or {}
            if not isinstance(method, str):
                return
            sid = self._tab_sessions.get(tab_id)
            out: dict[str, Any] = {"method": method, "params": params}
            if sid is not None:
                out["sessionId"] = sid
            await self._send_to_client(json.dumps(out))
            return

    # ---- helpers ---------------------------------------------------------

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
